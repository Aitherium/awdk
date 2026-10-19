"""harness2 is a registered harness, and only an entitled caller may see or start it.

The spec half is pure data. The gate half runs the real daemon app under an
in-process TestClient with a principals registry holding two callers -- one with
the `harness2` entitlement, one without -- and asserts both the picker (GET
/harnesses) and the start path (POST /sessions) refuse the unentitled one. The
harness PROCESS is never started.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from adk.harnesses.registry import SPECS, LaunchSpec, Transport

OWNER = "owner-bearer-for-tests-only"
LICENSED = "licensed-caller-token"
UNLICENSED = "unlicensed-caller-token"


def test_harness2_is_registered_one_shot_and_entitlement_gated():
    spec = SPECS["harness2"]
    assert spec.transport is Transport.ONESHOT_PER_TURN
    assert spec.requires_entitlement == "harness2"
    assert spec.prompt_carries_context is True
    assert spec.to_dict()["requires_entitlement"] == "harness2"
    # Never upstream's Vertex-bound CLI: the wrapper runs the local adapter.
    assert spec.binary == "harness2-aither"


def test_harness2_argv_is_one_executor_turn():
    spec = SPECS["harness2"]
    assert spec.argv(LaunchSpec(prompt="rate this")) == [
        "harness2-aither", "turn", "--prompt", "rate this",
    ]
    assert spec.argv(LaunchSpec(prompt="p", model="aither-orchestrator")) == [
        "harness2-aither", "--executor-model", "aither-orchestrator", "turn", "--prompt", "p",
    ]


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    for env, sub in (
        ("AITHER_DECISIONS_DIR", "decisions"),
        ("AITHER_STEER_DIR", "steer"),
        ("AITHER_HARNESS_ROOMS_ROOT", "rooms"),
        ("AITHER_HARNESS_ROOT", "sessions"),
    ):
        monkeypatch.setenv(env, str(tmp_path / sub))
    monkeypatch.setenv("AITHER_STEER_DISPATCH_STATUS", str(tmp_path / "dispatch.json"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", OWNER)
    registry = tmp_path / "harness_tokens.json"
    registry.write_text(json.dumps({
        _sha(LICENSED): {"principal": "t:licensed", "plan": "pro",
                         "entitlements": ["harness2"]},
        _sha(UNLICENSED): {"principal": "t:unlicensed", "plan": "pro",
                           "entitlements": ["atlas"]},
    }), encoding="utf-8")
    monkeypatch.setenv("AITHER_HARNESS_PRINCIPALS", str(registry))

    import adk.harnesses.daemon as daemon
    from adk.harnesses import session as session_mod
    from adk.harnesses.manager import SessionManager

    monkeypatch.setattr(daemon, "PRINCIPALS_PATH", registry)
    monkeypatch.setattr(session_mod.HarnessSession, "start", lambda self: None)
    mgr = SessionManager(root=tmp_path / "sessions")
    return TestClient(daemon.create_app(manager=mgr, token=OWNER))


def _ids(resp) -> set:
    assert resp.status_code == 200, resp.text
    return {r["id"] for r in resp.json()["harnesses"]}


def test_picker_hides_harness2_from_an_unentitled_caller(client):
    hdr = {"Authorization": f"Bearer {UNLICENSED}"}
    assert "harness2" not in _ids(client.get("/harnesses", headers=hdr))
    assert "harness2" in _ids(client.get("/harnesses",
                                         headers={"Authorization": f"Bearer {LICENSED}"}))


def test_unentitled_caller_cannot_start_harness2_by_naming_it(client):
    resp = client.post(
        "/sessions",
        json={"harness": "harness2", "cwd": ""},
        headers={"Authorization": f"Bearer {UNLICENSED}"},
    )
    assert resp.status_code == 403
    assert "harness2" in resp.json()["detail"]
