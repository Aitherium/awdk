"""`adk mesh ls` and the peer id (measured 2026-11-12).

* ``adk mesh ls`` answered 401: it sent no login session, to a host that has no peer
  directory. It now reads the account's node registry with the login session, and
  the overlay directory keeps the PSK and never sees a person's bearer.
* ``adk volunteer status`` said "Run 'adk mesh onboard' first": a command a signed-in
  person cannot use, about a peer id no flow ever saved.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from adk import mesh_directory as md
from adk import node_join_path as njp

IDP = "https://idp.aitherium.com"
PSK = {"Content-Type": "application/json", "X-Mesh-PSK": "psk", "Authorization": "Bearer psk"}
CANDS = [("stale-token", "portal.token"), ("live-token", "adk login session")]


class _Resp:
    def __init__(self, status, body=None, ctype="application/json"):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": ctype}

    def json(self):
        return self._body


def _getter(script):
    """``script`` maps a bearer (or "psk") to a response; records every call."""
    calls = []

    async def get(url, headers):
        calls.append((url, dict(headers)))
        key = headers.get("Authorization", "").replace("Bearer ", "")
        return script.get(key, _Resp(401, {"error": "Missing bearer token"}))

    return get, calls


def _run(coro):
    return asyncio.run(coro)


def test_the_public_default_reads_the_account_registry_with_the_login_session():
    get, calls = _getter({"live-token": _Resp(200, {"endpoints": [
        {"node_id": "laptop-1", "node_class": "laptop", "inference_kind": "llama-server"},
        {"node_id": "phone-1", "node_class": "phone", "inference_kind": "none"}]})})
    out = _run(md.fetch("https://gateway.aitherium.com", PSK, get,
                        candidates=CANDS, identity_base=IDP))
    assert out["ok"] is True and out["auth"] == "adk login session"
    assert out["url"] == IDP + "/v1/nodes" and "Identity /v1/nodes" in out["registry"]
    assert [n["node_id"] for n in out["nodes"]] == ["laptop-1", "phone-1"]
    assert out["nodes"][0]["role"] == "laptop" and out["nodes"][0]["services"] == ["llama-server"]
    assert out["nodes"][1]["services"] == []
    # the stale credential was refused, the live one was tried next; the PSK never left
    assert [c[1]["Authorization"] for c in calls] == ["Bearer stale-token", "Bearer live-token"]
    assert all("X-Mesh-PSK" not in c[1] for c in calls)
    assert all(c[0].startswith(IDP) for c in calls)        # nothing sent to the gateway host


def test_signed_out_says_adk_login_and_sends_nothing():
    get, calls = _getter({})
    out = _run(md.fetch("https://gateway.aitherium.com", PSK, get,
                        candidates=[], identity_base=IDP))
    assert out["ok"] is False and "adk login" in out["error"] and calls == []


def test_every_credential_refused_names_what_was_tried():
    get, calls = _getter({})
    out = _run(md.fetch("https://gateway.aitherium.com", PSK, get,
                        candidates=CANDS, identity_base=IDP))
    assert out["ok"] is False and len(calls) == 2
    assert "HTTP 401" in out["error"] and "portal.token, adk login session" in out["error"]
    assert "adk login" in out["error"]
    assert "stale-token" not in out["error"] and "live-token" not in out["error"]


def test_an_overlay_directory_keeps_the_psk_and_never_sees_a_bearer():
    get, calls = _getter({"psk": _Resp(200, [{"node_id": "n1", "overlay_ip": "10.77.0.2"}])})
    out = _run(md.fetch("https://10.77.0.1:8125/", PSK, get,
                        candidates=CANDS, identity_base=IDP))
    assert out["ok"] is True and out["nodes"] == [{"node_id": "n1", "overlay_ip": "10.77.0.2"}]
    assert calls == [("https://10.77.0.1:8125/nodes", PSK)]
    assert "mesh-core" in out["registry"]


def test_an_overlay_refusal_is_explained_and_a_500_is_not_called_auth():
    get, _ = _getter({})
    out = _run(md.fetch("https://10.77.0.1:8125", {"Content-Type": "application/json"}, get))
    assert out["ok"] is False and "AITHER_MESH_PSK" in out["error"]
    get500, _ = _getter({"live-token": _Resp(500, None)})
    out = _run(md.fetch("https://gateway.aitherium.com", PSK, get500,
                        candidates=CANDS[1:], identity_base=IDP))
    assert out["ok"] is False and out["error"] == "the node directory answered HTTP 500"


def test_a_foreign_identity_base_gets_one_credential_only(monkeypatch):
    from adk import mcp_setup

    monkeypatch.setattr(mcp_setup, "auth_candidates", lambda: list(CANDS))
    p = md.plan("https://gateway.aitherium.com", PSK, identity_base="https://idp.example.org")
    assert len(p["attempts"]) == 1                       # never the whole local store
    p = md.plan("https://gateway.aitherium.com", PSK, identity_base=IDP)
    assert len(p["attempts"]) == 2


# ── the peer id ─────────────────────────────────────────────────────────────

def test_the_join_keeps_the_peer_id_the_conductor_names(tmp_path):
    auth = tmp_path / "node_auth.json"
    auth.write_text(json.dumps({"node_id": "adk-1", "mode": "rich", "tenant_id": "t1"}),
                    encoding="utf-8")
    saved = njp.remember_peer_id({"overlay_ip": "10.77.0.9",
                                  "strata_peer": "peer-abc123def456"}, auth)
    assert saved == "peer-abc123def456"
    data = json.loads(auth.read_text(encoding="utf-8"))
    assert data == {"node_id": "adk-1", "mode": "rich", "tenant_id": "t1",
                    "peer_id": "peer-abc123def456"}      # nothing else touched


@pytest.mark.parametrize("resp", [
    {}, {"strata_peer": None}, {"strata_peer": "peer-t1-adk-1-deferred"},
    {"strata_peer": "adk-1"}, {"peer_id": ""},
])
def test_a_missing_or_deferred_peer_is_not_saved(tmp_path, resp):
    auth = tmp_path / "node_auth.json"
    assert njp.remember_peer_id(resp, auth) == ""
    assert not auth.exists()


def test_an_unreadable_node_auth_is_left_alone(tmp_path):
    auth = tmp_path / "node_auth.json"
    auth.write_text("{not json", encoding="utf-8")
    assert njp.remember_peer_id({"strata_peer": "peer-abc123def456"}, auth) == ""
    assert auth.read_text(encoding="utf-8") == "{not json"


def test_mesh_join_calls_remember_peer_id():
    src = (Path(md.__file__).parent / "mesh.py").read_text(encoding="utf-8")
    onboard_at = src.index("resp = await onboard(conductor_url")
    assert "remember_peer_id(resp)" in src[onboard_at:onboard_at + 900]


def test_the_peer_id_error_says_what_it_saw_and_the_commands(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PEER_ID", raising=False)
    auth = tmp_path / "node_auth.json"
    msg = njp.peer_id_problem(auth)
    assert msg.startswith("Cannot resolve peer_id")
    assert "does not exist" in msg and "AITHER_PEER_ID (unset)" in msg
    assert "adk login && adk mesh join" in msg and "adk mesh onboard" not in msg
    assert njp.OWNER_JOIN_COMMAND in msg
    auth.write_text(json.dumps({"node_id": "adk-1"}), encoding="utf-8")
    assert "has node_id adk-1 but no peer_id" in njp.peer_id_problem(auth)


def test_both_resolvers_raise_the_same_message(tmp_path, monkeypatch):
    from adk import mesh_provider
    from adk.commands import volunteer

    monkeypatch.delenv("AITHER_PEER_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    seen = []
    for mod in (volunteer, mesh_provider):
        with pytest.raises(RuntimeError, match="Cannot resolve peer_id") as exc:
            asyncio.run(mod._resolve_peer_id())
        seen.append(str(exc.value))
    assert seen[0] == seen[1] and "adk mesh join" in seen[0]
    (tmp_path / ".aither").mkdir()
    (tmp_path / ".aither" / "node_auth.json").write_text(
        json.dumps({"peer_id": "peer-abc123def456"}), encoding="utf-8")
    assert asyncio.run(volunteer._resolve_peer_id()) == "peer-abc123def456"


def test_the_help_block_marks_one_human_step():
    text = njp.join_help()
    assert text.count("[HUMAN: owner]") == 1
    assert njp.MACHINE_COMMAND in text and njp.OWNER_JOIN_COMMAND in text
    assert njp.STATUS_COMMAND in text
