"""Sandboxed tool implementations for the coding agent."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import settings
from .git_ops import (
    default_feature_branch,
    git_checkout_branch,
    git_commit,
    git_fetch,
    git_push,
    git_status,
)
from .github_client import GitHubClient, GitHubError
from .github_prefs import effective_github_repo, token_configured
from .workspace_sync import ensure_workspace_has_repo


def _resolve_safe(path: str) -> Path:
    root = Path(settings.workspace_root).resolve()
    target = (root / path.lstrip("/")).resolve()
    if not str(target).startswith(str(root)):
        raise PermissionError(f"Path escapes workspace: {path}")
    return target


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_directory",
        "description": "List files and directories at a path relative to the workspace root.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path (default '.')",
                }
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file. Optionally limit lines.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path"},
                "offset": {
                    "type": "integer",
                    "description": "1-based start line (default 1)",
                },
                "limit": {"type": "integer", "description": "Max lines to return"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file relative to the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "search_files",
        "description": "Search file contents with a regex pattern (ripgrep-like).",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": "Relative directory to search (default '.')",
                },
                "glob": {"type": "string", "description": "Optional glob filter"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob_files",
        "description": "Find files matching a glob pattern under the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob like **/*.py",
                },
                "path": {"type": "string", "description": "Base directory (default '.')"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a shell command in the workspace directory. Use for builds, tests, git, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "description": {
                    "type": "string",
                    "description": "Short note on what this command does",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "github_readme",
        "description": (
            "Fetch README from the configured GitHub repo via API (when workspace is empty "
            "or clone failed). Use before claiming you cannot see the project."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "github_get_issue",
        "description": "Fetch a GitHub issue (title, body, comments) for the configured repo.",
        "parameters": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Issue number"},
            },
            "required": ["number"],
        },
    },
    {
        "name": "github_post_comment",
        "description": "Post a comment on a GitHub issue or PR by number.",
        "parameters": {
            "type": "object",
            "properties": {
                "number": {"type": "integer"},
                "body": {"type": "string"},
            },
            "required": ["number", "body"],
        },
    },
    {
        "name": "github_create_pull_request",
        "description": "Open a pull request on GitHub (after pushing the branch).",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "head_branch": {
                    "type": "string",
                    "description": "Branch you pushed (defaults to current git branch)",
                },
                "base_branch": {
                    "type": "string",
                    "description": "Target branch (default from config, usually main)",
                },
                "draft": {"type": "boolean"},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "clone_workspace_repo",
        "description": (
            "Clone the GitHub repo selected in the UI (or AGENT_GITHUB_REPO) into the "
            "empty workspace mount. Call this when list_directory is empty but a repo is configured — "
            "do NOT ask the user for a URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Optional owner/name override; default is the UI-selected repo",
                }
            },
        },
    },
    {
        "name": "git_status",
        "description": "Show current git branch and uncommitted changes.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "git_checkout_branch",
        "description": "Create or checkout a feature branch (use cursor/ prefix style names).",
        "parameters": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch name; omit to auto-generate from task title",
                },
                "title_hint": {
                    "type": "string",
                    "description": "Used to auto-name branch when branch omitted",
                },
            },
        },
    },
    {
        "name": "git_commit",
        "description": "Stage and commit changes. Use paths to limit staged files.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional paths to stage; default all changes",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "git_push",
        "description": "Push current (or named) branch to origin on GitHub.",
        "parameters": {
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
            },
        },
    },
]


def _list_directory(path: str = ".") -> str:
    target = _resolve_safe(path)
    if not target.exists():
        return json.dumps({"error": f"Not found: {path}"})
    if not target.is_dir():
        return json.dumps({"error": f"Not a directory: {path}"})
    entries = []
    for child in sorted(target.iterdir()):
        kind = "dir" if child.is_dir() else "file"
        try:
            size = child.stat().st_size if child.is_file() else None
        except OSError:
            size = None
        rel = child.relative_to(Path(settings.workspace_root).resolve())
        entries.append({"name": child.name, "path": str(rel), "type": kind, "size": size})
    payload: dict[str, Any] = {"path": path, "entries": entries}
    if not entries and path in (".", ""):
        repo = effective_github_repo()
        if repo and token_configured():
            payload["hint"] = (
                f"Workspace is empty on disk. GitHub repo `{repo}` is selected — "
                "call clone_workspace_repo (do not ask for a URL)."
            )
        else:
            payload["hint"] = (
                "Workspace is empty. User must pick a repo in the GitHub sidebar and "
                "click Clone into workspace, or set AGENT_GITHUB_TOKEN."
            )
    return json.dumps(payload, indent=2)


def _read_file(path: str, offset: int = 1, limit: int | None = None) -> str:
    target = _resolve_safe(path)
    if not target.is_file():
        return json.dumps({"error": f"Not a file: {path}"})
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return json.dumps({"error": str(e)})
    lines = text.splitlines()
    start = max(1, offset) - 1
    end = len(lines) if limit is None else min(len(lines), start + limit)
    numbered = [f"{i + 1:6}|{lines[i]}" for i in range(start, end)]
    return "\n".join(numbered) if numbered else "(empty file)"


def _write_file(path: str, content: str) -> str:
    target = _resolve_safe(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return json.dumps({"ok": True, "path": path, "bytes": len(content.encode("utf-8"))})


def _search_files(pattern: str, path: str = ".", glob: str | None = None) -> str:
    base = _resolve_safe(path)
    if not base.exists():
        return json.dumps({"error": f"Not found: {path}"})
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex: {e}"})
    matches: list[dict[str, Any]] = []
    root = Path(settings.workspace_root).resolve()

    def scan_file(fp: Path) -> None:
        if len(matches) >= 80:
            return
        try:
            if fp.stat().st_size > 2_000_000:
                return
            for i, line in enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    matches.append(
                        {
                            "path": str(fp.relative_to(root)),
                            "line": i,
                            "text": line[:300],
                        }
                    )
                    if len(matches) >= 80:
                        return
        except OSError:
            pass

    if base.is_file():
        scan_file(base)
    else:
        for fp in base.rglob("*"):
            if not fp.is_file():
                continue
            if glob and not fp.match(glob):
                continue
            scan_file(fp)
            if len(matches) >= 80:
                break

    return json.dumps({"pattern": pattern, "match_count": len(matches), "matches": matches}, indent=2)


def _glob_files(pattern: str, path: str = ".") -> str:
    base = _resolve_safe(path)
    root = Path(settings.workspace_root).resolve()
    if not base.exists():
        return json.dumps({"error": f"Not found: {path}"})
    found = sorted(str(p.relative_to(root)) for p in base.glob(pattern))[:200]
    return json.dumps({"pattern": pattern, "files": found}, indent=2)


async def _run_shell(command: str, description: str | None = None) -> str:
    root = Path(settings.workspace_root).resolve()
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HOME": str(root), "CI": "1"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.shell_timeout_sec
        )
    except asyncio.TimeoutError:
        proc.kill()
        return json.dumps(
            {
                "error": "timeout",
                "command": command,
                "description": description,
            }
        )
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    if len(out) > 50_000:
        out = out[:50_000] + "\n... (truncated)"
    if len(err) > 20_000:
        err = err[:20_000] + "\n... (truncated)"
    return json.dumps(
        {
            "exit_code": proc.returncode,
            "stdout": out,
            "stderr": err,
            "command": command,
            "description": description,
        },
        indent=2,
    )


async def _list_directory_maybe_clone(path: str = ".") -> str:
    raw = _list_directory(path)
    if path not in (".", ""):
        return raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if data.get("entries"):
        return raw
    repo = effective_github_repo()
    if not repo or not token_configured():
        return raw
    clone = await ensure_workspace_has_repo(repo)
    data["clone_attempt"] = clone
    if clone.get("ok"):
        data = json.loads(_list_directory(path))
        data["cloned"] = True
        data["clone"] = {"action": clone.get("action"), "repo": clone.get("repo", repo)}
    return json.dumps(data, indent=2)


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    from .tool_calls import resolve_tool_name

    name = resolve_tool_name(name)
    try:
        if name == "list_directory":
            return await _list_directory_maybe_clone(arguments.get("path", "."))
        if name == "read_file":
            return _read_file(
                arguments["path"],
                int(arguments.get("offset", 1)),
                int(arguments["limit"]) if arguments.get("limit") is not None else None,
            )
        if name == "write_file":
            return _write_file(arguments["path"], arguments["content"])
        if name == "search_files":
            pattern = arguments.get("pattern") or arguments.get("query") or "."
            return _search_files(
                pattern,
                arguments.get("path", "."),
                arguments.get("glob"),
            )
        if name == "glob_files":
            return _glob_files(arguments["pattern"], arguments.get("path", "."))
        if name == "run_shell":
            return await _run_shell(
                arguments["command"], arguments.get("description")
            )
        if name == "github_readme":
            try:
                gh = GitHubClient()
                return json.dumps(await gh.get_readme(), indent=2)
            except GitHubError as e:
                return json.dumps({"error": e.message, "status": e.status})
        if name == "github_get_issue":
            try:
                number = int(arguments.get("number") or 0)
                if number < 1:
                    return json.dumps(
                        {
                            "error": "Issue number must be >= 1. Ask the user which GitHub issue to use, or use the sidebar Run on issue # control.",
                        }
                    )
                gh = GitHubClient()
                issue = await gh.get_issue(number)
                return json.dumps(issue, indent=2)
            except GitHubError as e:
                return json.dumps({"error": e.message, "status": e.status})
        if name == "github_post_comment":
            gh = GitHubClient()
            res = await gh.create_comment(int(arguments["number"]), arguments["body"])
            return json.dumps(
                {"ok": True, "html_url": res.get("html_url"), "id": res.get("id")}
            )
        if name == "github_create_pull_request":
            gh = GitHubClient()
            head = arguments.get("head_branch")
            if not head:
                st = await git_status()
                head = st["branch"]
            pr = await gh.create_pull_request(
                title=arguments["title"],
                body=arguments["body"],
                head=head,
                base=arguments.get("base_branch"),
                draft=bool(arguments.get("draft", False)),
            )
            return json.dumps(
                {
                    "number": pr.get("number"),
                    "html_url": pr.get("html_url"),
                    "title": pr.get("title"),
                    "head": head,
                },
                indent=2,
            )
        if name == "clone_workspace_repo":
            repo = (arguments.get("repo") or effective_github_repo() or "").strip()
            if not repo:
                return json.dumps(
                    {
                        "error": "no_repo_selected",
                        "hint": "Pick a repository in the GitHub sidebar of the web UI.",
                    }
                )
            if not token_configured():
                return json.dumps(
                    {
                        "error": "no_token",
                        "hint": "Set AGENT_GITHUB_TOKEN on the container.",
                    }
                )
            result = await ensure_workspace_has_repo(repo)
            return json.dumps(result, indent=2)
        if name == "git_status":
            return json.dumps(await git_status(), indent=2)
        if name == "git_checkout_branch":
            branch = arguments.get("branch")
            if not branch:
                hint = arguments.get("title_hint") or "agent-task"
                branch = default_feature_branch(hint)
            await git_fetch()
            st = await git_checkout_branch(branch)
            return json.dumps({"branch": branch, **st}, indent=2)
        if name == "git_commit":
            paths = arguments.get("paths")
            res = await git_commit(arguments["message"], paths=paths)
            return json.dumps(res, indent=2)
        if name == "git_push":
            res = await git_push(arguments.get("branch"))
            return json.dumps(res, indent=2)
        return json.dumps({"error": f"Unknown tool: {name}"})
    except PermissionError as e:
        return json.dumps({"error": str(e)})
    except KeyError as e:
        return json.dumps({"error": f"Missing argument: {e}"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


def tools_for_ollama() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOL_DEFINITIONS
    ]
