"""Host side of the workflow -> expedition mirror.

Pure functions get real fixtures (journal shapes measured on live runs); the
gateway is a recording fake, so every test asserts what would have been
POSTED, not that a subprocess exited 0.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

from adk import workflow_mirror as wm

SCRIPT = """
export const meta = {
  name: 'review-changes',
  description: "Review changed files across dimensions, verify each finding",
  phases: [{ title: 'Review' }, { title: 'Verify', detail: 'refute each' }],   // trailing
}
const x = 1
"""


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_WORKFLOW_MIRROR_DIR", str(tmp_path / "mirror-state"))
    monkeypatch.setattr(wm, "read_bearer", lambda: "test-bearer")


class FakeGateway(wm.GatewayClient):
    """Records every tools/call; answers like the real fleet half."""

    def __init__(self, unknown_tools=()):
        super().__init__(bearer="test-bearer", opener=lambda *a, **k: None)
        self.calls = []
        self.unknown = set(unknown_tools)
        self._session_id = "sid"

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.unknown:
            raise wm.MirrorError(f"{name}: Unknown tool: {name}")
        if name == "expedition_mirror_create":
            return {"expedition_id": f"mirror-{arguments['source']}-{arguments['run_id'][:12]}",
                    "created": True}
        if name == "expedition_mirror_event":
            return {"expedition_id": "x", "task_id": "t", "acknowledged": True, "handled": True}
        return {}


def _journal(run_dir: Path, entries, mtime=None):
    run_dir.mkdir(parents=True, exist_ok=True)
    j = run_dir / "journal.jsonl"
    j.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(j, (mtime, mtime))
    return j


REAL_ENTRIES = [
    {"type": "launched"},
    {"type": "started", "key": "v2:aaa", "agentId": "a1", "label": "review:bugs",
     "phase": "Review"},
    {"type": "started", "key": "v2:bbb", "agentId": "a2", "label": "review:perf",
     "phase": "Review"},
    {"type": "result", "key": "v2:aaa", "agentId": "a1", "result": {"findings": 3}},
    {"type": "failed", "key": "v2:bbb", "agentId": ""},
    {"type": "started", "key": "v2:ccc", "agentId": "a3", "label": "verify:1", "phase": "Verify"},
    {"type": "result", "key": "v2:ccc", "agentId": "a3", "result": "ok"},
]


# --- meta --------------------------------------------------------------------


def test_parse_script_meta_handles_js_literal():
    meta = wm.parse_script_meta(SCRIPT)
    assert meta["name"] == "review-changes"
    assert meta["description"].startswith("Review changed files")
    assert meta["phases"] == [{"title": "Review", "detail": ""},
                              {"title": "Verify", "detail": "refute each"}]


def test_parse_script_meta_is_total():
    assert wm.parse_script_meta("") == {"name": "", "description": "", "phases": []}
    assert wm.parse_script_meta("export const meta = {name: broken")["name"] == ""


def test_parse_run_info_from_tool_response():
    text = ("Workflow launched.\nRun ID: wf_c1de533a-21b\n"
            "Transcript dir: C:\\Users\\x\\.claude\\projects\\p\\subagents\\workflows"
            "\\wf_c1de533a-21b\n"
            "Use /workflows to watch.")
    run_id, tdir = wm.parse_run_info(text)
    assert run_id == "wf_c1de533a-21b"
    assert tdir.endswith("wf_c1de533a-21b")
    assert wm.parse_run_info("no run here") == (None, None)


def test_parse_run_info_from_a_structured_response():
    # PostToolUse may carry the response as a dict or a list of content blocks; the
    # transcript dir must come back as the real path, not escaped JSON to end of string.
    text = ("Workflow launched.\nTranscript dir: C:\\Users\\x\\subagents\\workflows"
            "\\wf_c1de533a-21b\nScript file: s.js\nRun ID: wf_c1de533a-21b\n")
    want = ("wf_c1de533a-21b", "C:\\Users\\x\\subagents\\workflows\\wf_c1de533a-21b")
    assert wm.parse_run_info({"content": text}) == want
    assert wm.parse_run_info([{"type": "text", "text": text}]) == want
    assert wm.parse_run_info(json.dumps([{"type": "text", "text": text}])) == want
    assert wm.parse_run_info(None) == (None, None)


# --- journal -> events ---------------------------------------------------------


def test_journal_to_events_covers_every_recorded_type():
    state = wm.RunState(run_id="wf_x")
    events = wm.journal_to_events(REAL_ENTRIES, state)
    types = [e["type"] for e in events]
    assert types == ["log", "phase", "agent_started", "agent_started", "agent_result",
                     "agent_failed", "phase", "agent_started", "agent_result"]
    assert state.cursor == len(REAL_ENTRIES)
    assert (state.started, state.completed, state.failed) == (3, 2, 1)
    assert state.open_agents == {}
    assert state.phase == "Verify"
    result = [e for e in events if e["type"] == "agent_result"][0]
    assert result["label"] == "review:bugs" and json.loads(result["result"]) == {"findings": 3}
    failed = [e for e in events if e["type"] == "agent_failed"][0]
    assert failed["label"] == "review:perf"


def test_journal_to_events_resumes_from_cursor():
    state = wm.RunState(run_id="wf_x")
    wm.journal_to_events(REAL_ENTRIES[:3], state)
    assert state.open_agents == {"v2:aaa": "review:bugs", "v2:bbb": "review:perf"}
    later = wm.journal_to_events(REAL_ENTRIES, state)
    assert [e["type"] for e in later] == ["agent_result", "agent_failed", "phase",
                                          "agent_started", "agent_result"]


def test_result_is_capped():
    state = wm.RunState(run_id="wf_x")
    big = "x" * (wm.RESULT_CAP + 500)
    ev = wm.journal_to_events([{"type": "result", "key": "k", "result": big}], state)[0]
    assert len(ev["result"]) < wm.RESULT_CAP + 40 and ev["result"].endswith("(truncated)")


def test_unknown_journal_type_becomes_log_not_silence():
    state = wm.RunState(run_id="wf_x")
    ev = wm.journal_to_events([{"type": "teleport", "x": 1}], state)
    assert ev[0]["type"] == "log" and "teleport" in ev[0]["message"]


# --- terminal inference -------------------------------------------------------


def test_infer_terminal_waits_while_agents_open_or_journal_fresh():
    st = wm.RunState(run_id="r", started=1, open_agents={"k": "l"})
    assert wm.infer_terminal(st, journal_mtime=0, now=10_000) is None
    st = wm.RunState(run_id="r", started=1, completed=1)
    assert wm.infer_terminal(st, journal_mtime=10_000 - 5, now=10_000) is None


def test_infer_terminal_completed_and_failed_shapes():
    st = wm.RunState(run_id="r", started=2, completed=1, failed=1)
    ev = wm.infer_terminal(st, journal_mtime=0, now=wm.QUIET_SECONDS + 1)
    assert ev["type"] == "completed" and "inferred" in ev["result"]
    st = wm.RunState(run_id="r", started=1, failed=1)
    assert wm.infer_terminal(st, 0, wm.QUIET_SECONDS + 1)["type"] == "failed"
    st = wm.RunState(run_id="r", failed=1)  # failed before any agent started
    assert wm.infer_terminal(st, 0, wm.QUIET_SECONDS + 1)["type"] == "failed"
    st = wm.RunState(run_id="r")  # launched only, recent
    assert wm.infer_terminal(st, 0, wm.QUIET_SECONDS + 1) is None
    assert wm.infer_terminal(st, 0, wm.ORPHAN_SECONDS + 1)["type"] == "failed"
    st = wm.RunState(run_id="r", started=1, completed=1, finished="completed")
    assert wm.infer_terminal(st, 0, 10 ** 9) is None


# --- the sync ---------------------------------------------------------------------


def test_scan_and_mirror_creates_then_streams_then_finishes(tmp_path):
    root = tmp_path / "projects"
    run_dir = root / "sess" / "subagents" / "workflows" / "wf_abc123456789"
    old = time.time() - wm.QUIET_SECONDS - 5
    _journal(run_dir, REAL_ENTRIES, mtime=old)
    wm.record_run("wf_abc123456789", "sess", str(run_dir),
                  {"sha": "deadbeef", "meta": wm.parse_script_meta(SCRIPT)})
    gw = FakeGateway()

    summary = wm.scan_and_mirror(root=root, client=gw)
    assert summary["runs"] == 1 and summary["errors"] == {}
    names = [c[0] for c in gw.calls]
    assert names[0] == "expedition_mirror_create"
    create = gw.calls[0][1]
    assert create["run_id"] == "wf_abc123456789" and create["source"] == wm.SOURCE
    assert create["name"] == "review-changes"
    assert json.loads(create["phases_json"])[1]["title"] == "Verify"
    assert create["script_sha256"] == "deadbeef"
    events = [c[1] for c in gw.calls[1:]]
    assert all(e["run_id"] == "wf_abc123456789" and e["source"] == wm.SOURCE for e in events)
    assert events[-1]["type"] == "completed"
    assert summary["events"] == len(events)

    # Second pass: nothing new, nothing re-posted, run stays finished.
    gw.calls.clear()
    summary2 = wm.scan_and_mirror(root=root, client=gw)
    assert gw.calls == [] and summary2["events"] == 0
    assert wm.load_states()["wf_abc123456789"].finished == "completed"


def test_scan_and_mirror_resumes_from_cursor_across_passes(tmp_path):
    root = tmp_path / "projects"
    run_dir = root / "sess" / "subagents" / "workflows" / "wf_cursor000001"
    _journal(run_dir, REAL_ENTRIES[:3])
    gw = FakeGateway()
    wm.scan_and_mirror(root=root, client=gw)
    first = [c[1]["type"] for c in gw.calls if c[0] == "expedition_mirror_event"]
    assert first == ["log", "phase", "agent_started", "agent_started"]
    _journal(run_dir, REAL_ENTRIES)
    gw.calls.clear()
    wm.scan_and_mirror(root=root, client=gw)
    second = [c[1]["type"] for c in gw.calls if c[0] == "expedition_mirror_event"]
    assert second == ["agent_result", "agent_failed", "phase", "agent_started", "agent_result"]
    assert not any(c[0] == "expedition_mirror_create" for c in gw.calls)


def test_scan_and_mirror_skips_old_unbound_runs_but_keeps_bound_ones(tmp_path):
    root = tmp_path / "projects"
    month = time.time() - 30 * 86400
    _journal(root / "s" / "subagents" / "workflows" / "wf_oldunbound01", REAL_ENTRIES, month)
    bound = root / "s" / "subagents" / "workflows" / "wf_oldbound0001"
    _journal(bound, REAL_ENTRIES, month)
    wm.record_run("wf_oldbound0001", "s", str(bound),
                  {"sha": "x", "meta": wm.parse_script_meta(SCRIPT)})
    gw = FakeGateway()
    summary = wm.scan_and_mirror(root=root, client=gw)
    created = [c[1]["run_id"] for c in gw.calls if c[0] == "expedition_mirror_create"]
    assert created == ["wf_oldbound0001"] and summary["runs"] == 1


def test_scan_and_mirror_reports_a_dark_fleet_half(tmp_path):
    root = tmp_path / "projects"
    _journal(root / "s" / "subagents" / "workflows" / "wf_dark00000001", REAL_ENTRIES[:2])
    gw = FakeGateway(unknown_tools={"expedition_mirror_create"})
    summary = wm.scan_and_mirror(root=root, client=gw)
    assert "wf_dark00000001" in summary["errors"]
    assert "Unknown tool" in summary["errors"]["wf_dark00000001"]
    st = wm.load_states()["wf_dark00000001"]
    assert st.cursor == 0 and st.expedition_id == ""  # nothing consumed; retried next pass


def test_bind_run_records_and_creates(tmp_path):
    wm.write_pending("sess-1", SCRIPT)
    gw = FakeGateway()
    record, exp_id = wm.bind_run("wf_bind00000001", "sess-1", "C:/t", client=gw)
    assert exp_id == "mirror-claude-code-workflow-wf_bind00000"
    assert record["meta"]["name"] == "review-changes" and record["sha"]
    assert wm.read_runs()["wf_bind00000001"]["expedition_id"] == exp_id
    assert gw.calls[0][1]["transcript_dir"] == "C:/t"


def test_bind_run_survives_gateway_failure_and_leaves_a_retry_marker():
    gw = FakeGateway(unknown_tools={"expedition_mirror_create"})
    record, exp_id = wm.bind_run("wf_bind00000002", "nosession", "", client=gw)
    assert exp_id is None
    assert "Unknown tool" in wm.read_runs()["wf_bind00000002"]["last_error"]


def test_corrupt_state_file_is_quarantined_not_read_as_empty(tmp_path, caplog):
    wm.save_states({"wf_a": wm.RunState(run_id="wf_a", cursor=3)})
    assert wm.load_states()["wf_a"].cursor == 3
    wm.state_path().write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="adk.workflow_mirror"):
        assert wm.load_states() == {}
    assert "unreadable" in caplog.text
    leftovers = list(wm.state_path().parent.glob("state.json.corrupt-*"))
    assert len(leftovers) == 1 and not wm.state_path().exists()


# --- gateway envelope ----------------------------------------------------------


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self._body = body

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener_with(responses):
    calls = []

    def opener(req, timeout=None):
        calls.append(json.loads(req.data.decode("utf-8")))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    opener.calls = calls
    return opener


def test_client_handshakes_then_calls_and_decodes_text_json():
    resps = [
        _Resp(200, '{"jsonrpc":"2.0","id":1,"result":{}}',
              {"Content-Type": "application/json", "Mcp-Session-Id": "S1"}),
        _Resp(202, ""),  # notifications/initialized
        _Resp(200, json.dumps({"jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": json.dumps({"expedition_id": "e1"})}],
            "isError": False}})),
    ]
    op = _opener_with(resps)
    c = wm.GatewayClient(bearer="b", opener=op)
    out = c.call("expedition_mirror_create", {"run_id": "r"})
    assert out == {"expedition_id": "e1"}
    assert [m["method"] for m in op.calls] == ["initialize", "notifications/initialized",
                                              "tools/call"]
    assert op.calls[2]["params"] == {"name": "expedition_mirror_create",
                                     "arguments": {"run_id": "r"}}


def test_client_treats_unknown_tool_and_is_error_and_401_as_errors():
    def mk(text, is_error=False):
        return _Resp(200, json.dumps({"jsonrpc": "2.0", "id": 9, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_error}}))
    c = wm.GatewayClient(bearer="b", opener=_opener_with([mk("Unknown tool: x")]))
    c._session_id = "S"
    with pytest.raises(wm.MirrorError, match="Unknown tool"):
        c.call("x", {})
    c = wm.GatewayClient(bearer="b", opener=_opener_with([mk("boom", True)]))
    c._session_id = "S"
    with pytest.raises(wm.MirrorError, match="boom"):
        c.call("x", {})
    c = wm.GatewayClient(bearer="b", opener=_opener_with([mk('{"error": "permission_denied"}')]))
    c._session_id = "S"
    with pytest.raises(wm.MirrorError, match="permission_denied"):
        c.call("x", {})
    err = urllib.error.HTTPError("u", 401, "nope", {}, io.BytesIO(b""))
    c = wm.GatewayClient(bearer="b", opener=_opener_with([err]))
    c._session_id = "S"
    with pytest.raises(wm.MirrorError, match="re-mint"):
        c.call("x", {})


def test_client_reinitializes_when_the_gateway_expired_the_session():
    # Measured live: after a 15-minute quiet spell the gateway answers HTTP 404 with a
    # JSON-RPC "Session not found". The client must re-handshake and retry once.
    expired = urllib.error.HTTPError(
        "u", 404, "nf", {"Content-Type": "application/json"},
        io.BytesIO(b'{"jsonrpc":"2.0","id":"server-error","error":'
                   b'{"code":-32600,"message":"Session not found"}}'))
    ok = _Resp(200, json.dumps({"jsonrpc": "2.0", "id": 4, "result": {
        "content": [{"type": "text", "text": json.dumps({"ok": 1})}]}}))
    op = _opener_with([
        expired,
        _Resp(200, '{"jsonrpc":"2.0","id":2,"result":{}}',
              {"Content-Type": "application/json", "Mcp-Session-Id": "S2"}),
        _Resp(202, ""),
        ok,
    ])
    c = wm.GatewayClient(bearer="b", opener=op)
    c._session_id = "S1"
    assert c.call("t", {}) == {"ok": 1}
    assert [m.get("method") for m in op.calls] == ["tools/call", "initialize",
                                                  "notifications/initialized", "tools/call"]
    assert c._session_id == "S2"


def test_client_decodes_event_stream_frames():
    frame = json.dumps({"jsonrpc": "2.0", "id": 3, "result": {
        "content": [{"type": "text", "text": json.dumps({"ok": 1})}]}})
    body = f"event: message\ndata: {frame}\n\n"
    c = wm.GatewayClient(bearer="b", opener=_opener_with(
        [_Resp(200, body, {"Content-Type": "text/event-stream"})]))
    c._session_id = "S"
    assert c.call("t", {}) == {"ok": 1}


# --- discovery ---------------------------------------------------------------------


def test_scan_runs_accepts_projects_root_session_dir_and_workflows_dir(tmp_path):
    root = tmp_path / "projects"
    a = root / "s1" / "subagents" / "workflows" / "wf_a"
    b = root / "s2" / "subagents" / "workflows" / "wf_b"
    _journal(a, [{"type": "launched"}])
    _journal(b, [{"type": "launched"}])
    (root / "s2" / "subagents" / "workflows" / "not_a_run").mkdir()
    assert [p.name for p in wm.scan_runs(root)] == ["wf_a", "wf_b"]
    assert [p.name for p in wm.scan_runs(root / "s2")] == ["wf_b"]
    assert [p.name for p in wm.scan_runs(root / "s2" / "subagents" / "workflows")] == ["wf_b"]
    assert [p.name for p in wm.scan_runs(b)] == ["wf_b"]
    assert list(wm.scan_runs(tmp_path / "missing")) == []


def test_scan_runs_finds_the_real_project_session_layout(tmp_path):
    # ~/.claude/projects/<project>/<session>/subagents/workflows/wf_*: what Claude Code
    # actually writes, and what the default root must find.
    root = tmp_path / "projects"
    run = root / "C--repo" / "0491-session" / "subagents" / "workflows" / "wf_real"
    _journal(run, [{"type": "launched"}])
    assert [p.name for p in wm.scan_runs(root)] == ["wf_real"]
    assert [p.name for p in wm.scan_runs(root / "C--repo")] == ["wf_real"]
