"""Multi-provider captured NDJSON replay tests across all 5 structured harnesses.

Verifies deterministic end-to-end event normalization (session init, tool call/result
pairing, error propagation, streaming deltas, usage/cost extraction, and RAW fallback
for unknown future schema shapes) across Claude Code, Gemini CLI 0.70, Codex CLI,
OpenCode 1.18, and Google Antigravity (`agentapi` / `agy`).
"""
from __future__ import annotations

import json

from adk.harnesses.adapters import (
    ADAPTERS,
    translate_antigravity,
    translate_claude,
    translate_codex,
    translate_gemini,
    translate_opencode,
)
from adk.harnesses.events import EventKind


def _replay(adapter_fn, lines: list, *, as_str: bool = False):
    out = []
    for item in lines:
        payload = json.dumps(item) if (as_str and isinstance(item, dict)) else item
        out.extend(adapter_fn(payload))
    return out


def test_claude_full_turn_replay_with_partial_deltas_tools_and_unknown_event():
    transcript = [
        {"type": "system", "subtype": "init", "model": "claude-opus-4-6",
         "session_id": "cl-sess-1", "cwd": "/repo", "tools": ["Read", "Bash"]},
        {"type": "stream_event", "event": {"delta": {"type": "thinking_delta", "thinking": "check"}}},
        {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Checking..."}}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "app.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "print(1)", "is_error": False},
        ]}},
        {"type": "rate_limit_event", "retry_after_ms": 500},
        {"type": "future_protocol_v9", "payload": {"x": 1}},
        {"type": "result", "subtype": "success", "result": "All good.", "session_id": "cl-sess-1",
         "total_cost_usd": 0.012, "duration_ms": 1420, "num_turns": 1,
         "usage": {"input_tokens": 120, "output_tokens": 35}},
    ]
    evs = _replay(translate_claude, transcript)
    kinds = [e.kind for e in evs]
    assert kinds[0] == EventKind.SESSION_READY
    assert evs[0].data["harness_session_id"] == "cl-sess-1"
    assert EventKind.THINKING_DELTA in kinds and EventKind.TEXT_DELTA in kinds
    assert EventKind.TOOL_CALL in kinds and EventKind.TOOL_RESULT in kinds
    assert EventKind.NOTICE in kinds
    assert EventKind.RAW in kinds, "unknown future event must never vanish"
    assert kinds[-2:] == [EventKind.USAGE, EventKind.TURN_COMPLETED]
    assert evs[-2].data["cost_usd"] == 0.012
    assert evs[-1].data["is_error"] is False


def test_gemini_070_full_turn_replay_tools_usage_and_raw():
    transcript = [
        {"type": "init", "session_id": "gem-42", "model": "gemini-3-pro"},
        {"type": "message", "role": "user", "content": "inspect"},
        {"type": "tool_use", "tool_name": "grep", "tool_id": "g1", "parameters": {"q": "TODO"}},
        {"type": "tool_result", "tool_id": "g1", "status": "success", "output": "a.py:10"},
        {"type": "message", "role": "assistant", "content": "Found 1 TODO.", "delta": True},
        {"type": "gemini_telemetry_v2", "metrics": {"latency": 12}},
        {"type": "result", "status": "success", "session_id": "gem-42",
         "stats": {"total_tokens": 420, "duration_ms": 810}},
    ]
    evs = _replay(translate_gemini, transcript)
    assert evs[0].kind == EventKind.SESSION_READY and evs[0].data["harness_session_id"] == "gem-42"
    calls = [e for e in evs if e.kind == EventKind.TOOL_CALL]
    results = [e for e in evs if e.kind == EventKind.TOOL_RESULT]
    assert len(calls) == 1 and len(results) == 1 and results[0].data["is_error"] is False
    assert any(e.kind == EventKind.RAW for e in evs)
    assert evs[-1].kind == EventKind.TURN_COMPLETED and evs[-1].data["is_error"] is False


def test_codex_full_turn_replay_file_change_mcp_error_and_raw():
    transcript = [
        {"type": "thread.started", "thread_id": "cx-9"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "i1", "type": "file_change", "status": "completed",
            "changes": [{"path": "src/a.py", "kind": "update"}],
        }},
        {"type": "item.completed", "item": {
            "id": "i2", "type": "mcp_tool_call", "server": "awgit", "tool": "status",
            "arguments": {}, "error": {"message": "lease expired"}, "status": "failed",
        }},
        {"type": "item.completed", "item": {"id": "i3", "type": "todo_list", "items": ["done"]}},
        {"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": "Updated."}},
        {"type": "codex.experimental", "v": 1},
        {"type": "turn.completed", "usage": {"input_tokens": 90, "output_tokens": 12}},
    ]
    evs = _replay(translate_codex, transcript)
    assert evs[0].kind == EventKind.SESSION_READY and evs[0].data["harness_session_id"] == "cx-9"
    calls = {e.tool_use_id: e for e in evs if e.kind == EventKind.TOOL_CALL}
    results = {e.tool_use_id: e for e in evs if e.kind == EventKind.TOOL_RESULT}
    assert calls["i1"].tool == "apply_patch" and results["i1"].data["is_error"] is False
    assert calls["i2"].tool == "mcp__awgit__status" and results["i2"].data["is_error"] is True
    assert "lease expired" in str(results["i2"].data["output"])
    assert any(e.kind == EventKind.NOTICE for e in evs)
    assert any(e.kind == EventKind.RAW for e in evs)
    assert any(e.kind == EventKind.USAGE for e in evs)


def test_opencode_full_turn_replay_mixed_json_and_plain_text():
    transcript = [
        {"type": "step_start", "sessionID": "oc-7", "part": {"type": "step-start"}},
        {"type": "tool_use", "sessionID": "oc-7", "part": {
            "tool": "edit", "callID": "c1",
            "state": {"status": "completed", "input": {"path": "x.py"}, "output": "ok"},
        }},
        "plain diagnostic line",
        {"type": "text", "sessionID": "oc-7", "part": {"text": "Done."}},
        {"type": "step_finish", "sessionID": "oc-7", "part": {"cost": 0.004, "tokens": {"input": 50}}},
        {"type": "unknown_oc_event", "x": 2},
    ]
    evs = _replay(translate_opencode, transcript, as_str=True)
    assert any(e.kind == EventKind.TOOL_CALL and e.tool == "edit" for e in evs)
    assert any(e.kind == EventKind.TEXT_DELTA and "plain diagnostic" in e.text for e in evs)
    assert any(e.kind == EventKind.USAGE and e.data["cost_usd"] == 0.004 for e in evs)
    assert any(e.kind == EventKind.RAW for e in evs)


def test_antigravity_agentapi_and_agy_replay():
    transcript = [
        {"conversation": {"id": "ag-conv-101", "title": "Fix gap"}},
        {"type": "message", "role": "assistant", "content": "Applying patch."},
        {"error": "transient rpc warning"},
        {"unknown_ag_field": 123},
        "plain line from agy",
    ]
    evs = _replay(translate_antigravity, transcript, as_str=True)
    assert evs[0].kind == EventKind.SESSION_READY
    assert evs[0].data["harness_session_id"] == "ag-conv-101"
    assert any(e.kind == EventKind.TEXT_DELTA and "Applying patch." in e.text for e in evs)
    assert any(e.kind == EventKind.ERROR and "transient rpc warning" in e.text for e in evs)
    assert any(e.kind == EventKind.RAW for e in evs)
    assert any(e.kind == EventKind.TEXT_DELTA and "plain line from agy" in e.text for e in evs)
    assert set(("claude", "gemini", "codex", "opencode", "antigravity")) <= set(ADAPTERS)
