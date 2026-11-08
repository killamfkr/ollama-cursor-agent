from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from .config import settings
from .tools import tools_for_ollama

logger = logging.getLogger(__name__)


class OllamaChatError(Exception):
    def __init__(self, status: int, message: str, *, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body


def trim_messages_for_ollama(
    messages: list[dict[str, Any]], *, max_turns: int = 28
) -> list[dict[str, Any]]:
    if len(messages) <= max_turns:
        return messages
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return system + rest[-max_turns:]


def coerce_tool_arguments_json(raw: Any) -> str:
    """Ensure Ollama sees valid JSON in tool_calls[].function.arguments."""
    if isinstance(raw, dict):
        return json.dumps(raw)
    if not isinstance(raw, str) or not raw.strip():
        return "{}"
    s = raw.strip()
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass
    # Truncated object — close open strings/braces heuristically
    if s.startswith("{"):
        depth = 0
        in_str = False
        esc = False
        for ch in s:
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
        repaired = s
        if in_str:
            repaired += '"'
        repaired += "}" * max(0, depth)
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            pass
    return "{}"


def strip_assistant_tool_calls(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "assistant":
            out.append(m)
            continue
        row = {"role": "assistant", "content": str(m.get("content") or "")}
        if not row["content"] and m.get("tool_calls"):
            names = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or tc
                if fn.get("name"):
                    names.append(str(fn["name"]))
            if names:
                row["content"] = f"(called tools: {', '.join(names)})"
        out.append(row)
    return out


def normalize_ollama_assistant_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Fix assistant messages before they enter session history."""
    out: dict[str, Any] = {
        "role": "assistant",
        "content": str(msg.get("content") or ""),
    }
    tcs = msg.get("tool_calls")
    if not tcs:
        return out
    fixed: list[dict[str, Any]] = []
    for tc in tcs:
        fn = tc.get("function") or tc
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        fixed.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": coerce_tool_arguments_json(
                        fn.get("arguments", fn.get("parameters", "{}"))
                    ),
                },
            }
        )
    if fixed:
        out["tool_calls"] = fixed
        if out["content"].strip().startswith("{"):
            out["content"] = ""
    return out


def sanitize_messages_for_ollama(
    messages: list[dict[str, Any]], *, max_tool_content: int = 12000
) -> list[dict[str, Any]]:
    """Shape messages for Ollama /api/chat (tool_name required on tool results)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            body = str(m.get("content") or "")
            if len(body) > max_tool_content:
                body = (
                    body[:max_tool_content]
                    + f"\n…(truncated, {len(body)} chars total)"
                )
            out.append(
                {
                    "role": "tool",
                    "tool_name": str(m.get("tool_name") or "tool"),
                    "content": body,
                }
            )
            continue
        if role == "assistant":
            out.append(normalize_ollama_assistant_message(m))
            continue
        if role in ("user", "system"):
            out.append({"role": role, "content": str(m.get("content") or "")})
    return out


class OllamaClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            data = r.json()
            return [m["name"] for m in data.get("models", [])]

    async def pull_model(self, name: str) -> AsyncIterator[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/pull",
                json={"name": name, "stream": True},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.strip():
                        yield json.loads(line)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: bool = True,
        stream: bool = False,
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        clean = sanitize_messages_for_ollama(trim_messages_for_ollama(messages))
        payload: dict[str, Any] = {
            "model": model,
            "messages": clean,
            "stream": stream,
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
        if tools:
            payload["tools"] = tools_for_ollama()

        if stream:
            return self._chat_stream(payload)

        return await self._chat_once(payload, model=model, allow_tool_fallback=tools)

    async def _chat_once(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        allow_tool_fallback: bool,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=600.0) as client:
            r = await client.post(f"{self.base_url}/api/chat", json=payload)
            if r.status_code < 400:
                return r.json()

            body = r.text
            logger.warning(
                "Ollama chat %s for model %s: %s",
                r.status_code,
                model,
                body[:500],
            )

            if r.status_code == 400:
                fallbacks: list[dict[str, Any]] = []
                base = {k: v for k, v in payload.items() if k != "tools"}
                base["options"] = {**base.get("options", {}), "num_ctx": 4096}
                fallbacks.append(base)
                if allow_tool_fallback and "tools" in payload:
                    fallbacks.append(
                        {
                            **payload,
                            "options": {**payload.get("options", {}), "num_ctx": 4096},
                        }
                    )
                trimmed = trim_messages_for_ollama(payload["messages"], max_turns=16)
                fallbacks.append(
                    {
                        **base,
                        "messages": sanitize_messages_for_ollama(trimmed),
                    }
                )
                plain_history = strip_assistant_tool_calls(
                    sanitize_messages_for_ollama(trimmed)
                )
                fallbacks.append({**base, "messages": plain_history})
                if allow_tool_fallback and "tools" in payload:
                    fallbacks.append(
                        {
                            **payload,
                            "messages": plain_history,
                            "options": {**payload.get("options", {}), "num_ctx": 4096},
                        }
                    )
                for fb in fallbacks:
                    r2 = await client.post(f"{self.base_url}/api/chat", json=fb)
                    if r2.status_code < 400:
                        return r2.json()
                    body = r2.text
                    r = r2

            detail = body
            try:
                detail = r.json().get("error", body)
            except Exception:
                pass
            raise OllamaChatError(
                r.status_code,
                f"Ollama returned {r.status_code}: {detail}",
                body=body,
            )

    async def _chat_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.strip():
                        yield json.loads(line)
