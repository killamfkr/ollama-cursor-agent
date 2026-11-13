"""Keep the mounted workspace in sync with the GitHub repo selected in the UI."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import settings
from .git_ops import _authenticated_url, configure_origin_from_settings
from .github_prefs import detect_repo_from_workspace, effective_github_repo


def workspace_root() -> Path:
    return Path(settings.workspace_root).resolve()


def _ignored_entry(name: str) -> bool:
    return name in {".agent", ".ollama-agent-state", ".DS_Store"}


def _scrub_secrets(text: str) -> str:
    tok = settings.github_token.strip()
    if tok and tok in text:
        return text.replace(tok, "***")
    return text


def _make_tree_writable(path: Path) -> None:
    try:
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWUSR | stat.S_IRUSR)
                except OSError:
                    pass
            for name in dirs:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR)
                except OSError:
                    pass
        path.chmod(stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR)
    except OSError:
        pass


def _force_remove_path(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_dir() and not path.is_symlink():
            _make_tree_writable(path)
            shutil.rmtree(path)
        else:
            try:
                path.chmod(stat.S_IWUSR | stat.S_IRUSR)
            except OSError:
                pass
            path.unlink(missing_ok=True)
        return None
    except OSError as e1:
        try:
            subprocess.run(
                ["rm", "-rf", str(path)],
                check=True,
                capture_output=True,
                timeout=180,
            )
            return None
        except (subprocess.SubprocessError, OSError):
            return str(e1)


def _install_tree_into_workspace(src: Path, dest_root: Path) -> list[str]:
    errors: list[str] = []
    for item in sorted(src.iterdir(), key=lambda p: p.name):
        if _ignored_entry(item.name):
            continue
        target = dest_root / item.name
        if target.exists() or target.is_symlink():
            err = _force_remove_path(target)
            if target.exists():
                errors.append(f"Could not replace {item.name}: {err or 'still present'}")
                continue
        try:
            shutil.move(str(item), str(target))
        except OSError:
            try:
                if item.is_dir():
                    shutil.copytree(item, target)
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    shutil.copy2(item, target)
                    item.unlink(missing_ok=True)
            except OSError as e:
                errors.append(f"{item.name}: {e}")
    return errors


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
            'Use **Replace workspace & clone** (deletes files under /workspace) or mount an empty folder.'
        )
    elif selected and not synced and not empty:
        hint = (
            f"Selected {selected}, but /workspace has other project files "
            f"(origin: {origin or 'none'}). Use **Replace workspace & clone** to switch repos."
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


def _clear_workspace_sync() -> dict[str, Any]:
    """Delete everything under workspace root (keeps agent metadata in data_dir, not workspace)."""
    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []
    errors: list[str] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if _ignored_entry(child.name):
            continue
        err = _force_remove_path(child)
        if child.exists():
            errors.append(f"{child.name}: {err or 'remove failed'}")
        else:
            removed.append(child.name)
    return {"removed": removed, "errors": errors, "path": str(root)}


async def clear_workspace() -> dict[str, Any]:
    return await asyncio.to_thread(_clear_workspace_sync)


async def replace_workspace_with_repo(repo: str | None = None) -> dict[str, Any]:
    """Wipe /workspace and shallow-clone the selected GitHub repo (Cursor Cloud–style switch)."""
    repo = (repo or effective_github_repo()).strip()
    if not repo or "/" not in repo:
        return {"ok": False, "action": "skipped", "reason": "no repo selected"}

    token = settings.github_token.strip()
    if not token:
        return {"ok": False, "action": "skipped", "reason": "AGENT_GITHUB_TOKEN not set"}

    owner, name = repo.split("/", 1)
    url = _authenticated_url(owner, name, token)
    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)

    # Clone on container /tmp first — survives Unraid mounts that resist delete-before-clone.
    tmpdir = Path(tempfile.mkdtemp(prefix="oclone-", dir="/tmp"))
    try:
        code, out, err = await _run_git(
            "clone",
            "--depth",
            "1",
            url,
            ".",
            cwd=tmpdir,
        )
        if code != 0:
            msg = _scrub_secrets((err or out).strip() or f"git exit {code}")
            return {
                "ok": False,
                "action": "clone_failed",
                "repo": repo,
                "error": msg,
                "hint": (
                    "Check AGENT_GITHUB_TOKEN (repo scope), repo name, and that "
                    "the container can reach github.com."
                ),
            }

        cleared = await clear_workspace()
        install_errors = await asyncio.to_thread(
            _install_tree_into_workspace, tmpdir, root
        )

        await configure_origin_from_settings()
        st = await workspace_status()
        synced = bool(st.get("synced"))

        if not synced and not (root / ".git").is_dir():
            return {
                "ok": False,
                "action": "install_failed",
                "repo": repo,
                "error": "; ".join(install_errors or cleared.get("errors") or ["unknown"]),
                "cleared": cleared,
                "install_errors": install_errors,
                "hint": (
                    "Clone succeeded in /tmp but files could not be moved into /workspace. "
                    "Check workspace mount permissions on Unraid (Mode rw, not read-only)."
                ),
            }

        warnings: list[str] = []
        warnings.extend(cleared.get("errors") or [])
        warnings.extend(install_errors)

        return {
            "ok": True,
            "action": "replaced",
            "repo": repo,
            "cleared": cleared,
            "install_errors": install_errors,
            "warnings": warnings,
            "workspace": st,
            "synced": synced,
            "message": (
                f"Cloned {repo} into workspace"
                + (
                    f" ({len(warnings)} file warning(s) — see logs)"
                    if warnings
                    else ""
                )
                + ".",
            ),
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def ensure_workspace_has_repo(
    repo: str | None = None, *, force_replace: bool = False
) -> dict[str, Any]:
    """Clone or pull the selected GitHub repo into the workspace mount."""
    if force_replace:
        return await replace_workspace_with_repo(repo)
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
            or (
                "Workspace is not empty. Use **Replace workspace & clone** in the UI "
                "or mount an empty folder at /workspace."
            ),
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
