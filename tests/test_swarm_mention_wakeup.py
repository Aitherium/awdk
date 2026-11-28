"""Tests for M1.2: Swarm/Room mention-wakeup bridge, local Room-backed SwarmBoard,
named/pooled slots, scope ownership, awgit binding, ThreadRouter multi-slot,
mcp_stdio swarm tools, and daemon.py M1<->M2 rc_routes contract.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List

import pytest

_AWDK = Path(__file__).resolve().parents[1]
_REPO = _AWDK.parent
_AWGIT_PKG = _REPO / "AitherOS" / "packages" / "awgit"
if str(_AWDK) not in sys.path:
    sys.path.insert(0, str(_AWDK))

from adk.harnesses import mcp_stdio, rooms as rooms_mod, swarm_routes  # noqa: E402
from adk.harnesses.rooms import Room, RoomRegistry, extract_swarm_mentions  # noqa: E402
from adk.harnesses.steer_dispatch import (  # noqa: E402
    SwarmSlotRegistry,
    frame_peer_text,
    register as register_steer,
)
from adk.harnesses.thread_router import SpawnRequest, ThreadRouter  # noqa: E402
from adk.harnesses.thread_subs import ThreadSubscriptions  # noqa: E402


class _WarmSession:
    """Fake persistent harness session supporting idle <-> busy turns and cache ID."""

    def __init__(self, sid: str, *, state: str = "idle", harness_sid: str = "") -> None:
        self.id = sid
        self.state = state
        self.turn = 1
        self.harness_session_id = harness_sid or f"h_{sid}"
        self.submitted: List[str] = []
        self.auto_finish_turns = 1
        self._events: List[Dict[str, Any]] = [
            {"type": "assistant", "text": f"Turn 1 complete for {sid}."}
        ]

    def submit(self, text: str) -> bool:
        self.submitted.append(text)
        self.turn += 1
        self.state = "busy"
        self._events.append({"type": "assistant", "text": f"Turn {self.turn} handled: {text[:80]}"})
        if self.auto_finish_turns > 0:
            self.auto_finish_turns -= 1
            threading.Timer(0.05, self.finish_turn).start()
        return True

    def finish_turn(self) -> None:
        self.state = "idle"

    def recent_events(self) -> List[Dict[str, Any]]:
        return list(self._events)


class _WarmManager:
    def __init__(self, *, initial_state: str = "idle") -> None:
        self.sessions: Dict[str, _WarmSession] = {}
        self.configs: List[Any] = []
        self.woken: List[str] = []
        self.initial_state = initial_state

    def list_sessions(self) -> List[Dict[str, Any]]:
        return [
            {"id": s.id, "state": s.state, "harness_session_id": s.harness_session_id}
            for s in self.sessions.values()
        ]

    def get_session(self, sid: str) -> _WarmSession:
        if sid not in self.sessions:
            raise KeyError(sid)
        return self.sessions[sid]

    def create(self, cfg: Any) -> _WarmSession:
        self.configs.append(cfg)
        sid = f"warm_{len(self.configs)}"
        sess = _WarmSession(sid, state=self.initial_state)
        self.sessions[sid] = sess
        return sess

    def wake(self, sid: str) -> Dict[str, Any]:
        self.woken.append(sid)
        sess = self.sessions[sid]
        sess.state = "idle"
        return {"woken": True, "session": {"id": sid}}


@pytest.fixture()
def swarm_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_TESTING", "1")
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_LOCAL_SWARMS_DIR", str(tmp_path / "local-swarms"))
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.delenv("AITHER_INTERNAL_SECRET", raising=False)
    monkeypatch.delenv("AITHER_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
    monkeypatch.setattr(rooms_mod, "_registry", None)
    monkeypatch.setattr(swarm_routes, "_emit_harness_trace_span", lambda **kw: None)
    return tmp_path


def test_standalone_imports_succeed_without_lib():
    code = (
        "import sys; sys.modules['lib'] = None; "
        "import adk.harnesses.swarm_routes, adk.harnesses.rooms, "
        "adk.harnesses.steer_dispatch, adk.harnesses.thread_router, "
        "adk.harnesses.daemon, adk.harnesses.mcp_stdio; "
        "print('OK')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_AWDK)
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_AWDK),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    assert "OK" in res.stdout


def test_extract_swarm_mentions_preserves_room_invariants():
    assert extract_swarm_mentions("hey @fix-1 and @ALL check foo@bar.com", ["verifier"]) == [
        "verifier",
        "fix-1",
        "all",
    ]
    assert rooms_mod.MAX_TO_TARGETS == 8


def test_warm_waiting_wakeup_bounce_qa_and_all_fanout(swarm_env):
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    mgr = _WarmManager(initial_state="idle")
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()

    def send_managed(sid: str, text: str) -> bool:
        try:
            return mgr.get_session(sid).submit(text)
        except KeyError:
            return False

    dispatcher = register_steer(
        rooms,
        send_managed_input=send_managed,
        swarm_registry=slot_reg,
        wake_session_fn=mgr.wake,
    )
    coord = swarm_routes.LocalSwarmCoordinator(
        swarm_env / "local-swarms",
        rooms=rooms,
        swarm_registry=slot_reg,
        steer_dispatcher=dispatcher,
        mgr=mgr,
    )

    class _P:
        def __init__(self, pid: str, plan: str) -> None:
            self.id = pid
            self.plan = plan

    def auth(authorization: str = Header(default="")) -> _P:
        if authorization == "Bearer owner-tok":
            return _P("owner", "owner")
        if authorization == "Bearer agent-tok":
            return _P("agent:fix-1", "agent")
        raise HTTPException(401, "missing")

    def require_owner(p: _P = Depends(auth)) -> _P:
        if p.plan != "owner":
            raise HTTPException(403, "owner only")
        return p

    app = FastAPI()
    swarm_routes.mount(
        app,
        auth,
        require_owner,
        mgr=mgr,
        rooms=rooms,
        steer_dispatcher=dispatcher,
        swarm_registry=slot_reg,
        local_coordinator=coord,
    )
    tc = TestClient(app)
    h_own = {"Authorization": "Bearer owner-tok"}
    h_agent = {"Authorization": "Bearer agent-tok"}

    # F5 + F6: Launch local swarm with named + pooled fix-1..fix-5 slots (>4 slots)
    launched = tc.post(
        "/swarms",
        headers=h_own,
        json={
            "goal": "Fix multi-track auth and room issues",
            "local_room": True,
            "tasks": [
                {
                    "pool": "fix",
                    "count": 5,
                    "agent": "demiurge",
                    "owns": ["awdk/adk/harnesses/"],
                    "must_not_touch": ["AitherOS/"],
                },
                {
                    "slot_id": "custom-verifier",
                    "agent": "vera",
                    "owns": ["awdk/tests/"],
                    "must_not_touch": ["AitherOS/"],
                },
            ],
        },
    )
    assert launched.status_code == 200, launched.text
    sw = launched.json()
    sid = sw["swarm_id"]
    assert len(sw["members"]) == 6
    assert set(sw["members_by_slot"]) == {
        "fix-1",
        "fix-2",
        "fix-3",
        "fix-4",
        "fix-5",
        "custom-verifier",
    }

    # Delegate fix-1 and fix-2 (same base agent "demiurge", distinct slots)
    d1 = tc.post(
        f"/swarms/{sid}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "agent": "demiurge",
            "slot_id": "fix-1",
            "pool": "fix",
            "owns": ["awdk/adk/harnesses/rooms.py"],
            "must_not_touch": ["AitherOS/"],
            "prompt": "Fix rooms mention parsing",
            "report_to": "custom-verifier",
            "wait": True,
            "timeout_s": 5.0,
        },
    )
    assert d1.status_code == 200, d1.text
    res1 = d1.json()
    assert res1["result"]["session_state"] == "waiting"
    assert res1["result"]["warm"] is True
    sess1_id = res1["session_id"]
    assert mgr.configs[0].allow_peer_input is True
    assert "owns: awdk/adk/harnesses/rooms.py" in mgr.sessions[sess1_id].submitted[0]
    assert "sibling_digests:" in mgr.sessions[sess1_id].submitted[0]

    d2 = tc.post(
        f"/swarms/{sid}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "agent": "demiurge",
            "slot_id": "fix-2",
            "pool": "fix",
            "owns": ["awdk/adk/harnesses/steer_dispatch.py"],
            "prompt": "Fix steer dispatch",
            "wait": True,
            "timeout_s": 5.0,
        },
    )
    assert d2.status_code == 200
    sess2_id = d2.json()["session_id"]
    assert sess1_id != sess2_id

    # F2: Verifier [bounce] wakes exact warm session_id (preserving harness_session_id)
    prev_hsid = mgr.sessions[sess1_id].harness_session_id
    bounce = tc.post(
        f"/swarms/{sid}/post",
        headers=h_agent,
        json={
            "agent": "custom-verifier",
            "kind": "bounce",
            "text": "@fix-1 [bounce] Add edge-case check for email addresses.",
        },
    )
    assert bounce.status_code == 200, bounce.text
    assert len(mgr.configs) == 2, "must wake warm session, never cold-respawn"
    assert mgr.sessions[sess1_id].harness_session_id == prev_hsid
    assert any("[bounce]" in msg for msg in mgr.sessions[sess1_id].submitted[1:])
    assert mgr.sessions[sess1_id].submitted[-1].startswith("[via room from custom-verifier]")

    # Complete turn 2 and verify watcher stays attached and re-parks in waiting
    mgr.sessions[sess1_id].finish_turn()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        board = tc.get(f"/swarms/{sid}/board", headers=h_agent).json()
        if any("finished follow-up turn" in p["text"] for p in board["posts"]):
            break
        time.sleep(0.03)
    assert any("finished follow-up turn" in p["text"] for p in board["posts"])
    assert coord.get_swarm(sid)["members_by_slot"]["fix-1"]["session_state"] == "waiting"

    # Worker-to-worker board Q&A
    qa = tc.post(
        f"/swarms/{sid}/post",
        headers=h_agent,
        json={
            "agent": "fix-1",
            "kind": "question",
            "text": "@fix-2 which receipt channel should warm wake emit?",
        },
    )
    assert qa.status_code == 200
    assert any("which receipt channel" in s for s in mgr.sessions[sess2_id].submitted)

    # Hop ceiling (>=2) refused without looping
    before_s1 = len(mgr.sessions[sess1_id].submitted)
    looped = tc.post(
        f"/swarms/{sid}/post",
        headers=h_agent,
        json={
            "agent": "fix-2",
            "kind": "answer",
            "text": "@fix-1 loop guard test",
            "hops": 2,
        },
    )
    assert looped.status_code == 200
    assert len(mgr.sessions[sess1_id].submitted) == before_s1

    # @all broadcast wakes active peers (excluding sender)
    # Register 9 more slots (> MAX_TO_TARGETS=8) to verify fan-out works cleanly
    for idx in range(3, 12):
        s_extra = _WarmSession(f"warm_extra_{idx}", state="idle")
        mgr.sessions[s_extra.id] = s_extra
        slot_reg.register_slot(
            swarm_id=sid,
            slot_id=f"extra-{idx}",
            session_id=s_extra.id,
            room_id=sw["room_id"],
        )

    bcast = tc.post(
        f"/swarms/{sid}/post",
        headers=h_agent,
        json={"agent": "coordinator", "text": "@all sync on new schema"},
    )
    assert bcast.status_code == 200
    assert any("sync on new schema" in s for s in mgr.sessions[sess1_id].submitted)
    assert any("sync on new schema" in s for s in mgr.sessions[sess2_id].submitted)
    assert any("sync on new schema" in s for s in mgr.sessions["warm_extra_11"].submitted)

    room = rooms.get(sw["room_id"])
    receipts = [e for e in room.events_since(0) if e["type"] == "steering_receipt"]
    assert any(r["payload"]["channel"] == "swarm_wake" and r["payload"]["landed_now"] for r in receipts)


def test_monitor_stall_nudge_before_escalation_and_fallbacks(swarm_env):
    mgr = _WarmManager(initial_state="idle")
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()

    # Track wake fallback when session initially refuses submit until woken
    dead_once = {"sess_dead": True}

    def send_managed(sid: str, text: str) -> bool:
        if dead_once.get(sid):
            dead_once[sid] = False
            return False
        return mgr.get_session(sid).submit(text)

    register_steer(
        rooms,
        send_managed_input=send_managed,
        swarm_registry=slot_reg,
        wake_session_fn=mgr.wake,
    )
    coord = swarm_routes.LocalSwarmCoordinator(
        swarm_env / "local-swarms",
        rooms=rooms,
        swarm_registry=slot_reg,
        mgr=mgr,
    )
    sw = coord.create_swarm({"goal": "Test monitor nudge", "leads": ["fix-1"]})
    sid = sw["swarm_id"]

    # Register a session that needs wake() before submit()
    s1 = _WarmSession("sess_dead", state="exited")
    mgr.sessions[s1.id] = s1
    coord.register_member_session(sid, slot_id="fix-1", member_id="fix-1", agent="demiurge", session_id=s1.id)

    # First @monitor nudge wakes blocking worker BEFORE escalating
    r1 = coord.nudge_stalled_slot(sid, "fix-1", reason="no commit in 120s", escalate_after_s=10.0)
    assert r1["action"] == "nudged"
    assert r1["escalated"] is False
    assert r1["status"] == "live"
    assert mgr.woken == ["sess_dead"]
    assert any("[stall]" in txt for txt in s1.submitted)
    assert coord.get_swarm(sid)["status"] == "live"

    # Second nudge past grace escalates to stalled
    r2 = coord.nudge_stalled_slot(sid, "fix-1", reason="still silent", escalate_after_s=0.0)
    assert r2["action"] == "escalated"
    assert r2["escalated"] is True
    assert r2["status"] == "stalled"
    assert coord.get_swarm(sid)["status"] == "stalled"

    # Mailbox fallback when no send_managed_input is wired
    rooms_mb = RoomRegistry()
    reg_mb = SwarmSlotRegistry()
    register_steer(rooms_mb, swarm_registry=reg_mb)
    reg_mb.register_slot(swarm_id="sw_mb", slot_id="fix-9", session_id="sess_mb", room_id="swarm-sw_mb")
    r_mb = rooms_mb.get_or_create("swarm-sw_mb")
    r_mb.publish(
        {
            "type": "agent_message",
            "actor": {"kind": "adk_agent", "id": "verifier", "name": "verifier"},
            "to": ["fix-9"],
            "payload": {"swarm_id": "sw_mb", "text": "check mailbox fallback"},
        }
    )
    steer_files = list((swarm_env / "steer" / "sess_mb").glob("*.md"))
    assert len(steer_files) == 1


def test_awgit_worktree_and_lease_conflict_rollback(swarm_env, monkeypatch):
    if not shutil.which("git") or not _AWGIT_PKG.is_dir():
        pytest.skip("git or awgit package unavailable")

    monkeypatch.syspath_prepend(str(_AWGIT_PKG))
    monkeypatch.setenv("VCS_DATA_ROOT", str(swarm_env / "vcs_data"))
    monkeypatch.setenv("AWGIT_DATA_ROOT", str(swarm_env / "vcs_data"))
    monkeypatch.delenv("VCS_LEASE_API_URL", raising=False)

    repo = swarm_env / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "feature-local"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@aitherium.local"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo), check=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    mgr = _WarmManager(initial_state="exited")
    coord = swarm_routes.LocalSwarmCoordinator(swarm_env / "local-swarms", mgr=mgr)
    sw = coord.create_swarm({"goal": "awgit isolation test", "leads": ["fix-1", "fix-2"]})
    sid = sw["swarm_id"]

    # Slot fix-1 binds isolated worktree (even without standard trunk via --at HEAD fallback) + lease
    res1 = swarm_routes.delegate_harness_worker(
        mgr,
        None,
        swarm_id=sid,
        agent="demiurge",
        slot_id="fix-1",
        harness="claude",
        prompt="Edit a.py",
        cwd=str(repo),
        owns=["a.py"],
        use_worktree=True,
        use_leases=True,
        local_only=True,
        local_coordinator=coord,
        wait=True,
        timeout_s=5.0,
    )
    assert res1["worktree_path"] and Path(res1["worktree_path"]).is_dir()
    assert len(res1["lease_ids"]) == 1
    assert mgr.configs[0].cwd == res1["worktree_path"]

    # Slot fix-2 acquiring conflicting lease on a.py must roll back its worktree and raise 409
    wt_dir_before = set(p.name for p in (repo / ".worktrees").iterdir()) if (repo / ".worktrees").is_dir() else set()
    with pytest.raises(swarm_routes.SwarmLeaseConflictError) as exc_info:
        swarm_routes.delegate_harness_worker(
            mgr,
            None,
            swarm_id=sid,
            agent="demiurge",
            slot_id="fix-2",
            harness="claude",
            prompt="Conflicting edit on a.py",
            cwd=str(repo),
            owns=["a.py"],
            use_worktree=True,
            use_leases=True,
            local_only=True,
            local_coordinator=coord,
        )
    assert exc_info.value.status == 409
    wt_dir_after = set(p.name for p in (repo / ".worktrees").iterdir()) if (repo / ".worktrees").is_dir() else set()
    assert wt_dir_before == wt_dir_after, "conflicting lease must roll back newly created worktree"

    # Cancel swarm releases fix-1's lease so fix-2 can now acquire a.py
    coord.control_swarm(sid, {"action": "cancel"})
    res2 = swarm_routes.bind_slot_worktree_and_leases(
        cwd=str(repo),
        swarm_id=sid,
        slot_id="fix-2",
        owns=["a.py"],
        use_worktree=False,
        use_leases=True,
    )
    assert len(res2["lease_ids"]) == 1


def test_thread_router_multi_slot_and_swarm_owned_skip(swarm_env):
    subs = ThreadSubscriptions(swarm_env / "subs.db")
    try:
        spawned_reqs: List[SpawnRequest] = []
        reg = SwarmSlotRegistry()
        reg.register_slot(
            swarm_id="sw_owned",
            slot_id="fix-warm",
            session_id="warm_1",
            thread_id="th_warm",
        )

        def spawner(req: SpawnRequest) -> str:
            spawned_reqs.append(req)
            return f"worker-{len(spawned_reqs)}"

        router = ThreadRouter(
            thread_subscriptions=subs,
            spawn=spawner,
            max_workers=2,
            allow_unstamped=True,
            swarm_registry=reg,
            status_file=swarm_env / "tr.json",
        )
        room = Room("tr")
        room.add_listener(router.handle)

        # Registered warm swarm slot skips cold spawn
        room.publish(
            {
                "type": "agent_message",
                "actor": {"kind": "human", "id": "owner", "name": "owner"},
                "payload": {
                    "text": "@aither wake warm",
                    "work_request": True,
                    "swarm_id": "sw_owned",
                    "slot_id": "fix-warm",
                    "thread": {"surface": "swarm", "id": "th_warm"},
                },
            }
        )
        assert spawned_reqs == []

        # Multi-worker distinct slot_ids spawn cleanly past generic cap=2 with scope propagated
        for i in range(1, 4):
            room.publish(
                {
                    "type": "agent_message",
                    "actor": {"kind": "human", "id": "owner", "name": "owner"},
                    "payload": {
                        "text": f"work slot {i}",
                        "work_request": True,
                        "allow_multi_worker": True,
                        "slot_id": f"fix-{i}",
                        "pool": "fix",
                        "owns": [f"file_{i}.py"],
                        "must_not_touch": ["secret.py"],
                        "sibling_digests": [{"slot_id": "other", "title": "t"}],
                        "thread": {"surface": "swarm", "id": "sw_multi"},
                    },
                }
            )
        assert len(spawned_reqs) == 3
        assert [r.slot_id for r in spawned_reqs] == ["fix-1", "fix-2", "fix-3"]
        assert spawned_reqs[0].owns == ("file_1.py",)
        assert spawned_reqs[0].must_not_touch == ("secret.py",)
        routed = [e for e in room.events_since(0) if e["type"] == "thread.routed"]
        assert len(routed) == 3
        assert routed[0]["payload"]["slot_id"] == "fix-1"
    finally:
        subs.close()


def test_attach_engine_optional_m1_1_adapter(swarm_env):
    mgr = _WarmManager(initial_state="idle")
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    register_steer(
        rooms,
        send_managed_input=lambda sid, txt: mgr.get_session(sid).submit(txt),
        swarm_registry=slot_reg,
    )
    sess = _WarmSession("sess_eng", state="idle")
    mgr.sessions[sess.id] = sess
    slot_reg.register_slot(
        swarm_id="sw_eng",
        slot_id="fix-1",
        session_id=sess.id,
        room_id="swarm-sw_eng",
    )

    class _FakeEngine:
        def __init__(self) -> None:
            self.mention_cbs = []
            self.stall_cbs = []

        def register_mention_listener(self, cb):
            self.mention_cbs.append(cb)

        def register_stall_nudge_listener(self, cb):
            self.stall_cbs.append(cb)

    eng = _FakeEngine()
    res = swarm_routes.attach_engine(
        eng, mgr=mgr, rooms=rooms, swarm_registry=slot_reg
    )
    assert res["attached"] is True and res["mention"] and res["stall"]

    sw_obj = SimpleNamespace(swarm_id="sw_eng")
    msg_obj = SimpleNamespace(agent="verifier", text="@fix-1 [bounce] check return", kind="bounce", thread_id="t1")
    eng.mention_cbs[0](sw_obj, msg_obj, ["fix-1"])
    eng.stall_cbs[0](sw_obj, "fix-1", "idle 60s")
    assert len(sess.submitted) == 2


def test_mcp_stdio_swarm_post_board_and_schemas(monkeypatch):
    for name in (
        "awsh_swarm_list",
        "awsh_swarm_status",
        "awsh_swarm_launch",
        "awsh_swarm_steer",
        "awsh_swarm_pause",
        "awsh_swarm_board",
        "awsh_swarm_post",
        "awsh_swarm_delegate_harness",
    ):
        assert name in mcp_stdio.BY_NAME

    launch_props = mcp_stdio.BY_NAME["awsh_swarm_launch"]["schema"]["properties"]
    assert {"tasks", "local_room", "use_worktree", "use_leases", "stall_timeout_s"} <= set(launch_props)
    del_props = mcp_stdio.BY_NAME["awsh_swarm_delegate_harness"]["schema"]["properties"]
    assert {"slot_id", "pool", "owns", "must_not_touch", "use_worktree", "use_leases", "keep_warm"} <= set(del_props)

    calls: List[tuple] = []
    monkeypatch.setattr(mcp_stdio, "_req", lambda m, p, body=None: calls.append((m, p, body)) or {"ok": True})
    mcp_stdio.BY_NAME["awsh_swarm_board"]["fn"]({"swarm_id": "sw_1", "since": 3, "limit": 20})
    mcp_stdio.BY_NAME["awsh_swarm_post"]["fn"]({
        "swarm_id": "sw_1",
        "agent": "fix-1",
        "text": "@verifier [done] ready",
        "kind": "done",
    })
    assert calls[0][:2] == ("GET", "/swarms/sw_1/board?since=3&limit=20")
    assert calls[1][:2] == ("POST", "/swarms/sw_1/post")
    assert calls[1][2]["kind"] == "done"


def test_daemon_rc_contract_and_scoped_link_paths(swarm_env, monkeypatch):
    from fastapi.testclient import TestClient

    import adk.harnesses.daemon as daemon
    from adk.harnesses import pairing, session as session_mod, session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    token = "owner-test-token"
    reg_path = swarm_env / "principals.json"
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", token)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(reg_path))
    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", reg_path)
    monkeypatch.setattr(
        directory_mod, "_directory", directory_mod.SessionDirectory(discover_fn=lambda: [])
    )
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)

    # 1. Clean boot when rc_routes is absent (ImportError fallback)
    monkeypatch.setitem(sys.modules, "adk.harnesses.rc_routes", None)
    app_no_rc = daemon.create_app(manager=SessionManager(root=swarm_env / "s1"), token=token)
    assert app_no_rc is not None

    # 2. Mounts /rc/status & /rc/off when rc_routes is present + preserves SCOPED_LINK_PATHS
    fake_rc = ModuleType("adk.harnesses.rc_routes")

    def _mount_rc(app, auth, require_owner):
        from fastapi import Depends

        @app.get("/rc/status", dependencies=[Depends(auth)])
        def _rc_status():
            return {"active": False, "instance_id": None, "node_id": None, "rc_url": None, "app_url": None, "reach": "off"}

        @app.post("/rc/off", dependencies=[Depends(require_owner)])
        def _rc_off():
            return {"ok": True}

    fake_rc.mount = _mount_rc  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adk.harnesses.rc_routes", fake_rc)
    monkeypatch.setattr(rooms_mod, "_registry", None)
    app_with_rc = daemon.create_app(manager=SessionManager(root=swarm_env / "s2"), token=token)
    tc = TestClient(app_with_rc)

    h_owner = {"Authorization": f"Bearer {token}"}
    assert tc.get("/rc/status", headers=h_owner).status_code == 200

    # Verify SCOPED_LINK_PATHS & AWCONNECT_PATHS untouched
    assert set(["/sessions", "/decisions", "/desk/fleet/status"]).issubset(set(daemon.SCOPED_LINK_PATHS))
    assert "GET /sessions" in pairing.AWCONNECT_PATHS and "POST /sessions" not in pairing.AWCONNECT_PATHS

    link_tok = daemon.mint_scoped_token(principal_id="node:phone-1", path=reg_path)
    h_link_plain = {"Authorization": f"Bearer {link_tok}"}
    h_link_owner = {"Authorization": f"Bearer {link_tok}", daemon.LINK_ACTOR_HEADER: daemon.LINK_ACTOR_OWNER}

    # Spawn one session as owner
    created = tc.post("/sessions", json={"harness": "claude", "cwd": ""}, headers=h_owner)
    assert created.status_code == 200
    sid = created.json()["id"]

    # Link token: transcript refused (403) without owner verdict, permitted (!=403/401) with owner verdict;
    # interrupt permitted; focus & unverified POST /sessions rejected (403)
    assert tc.get(f"/sessions/{sid}/transcript", headers=h_link_plain).status_code == 403
    tr_ok = tc.get(f"/sessions/{sid}/transcript", headers=h_link_owner)
    assert tr_ok.status_code not in (401, 403), tr_ok.text
    assert tc.post(f"/sessions/{sid}/interrupt", headers=h_link_plain).status_code != 403
    assert tc.post(f"/sessions/{sid}/focus", headers=h_link_owner).status_code == 403
    assert tc.post("/sessions", json={"harness": "claude", "cwd": ""}, headers=h_link_plain).status_code == 403


# ── Iteration 2 Remediation Regression Tests (Findings 1-9) ───────────────────


def test_steer_swarm_prevents_owner_authority_laundering(swarm_env):
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from fastapi.testclient import TestClient

    mgr = _WarmManager(initial_state="idle")
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    dispatcher = register_steer(
        rooms,
        send_managed_input=lambda sid, txt: mgr.get_session(sid).submit(txt),
        swarm_registry=slot_reg,
        wake_session_fn=mgr.wake,
    )
    coord = swarm_routes.LocalSwarmCoordinator(
        swarm_env / "local-swarms",
        rooms=rooms,
        swarm_registry=slot_reg,
        steer_dispatcher=dispatcher,
        mgr=mgr,
    )

    class _P:
        def __init__(self, pid: str, plan: str) -> None:
            self.id = pid
            self.plan = plan

    def auth(authorization: str = Header(default="")) -> _P:
        if authorization == "Bearer owner-tok":
            return _P("owner", "owner")
        if authorization == "Bearer agent-tok":
            return _P("agent:tab1", "agent")
        if authorization == "Bearer link-tok":
            return _P("node:phone", "link")
        raise HTTPException(401, "missing")

    def require_owner(p: _P = Depends(auth)) -> _P:
        if p.plan != "owner":
            raise HTTPException(403, "owner only")
        return p

    app = FastAPI()
    swarm_routes.mount(
        app,
        auth,
        require_owner,
        mgr=mgr,
        rooms=rooms,
        steer_dispatcher=dispatcher,
        swarm_registry=slot_reg,
        local_coordinator=coord,
    )
    tc = TestClient(app)
    h_own = {"Authorization": "Bearer owner-tok"}
    h_agent = {"Authorization": "Bearer agent-tok"}
    h_link_plain = {"Authorization": "Bearer link-tok"}
    h_link_owner = {"Authorization": "Bearer link-tok", "x-aither-link-actor": "owner"}

    sw = tc.post(
        "/swarms",
        headers=h_own,
        json={"goal": "Verify steer auth", "local_room": True, "leads": ["locked", "open"]},
    ).json()
    sid = sw["swarm_id"]

    d_locked = tc.post(
        f"/swarms/{sid}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "locked",
            "agent": "demiurge",
            "prompt": "Work locked",
            "allow_peer_input": False,
            "wait": True,
            "timeout_s": 5.0,
        },
    ).json()
    d_open = tc.post(
        f"/swarms/{sid}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "open",
            "agent": "demiurge",
            "prompt": "Work open",
            "allow_peer_input": True,
            "wait": True,
            "timeout_s": 5.0,
        },
    ).json()
    s_locked = mgr.sessions[d_locked["session_id"]]
    s_open = mgr.sessions[d_open["session_id"]]

    # PoC 1a: Scoped plan="agent" token calling /steer on allow_peer_input=False must NOT hit Tier-1 PTY
    r_agent_locked = tc.post(
        f"/swarms/{sid}/steer",
        headers=h_agent,
        json={"message": "@locked run arbitrary cmd", "user": "owner"},
    )
    assert r_agent_locked.status_code == 200
    assert len(s_locked.submitted) == 1, "agent token must not bypass allow_peer_input=False"
    steer_files = list((swarm_env / "steer" / s_locked.id).glob("*.md"))
    assert len(steer_files) == 1
    room = rooms.get(sw["room_id"])
    last_steer_ev = [e for e in room.events_since(0) if e["type"] in ("agent_message", "steering")][-1]
    assert last_steer_ev["auth"]["plan"] == "agent"
    assert last_steer_ev["actor"]["kind"] == "adk_agent"

    # PoC 1b: Scoped plan="agent" token calling /steer on allow_peer_input=True MUST be peer-framed
    r_agent_open = tc.post(
        f"/swarms/{sid}/steer",
        headers=h_agent,
        json={"message": "@open peer check"},
    )
    assert r_agent_open.status_code == 200
    assert s_open.submitted[-1].startswith("[via room from ")

    # PoC 1c: Link token WITHOUT x-aither-link-actor: owner stays non-owner
    tc.post(f"/swarms/{sid}/steer", headers=h_link_plain, json={"message": "@locked plain link"})
    assert len(s_locked.submitted) == 1

    # PoC 1d: Link token WITH x-aither-link-actor: owner and real owner reach Tier-1 unframed
    tc.post(f"/swarms/{sid}/steer", headers=h_link_owner, json={"message": "@locked owner via link"})
    assert len(s_locked.submitted) == 2
    assert s_locked.submitted[-1] == "@locked owner via link"

    # Direct LocalSwarmCoordinator.steer_swarm without owner auth never launders to owner
    coord.steer_swarm(sid, {"message": "@locked direct unauthed", "user": "stranger"})
    assert len(s_locked.submitted) == 2

    # Ensure unused Request import in test doesn't trip ruff
    assert Request is not None


def test_attach_engine_4arg_m1_1_stall_nudge_signature(swarm_env):
    mgr = _WarmManager(initial_state="idle")
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    register_steer(
        rooms,
        send_managed_input=lambda sid, txt: mgr.get_session(sid).submit(txt),
        swarm_registry=slot_reg,
    )
    s1 = _WarmSession("sess_4arg", state="idle")
    s2 = _WarmSession("sess_ver", state="idle")
    mgr.sessions[s1.id] = s1
    mgr.sessions[s2.id] = s2
    slot_reg.register_slot(swarm_id="sw_m11", slot_id="fix-1", session_id=s1.id, room_id="swarm-sw_m11")
    slot_reg.register_slot(swarm_id="sw_m11", slot_id="verifier", session_id=s2.id, room_id="swarm-sw_m11")

    class _M11Engine:
        def __init__(self) -> None:
            self.stall_cbs = []

        def register_stall_nudge_listener(self, cb):
            self.stall_cbs.append(cb)

    eng = _M11Engine()
    res = swarm_routes.attach_engine(eng, mgr=mgr, rooms=rooms, swarm_registry=slot_reg)
    assert res["stall"] is True

    # 4-arg call (session, bm, target_th, post) with real member + thread
    sw_obj = SimpleNamespace(swarm_id="sw_m11", stall=SimpleNamespace(blocking_agent="fix-1"))
    bm = SimpleNamespace(slot_id="fix-1", member_id="fix-1", name="demiurge")
    th = SimpleNamespace(id="th_1", slot_id="fix-1", owner="demiurge")
    post = SimpleNamespace(text="@fix-1 [nudge] Monitor checking in.", thread_id="th_1", mentions=["fix-1"])
    eng.stall_cbs[0](sw_obj, bm, th, post)
    assert len(s1.submitted) == 1
    assert "[nudge]" in s1.submitted[0]

    # 4-arg call with bm=None, target_th=None (all threads in_review -> blocking='verifier')
    post_ver = SimpleNamespace(text="@verifier [nudge] Review pending.", thread_id="", mentions=["verifier"])
    eng.stall_cbs[0](sw_obj, None, None, post_ver)
    assert len(s2.submitted) == 1
    assert "[nudge]" in s2.submitted[0]


def test_empty_room_tier1_bypass_and_main_room_guard(swarm_env):
    sent: List[tuple[str, str]] = []
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    slot_reg.register_slot(
        swarm_id="sw_sec",
        slot_id="demiurge",
        session_id="swarm_sess_1",
        room_id="swarm-sw_sec",
    )
    dispatcher = register_steer(
        rooms,
        send_managed_input=lambda sid, txt: sent.append((sid, txt)) or True,
        swarm_registry=slot_reg,
    )
    # Direct handle() with no room and no swarm_id must fail closed (no Tier-1 PTY!)
    dispatcher.handle(
        None,
        {
            "id": "ev_no_room_1",
            "type": "agent_message",
            "actor": {"kind": "adk_agent", "id": "stranger", "name": "stranger"},
            "to": ["demiurge"],
            "payload": {"text": "unvouched direct call with no room"},
        },
    )
    assert sent == []

    # LocalSwarmCoordinator.create_swarm must disallow room_id="main"
    coord = swarm_routes.LocalSwarmCoordinator(swarm_env / "local-swarms", rooms=rooms, swarm_registry=slot_reg)
    with pytest.raises(swarm_routes.SwarmApiError) as ei:
        coord.create_swarm({"goal": "Hijack main", "room_id": "main"})
    assert ei.value.status == 422


def test_watch_and_report_no_all_mention_cascade(swarm_env):
    class _EchoPromptSession(_WarmSession):
        def submit(self, text: str) -> bool:
            self.submitted.append(text)
            self.turn += 1
            self.state = "busy"
            # Echo the received prompt (including '@all sync now') back into events!
            self._events.append({"type": "assistant", "text": f"Handled prompt: {text[:120]}"})
            threading.Timer(0.03, self.finish_turn).start()
            return True

    class _EchoManager(_WarmManager):
        def create(self, cfg: Any) -> _WarmSession:
            self.configs.append(cfg)
            sid = f"echo_{len(self.configs)}"
            sess = _EchoPromptSession(sid, state=self.initial_state)
            self.sessions[sid] = sess
            return sess

    mgr = _EchoManager(initial_state="idle")
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    dispatcher = register_steer(
        rooms,
        send_managed_input=lambda sid, txt: mgr.get_session(sid).submit(txt),
        swarm_registry=slot_reg,
        wake_session_fn=mgr.wake,
    )
    coord = swarm_routes.LocalSwarmCoordinator(
        swarm_env / "local-swarms",
        rooms=rooms,
        swarm_registry=slot_reg,
        steer_dispatcher=dispatcher,
        mgr=mgr,
    )
    sw = coord.create_swarm({"goal": "Cascade test", "leads": ["fix-1", "fix-2"]})
    sid = sw["swarm_id"]

    r1 = swarm_routes.delegate_harness_worker(
        mgr, None, swarm_id=sid, agent="demiurge", slot_id="fix-1",
        harness="claude", prompt="Task 1", wait=True, timeout_s=5.0, poll_s=0.02,
        local_only=True, local_coordinator=coord,
    )
    r2 = swarm_routes.delegate_harness_worker(
        mgr, None, swarm_id=sid, agent="demiurge", slot_id="fix-2",
        harness="claude", prompt="Task 2", wait=True, timeout_s=5.0, poll_s=0.02,
        local_only=True, local_coordinator=coord,
    )

    coord.post_message(sid, {"agent": "coordinator", "text": "@all sync now"})
    time.sleep(0.4)
    s1 = mgr.sessions[r1["session_id"]]
    s2 = mgr.sessions[r2["session_id"]]
    assert len(s1.submitted) == 2, f"cascade detected on fix-1: {s1.submitted}"
    assert len(s2.submitted) == 2, f"cascade detected on fix-2: {s2.submitted}"


def test_shared_thread_multi_slot_not_blocked_by_is_thread_owned(swarm_env):
    subs = ThreadSubscriptions(swarm_env / "subs_shared.db")
    try:
        spawned: List[SpawnRequest] = []
        reg = SwarmSlotRegistry()
        reg.register_slot(
            swarm_id="sw_a",
            slot_id="fix-1",
            session_id="sess_fix_1",
            thread_id="th_shared",
        )
        router = ThreadRouter(
            thread_subscriptions=subs,
            spawn=lambda req: spawned.append(req) or f"w-{len(spawned)}",
            allow_unstamped=True,
            swarm_registry=reg,
            status_file=swarm_env / "tr_shared.json",
        )
        room = Room("trshared")
        room.add_listener(router.handle)

        # Same thread, DIFFERENT slot_id -> must spawn fix-2
        room.publish(
            {
                "type": "agent_message",
                "actor": {"kind": "human", "id": "owner", "name": "owner"},
                "payload": {
                    "text": "work fix-2",
                    "work_request": True,
                    "allow_multi_worker": True,
                    "slot_id": "fix-2",
                    "thread": {"surface": "swarm", "id": "th_shared"},
                },
            }
        )
        assert [r.slot_id for r in spawned] == ["fix-2"]

        # Same thread, SAME slot_id (fix-1) -> must skip
        room.publish(
            {
                "type": "agent_message",
                "actor": {"kind": "human", "id": "owner", "name": "owner"},
                "payload": {
                    "text": "work fix-1",
                    "work_request": True,
                    "allow_multi_worker": True,
                    "slot_id": "fix-1",
                    "thread": {"surface": "swarm", "id": "th_shared"},
                },
            }
        )
        assert [r.slot_id for r in spawned] == ["fix-2"]
    finally:
        subs.close()


def test_awgit_rollback_deletes_branch_allowing_retry(swarm_env, monkeypatch):
    if not shutil.which("git") or not _AWGIT_PKG.is_dir():
        pytest.skip("git or awgit package unavailable")

    monkeypatch.syspath_prepend(str(_AWGIT_PKG))
    monkeypatch.setenv("VCS_DATA_ROOT", str(swarm_env / "vcs_retry"))
    monkeypatch.setenv("AWGIT_DATA_ROOT", str(swarm_env / "vcs_retry"))
    monkeypatch.delenv("VCS_LEASE_API_URL", raising=False)

    repo = swarm_env / "repo_retry"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@aitherium.local"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo), check=True)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    b1 = swarm_routes.bind_slot_worktree_and_leases(
        cwd=str(repo), swarm_id="sw_edge", slot_id="fix-1", owns=["src/a.py"],
        use_worktree=True, use_leases=True,
    )
    assert b1["worktree_path"]

    with pytest.raises(swarm_routes.SwarmLeaseConflictError):
        swarm_routes.bind_slot_worktree_and_leases(
            cwd=str(repo), swarm_id="sw_edge", slot_id="fix-2", owns=["src/a.py"],
            use_worktree=True, use_leases=True,
        )
    branches = subprocess.run(
        ["git", "branch", "--list", "swarm/sw_edge/fix-2"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branches == "", f"rollback leaked branch: {branches!r}"

    swarm_routes.release_slot_bindings(
        cwd=str(repo), swarm_id="sw_edge", slot_id="fix-1",
        lease_ids=b1["lease_ids"], worktree_name=b1["worktree_name"],
        worktree_path=b1["worktree_path"], branch=b1["branch"], remove_worktree=True,
    )
    b2 = swarm_routes.bind_slot_worktree_and_leases(
        cwd=str(repo), swarm_id="sw_edge", slot_id="fix-2", owns=["src/a.py"],
        use_worktree=True, use_leases=True,
    )
    assert b2["worktree_path"] and Path(b2["worktree_path"]).is_dir()
    assert len(b2["lease_ids"]) == 1


def test_stall_nudged_reset_case_insensitive_self_dedup_and_awake_sync(swarm_env):
    mgr = _WarmManager(initial_state="idle")
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    dispatcher = register_steer(
        rooms,
        send_managed_input=lambda sid, txt: mgr.get_session(sid).submit(txt),
        swarm_registry=slot_reg,
        wake_session_fn=mgr.wake,
    )
    coord = swarm_routes.LocalSwarmCoordinator(
        swarm_env / "local-swarms",
        rooms=rooms,
        swarm_registry=slot_reg,
        steer_dispatcher=dispatcher,
        mgr=mgr,
    )
    leads = [f"fix-{i}" for i in range(1, 10)]
    sw = coord.create_swarm({"goal": "Test 5a/5b/awake", "leads": leads, "stall_timeout_s": 10.0})
    sid = sw["swarm_id"]

    sessions: Dict[str, _WarmSession] = {}
    for lead in leads:
        sess = _WarmSession(f"s_{lead}", state="idle")
        mgr.sessions[sess.id] = sess
        sessions[lead] = sess
        coord.register_member_session(sid, slot_id=lead, member_id=lead, agent="demiurge", session_id=sess.id)

    # 5a: Nudge fix-1, then fix-1 posts update -> stall_nudged resets -> next stall nudges instead of escalating
    first = coord.check_stalls(sid, stall_timeout_s=10.0, nudge_grace_s=30.0, now=time.time() + 50.0)
    assert any(a["slot_id"] == "fix-1" and a["action"] == "nudged" for a in first["actions"])
    coord.post_message(sid, {"agent": "fix-1", "kind": "update", "text": "making progress"})
    second = coord.check_stalls(sid, stall_timeout_s=10.0, nudge_grace_s=30.0, now=time.time() + 250.0)
    fix1_actions = [a for a in second["actions"] if a["slot_id"] == "fix-1"]
    assert fix1_actions and fix1_actions[0]["action"] == "nudged"

    # Auditor: non-bounce question/mention syncs persisted session_state='awake'
    coord.set_member_session_state(sid, "fix-2", "waiting")
    assert coord.get_swarm(sid)["members_by_slot"]["fix-2"]["session_state"] == "waiting"
    coord.post_message(sid, {"agent": "fix-1", "kind": "question", "text": "@fix-2 please confirm"})
    assert coord.get_swarm(sid)["members_by_slot"]["fix-2"]["session_state"] == "awake"

    # 5b + Auditor dedup: >8 mentions with 'Fix-1' sender and 'all' + 'fix-2'
    before_s1 = len(sessions["fix-1"].submitted)
    before_s2 = len(sessions["fix-2"].submitted)
    coord.post_message(
        sid,
        {
            "agent": "Fix-1",
            "kind": "update",
            "text": "fanout check",
            "mentions": ["all", "fix-1", "fix-2", "fix-3", "fix-4", "fix-5", "fix-6", "fix-7", "fix-8", "fix-9"],
        },
    )
    assert len(sessions["fix-1"].submitted) == before_s1, "case-insensitive self-exclusion failed"
    assert len(sessions["fix-2"].submitted) == before_s2 + 1, "duplicate delivery for all + explicit slot"

    # Concurrent status writes must not raise FileNotFoundError
    errs: List[str] = []

    def _spam_status():
        for _ in range(15):
            dispatcher._write_status()
            if dispatcher.status()["last_error"]:
                errs.append(dispatcher.status()["last_error"])

    threads = [threading.Thread(target=_spam_status) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errs == []


def test_extract_session_summary_supports_harness_event_kind():
    from adk.harnesses.events import error as err_ev, text_delta

    summary = swarm_routes.extract_session_summary(
        [text_delta("Fixed bug").to_dict(), err_ev("boom").to_dict()]
    )
    assert "Fixed bug" in summary
    assert "ERROR: boom" in summary


def test_watch_and_report_snapshots_before_post_and_populates_result_before_trace(swarm_env, monkeypatch):
    class _BouncePoster:
        def __init__(self, sess: _WarmSession) -> None:
            self.sess = sess
            self.posts: List[Dict[str, Any]] = []

        def post_message(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            self.posts.append(payload)
            if len(self.posts) == 1:
                # Simulate synchronous bounce arriving before turn-1 post returns
                self.sess.submit("bounced follow-up")
            return {"seq": len(self.posts)}

    sess = _WarmSession("sess_race", state="idle")
    poster = _BouncePoster(sess)
    result_box: Dict[str, Any] = {}
    observed_during_trace: Dict[str, Any] = {}

    def _fake_trace(**kwargs):
        observed_during_trace.update(result_box)
        return "sp_test_123"

    monkeypatch.setattr(swarm_routes, "_emit_harness_trace_span", _fake_trace)
    turn1_done = threading.Event()
    t = threading.Thread(
        target=swarm_routes._watch_and_report,
        kwargs={
            "session": sess,
            "poster": poster,
            "swarm_id": "sw_race",
            "agent": "fix-1",
            "slot_id": "fix-1",
            "harness": "claude",
            "thread_id": "th_1",
            "report_to": "verifier",
            "timeout_s": 2.0,
            "poll_s": 0.02,
            "result_box": result_box,
            "keep_warm": True,
            "turn1_done": turn1_done,
        },
        daemon=True,
    )
    t.start()
    assert turn1_done.wait(timeout=3.0)
    assert observed_during_trace.get("reported") is True
    assert result_box.get("span_id") == "sp_test_123"

    # Follow-up turn from the synchronous bounce MUST be observed and reported (not lost!)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(poster.posts) < 2:
        time.sleep(0.02)
    assert len(poster.posts) >= 2
    assert "finished follow-up turn" in poster.posts[1]["text"]
    sess.state = "exited"
    t.join(timeout=1.0)


def test_watch_and_report_sync_bounce_preserves_awake_state(swarm_env, monkeypatch):
    monkeypatch.setattr(swarm_routes, "_emit_harness_trace_span", lambda **kw: None)
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    coord = swarm_routes.LocalSwarmCoordinator(
        swarm_env / "local-swarms",
        rooms=rooms,
        swarm_registry=slot_reg,
    )
    sw = coord.create_swarm({"goal": "sync bounce order", "leads": ["fix-1", "verifier"]})
    sid = sw["swarm_id"]
    sess = _WarmSession("sess_sync_bounce", state="idle")
    coord.register_member_session(
        sid,
        slot_id="fix-1",
        member_id="fix-1",
        agent="fix-1",
        session_id=sess.id,
        thread_id="th_1",
    )

    class _SyncBouncePoster:
        def post_message(self, swarm_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            res = coord.post_message(swarm_id, payload)
            if payload.get("kind") == "done" and res["seq"] == 1:
                coord.post_message(
                    swarm_id,
                    {
                        "agent": "verifier",
                        "slot_id": "verifier",
                        "kind": "bounce",
                        "text": "@fix-1 [bounce] fix edge case",
                        "mentions": ["fix-1"],
                    },
                )
            return res

    result_box: Dict[str, Any] = {}
    turn1_done = threading.Event()
    t = threading.Thread(
        target=swarm_routes._watch_and_report,
        kwargs={
            "session": sess,
            "poster": _SyncBouncePoster(),
            "swarm_id": sid,
            "agent": "fix-1",
            "slot_id": "fix-1",
            "harness": "claude",
            "thread_id": "th_1",
            "report_to": "verifier",
            "timeout_s": 2.0,
            "poll_s": 0.02,
            "result_box": result_box,
            "keep_warm": True,
            "turn1_done": turn1_done,
            "swarm_registry": slot_reg,
            "local_coordinator": coord,
        },
        daemon=True,
    )
    t.start()
    assert turn1_done.wait(timeout=10.0)
    assert result_box.get("reported") is True
    assert result_box.get("session_state") == "awake"
    view = coord.get_swarm(sid)
    assert view["members_by_slot"]["fix-1"]["session_state"] == "awake"
    sess.state = "exited"
    t.join(timeout=1.0)


def test_launch_swarm_genesis_fallback_returns_422_and_watch_forwards_awrun_dir(swarm_env, monkeypatch):
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    monkeypatch.setattr(swarm_routes, "_emit_harness_trace_span", lambda **kw: None)
    monkeypatch.setenv("AITHER_SWARM_API_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(swarm_routes, "_internal_key", lambda: "")
    monkeypatch.setattr(swarm_routes, "_session_bearer", lambda: "")
    monkeypatch.delenv("AITHER_INTERNAL_SECRET", raising=False)
    monkeypatch.delenv("AITHER_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
    monkeypatch.delenv("AITHER_API_KEY", raising=False)

    mgr = _WarmManager(initial_state="exited")
    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    coord = swarm_routes.LocalSwarmCoordinator(
        swarm_env / "local-swarms",
        rooms=rooms,
        swarm_registry=slot_reg,
        mgr=mgr,
    )

    class _P:
        def __init__(self, pid: str, plan: str) -> None:
            self.id = pid
            self.plan = plan

    def auth(authorization: str = Header(default="")) -> _P:
        if authorization == "Bearer owner-tok":
            return _P("owner", "owner")
        raise HTTPException(401, "missing")

    def require_owner(p: _P = Depends(auth)) -> _P:
        if p.plan != "owner":
            raise HTTPException(403, "owner only")
        return p

    app = FastAPI()
    swarm_routes.mount(
        app,
        auth,
        require_owner,
        mgr=mgr,
        rooms=rooms,
        swarm_registry=slot_reg,
        local_coordinator=coord,
    )
    tc = TestClient(app)
    h_own = {"Authorization": "Bearer owner-tok"}

    # Genesis has no creds -> falls back to local_coord.create_swarm, which rejects room_id='main' with 422 (never 500)
    bad = tc.post(
        "/swarms",
        headers=h_own,
        json={"goal": "Fallback validation", "local_only": True, "room_id": "main"},
    )
    assert bad.status_code == 422, bad.text
    assert "main" in bad.text.lower()

    # _watch_and_report forwards awrun_dir even if AITHER_AWRUN_DIR changes before completion
    awrun_pkg = Path(__file__).resolve().parents[2] / "AitherOS" / "packages" / "awrun"
    if awrun_pkg.is_dir():
        monkeypatch.syspath_prepend(str(awrun_pkg))
        from awrun.store import RunStore

        custom_awrun = swarm_env / "explicit_awrun"
        monkeypatch.setenv("AITHER_AWRUN_DIR", str(custom_awrun))
        sw = coord.create_swarm({"goal": "Forward awrun_dir", "leads": ["fix-1"]})
        sid = sw["swarm_id"]

        class _DelayedExitMgr(_WarmManager):
            def create(self, cfg: Any) -> _WarmSession:
                sess = super().create(cfg)
                sess.state = "busy"
                return sess

        dmgr = _DelayedExitMgr(initial_state="busy")
        res = swarm_routes.delegate_harness_worker(
            dmgr,
            None,
            swarm_id=sid,
            agent="demiurge",
            slot_id="fix-1",
            harness="claude",
            prompt="Run with explicit awrun_dir",
            execution_mode="awrun",
            wait=False,
            timeout_s=5.0,
            poll_s=0.02,
            local_only=True,
            local_coordinator=coord,
        )
        run_id = res["run_id"]
        assert run_id.startswith("r-")
        # Mutate AITHER_AWRUN_DIR to a different path before watcher finishes!
        monkeypatch.setenv("AITHER_AWRUN_DIR", str(swarm_env / "other_awrun"))
        sess = dmgr.sessions[res["session_id"]]
        sess._events.append({"type": "assistant", "text": "Finished clean"})
        sess.state = "exited"

        store = RunStore(custom_awrun)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            cur = store.get(run_id)
            if cur is not None and cur.status == "done":
                break
            time.sleep(0.02)
        final_item = store.get(run_id)
        assert final_item is not None
        assert final_item.status == "done"

