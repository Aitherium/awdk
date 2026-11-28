"""F16 Integration tests for M1.2: awrun, awseal, and zero-libc / awzeal in swarm_routes & mcp_stdio."""
from __future__ import annotations

import contextlib
import importlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

_AWDK = Path(__file__).resolve().parents[1]
_REPO = _AWDK.parent
_AWRUN_PKG = _REPO / "AitherOS" / "packages" / "awrun"
_AWSEAL_PKG = _REPO / "AitherOS" / "packages" / "awseal"
if str(_AWDK) not in sys.path:
    sys.path.insert(0, str(_AWDK))

from adk.harnesses import mcp_stdio, rooms as rooms_mod, swarm_routes  # noqa: E402
from adk.harnesses.rooms import RoomRegistry  # noqa: E402
from adk.harnesses.steer_dispatch import SwarmSlotRegistry, register as register_steer  # noqa: E402


class _SessionStub:
    def __init__(self, sid: str, state: str = "exited") -> None:
        self.id = sid
        self.state = state
        self.turn = 1
        self.submitted: List[str] = []
        self._events: List[Dict[str, Any]] = [
            {"kind": "text.delta", "text": f"Completed track for {sid}."}
        ]

    def submit(self, text: str) -> bool:
        self.submitted.append(text)
        return True

    def recent_events(self) -> List[Dict[str, Any]]:
        return list(self._events)


class _MgrStub:
    def __init__(self, state: str = "exited") -> None:
        self.state = state
        self.configs: List[Any] = []
        self.sessions: Dict[str, _SessionStub] = {}

    def list_sessions(self) -> List[Dict[str, Any]]:
        return [{"id": s.id, "state": s.state} for s in self.sessions.values()]

    def get_session(self, sid: str) -> _SessionStub:
        return self.sessions[sid]

    def create(self, cfg: Any) -> _SessionStub:
        self.configs.append(cfg)
        sid = f"f16_{len(self.configs)}"
        sess = _SessionStub(sid, state=self.state)
        self.sessions[sid] = sess
        return sess


@pytest.fixture()
def f16_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HARNESS_ROOMS_ROOT", str(tmp_path / "rooms"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_LOCAL_SWARMS_DIR", str(tmp_path / "local-swarms"))
    monkeypatch.setenv("AITHER_AWRUN_DIR", str(tmp_path / "awrun"))
    monkeypatch.setenv("AITHER_HARNESS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.delenv("AITHER_INTERNAL_SECRET", raising=False)
    monkeypatch.delenv("AITHER_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("AITHER_SESSION_BEARER", raising=False)
    monkeypatch.setattr(rooms_mod, "_registry", None)
    return tmp_path


def _make_app(f16_env: Path, mgr: Any = None):
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    rooms = RoomRegistry()
    slot_reg = SwarmSlotRegistry()
    dispatcher = register_steer(
        rooms,
        send_managed_input=(lambda sid, txt: mgr.get_session(sid).submit(txt)) if mgr else None,
        swarm_registry=slot_reg,
    )
    coord = swarm_routes.LocalSwarmCoordinator(
        f16_env / "local-swarms",
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
            return _P("agent:worker", "agent")
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
    return TestClient(app), coord


def test_awrun_confinement_lineage_capacity_and_export(f16_env, monkeypatch):
    if not _AWRUN_PKG.is_dir():
        pytest.skip("awrun package unavailable")
    monkeypatch.syspath_prepend(str(_AWRUN_PKG))

    from awrun.store import RunStore

    mgr = _MgrStub(state="exited")
    tc, coord = _make_app(f16_env, mgr=mgr)
    h_own = {"Authorization": "Bearer owner-tok"}
    h_agent = {"Authorization": "Bearer agent-tok"}

    # 1. Fail-closed confinement: cpus/egress without container isolation MUST fail 422
    bad = tc.post(
        "/swarms",
        headers=h_own,
        json={
            "goal": "Unconfined attempt",
            "local_room": True,
            "execution_mode": "awrun",
            "limits": {"cpus": 1, "memory_mb": 256},
            "egress": {"hosts": []},
        },
    )
    assert bad.status_code == 422
    assert "container" in bad.text.lower() or "refusing" in bad.text.lower()

    # 2. Valid confined swarm launch records RunItem with paths & swarm lineage
    ok_sw = tc.post(
        "/swarms",
        headers=h_own,
        json={
            "goal": "Confined awrun track",
            "local_room": True,
            "execution_mode": "awrun",
            "tasks": [
                {
                    "slot_id": "fix-1",
                    "agent": "demiurge",
                    "owns": ["src/a.py"],
                    "limits": {"cpus": 1, "memory_mb": 256},
                    "egress": {"hosts": []},
                    "isolation": {"mode": "container", "image": "aither-worker:1"},
                }
            ],
        },
    )
    assert ok_sw.status_code == 200, ok_sw.text
    sw = ok_sw.json()
    sid = sw["swarm_id"]
    run_id = sw["members_by_slot"]["fix-1"]["run_id"]
    assert run_id.startswith("r-")

    store = RunStore()
    item = store.get(run_id)
    assert item is not None
    assert item.kind == "agent"
    assert item.paths == ["src/a.py"]
    assert item.spec["swarm_lineage"] == {
        "swarm_id": sid,
        "thread_id": "th_1",
        "slot_id": "fix-1",
        "pool": "fix",
    }

    # 3. Capacity reports awrun_available and active run_ids
    cap = tc.get("/swarms/capacity?probe=false", headers=h_agent).json()
    assert cap["awrun_available"] is True
    assert run_id in cap["awrun_active_runs"]

    # 4. Pause + export via awrun.handoff.export_run
    export_dir = f16_env / "exports"
    paused = tc.post(
        f"/swarms/{sid}/pause",
        headers=h_own,
        json={"export_runs": True, "export_dir": str(export_dir)},
    )
    assert paused.status_code == 200, paused.text
    p_data = paused.json()
    assert p_data["status"] == "paused"
    assert len(p_data["exported_runs"]) == 1
    bundle_path = Path(p_data["exported_runs"][0]["bundle"])
    assert bundle_path.is_file()
    with zipfile.ZipFile(bundle_path) as zf:
        assert "run.json" in zf.namelist()

    # 5a. F16-BUG-1: Fail-closed when container isolation is requested on a host mgr
    # that cannot execute wrapped_argv inside a container
    sw2 = tc.post(
        "/swarms",
        headers=h_own,
        json={"goal": "Delegate confined", "local_room": True, "leads": ["fix-2"]},
    ).json()
    sid2 = sw2["swarm_id"]
    refused = tc.post(
        f"/swarms/{sid2}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "fix-2",
            "owns": ["src/b.py"],
            "prompt": "Fix b.py",
            "execution_mode": "awrun",
            "limits": {"cpus": 1.5, "memory_mb": 512},
            "egress": {"hosts": []},
            "isolation": {"mode": "container", "image": "aither-worker:1"},
            "wait": True,
            "timeout_s": 5.0,
        },
    )
    assert refused.status_code == 422, refused.text
    assert "unconfined" in refused.text.lower() or "container" in refused.text.lower()
    assert mgr.configs == []

    # 5b. F16-BUG-1: Genuine container_runner / create_confined path receives wrapped_argv
    # and never falls back to unconfined mgr.create()
    class _ConfinedMgrStub(_MgrStub):
        def __init__(self, state: str = "exited") -> None:
            super().__init__(state=state)
            self.confined_calls: List[Dict[str, Any]] = []

        def create_confined(
            self,
            cfg: Any,
            *,
            wrapped_argv: List[str],
            isolation: Dict[str, Any],
            limits: Any = None,
            egress: Any = None,
            run_id: str = "",
        ) -> _SessionStub:
            self.confined_calls.append(
                {
                    "cfg": cfg,
                    "wrapped_argv": list(wrapped_argv),
                    "isolation": dict(isolation),
                    "limits": limits,
                    "egress": egress,
                    "run_id": run_id,
                }
            )
            sid = f"f16_confined_{len(self.confined_calls)}"
            sess = _SessionStub(sid, state=self.state)
            self.sessions[sid] = sess
            return sess

    confined_mgr = _ConfinedMgrStub(state="exited")
    tc_conf, _ = _make_app(f16_env, mgr=confined_mgr)
    sw3 = tc_conf.post(
        "/swarms",
        headers=h_own,
        json={"goal": "Delegate confined real runner", "local_room": True, "leads": ["fix-3"]},
    ).json()
    deleg = tc_conf.post(
        f"/swarms/{sw3['swarm_id']}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "fix-3",
            "owns": ["src/b.py"],
            "prompt": "Fix b.py",
            "execution_mode": "awrun",
            "limits": {"cpus": 1.5, "memory_mb": 512},
            "egress": {"hosts": []},
            "isolation": {"mode": "container", "image": "aither-worker:1"},
            "wait": True,
            "timeout_s": 5.0,
        },
    )
    assert deleg.status_code == 200, deleg.text
    assert confined_mgr.configs == []
    assert len(confined_mgr.confined_calls) == 1
    assert "--cap-drop=ALL" in confined_mgr.confined_calls[0]["wrapped_argv"]
    assert "--network" in confined_mgr.confined_calls[0]["wrapped_argv"]
    d_data = deleg.json()
    assert d_data["run_id"].startswith("r-")
    assert "--network" in d_data["wrapped_argv"] and "none" in d_data["wrapped_argv"]
    assert "--cap-drop=ALL" in d_data["wrapped_argv"]
    finished_item = store.get(d_data["run_id"])
    assert finished_item is not None and finished_item.status == "done"


def test_awrun_zero_open_runitem_leak_across_create_delegate_done(f16_env, monkeypatch):
    if not _AWRUN_PKG.is_dir():
        pytest.skip("awrun package unavailable")
    monkeypatch.syspath_prepend(str(_AWRUN_PKG))

    from awrun.store import OPEN_STATUSES, RunStore

    mgr = _MgrStub(state="idle")
    tc, _coord = _make_app(f16_env, mgr=mgr)
    h_own = {"Authorization": "Bearer owner-tok"}

    sw = tc.post(
        "/swarms",
        headers=h_own,
        json={
            "goal": "Zero RunItem leak check",
            "local_room": True,
            "execution_mode": "awrun",
            "tasks": [{"slot_id": "fix-1", "agent": "demiurge"}],
        },
    )
    assert sw.status_code == 200, sw.text
    sw_data = sw.json()
    sid = sw_data["swarm_id"]
    initial_run_id = sw_data["members_by_slot"]["fix-1"]["run_id"]
    assert initial_run_id.startswith("r-")

    store = RunStore()
    assert store.get(initial_run_id).status == "queued"

    deleg = tc.post(
        f"/swarms/{sid}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "fix-1",
            "prompt": "Fix slot 1",
            "execution_mode": "awrun",
            "wait": True,
            "timeout_s": 2.0,
        },
    )
    assert deleg.status_code == 200, deleg.text
    d_data = deleg.json()
    assert d_data["run_id"] == initial_run_id
    all_runs = store.list(kind="agent")
    assert len(all_runs) == 1
    assert all_runs[0].id == initial_run_id
    assert all_runs[0].status == "running"

    done_resp = tc.post(
        f"/swarms/{sid}/control",
        headers=h_own,
        json={"action": "done"},
    )
    assert done_resp.status_code == 200, done_resp.text
    assert store.list(statuses=list(OPEN_STATUSES), kind="agent") == []
    assert store.get(initial_run_id).status == "done"


@contextlib.contextmanager
def _scoped_repo_awseal(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    saved = {
        name: mod
        for name, mod in list(sys.modules.items())
        if name == "awseal" or name.startswith("awseal.")
    }
    for name in list(saved):
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(str(_AWSEAL_PKG))
    try:
        yield importlib.import_module("awseal")
    finally:
        for name in [
            k for k in list(sys.modules) if k == "awseal" or k.startswith("awseal.")
        ]:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


@pytest.mark.skipif(not _AWSEAL_PKG.is_dir(), reason="awseal package unavailable")
def test_awseal_signing_fail_closed_verification_and_sha256_fallback(f16_env, monkeypatch):
    with _scoped_repo_awseal(monkeypatch):
        from awseal import keys as awseal_keys

        key_path = f16_env / "signing.key"
        awseal_keys.generate(key_path)
        pub_hex = awseal_keys.public_key_hex(awseal_keys.load_private_key(key_path))

        other_key = f16_env / "other.key"
        awseal_keys.generate(other_key)
        other_pub = awseal_keys.public_key_hex(awseal_keys.load_private_key(other_key))

        art_dir = f16_env / "track_artifacts"
        art_dir.mkdir()
        (art_dir / "output.txt").write_text("verified build output\n", encoding="utf-8")

        tc, _coord = _make_app(f16_env, mgr=_MgrStub())
        h_own = {"Authorization": "Bearer owner-tok"}
        h_agent = {"Authorization": "Bearer agent-tok"}
        sid = tc.post(
            "/swarms",
            headers=h_own,
            json={
                "goal": "awseal track verification",
                "local_room": True,
                "leads": ["fix-1"],
            },
        ).json()["swarm_id"]

        # 1. Sign on @verifier [done]
        signed = tc.post(
            f"/swarms/{sid}/post",
            headers=h_agent,
            json={
                "agent": "fix-1",
                "kind": "done",
                "text": "@verifier [done] artifact sealed",
                "artifact_dir": str(art_dir),
                "seal_artifacts": True,
                "key_path": str(key_path),
            },
        )
        assert signed.status_code == 200, signed.text
        assert (art_dir / "awseal.json").is_file()
        assert signed.json()["seal"]["ed25519"] is True
        assert signed.json()["seal"]["sealed"] is True

        # 2. Verify with expected public key succeeds
        ok_v = tc.post(
            f"/swarms/{sid}/post",
            headers=h_agent,
            json={
                "agent": "verifier",
                "kind": "update",
                "text": "verified artifact",
                "artifact_dir": str(art_dir),
                "expect_key": pub_hex,
            },
        )
        assert ok_v.status_code == 200, ok_v.text
        assert ok_v.json()["seal"]["key_trusted"] is True

        # 3. Wrong expect_key fails closed (422)
        wrong_k = tc.post(
            f"/swarms/{sid}/post",
            headers=h_agent,
            json={
                "agent": "verifier",
                "kind": "update",
                "text": "wrong key check",
                "artifact_dir": str(art_dir),
                "expect_key": other_pub,
            },
        )
        assert wrong_k.status_code == 422

        # 4. Tamper 1 byte -> fails closed (422) and names modified file
        (art_dir / "output.txt").write_text("tampered build output\n", encoding="utf-8")
        tampered = tc.post(
            f"/swarms/{sid}/post",
            headers=h_agent,
            json={
                "agent": "verifier",
                "kind": "update",
                "text": "tamper check",
                "artifact_dir": str(art_dir),
                "expect_key": pub_hex,
            },
        )
        assert tampered.status_code == 422
        assert "output.txt" in tampered.text

        # 5. Standalone fallback when awseal is blocked:
        # 5a. Fail closed (503) when awseal.json or expect_key is present
        monkeypatch.setitem(sys.modules, "awseal", None)
        blocked_verify = tc.post(
            f"/swarms/{sid}/post",
            headers=h_agent,
            json={
                "agent": "verifier",
                "kind": "update",
                "text": "must fail closed without awseal",
                "artifact_dir": str(art_dir),
                "expect_key": pub_hex,
            },
        )
        assert blocked_verify.status_code == 503

        # 5b. Honest SHA-256 fallback (sealed=False, ed25519=False) when unsealed dir
        plain_dir = f16_env / "plain_artifacts"
        plain_dir.mkdir()
        (plain_dir / "report.txt").write_text("plain digest\n", encoding="utf-8")
        fb = tc.post(
            f"/swarms/{sid}/post",
            headers=h_agent,
            json={
                "agent": "fix-1",
                "kind": "done",
                "text": "@verifier [done] plain artifacts",
                "artifact_dir": str(plain_dir),
                "seal_artifacts": True,
            },
        )
        assert fb.status_code == 200, fb.text
        assert fb.json()["seal"]["scheme"] == "sha256-fallback"
        assert fb.json()["seal"]["sealed"] is False
        assert fb.json()["seal"]["ed25519"] is False
        assert "report.txt" in fb.json()["seal"]["files"]


def test_zero_libc_awzeal_metadata_and_mcp_schemas(f16_env, monkeypatch):
    mgr = _MgrStub(state="exited")
    tc, _coord = _make_app(f16_env, mgr=mgr)
    h_own = {"Authorization": "Bearer owner-tok"}

    # Invalid inference_runtime fails 422
    bad_rt = tc.post(
        "/swarms",
        headers=h_own,
        json={"goal": "Bad runtime", "local_room": True, "inference_runtime": "bogus"},
    )
    assert bad_rt.status_code == 422

    # Shared-RO mmap D5_shared_ro + model pin metadata propagates to threads, members, brief, capacity
    monkeypatch.setitem(sys.modules, "adk.llm.awzeal", None)
    sw = tc.post(
        "/swarms",
        headers=h_own,
        json={
            "goal": "Zero-libc shared-RO pool",
            "local_room": True,
            "inference_runtime": "awzeal",
            "shared_ro_mmap": True,
            "model_path": "/models/bonsai2-27b.pq2_0",
            "model_sha256": "a" * 64,
            "cpu_pin": "0-7",
            "tasks": [{"pool": "fix", "count": 2, "agent": "demiurge"}],
        },
    )
    assert sw.status_code == 200, sw.text
    sdata = sw.json()
    m1 = sdata["members_by_slot"]["fix-1"]
    assert m1["inference_runtime"] == "awzeal"
    assert m1["shared_ro_mmap"] is True
    assert m1["shared_ro_mode"] == "D5_shared_ro"
    assert m1["model_sha256"] == "a" * 64
    assert m1["cpu_pin"] == "0-7"
    assert m1["awzeal_provider_available"] is False

    deleg = tc.post(
        f"/swarms/{sdata['swarm_id']}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "fix-1",
            "prompt": "Run shared-RO inference task",
            "inference_runtime": "asm-llm",
            "shared_ro_mmap": True,
            "model_path": "/models/qwen3.asm",
            "model_sha256": "b" * 64,
            "cpu_pin": "0-3",
            "wait": True,
            "timeout_s": 5.0,
        },
    )
    assert deleg.status_code == 200, deleg.text
    dd = deleg.json()
    assert dd["inference_runtime"] == "asm-llm"
    assert dd["shared_ro_mode"] == "D5_shared_ro"
    sent_prompt = mgr.sessions[dd["session_id"]].submitted[0]
    assert "shared_ro_mmap: true (D5_shared_ro)" in sent_prompt
    assert "inference_runtime: asm-llm" in sent_prompt
    assert "cpu_pin: 0-3" in sent_prompt

    # F16-BUG-3: Delegating fix-2 without repeating awzeal/shared_ro_mmap inherits
    # the stored member/thread configuration into both response and prompt brief
    deleg2 = tc.post(
        f"/swarms/{sdata['swarm_id']}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "fix-2",
            "prompt": "Inherit shared-RO metadata",
            "wait": True,
            "timeout_s": 5.0,
        },
    )
    assert deleg2.status_code == 200, deleg2.text
    dd2 = deleg2.json()
    assert dd2["inference_runtime"] == "awzeal"
    assert dd2["shared_ro_mmap"] is True
    assert dd2["shared_ro_mode"] == "D5_shared_ro"
    assert dd2["model_path"] == "/models/bonsai2-27b.pq2_0"
    assert dd2["model_sha256"] == "a" * 64
    assert dd2["cpu_pin"] == "0-7"
    sent_prompt2 = mgr.sessions[dd2["session_id"]].submitted[0]
    assert "inference_runtime: awzeal" in sent_prompt2
    assert "shared_ro_mmap: true (D5_shared_ro)" in sent_prompt2
    assert "model_path: /models/bonsai2-27b.pq2_0" in sent_prompt2
    assert f"model_sha256: {'a' * 64}" in sent_prompt2
    assert "cpu_pin: 0-7" in sent_prompt2

    # mcp_stdio schemas advertise F16 fields
    launch_props = mcp_stdio.BY_NAME["awsh_swarm_launch"]["schema"]["properties"]
    assert {
        "execution_mode", "limits", "egress", "isolation",
        "inference_runtime", "shared_ro_mmap", "model_path", "model_sha256", "cpu_pin",
    } <= set(launch_props)
    post_props = mcp_stdio.BY_NAME["awsh_swarm_post"]["schema"]["properties"]
    assert {"artifact_dir", "seal_artifacts", "expect_key", "key_path"} <= set(post_props)
    del_props = mcp_stdio.BY_NAME["awsh_swarm_delegate_harness"]["schema"]["properties"]
    assert {
        "execution_mode", "limits", "egress", "isolation",
        "inference_runtime", "shared_ro_mmap", "model_path", "model_sha256", "cpu_pin",
    } <= set(del_props)
    pause_props = mcp_stdio.BY_NAME["awsh_swarm_pause"]["schema"]["properties"]
    assert {"export_runs", "export_dir"} <= set(pause_props)


def test_standalone_blocked_lib_awseal_awrun_imports():
    code = (
        "import sys; "
        "sys.modules['lib'] = None; "
        "sys.modules['awseal'] = None; "
        "sys.modules['awrun'] = None; "
        "import adk.harnesses.swarm_routes, adk.harnesses.rooms, "
        "adk.harnesses.steer_dispatch, adk.harnesses.thread_router, "
        "adk.harnesses.daemon, adk.harnesses.mcp_stdio; "
        "cap = adk.harnesses.swarm_routes.local_swarm_capacity(); "
        "assert cap['awrun_available'] is False; "
        "assert cap['awseal_available'] is False; "
        "print('STANDALONE_F16_OK')"
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
    assert "STANDALONE_F16_OK" in res.stdout


def test_awseal_011_compat_manifest_fallback_and_fail_closed(f16_env, monkeypatch):
    from types import SimpleNamespace

    f1 = f16_env / "artifact.txt"
    f1.write_text("manifest artifact\n", encoding="utf-8")

    # Simulate awseal 0.1.1 (has sign/write/verify only; lacks sign_files/from_dict/verify_files)
    awseal_011 = SimpleNamespace(
        sign=lambda *a, **kw: None,
        write=lambda *a, **kw: None,
        verify=lambda *a, **kw: {"ok": True},
    )
    monkeypatch.setattr(swarm_routes, "_load_awseal", lambda: awseal_011)

    res = swarm_routes.verify_or_seal_track_artifacts(
        artifacts=[str(f1)],
        seal_artifacts=True,
        kind="done",
    )
    assert res is not None
    assert res["ok"] is True
    assert res["sealed"] is False
    assert res["ed25519"] is False
    assert res["scheme"] == "sha256-fallback"
    assert str(f1) in res["files"]

    with pytest.raises(swarm_routes.SwarmApiError) as exc_info:
        swarm_routes.verify_or_seal_track_artifacts(
            artifacts=[str(f1)],
            seal_doc={"version": 1},
            expect_key="a" * 64,
        )
    assert exc_info.value.status == 503


def test_awrun_create_swarm_partial_failure_rolls_back_runitems(f16_env, monkeypatch):
    """F16-BUG-5: partial multi-slot create_swarm failure must roll back earlier RunItems."""
    if not _AWRUN_PKG.is_dir():
        pytest.skip("awrun package unavailable")
    monkeypatch.syspath_prepend(str(_AWRUN_PKG))

    from awrun.store import OPEN_STATUSES, STATUS_FAILED, RunStore

    tc, coord = _make_app(f16_env, mgr=_MgrStub(state="idle"))
    h_own = {"Authorization": "Bearer owner-tok"}
    h_agent = {"Authorization": "Bearer agent-tok"}

    resp = tc.post(
        "/swarms",
        headers=h_own,
        json={
            "goal": "Partial multi-slot failure",
            "local_room": True,
            "execution_mode": "awrun",
            "tasks": [
                {"slot_id": "fix-1"},
                {"slot_id": "fix-2", "limits": {"cpus": 1}},
            ],
        },
    )
    assert resp.status_code == 422, resp.text

    store = RunStore()
    assert store.list(statuses=list(OPEN_STATUSES), kind="agent") == []
    closed = store.list(statuses=[STATUS_FAILED], kind="agent")
    assert len(closed) == 1
    assert closed[0].spec["swarm_lineage"]["slot_id"] == "fix-1"

    cap = tc.get("/swarms/capacity?probe=false", headers=h_agent).json()
    assert cap["awrun_active_runs"] == []
    assert coord.list_swarms()["count"] == 0

    with pytest.raises(swarm_routes.SwarmApiError) as exc_info:
        coord.create_swarm(
            {
                "goal": "Direct multi-slot failure",
                "execution_mode": "awrun",
                "tasks": [
                    {"slot_id": "fix-a"},
                    {"slot_id": "fix-b"},
                    {"slot_id": "fix-c", "egress": {"hosts": ["api.example.com"]}},
                ],
            }
        )
    assert exc_info.value.status == 422
    assert store.list(statuses=list(OPEN_STATUSES), kind="agent") == []
    assert len(store.list(statuses=[STATUS_FAILED], kind="agent")) == 3


def test_zero_libc_awzeal_explicit_standard_downgrade_persists(f16_env):
    """F16-BUG-6: explicit override back to standard/shared_ro_mmap=False persists on swarm."""
    mgr = _MgrStub(state="exited")
    tc, coord = _make_app(f16_env, mgr=mgr)
    h_own = {"Authorization": "Bearer owner-tok"}
    h_agent = {"Authorization": "Bearer agent-tok"}

    sw = tc.post(
        "/swarms",
        headers=h_own,
        json={
            "goal": "Downgrade slot back to standard",
            "local_room": True,
            "inference_runtime": "awzeal",
            "shared_ro_mmap": True,
            "model_path": "/models/b.pq2",
            "model_sha256": "d" * 64,
            "cpu_pin": "0-7",
            "tasks": [{"pool": "fix", "count": 2}],
        },
    )
    assert sw.status_code == 200, sw.text
    sid = sw.json()["swarm_id"]

    deleg = tc.post(
        f"/swarms/{sid}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "fix-2",
            "prompt": "Run on standard runtime",
            "inference_runtime": "standard",
            "shared_ro_mmap": False,
            "model_path": "",
            "model_sha256": "",
            "cpu_pin": "",
            "wait": True,
            "timeout_s": 5.0,
        },
    )
    assert deleg.status_code == 200, deleg.text
    dd = deleg.json()
    assert dd["inference_runtime"] == "standard"
    assert dd["shared_ro_mmap"] is False
    assert dd["shared_ro_mode"] == ""
    assert dd["model_path"] == ""
    assert dd["model_sha256"] == ""
    assert dd["cpu_pin"] == ""

    view = tc.get(f"/swarms/{sid}", headers=h_agent).json()
    m2 = view["members_by_slot"]["fix-2"]
    assert m2["inference_runtime"] == "standard"
    assert m2["shared_ro_mmap"] is False
    assert m2["shared_ro_mode"] == ""
    assert m2["model_path"] == ""
    assert m2["model_sha256"] == ""
    assert m2["cpu_pin"] == ""

    th2 = next(t for t in view["threads"] if t["slot_id"] == "fix-2")
    assert th2["inference_runtime"] == "standard"
    assert th2["shared_ro_mmap"] is False
    assert th2["shared_ro_mode"] == ""
    assert th2["model_path"] == ""
    assert th2["model_sha256"] == ""
    assert th2["cpu_pin"] == ""

    # Unmodified sibling slot preserves awzeal/D5_shared_ro
    m1 = coord.get_swarm(sid)["members_by_slot"]["fix-1"]
    assert m1["inference_runtime"] == "awzeal"
    assert m1["shared_ro_mmap"] is True
    assert m1["shared_ro_mode"] == "D5_shared_ro"

    # Re-delegating fix-2 without explicit overrides retains the persisted standard downgrade
    redeleg = tc.post(
        f"/swarms/{sid}/delegate-harness",
        headers=h_own,
        json={
            "harness": "claude",
            "slot_id": "fix-2",
            "prompt": "Follow-up on standard runtime",
            "wait": True,
            "timeout_s": 5.0,
        },
    )
    assert redeleg.status_code == 200, redeleg.text
    rd = redeleg.json()
    assert rd["inference_runtime"] == "standard"
    assert rd["shared_ro_mmap"] is False
    assert rd["model_path"] == ""
    assert rd["model_sha256"] == ""
    assert rd["cpu_pin"] == ""
