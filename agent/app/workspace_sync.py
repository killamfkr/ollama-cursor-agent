"""Keep the mounted workspace in sync with the GitHub repo selected in the UI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .config import settings
from .git_ops import _authenticated_url, configure_origin_from_settings
from .github_prefs import detect_repo_from_workspace, effective_github_repo


def workspace_root() -> Path:
    return Path(settings.workspace_root).resolve()


def _ignored_entry(name: str) -> bool:
    return name in {".agent", ".ollama-agent-state", ".DS_Store"}


async def _run_git(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    root = cwd or workspace_root()
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HOME": str(root), "GIT_TERMINAL_PROMPT": "0"},
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def workspace_has_git() -> bool:
    if not workspace_root().is_dir():
        return False
    code, _, _ = await _run_git("rev-parse", "--git-dir")
    return code == 0


async def workspace_status() -> dict[str, Any]:
    root = workspace_root()
    selected = effective_github_repo()
    origin = detect_repo_from_workspace()

    if not root.is_dir():
        return {
            "path": str(root),
            "exists": False,
            "empty": True,
            "is_git": False,
            "origin_repo": None,
            "selected_repo": selected or None,
            "synced": False,
            "hint": "Workspace directory missing — check Unraid Workspace mount.",
        }

    try:
        entries = [
            p.name
            for p in root.iterdir()
            if not _ignored_entry(p.name) and not p.name.startswith(".")
        ]
    except OSError as e:
        return {
            "path": str(root),
            "exists": True,
            "error": str(e),
            "selected_repo": selected or None,
        }

    is_git = await workspace_has_git()
    empty = len(entries) == 0 and not is_git
    synced = bool(selected and origin and selected.lower() == origin.lower())

    hint = None
    if selected and empty:
        hint = (
            f"GitHub repo {selected} is selected, but this folder is empty on disk. "
            "Unlike Cursor Cloud, the agent cannot see github.com in the browser — "
            'use "Clone into workspace" below or git clone on Unraid.'
        )
    elif selected and is_git and origin and not synced:
        hint = (
            f"Workspace origin is {origin} but you selected {selected}. "
            "Use an empty workspace mount or clone the selected repo."
        )
    elif selected and synced and entries:
        hint = f"Workspace matches {selected} ({len(entries)} top-level items)."

    return {
        "path": str(root),
        "exists": True,
        "empty": empty,
        "is_git": is_git,
        "top_level_count": len(entries),
        "top_level_sample": entries[:12],
        "origin_repo": origin or None,
        "selected_repo": selected or None,
        "synced": synced,
        "hint": hint,
    }


async def ensure_workspace_has_repo(repo: str | None = None) -> dict[str, Any]:
    """Clone or pull the selected GitHub repo into the workspace mount."""
    repo = (repo or effective_github_repo()).strip()
    if not repo or "/" not in repo:
        return {"ok": False, "action": "skipped", "reason": "no repo selected"}

    token = settings.github_token.strip()
    if not token:
        return {"ok": False, "action": "skipped", "reason": "AGENT_GITHUB_TOKEN not set"}

    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    owner, name = repo.split("/", 1)
    url = _authenticated_url(owner, name, token)

    status = await workspace_status()
    if status.get("is_git") and status.get("origin_repo", "").lower() == repo.lower():
        code, out, err = await _run_git("pull", "--ff-only", "origin")
        await configure_origin_from_settings()
        if code == 0:
            return {
                "ok": True,
                "action": "pulled",
                "repo": repo,
                "message": (out or err).strip() or "pulled",
            }
        return {"ok": False, "action": "pull_failed", "repo": repo, "error": err or out}

    if not status.get("empty"):
        return {
            "ok": False,
            "action": "blocked",
            "repo": repo,
            "error": "workspace_not_empty",
            "hint": status.get("hint")
            or "Mount an empty folder at /workspace, then clone again.",
            "sample": status.get("top_level_sample"),
        }

    code, out, err = await _run_git(
        "clone",
        "--depth",
        "1",
        url,
        ".",
        cwd=root,
    )
    if code != 0:
        return {
            "ok": False,
            "action": "clone_failed",
            "repo": repo,
            "error": (err or out).strip() or f"git exit {code}",
        }

    await configure_origin_from_settings()
    st = await workspace_status()
    return {
        "ok": True,
        "action": "cloned",
        "repo": repo,
        "workspace": st,
        "message": (out or "clone complete").strip(),
    }
