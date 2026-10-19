"""opencode / codex adapters: default-harness tool calls become tool.call + tool.result.

Before these adapters both harnesses used ``translate_text``: opencode's output was
plain text and codex's JSON lines fell through to RAW, so no awsh session of the
default harness ever carried a harvestable tool call (plan P8).
"""

import json

from adk.harnesses import registry
from adk.harnesses.adapters import ADAPTERS, translate_codex, translate_opencode
from adk.harnesses.events import EventKind

#: ``opencode run --format json`` lines, the shape opencode 1.18 emits.
OPENCODE_LINES = [
    {"type": "step_start", "timestamp": 1, "sessionID": "ses_1", "part": {"type": "step-start"}},
    {"type": "tool_use", "timestamp": 2, "sessionID": "ses_1", "part": {
        "type": "tool", "tool": "bash", "callID": "call_1", "state": {
            "status": "completed", "input": {"command": "ls", "description": "list"},
            "output": "a.py\nb.py\n"}}},
    {"type": "tool_use", "timestamp": 3, "sessionID": "ses_1", "part": {
        "type": "tool", "tool": "read", "callID": "call_2", "state": {
            "status": "error", "input": {"filePath": "missing.py"},
            "error": "File not found"}}},
    {"type": "text", "timestamp": 4, "sessionID": "ses_1",
     "part": {"type": "text", "text": "Two files: a.py and b.py."}},
    {"type": "reasoning", "timestamp": 4, "sessionID": "ses_1",
     "part": {"type": "reasoning", "text": "thinking"}},
    {"type": "step_finish", "timestamp": 5, "sessionID": "ses_1", "part": {
        "type": "step-finish", "reason": "stop", "cost": 0, "tokens": {"input": 9}}},
    {"type": "error", "timestamp": 6, "sessionID": "ses_1",
     "error": {"name": "APIError", "data": {"message": "rate limited"}}},
]


def _all(lines, fn):
    out = []
    for ln in lines:
        out += fn(json.dumps(ln) if fn is translate_opencode else ln)
    return out


def test_opencode_tool_use_yields_paired_call_and_result():
    evs = _all(OPENCODE_LINES, translate_opencode)
    calls = [e for e in evs if e.kind == EventKind.TOOL_CALL]
    results = {e.tool_use_id: e for e in evs if e.kind == EventKind.TOOL_RESULT}
    assert [c.tool for c in calls] == ["bash", "read"]
    assert calls[0].data["input"] == {"command": "ls", "description": "list"}
    assert all(c.tool_use_id in results for c in calls)
    assert results["call_1"].data == {"output": "a.py\nb.py\n", "is_error": False}
    assert results["call_2"].data["is_error"] is True
    assert results["call_2"].data["output"] == "File not found"


def test_opencode_text_usage_error_and_no_turn_completed():
    evs = _all(OPENCODE_LINES, translate_opencode)
    kinds = [e.kind for e in evs]
    assert EventKind.TEXT_DELTA in kinds and EventKind.THINKING_DELTA in kinds
    assert EventKind.USAGE in kinds
    assert any(e.kind == EventKind.ERROR and e.text == "rate limited" for e in evs)
    # the session closes a one-shot turn itself; the adapter must not double it
    assert EventKind.TURN_COMPLETED not in kinds
    assert EventKind.RAW not in kinds


def test_opencode_plain_text_line_still_reads_as_text():
    (ev,) = translate_opencode("Two files: a.py and b.py.")
    assert ev.kind == EventKind.TEXT_DELTA and ev.text.endswith("\n")
    (ev,) = translate_opencode("{not json")
    assert ev.kind == EventKind.TEXT_DELTA
    (ev,) = translate_opencode(json.dumps({"type": "brand_new_event"}))
    assert ev.kind == EventKind.RAW


CODEX_LINES = [
    {"type": "thread.started", "thread_id": "th_1"},
    {"type": "turn.started"},
    {"type": "item.started", "item": {"id": "item_1", "type": "command_execution",
                                      "command": "bash -lc ls", "aggregated_output": "",
                                      "exit_code": None, "status": "in_progress"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "command_execution",
                                        "command": "bash -lc ls", "aggregated_output": "a.py\n",
                                        "exit_code": 0, "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "mcp_tool_call",
                                        "server": "aitheros", "tool": "get_status",
                                        "arguments": {"service": "genesis"},
                                        "result": {"content": [{"type": "text", "text": "up"}]},
                                        "status": "completed"}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "command_execution",
                                        "command": "false", "aggregated_output": "",
                                        "exit_code": 1, "status": "failed"}},
    {"type": "item.completed", "item": {"id": "item_4", "type": "reasoning", "text": "hm"}},
    {"type": "item.completed", "item": {"id": "item_5", "type": "agent_message",
                                        "text": "Listed one file."}},
    {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}},
]


def test_codex_items_yield_paired_calls_once():
    evs = _all(CODEX_LINES, translate_codex)
    calls = [e for e in evs if e.kind == EventKind.TOOL_CALL]
    results = {e.tool_use_id: e for e in evs if e.kind == EventKind.TOOL_RESULT}
    # item.started emits nothing; item.completed emits call + result exactly once
    assert [c.tool for c in calls] == ["shell", "mcp__aitheros__get_status", "shell"]
    assert [c.tool_use_id for c in calls] == ["item_1", "item_2", "item_3"]
    assert set(results) == {"item_1", "item_2", "item_3"}
    assert calls[0].data["input"] == {"command": "bash -lc ls"}
    assert calls[1].data["input"] == {"service": "genesis"}
    assert results["item_1"].data == {"output": "a.py\n", "is_error": False}
    assert results["item_3"].data["is_error"] is True
    kinds = [e.kind for e in evs]
    assert EventKind.SESSION_READY in kinds and EventKind.USAGE in kinds
    assert EventKind.TEXT_DELTA in kinds and EventKind.THINKING_DELTA in kinds
    assert EventKind.TURN_COMPLETED not in kinds and EventKind.RAW not in kinds


def test_registry_routes_both_harnesses_to_their_adapters(monkeypatch):
    assert ADAPTERS["opencode"] is translate_opencode and ADAPTERS["codex"] is translate_codex
    oc, cx = registry.get("opencode"), registry.get("codex")
    assert oc.adapter == "opencode" and cx.adapter == "codex" and cx.json_lines
    monkeypatch.delenv("AITHER_HARNESS_OPENCODE_FORMAT", raising=False)
    argv = oc.argv(registry.LaunchSpec(prompt="fix it"))
    assert argv[1:4] == ["run", "--format", "json"] and argv[-1] == "fix it"
    monkeypatch.setenv("AITHER_HARNESS_OPENCODE_FORMAT", "default")
    assert oc.argv(registry.LaunchSpec(prompt="fix it"))[1:] == ["run", "fix it"]
    # opencode lines arrive as raw strings (json_lines False) and still parse
    assert [e.kind for e in oc.translate(json.dumps(OPENCODE_LINES[1]))] == [
        EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
