"""wake(session_id) and the one positional read of a harness session log.

A harness session writes every event to ``<root>/<id>/events.jsonl``. These tests kill
a real (fake) harness process, wake it, and assert it comes back under the SAME id with
its seq continuing, its harness-native id passed back as resume, and the log tail handed
to it as resume context -- and that ``GET /sessions/{id}/events?after=&limit=`` pages the
log positionally whether or not the daemon still holds the session.
"""

from __future__ import annotations

import sys

import pytest

from adk.harnesses import registry
from adk.harnesses.events import EventKind, HarnessEvent
from adk.harnesses.manager import ManagerError, SessionManager
from adk.harnesses.registry import HarnessSpec, Transport
from adk.harnesses.session import (
    SessionConfig,
    SessionState,
    read_events_page,
    resume_context,
)

TOKEN = "test-token-for-testing-only"
LAUNCHES: list = []


def _argv(spec, launch):
    LAUNCHES.append(launch)
    # A long-lived child that waits on stdin: a harness that is up until killed.
    return [spec.binary, "-c", "import sys; sys.stdin.read()"]


@pytest.fixture
def fake_harness(monkeypatch):
    LAUNCHES.clear()
    spec = HarnessSpec(
        id="wake-probe", label="Wake Probe", description="sleeps on stdin",
        transport=Transport.STRUCTURED_BIDI, binary=sys.executable, adapter="text",
        supports_resume=True, build_argv=_argv,
    )
    monkeypatch.setitem(registry.SPECS, spec.id, spec)
    return spec


def _start_with_history(mgr: SessionManager, tmp_path):
    session = mgr.create(SessionConfig(harness="wake-probe", cwd=str(tmp_path),
                                       system_prompt_append="ORIGINAL"))
    session._emit(HarnessEvent(kind=EventKind.TURN_STARTED, text="fix the parser", turn=1))
    session._emit(HarnessEvent(kind=EventKind.TEXT_DELTA, text="Looking at ", turn=1))
    session._emit(HarnessEvent(kind=EventKind.TEXT_DELTA, text="parser.py now", turn=1))
    session._emit(HarnessEvent(kind=EventKind.TOOL_CALL, tool="Edit", turn=1))
    session._emit(HarnessEvent(kind=EventKind.TURN_COMPLETED, turn=1,
                               data={"harness_session_id": "native-abc"}))
    return session


def _kill(session):
    assert session._proc is not None and session._proc.poll() is None
    session._proc.kill()
    assert session.wait_closed(10)
    assert session.state == SessionState.EXITED


def test_a_killed_harness_wakes_with_its_tail(fake_harness, tmp_path):
    mgr = SessionManager(root=tmp_path / "sessions")
    session = _start_with_history(mgr, tmp_path)
    sid = session.id
    _kill(session)
    last = session.last_seq

    out = mgr.wake(sid)
    try:
        assert out["woken"] is True and out["resumed_from_seq"] == last
        woken = mgr.get_session(sid)
        assert woken is not session and woken.id == sid
        assert woken.state not in (SessionState.EXITED, SessionState.FAILED)
        launch = LAUNCHES[-1]
        assert launch.resume_session_id == "native-abc"
        ctx = launch.system_prompt_append
        assert ctx.startswith("ORIGINAL")
        assert "user: fix the parser" in ctx
        assert "assistant: Looking at parser.py now" in ctx
        assert "tool: Edit" in ctx
        assert f'after_seq="{last}"' in ctx
        # The log continues: the wake notice is the very next seq, nothing renumbered.
        page, _ = read_events_page(woken._transcript, last)
        assert page[0]["seq"] == last + 1 and page[0]["data"].get("wake") is True
        seqs = [e["seq"] for e in read_events_page(woken._transcript, 0)[0]]
        assert seqs == sorted(set(seqs))
    finally:
        mgr.stop_all()


def test_wake_is_idempotent_on_a_live_session(fake_harness, tmp_path):
    mgr = SessionManager(root=tmp_path / "sessions")
    session = _start_with_history(mgr, tmp_path)
    try:
        before = len(LAUNCHES)
        out = mgr.wake(session.id)
        assert out == {"woken": False, "already_live": True, "session": session.info()}
        assert len(LAUNCHES) == before
        assert mgr.get_session(session.id) is session
    finally:
        mgr.stop_all()


def test_wake_after_daemon_restart_reads_the_log_from_disk(fake_harness, tmp_path):
    root = tmp_path / "sessions"
    first = SessionManager(root=root)
    session = _start_with_history(first, tmp_path)
    sid = session.id
    _kill(session)

    fresh = SessionManager(root=root)  # the daemon restarted: nothing in memory
    try:
        out = fresh.wake(sid)
        assert out["woken"] is True
        # The resume block is built on the ORIGINAL config, never stacked on a prior wake.
        fresh.stop(sid)
        again = SessionManager(root=root).wake(sid)
        ctx = LAUNCHES[-1].system_prompt_append
        assert ctx.count("<resumed-session") == 1 and ctx.startswith("ORIGINAL")
        assert again["woken"] is True
    finally:
        fresh.stop_all()


def test_wake_refuses_unknown_and_traversal_ids(tmp_path):
    mgr = SessionManager(root=tmp_path / "sessions")
    with pytest.raises(ManagerError, match="no such session"):
        mgr.wake("deadbeef00000000")
    with pytest.raises(ManagerError, match="invalid session id"):
        mgr.wake("../etc")


def test_events_page_is_positional(fake_harness, tmp_path):
    mgr = SessionManager(root=tmp_path / "sessions")
    session = _start_with_history(mgr, tmp_path)
    try:
        last = session.last_seq
        p1 = mgr.read_events(session.id, after=0, limit=2)
        assert [e["seq"] for e in p1["events"]] == [1, 2] and p1["more"] is True
        assert p1["next_after"] == 2
        p2 = mgr.read_events(session.id, after=p1["next_after"], limit=1000)
        assert p2["events"][0]["seq"] == 3 and p2["events"][-1]["seq"] == last
        assert p2["more"] is False
        assert mgr.read_events(session.id, after=last)["events"] == []
    finally:
        mgr.stop_all()

    # Same shape, served from disk, after the daemon forgot the session.
    cold = SessionManager(root=tmp_path / "sessions")
    d = cold.read_events(session.id, after=1, limit=2)
    assert [e["seq"] for e in d["events"]] == [2, 3]
    assert d["state"] == "dormant" and d["more"] is True


def test_read_events_page_skips_a_torn_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"seq": 1, "kind": "notice"}\n{"seq": 2, "ki\n{"seq": 3, "kind": "x"}\n',
                    encoding="utf-8")
    page, more = read_events_page(path, 0)
    assert [e["seq"] for e in page] == [1, 3] and more is False


def test_resume_context_keeps_the_most_recent_within_budget():
    events = [{"kind": "turn.started", "text": f"ask {i}"} for i in range(50)]
    ctx = resume_context(events, budget=40)
    assert ctx.endswith("user: ask 49") and "ask 0" not in ctx and len(ctx) <= 40


# ── daemon routes ───────────────────────────────────────────────────────────


@pytest.fixture
def daemon(monkeypatch, fake_harness, tmp_path):
    from fastapi.testclient import TestClient

    from adk.harnesses.daemon import create_app

    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    mgr = SessionManager(root=tmp_path / "sessions")
    client = TestClient(create_app(manager=mgr, token=TOKEN))
    client.headers = {"Authorization": f"Bearer {TOKEN}"}
    yield client, mgr
    mgr.stop_all()


def test_daemon_wake_route_and_events_paging(daemon, tmp_path):
    client, mgr = daemon
    session = _start_with_history(mgr, tmp_path)
    _kill(session)
    r = client.post(f"/sessions/{session.id}/wake", json={"tail": 50})
    assert r.status_code == 200, r.text
    assert r.json()["woken"] is True
    again = client.post(f"/sessions/{session.id}/wake", json={})
    assert again.status_code == 200 and again.json()["woken"] is False

    page = client.get(f"/sessions/{session.id}/events", params={"after": 0, "limit": 3})
    assert page.status_code == 200
    body = page.json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 3] and body["more"] is True
    # The older ?since= spelling still answers everything after the cursor.
    legacy = client.get(f"/sessions/{session.id}/events", params={"since": 3}).json()
    assert legacy["events"][0]["seq"] == 4 and legacy["more"] is False

    assert client.post("/sessions/nosuchsession00/wake", json={}).status_code == 404


def test_daemon_wake_and_dormant_read_are_owner_only(monkeypatch, fake_harness, tmp_path):
    """Both arms: the owner token wakes / reads a dormant log; an agent token cannot."""
    from fastapi.testclient import TestClient

    from adk.harnesses import daemon as dmod

    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    mgr = SessionManager(root=tmp_path / "sessions")
    session = _start_with_history(mgr, tmp_path)
    _kill(session)
    cold = SessionManager(root=tmp_path / "sessions")
    app = dmod.create_app(manager=cold, token=TOKEN)
    agent = dmod.Principal(id="peer-agent", plan="agent")
    owner_client = TestClient(app)
    owner_client.headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        assert owner_client.get(f"/sessions/{session.id}/events").status_code == 200
        app.dependency_overrides[_auth_dep(app)] = lambda: agent
        peer = TestClient(app)
        assert peer.get(f"/sessions/{session.id}/events").status_code == 403
        assert peer.post(f"/sessions/{session.id}/wake", json={}).status_code == 403
        app.dependency_overrides.clear()
        assert owner_client.post(f"/sessions/{session.id}/wake", json={}).status_code == 200
    finally:
        cold.stop_all()


def _auth_dep(app):
    """The ``auth`` dependency callable the wake route declares (for an override)."""
    for route in app.routes:
        if getattr(route, "path", "") == "/sessions/{session_id}/wake":
            for dep in route.dependant.dependencies:
                if dep.name == "principal":
                    return dep.call
    raise AssertionError("wake route has no principal dependency")
