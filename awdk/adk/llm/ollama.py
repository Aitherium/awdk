"""Ollama LLM provider — direct httpx client for local Ollama instances."""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from .base import (
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
    _timer,
    messages_to_dicts,
)


def _ollama_messages(messages: list[Message]) -> list[dict]:
    """messages_to_dicts, but with tool-call ``arguments`` as OBJECTS.

    OpenAI serialises ``function.arguments`` as a JSON *string*; Ollama's
    ``/api/chat`` expects a JSON *object* and 400s on a string ("Value looks
    like object, but can't find closing '}' symbol"). We parse any string
    arguments back to a dict on a COPY so the shared Message is untouched.
    """
    import copy

    msgs = messages_to_dicts(messages)
    for m in msgs:
        tcs = m.get("tool_calls")
        if not tcs:
            continue
        fixed = []
        for tc in tcs:
            tc = copy.deepcopy(tc) if isinstance(tc, dict) else tc
            if isinstance(tc, dict):
                fn = tc.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    try:
                        fn["arguments"] = json.loads(fn["arguments"]) if fn["arguments"].strip() else {}
                    except (ValueError, TypeError):
                        fn["arguments"] = {}
            fixed.append(tc)
        m["tool_calls"] = fixed
    return msgs


class OllamaProvider(LLMProvider):
    """Talk to a local or remote Ollama instance."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        default_model: str = "nemotron-orchestrator-8b",
        timeout: float = 120.0,
    ):
        self.host = host.rstrip("/")
        self.default_model = default_model
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.host, timeout=self._timeout)

    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        **kwargs,
    ) -> LLMResponse:
        model = model or self.default_model
        options: dict = {
            "temperature": temperature,
            "num_predict": max_tokens,
        }
        if top_p is not None:
            options["top_p"] = top_p
        if repetition_penalty is not None:
            options["repeat_penalty"] = repetition_penalty
        payload: dict = {
            "model": model,
            "messages": _ollama_messages(messages),
            "stream": False,
            "options": options,
        }
        if tools:
            payload["tools"] = tools

        start = _timer()
        async with self._client() as client:
            resp = await client.post("/api/chat", json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Ollama /api/chat {resp.status_code}: {resp.text[:400]}")
            data = resp.json()

        latency = _timer() - start
        msg = data.get("message", {})

        tool_calls = []
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            tool_calls.append(ToolCall(
                id=fn.get("name", ""),
                name=fn.get("name", ""),
                arguments=fn.get("arguments", {}),
            ))

        content = msg.get("content", "")

        # Hermes XML fallback: if model emitted <tool_call> tags in content
        # but no structured tool_calls, parse them from text
        if not tool_calls and content and "<tool_call>" in content:
            from .base import extract_tool_calls_from_text
            fallback_calls, content = extract_tool_calls_from_text(content)
            if fallback_calls:
                tool_calls = fallback_calls

        return LLMResponse(
            content=content,
            model=data.get("model", model),
            tokens_used=data.get("eval_count", 0) + data.get("prompt_eval_count", 0),
            prompt_tokens=data.get("prompt_eval_count", 0),
            completion_tokens=data.get("eval_count", 0),
            latency_ms=latency,
            tool_calls=tool_calls,
        )

    async def chat_stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        model = model or self.default_model
        options: dict = {
            "temperature": temperature,
            "num_predict": max_tokens,
        }
        if top_p is not None:
            options["top_p"] = top_p
        if repetition_penalty is not None:
            options["repeat_penalty"] = repetition_penalty
        payload: dict = {
            "model": model,
            "messages": _ollama_messages(messages),
            "stream": True,
            "options": options,
        }
        if tools:
            payload["tools"] = tools

        async with self._client() as client:
            async with client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise RuntimeError(
                        f"Ollama /api/chat {resp.status_code}: {body[:400]}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    msg = data.get("message", {})
                    done = data.get("done", False)
                    yield StreamChunk(
                        content=msg.get("content", ""),
                        done=done,
                        model=data.get("model", model),
                    )

    async def list_models(self) -> list[str]:
        async with self._client() as client:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
            data = resp.json()
        return [m["name"] for m in data.get("models", [])]

    async def health_check(self) -> bool:
        """Fast connectivity check — uses a 5s timeout instead of the default 120s."""
        try:
            async with httpx.AsyncClient(base_url=self.host, timeout=5.0) as client:
                resp = await client.get("/api/tags")
                return resp.status_code == 200
        except Exception:
            return False

    async def pull_model(self, model: str) -> None:
        """Pull a model from the Ollama library."""
        async with self._client() as client:
            resp = await client.post(
                "/api/pull",
                json={"name": model, "stream": False},
                timeout=600.0,
            )
            resp.raise_for_status()
