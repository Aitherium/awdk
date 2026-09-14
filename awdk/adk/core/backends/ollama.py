"""Ollama backend. Talks to a local Ollama daemon (``/api/chat``)."""

from __future__ import annotations

from typing import Any, AsyncIterator

from adk.core.model import Message, ModelResponse


class OllamaBackend:
    name = "ollama"

    def __init__(self, *, base_url: str = "http://localhost:11434", model: str = "llama3.1:8b") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _client(self):
        import httpx

        return httpx.AsyncClient(base_url=self.base_url, timeout=120.0)

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **opts: Any,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "stream": False,
            "options": {"temperature": temperature, **opts},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens

        async with self._client() as c:
            r = await c.post("/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
        msg = data.get("message", {}) or {}
        return ModelResponse(
            text=msg.get("content", ""),
            model=data.get("model", self.model),
            finish_reason=data.get("done_reason"),
            usage={
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
            },
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

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "stream": True,
            "options": {"temperature": temperature, **opts},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens

        async with self._client() as c:
            async with c.stream("POST", "/api/chat", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = (data.get("message") or {}).get("content", "")
                    if chunk:
                        yield chunk
                    if data.get("done"):
                        break
