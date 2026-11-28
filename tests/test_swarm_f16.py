"""Focused M1.3 tests for M1<->M2 daemon RC contract and swarm_routes parity/F16."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

_AWDK = Path(__file__).resolve().parents[1]
_REPO = _AWDK.parent
_AWSEAL_PKG = _REPO / "AitherOS" / "packages" / "awseal"
if str(_AWDK) not in sys.path:
    sys.path.insert(0, str(_AWDK))

from adk.harnesses import rooms as rooms_mod, swarm_routes  # noqa: E402


def test_daemon_rc_status_scoped_link_contract_and_fallback(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import adk.harnesses.daemon as daemon
    from adk.harnesses import pairing, session as session_mod, session_directory as directory_mod
    from adk.harnesses.manager import SessionManager

    token = "owner-rc-contract-token"
    reg_path = tmp_path / "principals.json"
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", token)
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(reg_path))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setenv("AITHER_LOCAL_SWARMS_DIR", str(tmp_path / "local-swarms"))
    monkeypatch.delenv("AITHER_RC_STATE_FILE", raising=False)
    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", reg_path)
    monkeypatch.setattr(
        directory_mod, "_directory", directory_mod.SessionDirectory(discover_fn=lambda: [])
    )
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)
    monkeypatch.setattr(rooms_mod, "_registry", None)

    assert "GET /rc/status" in daemon.SCOPED_LINK_PATHS
    assert all(isinstance(entry, str) for entry in daemon.SCOPED_LINK_PATHS)

    # 1. Missing adk.harnesses.rc_routes -> create_app() boots cleanly, GET /rc/status -> 404
    monkeypatch.setitem(sys.modules, "adk.harnesses.rc_routes", None)
    app_no_rc = daemon.create_app(manager=SessionManager(root=tmp_path / "s1"), token=token)
    tc_no_rc = TestClient(app_no_rc)
    h_owner = {"Authorization": f"Bearer {token}"}
    assert tc_no_rc.get("/rc/status", headers=h_owner).status_code == 404

    # 2. Real rc_routes mounted -> GET /rc/status returns 200 for owner, link token, and awconnect token
    sys.modules.pop("adk.harnesses.rc_routes", None)
    import adk.harnesses.rc_routes  # noqa: F401, PLC0415

    monkeypatch.setattr(rooms_mod, "_registry", None)
    app_with_rc = daemon.create_app(manager=SessionManager(root=tmp_path / "s2"), token=token)
    tc = TestClient(app_with_rc)

    link_tok = daemon.mint_scoped_token(principal_id="node:n1", path=reg_path)
    awc_tok = daemon.mint_scoped_token(
        principal_id="awconnect:ext",
        paths=pairing.AWCONNECT_PATHS,
        plan="awconnect",
        path=reg_path,
    )
    h_link = {"Authorization": f"Bearer {link_tok}"}
    h_link_owner = {
        "Authorization": f"Bearer {link_tok}",
        daemon.LINK_ACTOR_HEADER: daemon.LINK_ACTOR_OWNER,
    }
    h_awc = {"Authorization": f"Bearer {awc_tok}"}

    for hdrs in (h_owner, h_link, h_awc):
        r = tc.get("/rc/status", headers=hdrs)
        assert r.status_code == 200, r.text
        data = r.json()
        assert {"active", "instance_id", "node_id", "rc_url", "app_url", "reach"} <= set(data)

    # POST /rc/status & POST /rc/off must never be permitted for scoped link & awconnect tokens
    assert tc.post("/rc/status", headers=h_link).status_code in (403, 405)
    assert tc.post("/rc/off", headers=h_link).status_code == 403
    assert tc.post("/rc/off", headers=h_awc).status_code == 403
    assert tc.post("/rc/off", headers=h_owner).status_code == 200

    # Existing session gates remain intact for scoped link tokens
    created = tc.post("/sessions", json={"harness": "claude", "cwd": ""}, headers=h_owner)
    assert created.status_code == 200
    sid = created.json()["id"]
    assert tc.get(f"/sessions/{sid}/transcript", headers=h_link).status_code == 403
    assert tc.get(f"/sessions/{sid}/transcript", headers=h_link_owner).status_code not in (401, 403)
    assert tc.post(f"/sessions/{sid}/interrupt", headers=h_link).status_code != 403
    assert tc.post(f"/sessions/{sid}/focus", headers=h_link_owner).status_code == 403
    assert tc.post("/sessions", json={"harness": "claude", "cwd": ""}, headers=h_link).status_code == 403


def test_swarm_routes_launch_parity_inline_pools_workers_and_genesis_proxies(tmp_path, monkeypatch):
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AITHER_LOCAL_SWARMS_DIR", str(tmp_path / "local-swarms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setattr(rooms_mod, "_registry", None)

    assert "inline" in swarm_routes.VALID_EXECUTION_MODES

    genesis_calls: List[tuple[str, str, Any]] = []

    class _FakeGenesisClient:
        base_url = "https://genesis.local"

        def list_swarms(self, **kw):
            return {"swarms": [], "count": 0}

        def steer_swarm(self, swarm_id: str, payload: Dict[str, Any]):
            genesis_calls.append(("steer", swarm_id, payload))
            return {"ok": True, "swarm_id": swarm_id, "seq": 1}

        def nudge_slot(self, swarm_id: str, payload: Dict[str, Any]):
            genesis_calls.append(("nudge", swarm_id, payload))
            return {"ok": True, "swarm_id": swarm_id, "action": "nudged"}

        def probe_reachable(self, timeout_s: float = 3.0):
            return {"reachable": True, "count": 0}

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
        mgr=SimpleNamespace(list_sessions=lambda: []),
        genesis_factory=lambda: _FakeGenesisClient(),
    )
    tc = TestClient(app)
    h_own = {"Authorization": "Bearer owner-tok"}

    # Launch local swarm using execution_mode="inline", workers, pools, and awseal_expect_key
    sw = tc.post(
        "/swarms",
        headers=h_own,
        json={
            "goal": "Workers + pools parity",
            "local_room": True,
            "execution_mode": "inline",
            "workers": [{"slot_id": "api-dev", "agent": "demiurge", "owns": ["api.py"]}],
            "pools": {"fix": {"count": 2, "owns": ["a.py", "b.py"]}},
            "awseal_expect_key": "deadbeef" * 8,
            "seal_artifacts": True,
        },
    )
    assert sw.status_code == 200, sw.text
    sdata = sw.json()
    assert set(["api-dev", "fix-1", "fix-2"]) <= set(sdata["members_by_slot"])
    assert sdata["members_by_slot"]["fix-1"]["owns"] == ["a.py"]
    assert sdata["members_by_slot"]["fix-2"]["owns"] == ["b.py"]
    assert sdata["metadata"]["expect_key"] == "deadbeef" * 8
    assert sdata["metadata"]["seal_artifacts"] is True

    # Local nudge with thread_id only
    th_id = sdata["members_by_slot"]["api-dev"]["thread_id"]
    n_local = tc.post(
        f"/swarms/{sdata['swarm_id']}/nudge",
        headers=h_own,
        json={"thread_id": th_id, "message": "check in"},
    )
    assert n_local.status_code == 200, n_local.text
    assert n_local.json()["slot_id"] == "api-dev"

    # Remote steer forwards mentions & remote nudge proxies to Genesis nudge_slot
    r_steer = tc.post(
        "/swarms/sw_remote_1/steer",
        headers=h_own,
        json={"message": "Focus M1", "mentions": ["fix-1"]},
    )
    assert r_steer.status_code == 200
    r_nudge = tc.post(
        "/swarms/sw_remote_1/nudge",
        headers=h_own,
        json={"thread_id": "th_9", "message": "wake up"},
    )
    assert r_nudge.status_code == 200
    assert genesis_calls[0] == (
        "steer",
        "sw_remote_1",
        {"message": "Focus M1", "user": "awsh:owner", "role": "admin", "source": "awsh", "mentions": ["fix-1"]},
    )
    assert genesis_calls[1] == (
        "nudge",
        "sw_remote_1",
        {"slot_id": None, "thread_id": "th_9", "message": "wake up"},
    )


def test_swarm_routes_rejects_invalid_pools_workers_and_isolation_with_422(tmp_path, monkeypatch):
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AITHER_LOCAL_SWARMS_DIR", str(tmp_path / "local-swarms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_AWRUN_DIR", str(tmp_path / "awrun"))
    monkeypatch.setattr(rooms_mod, "_registry", None)

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

    # Cover both local_room=True and Genesis-unreachable fallback (local_only=True, no genesis_factory)
    app = FastAPI()
    swarm_routes.mount(
        app,
        auth,
        require_owner,
        mgr=SimpleNamespace(list_sessions=lambda: []),
    )
    tc = TestClient(app, raise_server_exceptions=False)
    h_own = {"Authorization": "Bearer owner-tok"}

    bad_payloads = [
        {"goal": "t", "pools": {"fix": "abc"}, "local_room": True},
        {"goal": "t", "pools": {"fix": [1, 2]}, "local_only": True},
        {"goal": "t", "pools": {"fix": 0}, "local_room": True},
        {"goal": "t", "pools": {"fix": -1}, "local_room": True},
        {"goal": "t", "pools": {"fix": True}, "local_room": True},
        {"goal": "t", "pools": {"fix": 1.5}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"count": "abc"}}, "local_only": True},
        {"goal": "t", "pools": {"fix": {"size": "abc"}}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"count": [1]}}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"count": 0}}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"count": 0, "size": 2}}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"count": -1}}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"count": True}}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"owns": 123}}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"owns": "a.py"}}, "local_room": True},
        {"goal": "t", "pools": {"fix": {"must_not_touch": 123}}, "local_room": True},
        {"goal": "t", "workers": [{"pool": "fix", "count": "abc"}], "local_room": True},
        {"goal": "t", "workers": [{"pool": "fix", "count": 0}], "local_room": True},
        {"goal": "t", "workers": [{"slot_id": "w1", "owns": 123}], "local_room": True},
        {"goal": "t", "workers": [{"slot_id": "w1", "must_not_touch": "b.py"}], "local_room": True},
        {"goal": "t", "execution_mode": "awrun", "workers": [{"slot_id": "w1", "isolation": "bad"}], "local_room": True},
    ]
    for body in bad_payloads:
        resp = tc.post("/swarms", headers=h_own, json=body)
        assert resp.status_code == 422, f"{body!r} -> {resp.status_code}: {resp.text}"


def test_swarm_routes_awseal_011_compat_and_duplicate_slot_id_422(tmp_path, monkeypatch):
    import sys as _sys
    from types import ModuleType

    monkeypatch.setenv("AITHER_LOCAL_SWARMS_DIR", str(tmp_path / "local-swarms"))
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    awrun_dir = tmp_path / "awrun"
    monkeypatch.setenv("AITHER_AWRUN_DIR", str(awrun_dir))
    monkeypatch.setattr(rooms_mod, "_registry", None)

    # 1. Duplicate slot_id must fail 422 before creating any awrun RunItem
    coord = swarm_routes.LocalSwarmCoordinator(storage_dir=tmp_path / "local-swarms")
    with pytest.raises(swarm_routes.SwarmApiError, match="duplicate worker slot_id") as exc_info:
        coord.create_swarm(
            {
                "goal": "Dup slot check",
                "local_room": True,
                "execution_mode": "awrun",
                "workers": [
                    {"agent": "dev", "slot_id": "w1", "owns": ["a.py"]},
                    {"agent": "qa", "slot_id": "w1", "owns": ["b.py"]},
                ],
            }
        )
    assert exc_info.value.status == 422
    assert not list(awrun_dir.glob("*.json"))

    # 2. Simulated awseal 0.1.1 (no from_dict/verify_files/sign_files) still signs & verifies fail-closed
    if str(_AWSEAL_PKG) not in _sys.path:
        _sys.path.insert(0, str(_AWSEAL_PKG))
    import awseal as real_awseal  # noqa: PLC0415

    priv = tmp_path / "k.key"
    real_awseal.keygen(priv)
    pub_hex = real_awseal.public_key_hex(path=priv)

    compat_011 = ModuleType("awseal_011_stub")
    for attr in ("sign", "verify", "load", "write", "load_public_key", "public_key_hex", "SealError"):
        setattr(compat_011, attr, getattr(real_awseal, attr))
    monkeypatch.setattr(swarm_routes, "_load_awseal", lambda: compat_011)

    art_file = tmp_path / "out.txt"
    art_file.write_text("verified-content", encoding="utf-8")
    sealed = swarm_routes.verify_or_seal_track_artifacts(
        artifacts=[str(art_file)],
        seal_artifacts=True,
        key_path=str(priv),
        subject="track-011",
        kind="done",
    )
    assert sealed is not None
    assert sealed["scheme"] == "awseal-ed25519"
    assert sealed["ed25519"] is True

    verified = swarm_routes.verify_or_seal_track_artifacts(
        artifacts=[str(art_file)],
        seal_doc=sealed["seal"],
        expect_key=pub_hex,
        kind="done",
    )
    assert verified is not None
    assert verified["ok"] is True
    assert verified["key_trusted"] is True

    art_file.write_text("tampered-content", encoding="utf-8")
    with pytest.raises(swarm_routes.SwarmApiError, match="verification failed") as bad_exc:
        swarm_routes.verify_or_seal_track_artifacts(
            artifacts=[str(art_file)],
            seal_doc=sealed["seal"],
            expect_key=pub_hex,
            kind="done",
        )
    assert bad_exc.value.status == 422

