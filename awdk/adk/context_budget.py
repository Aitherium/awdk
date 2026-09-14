"""Context-window budgeting for the ReAct loop.

The agent loop used to manage context by COUNT: keep the last 5 tool results,
overwrite every older one with the literal string ``[Prior result cleared]``.
That has three problems, all of which show up as "the agent got dumber the
longer it worked":

1. It destroys information instead of compressing it. By iteration 7 the agent
   has no record of what it read on iterations 1-2, so it re-reads the same
   file — which then trips the LoopGuard duplicate detector and the turn gets
   circuit-broken for "looping" when it was actually just amnesiac.
2. It is blind to the actual context window. A 200k-context model is starved
   identically to an 8k one, and a turn with five 100kB tool results still
   blows the window because nothing was over the count threshold.
3. It never summarizes, so genuinely long turns have no path to continue.

This module replaces that with a two-layer, token-budgeted scheme:

  Layer 1 (lossy but readable) — snip old tool results to head+tail with an
  explicit ``[... N chars snipped ...]`` marker, so the model can still see
  what the call was and roughly what came back.

  Layer 2 (summarize) — if still over budget, fold the old prefix into an LLM
  summary and keep the recent tail verbatim.

MESSAGE-STRUCTURE INVARIANT (the part a naive port gets wrong): an assistant
message declaring ``tool_calls`` must be followed CONTIGUOUSLY by exactly one
``tool`` message per tool_call_id. DeepSeek and strict vLLM chat templates
reject anything else with a 400. So Layer 1 never drops a tool message (it only
shortens content, preserving tool_call_id), and Layer 2 only ever splits at a
boundary where every preceding tool_call has its result — see
``find_safe_split_point``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

# Chars-per-token. Deliberately conservative (real English is ~4.0); undershooting
# the divisor overestimates tokens, which makes us compact slightly early rather
# than blow the window.
_CHARS_PER_TOKEN = 3.5

# Fraction of the context window we allow the message history to occupy before
# compacting. The remainder is headroom for the system prompt, tool schemas and
# the model's own output.
DEFAULT_BUDGET_RATIO = 0.7

# Model-family → context window. Matched by substring against a lowercased model
# id, longest pattern first, so "claude-opus-5" beats a bare "claude".
_CONTEXT_LIMITS: tuple[tuple[str, int], ...] = (
    ("claude-haiku-4", 200_000),
    ("claude-opus", 200_000),
    ("claude-sonnet", 200_000),
    ("claude-fable", 200_000),
    ("claude", 200_000),
    ("gpt-4o", 128_000),
    ("gpt-4", 128_000),
    ("o1", 200_000),
    ("deepseek", 128_000),
    ("qwen3", 128_000),
    ("qwen", 32_768),
    ("gemma4", 128_000),
    ("gemma", 8_192),
    ("kimi", 128_000),
    ("llama3", 128_000),
    ("mistral", 32_768),
    ("aither-orchestrator", 32_768),
)

# Fallback when the model id matches nothing known. Low on purpose: compacting a
# turn that did not need it costs one cheap summary call, whereas assuming a big
# window that is not there costs the whole turn with a hard 400.
FALLBACK_CONTEXT_LIMIT = 32_768


def context_limit_for(model: Any) -> int:
    """Best-effort context window (in tokens) for ``model``.

    An explicit ``ADK_CONTEXT_LIMIT`` env override wins — needed for self-hosted
    vLLM/Ollama deployments where the served window is a launch flag
    (``--max-model-len``) and is not inferable from the model name.

    ``model`` is annotated loosely on purpose. The only caller reaches it via
    ``getattr(self.llm, "model", None)``, so it is whatever the configured LLM
    handle exposes — which is a string for every real backend, but need not be.
    Naming the parameter ``str | None`` did not make it one: a backend whose
    ``.model`` was not a plain string raised ``TypeError`` from the substring
    scan below and took down the whole turn, mid-compaction, for a value that is
    only ever used to pick a heuristic default. An unrecognisable model now
    degrades to the fallback window, which is exactly what an unrecognised model
    *name* already did.
    """
    override = os.environ.get("ADK_CONTEXT_LIMIT", "").strip()
    if override:
        try:
            value = int(override)
            if value > 0:
                return value
        except ValueError:
            logger.debug("Ignoring non-integer ADK_CONTEXT_LIMIT=%r", override)

    if model is None:
        name = ""
    elif isinstance(model, str):
        name = model.lower()
    else:
        logger.debug(
            "context_limit_for got a non-string model (%s); using the fallback window",
            type(model).__name__,
        )
        return FALLBACK_CONTEXT_LIMIT
    for pattern, limit in _CONTEXT_LIMITS:
        if pattern in name:
            return limit
    return FALLBACK_CONTEXT_LIMIT


def _content_chars(content: Any) -> int:
    """Character count of a message ``content``, which may be a str or an
    OpenAI-style content-part list (multimodal turns)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                for value in block.values():
                    if isinstance(value, str):
                        total += len(value)
            elif isinstance(block, str):
                total += len(block)
        return total
    return 0


def estimate_tokens(messages: Iterable[Any]) -> int:
    """Approximate token count for a message list.

    Accepts adk ``Message`` dataclasses or plain dicts, so the same estimator
    works on both sides of the provider boundary.
    """
    total_chars = 0
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content", "")
            tool_calls = message.get("tool_calls") or []
            blocks = message.get("content_blocks") or []
        else:
            content = getattr(message, "content", "")
            tool_calls = getattr(message, "tool_calls", None) or []
            blocks = getattr(message, "content_blocks", None) or []

        total_chars += _content_chars(content)
        total_chars += _content_chars(blocks)

        for call in tool_calls:
            if isinstance(call, dict):
                total_chars += _content_chars(list(call.values()))
            else:
                total_chars += len(str(call))

    return int(total_chars / _CHARS_PER_TOKEN)


_SNIP_MARKER = re.compile(r"\[\.\.\. \d+ chars snipped \.\.\.\]")


def _is_already_snipped(content: str) -> bool:
    return bool(_SNIP_MARKER.search(content))


def snip_old_tool_results(
    messages: list,
    max_chars: int = 2000,
    preserve_last_n: int = 6,
) -> int:
    """Layer 1: shorten oversized tool results older than the last ``preserve_last_n``
    messages, keeping a head and a tail with an explicit marker between them.

    Mutates ``messages`` in place. Returns the number of characters reclaimed.

    Only the ``content`` of ``tool``-role messages is touched — role,
    ``tool_call_id`` and message ORDER are all preserved, so the
    assistant-tool_calls → tool-results pairing invariant cannot be broken here.
    """
    cutoff = max(0, len(messages) - preserve_last_n)
    reclaimed = 0

    for index in range(cutoff):
        message = messages[index]
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
        if role != "tool":
            continue

        content = (
            message.get("content", "")
            if isinstance(message, dict)
            else getattr(message, "content", "")
        )
        if not isinstance(content, str) or len(content) <= max_chars:
            continue
        if _is_already_snipped(content):
            continue

        head = content[: max_chars // 2]
        tail = content[-(max_chars // 4):]
        dropped = len(content) - len(head) - len(tail)
        snipped = f"{head}\n[... {dropped} chars snipped ...]\n{tail}"

        if isinstance(message, dict):
            message["content"] = snipped
        else:
            message.content = snipped
        reclaimed += dropped

    return reclaimed


def _declared_tool_call_ids(message: Any) -> set[str]:
    """tool_call ids declared by an assistant message (empty for other roles)."""
    tool_calls = (
        message.get("tool_calls")
        if isinstance(message, dict)
        else getattr(message, "tool_calls", None)
    ) or []
    ids: set[str] = set()
    for call in tool_calls:
        if isinstance(call, dict):
            call_id = call.get("id", "")
        else:
            call_id = getattr(call, "id", "")
        if call_id:
            ids.add(call_id)
    return ids


def find_safe_split_point(messages: list, keep_ratio: float = 0.3) -> int:
    """Index splitting ``messages`` so roughly ``keep_ratio`` of the tokens are in
    the retained tail, snapped FORWARD to a structurally safe boundary.

    "Safe" means: the tail must not begin part-way through a tool exchange. If
    the naive token-ratio split lands on a ``tool`` message, or on an assistant
    message whose declared tool_calls have results after the split, the boundary
    is advanced until every open tool_call is closed. Splitting naively is the
    bug that produces a 400 ("insufficient tool messages following tool_calls
    message") on DeepSeek and strict vLLM templates.

    Returns 0 when no safe split exists (caller should leave history alone).
    """
    if not messages:
        return 0

    total = estimate_tokens(messages)
    if total <= 0:
        return 0

    target = int(total * keep_ratio)
    running = 0
    naive = 0
    for index in range(len(messages) - 1, -1, -1):
        running += estimate_tokens([messages[index]])
        if running >= target:
            naive = index
            break

    # Advance to a boundary that starts a clean exchange: not a tool result, and
    # not immediately after an assistant turn with unresolved tool_calls.
    split = naive
    while split < len(messages):
        candidate = messages[split]
        role = (
            candidate.get("role")
            if isinstance(candidate, dict)
            else getattr(candidate, "role", "")
        )
        if role == "tool":
            split += 1
            continue

        # Walk back to the nearest assistant turn; if it declared tool_calls whose
        # results live at or after `split`, this is mid-exchange.
        open_ids: set[str] = set()
        for back in range(split - 1, -1, -1):
            previous = messages[back]
            prev_role = (
                previous.get("role")
                if isinstance(previous, dict)
                else getattr(previous, "role", "")
            )
            if prev_role == "assistant":
                open_ids = _declared_tool_call_ids(previous)
                break
            if prev_role != "tool":
                break

        if not open_ids:
            break

        satisfied_after_split = set()
        for message in messages[split:]:
            role_after = (
                message.get("role")
                if isinstance(message, dict)
                else getattr(message, "role", "")
            )
            if role_after != "tool":
                continue
            call_id = (
                message.get("tool_call_id")
                if isinstance(message, dict)
                else getattr(message, "tool_call_id", None)
            )
            if call_id:
                satisfied_after_split.add(call_id)

        if open_ids & satisfied_after_split:
            split += 1
            continue
        break

    if split >= len(messages):
        return 0
    return split


def render_history_for_summary(messages: list, per_message_chars: int = 600) -> str:
    """Flatten a message prefix into text for the summarizer."""
    lines: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            role = message.get("role", "?")
            content = message.get("content", "")
            tool_calls = message.get("tool_calls") or []
        else:
            role = getattr(message, "role", "?")
            content = getattr(message, "content", "")
            tool_calls = getattr(message, "tool_calls", None) or []

        if isinstance(content, list):
            rendered = "(structured content)"
        else:
            rendered = str(content or "")
        rendered = rendered[:per_message_chars]

        if tool_calls:
            names = []
            for call in tool_calls:
                if isinstance(call, dict):
                    names.append(call.get("function", {}).get("name") or call.get("name", "?"))
                else:
                    names.append(getattr(call, "name", "?"))
            rendered = f"{rendered} (called: {', '.join(names)})".strip()

        lines.append(f"[{role}] {rendered}")
    return "\n".join(lines)


SUMMARY_INSTRUCTION = (
    "Summarize this agent's work-in-progress conversation so it can continue "
    "without the raw history. Preserve, verbatim where possible: file paths "
    "touched, exact identifiers/symbols, commands run and their outcomes, "
    "decisions already made, facts established from tool results, and anything "
    "still outstanding. Omit pleasantries and reasoning that led nowhere. "
    "Write it as notes to your future self, not as a report to a user."
)


async def maybe_compact(
    messages: list,
    model: str | None,
    summarize: Callable[[str], Awaitable[str]] | None = None,
    budget_ratio: float = DEFAULT_BUDGET_RATIO,
    make_message: Callable[[str, str], Any] | None = None,
) -> tuple[list, bool]:
    """Bring ``messages`` under the model's context budget.

    Returns ``(messages, compacted)``. ``messages`` may be the same list object
    (Layer 1 mutates in place) or a new list (Layer 2 rebuilds it).

    ``summarize`` performs the LLM summary call; when omitted, Layer 2 is
    skipped and only snipping applies — so this is always safe to call even
    where no LLM handle is available.
    ``make_message`` builds a message object of the caller's type from
    ``(role, content)``; defaults to a plain dict.
    """
    limit = context_limit_for(model)
    threshold = int(limit * budget_ratio)
    current = estimate_tokens(messages)

    if current <= threshold:
        return messages, False

    logger.info(
        "[CONTEXT] %d tokens over budget %d (model=%s, window=%d) — compacting",
        current, threshold, model, limit,
    )

    # ── Layer 1: snip old tool results ──
    reclaimed = snip_old_tool_results(messages)
    if reclaimed:
        current = estimate_tokens(messages)
        logger.info("[CONTEXT] Layer 1 snipped %d chars → %d tokens", reclaimed, current)
    if current <= threshold:
        return messages, True

    # ── Layer 2: summarize the old prefix ──
    if summarize is None:
        logger.warning(
            "[CONTEXT] Still %d tokens over budget %d but no summarizer available "
            "— proceeding with snipped history",
            current, threshold,
        )
        return messages, True

    split = find_safe_split_point(messages)
    if split <= 0:
        logger.warning("[CONTEXT] No safe split point — proceeding with snipped history")
        return messages, True

    old, recent = messages[:split], messages[split:]
    build = make_message or (lambda role, content: {"role": role, "content": content})

    try:
        summary = await summarize(
            f"{SUMMARY_INSTRUCTION}\n\n{render_history_for_summary(old)}"
        )
    except Exception as exc:  # noqa: BLE001 — compaction must never kill the turn
        logger.warning("[CONTEXT] Summarization failed (%s) — keeping snipped history", exc)
        return messages, True

    summary = (summary or "").strip()
    if not summary:
        logger.warning("[CONTEXT] Summarizer returned nothing — keeping snipped history")
        return messages, True

    compacted = [
        build("user", f"[Earlier conversation, compacted]\n{summary}"),
        build("assistant", "Understood — continuing from that context."),
        *recent,
    ]
    logger.info(
        "[CONTEXT] Layer 2 compacted %d messages → summary + %d recent (%d → %d tokens)",
        len(old), len(recent), current, estimate_tokens(compacted),
    )
    return compacted, True


# ── Turn budget: continue-when-there-is-room ────────────────────────────────
#
# A ReAct loop that treats "the model returned no tool calls" as unconditionally
# final stops the moment the model loses momentum — which, on a long task, is
# usually well before the task is done. The model says "I've made a good start,
# here's a summary" and the loop obligingly ends the turn.
#
# The fix is to make stopping a DECISION rather than a default: if the turn has
# consumed only a fraction of its allotted token budget and output is still
# substantive, nudge the model to keep working instead of accepting the stop.
# Termination then comes from one of two honest signals — the budget is nearly
# spent, or successive iterations have stopped producing anything (diminishing
# returns) — rather than from the model's momentary willingness to quit.
#
# This is OPT-IN: with no budget configured the tracker always says stop, so
# ordinary conversational turns are completely unaffected.

# Fraction of budget past which we stop asking for more work.
COMPLETION_THRESHOLD = 0.9

# An iteration producing fewer than this many tokens counts as "not progressing".
DIMINISHING_THRESHOLD = 500

# Continuations required before diminishing-returns detection can fire, so a
# single quiet iteration early on doesn't end a turn prematurely.
MIN_CONTINUATIONS_BEFORE_DIMINISHING = 3


class TurnBudget:
    """Tracks token spend across a turn and decides continue-vs-stop.

    ``budget`` is the turn's token allowance; ``None`` or non-positive disables
    continuation entirely (``should_continue`` always returns False).
    """

    def __init__(self, budget: int | None = None) -> None:
        self.budget = budget
        self.continuations = 0
        self._last_delta = 0
        # Cumulative COMPLETION tokens at the last decision — the progress signal.
        # Deliberately not total spend; see should_continue().
        self._last_progress = 0
        self.stopped_for: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.budget and self.budget > 0)

    def should_continue(
        self,
        tokens_used_this_turn: int,
        output_tokens_this_turn: int | None = None,
    ) -> tuple[bool, str]:
        """Decide whether to push the model for another iteration.

        ``tokens_used_this_turn`` is total SPEND (prompt + completion) and is
        measured against the budget. ``output_tokens_this_turn`` is cumulative
        COMPLETION tokens and is what diminishing-returns is measured on.

        These must be two different numbers, and conflating them is a real bug
        this code shipped with: total spend grows every iteration purely because
        the conversation gets longer, so a per-iteration delta computed from it
        RISES monotonically and the diminishing-returns guard can never fire.
        Observed live against a local vLLM — a "name three risks, be brief"
        question was nudged 19+ times with deltas climbing 750 -> 4644, and only
        the budget ceiling would have stopped it. A scripted test with a fixed
        token count per reply cannot see this; only real traffic can.

        Falls back to total when output is not supplied, which preserves the old
        (wrong) behaviour rather than crashing on an older caller — the caller in
        this repo always supplies it.

        Returns ``(continue_, nudge_message)``. When ``continue_`` is False the
        reason is recorded on ``stopped_for``.
        """
        if not self.enabled:
            self.stopped_for = "no_budget"
            return False, ""

        budget = self.budget or 0
        progress = (
            output_tokens_this_turn
            if output_tokens_this_turn is not None
            else tokens_used_this_turn
        )
        delta = progress - self._last_progress

        diminishing = (
            self.continuations >= MIN_CONTINUATIONS_BEFORE_DIMINISHING
            and delta < DIMINISHING_THRESHOLD
            and self._last_delta < DIMINISHING_THRESHOLD
        )

        if diminishing:
            self.stopped_for = "diminishing_returns"
            return False, ""

        if tokens_used_this_turn >= budget * COMPLETION_THRESHOLD:
            self.stopped_for = "budget_exhausted"
            return False, ""

        self.continuations += 1
        self._last_delta = delta
        self._last_progress = progress
        pct = int((tokens_used_this_turn / budget) * 100) if budget else 0
        return True, continuation_nudge(pct, tokens_used_this_turn, budget)


# Effort at/above which a turn gets a continuation budget by default. Tiers 7-10
# are defined as "architecture / root-cause / multi-file builds" — work that is
# supposed to run to completion, and exactly where a model quitting early is most
# expensive. Below this, turns behave exactly as before (no budget, no nudging).
DEFAULT_BUDGET_MIN_EFFORT = 7

# Tokens of budget granted per effort tier above the threshold.
BUDGET_PER_EFFORT_TIER = 50_000


def default_token_budget(effort: int | None) -> int | None:
    """Turn token budget implied by an effort tier, or None for no budget.

    ``ADK_TURN_TOKEN_BUDGET`` overrides: a positive integer forces that budget at
    every effort, and ``0`` disables continuation entirely (the escape hatch for
    anyone who does not want the extra spend).

    COST NOTE: a turn with a budget may invoke the model several extra times
    before it stops, bounded by the completion threshold, diminishing-returns
    detection and the loop ceiling. That is the intended trade — finishing the
    work costs more than abandoning it.
    """
    override = os.environ.get("ADK_TURN_TOKEN_BUDGET", "").strip()
    if override:
        try:
            value = int(override)
            return value if value > 0 else None
        except ValueError:
            logger.debug("Ignoring non-integer ADK_TURN_TOKEN_BUDGET=%r", override)

    tier = effort if isinstance(effort, int) else 5
    if tier < DEFAULT_BUDGET_MIN_EFFORT:
        return None
    # Return None rather than a non-positive number if the threshold is ever
    # retuned: TurnBudget treats <=0 as "disabled", so a negative would be
    # silently inert — correct behaviour reached by accident, which is the kind
    # of thing that stops being correct when someone changes it.
    budget = (tier - DEFAULT_BUDGET_MIN_EFFORT + 1) * BUDGET_PER_EFFORT_TIER
    return budget if budget > 0 else None


def continuation_nudge(pct: int, used: int, budget: int) -> str:
    """Message asking the model to keep working when budget remains."""
    return (
        f"[BUDGET: {pct}% used — {used:,} of {budget:,} tokens]\n"
        "You stopped without calling any tools, but this turn has substantial "
        "budget left. If the task is genuinely complete, say so plainly and stop. "
        "Otherwise KEEP GOING: verify what you produced actually works, handle the "
        "cases you skipped, and finish the parts you deferred. Do not re-summarize "
        "work you have already described — do the remaining work."
    )
