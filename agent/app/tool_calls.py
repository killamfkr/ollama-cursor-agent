"""Normalize and recover tool calls from Ollama / weaker tool models."""

from __future__ import annotations

import json
import re
from typing import Any

TOOL_ALIASES: dict[str, str] = {
    "git_init": "clone_workspace_repo",
    "git_initialize": "clone_workspace_repo",
    "initialize_repository": "clone_workspace_repo",
    "clone_repo": "clone_workspace_repo",
    "github_clone": "clone_workspace_repo",
}


def resolve_tool_name(name: str) -> str:
    n = (name or "").strip()
    return TOOL_ALIASES.get(n, n)


def normalize_tool_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    name = resolve_tool_name(name)
    if not isinstance(args, dict):
        args = {}

    # Some models nest args under "parameters"
    if isinstance(args.get("parameters"), dict):
        merged = dict(args["parameters"])
        for key, val in args.items():
            if key not in ("parameters", "name"):
                merged[key] = val
        args = merged

    # Flatten single-key wrappers
    if name in args and len(args) == 1:
        inner = args[name]
        if isinstance(inner, dict):
            args = inner

    if name == "search_files" and not args.get("pattern"):
        args = dict(args)
        args["pattern"] = args.pop("query", None) or args.pop("regex", None) or "."
    if name == "glob_files" and not args.get("pattern"):
        args = dict(args)
        args["pattern"] = "**/*"

    if name == "list_directory" and "path" not in args:
        args = dict(args)
        args["path"] = "."

    if name == "github_get_issue" and "number" in args:
        try:
            n = int(args["number"])
        except (TypeError, ValueError):
            n = 0
        if n < 1:
            return {}  # execute_tool reroutes to describe_project

    return args


def maybe_reroute_overview_tool(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if name == "github_get_issue":
        try:
            n = int(args.get("number") or 0)
        except (TypeError, ValueError):
            n = 0
        if n < 1:
            return "describe_project", {}
    return name, args


def _args_from_function(fn: dict[str, Any]) -> dict[str, Any]:
    raw = fn.get("arguments", fn.get("parameters", "{}"))
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        return raw
    return {}


def ollama_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return [{name, arguments}] from an assistant message."""
    out: list[dict[str, Any]] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or tc
        name = resolve_tool_name(fn.get("name") or tc.get("name") or "")
        if not name:
            continue
        args = _args_from_function(fn)
        args = normalize_tool_arguments(name, args)
        name, args = maybe_reroute_overview_tool(name, args)
        out.append({"name": name, "arguments": args})

    if out:
        return out

    content = (message.get("content") or "").strip()
    if not content:
        return out

    # Whole message is a single JSON tool call
    if content.startswith("{"):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and parsed.get("name"):
                nm = resolve_tool_name(str(parsed["name"]))
                args = parsed.get("parameters") or parsed.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if isinstance(args, dict):
                    out.append(
                        {
                            "name": nm,
                            "arguments": normalize_tool_arguments(nm, args),
                        }
                    )
                    return out
        except json.JSONDecodeError:
            pass

    # JSON object embedded in prose
    for m in re.finditer(
        r'\{[^{}]*"name"\s*:\s*"([a-zA-Z0-9_]+)"[^{}]*(?:\{[^{}]*\}[^{}]*)*\}',
        content,
        flags=re.DOTALL,
    ):
        try:
            parsed = json.loads(m.group(0))
            if parsed.get("name"):
                nm = resolve_tool_name(str(parsed["name"]))
                args = parsed.get("parameters") or parsed.get("arguments") or {}
                if isinstance(args, dict):
                    out.append(
                        {
                            "name": nm,
                            "arguments": normalize_tool_arguments(nm, args),
                        }
                    )
        except json.JSONDecodeError:
            continue

    return out
