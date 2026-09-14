"""Google Generative Language API provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import AsyncIterator

import httpx

from .base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderCapabilities,
    StreamChunk,
    ToolCall,
    _timer,
)

logger = logging.getLogger("adk.llm.gemini")

_MAX_RETRIES = 3
# Transient statuses worth retrying
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


def _ensure_ok(resp: httpx.Response) -> None:
    """Raise on 4xx/5xx but include provider body in the error message."""
    if resp.status_code < 400:
        return
    body = ""
    try:
        body = (resp.text or "").strip()
    except Exception:  # noqa: BLE001
        body = ""
    msg = f"{resp.status_code} {resp.reason_phrase} for {resp.request.url}"
    if body:
        msg += f"\nProvider response: {body[:1200]}"
    raise httpx.HTTPStatusError(msg, request=resp.request, response=resp)


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, payload: dict
) -> httpx.Response:
    """POST with retry on transient errors."""
    resp = None
    for attempt in range(1, _MAX_RETRIES + 1):
        resp = await client.post(url, json=payload)
        if resp.status_code not in _RETRYABLE_STATUS:
            _ensure_ok(resp)
            return resp
        if attempt == _MAX_RETRIES:
            break
        retry_after = resp.headers.get("retry-after", "")
        try:
            wait = int(retry_after) if retry_after else min(2 ** attempt, 30)
        except (ValueError, TypeError):
            wait = min(2 ** attempt, 30)
        wait = min(wait, 120)
        logger.warning(
            "Transient %s from %s — retrying in %ds (%d/%d)...",
            resp.status_code, url, wait, attempt, _MAX_RETRIES,
        )
        await asyncio.sleep(wait)
    _ensure_ok(resp)
    return resp


_GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]


def _hash_prefix(system: str, tools: list[dict] | None) -> str:
    """Hash system instruction + tools to identify cached content."""
    data = {"system": system, "tools": tools or []}
    canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


class GeminiProvider(LLMProvider):
    """Google Generative Language API client (gemini-api.google.com)."""

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "gemini-2.0-flash",
        timeout: float = 120.0,
    ):
        self.api_key = api_key
        self.default_model = default_model
        self._timeout = timeout
        self._base_url = "https://generativelanguage.googleapis.com/v1beta"
        # In-instance cache: {prefix_hash: (cache_name, created_timestamp)}
        self._cached_content: dict[str, tuple[str, float]] = {}

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout, headers=self._headers())

    def capabilities(self) -> ProviderCapabilities:
        # Gemini supports explicit caching via cachedContents API
        return ProviderCapabilities(prompt_cache="explicit", batch=False)

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[str, list[dict]]:
        """Split system message from conversation; convert to Gemini format."""
        system = ""
        converted = []
        for m in messages:
            if m.role == "system":
                system = m.content
            elif m.role == "tool":
                # Gemini tool response format
                converted.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": m.name or "unknown",
                            "response": {"result": m.content},
                        }
                    }],
                })
            else:
                # user or model
                converted.append({
                    "role": "user" if m.role == "user" else "model",
                    "parts": [{"text": m.content}],
                })
        return system, converted

    def _convert_tools(self, tools: list[dict] | None) -> dict | None:
        """Convert adk tool schema to Gemini functionDeclarations."""
        if not tools:
            return None

        declarations = []
        for t in tools:
            fn = t.get("function", t)
            declarations.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })

        return {"functionDeclarations": declarations}

    async def _get_or_create_cached_content(
        self,
        system: str,
        tools: list[dict] | None,
        model: str,
    ) -> str | None:
        """Create or reuse a cachedContents handle for the system+tools prefix.

        Returns the cache_name on success, None if creation fails (fall back to inline).
        """
        prefix_hash = _hash_prefix(system, tools)

        # Check if we already have this cached
        if prefix_hash in self._cached_content:
            cache_name, created = self._cached_content[prefix_hash]
            # Skip stale caches (older than 1 hour)
            if time.time() - created < 3600:
                return cache_name

        # Try to create a new cachedContents
        try:
            async with self._client() as client:
                create_payload = {
                    "model": f"models/{model}",
                    "displayName": f"adk_cache_{prefix_hash[:8]}",
                    "systemInstruction": {
                        "parts": [{"text": system}] if system else [],
                        "role": "user",
                    },
                }
                if tools:
                    create_payload["tools"] = [self._convert_tools(tools)]

                create_url = f"{self._base_url}/cachedContents?key={self.api_key}"
                resp = await client.post(create_url, json=create_payload)

                if resp.status_code == 200:
                    data = resp.json()
                    cache_name = data.get("name", "")
                    if cache_name:
                        self._cached_content[prefix_hash] = (cache_name, time.time())
                        logger.debug("Created cached content: %s", cache_name)
                        return cache_name
                else:
                    logger.warning(
                        "Failed to create cached content (status %d): %s",
                        resp.status_code, resp.text[:200]
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("Cached content creation failed: %s", e)

        return None

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
        cache: bool = False,
        **kwargs,
    ) -> LLMResponse:
        model = model or self.default_model
        system, conv_messages = self._convert_messages(messages)

        payload: dict = {
            "model": f"models/{model}",
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
            "contents": conv_messages,
        }

        # Add system instruction
        if system:
            payload["systemInstruction"] = {
                "parts": [{"text": system}],
                "role": "user",
            }

        # Add tools
        if tools:
            tool_spec = self._convert_tools(tools)
            if tool_spec:
                payload["tools"] = [tool_spec]

        if top_p is not None:
            payload["generationConfig"]["topP"] = top_p

        # Handle explicit prompt caching via cachedContents
        if cache and (system or tools):
            cache_name = await self._get_or_create_cached_content(system, tools, model)
            if cache_name:
                payload["cachedContent"] = cache_name
                logger.debug("Using cached content: %s", cache_name)
            # If creation failed, continue with inline system (no hard fail)

        start = _timer()
        async with self._client() as client:
            url = f"{self._base_url}/models/{model}:generateContent?key={self.api_key}"
            resp = await _post_with_retry(client, url, payload)
            data = resp.json()

        latency = _timer() - start
        usage = data.get("usageMetadata", {})

        # Parse content
        content_parts = []
        tool_calls = []
        for candidate in data.get("candidates", [{}]):
            for part in candidate.get("content", {}).get("parts", []):
                if "text" in part:
                    content_parts.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append(ToolCall(
                        id=f"call_{fc.get('name', 'unknown')}_{len(tool_calls)}",
                        name=fc.get("name", ""),
                        arguments=fc.get("args", {}),
                    ))

        # Normalize cache accounting
        cache_read = usage.get("cachedContentTokenCount", 0)
        cache_write = 0  # Gemini doesn't return creation tokens in the same way
        cache_status = ""
        if cache_read > 0:
            cache_status = "hit"
        elif cache and system:
            cache_status = "write"

        finish_reason = data.get("candidates", [{}])[0].get("finishReason", "STOP")
        # Normalize finish_reason: Gemini uses STOP, TOOL_CALLS, etc.
        if finish_reason == "STOP":
            finish_reason = "stop"
        elif finish_reason in ("TOOL_CALLS", "FUNCTION_CALLS"):
            finish_reason = "tool_calls"

        return LLMResponse(
            content="\n".join(content_parts),
            model=model,
            tokens_used=usage.get("totalTokenCount", 0),
            prompt_tokens=usage.get("promptTokenCount", 0),
            completion_tokens=usage.get("candidatesTokenCount", 0),
            latency_ms=latency,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            cache_status=cache_status,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
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
        cache: bool = False,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a response. For simplicity, wraps chat() and yields one chunk."""
        resp = await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            cache=cache,
            **kwargs,
        )
        yield StreamChunk(
            content=resp.content,
            done=True,
            model=resp.model,
            tool_calls=resp.tool_calls,
            finish_reason=resp.finish_reason,
        )

    async def list_models(self) -> list[str]:
        """List available Gemini models."""
        try:
            async with self._client() as client:
                url = f"{self._base_url}/models?key={self.api_key}"
                resp = await client.get(url)
                _ensure_ok(resp)
                data = resp.json()
                models = []
                for model in data.get("models", []):
                    name = model.get("name", "")
                    if name.startswith("models/"):
                        models.append(name[7:])  # Strip "models/" prefix
                return models
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to list models: %s", e)
            return _GEMINI_MODELS
