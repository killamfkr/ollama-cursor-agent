"""Runtime GitHub repo selection (UI) with optional env override."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import settings

_STATE_NAME = "github.json"
_GITHUB_REMOTE = re.compile(
    r"github\.com[/:](?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+?)(?:\.git)?(?:/|$)"
)


def data_dir() -> Path:
    root = Path(settings.data_dir).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root.resolve()


def _state_path() -> Path:
    return data_dir() / _STATE_NAME


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_selected_repo(full_name: str) -> str:
    full_name = full_name.strip()
    if not full_name or "/" not in full_name:
        raise ValueError("Repo must be owner/name")
    owner, name = full_name.split("/", 1)
    if not owner or not name:
        raise ValueError("Invalid repo name")
    state = load_state()
    state["selected_repo"] = f"{owner}/{name}"
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state["selected_repo"]


def selected_repo_from_store() -> str:
    return (load_state().get("selected_repo") or "").strip()


def detect_repo_from_workspace() -> str:
    root = Path(settings.workspace_root).resolve()
    if not root.is_dir():
        return ""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    url = proc.stdout.strip()
    match = _GITHUB_REMOTE.search(url)
    if not match:
        return ""
    return f"{match.group('owner')}/{match.group('repo')}"


def effective_github_repo() -> str:
    env_repo = settings.github_repo.strip()
    if env_repo and "/" in env_repo:
        return env_repo
    stored = selected_repo_from_store()
    if stored and "/" in stored:
        return stored
    return detect_repo_from_workspace()


def token_configured() -> bool:
    return bool(settings.github_token.strip())


def github_env_repo_locked() -> bool:
    return bool(settings.github_repo.strip() and "/" in settings.github_repo.strip())
