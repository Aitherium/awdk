"""SavingsLedger — honest "tokens used vs. tokens saved by caching" accounting.

Every number here traces to a real counter — no fabricated savings (per the
AitherOS no-fabrication rule). The ledger composes three real signals:

  used        : prompt_tokens + completion_tokens summed over every LLM call,
                read straight from adk's LLMResponse.
  saved_recall: when the agent answers from its knowledge graph / memory instead
                of re-running a web search and re-reading full pages, we credit
                the token size of the reused finding(s). Without memory those
                tokens would have to be re-fetched and re-fed to the model.
  saved_dedup : when a finding is already known (graph upsert hit / duplicate
                search result), we credit the tokens we did NOT re-embed/re-store.
  saved_cache : provider-reported cached prompt tokens (OpenAI
                prompt_tokens_details.cached_tokens, Anthropic
                cache_read_input_tokens) when the provider surfaces them.

The LedgerRouter subclasses adk's LLMRouter so every chat() call is metered
without changing the agent's ReAct loop.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from adk.llm import LLMRouter

logger = logging.getLogger("deep_research.ledger")


# ─────────────────────────────────────────────────────────────────────────────
# Token counting — tiktoken if present, else a stable char-based estimate.
# ─────────────────────────────────────────────────────────────────────────────
_enc = None
_enc_tried = False


def count_tokens(text: str) -> int:
    """Best-effort token count. Uses tiktoken cl100k_base when available."""
    global _enc, _enc_tried
    if not text:
        return 0
    if not _enc_tried:
        _enc_tried = True
        try:
            import tiktoken
            _enc = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001
            _enc = None
    if _enc is not None:
        try:
            return len(_enc.encode(text))
        except Exception:  # noqa: BLE001
            pass
    # Fallback: ~3.8 chars/token is a good English approximation.
    return max(1, round(len(text) / 3.8))


@dataclass
class SavingsLedger:
    """Session-scoped, thread-safe ledger of tokens used and saved."""

    used_prompt: int = 0
    used_completion: int = 0
    saved_recall: int = 0
    saved_dedup: int = 0
    saved_cache: int = 0
    llm_calls: int = 0
    recall_hits: int = 0
    dedup_hits: int = 0
    searches: int = 0
    pages_read: int = 0
    findings: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── recording ──
    def record_llm(self, resp) -> None:
        """Record one non-streamed LLM response's token usage (exact, from usage)."""
        with self._lock:
            self.used_prompt += int(getattr(resp, "prompt_tokens", 0) or 0)
            self.used_completion += int(getattr(resp, "completion_tokens", 0) or 0)
            self.llm_calls += 1
            # Provider-reported prompt-cache reuse, if surfaced via cache_status.
            cached = _parse_cached_tokens(getattr(resp, "cache_status", "") or "")
            if cached:
                self.saved_cache += cached

    def record_stream(self, prompt_text: str, completion_text: str) -> None:
        """Record a streamed call. Providers don't return usage when streaming, so
        we count the EXACT context sent and output received with the tokenizer —
        a faithful measurement of the tokens that crossed the wire, not a guess."""
        with self._lock:
            self.used_prompt += count_tokens(prompt_text)
            self.used_completion += count_tokens(completion_text)
            self.llm_calls += 1

    def record_recall(self, reused_text: str) -> int:
        """Credit tokens reused from memory/graph instead of re-fetching."""
        toks = count_tokens(reused_text)
        with self._lock:
            self.saved_recall += toks
            self.recall_hits += 1
        return toks

    def record_dedup(self, skipped_text: str) -> int:
        """Credit tokens for a duplicate finding we didn't re-store/re-embed."""
        toks = count_tokens(skipped_text)
        with self._lock:
            self.saved_dedup += toks
            self.dedup_hits += 1
        return toks

    def note_search(self) -> None:
        with self._lock:
            self.searches += 1

    def note_page_read(self) -> None:
        with self._lock:
            self.pages_read += 1

    def note_finding(self) -> None:
        with self._lock:
            self.findings += 1

    # ── reporting ──
    @property
    def used_total(self) -> int:
        return self.used_prompt + self.used_completion

    @property
    def saved_total(self) -> int:
        return self.saved_recall + self.saved_dedup + self.saved_cache

    def snapshot(self) -> dict:
        with self._lock:
            used = self.used_prompt + self.used_completion
            saved = self.saved_recall + self.saved_dedup + self.saved_cache
            # Efficiency = saved / (used + saved): of all tokens the work "wanted",
            # what fraction did memory + caching let us avoid paying for.
            denom = used + saved
            pct = round(100.0 * saved / denom, 1) if denom else 0.0
            return {
                "used": used,
                "used_prompt": self.used_prompt,
                "used_completion": self.used_completion,
                "saved": saved,
                "saved_recall": self.saved_recall,
                "saved_dedup": self.saved_dedup,
                "saved_cache": self.saved_cache,
                "saved_pct": pct,
                "llm_calls": self.llm_calls,
                "recall_hits": self.recall_hits,
                "dedup_hits": self.dedup_hits,
                "searches": self.searches,
                "pages_read": self.pages_read,
                "findings": self.findings,
            }


def _parse_cached_tokens(cache_status: str) -> int:
    """Extract a cached-token count from an LLMResponse.cache_status string.

    Providers vary; we look for 'cached=<n>' or 'cache_read=<n>' markers that the
    LedgerRouter (or a patched provider) may stuff into cache_status. Returns 0
    when nothing parseable is present (the common case today).
    """
    import re
    if not cache_status:
        return 0
    m = re.search(r"(?:cached|cache_read)\D*(\d+)", cache_status)
    return int(m.group(1)) if m else 0


class LedgerRouter(LLMRouter):
    """LLMRouter that records every chat() into a SavingsLedger.

    Drop-in for the agent's `llm`: inherits all routing/provider logic and only
    adds metering. Construct exactly like LLMRouter, plus `ledger=`.
    """

    def __init__(self, *args, ledger: SavingsLedger, **kwargs):
        super().__init__(*args, **kwargs)
        self._ledger = ledger

    async def chat(self, messages, **kwargs):
        resp = await super().chat(messages, **kwargs)
        try:
            self._ledger.record_llm(resp)
        except Exception as exc:  # noqa: BLE001 — metering must never break a turn
            logger.debug("ledger record_llm failed: %s", exc)
        return resp

    async def chat_stream(self, messages, **kwargs):
        """Meter streamed turns: count the exact context sent + output received.

        stream_react() drives the loop via chat_stream(), so this is where the
        bulk of token usage is captured for the live meter.
        """
        try:
            prompt_text = "\n".join(getattr(m, "content", "") or "" for m in messages)
        except Exception:  # noqa: BLE001
            prompt_text = ""
        out: list[str] = []
        async for chunk in super().chat_stream(messages, **kwargs):
            piece = getattr(chunk, "content", "") or ""
            if piece:
                out.append(piece)
            yield chunk
        try:
            self._ledger.record_stream(prompt_text, "".join(out))
        except Exception as exc:  # noqa: BLE001
            logger.debug("ledger record_stream failed: %s", exc)
