"""Short workspace summary injected into the agent system prompt."""

from __future__ import annotations

from pathlib import Path

from .config import settings
from .github_prefs import detect_repo_from_workspace, effective_github_repo


def workspace_context_block() -> str:
    root = Path(settings.workspace_root).resolve()
    parts: list[str] = [f"Mounted workspace: `{root}`"]

    if not root.is_dir():
        parts.append(
            "(Workspace path is missing or not a directory — file tools may fail until the mount is fixed.)"
        )
        return "\n".join(parts)

    try:
        top = sorted(
            p.name for p in root.iterdir() if not p.name.startswith(".")
        )[:40]
        if top:
            parts.append("Top-level entries: " + ", ".join(top))
    except OSError:
        pass

    readme = root / "README.md"
    if readme.is_file():
        try:
            excerpt = readme.read_text(encoding="utf-8", errors="replace")[:1200].strip()
            parts.append(f"README excerpt:\n{excerpt}")
        except OSError:
            pass

    origin = detect_repo_from_workspace()
    gh = effective_github_repo()
    if gh:
        parts.append(f"GitHub repo for issues/PR workflow: `{gh}`")
    elif origin:
        parts.append(f"Git origin suggests repo: `{origin}`")

    return "\n".join(parts)
