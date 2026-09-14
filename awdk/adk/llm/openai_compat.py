"""OpenAI-compatible LLM provider — works with OpenAI, vLLM, LM Studio, llama.cpp, Groq, Together."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

import httpx

logger = logging.getLogger("adk.llm.openai")


def _chat_template_kwargs() -> dict | None:
    """Optional server-side chat-template kwargs (vLLM), e.g. disabling Qwen3
    thinking mode: set ADK_CHAT_TEMPLATE_KWARGS='{"enable_thinking": false}'.
    Without this, thinking-tuned models can burn the whole max_tokens budget on
    reasoning and return an EMPTY content string."""
    raw = os.getenv("ADK_CHAT_TEMPLATE_KWARGS", "").strip()
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else None
    except Exception:  # noqa: BLE001
        logger.warning("ADK_CHAT_TEMPLATE_KWARGS is not valid JSON; ignoring")
        return None

_MAX_RETRIES = 3
# Transient statuses worth retrying: rate limit (429), Anthropic "overloaded"
# (529), and gateway/server blips (500/502/503/504, 408). A single upstream
# hiccup shouldn't kill a whole research run.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})


def _ensure_ok(resp: "httpx.Response") -> None:
    """Raise on a 4xx/5xx, but INCLUDE the provider's response body in the message.

    ``resp.raise_for_status()`` discards the body, so an OpenAI-compatible 400
    ("This model's maximum context length is …", "Invalid 'messages': …") becomes
    an opaque ``Client error '400 Bad Request'`` with no cause. We re-raise the
    same ``httpx.HTTPStatusError`` type (so existing ``except`` blocks keep
    working) enriched with what the provider actually said.
    """
    if resp.status_code < 400:
        return
    body = ""
    try:
        body = (resp.text or "").strip()
    except Exception:  # noqa: BLE001 - never let error-reporting mask the real error
        body = ""
    msg = f"{resp.status_code} {resp.reason_phrase} for {resp.request.url}"
    if body:
        msg += f"\nProvider response: {body[:1200]}"
    raise httpx.HTTPStatusError(msg, request=resp.request, response=resp)


def _demote_nonleading_system(dicts: list[dict]) -> list[dict]:
    """Demote any ``system`` message that appears AFTER the conversation has
    started to ``user``.

    OpenAI tolerates system messages anywhere, but DeepSeek (and several other
    OpenAI-compatible backends, e.g. vLLM with strict templates) reject a
    non-leading ``system`` message with a 400 — the cause is opaque without the
    body. Agent ReAct loops legitimately inject mid-conversation ``system``
    steering nudges (diminishing-returns hints, loop-guard warnings); demoting
    them to ``user`` keeps the steering intent while producing a payload that is
    valid on every backend. Leading system message(s) are left untouched.
    """
    out: list[dict] = []
    started = False
    for m in dicts:
        if m.get("role") == "system":
            if started:
                m = {**m, "role": "user"}
        else:
            started = True
        out.append(m)
    return out

from .base import (
    BatchRequest,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderCapabilities,
    StreamChunk,
    ToolCall,
    _timer,
    messages_to_dicts,
)


def _content_or_reasoning(msg: dict) -> str:
    """Message text, falling back to the reasoning channel when ``content`` is empty.

    A thinking-tuned model can spend its entire ``max_tokens`` budget inside the
    reasoning channel and return ``content: null`` with the text in ``reasoning``
    (renamed from ``reasoning_content`` in vLLM >= 0.8 — both are checked).
    Measured live 2026-07-26 against qwen3.6-27B-NVFP4 on a DGX Spark: a
    ``max_tokens=200`` request came back with ``completion_tokens=200``,
    ``finish_reason="length"`` and ``content=None``.

    Returning "" in that case is a SILENT NO-OP — the caller sees a successful
    200 with an empty answer and nothing to trace. Preferring the reasoning text
    over nothing keeps the failure visible and the answer usable. Callers that
    need the two separated should read ``msg`` directly.

    The durable server-side fix is ``chat_template_kwargs={"enable_thinking":
    false}`` (see ``_chat_template_kwargs`` / ``ctk_by_model``); this is the
    fail-safe for every backend that is not configured that way.
    """
    content = (msg.get("content") or "").strip()
    if content:
        return msg.get("content") or ""
    return (msg.get("reasoning_content") or msg.get("reasoning") or "") or ""


def _read_cached_tokens(usage: dict) -> int:
    """Pull the cached-prompt-token count out of an OpenAI-compatible usage block.

    OpenAI nests it as ``prompt_tokens_details.cached_tokens``; DeepSeek reports
    ``prompt_cache_hit_tokens`` at the top level. Both are automatic server-side
    caches — we don't request them, we just surface the savings to the meter.
    """
    details = usage.get("prompt_tokens_details") or {}
    return int(
        details.get("cached_tokens", 0)
        or usage.get("prompt_cache_hit_tokens", 0)
        or 0
    )


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible API client. Works with any endpoint that speaks the OpenAI format."""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        default_model: str = "gpt-4o-mini",
        timeout: float = 120.0,
        ctk_by_model: dict[str, dict] | None = None,
        verify: "bool | str | None" = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self._timeout = timeout
        # TLS verify= for the httpx client. None -> httpx default (system trust),
        # correct for public providers (openai.com/together/groq). For a self-hosted
        # endpoint served over https with the internal AitherNet CA (e.g. Genesis on
        # :8001), the caller passes tls_verify() so the internal cert is trusted.
        # Harmless on plain-http localhost endpoints (httpx ignores verify for http).
        self._verify = verify
        # Per-model chat_template_kwargs: keys are case-insensitive substrings of
        # a model id (e.g. "qwen" -> {"enable_thinking": False}). Resolved per
        # call so a qwen reasoning model and a gemma vision model on the SAME
        # provider (the gateway serves both) get different template args, instead
        # of one global ADK_CHAT_TEMPLATE_KWARGS env applied to every request.
        self._ctk_by_model = {k.lower(): v for k, v in (ctk_by_model or {}).items()}

    def _resolve_ctk(self, model: str | None) -> dict | None:
        """chat_template_kwargs for this model: the per-model map wins, else the
        global ADK_CHAT_TEMPLATE_KWARGS env (back-compat)."""
        if self._ctk_by_model and model:
            ml = model.lower()
            for key, ctk in self._ctk_by_model.items():
                if key in ml:
                    return ctk
        return _chat_template_kwargs()

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
            # Aitherium identity keys (aither_...) also send X-API-Key, matching
            # the gateway/MCP auth contract (adk/mcp.py MCPAuth.headers). Tenant
            # scoping is enforced server-side from the key (HMAC), not a client
            # header. Non-aither keys (openai/deepseek) are unaffected.
            if self.api_key.startswith("aither_"):
                h["X-API-Key"] = self.api_key
        return h

    def _client(self) -> httpx.AsyncClient:
        kw: dict = {"timeout": self._timeout, "headers": self._headers()}
        if self._verify is not None:
            kw["verify"] = self._verify
        return httpx.AsyncClient(**kw)

    def capabilities(self) -> ProviderCapabilities:
        # OpenAI/DeepSeek/Together cache stable prefixes automatically (no request
        # flag); local OpenAI-compatible engines (vLLM/LM Studio) just report 0.
        # Batch API is supported (cloud providers); local engines gracefully fall back.
        return ProviderCapabilities(prompt_cache="automatic", batch=True)

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
        # `cache` is a no-op on the request for this family (prompt caching is
        # automatic server-side); we still surface cache reads in the response.
        model = model or self.default_model
        payload: dict = {
            "model": model,
            "messages": _demote_nonleading_system(messages_to_dicts(messages)),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice is not None and tools:
            payload["tool_choice"] = tool_choice
        if top_p is not None:
            payload["top_p"] = top_p
        if repetition_penalty is not None:
            # OpenAI uses frequency_penalty; vLLM accepts repetition_penalty
            payload["frequency_penalty"] = repetition_penalty - 1.0  # normalize: 1.3 -> 0.3
        _ctk = self._resolve_ctk(model)
        if _ctk:
            payload["chat_template_kwargs"] = _ctk

        start = _timer()
        async with self._client() as client:
            resp = await self._post_with_retry(
                client, f"{self.base_url}/chat/completions", payload
            )
            data = resp.json()

        latency = _timer() - start
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})

        tool_calls = []
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=args,
            ))

        content = _content_or_reasoning(msg)

        # Hermes XML fallback: if model emitted <tool_call> tags in content
        # but no structured tool_calls, parse them from text
        if not tool_calls and content and "<tool_call>" in content:
            from .base import extract_tool_calls_from_text
            fallback_calls, content = extract_tool_calls_from_text(content)
            if fallback_calls:
                tool_calls = fallback_calls

        cache_read = _read_cached_tokens(usage)
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            tokens_used=usage.get("total_tokens", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency,
            tool_calls=tool_calls,
            cache_status="hit" if cache_read else "",
            cache_read_tokens=cache_read,
            finish_reason=choice.get("finish_reason", "stop"),
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
        payload: dict = {
            "model": model,
            "messages": _demote_nonleading_system(messages_to_dicts(messages)),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice is not None and tools:
            payload["tool_choice"] = tool_choice
        if top_p is not None:
            payload["top_p"] = top_p
        if repetition_penalty is not None:
            payload["frequency_penalty"] = repetition_penalty - 1.0
        _ctk = self._resolve_ctk(model)
        if _ctk:
            payload["chat_template_kwargs"] = _ctk

        async with self._client() as client:
            async with client.stream(
                "POST", f"{self.base_url}/chat/completions", json=payload
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()  # body isn't loaded in stream mode until read
                    _ensure_ok(resp)
                # Streaming counterpart of _content_or_reasoning: a thinking model
                # can emit only reasoning deltas and never a single content token,
                # which streams as a perfectly successful EMPTY answer. Buffer the
                # reasoning and flush it as content iff the stream ends having
                # produced no content at all.
                saw_content = False
                reasoning_buf: list[str] = []
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        if not saw_content and reasoning_buf:
                            yield StreamChunk(
                                content="".join(reasoning_buf), model=model
                            )
                        yield StreamChunk(done=True, model=model)
                        return
                    data = json.loads(data_str)
                    # Some AitherOS backends (MicroScheduler's /v1 facade, Genesis)
                    # answer stream=true with AitherOS-typed SSE events
                    # ({"type": "token", "t": ...}) instead of OpenAI chunks
                    # ({"choices": [{"delta": ...}]}). Parsing only the OpenAI
                    # shape turns such a stream into a perfectly silent EMPTY
                    # answer — zero chunks, no error, every probe green. Accept
                    # both dialects; "thinking"/other typed events are skipped.
                    if "choices" not in data and "type" in data:
                        ev = data.get("type")
                        if ev == "token":
                            t = data.get("t") or data.get("text") or ""
                            if t:
                                saw_content = True
                                yield StreamChunk(content=t, model=data.get("model", model))
                        elif ev == "answer" and not saw_content:
                            ans = data.get("answer") or ""
                            if ans:
                                saw_content = True
                                yield StreamChunk(content=ans, model=data.get("model", model))
                        elif ev == "complete":
                            yield StreamChunk(done=True, model=data.get("model", model))
                            return
                        elif ev == "error":
                            raise RuntimeError(
                                f"provider stream error: {data.get('error') or data}"
                            )
                        continue
                    choice = data.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    chunk_content = delta.get("content", "") or ""
                    if chunk_content:
                        saw_content = True
                    else:
                        _r = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        if _r:
                            reasoning_buf.append(_r)
                    _finished = choice.get("finish_reason") is not None
                    # Flush the rescue BEFORE the terminal chunk so consumers that
                    # stop reading at done=True still receive the text.
                    if _finished and not saw_content and reasoning_buf:
                        yield StreamChunk(
                            content="".join(reasoning_buf),
                            model=data.get("model", model),
                        )
                        reasoning_buf.clear()
                    yield StreamChunk(
                        content=chunk_content,
                        done=_finished,
                        model=data.get("model", model),
                    )

    async def _post_with_retry(
        self, client: httpx.AsyncClient, url: str, payload: dict
    ) -> httpx.Response:
        """POST with retry on transient errors (429 + 5xx). Honors Retry-After,
        else exponential backoff. Surfaces the provider body on the final failure.
        """
        resp = None
        for attempt in range(1, _MAX_RETRIES + 1):
            resp = await client.post(url, json=payload)
            if resp.status_code not in _RETRYABLE_STATUS:
                _ensure_ok(resp)   # 2xx returns; non-retryable 4xx raises with body
                return resp
            if attempt == _MAX_RETRIES:
                break  # exhausted — fall through and raise with body
            retry_after = resp.headers.get("retry-after", "")
            try:
                wait = int(retry_after) if retry_after else min(2 ** attempt, 30)
            except (ValueError, TypeError):
                wait = min(2 ** attempt, 30)
            wait = min(wait, 120)  # cap at 2 minutes
            logger.warning(
                "Transient %s from %s — retrying in %ds (%d/%d)...",
                resp.status_code, url, wait, attempt, _MAX_RETRIES,
            )
            await asyncio.sleep(wait)
        _ensure_ok(resp)   # all retries exhausted — raise with the provider's body
        return resp

    async def list_models(self) -> list[str]:
        async with self._client() as client:
            try:
                resp = await client.get(f"{self.base_url}/models")
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPStatusError, json.JSONDecodeError):
                return []
        return [m["id"] for m in data.get("data", [])]

    async def health_check(self) -> bool:
        """Fast connectivity check — uses a 5s timeout instead of the default 120s."""
        try:
            kw: dict = {"timeout": 5.0, "headers": self._headers()}
            if self._verify is not None:
                kw["verify"] = self._verify
            async with httpx.AsyncClient(**kw) as client:
                resp = await client.get(f"{self.base_url}/models")
                return resp.status_code == 200
        except Exception:
            return False

    async def submit_batch(self, requests: list[BatchRequest]) -> str:
        """Submit a batch of requests to OpenAI /v1/batches API.

        Local OpenAI-compatible engines (vLLM, LM Studio) don't have /v1/batches
        endpoint — they raise NotImplementedError on 404, allowing batch_runner
        to fall back to concurrent chat().
        """
        # Build JSONL payload: one request per line
        lines = []
        for req in requests:
            request_obj = {
                "custom_id": req.custom_id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": req.model or self.default_model,
                    "messages": _demote_nonleading_system(messages_to_dicts(req.messages)),
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                }
            }
            if req.tools:
                request_obj["body"]["tools"] = req.tools
            lines.append(json.dumps(request_obj))

        jsonl_content = "\n".join(lines)

        # Upload JSONL file
        async with self._client() as client:
            resp = await client.post(
                f"{self.base_url}/files",
                files={"file": ("batch.jsonl", jsonl_content, "application/json")},
            )
            _ensure_ok(resp)
            file_data = resp.json()
            file_id = file_data.get("id", "")

            # Create batch
            batch_payload = {
                "input_file_id": file_id,
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
            }
            resp = await self._post_with_retry(
                client, f"{self.base_url}/batches", batch_payload
            )
            batch_data = resp.json()

        return batch_data.get("id", "")

    async def poll_batch(self, batch_id: str) -> str:
        """Poll status of a batch."""
        async with self._client() as client:
            resp = await client.get(
                f"{self.base_url}/batches/{batch_id}",
                headers=self._headers(),
            )
            if resp.status_code == 404:
                raise NotImplementedError("Batch endpoint not found (local engine?)")
            resp.raise_for_status()
            data = resp.json()
        return data.get("status", "unknown")

    async def fetch_batch_results(self, batch_id: str) -> list[LLMResponse]:
        """Fetch results of a completed batch."""
        async with self._client() as client:
            resp = await client.get(
                f"{self.base_url}/batches/{batch_id}",
                headers=self._headers(),
            )
            if resp.status_code == 404:
                raise NotImplementedError("Batch endpoint not found (local engine?)")
            resp.raise_for_status()
            batch_data = resp.json()

            output_file_id = batch_data.get("output_file_id")
            if not output_file_id:
                return []

            # Download output file
            resp = await client.get(
                f"{self.base_url}/files/{output_file_id}/content",
                headers=self._headers(),
            )
            resp.raise_for_status()

        # Parse JSONL results
        results: dict[str, LLMResponse] = {}
        for line in resp.text.strip().split("\n"):
            if not line.strip():
                continue
            item = json.loads(line)
            custom_id = item.get("custom_id", "")
            result = item.get("result", {})
            body = result.get("body", {})

            # Handle error responses
            if "error" in body:
                results[custom_id] = LLMResponse(
                    content="",
                    finish_reason="error",
                )
                continue

            choice = (body.get("choices", [{}]) or [{}])[0]
            msg = choice.get("message", {})
            usage = body.get("usage", {})

            tool_calls = []
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=args,
                ))

            content = _content_or_reasoning(msg)
            cache_read = _read_cached_tokens(usage)
            resp_obj = LLMResponse(
                content=content,
                model=body.get("model", ""),
                tokens_used=usage.get("total_tokens", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                tool_calls=tool_calls,
                cache_status="hit" if cache_read else "",
                cache_read_tokens=cache_read,
                finish_reason=choice.get("finish_reason", "stop"),
            )
            results[custom_id] = resp_obj

        # Return results in sorted order by custom_id
        return [results[cid] for cid in sorted(results.keys()) if cid in results]
