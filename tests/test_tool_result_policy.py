"""L3: Layer-1 snipping is shaped by WHAT the tool result is.

Measured 2026-09-21 (babel-1141 on a 16,384-token slot): ~700 tokens per step, so a
40-step budget crosses the slot near step 15. A flat cap treats a stale file_read and
the latest failing test run alike; these tests pin the shape that keeps the loop fed."""
from __future__ import annotations

from adk.context_budget import (
    TAIL_HEAVY_TOOLS,
    TOOL_RESULT_POLICY,
    pinned_tool_call_ids,
    snip_old_tool_results,
    tool_names_by_call_id,
)


def _call(name: str, cid: str, **args):
    import json
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _exchange(name: str, cid: str, result: str, **args):
    return [
        {"role": "assistant", "content": "", "tool_calls": [_call(name, cid, **args)]},
        {"role": "tool", "tool_call_id": cid, "content": result},
    ]


def _history(*exchanges, tail: int = 0):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for ex in exchanges:
        msgs.extend(ex)
    for i in range(tail):
        msgs.append({"role": "user", "content": f"recent {i}"})
    return msgs


def test_names_and_arguments_are_recovered_from_dict_tool_calls():
    msgs = _history(_exchange("file_read", "c1", "x" * 10, path="a/b.py"))
    names = tool_names_by_call_id(msgs)
    assert names["c1"][0] == "file_read"
    assert "a/b.py" in str(names["c1"][1])


def test_per_tool_caps_apply_to_old_results_and_unknown_tools_keep_the_flat_cap():
    big = "L" * 5000
    msgs = _history(
        _exchange("file_read", "c1", big, path="a.py"),
        _exchange("file_list", "c2", big, path="."),
        _exchange("mystery_tool", "c3", big),
        tail=8,
    )
    reclaimed = snip_old_tool_results(msgs, max_chars=2000)
    assert reclaimed > 0
    by = {m["tool_call_id"]: m["content"] for m in msgs if m.get("role") == "tool"}
    # file_read: 1200 -> head 600 + tail 300 + marker; file_list: 600; unknown: 2000
    assert len(by["c1"]) < len(by["c3"]) < 5000
    assert len(by["c2"]) < len(by["c1"])
    assert TOOL_RESULT_POLICY["file_read"] == 1200 and TOOL_RESULT_POLICY["file_list"] == 600
    assert "chars snipped" in by["c1"] and "chars snipped" in by["c3"]


def test_tail_heavy_tools_keep_the_end():
    out = "".join(f"line {i}\n" for i in range(400)) + "FAILED test_x - AssertionError\n"
    msgs = _history(_exchange("run_tests", "t1", out), _exchange("file_read", "r1", "y" * 10),
                    tail=8)
    # t1 is the LATEST run_tests -> pinned; add a newer one so t1 becomes snippable
    msgs = _history(_exchange("run_tests", "t1", out), _exchange("run_tests", "t2", "PASS"),
                    tail=8)
    snip_old_tool_results(msgs)
    t1 = next(m["content"] for m in msgs if m.get("tool_call_id") == "t1")
    assert "FAILED test_x - AssertionError" in t1, "the failure summary at the END survives"
    assert "run_tests" in TAIL_HEAVY_TOOLS


def test_latest_run_tests_and_the_read_of_the_edited_file_are_pinned():
    big = "Z" * 6000
    msgs = _history(
        _exchange("file_read", "r1", big, path="pkg/mod.py"),
        _exchange("file_read", "r2", big, path="pkg/other.py"),
        _exchange("run_tests", "t1", big),
        _exchange("file_edit", "e1", "ok", path="pkg/mod.py", old_text="a", new_text="b"),
        tail=8,
    )
    pins = pinned_tool_call_ids(msgs)
    assert pins == {"t1", "r1"}
    snip_old_tool_results(msgs)
    by = {m["tool_call_id"]: m["content"] for m in msgs if m.get("role") == "tool"}
    assert by["r1"] == big and by["t1"] == big, "pinned results stay whole"
    assert len(by["r2"]) < 6000, "the other read is snipped"


def test_flat_rule_is_still_available_and_matches_the_old_behaviour():
    big = "Q" * 5000
    msgs = _history(_exchange("file_read", "c1", big, path="a.py"), tail=8)
    snip_old_tool_results(msgs, max_chars=2000, policy={}, pin=False)
    c1 = next(m["content"] for m in msgs if m.get("tool_call_id") == "c1")
    assert len(c1) < 5000 and c1.startswith("Q" * 1000)


def test_recent_results_are_never_touched():
    big = "R" * 9000
    msgs = _history(_exchange("file_read", "c1", big, path="a.py"))
    assert snip_old_tool_results(msgs) == 0
    assert msgs[-1]["content"] == big
