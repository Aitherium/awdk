"""Tests for awdk harness daemon /swarms/* bridge, MCP tools, and awconnect status."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any, Dict, List

import pytest

_AWDK = Path(__file__).resolve().parents[1]
if str(_AWDK) not in sys.path:
    sys.path.insert(0, str(_AWDK))

from adk import awconnect_setup  # noqa: E402
from adk.harnesses import mcp_stdio, pairing, swarm_routes  # noqa: E402


class _FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _FakeGenesis:
    def __init__(self) -> None:
        self.calls: List[tuple[str, str, Dict[str, str], Dict[str, Any]]] = []
        self.swarms: Dict[str, Dict[str, Any]] = {
            "swm_demo1": {
                "swarm_id": "swm_demo1",
                "title": "Demo Swarm",
                "status": "live",
                "ui_url": "https://app.aitherium.com/swarm?id=swm_demo1",
                "members": [{"name": "atlas", "state": "working"}, {"name": "demiurge", "state": "working"}],
                "overview": {"progress": {"done": 1, "total": 2}},
                "board": [],
            }
        }

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        headers = {k.lower(): v for k, v in req.header_items()}
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.calls.append((method, url, headers, body))
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path.split("?", 1)[0]
        if path.startswith("/api/bridge/genesis"):
            path = path[len("/api/bridge/genesis"):] or "/"
        if method == "GET" and path == "/swarms":
            rows = list(self.swarms.values())
            return _FakeResp(json.dumps({"swarms": rows, "count": len(rows)}).encode())
        if method == "POST" and path == "/swarms":
            sid = f"swm_{len(self.swarms) + 1:04d}"
            rec = {
                "swarm_id": sid,
                "title": body.get("title") or body.get("goal", "Swarm")[:40],
                "status": "live",
                "coordinator": body.get("coordinator", "aither"),
                "ui_url": f"https://app.aitherium.com/swarm?id={sid}",
                "board": [],
            }
            self.swarms[sid] = rec
            return _FakeResp(json.dumps(rec).encode())
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "swarms":
            sid = parts[1]
            rec = self.swarms.get(sid)
            if rec is None:
                raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b'{"detail":"not found"}'))
            if len(parts) == 2 and method == "GET":
                return _FakeResp(json.dumps(rec).encode())
            if len(parts) == 3 and parts[2] == "steer" and method == "POST":
                seq = len(rec["board"]) + 1
                post = {"seq": seq, "agent": body.get("user", "human"), "text": body.get("message", "")}
                rec["board"].append(post)
                return _FakeResp(json.dumps({"ok": True, "swarm_id": sid, "seq": seq, "status": rec["status"]}).encode())
            if len(parts) == 3 and parts[2] == "pause" and method == "POST":
                rec["status"] = "paused"
                return _FakeResp(json.dumps({"ok": True, "swarm_id": sid, "status": "paused"}).encode())
            if len(parts) == 3 and parts[2] == "control" and method == "POST":
                rec["status"] = body.get("action", "paused")
                return _FakeResp(json.dumps({"ok": True, "swarm_id": sid, "status": rec["status"]}).encode())
            if len(parts) == 3 and parts[2] == "post" and method == "POST":
                seq = len(rec["board"]) + 1
                post = {"seq": seq, **body}
                rec["board"].append(post)
                return _FakeResp(json.dumps({"ok": True, "swarm_id": sid, "seq": seq, "post": post}).encode())
        raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b"{}"))


class _FakeSession:
    def __init__(self, sid: str, state: str, events: List[Dict[str, Any]]) -> None:
        self.id = sid
        self.state = state
        self._events = events
        self.sent: List[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def recent_events(self) -> List[Dict[str, Any]]:
        return list(self._events)


class _FakeManager:
    def __init__(self, session_state: str = "exited", events: List[Dict[str, Any]] | None = None) -> None:
        self.created: List[Any] = []
        self.session_state = session_state
        self.events = events or [
            {"type": "assistant", "text": "Implemented fix and passed tests.\nScreenshot: /tmp/proof.png | Live proof"},
        ]

    def list_sessions(self) -> List[Any]:
        return []

    def create(self, cfg: Any) -> _FakeSession:
        self.created.append(cfg)
        return _FakeSession(f"sess_{len(self.created)}", self.session_state, self.events)


def test_genesis_client_is_fail_closed_for_writes(monkeypatch):
    fake = _FakeGenesis()
    monkeypatch.delenv("AITHER_INTERNAL_SECRET", raising=False)
    monkeypatch.delenv("AITHER_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
    client = swarm_routes.SwarmGenesisClient(
        "https://127.0.0.1:8001", internal_key="", bearer="", opener=fake,
    )
    # Reads work, writes fail closed with 401 SwarmAuthError and make zero network calls
    assert client.list_swarms()["count"] == 1
    calls_before = len(fake.calls)
    for fn in (
        lambda: client.create_swarm({"goal": "test"}),
        lambda: client.steer_swarm("swm_demo1", {"message": "go"}),
        lambda: client.pause_swarm("swm_demo1"),
        lambda: client.control_swarm("swm_demo1", {"action": "cancel"}),
        lambda: client.post_message("swm_demo1", {"agent": "atlas", "text": "hi"}),
    ):
        with pytest.raises(swarm_routes.SwarmAuthError) as ei:
            fn()
        assert ei.value.status == 401
    assert len(fake.calls) == calls_before

    # With internal key configured, header is attached and write succeeds
    authed = swarm_routes.SwarmGenesisClient(
        "https://127.0.0.1:8001", internal_key="k_test", bearer="", opener=fake,
    )
    created = authed.create_swarm({"goal": "Build bridge"})
    assert created["swarm_id"] == "swm_0002"
    assert fake.calls[-1][2].get("x-internal-key") == "k_test"


def test_delegate_harness_worker_spawns_session_and_streams_done_and_artifacts():
    fake = _FakeGenesis()
    client = swarm_routes.SwarmGenesisClient(
        "https://127.0.0.1:8001", internal_key="k_test", bearer="", opener=fake,
    )
    mgr = _FakeManager(session_state="exited")
    res = swarm_routes.delegate_harness_worker(
        mgr,
        client,
        swarm_id="swm_demo1",
        agent="demiurge",
        harness="claude",
        prompt="Fix auth timeout and attach proof",
        thread_id="t1",
        wait=True,
        timeout_s=5.0,
        poll_s=0.02,
    )
    assert res["ok"] is True
    assert res["session_id"] == "sess_1"
    assert res["result"]["reported"] is True
    assert res["result"]["kind"] == "done"
    assert mgr.created[0].harness == "claude"
    board = fake.swarms["swm_demo1"]["board"]
    done_post = board[-1]
    assert done_post["kind"] == "done"
    assert done_post["agent"] == "demiurge"
    assert "@coordinator [done]" in done_post["text"]
    assert "Implemented fix" in done_post["text"]
    assert done_post["artifacts"] == [{"path": "/tmp/proof.png", "kind": "screenshot", "caption": "Live proof"}]

    # Failed harness session reports [stall] back onto the board
    mgr_fail = _FakeManager(session_state="failed", events=[{"type": "error", "text": "OOM killed"}])
    res_fail = swarm_routes.delegate_harness_worker(
        mgr_fail, client, swarm_id="swm_demo1", agent="atlas", harness="codex",
        prompt="Run audit", wait=True, timeout_s=5.0, poll_s=0.02,
    )
    assert res_fail["result"]["kind"] == "stall"
    assert "[stall]" in fake.swarms["swm_demo1"]["board"][-1]["text"]


def test_genesis_base_defaults_to_control_plane_not_localhost_and_honors_override(monkeypatch):
    for var in ("AITHER_SWARM_API_URL", "AITHER_GENESIS_URL", "AITHER_API_URL", "AITHER_GATEWAY_URL", "AITHER_PORTAL_URL", "AITHER_ELYSIUM_URL"):
        monkeypatch.delenv(var, raising=False)
    default_url = swarm_routes._genesis_base()
    assert "127.0.0.1:8001" not in default_url and "localhost:8001" not in default_url
    assert default_url == "https://api.aitherium.com/api/bridge/genesis"

    monkeypatch.setenv("AITHER_SWARM_API_URL", "https://aitheros-genesis:8001/")
    assert swarm_routes._genesis_base() == "https://aitheros-genesis:8001"

    # Unreachable Genesis is reported honestly in probe_reachable() with actionable hint
    def _boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    dead = swarm_routes.SwarmGenesisClient("https://aitheros-genesis:8001", opener=_boom)
    probe = dead.probe_reachable(timeout_s=1.0)
    assert probe["reachable"] is False
    assert probe["status"] == 503
    assert "AITHER_SWARM_API_URL" in probe["hint"]


def test_local_swarm_capacity_and_awconnect_status_report_swarm_fields(monkeypatch):
    for var in ("AITHER_SWARM_API_URL", "AITHER_GENESIS_URL", "AITHER_API_URL", "AITHER_GATEWAY_URL"):
        monkeypatch.delenv(var, raising=False)
    fake = _FakeGenesis()
    client = swarm_routes.SwarmGenesisClient(
        "https://api.aitherium.com/api/bridge/genesis", internal_key="k_test", bearer="", opener=fake,
    )
    cap = swarm_routes.local_swarm_capacity(
        _FakeManager(), genesis=client, installed_ids=["claude", "codex", "gemini", "aither", "awdk", "terminal"],
        probe_genesis=True,
    )
    assert cap["swarm_harnesses"] == ["claude", "codex", "gemini", "aither", "awdk"]
    assert cap["swarms_endpoint"] == "/swarms"
    assert cap["ui_url"] == "https://app.aitherium.com/swarm"
    assert cap["genesis"]["reachable"] is True
    assert cap["genesis"]["url"] == "https://api.aitherium.com/api/bridge/genesis/swarms"
    assert swarm_routes.swarm_ui_url({"swarm_id": "sw_973f06c767", "ui_url": "/swarm?id=sw_973f06c767"}) == "https://app.aitherium.com/swarm?id=sw_973f06c767"

    st = awconnect_setup.status(browsers=[])
    assert "swarm" in st
    assert st["swarm"]["ui_url"] == "https://app.aitherium.com/swarm"
    assert st["swarm"]["swarms_endpoint"].endswith("/swarms")
    assert "127.0.0.1:8001" not in st["swarm"]["swarms_endpoint"]
    assert isinstance(st["swarm"]["local_harnesses"], list)


def test_fastapi_swarm_routes_enforce_owner_vs_awconnect_scope():
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    fake = _FakeGenesis()
    mgr = _FakeManager()

    class _P:
        def __init__(self, pid: str, owner: bool) -> None:
            self.id = pid
            self.owner = owner

    def auth(authorization: str = Header(default="")) -> _P:
        if authorization == "Bearer owner-tok":
            return _P("owner", True)
        if authorization == "Bearer awconnect-tok":
            return _P("awconnect", False)
        raise HTTPException(401, "missing token")

    def require_owner(p: _P = Depends(auth)) -> _P:
        if not p.owner:
            raise HTTPException(403, "owner only")
        return p

    app = FastAPI()
    swarm_routes.mount(
        app, auth, require_owner, mgr=mgr,
        genesis_factory=lambda: swarm_routes.SwarmGenesisClient(
            "https://127.0.0.1:8001", internal_key="k_test", bearer="", opener=fake,
        ),
    )
    tc = TestClient(app)

    # Unauthenticated -> 401
    assert tc.get("/swarms").status_code == 401
    # Paired awconnect token: can read & steer, CANNOT launch/pause/control/delegate
    h_awc = {"Authorization": "Bearer awconnect-tok"}
    assert tc.get("/swarms/capacity", headers=h_awc).status_code == 200
    assert tc.get("/swarms", headers=h_awc).status_code == 200
    assert tc.get("/swarms/swm_demo1", headers=h_awc).status_code == 200
    assert tc.post("/swarms/swm_demo1/steer", headers=h_awc, json={"message": "Focus tests"}).status_code == 200
    assert tc.post("/swarms", headers=h_awc, json={"goal": "Unauthorized"}).status_code == 403
    assert tc.post("/swarms/swm_demo1/pause", headers=h_awc, json={}).status_code == 403
    assert tc.post("/swarms/swm_demo1/control", headers=h_awc, json={"action": "cancel"}).status_code == 403
    assert tc.post("/swarms/swm_demo1/delegate-harness", headers=h_awc,
                   json={"harness": "claude", "prompt": "hack"}).status_code == 403

    # Owner token: full lifecycle works
    h_own = {"Authorization": "Bearer owner-tok"}
    r_new = tc.post("/swarms", headers=h_own, json={"goal": "Ship swarm"})
    assert r_new.status_code == 200 and r_new.json()["swarm_id"] == "swm_0002"
    r_del = tc.post("/swarms/swm_demo1/delegate-harness", headers=h_own,
                    json={"harness": "claude", "agent": "demiurge", "prompt": "Fix bug", "wait": True})
    assert r_del.status_code == 200 and r_del.json()["ok"] is True
    assert tc.post("/swarms/swm_demo1/pause", headers=h_own, json={"reason": "review"}).status_code == 200
    assert "GET /swarms" in pairing.AWCONNECT_PATHS and "POST /swarms" in pairing.AWCONNECT_PATHS


def test_mcp_stdio_exposes_all_six_swarm_tools(monkeypatch):
    want = {
        "awsh_swarm_list",
        "awsh_swarm_status",
        "awsh_swarm_launch",
        "awsh_swarm_steer",
        "awsh_swarm_pause",
        "awsh_swarm_delegate_harness",
    }
    assert want <= set(mcp_stdio.BY_NAME)
    assert "/swarms" in mcp_stdio.AGENT_TOKEN_PATHS
    seen: List[tuple] = []
    monkeypatch.setattr(mcp_stdio, "_req", lambda m, p, body=None: seen.append((m, p, body)) or {"ok": True})
    mcp_stdio.BY_NAME["awsh_swarm_list"]["fn"]({"status": "live", "limit": 10})
    mcp_stdio.BY_NAME["awsh_swarm_status"]["fn"]({"swarm_id": "swm_demo1"})
    mcp_stdio.BY_NAME["awsh_swarm_status"]["fn"]({"probe": True})
    mcp_stdio.BY_NAME["awsh_swarm_launch"]["fn"]({"goal": "Audit auth", "leads": ["atlas", "lyra"]})
    mcp_stdio.BY_NAME["awsh_swarm_steer"]["fn"]({"swarm_id": "swm_demo1", "message": "Prioritize tests"})
    mcp_stdio.BY_NAME["awsh_swarm_pause"]["fn"]({"swarm_id": "swm_demo1", "reason": "done"})
    mcp_stdio.BY_NAME["awsh_swarm_delegate_harness"]["fn"]({
        "swarm_id": "swm_demo1", "harness": "codex", "agent": "demiurge", "prompt": "Fix test",
    })
    assert [c[:2] for c in seen] == [
        ("GET", "/swarms?status=live&limit=10"),
        ("GET", "/swarms/swm_demo1"),
        ("GET", "/swarms/capacity?probe=true"),
        ("POST", "/swarms"),
        ("POST", "/swarms/swm_demo1/steer"),
        ("POST", "/swarms/swm_demo1/pause"),
        ("POST", "/swarms/swm_demo1/delegate-harness"),
    ]
