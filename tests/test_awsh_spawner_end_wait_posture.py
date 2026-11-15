"""awsh session lifecycle gaps measured 2026-11-12, each with a test that can fail.

(a) a session spawned with defaults could not be prompted by the tab that spawned it
    (mailbox only, stuck at turn 0) -- the spawner now reaches its own child's pty;
(b) ``awsh_wait(timeout=45)`` hit the client's 20 s socket budget and reported the
    daemon "unreachable";
(c) no tool could END a session, and ``awsh_interrupt`` answered a bare false;
(d) a local model profile parked by the fleet posture started a session that died on
    its first turn -- it is now refused at spawn, naming the posture and what is served;
(5) ``/awrun`` stays owner-only for agent tabs, and the tools say so.

The daemon arms run the real ``create_app`` under ``TestClient`` with only the harness
PROCESS stubbed. Nothing touches a live ``~/.aither``, daemon or model spine.
"""

from __future__ import annotations

import socket
import threading

import pytest
from fastapi.testclient import TestClient

from adk.harnesses import local_servable as ls
from adk.harnesses import mcp_stdio as m

TOKEN = "root-bearer-for-tests-only"


# ── a daemon with real routes and a stubbed harness process ─────────────────────────


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    registry_path = tmp_path / "harness_tokens.json"
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(registry_path))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import rooms as rooms_mod
    from adk.harnesses import session as session_mod
    from adk.harnesses import session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", registry_path)
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(directory_mod, "_directory",
                        directory_mod.SessionDirectory(discover_fn=lambda: []))
    # No test may read this box's model spine.
    monkeypatch.setattr(daemon, "local_profile_refusal", lambda profile: "")

    typed: dict[str, list[str]] = {}
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)

    def fake_send(self, text):
        typed.setdefault(self.id, []).append(text)
        return True

    monkeypatch.setattr(session_mod.HarnessSession, "submit", fake_send)
    monkeypatch.setattr(session_mod.HarnessSession, "send", fake_send)

    mgr = SessionManager(root=tmp_path / "sessions")
    client = TestClient(daemon.create_app(manager=mgr, token=TOKEN))

    def agent(label: str) -> dict:
        token = daemon.mint_scoped_token(
            principal_id=f"agent:{label}", paths=m.AGENT_TOKEN_PATHS, plan="agent",
            path=registry_path)
        return {"Authorization": f"Bearer {token}"}

    return {"client": client, "mgr": mgr, "typed": typed, "daemon": daemon,
            "owner": {"Authorization": f"Bearer {TOKEN}"}, "agent": agent}


def _spawn(h, headers, **body) -> dict:
    resp = h["client"].post("/sessions", json={"harness": "claude", "cwd": "", **body},
                            headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── (a) the spawner prompts the session it started ──────────────────────────────────


def test_the_spawning_agent_reaches_the_pty_of_a_default_spawn(harness):
    tab_a, tab_b = harness["agent"]("tab-a"), harness["agent"]("tab-b")
    info = _spawn(harness, tab_a)
    sid = info["id"]
    assert info["allow_peer_input"] is False and info["spawned_by"] == "agent:tab-a"

    landed = harness["client"].post(f"/sessions/{sid}/input",
                                    json={"text": "reply with the word ok"}, headers=tab_a)
    assert landed.status_code == 200, landed.text
    (text,) = harness["typed"][sid]
    # On the keyboard, and still framed as a peer naming the daemon's principal.
    assert "reply with the word ok" in text and "agent:tab-a" in text
    assert text != "reply with the word ok"

    # Any OTHER agent is refused exactly as before: the pty did not open to peers.
    other = harness["client"].post(f"/sessions/{sid}/input",
                                   json={"text": "do my thing instead"}, headers=tab_b)
    assert other.status_code == 403 and "did not opt in to peer input" in other.text
    assert len(harness["typed"][sid]) == 1


def test_the_spawner_is_the_authenticated_principal_never_a_body_field(harness):
    tab_a, tab_b = harness["agent"]("tab-a"), harness["agent"]("tab-b")
    # tab-b claims tab-a started it, and names itself owner for good measure.
    resp = harness["client"].post(
        "/sessions", headers=tab_b,
        json={"harness": "claude", "cwd": "", "spawned_by": "agent:tab-a",
              "owner": "owner"})
    assert resp.status_code == 200, resp.text
    sid = resp.json()["id"]
    assert resp.json()["spawned_by"] == "agent:tab-b"
    refused = harness["client"].post(f"/sessions/{sid}/input", json={"text": "x"},
                                     headers=tab_a)
    assert refused.status_code == 403


def test_a_session_the_owner_started_stays_closed_to_agents(harness):
    sid = _spawn(harness, harness["owner"])["id"]
    assert harness["mgr"].get_session(sid).config.spawned_by == "owner"
    refused = harness["client"].post(f"/sessions/{sid}/input", json={"text": "x"},
                                     headers=harness["agent"]("tab-a"))
    assert refused.status_code == 403
    assert sid not in harness["typed"]


def test_the_client_guard_lets_the_spawner_through_and_nobody_else(monkeypatch):
    rows = {
        "mine": {"id": "mine", "allow_peer_input": False,
                 "spawned_by": "agent:%s" % m.SENDER},
        "theirs": {"id": "theirs", "allow_peer_input": False, "spawned_by": "agent:other"},
        "old": {"id": "old", "allow_peer_input": False},          # daemon with no field
        "open": {"id": "open", "allow_peer_input": True, "spawned_by": "owner"},
    }
    monkeypatch.setattr(m, "_managed_rows", lambda: rows)
    monkeypatch.setattr(m, "_SPAWNED_HERE", set())
    assert m._peer_input_guard("mine") is None
    assert m._peer_input_guard("open") is None
    assert "did not opt in" in m._peer_input_guard("theirs")["error"]
    assert "did not opt in" in m._peer_input_guard("old")["error"]
    assert "DISCOVERED" in m._peer_input_guard("nope")["error"]
    # A session this server process spawned counts even when the daemon cannot say so.
    m._SPAWNED_HERE.add("old")
    assert m._peer_input_guard("old") is None


def test_spawn_records_the_session_and_send_then_lands_on_the_pty(monkeypatch):
    calls = []

    def fake_req(method, path, body=None, timeout=20.0, _retried=False):
        calls.append((method, path))
        if (method, path) == ("POST", "/sessions"):
            return {"id": "s-new", "state": "starting"}
        if (method, path) == ("GET", "/sessions"):
            return {"sessions": [{"id": "s-new", "allow_peer_input": False}]}
        if (method, path) == ("POST", "/sessions/s-new/input"):
            return {"ok": True, "turn": 1, "seq": 9}
        raise AssertionError((method, path))

    monkeypatch.setattr(m, "_req", fake_req)
    monkeypatch.setattr(m, "_SPAWNED_HERE", set())
    spawned = m._spawn({"cwd": ""})
    assert spawned["session_id"] == "s-new" and "awsh_end" in spawned["next"]
    sent = m._send("s-new", "reply with the word ok")
    assert sent["channel"] == "pty" and sent["landed_now"] is True
    assert ("POST", "/sessions/s-new/input") in calls


def test_an_older_daemon_refusing_the_spawner_degrades_to_the_mailbox(monkeypatch, tmp_path):
    def fake_req(method, path, body=None, timeout=20.0, _retried=False):
        if (method, path) == ("GET", "/sessions"):
            return {"sessions": [{"id": "s1", "allow_peer_input": False}]}
        if method == "POST" and path.endswith("/input"):
            return {"error": "harness POST /sessions/s1/input: HTTP 403", "status": 403,
                    "detail": '{"detail":"session \'s1\' did not opt in to peer input"}'}
        raise AssertionError((method, path))

    written = []
    monkeypatch.setattr(m, "_req", fake_req)
    monkeypatch.setattr(m, "_SPAWNED_HERE", {"s1"})
    monkeypatch.setattr(m, "_write_mailbox_fallback",
                        lambda sid, text, **kw: written.append(sid) or tmp_path / "x.steer")
    out = m._send("s1", "hello")
    assert out["channel"] == "mailbox" and out["landed_now"] is False
    assert "predates the spawner rule" in out["why_not_pty"] and written == ["s1"]

    # Any other pty error is reported, never quietly turned into a mailbox write.
    monkeypatch.setattr(m, "_req", lambda method, path, body=None, timeout=20.0,
                        _retried=False: (
        {"sessions": [{"id": "s1"}]} if method == "GET" else
        {"error": "harness POST: HTTP 409", "status": 409, "detail": "cannot accept input"}))
    assert m._send("s1", "hello")["status"] == 409 and written == ["s1"]


# ── (b) awsh_wait: a slow answer is not a dead daemon ───────────────────────────────


@pytest.fixture()
def silent_server(monkeypatch):
    """A listener that accepts and never answers; and the port of one that is closed."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    monkeypatch.setattr(m, "HOST", "127.0.0.1")
    monkeypatch.setattr(m, "_token", lambda: "t")
    yield srv.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    for conn in held:
        conn.close()
    srv.close()


def test_a_client_timeout_is_not_reported_as_an_unreachable_daemon(silent_server, monkeypatch):
    monkeypatch.setattr(m, "PORT", silent_server)
    out = m._req("GET", "/sessions/s1/wait?until=idle&timeout=45", timeout=0.4)
    assert out.get("timed_out") is True
    assert "unreachable" not in out["error"] and "no answer within" in out["error"]

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    monkeypatch.setattr(m, "PORT", port)
    dead = m._req("GET", "/health", timeout=2.0)
    assert "unreachable" in dead["error"] and not dead.get("timed_out")


def test_wait_gives_the_socket_longer_than_the_wait_and_clamps(monkeypatch):
    seen = []

    def fake_req(method, path, body=None, timeout=20.0, _retried=False):
        seen.append((path, timeout))
        return {"session": {"id": "s1"}, "matched": "idle", "waited_seconds": 0.5}

    monkeypatch.setattr(m, "_req", fake_req)
    assert m._wait({"session_id": "s1", "timeout": 45})["matched"] == "idle"
    path, budget = seen[-1]
    assert path == "/sessions/s1/wait?until=idle,exited,failed&timeout=45"
    assert budget >= 45 + 10          # the measured failure: 20 s budget for a 45 s wait
    m._wait({"session_id": "s1", "timeout": 9999, "until": "waiting"})
    assert seen[-1] == ("/sessions/s1/wait?until=waiting&timeout=300", 315.0)
    m._wait({"session_id": "s1"})
    assert seen[-1][0].endswith("timeout=30") and seen[-1][1] == 45.0
    assert "error" in m._wait({"session_id": "s1", "timeout": "soon"})
    assert m.BY_NAME["awsh_wait"]["fn"] is m._wait


def test_a_wait_that_runs_out_is_an_answer_not_an_error(monkeypatch):
    monkeypatch.setattr(m, "_req", lambda *a, **k: {
        "error": "harness GET /sessions/s1/wait: HTTP 408", "status": 408,
        "detail": "session 's1' did not reach ['busy'] within 5s (state='idle')"})
    out = m._wait({"session_id": "s1", "until": "busy", "timeout": 5})
    assert out["timed_out"] is True and out["matched"] is None and "error" not in out
    assert "state='idle'" in out["detail"]
    # A real failure still reads as one.
    monkeypatch.setattr(m, "_req", lambda *a, **k: {
        "error": "harness GET: HTTP 404", "status": 404, "detail": "no such session"})
    assert m._wait({"session_id": "nope"})["status"] == 404


# ── (c) ending a session, and an interrupt that explains itself ─────────────────────


def _end_env(monkeypatch, rows, deleted):
    def fake_req(method, path, body=None, timeout=20.0, _retried=False):
        if method == "DELETE":
            deleted.append(path)
            return {"id": path.rsplit("/", 1)[-1], "state": "exited", "exit_code": 0}
        raise AssertionError((method, path))

    monkeypatch.setattr(m, "_managed_rows", lambda: rows)
    monkeypatch.setattr(m, "_req", fake_req)
    monkeypatch.setattr(m, "_SPAWNED_HERE", set())


def test_end_is_a_tool_and_stops_only_what_this_tab_may_stop(monkeypatch):
    assert "awsh_end" in m.BY_NAME and m.BY_NAME["awsh_end"]["fn"] is m._end
    assert m.BY_NAME["awsh_end"]["schema"]["required"] == ["session_id"]
    me = "agent:%s" % m.SENDER
    rows = {
        "mine": {"id": "mine", "state": "idle", "spawned_by": me, "title": "t"},
        "busy": {"id": "busy", "state": "busy", "spawned_by": me},
        "theirs": {"id": "theirs", "state": "idle", "spawned_by": "agent:other"},
        "legacy": {"id": "legacy", "state": "ready"},
        "gone": {"id": "gone", "state": "exited", "spawned_by": "agent:other"},
    }
    deleted: list = []
    _end_env(monkeypatch, rows, deleted)

    assert m._end({"session_id": "mine"}) == {
        "ended": True, "session_id": "mine", "state": "exited", "exit_code": 0,
        "was": "idle", "forced": False}
    assert deleted == ["/sessions/mine"]

    # Not this tab's, not managed, mid-turn: refused with the reason, nothing deleted.
    assert "not spawned by this tab" in m._end({"session_id": "theirs"})["error"]
    assert "unrecorded" in m._end({"session_id": "legacy"})["error"]
    assert "not MANAGED" in m._end({"session_id": "discovered-tab"})["error"]
    assert "mid-turn" in m._end({"session_id": "busy"})["error"]
    assert "session_id" in m._end({})["error"]
    assert deleted == ["/sessions/mine"]

    # An already-ended session is reported, not deleted twice.
    assert m._end({"session_id": "gone"})["already"] is True
    # force is the explicit override, and the answer says it was used.
    forced = m._end({"session_id": "legacy", "force": True})
    assert forced["ended"] is True and forced["forced"] is True
    assert m._end({"session_id": "busy", "force": True})["ended"] is True
    assert deleted == ["/sessions/mine", "/sessions/legacy", "/sessions/busy"]


def test_end_reaches_the_daemon_with_an_agent_token(harness, monkeypatch):
    """The scoped token an agent tab holds is enough: DELETE lives under /sessions."""
    tab_a = harness["agent"]("tab-a")
    sid = _spawn(harness, tab_a)["id"]
    resp = harness["client"].delete(f"/sessions/{sid}", headers=tab_a)
    assert resp.status_code == 200 and resp.json()["state"] == "exited"
    assert harness["client"].delete("/sessions/nope", headers=tab_a).status_code == 404
    # ...and the token file the measurement used (`~/.aither/daemon-token`) is not
    # the daemon's: a bearer the daemon never issued is "invalid token".
    bad = harness["client"].delete(f"/sessions/{sid}",
                                   headers={"Authorization": "Bearer not-a-token"})
    assert bad.status_code == 403 and "invalid token" in bad.text


def test_interrupt_says_why_nothing_was_interrupted(harness, monkeypatch):
    from adk.harnesses.daemon import interrupt_refusal

    assert "no turn is running" in interrupt_refusal("idle", "structured-bidi")["reason"]
    assert "already ended" in interrupt_refusal("exited", "pty")["reason"]
    assert "no interrupt channel" in interrupt_refusal("busy", "structured-bidi")["reason"]

    sid = _spawn(harness, harness["owner"])["id"]
    harness["mgr"].get_session(sid).state = "idle"      # the stub never started a process
    out = harness["client"].post(f"/sessions/{sid}/interrupt", json={},
                                 headers=harness["owner"]).json()
    assert out["interrupted"] is False and out["state"] == "idle"
    assert "no turn is running" in out["reason"] and "awsh_end" in out["to_end_it"]


def test_the_interrupt_tool_adds_the_reason_an_older_daemon_omits(monkeypatch):
    def fake_req(method, path, body=None, timeout=20.0, _retried=False):
        if method == "POST":
            return {"interrupted": False}
        return {"id": "s1", "state": "idle", "transport": "structured-bidi"}

    monkeypatch.setattr(m, "_managed_rows", lambda: {"s1": {"id": "s1"}})
    monkeypatch.setattr(m, "_req", fake_req)
    out = m._interrupt({"session_id": "s1"})
    assert out["interrupted"] is False and "state=idle" in out["reason"]
    assert "awsh_end" in out["next"]
    assert "DISCOVERED" in m._interrupt({"session_id": "elsewhere"})["error"]


# ── (d) a local profile the posture does not serve is refused at spawn ───────────────

PARKED = "served only in posture(s) ['full-mesh', 'pool-lean']; active is pool-fast"
SPINE = [
    {"id": "aither-orchestrator", "owned_by": "vllm",
     "aither": {"available": False, "parked": True, "note": PARKED,
                "upstream_model": "aither-orchestrator"}},
    {"id": "gemma4-12b", "owned_by": "vllm_gemma4_12b",
     "aither": {"available": True, "upstream_model": "gemma4-12b"}},
    {"id": "gemma4-reasoning", "owned_by": "vllm_gemma4_12b",
     "aither": {"available": True, "upstream_model": "gemma4-12b"}},
    {"id": "deepseek-v4-flash-pool", "owned_by": "llamacpp_pool",
     "aither": {"available": True, "upstream_model": "deepseek-v4-flash"}},
    {"id": "aither-code-embed-0.6b", "owned_by": "llamacpp_local",
     "aither": {"available": True, "upstream_model": "aither-code-embed"}},
    {"id": "or-claude-sonnet", "owned_by": "openrouter_api",
     "aither": {"available": True, "upstream_model": "claude"}},
    {"id": "no-verdict", "owned_by": "vllm"},
]
PROFILES = {
    "aither-orchestrator": {"transport": "bridge", "model": "aither-orchestrator",
                            "context_window": 32768},
    "aither-fast": {"transport": "bridge", "model": "aither-fast", "context_window": 16384},
    "deepseek-flash": {"transport": "native", "model": "deepseek-flash"},
    "mystery": {"transport": "bridge", "model": "no-verdict"},
}
ALIASES = {"aither-orchestrator": "microscheduler/aither-orchestrator",
           "aither-fast": "microscheduler/gemma4-12b"}


def test_a_parked_local_profile_is_refused_naming_the_posture_and_what_is_served():
    why = ls.profile_refusal("aither-orchestrator", PROFILES["aither-orchestrator"],
                             SPINE, ALIASES, PROFILES)
    assert "'aither-orchestrator' cannot answer now" in why
    assert PARKED in why                                    # the posture, by name
    # One name per served ENGINE; no embedder, no cloud row, no parked row.
    assert "Local models served now: gemma4-12b, deepseek-v4-flash-pool." in why
    assert "aither-fast (gemma4-12b, 16384 ctx)" in why
    assert "no other model was substituted" in why


def test_served_cloud_unknown_and_unjudgeable_profiles_are_not_refused():
    assert ls.profile_refusal("aither-fast", PROFILES["aither-fast"], SPINE, ALIASES) == ""
    assert ls.profile_refusal("deepseek-flash", PROFILES["deepseek-flash"], SPINE) == ""
    # A row with no availability verdict, a model the spine does not list, and a
    # spine that could not be read: none is evidence the model is down.
    assert ls.profile_refusal("mystery", PROFILES["mystery"], SPINE) == ""
    assert ls.profile_refusal("x", {"transport": "bridge", "model": "absent"}, SPINE) == ""
    assert ls.profile_refusal("aither-orchestrator", PROFILES["aither-orchestrator"],
                              None, ALIASES) == ""
    assert ls.spine_models(fetch=lambda url, timeout: (_ for _ in ()).throw(OSError())) is None
    assert ls.spine_models(fetch=lambda url, timeout: {"data": "nope"}) is None
    assert ls.spine_models(fetch=lambda url, timeout: {"data": SPINE}) == SPINE
    assert ls.upstream_model("aither-fast", ALIASES) == "gemma4-12b"
    assert ls.upstream_model("bare-name", ALIASES) == "bare-name"


def test_the_daemon_refuses_the_spawn_with_409_and_starts_nothing(harness, monkeypatch):
    daemon = harness["daemon"]
    monkeypatch.setattr(daemon, "local_profile_refusal",
                        lambda profile: "model profile %r cannot answer now: %s"
                        % (profile, PARKED) if profile == "aither-orchestrator" else "")
    before = len(harness["mgr"].list_sessions())
    resp = harness["client"].post(
        "/sessions", headers=harness["owner"],
        json={"harness": "claude", "cwd": "", "model_profile": "aither-orchestrator"})
    assert resp.status_code == 409
    assert "pool-fast" in resp.json()["detail"]
    assert len(harness["mgr"].list_sessions()) == before
    # No profile named: nothing is asked and the spawn goes through.
    assert _spawn(harness, harness["owner"])["id"]


def test_the_preflight_is_wired_can_be_switched_off_and_never_blocks_on_its_own_failure():
    from adk.harnesses.daemon import local_profile_availability, local_profile_refusal

    said = []
    check = lambda profile: said.append(profile) or "parked"   # noqa: E731
    assert local_profile_refusal("aither-orchestrator", env={}, check=check) == "parked"
    assert local_profile_refusal("", env={}, check=check) == ""
    assert local_profile_refusal("aither-orchestrator",
                                 env={"AITHER_HARNESS_SPAWN_PREFLIGHT": "0"},
                                 check=check) == ""
    assert said == ["aither-orchestrator"]

    def broken(profile):
        raise RuntimeError("spine exploded")

    assert local_profile_refusal("aither-orchestrator", env={}, check=broken) == ""
    rows = [{"id": "aither-orchestrator", "transport": "bridge"}]
    assert local_profile_availability(rows, env={}, annotate=broken) == rows
    assert local_profile_availability(
        rows, env={}, annotate=lambda r: [dict(r[0], available=False)])[0]["available"] is False


def test_the_backend_list_marks_a_parked_local_profile(monkeypatch):
    monkeypatch.setattr(ls, "spine_models", lambda fetch=None, **k: SPINE)
    monkeypatch.setattr(ls, "bridge_aliases", lambda tool: ALIASES)
    rows = ls.annotate_profiles([
        {"id": "aither-orchestrator", "transport": "bridge", "model": "aither-orchestrator"},
        {"id": "aither-fast", "transport": "bridge", "model": "aither-fast"},
        {"id": "deepseek-flash", "transport": "native", "model": "deepseek-flash"},
        {"id": "mystery", "transport": "bridge", "model": "no-verdict"},
    ])
    by_id = {r["id"]: r for r in rows}
    assert by_id["aither-orchestrator"]["available"] is False
    assert by_id["aither-orchestrator"]["unavailable_reason"] == PARKED
    assert by_id["aither-fast"]["available"] is True
    assert "available" not in by_id["deepseek-flash"]       # not a local profile
    assert "available" not in by_id["mystery"]              # no verdict: not judged


# ── (5) /awrun stays owner-only for an agent tab, and the tools say so ───────────────


def test_an_agent_token_is_refused_on_awrun_and_the_owner_is_not(harness):
    refused = harness["client"].get("/awrun/queue", headers=harness["agent"]("tab-a"))
    assert refused.status_code == 403 and "is not scoped to /awrun/queue" in refused.text
    assert not any(str(p).split(" ")[-1].startswith(("/awrun", "/wakes", "/fs", "/decisions"))
                   for p in m.AGENT_TOKEN_PATHS)
    for name in ("awsh_awrun_queue", "awsh_awrun_status", "awsh_awrun_cancel"):
        assert "OWNER-ONLY" in m.BY_NAME[name]["description"], name
