"""Auto-continuation for truncated LLM completions (adk-local primitive).

ONE place in adk for the continue-until-complete logic, so every provider and
call-site inherits it instead of each hand-rolling its own retry loop. When a
completion stops because it hit the output token cap (``finish_reason ==
"length"``), this continues the generation and STITCHES the chunks into a
complete answer.

This mirrors the monorepo's ``lib/core/llm_continuation`` semantics, but is
fully self-contained — adk is a standalone, distributed package and must not
import the monorepo. The two implementations are kept behaviourally in sync
(overlap-dedup + restart-detect stitch; bounded by rounds AND output ceiling
AND a no-growth break).

Design rules:
- Trigger ONLY on truncation (finish_reason == "length") — never on stop /
  tool_calls / tool_use / content_filter.
- Bounded three ways so it can never loop forever or run away on cost.
- Never continue a tool-call turn (that is handled by the ReAct loop, not here).
- Does NOT mutate the caller's message list — it builds its own prefill messages
  per round, so the agent's conversation history stays clean.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from .base import LLMResponse, Message

logger = logging.getLogger("adk.llm.continuation")

DEFAULT_CONTINUE_TEXT = (
    "Your previous response was cut off before it finished. Continue EXACTLY "
    "where you left off — do not repeat any earlier text, do not restart, and "
    "output only the remaining content."
)

# A token is ~4 chars; budget in chars to stay tokenizer-agnostic.
_CHARS_PER_TOKEN = 4

# Defaults match the monorepo `chat` task profile; env-overridable.
_DEFAULT_MAX_CONTINUATIONS = 2
_DEFAULT_MAX_TOTAL_OUTPUT_TOKENS = 8192

_OFF = ("off", "0", "false", "disabled", "no")


def continuation_enabled() -> bool:
    """False iff the ``ADK_LLM_CONTINUATION`` kill-switch disables it."""
    return os.environ.get("ADK_LLM_CONTINUATION", "").strip().lower() not in _OFF


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip()))
    except (TypeError, ValueError):
        return default


def stitch(accumulated: str, chunk: str, max_overlap: int = 800) -> str:
    """Join a continuation ``chunk`` onto ``accumulated`` text robustly.

    Handles three real continuation behaviours:
      1. Seamless prefill continuation (no overlap) → plain concatenation.
      2. The model repeats a tail of the prior output → drop the overlapping
         prefix of ``chunk`` (longest suffix-of-accumulated that prefixes chunk).
      3. The model RESTARTS the whole output (re-emits the opening) → keep the
         longer/more-complete of the two rather than concatenating two partials.
    """
    if not accumulated:
        return chunk or ""
    if not chunk:
        return accumulated

    # (3) Restart detection — the chunk re-emits the document's opening.
    head = accumulated[:60].strip()
    probe = head[:30]
    if len(probe) >= 12 and probe in chunk[:120]:
        return chunk if len(chunk) >= len(accumulated) else accumulated

    # (2) Overlap dedup — longest suffix of accumulated that is a prefix of chunk.
    window = min(max_overlap, len(accumulated), len(chunk))
    tail = accumulated[-window:]
    overlap = 0
    for n in range(window, 0, -1):
        if tail[-n:] == chunk[:n]:
            overlap = n
            break

    # (1) Plain concat (overlap == 0) or trimmed concat.
    return accumulated + chunk[overlap:]


async def run_continuation(
    chat_fn: Callable[[list[Message]], Awaitable[LLMResponse]],
    base_messages: list[Message],
    first: LLMResponse,
    *,
    max_continuations: int | None = None,
    max_total_output_tokens: int | None = None,
    continue_text: str = DEFAULT_CONTINUE_TEXT,
    truncation_reason: str = "length",
    enabled: bool | None = None,
) -> LLMResponse:
    """Continue a truncated completion until it finishes or a bound is hit.

    ``chat_fn(messages) -> LLMResponse`` runs ONE completion (the caller binds
    model/tools/effort/etc. into the closure). Returns a single LLMResponse with
    the fully-stitched ``content``, the FINAL ``finish_reason`` (stays "length"
    only if a bound was hit), and ``completion_tokens`` summed across rounds.

    Provider- and router-agnostic: works with any object that produces an
    ``LLMResponse``. Returns ``first`` unchanged when continuation does not apply.
    """
    if enabled is None:
        enabled = continuation_enabled()
    if not enabled or first.finish_reason != truncation_reason:
        return first

    if max_continuations is None:
        max_continuations = _env_int("ADK_LLM_MAX_CONTINUATIONS", _DEFAULT_MAX_CONTINUATIONS)
    if max_total_output_tokens is None:
        max_total_output_tokens = _env_int(
            "ADK_LLM_MAX_TOTAL_OUTPUT_TOKENS", _DEFAULT_MAX_TOTAL_OUTPUT_TOKENS
        )

    content = first.content or ""
    finish = first.finish_reason
    total_completion = first.completion_tokens or 0
    rounds = 0
    result = first
    max_chars = max(1, max_total_output_tokens) * _CHARS_PER_TOKEN

    while finish == truncation_reason and rounds < max_continuations and len(content) < max_chars:
        prev_len = len(content)
        cont_messages = list(base_messages) + [
            Message(role="assistant", content=content),
            Message(role="user", content=continue_text),
        ]
        try:
            r = await chat_fn(cont_messages)
        except Exception as e:  # never let a continuation failure mask the partial
            logger.warning("continuation round %d failed: %s: %s", rounds + 1, type(e).__name__, e)
            break

        rounds += 1
        chunk = r.content or ""
        if not chunk.strip():
            break

        content = stitch(content, chunk)
        finish = r.finish_reason or "stop"
        total_completion += r.completion_tokens or 0
        result = r

        if len(content) <= prev_len:
            break

    if rounds:
        # Settle the chosen response object with the stitched whole.
        result.content = content
        result.finish_reason = finish
        result.completion_tokens = total_completion
        logger.info(
            "continuation: %d round(s), final finish_reason=%r, %d chars",
            rounds, finish, len(content),
        )
    return result
