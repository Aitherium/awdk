"""OpenAI-compatible backends: OpenAI, vLLM, DeepSeek.

All three speak the OpenAI ``/v1/chat/completions`` shape, so they share a
single implementation parameterized by base URL + API key + model.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from adk.core.model import Message, ModelResponse


class _OpenAICompatBackend:
    name = "openai-compat"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.extra_headers = dict(extra_headers or {})

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

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
            "temperature": temperature,
            "stream": False,
            **opts,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        async with self._client() as c:
            r = await c.post("/v1/chat/completions", json=payload, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        return ModelResponse(
            text=msg.get("content", "") or "",
            model=data.get("model", self.model),
            finish_reason=choice.get("finish_reason"),
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

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.as_dict() for m in messages],
            "temperature": temperature,
            "stream": True,
            **opts,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        async with self._client() as c:
            async with c.stream(
                "POST", "/v1/chat/completions", json=payload, headers=self._headers()
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:") :].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choice = (data.get("choices") or [{}])[0]
                    delta = choice.get("delta", {}) or {}
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk


class OpenAIBackend(_OpenAICompatBackend):
    name = "openai"

    def __init__(self, *, api_key: str, model: str = "gpt-4o-mini") -> None:
        super().__init__(
            base_url="https://api.openai.com", model=model, api_key=api_key
        )


class VLLMBackend(_OpenAICompatBackend):
    name = "vllm"

    def __init__(self, *, base_url: str, model: str = "auto", api_key: str | None = None) -> None:
        super().__init__(base_url=base_url, model=model, api_key=api_key)


class DeepSeekBackend(_OpenAICompatBackend):
    name = "deepseek"

    def __init__(self, *, api_key: str, model: str = "deepseek-chat") -> None:
        super().__init__(
            base_url="https://api.deepseek.com", model=model, api_key=api_key
        )


class MoonshotBackend(_OpenAICompatBackend):
    name = "moonshot"

    def __init__(self, *, api_key: str, model: str = "kimi-k3") -> None:
        super().__init__(
            base_url="https://api.moonshot.ai", model=model, api_key=api_key
        )
