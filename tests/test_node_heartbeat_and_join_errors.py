"""A registered device stays online, and a refused join says why.

Two failures the owner sees from the desktop's "Register this device":

* The device appears under the account and then sits there offline. The
  heartbeat lived only in the process that registered it, so after the daemon's
  next start nothing was beating. The daemon now resumes it from the stored node
  record -- and ONLY when that record exists: a device that was never registered
  must not start phoning anyone.
* A join that the platform refused (HTTP 402: no plan, or the device limit) or
  that could never work (no ``tailscale`` binary) came back as a generic 502.

Nothing here touches the network: ``httpx.AsyncClient`` is replaced, and the
heartbeat loop is a stub wherever the test is about WHETHER it starts.
"""

import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from adk import enrollment, fleet_enroll, mesh, server

GOOD_ORIGIN = "https://aitherium.com"
LOOPBACK = ("127.0.0.1", 41234)
IDP = "https://idp.example.test"


@pytest.fixture(autouse=True)
def _clean_heartbeat(tmp_path, monkeypatch):
    """Fresh heartbeat state and a node record under tmp, never the real home."""
    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    monkeypatch.setattr(enrollment, "_heartbeat_task", None)
    monkeypatch.setattr(enrollment, "_heartbeat_state", enrollment._new_heartbeat_state())
    monkeypatch.delenv("AITHER_BROWSER_HANDOFF", raising=False)
    monkeypatch.delenv("AITHER_IDP_URL", raising=False)
    monkeypatch.delenv("AITHER_IDP_BASE_URL", raising=False)
    yield


def _write_record(tmp_path, **over):
    rec = {"node_id": "node-abc", "mode": "rich", "enroll_base": IDP,
           "inference_url": "", "node_class": "laptop"}
    rec.update(over)
    (tmp_path / "node_auth.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def _stub_loop(monkeypatch):
    """Replace the beat loop with one that records its arguments and parks."""
    calls = []

    async def _loop(base_url, token, node_id, **kw):
        calls.append({"base_url": base_url, "token": token, "node_id": node_id, **kw})
        await asyncio.sleep(3600)

    monkeypatch.setattr(enrollment, "heartbeat_loop", _loop)
    return calls


def _signed_in(monkeypatch, token="user-token"):
    monkeypatch.setattr(server, "_active_access_token", lambda: token)


class TestResumeOnDaemonStart:
    def test_starts_when_the_node_record_exists(self, tmp_path, monkeypatch):
        _write_record(tmp_path)
        calls = _stub_loop(monkeypatch)
        _signed_in(monkeypatch)

        async def run():
            out = server.resume_node_heartbeat()
            await asyncio.sleep(0)  # let the task take its first step
            status = enrollment.heartbeat_status()
            await enrollment.stop_heartbeat()
            return out, status

        out, status = asyncio.run(run())
        assert out == {"started": True, "reason": "", "node_id": "node-abc"}
        assert len(calls) == 1
        assert calls[0]["base_url"] == IDP
        assert calls[0]["token"] == "user-token"
        assert calls[0]["node_id"] == "node-abc"
        # Nothing told the platform this node is back, so the first beat is now.
        assert calls[0]["beat_immediately"] is True
        assert status["running"] is True
        # ...but no beat has been ACCEPTED yet, so it must not claim to be online.
        assert status["online"] is False

    def test_does_not_start_when_the_record_is_absent(self, tmp_path, monkeypatch):
        calls = _stub_loop(monkeypatch)
        _signed_in(monkeypatch)

        async def run():
            out = server.resume_node_heartbeat()
            await asyncio.sleep(0)
            return out, enrollment.heartbeat_status()

        out, status = asyncio.run(run())
        assert out["started"] is False
        assert out["reason"] == "not registered"
        assert calls == []
        assert status["running"] is False
        assert status["not_started_reason"] == "not registered"

    def test_does_not_start_without_a_sign_in(self, tmp_path, monkeypatch):
        _write_record(tmp_path)
        calls = _stub_loop(monkeypatch)
        _signed_in(monkeypatch, token="")

        async def run():
            return server.resume_node_heartbeat(api_key="")

        out = asyncio.run(run())
        assert out["started"] is False
        assert out["reason"] == "no sign-in on this device"
        assert calls == []

    def test_a_legacy_record_is_left_to_its_own_loop(self, tmp_path, monkeypatch):
        _write_record(tmp_path, mode="federation")
        calls = _stub_loop(monkeypatch)
        _signed_in(monkeypatch)

        async def run():
            return server.resume_node_heartbeat()

        assert asyncio.run(run())["started"] is False
        assert calls == []

    def test_a_second_start_replaces_the_first_loop(self, monkeypatch):
        calls = _stub_loop(monkeypatch)

        async def run():
            enrollment.start_heartbeat(IDP, "t1", "n1")
            first = enrollment._heartbeat_task
            await asyncio.sleep(0)
            enrollment.start_heartbeat(IDP, "t2", "n1")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            cancelled = first.cancelled()
            await enrollment.stop_heartbeat()
            return cancelled

        assert asyncio.run(run()) is True, "two loops beating for one node"
        assert [c["token"] for c in calls] == ["t1", "t2"]


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {"status": "ok"} if body is None else body
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _fake_async_client(monkeypatch, responder):
    posts = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            posts.append({"url": url, **k})
            return responder(url)

        async def aclose(self):
            return None

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return posts


def _fake_registration(monkeypatch):
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **k: {
        "inference_ready": False, "available_models": [], "gpu_vram_mb": 0,
        "inference_url": "", "inference_kind": "none",
    })


class TestBeatOutcomeIsRecorded:
    def test_an_accepted_beat_makes_the_node_online(self, monkeypatch):
        posts = _fake_async_client(monkeypatch, lambda url: _Resp(200))
        _fake_registration(monkeypatch)

        async def run():
            enrollment.start_heartbeat(IDP, "t", "n", interval=0, max_beats=2,
                                       beat_immediately=True)
            task = enrollment._heartbeat_task
            await asyncio.wait_for(task, timeout=5)
            return enrollment.heartbeat_status()

        status = asyncio.run(run())
        assert [p["url"] for p in posts] == [f"{IDP}/v1/nodes/heartbeat"] * 2
        assert status["beats"] == 2
        assert status["last_status"] == 200
        assert status["last_result"] == "ok"
        assert status["age_seconds"] is not None and status["age_seconds"] < 5
        assert status["consecutive_failures"] == 0

    def test_a_refused_beat_is_visible(self, monkeypatch):
        _fake_async_client(monkeypatch, lambda url: _Resp(401, {"detail": "expired"}))
        _fake_registration(monkeypatch)

        async def run():
            enrollment.start_heartbeat(IDP, "t", "n", interval=0, max_beats=3,
                                       beat_immediately=True)
            await asyncio.wait_for(enrollment._heartbeat_task, timeout=5)
            return enrollment.heartbeat_status()

        status = asyncio.run(run())
        assert status["last_status"] == 401
        assert status["last_result"] == "refused"
        assert status["consecutive_failures"] == 3
        assert status["age_seconds"] is None
        assert status["online"] is False

    def test_the_token_is_re_read_every_beat(self, monkeypatch):
        posts = _fake_async_client(monkeypatch, lambda url: _Resp(200))
        _fake_registration(monkeypatch)
        tokens = iter(["fresh-1", "", "fresh-3"])

        async def run():
            await enrollment.heartbeat_loop(
                IDP, "stale", "n", interval=0, max_beats=3,
                token_provider=lambda: next(tokens),
            )

        asyncio.run(run())
        sent = [p["headers"]["Authorization"] for p in posts]
        # An empty answer keeps the bearer the loop already had.
        assert sent == ["Bearer fresh-1", "Bearer fresh-1", "Bearer fresh-3"]


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


class TestHealthReportsTheHeartbeat:
    def test_health_carries_the_heartbeat_state(self, monkeypatch):
        st = enrollment._new_heartbeat_state()
        st.update({"node_id": "node-abc", "interval": 60, "beats": 4,
                   "last_status": 401, "last_result": "refused",
                   "last_error": "heartbeat answered HTTP 401",
                   "consecutive_failures": 4})
        monkeypatch.setattr(enrollment, "_heartbeat_state", st)

        body = TestClient(_app()).get("/health").json()
        hb = body["node_heartbeat"]
        assert hb["node_id"] == "node-abc"
        assert hb["running"] is False
        assert hb["online"] is False
        assert hb["last_status"] == 401
        assert hb["last_result"] == "refused"
        assert hb["consecutive_failures"] == 4
        assert hb["age_seconds"] is None

    def test_health_says_why_no_heartbeat_is_running(self, monkeypatch):
        _signed_in(monkeypatch)
        server.resume_node_heartbeat()  # no record on disk
        hb = TestClient(_app()).get("/health").json()["node_heartbeat"]
        assert hb["running"] is False
        assert hb["not_started_reason"] == "not registered"

    def test_health_never_carries_the_bearer(self, monkeypatch):
        calls = _stub_loop(monkeypatch)

        async def run():
            enrollment.start_heartbeat(IDP, "super-secret-bearer", "n")
            await asyncio.sleep(0)
            status = enrollment.heartbeat_status()
            await enrollment.stop_heartbeat()
            return status

        assert "super-secret-bearer" not in json.dumps(asyncio.run(run()))
        assert calls


class TestClassifyRefusal:
    def test_the_device_limit_is_its_own_code(self):
        body = json.dumps({"detail": {
            "error": "device_quota_exceeded", "current": 3, "limit": 3,
            "hint": "You have reached your device limit (3).",
            "upgrade": {"portal_url": "https://example.test/billing"},
        }})
        out = enrollment.classify_refusal(402, body)
        assert out["code"] == "device_quota_exceeded"
        assert (out["current"], out["limit"]) == (3, 3)
        assert out["upgrade"] == {"portal_url": "https://example.test/billing"}

    def test_a_bare_402_is_still_a_distinct_result(self):
        assert enrollment.classify_refusal(402, "not json")["code"] == "payment_required"

    def test_other_failures_are_not_refusals(self):
        assert enrollment.classify_refusal(500, '{"detail": "boom"}') is None
        assert enrollment.classify_refusal(403, '{"detail": "no tenant"}') is None
        assert enrollment.classify_refusal(None, "") is None


@pytest.fixture()
def join(monkeypatch):
    """A /mesh/join harness: signed in, every outbound call faked and counted."""
    import adk.auth as auth

    class _Store:
        def get_active_profile(self):
            return {"access_token": "user-token", "endpoint": IDP}

    monkeypatch.setattr(auth, "AuthStore", _Store)
    monkeypatch.setattr(auth, "resolve_credentials",
                        lambda store=None: MagicMock(is_expired=False))

    h = MagicMock()
    h.key_response = _Resp(200, {"mesh_key": "hskey"})
    h.posts = _fake_async_client(monkeypatch, lambda url: h.key_response)
    h.enroll_calls = []
    h.enroll_result = {
        "enrolled": True, "node_id": "n", "tenant_id": "ten-1",
        "bearer_token": "node-cap", "registration": {"node_class": "laptop"},
    }

    async def _rich_enroll(base, token, node_id, **kw):
        h.enroll_calls.append({"base": base, "token": token, "node_id": node_id, **kw})
        return h.enroll_result

    h.join_calls = []

    async def _join(*a, **kw):
        h.join_calls.append(kw)
        return {"overlay_ip": "10.77.0.9", "transport": "headscale"}

    monkeypatch.setattr(enrollment, "rich_enroll", _rich_enroll)
    monkeypatch.setattr(mesh, "join", _join)
    monkeypatch.setattr(mesh, "_tailscale", lambda: "/usr/bin/tailscale")
    h.client = TestClient(_app(), client=LOOPBACK)
    h.post = lambda: h.client.post("/mesh/join", headers={"Origin": GOOD_ORIGIN}, json={})
    return h


class TestJoinErrors:
    def test_missing_tailscale_still_registers_the_device(self, join, tmp_path, monkeypatch):
        # "Register this device" means the machine is listed under the account.
        # The overlay is the only step that needs the binary, so it is the only
        # step skipped -- and it is skipped BEFORE the overlay join is attempted.
        monkeypatch.setattr(mesh, "_tailscale", lambda: None)
        resp = join.post()
        body = resp.json()
        assert resp.status_code == 200
        assert body["ok"] is True
        assert body["registered"] is True
        assert body["overlay"]["ok"] is False
        assert body["overlay"]["code"] == "tailscale_missing"
        assert body["overlay"]["install_url"].startswith("https://")
        assert body["overlay"]["detail"]
        assert body["overlay_ip"] == ""
        assert body["tenant_id"] == "ten-1"

        # The registration call IS made, with the heartbeat on...
        assert len(join.enroll_calls) == 1
        assert join.enroll_calls[0]["enable_heartbeat"] is True
        assert body["node_id"] == join.enroll_calls[0]["node_id"]
        # ...the record a restarted daemon resumes from is on disk...
        rec = json.loads((tmp_path / "node_auth.json").read_text(encoding="utf-8"))
        assert rec["node_id"] == body["node_id"] and rec["mode"] == "rich"
        # ...and the overlay join was never attempted.
        assert join.join_calls == []
        steps = {s["step"]: s for s in body["steps"]}
        assert steps["identity_enroll"]["ok"] is True
        assert steps["overlay_join"]["ok"] is False
        assert steps["overlay_join"]["code"] == "tailscale_missing"

    def test_missing_tailscale_still_starts_the_heartbeat(self, tmp_path, monkeypatch):
        # The registration above is a fake. This one runs the REAL rich_enroll
        # against a faked platform, so the heartbeat start itself is observed.
        import adk.auth as auth

        class _Store:
            def get_active_profile(self):
                return {"access_token": "user-token", "endpoint": IDP}

        monkeypatch.setattr(auth, "AuthStore", _Store)
        monkeypatch.setattr(auth, "resolve_credentials",
                            lambda store=None: MagicMock(is_expired=False))
        monkeypatch.setattr(enrollment, "_AITHER_DIR", tmp_path)
        monkeypatch.setattr(enrollment, "_WORKSPACE_FILE", tmp_path / "workspace.json")
        monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **k: {
            "node_id": node_id, "inference_url": "", "inference_kind": "none",
            "node_class": "laptop",
        })

        def _platform(url):
            if url.endswith("/v1/mesh-keys/issue"):
                return _Resp(200, {"mesh_key": "hskey"})
            return _Resp(200, {"tenant_id": "ten-1", "bearer_token": "node-cap"})

        posts = _fake_async_client(monkeypatch, _platform)
        started = []
        monkeypatch.setattr(
            enrollment, "start_heartbeat",
            lambda base, token, node_id, **kw: started.append(
                {"base": base, "token": token, "node_id": node_id, **kw}) or True,
        )
        monkeypatch.setattr(mesh, "_tailscale", lambda: None)

        async def _never(*a, **k):
            raise AssertionError("overlay join attempted without tailscale")

        monkeypatch.setattr(mesh, "join", _never)

        resp = TestClient(_app(), client=LOOPBACK).post(
            "/mesh/join", headers={"Origin": GOOD_ORIGIN}, json={})
        body = resp.json()
        assert resp.status_code == 200 and body["registered"] is True
        assert body["overlay"]["code"] == "tailscale_missing"
        assert f"{IDP}/v1/nodes/register" in [p["url"] for p in posts]
        assert len(started) == 1
        assert started[0]["base"] == IDP
        assert started[0]["node_id"] == body["node_id"]
        assert started[0]["registered"] is True
        assert started[0]["token_provider"] is server._active_access_token
        assert (tmp_path / "node_auth.json").is_file()

    def test_missing_tailscale_does_not_hide_a_platform_refusal(self, join, monkeypatch):
        monkeypatch.setattr(mesh, "_tailscale", lambda: None)
        join.enroll_result = {
            "enrolled": False, "error": "HTTP 402: ...", "http_status": 402,
            "body": json.dumps({"detail": {"error": "device_quota_exceeded"}}),
        }
        resp = join.post()
        assert resp.status_code == 402
        assert resp.json()["code"] == "device_quota_exceeded"

    def test_with_tailscale_the_overlay_is_reported_ok(self, join):
        body = join.post().json()
        assert body["registered"] is True
        assert body["overlay"] == {"ok": True, "overlay_ip": "10.77.0.9"}
        assert len(join.join_calls) == 1

    def test_a_402_from_registration_is_a_distinct_result(self, join):
        refusal = {"detail": {
            "error": "device_quota_exceeded", "current": 2, "limit": 2,
            "hint": "You have reached your device limit (2). "
                    "Upgrade your plan to add more devices.",
            "upgrade": {"portal_url": "https://example.test/billing"},
        }}
        join.enroll_result = {
            "enrolled": False, "error": "HTTP 402: ...", "http_status": 402,
            "body": json.dumps(refusal),
        }
        resp = join.post()
        body = resp.json()
        assert resp.status_code == 402
        assert body["ok"] is False
        assert body["code"] == "device_quota_exceeded"
        assert body["step"] == "identity_enroll"
        assert body["detail"] == refusal["detail"]["hint"]
        assert (body["current"], body["limit"]) == (2, 2)
        assert body["upgrade"] == {"portal_url": "https://example.test/billing"}
        assert join.join_calls == [], "kept going after the platform said no"

    def test_a_402_on_the_mesh_key_is_passed_through_too(self, join):
        join.key_response = _Resp(402, {"detail": {
            "error": "subscription_required", "hint": "A plan is required.",
        }})
        resp = join.post()
        assert resp.status_code == 402
        assert resp.json()["code"] == "subscription_required"
        assert resp.json()["step"] == "mesh_key"
        assert join.enroll_calls == []

    def test_other_enroll_failures_keep_the_generic_answer(self, join):
        join.enroll_result = {"enrolled": False, "error": "HTTP 500: boom",
                              "http_status": 500, "body": "boom"}
        resp = join.post()
        assert resp.status_code == 502
        assert "code" not in resp.json()


class TestJoinKeepsTheDeviceOnline:
    def test_a_join_persists_the_record_the_next_start_resumes_from(
        self, join, tmp_path, monkeypatch,
    ):
        resp = join.post()
        assert resp.status_code == 200 and resp.json()["ok"] is True
        assert join.enroll_calls[0]["enable_heartbeat"] is True
        assert join.enroll_calls[0]["token_provider"] is server._active_access_token

        rec = json.loads((tmp_path / "node_auth.json").read_text(encoding="utf-8"))
        assert rec["mode"] == "rich"
        assert rec["enroll_base"] == IDP
        assert rec["tenant_id"] == "ten-1"
        assert rec["node_id"] == join.enroll_calls[0]["node_id"]
        # The record says the device is registered; it is not a credential store.
        assert "user-token" not in json.dumps(rec)
        assert "node-cap" not in json.dumps(rec)

        # ...and that record is exactly what a restarted daemon resumes from.
        calls = _stub_loop(monkeypatch)
        _signed_in(monkeypatch)

        async def run():
            out = server.resume_node_heartbeat()
            await asyncio.sleep(0)
            await enrollment.stop_heartbeat()
            return out

        assert asyncio.run(run())["started"] is True
        assert calls[0]["node_id"] == rec["node_id"]

    def test_a_registered_device_rejoins_as_the_same_node(self, join, tmp_path):
        _write_record(tmp_path, node_id="node-already")
        assert join.post().status_code == 200
        assert join.enroll_calls[0]["node_id"] == "node-already"
