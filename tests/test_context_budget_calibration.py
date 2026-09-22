"""L3b: the compaction trigger is calibrated with the provider's real prompt_tokens.

Measured 2026-09-21 (L-bonsai-S1-full3, mesa-2394, 10 tools on a 16,384 slot): the real
prompt grew 3,256 -> 15,953 tokens over 12 calls while ``estimate_tokens`` (message chars
only) never crossed the 0.7 threshold, so nothing compacted and the slot was full at step
12 before the first edit. The overhead the estimator cannot see -- schema, injected system
prompt, tokenizer gap -- is measured from the last response and added to the check.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from adk.context_budget import calibrate_overhead, estimate_tokens, maybe_compact


def _tool_result(cid: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": cid, "content": content}


def test_calibrate_overhead_is_the_gap_and_never_negative():
    assert calibrate_overhead(3000, 5600) == 2600
    assert calibrate_overhead(3000, 2000) == 0
    assert calibrate_overhead(3000, 0) == 0          # no usage reported = no claim
    assert calibrate_overhead(3000, None) == 0


@pytest.mark.asyncio
async def test_overhead_makes_compaction_fire_where_the_estimate_alone_would_not():
    # 6 old results of 3,000 chars ~ 5,150 estimated tokens: under gemma-2b 8k*0.7 = 5,734
    messages = [_tool_result(f"c{i}", "x" * 3000) for i in range(6)]
    messages += [{"role": "user", "content": "now what"}]
    est = estimate_tokens(messages)
    limit_threshold = int(8192 * 0.7)
    assert est < limit_threshold, "fixture must sit under the threshold on estimate alone"

    same, compacted = await maybe_compact(list(messages), model="gemma-2b", summarize=None)
    assert compacted is False

    overhead = limit_threshold - est + 500          # the provider counted 500 more
    snipped, compacted = await maybe_compact(list(messages), model="gemma-2b", summarize=None,
                                             overhead_tokens=overhead)
    assert compacted is True
    assert estimate_tokens(snipped) < est


@pytest.mark.asyncio
async def test_zero_or_negative_overhead_is_the_old_behaviour():
    messages = [{"role": "user", "content": "short"}]
    _, c0 = await maybe_compact(messages, model="gemma-2b", overhead_tokens=0)
    _, c1 = await maybe_compact(messages, model="gemma-2b", overhead_tokens=-999)
    assert c0 is False and c1 is False


def test_the_loop_calibrates_from_the_response_and_passes_it_back():
    """Source-level: the ReAct loop measures the estimate at send time, reads the
    response's prompt_tokens, and hands the overhead to maybe_compact."""
    src = (Path(__file__).resolve().parents[1] / "adk" / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "maybe_compact"]
    assert calls, "the loop no longer calls maybe_compact"
    assert any(any(k.arg == "overhead_tokens" for k in c.keywords) for c in calls), \
        "maybe_compact is called without overhead_tokens: the calibration is unwired"
    assert "calibrate_overhead(_est_at_send" in src, "the loop never calibrates from a response"


def test_the_summarizer_is_pinned_to_the_turn_model():
    """Source-level: the Layer 2 summarizer call names the SAME model as the turn, so a
    local arm's compaction is not silently served by a cloud tier."""
    agent_py = Path(__file__).resolve().parents[1] / "adk" / "agent.py"
    tree = ast.parse(agent_py.read_text(encoding="utf-8"))
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
           and n.name == "_summarize_history"]
    assert fns, "_summarize_history is gone"
    calls = [c for c in ast.walk(fns[0]) if isinstance(c, ast.Call)
             and getattr(c.func, "attr", "") == "chat"]
    assert calls, "no chat call inside _summarize_history"
    assert any(k.arg == "model" for k in calls[0].keywords), "the summarizer does not pin model="


@pytest.mark.asyncio
async def test_layer1b_trims_a_huge_recent_result():
    """One fresh 87k-char tool result must not survive compaction whole: Layer 1 exempts
    the last six messages, so Layer 1b trims recent results to a slot-sized cap."""
    messages = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "tool_call_id": "c1", "content": "z" * 87_000}]
    before = estimate_tokens(messages)
    out, compacted = await maybe_compact(messages, model="bonsai2-27b", summarize=None)
    assert compacted is True
    assert estimate_tokens(out) < before // 3, "the recent result was not trimmed"
    assert out[-1]["tool_call_id"] == "c1", "order and pairing survive"
