"""Abstract LLM provider and shared data types."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import wraps
from typing import AsyncIterator

logger = logging.getLogger("adk.llm.base")


@dataclass
class Message:
    """A single message in a conversation."""
    role: str  # "system", "user", "assistant", "tool"
    # Usually a plain string. For MULTIMODAL requests it may be an OpenAI-style
    # content-part list, e.g. [{"type": "text", "text": ...},
    # {"type": "image_url", "image_url": {"url": <data-URI or http>}}]. The list
    # is passed through verbatim by messages_to_dicts to the OpenAI-compatible
    # body, so it only works on that provider family (gateway / vLLM / OpenAI) —
    # build one with adk.llm.multimodal.image_message().
    content: str | list[dict]
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list | None = None
    # Optional native-Anthropic content blocks. When set, the Anthropic provider
    # emits these verbatim instead of the string ``content`` — used to round-trip
    # advisor (server_tool_use + advisor_tool_result) blocks, which can't be
    # reconstructed from the normalized fields. None → unchanged string path.
    content_blocks: list[dict] | None = None


@dataclass
class ToolCall:
    """A tool invocation requested by the LLM."""
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    """Response from an LLM provider."""
    content: str
    model: str = ""
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    effort_level: int = 0
    cache_status: str = ""
    # Normalized prompt-cache accounting (provider-agnostic). Each provider maps
    # its own usage schema onto these so the "tokens saved" meter never has to
    # know which backend served the turn: Anthropic cache_read/creation_input,
    # OpenAI prompt_tokens_details.cached_tokens, DeepSeek prompt_cache_hit_tokens.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Advisor-tool accounting (Anthropic ``advisor-tool-2026-03-01``). The advisor
    # is a separate sub-inference billed at the advisor model's (Opus) rates, so
    # its tokens are tracked apart from the executor's tokens_used above.
    advisor_calls: int = 0
    advisor_text: str = ""
    advisor_input_tokens: int = 0
    advisor_output_tokens: int = 0
    advisor_stop_reason: str = ""        # "end_turn" | "max_tokens" (only when capped)
    advisor_error: str = ""              # error_code from advisor_tool_result_error, if any
    # Raw native assistant content blocks (text + tool_use + advisor blocks) for
    # verbatim round-trip across a ReAct loop. Populated only when advisor is on.
    raw_content_blocks: list[dict] = field(default_factory=list)


@dataclass
class BatchRequest:
    """A single request in a batch submission.

    custom_id is used to correlate results with input requests.
    """
    custom_id: str
    messages: list[Message]
    model: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.7
    tools: list[dict] | None = None
    cache: bool = False


@dataclass(frozen=True)
class ProviderCapabilities:
    """What cost-optimization levers a provider supports.

    Agent/router code branches on these capabilities, never on a provider name,
    so a new backend is wired by declaring caps + mapping its usage fields.

    prompt_cache:
        "explicit"  — caller must mark cache breakpoints (Anthropic cache_control,
                      Gemini cachedContents). The adapter inserts them on cache=True.
        "automatic" — provider caches stable prefixes itself (OpenAI, DeepSeek);
                      cache=True is a no-op on the request, but the adapter still
                      reads the provider's cache-usage fields into LLMResponse.
        "none"      — no billed prompt cache (local engines do KV/prefix reuse for
                      latency only). cache=True is a harmless no-op.
    batch:
        True if the provider exposes an async batch API (Anthropic Message Batches,
        OpenAI /v1/batches). When False, batch_runner falls back to concurrent chat().
    """
    prompt_cache: str = "none"   # "explicit" | "automatic" | "none"
    batch: bool = False


@dataclass
class StreamChunk:
    """A single chunk from a streaming response."""
    content: str = ""
    done: bool = False
    model: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Internal tag stripping (ported from monorepo UnifiedChatBackend)
# ─────────────────────────────────────────────────────────────────────────────

# Matches <tool_call>...</tool_call> and unclosed (truncated) <tool_call>...
_TOOL_CALL_TAG_RE = re.compile(
    r"<tool_call>.*?</tool_call>|<tool_call>.*",
    re.DOTALL,
)
# Matches leaked system prompt markers
_INTERNAL_TAG_RE = re.compile(
    r"\[(?:SYSTEM|AXIOMS|RULES|IDENTITY|CAPABILITIES|CONTEXT|MEMORIES|AFFECT|RESPONSE FORMAT)\]"
)


import json as _json

_HERMES_TOOL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


_BARE_TOOL_JSON_RE = re.compile(
    r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"(?:arguments|parameters)"\s*:\s*(\{.*?\})\s*\}',
    re.DOTALL,
)


def _parse_tool_json(raw_json: str, idx: int) -> ToolCall | None:
    """Parse a single JSON blob into a ToolCall, or None on failure."""
    try:
        data = _json.loads(raw_json)
        name = data.get("name", "")
        args = data.get("arguments", data.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = _json.loads(args)
            except (ValueError, _json.JSONDecodeError):
                args = {}
        if name:
            return ToolCall(
                id=f"call_{name}_{idx}",
                name=name,
                arguments=args if isinstance(args, dict) else {},
            )
    except (_json.JSONDecodeError, ValueError):
        pass
    return None


def extract_tool_calls_from_text(
    content: str,
    finish_reason_hint: str = "",
) -> tuple[list[ToolCall], str]:
    """Extract tool calls from model output text.

    Checks for Hermes-format ``<tool_call>`` XML tags first, then falls back
    to bare JSON ``{"name": "...", "arguments": {...}}`` patterns when
    *finish_reason_hint* signals tool use (to avoid false positives).

    Returns (tool_calls, cleaned_content). If nothing found, returns ([], content).
    """
    # ── Primary: Hermes XML tags ──
    matches = _HERMES_TOOL_RE.findall(content)
    if matches:
        tool_calls: list[ToolCall] = []
        for raw_json in matches:
            tc = _parse_tool_json(raw_json, len(tool_calls))
            if tc:
                tool_calls.append(tc)
        if tool_calls:
            cleaned = _HERMES_TOOL_RE.sub("", content).strip()
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            return tool_calls, cleaned

    # ── Secondary: bare JSON (only when finish_reason hints tool use) ──
    if finish_reason_hint in ("tool_calls", "tool_use"):
        bare_matches = _BARE_TOOL_JSON_RE.finditer(content)
        tool_calls_bare: list[ToolCall] = []
        for m in bare_matches:
            full_match = m.group(0)
            tc = _parse_tool_json(full_match, len(tool_calls_bare))
            if tc:
                tool_calls_bare.append(tc)
        if tool_calls_bare:
            cleaned = _BARE_TOOL_JSON_RE.sub("", content).strip()
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            return tool_calls_bare, cleaned

    return [], content


def strip_internal_tags(text: str) -> str:
    """Strip leaked <tool_call> XML and internal prompt markers from LLM output."""
    if not text:
        return text
    cleaned = _TOOL_CALL_TAG_RE.sub("", text)
    cleaned = _INTERNAL_TAG_RE.sub("", cleaned)
    # Collapse runs of blank lines left by removal
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Degeneration detection (ported from monorepo AitherLLMQueue)
# ─────────────────────────────────────────────────────────────────────────────

class DegenerationDetector:
    """Monitors streaming tokens for repetition loops and low diversity.

    Detects degenerate output (e.g., "I want I want I want...") by tracking
    n-gram frequencies and word diversity in a sliding window.
    """

    def __init__(
        self,
        ngram_size: int = 3,
        window_size: int = 50,
        repetition_threshold: float = 0.4,
        diversity_threshold: float = 0.15,
    ):
        self._ngram_size = ngram_size
        self._window_size = window_size
        self._repetition_threshold = repetition_threshold
        self._diversity_threshold = diversity_threshold
        self._tokens: list[str] = []
        self._degenerate = False

    @property
    def degenerate(self) -> bool:
        return self._degenerate

    def feed(self, text: str) -> bool:
        """Feed new text. Returns True if degeneration detected."""
        if self._degenerate:
            return True
        words = text.split()
        if not words:
            return False
        self._tokens.extend(words)
        # Only check when we have enough tokens
        if len(self._tokens) < self._window_size:
            return False
        window = self._tokens[-self._window_size:]
        # N-gram repetition check
        ngrams: dict[tuple, int] = {}
        for i in range(len(window) - self._ngram_size + 1):
            ng = tuple(w.lower() for w in window[i:i + self._ngram_size])
            ngrams[ng] = ngrams.get(ng, 0) + 1
        total_ngrams = len(window) - self._ngram_size + 1
        if total_ngrams > 0:
            max_freq = max(ngrams.values())
            if max_freq / total_ngrams > self._repetition_threshold:
                self._degenerate = True
                return True
        # Word diversity check
        unique = len(set(w.lower() for w in window))
        if unique / len(window) < self._diversity_threshold:
            self._degenerate = True
            return True
        return False

    def trim_clean(self, full_text: str) -> str:
        """Trim degenerate tail from accumulated text.

        Walks backward to find the last non-repeating sentence boundary.
        """
        if not full_text:
            return full_text
        # Find sentence boundaries
        sentences = re.split(r'(?<=[.!?])\s+', full_text)
        if len(sentences) <= 2:
            return full_text
        # Walk backward, drop sentences that look like repeats
        seen: set[str] = set()
        keep = []
        for s in sentences:
            normalized = s.lower().strip()
            if normalized in seen:
                break
            seen.add(normalized)
            keep.append(s)
        return " ".join(keep) if keep else sentences[0]


# ─────────────────────────────────────────────────────────────────────────────
# Retry with exponential backoff (connection/timeout errors only)
# ─────────────────────────────────────────────────────────────────────────────

# Exception types that warrant a retry (connection and timeout errors)
_RETRYABLE_EXCEPTIONS = (
    ConnectionError, TimeoutError, OSError,
)

# Also catch httpx-specific errors if httpx is available
try:
    import httpx
    _RETRYABLE_EXCEPTIONS = (
        ConnectionError, TimeoutError, OSError,
        httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
        httpx.PoolTimeout, httpx.RemoteProtocolError,
    )
except ImportError:
    pass


def llm_retry(
    max_retries: int = 5,
    base_delay_ms: int = 500,
    max_delay_ms: int = 16000,
):
    """Decorator that adds retry with exponential backoff to async LLM calls.

    Only retries on connection/timeout errors. API errors (auth, rate limit,
    bad request) are NOT retried — those indicate a real problem.

    Args:
        max_retries: Maximum number of retry attempts (default: 5).
        base_delay_ms: Base delay in milliseconds (default: 500).
        max_delay_ms: Maximum delay cap in milliseconds (default: 16000).
    """
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return await fn(*args, **kwargs)
                except _RETRYABLE_EXCEPTIONS as exc:
                    last_exc = exc
                    if attempt >= max_retries:
                        logger.warning(
                            "LLM call failed after %d retries: %s",
                            max_retries, exc,
                        )
                        raise
                    # Exponential backoff with jitter
                    delay_ms = min(base_delay_ms * (2 ** attempt), max_delay_ms)
                    jitter_ms = random.uniform(0, delay_ms * 0.25)
                    total_delay = (delay_ms + jitter_ms) / 1000.0
                    logger.debug(
                        "LLM call attempt %d/%d failed (%s), retrying in %.1fs",
                        attempt + 1, max_retries, type(exc).__name__, total_delay,
                    )
                    await asyncio.sleep(total_delay)
            raise last_exc  # Should not reach here, but satisfy type checker
        return wrapper
    return decorator


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def capabilities(self) -> ProviderCapabilities:
        """Cost-optimization levers this provider supports.

        Default is the conservative ``none`` profile so existing/local providers
        keep working untouched; caching/batching adapters override this.
        """
        return ProviderCapabilities()

    @abstractmethod
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
        """Send messages and get a response.

        ``cache=True`` signals that the leading system + tools form a stable,
        reusable prefix worth prompt-caching. Adapters realize it per their
        ``capabilities().prompt_cache`` style; it is always safe (no-op where
        unsupported).
        """
        ...

    async def chat_with_continuation(
        self,
        messages: list[Message],
        *,
        max_continuations: int | None = None,
        max_total_output_tokens: int | None = None,
        **chat_kwargs,
    ) -> LLMResponse:
        """``chat()`` that auto-continues a completion truncated at the output
        token cap (``finish_reason == "length"``) and stitches the chunks.

        Every provider inherits this for free — the continue+stitch logic lives
        in one place (``adk.llm.continuation``) instead of each call-site rolling
        its own retry loop. Returns the fully-stitched LLMResponse. A no-op when
        the first response was not truncated or the kill-switch is set.
        """
        from .continuation import run_continuation  # lazy: avoid import cycle

        first = await self.chat(messages, **chat_kwargs)

        async def _again(_msgs: list[Message]) -> LLMResponse:
            return await self.chat(_msgs, **chat_kwargs)

        return await run_continuation(
            _again, messages, first,
            max_continuations=max_continuations,
            max_total_output_tokens=max_total_output_tokens,
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
        """Stream a chat response. Default implementation wraps chat()."""
        resp = await self.chat(
            messages, model, temperature, max_tokens, tools,
            tool_choice=tool_choice, top_p=top_p,
            repetition_penalty=repetition_penalty, cache=cache, **kwargs,
        )
        yield StreamChunk(content=resp.content, done=True, model=resp.model)

    @abstractmethod
    async def list_models(self) -> list[str]:
        """List available models from this provider."""
        ...

    async def health_check(self) -> bool:
        """Check if the provider is reachable."""
        try:
            models = await self.list_models()
            return len(models) > 0
        except Exception:
            return False

    async def submit_batch(self, requests: list[BatchRequest]) -> str:
        """Submit a batch of requests. Returns a provider batch id.

        Default implementation raises NotImplementedError. Override in subclasses
        that support batching (set batch=True in capabilities()).
        """
        raise NotImplementedError("batch not supported")

    async def poll_batch(self, batch_id: str) -> str:
        """Poll the status of a submitted batch.

        Returns a status string: "in_progress", "completed", "failed", or similar.
        Default raises NotImplementedError.
        """
        raise NotImplementedError("batch not supported")

    async def fetch_batch_results(self, batch_id: str) -> list[LLMResponse]:
        """Fetch results of a completed batch, ordered by custom_id.

        Default raises NotImplementedError.
        """
        raise NotImplementedError("batch not supported")


def _timer() -> float:
    """Return current time in ms for latency tracking."""
    return time.monotonic() * 1000


def messages_to_dicts(messages: list[Message]) -> list[dict]:
    """Convert Message objects to plain dicts for API calls."""
    result = []
    for m in messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.name:
            d["name"] = m.name
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            d["tool_calls"] = m.tool_calls
        result.append(d)
    return result
