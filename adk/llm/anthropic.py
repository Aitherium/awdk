"""Anthropic Messages API provider."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


# ── effort -> thinking / output_config.effort ────────────────────────────────
#
# awdk's effort (1-10) used to pick only the model, the tool-loop ceiling and the
# budget; the Messages request carried no thinking or effort setting at all, so an
# effort-9 turn and an effort-2 turn reasoned exactly alike (plan B3, 2026-11-14).
# The mapping:
#
#   1-3   output_config.effort "low", no thinking requested. On models where
#         thinking cannot be switched off (Opus 5.5, Sonnet 5.5, Fable) low effort
#         IS the off switch; `{"type": "disabled"}` is a 400 there.
#   4-6   nothing sent: the model's own default.
#   7-8   thinking adaptive + effort "high"
#   9     thinking adaptive + effort "xhigh" ("high" where the model has no xhigh)
#   10    thinking adaptive + effort "max"  ("high" where the model has no max)
#
# Sampling: current models (Opus 4.7+, Sonnet/Haiku 5+, Fable, Mythos) reject
# temperature/top_p with a 400, and thinking forbids a non-default temperature, so
# both are dropped there; the old fixed 0.7 still goes to models that accept it.
# Forced tool_choice ("any"/"tool") is a 400 on Opus 5.5, Sonnet 5.5 and Fable 5.1+,
# so "required" becomes "auto" on those.

_FAMILY_RE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")

#: When a thinking turn is sent, max_tokens is raised to at least this (non-streaming
#: requests stay under the HTTP timeout at this size) and the request timeout to
#: THINKING_TIMEOUT_S: a high-effort turn can think for minutes.
THINKING_MIN_MAX_TOKENS = 16000
THINKING_TIMEOUT_S = 600.0
#: Older models (Haiku 4.5, Sonnet/Opus 4.5) still take a fixed thinking budget.
_LEGACY_BUDGET = {7: 4096, 8: 4096, 9: 8192, 10: 16000}


def _model_version(model: str) -> tuple[str, tuple[int, int]] | None:
    m = _FAMILY_RE.search((model or "").lower())
    if not m:
        return None
    return m.group(1), (int(m.group(2)), int(m.group(3) or 0))


def model_caps(model: str) -> dict:
    """What a model accepts: sampling params, effort, xhigh/max, adaptive thinking,
    forced tool_choice. Unknown ids get the legacy shape (temperature, nothing else)."""
    caps = {"sampling": True, "effort": False, "xhigh": False, "max": False,
            "adaptive": False, "budget_thinking": False, "forced_tools": True}
    parsed = _model_version(model)
    if parsed is None:
        return caps
    family, ver = parsed
    if family in ("fable", "mythos"):
        caps.update(sampling=False, effort=True, xhigh=True, max=True, adaptive=True,
                    forced_tools=ver < (5, 1))
    elif ver >= (4, 7):
        caps.update(sampling=False, effort=True, xhigh=True, max=True, adaptive=True,
                    forced_tools=not (family in ("opus", "sonnet") and ver >= (5, 5)))
    elif ver == (4, 6):
        caps.update(effort=True, max=True, adaptive=True)
    elif family == "opus" and ver == (4, 5):
        caps.update(effort=True, budget_thinking=True)
    elif ver >= (3, 7):
        caps.update(budget_thinking=True)
    return caps


def reasoning_settings(model: str, effort: int | None) -> dict:
    """{"effort": str|None, "thinking": dict|None} for this model at this awdk effort."""
    if effort is None:
        return {"effort": None, "thinking": None}
    try:
        e = max(1, min(10, int(effort)))
    except (TypeError, ValueError):
        return {"effort": None, "thinking": None}
    caps = model_caps(model)
    if e <= 3:
        return {"effort": "low" if caps["effort"] else None, "thinking": None}
    if e <= 6:
        return {"effort": None, "thinking": None}
    level = "high" if e <= 8 else ("xhigh" if e == 9 else "max")
    if (level == "xhigh" and not caps["xhigh"]) or (level == "max" and not caps["max"]):
        level = "high"
    thinking: dict | None = None
    if caps["adaptive"]:
        thinking = {"type": "adaptive"}
    elif caps["budget_thinking"]:
        thinking = {"type": "enabled", "budget_tokens": _LEGACY_BUDGET[e]}
    return {"effort": level if caps["effort"] else None, "thinking": thinking}


def apply_reasoning(payload: dict, model: str, effort: int | None) -> bool:
    """Set thinking/effort on ``payload`` and drop what the model rejects.

    Returns True when a thinking turn was requested (the caller widens its timeout).
    """
    caps = model_caps(model)
    rs = reasoning_settings(model, effort)
    thinking = rs["thinking"]
    if rs["effort"]:
        oc = dict(payload.get("output_config") or {})
        oc["effort"] = rs["effort"]
        payload["output_config"] = oc
    if thinking:
        payload["thinking"] = thinking
        floor = THINKING_MIN_MAX_TOKENS
        if thinking.get("budget_tokens"):
            floor = max(floor, int(thinking["budget_tokens"]) + 4096)
        payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), floor)
    if thinking or not caps["sampling"]:
        payload.pop("temperature", None)
        payload.pop("top_p", None)
    tc = payload.get("tool_choice")
    if not caps["forced_tools"] and isinstance(tc, dict) and tc.get("type") in ("any", "tool"):
        payload["tool_choice"] = {"type": "auto"}
    return bool(thinking)


def _build_request_payload(
    messages: list[Message],
    model: str,
    system: str | None,
    tools: list[dict] | None,
    temperature: float,
    max_tokens: int,
    cache: bool,
    effort: int | None = None,
) -> dict:
    """Build a single request payload for Anthropic API."""
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    apply_reasoning(payload, model, effort)
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
    # current generation first; the 4.x ids are still served
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5-2",
    "claude-opus-4-8",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API client."""

    #: LLMRouter passes ``effort=`` (1-10) only to providers that declare this;
    #: it becomes ``thinking`` + ``output_config.effort`` (see ``apply_reasoning``).
    accepts_effort = True

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "claude-sonnet-5-5",
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

    def _client(self, use_advisor: bool = False, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=max(self._timeout, timeout or 0.0), headers=self._headers(use_advisor)
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

        thinking_on = apply_reasoning(payload, model, kwargs.pop("effort", None))

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
        async with self._client(use_advisor=use_advisor,
                                 timeout=THINKING_TIMEOUT_S if thinking_on else None) as client:
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
            # Thinking blocks must go back verbatim on the next tool round (the API
            # rejects or drops a tool turn whose thinking was rebuilt as text), so a
            # thinking reply rides the same native round-trip as the advisor's.
            raw_content_blocks=list(raw_blocks) if (use_advisor or any(
                b.get("type") in ("thinking", "redacted_thinking") for b in raw_blocks
                if isinstance(b, dict))) else [],
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
        thinking_on = apply_reasoning(payload, model, kwargs.pop("effort", None))
        if cache:
            _apply_prompt_cache(payload)

        async with self._client(timeout=THINKING_TIMEOUT_S if thinking_on else None) as client:
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
