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


def sanitize_messages_for_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape messages for Ollama /api/chat (tool_name required on tool results)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_name": str(m.get("tool_name") or "tool"),
                    "content": str(m.get("content") or ""),
                }
            )
            continue
        if role == "assistant":
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": str(m.get("content") or ""),
            }
            tcs = m.get("tool_calls")
            if tcs:
                fixed: list[dict[str, Any]] = []
                for tc in tcs:
                    fn = tc.get("function") or tc
                    name = fn.get("name") or ""
                    args = fn.get("arguments", "{}")
                    if not isinstance(args, str):
                        args = json.dumps(args)
                    fixed.append(
                        {
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                    )
                if fixed:
                    msg["tool_calls"] = fixed
            out.append(msg)
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
        clean = sanitize_messages_for_ollama(messages)
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

            if allow_tool_fallback and r.status_code == 400 and "tools" in payload:
                payload_no_tools = {
                    k: v for k, v in payload.items() if k != "tools"
                }
                r2 = await client.post(
                    f"{self.base_url}/api/chat", json=payload_no_tools
                )
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
