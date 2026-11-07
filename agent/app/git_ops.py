from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import settings
from .github_prefs import effective_github_repo


def workspace_root() -> Path:
    return Path(settings.workspace_root).resolve()


def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len] or "task").strip("-")


def default_feature_branch(title: str) -> str:
    slug = _slugify(title)
    suffix = settings.github_branch_suffix.strip() or "local"
    prefix = settings.github_branch_prefix.strip() or "cursor/"
    if not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}{slug}-{suffix}"


def _authenticated_url(owner: str, repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


async def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    root = workspace_root()
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HOME": str(root), "GIT_TERMINAL_PROMPT": "0"},
    )
    stdout, stderr = await proc.communicate()
    text_out = stdout.decode("utf-8", errors="replace")
    text_err = stderr.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(text_err.strip() or text_out.strip() or f"git exit {proc.returncode}")
    return subprocess.CompletedProcess(args, proc.returncode, text_out, text_err)


async def ensure_git_identity() -> None:
    try:
        await _run_git("config", "user.email", "ollama-agent@users.noreply.github.com")
        await _run_git("config", "user.name", "Ollama Agent")
    except RuntimeError:
        pass


async def _workspace_is_git_repo() -> bool:
    try:
        await _run_git("rev-parse", "--git-dir")
        return True
    except RuntimeError:
        return False


async def configure_origin_from_settings() -> dict[str, Any]:
    token = settings.github_token.strip()
    repo = effective_github_repo()
    if not token or not repo or "/" not in repo:
        return {"configured": False, "reason": "missing token or repo"}
    if not await _workspace_is_git_repo():
        return {"configured": False, "reason": "workspace is not a git repository"}
    owner, name = repo.split("/", 1)
    url = _authenticated_url(owner, name, token)
    try:
        await _run_git("remote", "get-url", "origin")
        await _run_git("remote", "set-url", "origin", url)
    except RuntimeError:
        try:
            await _run_git("remote", "add", "origin", url)
        except RuntimeError as e:
            return {"configured": False, "reason": str(e)}
    return {"configured": True, "remote": f"https://github.com/{repo}.git"}


async def git_status() -> dict[str, Any]:
    branch = (await _run_git("rev-parse", "--abbrev-ref", "HEAD")).stdout.strip()
    status = (await _run_git("status", "--porcelain")).stdout
    lines = [ln for ln in status.splitlines() if ln.strip()]
    return {"branch": branch, "dirty": bool(lines), "changes": lines[:80]}


async def git_fetch() -> str:
    await configure_origin_from_settings()
    await _run_git("fetch", "origin")
    return "ok"


async def git_checkout_branch(branch: str, *, create: bool = True) -> dict[str, Any]:
    branch = branch.strip()
    if not re.match(r"^[a-zA-Z0-9_./-]+$", branch):
        raise ValueError("Invalid branch name")
    await configure_origin_from_settings()
    base = settings.github_default_branch
    try:
        await _run_git("rev-parse", "--verify", branch)
        await _run_git("checkout", branch)
    except RuntimeError:
        if create:
            try:
                await _run_git("fetch", "origin", base)
            except RuntimeError:
                pass
            try:
                await _run_git("checkout", "-b", branch, f"origin/{base}")
            except RuntimeError:
                await _run_git("checkout", "-b", branch)
        else:
            raise
    return await git_status()


async def git_commit(message: str, paths: list[str] | None = None) -> dict[str, Any]:
    if paths:
        for p in paths:
            await _run_git("add", "--", p)
    else:
        await _run_git("add", "-A")
    await _run_git("commit", "-m", message)
    sha = (await _run_git("rev-parse", "HEAD")).stdout.strip()
    return {"committed": True, "sha": sha, "message": message}


async def git_push(branch: str | None = None, set_upstream: bool = True) -> dict[str, Any]:
    await configure_origin_from_settings()
    branch = branch or (await _run_git("rev-parse", "--abbrev-ref", "HEAD")).stdout.strip()
    args = ["push"]
    if set_upstream:
        args.extend(["-u", "origin", branch])
    else:
        args.extend(["origin", branch])
    await _run_git(*args)
    return {"pushed": True, "branch": branch}
