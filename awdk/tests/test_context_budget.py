"""Tests for adk.context_budget — the ReAct loop's context management.

The defect these guard against is not "compaction produces a bad summary" but
"compaction produces a message list the provider rejects with a 400". A naive
split between an assistant tool_calls message and its tool results is accepted
by OpenAI and rejected by DeepSeek and strict vLLM chat templates, so the bug
only appears on some backends — exactly the kind that ships.
"""

from __future__ import annotations

import pytest

from adk.context_budget import (
    TurnBudget,
    context_limit_for,
    estimate_tokens,
    find_safe_split_point,
    maybe_compact,
    snip_old_tool_results,
)


def _assistant_with_calls(*call_ids: str) -> dict:
    return {
        "role": "assistant",
        "content": "working",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "file_read", "arguments": "{}"}}
            for cid in call_ids
        ],
    }


def _tool_result(call_id: str, content: str = "result") -> dict:
    return {"role": "tool", "content": content, "tool_call_id": call_id}


# ── Token estimation ──────────────────────────────────────────────────────


def test_estimate_tokens_counts_string_content():
    messages = [{"role": "user", "content": "x" * 350}]
    assert estimate_tokens(messages) == 100


def test_estimate_tokens_counts_multimodal_content_parts():
    """A content-part list must not be silently counted as zero — that is how a
    multimodal turn slips past the budget check and blows the window."""
    messages = [{
        "role": "user",
        "content": [{"type": "text", "text": "y" * 350}],
    }]
    # The block's own keys ("text") are counted too — they are on the wire — so
    # this is ~100 plus a little, not exactly 100. What matters is that a
    # content-part list is not counted as zero.
    assert 100 <= estimate_tokens(messages) <= 105


def test_estimate_tokens_counts_tool_call_arguments():
    """Tool-call arguments can be large (a whole file in an edit call) and were
    the part most likely to be missed."""
    messages = [_assistant_with_calls("c1")]
    assert estimate_tokens(messages) > 0


def test_estimate_tokens_handles_dataclass_style_objects():
    class Msg:
        role = "user"
        content = "z" * 350
        tool_calls = None
        content_blocks = None

    assert estimate_tokens([Msg()]) == 100


# ── Context limits ────────────────────────────────────────────────────────


def test_context_limit_prefers_longest_matching_pattern():
    assert context_limit_for("claude-opus-5") == 200_000
    assert context_limit_for("gemma-2b") == 8_192
    assert context_limit_for("gemma4-12b") == 128_000


def test_context_limit_falls_back_conservatively_for_unknown_model():
    assert context_limit_for("some-unknown-model-v9") == 32_768


def test_context_limit_env_override_wins(monkeypatch):
    monkeypatch.setenv("ADK_CONTEXT_LIMIT", "8000")
    assert context_limit_for("claude-opus-5") == 8000


def test_context_limit_ignores_junk_env_override(monkeypatch):
    monkeypatch.setenv("ADK_CONTEXT_LIMIT", "not-a-number")
    assert context_limit_for("claude-opus-5") == 200_000


# ── Layer 1: snipping ─────────────────────────────────────────────────────


def test_snip_shortens_old_tool_results_but_keeps_head_and_tail():
    body = "HEAD" + ("m" * 5000) + "TAIL"
    messages = [_tool_result("c1", body)] + [{"role": "user", "content": "q"}] * 6

    reclaimed = snip_old_tool_results(messages, max_chars=1000)

    assert reclaimed > 0
    snipped = messages[0]["content"]
    assert snipped.startswith("HEAD")
    assert snipped.endswith("TAIL")
    assert "chars snipped" in snipped
    assert len(snipped) < len(body)


def test_snip_preserves_tool_call_id_and_role():
    """Breaking either would orphan the tool call and 400 the next request."""
    messages = [_tool_result("c1", "q" * 5000)] + [{"role": "user", "content": "x"}] * 6
    snip_old_tool_results(messages, max_chars=500)
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "c1"


def test_snip_leaves_recent_results_untouched():
    body = "r" * 5000
    messages = [{"role": "user", "content": "x"}] * 3 + [_tool_result("c1", body)]
    assert snip_old_tool_results(messages, max_chars=500, preserve_last_n=6) == 0
    assert messages[-1]["content"] == body


def test_snip_is_idempotent_at_the_same_threshold():
    messages = [_tool_result("c1", "s" * 5000)] + [{"role": "user", "content": "x"}] * 6
    snip_old_tool_results(messages, max_chars=1000)
    first = messages[0]["content"]
    assert snip_old_tool_results(messages, max_chars=1000) == 0
    assert messages[0]["content"] == first


def test_snip_does_not_re_snip_at_a_smaller_threshold():
    """The real idempotency risk: a caller that tightens max_chars between
    iterations would otherwise snip an already-snipped result again, stacking
    markers and eroding the retained head/tail until nothing useful remained.

    (At a fixed threshold the length check alone short-circuits, so only a
    shrinking threshold actually exercises the marker guard.)
    """
    messages = [_tool_result("c1", "s" * 20000)] + [{"role": "user", "content": "x"}] * 6

    snip_old_tool_results(messages, max_chars=8000)
    once = messages[0]["content"]
    assert "chars snipped" in once

    reclaimed = snip_old_tool_results(messages, max_chars=1000)

    assert reclaimed == 0, "already-snipped content was snipped again"
    assert messages[0]["content"] == once
    assert once.count("chars snipped") == 1, "snip markers stacked"


# ── Layer 2: safe split ───────────────────────────────────────────────────


def test_split_never_lands_on_a_tool_result():
    messages = (
        [{"role": "user", "content": "u" * 4000}]
        + [_assistant_with_calls("c1"), _tool_result("c1", "r" * 4000)] * 8
    )
    split = find_safe_split_point(messages, keep_ratio=0.3)
    if split > 0:
        assert messages[split]["role"] != "tool"


def _assert_tail_is_self_contained(messages: list, split: int) -> None:
    """Every tool result in the retained tail must have its declaring assistant
    message in the tail too, or the provider 400s on an orphaned tool_call."""
    tail = messages[split:]
    declared = {c["id"] for m in tail for c in (m.get("tool_calls") or [])}
    for message in tail:
        if message.get("role") == "tool":
            assert message["tool_call_id"] in declared, (
                f"tail (split={split}) contains a tool result "
                f"{message['tool_call_id']!r} whose assistant tool_call was "
                "compacted away — provider will 400"
            )


def test_split_never_orphans_a_tool_call():
    """The regression this whole module exists to prevent: if the retained tail
    starts part-way through a tool exchange, the assistant message declaring the
    call is compacted away and the surviving results reference nothing.

    Each exchange is small relative to the whole so the 30% keep-ratio lands
    mid-history rather than collapsing to 0 — a fixture whose split is 0 tests
    nothing at all.
    """
    messages = [{"role": "user", "content": "u" * 200}]
    for i in range(40):
        messages.append(_assistant_with_calls(f"c{i}"))
        messages.append(_tool_result(f"c{i}", "r" * 200))

    split = find_safe_split_point(messages, keep_ratio=0.3)
    assert split > 0, "fixture too small to exercise splitting — test would be vacuous"
    assert messages[split]["role"] != "tool"
    _assert_tail_is_self_contained(messages, split)


def test_split_handles_multi_call_batches():
    """A batch of parallel tool calls must be kept whole — splitting between
    sibling results leaves the assistant message with fewer results than calls."""
    messages = [{"role": "user", "content": "u" * 200}]
    for i in range(20):
        messages.append(_assistant_with_calls(f"a{i}", f"b{i}", f"d{i}"))
        for prefix in ("a", "b", "d"):
            messages.append(_tool_result(f"{prefix}{i}", "r" * 200))

    split = find_safe_split_point(messages, keep_ratio=0.3)
    assert split > 0, "fixture too small to exercise splitting — test would be vacuous"
    _assert_tail_is_self_contained(messages, split)


def test_split_keeps_every_sibling_result_with_its_call():
    """Stronger than self-containment: the tail's first assistant batch must have
    ALL of its results, not just the ones that happened to survive."""
    messages = [{"role": "user", "content": "u" * 200}]
    for i in range(20):
        messages.append(_assistant_with_calls(f"a{i}", f"b{i}", f"d{i}"))
        for prefix in ("a", "b", "d"):
            messages.append(_tool_result(f"{prefix}{i}", "r" * 200))

    split = find_safe_split_point(messages, keep_ratio=0.3)
    assert split > 0

    tail = messages[split:]
    results_present = {
        m["tool_call_id"] for m in tail if m.get("role") == "tool"
    }
    for message in tail:
        for call in message.get("tool_calls") or []:
            assert call["id"] in results_present, (
                f"assistant in tail declared {call['id']!r} but its result is "
                "missing — provider will 400 on an unanswered tool_call"
            )


def test_split_returns_zero_for_empty_history():
    assert find_safe_split_point([]) == 0


# ── maybe_compact orchestration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_compaction_when_under_budget():
    messages = [{"role": "user", "content": "short"}]
    result, compacted = await maybe_compact(messages, model="claude-opus-5")
    assert compacted is False
    assert result is messages


@pytest.mark.asyncio
async def test_compaction_without_summarizer_still_snips():
    """No LLM handle available must degrade to Layer 1, never raise."""
    messages = [_tool_result(f"c{i}", "x" * 20000) for i in range(10)]
    messages += [{"role": "user", "content": "now what"}]
    before = estimate_tokens(messages)

    result, compacted = await maybe_compact(messages, model="gemma-2b", summarize=None)

    assert compacted is True
    assert estimate_tokens(result) < before


@pytest.mark.asyncio
async def test_summarizer_failure_does_not_kill_the_turn():
    async def _boom(_prompt: str) -> str:
        raise RuntimeError("summarizer down")

    messages = [_tool_result(f"c{i}", "x" * 20000) for i in range(10)]
    messages += [{"role": "user", "content": "now what"}]

    result, compacted = await maybe_compact(messages, model="gemma-2b", summarize=_boom)

    assert compacted is True
    assert result  # history survives, just snipped


@pytest.mark.asyncio
async def test_empty_summary_is_rejected():
    """An empty summary would silently delete the entire history prefix."""
    async def _empty(_prompt: str) -> str:
        return "   "

    messages = [_tool_result(f"c{i}", "x" * 20000) for i in range(10)]
    messages += [{"role": "user", "content": "now what"}]
    result, _ = await maybe_compact(messages, model="gemma-2b", summarize=_empty)

    assert not any(
        "[Earlier conversation, compacted]" in str(m.get("content", "")) for m in result
    )


# ── TurnBudget ────────────────────────────────────────────────────────────


def test_budget_disabled_by_default_never_continues():
    """Ordinary conversational turns must be completely unaffected."""
    budget = TurnBudget(None)
    proceed, _ = budget.should_continue(0)
    assert proceed is False
    assert budget.stopped_for == "no_budget"


def test_budget_continues_while_room_remains():
    budget = TurnBudget(100_000)
    proceed, nudge = budget.should_continue(5_000)
    assert proceed is True
    assert "5%" in nudge or "5 %" in nudge


def test_budget_stops_at_completion_threshold():
    budget = TurnBudget(10_000)
    proceed, _ = budget.should_continue(9_500)
    assert proceed is False
    assert budget.stopped_for == "budget_exhausted"


def test_budget_stops_on_diminishing_returns():
    """Three near-empty continuations in a row means the model is spinning, and
    more nudging just burns tokens."""
    budget = TurnBudget(1_000_000)
    for tokens in (1_000, 2_000, 3_000):
        proceed, _ = budget.should_continue(tokens)
        assert proceed is True

    proceed, _ = budget.should_continue(3_050)   # tiny delta
    proceed, _ = budget.should_continue(3_100)   # tiny delta again
    assert proceed is False
    assert budget.stopped_for == "diminishing_returns"


def test_budget_does_not_stop_early_on_one_quiet_iteration():
    budget = TurnBudget(1_000_000)
    budget.should_continue(1_000, output_tokens_this_turn=1_000)
    proceed, _ = budget.should_continue(1_010, output_tokens_this_turn=1_010)
    assert proceed is True


def test_diminishing_returns_uses_OUTPUT_not_total_spend():
    """The live failure this guards. Total spend includes PROMPT tokens, which
    grow every iteration purely because the conversation gets longer — so a
    delta computed from total RISES monotonically and diminishing-returns can
    never fire.

    Observed against a local vLLM: a "name three risks, be brief" question was
    nudged 19+ times, deltas climbing 750 -> 4644, stoppable only by the budget
    ceiling. A scripted test with a FIXED token count per reply cannot see this,
    which is exactly why it shipped.

    Here output plateaus (the model has nothing left to say) while total keeps
    climbing. The plateau must win.
    """
    budget = TurnBudget(1_000_000)
    total, output = 0, 0

    # Four productive iterations: output genuinely grows.
    for _ in range(4):
        total += 5_000
        output += 1_000
        proceed, _ = budget.should_continue(total, output_tokens_this_turn=output)
        assert proceed is True

    # Now the model is done: output barely moves, but total keeps climbing
    # because the prompt keeps growing.
    for _ in range(2):
        total += 5_000          # prompt growth alone
        output += 50            # nothing meaningful produced
        proceed, _ = budget.should_continue(total, output_tokens_this_turn=output)

    assert proceed is False, (
        "still nudging while output has plateaued — diminishing-returns is being "
        "computed from total spend, not progress"
    )
    assert budget.stopped_for == "diminishing_returns"


def test_budget_ceiling_still_uses_TOTAL_spend():
    """The converse: the budget is about COST, so it must count prompt tokens
    too. Measuring the ceiling on output alone would let a turn burn its real
    budget many times over."""
    budget = TurnBudget(10_000)
    proceed, _ = budget.should_continue(9_500, output_tokens_this_turn=200)
    assert proceed is False
    assert budget.stopped_for == "budget_exhausted"
