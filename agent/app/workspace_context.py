"""Short workspace summary injected into the agent system prompt."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import settings
from .github_prefs import detect_repo_from_workspace, effective_github_repo


def _has_git(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(root),
            capture_output=True,
            timeout=10,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def workspace_context_block() -> str:
    root = Path(settings.workspace_root).resolve()
    parts: list[str] = [f"Mounted workspace: `{root}`"]

    if not root.is_dir():
        parts.append(
            "(Workspace path is missing or not a directory — file tools may fail until the mount is fixed.)"
        )
        return "\n".join(parts)

    top: list[str] = []
    try:
        top = sorted(
            p.name
            for p in root.iterdir()
            if not p.name.startswith(".") and p.name not in {".agent"}
        )[:40]
        if top:
            parts.append("Top-level entries: " + ", ".join(top))
    except OSError:
        pass

    is_git = _has_git(root)
    if not top and not is_git:
        parts.append(
            "(Workspace folder is EMPTY on disk — you do NOT see the same files as github.com until the repo is cloned into this mount.)"
        )
    elif not is_git and top:
        parts.append("(Folder has files but is NOT a git repository.)")

    readme = root / "README.md"
    if readme.is_file():
        try:
            excerpt = readme.read_text(encoding="utf-8", errors="replace")[:1200].strip()
            parts.append(f"README excerpt:\n{excerpt}")
        except OSError:
            pass

    origin = detect_repo_from_workspace()
    gh = effective_github_repo()
    if gh and origin and gh.lower() != origin.lower():
        parts.append(
            f"MISMATCH: GitHub picker is `{gh}` but workspace origin is `{origin}`."
        )
    if gh:
        parts.append(f"GitHub repo for issues/PR/API: `{gh}`")
    elif origin:
        parts.append(f"Git origin suggests repo: `{origin}`")

    return "\n".join(parts)
