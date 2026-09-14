"""Anthropic Messages API provider."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from .advisor import ADVISOR_BETA, ADVISOR_TOOL_NAME, AdvisorConfig
from .base import (
    BatchRequest,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderCapabilities,
    StreamChunk,
    ToolCall,
    _timer,
)

logger = logging.getLogger("adk.llm.anthropic")

_EPHEMERAL = {"type": "ephemeral"}


def _apply_prompt_cache(payload: dict) -> None:
    """Insert Anthropic ``cache_control`` breakpoints over the stable prefix.

    A breakpoint in the ``system`` block caches everything before it in the
    canonical order (tools → system), so one mark covers the agent's whole
    reusable prefix. We also mark the last tool defensively (well within the
    4-breakpoint limit). Mutates ``payload`` in place; safe to call when the
    prefix is below the cacheable minimum — the API simply won't cache it.
    """
    system = payload.get("system")
    if isinstance(system, str) and system:
        payload["system"] = [{"type": "text", "text": system, "cache_control": _EPHEMERAL}]
    tools = payload.get("tools")
    if tools:
        # Copy so we never mutate the caller's shared tool dicts across turns.
        tools = [dict(t) for t in tools]
        # Mark the last NON-advisor tool. The advisor tool carries its own
        # ``caching`` field and must not receive a cache_control breakpoint.
        for t in reversed(tools):
            if not t.get("__no_cache_control"):
                t["cache_control"] = _EPHEMERAL
                break
        payload["tools"] = tools


def _build_request_payload(
    messages: list[Message],
    model: str,
    system: str | None,
    tools: list[dict] | None,
    temperature: float,
    max_tokens: int,
    cache: bool,
) -> dict:
    """Build a single request payload for Anthropic API."""
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        payload["system"] = system
    if tools:
        # Convert to Anthropic tool format
        anthropic_tools = []
        for t in tools:
            fn = t.get("function", t)
            anthropic_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {}),
            })
        payload["tools"] = anthropic_tools

    if cache:
        _apply_prompt_cache(payload)

    return payload

_MAX_RETRIES = 3
# Transient statuses worth retrying: rate limit (429), Anthropic "overloaded"
# (529), and gateway/server blips (500/502/503/504, 408). Anthropic 502s are
# common transient gateway hiccups — a single one shouldn't kill a run.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


def _ensure_ok(resp: httpx.Response) -> None:
    """Raise on 4xx/5xx but INCLUDE the provider's body (e.g. the overloaded /
    invalid-request message) — ``raise_for_status()`` discards it."""
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
    """POST with retry on transient errors (429 + 5xx). Honors Retry-After else
    exponential backoff; surfaces the provider body on the final failure."""
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

# Anthropic models that are always available
_ANTHROPIC_MODELS = [
    "claude-opus-4-8",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API client."""

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "claude-sonnet-4-6",
        timeout: float = 120.0,
    ):
        self.api_key = api_key
        self.default_model = default_model
        self._timeout = timeout
        self._base_url = "https://api.anthropic.com"

    def _headers(self, use_advisor: bool = False) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        if use_advisor:
            headers["anthropic-beta"] = ADVISOR_BETA
        return headers

    def _client(self, use_advisor: bool = False) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout, headers=self._headers(use_advisor)
        )

    def capabilities(self) -> ProviderCapabilities:
        # Explicit cache breakpoints; batch API supported.
        return ProviderCapabilities(prompt_cache="explicit", batch=True)

    def _convert_messages(self, messages: list[Message]) -> tuple[str, list[dict]]:
        """Split the system message out and convert the rest to Anthropic shape.

        Plain text messages stay ``{role, content-string}`` — byte-identical to
        before. Messages with tool semantics are converted to **native** Anthropic
        content blocks (the previous string flattening emitted ``role:"tool"``,
        which the API rejects):

        * ``content_blocks`` set      → emitted verbatim (advisor/tool round-trip)
        * ``tool_call_id`` set        → ``user`` role + ``tool_result`` block
        * assistant + ``tool_calls``  → text block + ``tool_use`` blocks
        """
        system = ""
        converted: list[dict] = []
        for m in messages:
            if m.role == "system":
                system = m.content
                continue

            # 1) Caller-supplied native blocks win (advisor + tool_use round-trip).
            if m.content_blocks:
                converted.append({"role": m.role, "content": m.content_blocks})
                continue

            # 2) Tool result → native tool_result under the user role.
            if m.tool_call_id:
                converted.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id,
                        "content": m.content or "",
                    }],
                })
                continue

            # 3) Assistant tool calls → text + native tool_use blocks.
            if m.role == "assistant" and m.tool_calls:
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    fn = tc.get("function", tc) if isinstance(tc, dict) else {}
                    raw_args = fn.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except (ValueError, json.JSONDecodeError):
                            raw_args = {}
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                    blocks.append({
                        "type": "tool_use",
                        "id": tc_id,
                        "name": fn.get("name", ""),
                        "input": raw_args if isinstance(raw_args, dict) else {},
                    })
                converted.append({"role": m.role, "content": blocks})
                continue

            # 4) Plain text — unchanged.
            converted.append({"role": m.role, "content": m.content})
        return system, converted

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
        # Advisor tool (beta): a server-side consult of a stronger model mid-turn.
        # Off / absent → every branch below is a no-op and the request is identical.
        advisor = AdvisorConfig.coerce(kwargs.pop("advisor", None))
        use_advisor = bool(advisor and advisor.enabled)
        if use_advisor:
            from .advisor import validate_pair
            warn = validate_pair(model, advisor.advisor_model)
            if warn:
                logger.warning("[ADVISOR] %s", warn)

        system, conv_messages = self._convert_messages(messages)

        payload: dict = {
            "model": model,
            "messages": conv_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if top_p is not None:
            payload["top_p"] = top_p
        # Anthropic doesn't have repetition_penalty — skip silently

        anthropic_tools: list[dict] = []
        if tools:
            for t in tools:
                fn = t.get("function", t)
                anthropic_tools.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {}),
                })
        if use_advisor:
            # The advisor is a server tool; it can ride alongside function tools
            # or be the only tool. Carries a __no_cache_control marker (stripped
            # below) so the prompt-cache breakpoint skips it.
            anthropic_tools.append(advisor.tool_dict())
        if anthropic_tools:
            payload["tools"] = anthropic_tools
            # Map tool_choice for Anthropic format
            if tool_choice is not None:
                if tool_choice == "auto":
                    payload["tool_choice"] = {"type": "auto"}
                elif tool_choice == "required":
                    payload["tool_choice"] = {"type": "any"}
                elif tool_choice == "none":
                    pass  # Don't send tools
                elif isinstance(tool_choice, dict):
                    payload["tool_choice"] = tool_choice

        if cache:
            _apply_prompt_cache(payload)

        # Strip internal markers (e.g. the advisor tool's __no_cache_control) so
        # they never reach the wire. Runs whether or not caching was applied.
        if payload.get("tools"):
            payload["tools"] = [
                {k: v for k, v in t.items() if k != "__no_cache_control"}
                for t in payload["tools"]
            ]

        start = _timer()
        async with self._client(use_advisor=use_advisor) as client:
            resp = await _post_with_retry(client, f"{self._base_url}/v1/messages", payload)
            data = resp.json()

        latency = _timer() - start
        usage = data.get("usage", {})

        content_parts = []
        tool_calls = []
        advisor_calls = 0
        advisor_text = ""
        advisor_stop_reason = ""
        advisor_error = ""
        raw_blocks = data.get("content", []) or []
        for block in raw_blocks:
            btype = block.get("type")
            if btype == "text":
                content_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block["id"],
                    name=block["name"],
                    arguments=block.get("input", {}),
                ))
            elif btype == "server_tool_use":
                if block.get("name") == ADVISOR_TOOL_NAME:
                    advisor_calls += 1
            elif btype == "advisor_tool_result":
                result = block.get("content", {})
                if isinstance(result, dict):
                    rtype = result.get("type")
                    if rtype == "advisor_result" or "text" in result:
                        advisor_text = result.get("text", "")
                        advisor_stop_reason = result.get("stop_reason", "") or advisor_stop_reason
                    elif rtype == "advisor_redacted_result" or "encrypted_content" in result:
                        advisor_stop_reason = result.get("stop_reason", "") or advisor_stop_reason
                    elif rtype == "advisor_tool_result_error" or "error_code" in result:
                        advisor_error = result.get("error_code", "unknown")
                        logger.warning("[ADVISOR] advisor call failed: %s", advisor_error)
            # Unknown block types are skipped — forward-safe.

        # Advisor sub-inference tokens are billed at the advisor (Opus) rate, so
        # they're summed apart from the executor's tokens. Top-level usage already
        # excludes them (output = executor iterations; input/cache = first only).
        advisor_in = 0
        advisor_out = 0
        for it in usage.get("iterations", []) or []:
            if it.get("type") == "advisor_message":
                advisor_in += it.get("input_tokens", 0)
                advisor_out += it.get("output_tokens", 0)

        # Normalize Anthropic's cache accounting onto the shared fields. Note
        # input_tokens already EXCLUDES cache reads/writes, so tokens_used stays
        # the billed-at-full-rate count; the cache_* fields are the savings.
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        return LLMResponse(
            content="\n".join(content_parts),
            model=data.get("model", model),
            tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            latency_ms=latency,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", "end_turn"),
            cache_status="hit" if cache_read else ("write" if cache_write else ""),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            advisor_calls=advisor_calls,
            advisor_text=advisor_text,
            advisor_input_tokens=advisor_in,
            advisor_output_tokens=advisor_out,
            advisor_stop_reason=advisor_stop_reason,
            advisor_error=advisor_error,
            raw_content_blocks=list(raw_blocks) if use_advisor else [],
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
        model = model or self.default_model
        system, conv_messages = self._convert_messages(messages)

        payload: dict = {
            "model": model,
            "messages": conv_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if cache:
            _apply_prompt_cache(payload)

        async with self._client() as client:
            for attempt in range(1, _MAX_RETRIES + 1):
                async with client.stream(
                    "POST", f"{self._base_url}/v1/messages", json=payload
                ) as resp:
                    if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                        await resp.aread()  # body not loaded in stream mode until read
                        wait = min(2 ** attempt, 30)
                        logger.warning(
                            "Transient %s on Anthropic stream — retrying in %ds (%d/%d)...",
                            resp.status_code, wait, attempt, _MAX_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code >= 400:
                        await resp.aread()
                        _ensure_ok(resp)
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        import json
                        data = json.loads(line[6:])
                        event_type = data.get("type", "")
                        if event_type == "content_block_delta":
                            delta = data.get("delta", {})
                            yield StreamChunk(
                                content=delta.get("text", ""),
                                model=model,
                            )
                        elif event_type == "message_stop":
                            yield StreamChunk(done=True, model=model)
                    return

    async def list_models(self) -> list[str]:
        return list(_ANTHROPIC_MODELS)

    async def submit_batch(self, requests: list[BatchRequest]) -> str:
        """Submit a batch of requests to Anthropic Message Batches API."""
        batch_requests = []
        for req in requests:
            system, conv_messages = self._convert_messages(req.messages)
            payload = _build_request_payload(
                messages=conv_messages,
                model=req.model or self.default_model,
                system=system,
                tools=req.tools,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                cache=req.cache,
            )
            batch_requests.append({
                "custom_id": req.custom_id,
                "params": payload,
            })

        batch_payload = {"requests": batch_requests}
        async with self._client() as client:
            resp = await _post_with_retry(
                client,
                f"{self._base_url}/v1/messages/batches",
                batch_payload,
            )
            data = resp.json()
        return data.get("id", "")

    async def poll_batch(self, batch_id: str) -> str:
        """Poll status of a batch."""
        async with self._client() as client:
            resp = await client.get(
                f"{self._base_url}/v1/messages/batches/{batch_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("processing_status", "unknown")

    async def fetch_batch_results(self, batch_id: str) -> list[LLMResponse]:
        """Fetch results of a completed batch."""
        async with self._client() as client:
            # Get batch metadata to find results URL
            resp = await client.get(
                f"{self._base_url}/v1/messages/batches/{batch_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            batch_data = resp.json()

            results_url = batch_data.get("results_url")
            if not results_url:
                return []

            # Fetch results from the results URL
            resp = await client.get(results_url, headers=self._headers())
            resp.raise_for_status()

        # Parse JSONL results
        results: dict[str, LLMResponse] = {}
        for line in resp.text.strip().split("\n"):
            if not line.strip():
                continue
            import json
            item = json.loads(line)
            custom_id = item.get("custom_id", "")
            result = item.get("result", {})
            message = result.get("message", {})
            usage = message.get("usage", {})

            content_parts = []
            tool_calls = []
            for block in message.get("content", []):
                if block.get("type") == "text":
                    content_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_calls.append(ToolCall(
                        id=block["id"],
                        name=block["name"],
                        arguments=block.get("input", {}),
                    ))

            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_write = usage.get("cache_creation_input_tokens", 0)
            resp_obj = LLMResponse(
                content="\n".join(content_parts),
                model=message.get("model", ""),
                tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
                tool_calls=tool_calls,
                finish_reason=message.get("stop_reason", "end_turn"),
                cache_status="hit" if cache_read else ("write" if cache_write else ""),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            results[custom_id] = resp_obj

        # Return results in the order of custom_ids from the batch request
        # For now, return in the order we received them (sorted by custom_id)
        return [results[cid] for cid in sorted(results.keys()) if cid in results]
