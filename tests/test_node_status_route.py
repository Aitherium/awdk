"""The page that registered a device can ask what it is registered as, and a
one-click registration also makes the device's sessions reachable.

* ``GET /node/status`` answers from the stored node record behind the same guard
  as ``/identity/whoami`` (loopback peer, first-party Origin). It never carries a
  credential.
* ``POST /mesh/join`` now also does what ``adk rc`` does: it mints a path-scoped
  session token and holds the reverse link, and the heartbeat advertises it.
  The daemon's root token is never advertised; a failed mint holds nothing.

Nothing here touches the network or the real home directory: the platform, the
session-token registry and the link's socket loop are all replaced.
"""

import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from adk import enrollment, fleet_enroll, mesh, node_link, rc, server
from adk.harnesses import daemon as harness_daemon

GOOD_ORIGIN = "https://aitherium.com"
LOOPBACK = ("127.0.0.1", 41234)
IDP = "https://idp.example.test"
SCOPED = "scoped-session-token"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A node record under tmp, no heartbeat, no link, no socket."""
    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    monkeypatch.setattr(fleet_enroll, "_node_link", None)
    monkeypatch.setattr(fleet_enroll, "_node_link_task", None)
    monkeypatch.setattr(enrollment, "_heartbeat_task", None)
    monkeypatch.setattr(enrollment, "_heartbeat_state", enrollment._new_heartbeat_state())
    monkeypatch.setattr(rc, "_held_readiness", None)
    for var in ("AITHER_BROWSER_HANDOFF", "AITHER_HARNESS_LINK", "AITHER_HARNESS_URL",
                "AITHER_HARNESS_PORT", "AITHER_IDP_URL", "AITHER_IDP_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    async def _parked(self, **kw):  # the link's socket loop: never dials
        await asyncio.sleep(3600)

    monkeypatch.setattr(node_link.NodeLink, "run", _parked)
    yield


@pytest.fixture()
def harness(monkeypatch):
    """The session daemon's token registry and probe, faked and recorded."""
    h = MagicMock()
    h.minted = []
    h.probes = []
    h.ready = True

    def _mint(principal, **kw):
        h.minted.append({"principal": principal, **kw})
        return SCOPED

    def _probe(base, token="", timeout=3.0):
        h.probes.append({"base": base, "token": token})
        return h.ready, "1 session(s)"

    monkeypatch.setattr(harness_daemon, "mint_scoped_token", _mint)
    monkeypatch.setattr(rc, "probe_harness", _probe)
    return h


def _write_record(tmp_path, **over):
    rec = {"node_id": "node-abc", "mode": "rich", "enroll_base": IDP,
           "tenant_id": "ten-1", "inference_url": "", "node_class": "laptop",
           "enrolled_at": "2026-10-02T10:00:00Z", "overlay_ip": "100.64.0.7",
           "api_key": "node-secret-key"}
    rec.update(over)
    (tmp_path / "node_auth.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _app():
    from adk.config import Config

    config = Config()
    config.gateway_url = ""
    config.aither_api_key = ""
    agent = MagicMock()
    agent.name = "test"
    agent.llm = MagicMock()
    agent.llm.provider_name = "test"
    agent._identity = MagicMock()
    agent._identity.name = "test"
    agent._identity.description = "Test"
    agent._identity.skills = []
    agent._tools = MagicMock()
    agent._tools.list_tools = MagicMock(return_value=[])
    agent._safety = None
    return server.create_app(agent=agent, identity="test", config=config)


def _get(origin=GOOD_ORIGIN, client=LOOPBACK):
    headers = {"Origin": origin} if origin else {}
    return TestClient(_app(), client=client).get("/node/status", headers=headers)


class TestNodeStatusRoute:
    def test_reflects_the_stored_node_record(self, tmp_path):
        _write_record(tmp_path)
        resp = _get()
        assert resp.status_code == 200
        body = resp.json()
        assert body["registered"] is True
        assert body["node_id"] == "node-abc"
        assert body["tenant_id"] == "ten-1"
        assert body["enrolled_at"] == "2026-10-02T10:00:00Z"
        assert body["overlay_ip"] == "100.64.0.7"
        assert body["heartbeat"] == {
            "running": False, "online": False, "age_seconds": None, "last_result": "never",
        }
        assert body["harness_link"]["held"] is False

    def test_never_carries_a_credential(self, tmp_path):
        _write_record(tmp_path)
        text = _get().text
        assert "node-secret-key" not in text
        assert "api_key" not in text and "token" not in text

    def test_an_unregistered_device_says_so(self):
        resp = _get()
        assert resp.status_code == 200
        assert resp.json() == {"registered": False}

    def test_a_legacy_record_is_not_an_identity_registration(self, tmp_path):
        _write_record(tmp_path, mode="legacy")
        assert _get().json() == {"registered": False}

    @pytest.mark.parametrize("origin", [
        "https://example.com",
        "https://aitherium.com.evil.example",
        "https://a.b.aitherium.com",
        "http://aitherium.com",
        "",
    ])
    def test_refused_for_a_foreign_origin(self, tmp_path, origin):
        _write_record(tmp_path)
        resp = _get(origin=origin)
        assert resp.status_code == 403
        assert "node-abc" not in resp.text

    def test_refused_for_a_peer_that_is_not_loopback(self, tmp_path):
        _write_record(tmp_path)
        resp = _get(client=("203.0.113.9", 5555))
        assert resp.status_code == 403
        assert "node-abc" not in resp.text

    def test_the_kill_switch_removes_it(self, tmp_path, monkeypatch):
        _write_record(tmp_path)
        monkeypatch.setenv("AITHER_BROWSER_HANDOFF", "0")
        assert _get().status_code == 404

    def test_the_heartbeat_age_is_this_nodes(self, tmp_path, monkeypatch):
        _write_record(tmp_path)

        async def _park(*a, **k):
            await asyncio.sleep(3600)

        monkeypatch.setattr(enrollment, "heartbeat_loop", _park)

        async def run(node_id):
            enrollment.start_heartbeat(IDP, "t", node_id, registered=True)
            transport = httpx.ASGITransport(app=_app(), client=LOOPBACK)
            async with httpx.AsyncClient(transport=transport, base_url="http://d") as c:
                r = await c.get("/node/status", headers={"Origin": GOOD_ORIGIN})
            await enrollment.stop_heartbeat()
            return r.json()["heartbeat"]

        mine = asyncio.run(run("node-abc"))
        assert mine["running"] is True and mine["online"] is True
        assert mine["age_seconds"] is not None and mine["last_result"] == "registered"
        # A beat running for some OTHER node id is not this device being online.
        other = asyncio.run(run("someone-else"))
        assert other == {
            "running": False, "online": False, "age_seconds": None, "last_result": "never",
        }


class TestHoldHarnessLink:
    def test_holds_a_scoped_link_and_reports_it(self, harness):
        async def run():
            res = await rc.hold_harness_link("node-abc", "user-token")
            return res, rc.harness_link_status(), fleet_enroll.active_node_link()

        res, status, link = asyncio.run(run())
        assert res["held"] is True and res["harness_ready"] is True
        assert harness.minted[0]["principal"] == "node:node-abc"
        # The link forwards with the SCOPED token, to the loopback session daemon.
        assert link.harness_token == SCOPED
        assert link.harness_url == "http://127.0.0.1:8362"
        assert link.node_id == "node-abc" and link.token == "user-token"
        assert harness.probes[0] == {"base": "http://127.0.0.1:8362", "token": SCOPED}
        assert status == {"held": True, "reach": "none", "state": "starting",
                          "harness_ready": True}
        assert res["reach_provider"]() == "none"

    def test_a_failed_mint_advertises_nothing(self, harness, monkeypatch):
        def _boom(*a, **k):
            raise OSError("registry not writable")

        monkeypatch.setattr(harness_daemon, "mint_scoped_token", _boom)

        async def run():
            res = await rc.hold_harness_link("node-abc", "user-token")
            return res, rc.harness_link_status(), fleet_enroll.active_node_link()

        res, status, link = asyncio.run(run())
        assert res["held"] is False and res["code"] == "token_mint_failed"
        assert link is None and status["held"] is False

    def test_the_switch_holds_nothing(self, harness, monkeypatch):
        monkeypatch.setenv("AITHER_HARNESS_LINK", "0")
        res = asyncio.run(rc.hold_harness_link("node-abc", "user-token"))
        assert res["held"] is False and res["code"] == "disabled"
        assert harness.minted == [] and fleet_enroll.active_node_link() is None

    def test_a_session_daemon_that_is_down_is_reported_not_ready(self, harness):
        harness.ready = False

        async def run():
            res = await rc.hold_harness_link("node-abc", "user-token")
            url, ready = res["harness_provider"]()
            return res, url, ready

        res, url, ready = asyncio.run(run())
        # The link is still held (the daemon may come up later); the beat says
        # the sessions are not reachable yet instead of claiming they are.
        assert res["held"] is True and res["harness_ready"] is False
        assert url == "http://127.0.0.1:8362" and ready is False

    def test_an_existing_link_gains_the_harness(self, harness):
        async def run():
            first = fleet_enroll._start_node_link("node-abc", "old-token")
            assert rc.harness_link_status()["held"] is False
            await rc.hold_harness_link("node-abc", "new-token")
            return first, fleet_enroll.active_node_link(), rc.harness_link_status()

        first, link, status = asyncio.run(run())
        assert link is first
        assert link.harness_token == SCOPED and link.token == "new-token"
        assert status["held"] is True

    def test_status_is_not_held_once_the_link_task_is_gone(self, harness):
        async def run():
            await rc.hold_harness_link("node-abc", "user-token")

        asyncio.run(run())  # the loop closed: nothing is driving the link
        assert rc.harness_link_status()["held"] is False


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {} if body is None else body
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.fixture()
def join(monkeypatch, harness):
    """A /mesh/join harness: signed in, every outbound call faked and counted."""
    import adk.auth as auth

    class _Store:
        def get_active_profile(self):
            return {"access_token": "user-token", "endpoint": IDP}

    monkeypatch.setattr(auth, "AuthStore", _Store)
    monkeypatch.setattr(auth, "resolve_credentials",
                        lambda store=None: MagicMock(is_expired=False))

    h = MagicMock()
    h.harness = harness
    h.key_response = _Resp(200, {"mesh_key": "hskey"})

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            return h.key_response

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    h.enroll_result = {
        "enrolled": True, "node_id": "n", "tenant_id": "ten-1", "bearer_token": "node-cap",
        "registration": {"node_class": "laptop", "inference_url": "http://127.0.0.1:8080"},
    }

    async def _rich_enroll(base, token, node_id, **kw):
        return h.enroll_result

    async def _join(*a, **kw):
        return {"overlay_ip": "10.77.0.9", "transport": "headscale"}

    h.beats = []
    monkeypatch.setattr(
        enrollment, "start_heartbeat",
        lambda base, token, node_id, **kw: h.beats.append(
            {"base": base, "token": token, "node_id": node_id, **kw}) or True,
    )
    monkeypatch.setattr(enrollment, "rich_enroll", _rich_enroll)
    monkeypatch.setattr(mesh, "join", _join)
    monkeypatch.setattr(mesh, "_tailscale", lambda: "/usr/bin/tailscale")
    h.post = lambda: TestClient(_app(), client=LOOPBACK).post(
        "/mesh/join", headers={"Origin": GOOD_ORIGIN}, json={})
    return h


class TestJoinHoldsTheHarnessLink:
    def test_a_join_holds_the_link_and_advertises_it(self, join, tmp_path):
        resp = join.post()
        body = resp.json()
        assert resp.status_code == 200 and body["registered"] is True
        assert body["harness_link"] == {
            "held": True, "code": "", "detail": "", "harness_ready": True,
        }
        steps = {s["step"]: s for s in body["steps"]}
        assert steps["harness_link"]["ok"] is True

        link = fleet_enroll.active_node_link()
        assert link.node_id == body["node_id"]
        assert link.harness_token == SCOPED
        assert link.inference_url == "http://127.0.0.1:8080"
        # The beat that advertises the link: this node, live reach, live readiness.
        assert len(join.beats) == 1
        beat = join.beats[0]
        assert beat["base"] == IDP and beat["node_id"] == body["node_id"]
        assert beat["registered"] is True
        assert beat["reach_provider"]() == "none"
        assert beat["harness_provider"]()[0] == "http://127.0.0.1:8362"
        assert beat["token_provider"] is server._active_access_token
        # The next daemon start re-holds it, and the overlay address is kept.
        rec = json.loads((tmp_path / "node_auth.json").read_text(encoding="utf-8"))
        assert rec["harness_link"] is True and rec["overlay_ip"] == "10.77.0.9"

    def test_the_response_never_carries_the_session_token(self, join):
        text = join.post().text
        assert SCOPED not in text and "user-token" not in text

    def test_a_join_without_tailscale_still_holds_the_link(self, join, monkeypatch):
        monkeypatch.setattr(mesh, "_tailscale", lambda: None)
        body = join.post().json()
        assert body["overlay"]["code"] == "tailscale_missing"
        assert body["harness_link"]["held"] is True

    def test_a_failed_mint_still_registers_the_device(self, join, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise OSError("registry not writable")

        monkeypatch.setattr(harness_daemon, "mint_scoped_token", _boom)
        resp = join.post()
        body = resp.json()
        assert resp.status_code == 200 and body["registered"] is True
        assert body["harness_link"]["held"] is False
        assert body["harness_link"]["code"] == "token_mint_failed"
        assert join.beats == []  # the registration's own heartbeat is left alone
        rec = json.loads((tmp_path / "node_auth.json").read_text(encoding="utf-8"))
        assert rec["harness_link"] is False

    def test_a_refused_registration_holds_nothing(self, join):
        join.enroll_result = {
            "enrolled": False, "http_status": 402,
            "body": json.dumps({"detail": {"error": "subscription_required"}}),
        }
        resp = join.post()
        assert resp.status_code == 402
        assert resp.json()["code"] == "subscription_required"
        assert join.harness.minted == [] and fleet_enroll.active_node_link() is None


class TestResumeOnDaemonStart:
    def _signed_in(self, monkeypatch):
        monkeypatch.setattr(server, "_active_access_token", lambda: "user-token")

    def test_a_device_registered_with_the_link_re_holds_it(self, harness, tmp_path, monkeypatch):
        _write_record(tmp_path, harness_link=True, inference_url="http://127.0.0.1:8080")
        self._signed_in(monkeypatch)
        beats = []
        monkeypatch.setattr(
            enrollment, "start_heartbeat",
            lambda base, token, node_id, **kw: beats.append({"node_id": node_id, **kw}) or True,
        )
        res = asyncio.run(server.resume_node_harness_link())
        assert res["held"] is True
        assert beats[0]["node_id"] == "node-abc"
        # Nothing told the platform this node is back: beat at once, claim nothing.
        assert beats[0]["registered"] is False and beats[0]["beat_immediately"] is True

    def test_a_device_registered_without_it_holds_nothing(self, harness, tmp_path, monkeypatch):
        _write_record(tmp_path)
        self._signed_in(monkeypatch)
        res = asyncio.run(server.resume_node_harness_link())
        assert res["held"] is False and res["code"] == "not_requested"
        assert harness.minted == []

    def test_no_sign_in_holds_nothing(self, harness, tmp_path, monkeypatch):
        _write_record(tmp_path, harness_link=True)
        monkeypatch.setattr(server, "_active_access_token", lambda: "")
        res = asyncio.run(server.resume_node_harness_link())
        assert res["held"] is False and res["code"] == "not_signed_in"
        assert harness.minted == []
