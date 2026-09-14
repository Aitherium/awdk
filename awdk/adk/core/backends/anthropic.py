"""Anthropic (Claude) backend. Talks to ``/v1/messages``.

Anthropic uses a different shape from OpenAI: ``system`` is a top-level
field, not a message role. We translate transparently.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from adk.core.model import Message, ModelResponse

_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, *, api_key: str, model: str = "claude-3-5-sonnet-latest") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.anthropic.com"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }

    def _client(self):
        import httpx

        return httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

    def _split_system(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        system_chunks: list[str] = []
        chat: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                system_chunks.append(m.content)
            elif m.role == "tool":
                # Anthropic represents tool output as a user turn with structured content.
                chat.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or m.name or "tool",
                                "content": m.content,
                            }
                        ],
                    }
                )
            else:
                chat.append({"role": m.role, "content": m.content})
        system = "\n\n".join(s for s in system_chunks if s) or None
        return system, chat

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> ModelResponse:
        system, chat = self._split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat,
            "max_tokens": max_tokens or 4096,
            "temperature": temperature,
            "stream": False,
            **opts,
        }
        if system:
            payload["system"] = system

        async with self._client() as c:
            r = await c.post("/v1/messages", json=payload, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        text_chunks = [
            block.get("text", "")
            for block in data.get("content", []) or []
            if block.get("type") == "text"
        ]
        return ModelResponse(
            text="".join(text_chunks),
            model=data.get("model", self.model),
            finish_reason=data.get("stop_reason"),
            usage=data.get("usage", {}) or {},
            raw=data,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> AsyncIterator[str]:
        import json

        system, chat = self._split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": chat,
            "max_tokens": max_tokens or 4096,
            "temperature": temperature,
            "stream": True,
            **opts,
        }
        if system:
            payload["system"] = system

        async with self._client() as c:
            async with c.stream(
                "POST", "/v1/messages", json=payload, headers=self._headers()
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:") :].strip()
                    if not data_str:
                        continue
                    try:
                        evt = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("type") == "content_block_delta":
                        delta = evt.get("delta", {}) or {}
                        if delta.get("type") == "text_delta":
                            chunk = delta.get("text")
                            if chunk:
                                yield chunk
                    elif evt.get("type") == "message_stop":
                        break
