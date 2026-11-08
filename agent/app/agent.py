from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Awaitable

from .config import settings
from .ollama_client import OllamaChatError, OllamaClient
from .tools import execute_tool
from .tool_calls import ollama_tool_calls
from .workspace_context import workspace_context_block


def build_system_prompt() -> str:
    gh_note = ""
    from .github_prefs import effective_github_repo

    github_repo = effective_github_repo()
    if settings.github_token and github_repo:
        gh_note = f"""
GitHub is configured for `{github_repo}` (default branch: `{settings.github_default_branch}`).
For deliverable work, follow the Cursor-style git workflow:
- `git_checkout_branch` on `{settings.github_branch_prefix}...` before edits
- Commit with `git_commit`, push with `git_push`
- Open a PR with `github_create_pull_request` and comment on the issue with `github_post_comment`
Branch suffix convention: `{settings.github_branch_suffix}`.
"""
    ctx = workspace_context_block()
    return f"""You are a local coding agent (similar to Cursor Cloud Agents). You help users build and fix software inside a mounted workspace directory.

Context (what is mounted right now):
{ctx}

Behaviors:
- The user sees files on github.com in a browser; you only see the **mounted workspace folder**. If list_directory is empty but a GitHub repo is configured below, call **clone_workspace_repo** — never `git init` and never ask for a repository URL.
- Do not describe what the project *does* from the repo name alone; after clone, read **README.md** (or docs) and summarize.
- Answer conversational questions in plain language using README and list_directory/read_file.
- Use tools to inspect the repo before changing files; prefer minimal, focused edits.
- For search_files you MUST pass a regex `pattern` (example: `pattern`=\"FastAPI\"). For broad exploration use list_directory or glob_files.
- Only use GitHub tools when the user names an issue number or asks for PR/issue work. Never call github_get_issue without a positive issue number.
- When the task is a GitHub issue, implement through branch → commit → push → PR.
{gh_note}
Tools: list_directory, read_file, write_file, search_files, glob_files, run_shell,
clone_workspace_repo, git_status, git_checkout_branch, git_commit, git_push,
github_readme, github_get_issue, github_post_comment, github_create_pull_request.
All file paths are relative to the workspace root.

Call tools via native tool_calls (not JSON in the chat text).
"""


SYSTEM_PROMPT = build_system_prompt()


@dataclass
class AgentEvent:
    type: str
    data: dict[str, Any]
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Session:
    id: str
    title: str
    model: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)

    def append_event(self, type_: str, **data: Any) -> AgentEvent:
        ev = AgentEvent(type=type_, data=data)
        self.events.append(ev)
        return ev


class AgentStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    def create(self, model: str, title: str = "New task") -> Session:
        sid = str(uuid.uuid4())
        session = Session(
            id=sid,
            title=title,
            model=model,
            messages=[{"role": "system", "content": build_system_prompt()}],
        )
        self.sessions[sid] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)


store = AgentStore()

_INSPECT_PROMISE = re.compile(
    r"\b(i'll|i will|let me|i'm going to)\b.*\b(inspect|look at|check|explore|investigate)\b",
    re.IGNORECASE,
)


def _is_inspect_promise_only(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 500:
        return False
    if _INSPECT_PROMISE.search(t):
        return True
    low = t.lower()
    return ("inspect" in low or "repository" in low) and len(t.split()) < 35 and "?" not in t


async def run_agent_turn(
    session: Session,
    user_message: str,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
) -> str:
    ollama = OllamaClient()
    session.messages.append({"role": "user", "content": user_message})
    if on_event:
        await on_event(session.append_event("user_message", content=user_message))

    from .github_prefs import token_configured
    from .workspace_sync import ensure_workspace_has_repo, workspace_status

    ws = await workspace_status()
    if ws.get("empty") and ws.get("selected_repo") and token_configured():
        if on_event:
            await on_event(
                session.append_event(
                    "status",
                    phase="clone",
                    message=f"Workspace empty — cloning {ws['selected_repo']}…",
                )
            )
        clone = await ensure_workspace_has_repo(ws["selected_repo"])
        if clone.get("ok"):
            session.messages[0] = {"role": "system", "content": build_system_prompt()}
            if on_event:
                await on_event(
                    session.append_event(
                        "status",
                        phase="clone",
                        message=f"Cloned {clone.get('repo', ws['selected_repo'])} into workspace.",
                    )
                )
        else:
            err = clone.get("hint") or clone.get("error") or json.dumps(clone)
            if on_event:
                await on_event(
                    session.append_event(
                        "status",
                        phase="clone",
                        message=f"Auto-clone failed: {err}",
                    )
                )
            session.messages.append(
                {
                    "role": "user",
                    "content": (
                        f"System: workspace is empty and auto-clone failed ({err}). "
                        "Call clone_workspace_repo before other git commands."
                    ),
                }
            )

    final_text = ""

    for step in range(settings.max_agent_steps):
        if on_event:
            await on_event(session.append_event("thinking", step=step + 1))
            await on_event(
                session.append_event(
                    "status",
                    phase="ollama",
                    message=f"Asking {session.model} (step {step + 1} of {settings.max_agent_steps})…",
                )
            )

        try:
            response = await ollama.chat(session.messages, model=session.model, tools=True)
        except OllamaChatError as e:
            hint = (
                f"Ollama error ({e.status}): {e.message}. "
                "Try a tool-capable model (qwen2.5-coder, llama3.1) and update Ollama to 0.3+. "
                "If tools are unsupported, the agent retried without tools."
            )
            final_text = hint
            if on_event:
                await on_event(session.append_event("assistant_message", content=hint))
            break

        msg = response.get("message", {})
        content = msg.get("content") or ""
        calls = ollama_tool_calls(msg)

        # Weak models emit JSON tool calls in `content` instead of tool_calls — record
        # a clean assistant message so the next turn produces a human answer.
        if calls and not msg.get("tool_calls"):
            stored = dict(msg)
            stored["content"] = ""
            stored["tool_calls"] = [
                {
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["arguments"]),
                    }
                }
                for c in calls
            ]
            session.messages.append(stored)
        else:
            session.messages.append(msg)

        if calls:
            for call in calls:
                name = call["name"]
                args = call["arguments"]

                if on_event:
                    await on_event(
                        session.append_event("tool_call", name=name, arguments=args)
                    )
                    await on_event(
                        session.append_event(
                            "status",
                            phase="tool",
                            message=f"Running {name}…",
                        )
                    )

                result = await execute_tool(name, args)

                if on_event:
                    await on_event(
                        session.append_event(
                            "tool_result",
                            name=name,
                            result=result[:4000] if len(result) > 4000 else result,
                        )
                    )

                session.messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": result,
                    }
                )
            continue

        if content.strip() and not ollama_tool_calls(
            {"content": content, "tool_calls": []}
        ):
            if _is_inspect_promise_only(content) and step < settings.max_agent_steps - 1:
                if on_event:
                    await on_event(
                        session.append_event(
                            "status",
                            phase="continue",
                            message="Continuing — model must use tools before answering…",
                        )
                    )
                session.messages.append({"role": "assistant", "content": content})
                session.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Do not reply with intentions only. Use tools now "
                            "(list_directory path='.', read_file README.md) and then answer."
                        ),
                    }
                )
                continue
            final_text = content.strip()
            if on_event:
                await on_event(session.append_event("assistant_message", content=final_text))
            break

    if not final_text:
        final_text = "Reached the step limit without a final answer. Try a narrower task or increase AGENT_MAX_AGENT_STEPS."
        if on_event:
            await on_event(session.append_event("assistant_message", content=final_text))

    return final_text


async def stream_agent_turn(
    session: Session,
    user_message: str,
) -> AsyncIterator[AgentEvent]:
    import asyncio

    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

    async def on_event(ev: AgentEvent) -> None:
        await queue.put(ev)

    async def runner() -> None:
        try:
            await run_agent_turn(session, user_message, on_event=on_event)
        except Exception as e:
            await queue.put(
                session.append_event("assistant_message", content=f"Agent error: {e}")
            )
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())

    while True:
        ev = await queue.get()
        if ev is None:
            break
        yield ev
    await task
