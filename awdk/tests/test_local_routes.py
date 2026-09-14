"""Contract tests for adk.local_routes — the /api/local/* router behind the
`local` UI pack (Tasks queue, local image generation, awmail-backed mail).

Everything here is pinned WITHOUT network or fleet: the queue wrappers are
monkeypatched to return their documented JSON strings, the image discovery is
replaced with fixture lanes, and the mail half is tested on both branches
(awmail absent, awmail misconfigured, awmail working with a fake Mailer).
The point is the ROUTE contract — shapes the UI depends on — not the
underlying stores, which have their own tests.
"""

from __future__ import annotations

import json
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from adk.local_routes import router as local_router


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(local_router)
    return TestClient(app)


# ── awrun queue (Tasks tab) ───────────────────────────────────────────────


def test_awrun_list_wraps_queue_list(client, monkeypatch):
    import adk.builtin_tools as bt

    monkeypatch.setattr(
        bt, "queue_list",
        lambda *a, **k: json.dumps([{"id": "r-1", "status": "queued", "task": "t"}]),
    )
    r = client.get("/api/local/awrun")
    assert r.status_code == 200
    assert r.json() == {"runs": [{"id": "r-1", "status": "queued", "task": "t"}]}


def test_awrun_list_passes_degradation_through(client, monkeypatch):
    import adk.builtin_tools as bt

    monkeypatch.setattr(
        bt, "queue_list",
        lambda *a, **k: json.dumps({"error": "awrun not available",
                                    "fix": "pip install awdk[queue]"}),
    )
    r = client.get("/api/local/awrun")
    assert r.status_code == 200
    assert r.json()["error"] == "awrun not available"


def test_awrun_submit_passes_task_agent(client, monkeypatch):
    import adk.builtin_tools as bt

    captured = {}

    def fake_submit(kind, priority=0, paths=None, task="", agent="", adk_args=None,
                    workflow="", ref="", inputs=None, service_name="", target="",
                    spec=None):
        captured.update(kind=kind, task=task, agent=agent, priority=priority)
        return json.dumps({"id": "r-x", "status": "queued"})

    monkeypatch.setattr(bt, "queue_submit", fake_submit)
    r = client.post("/api/local/awrun",
                    json={"kind": "agent", "task": "Do the thing", "agent": "aither",
                          "priority": 1})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert captured == {"kind": "agent", "task": "Do the thing",
                        "agent": "aither", "priority": 1}


def test_awrun_submit_refuses_comet_deploy(client):
    """The money-spending kind must be refused on an HTTP route whose identity
    is the daemon's bearer, never a per-caller session (fail closed)."""
    r = client.post("/api/local/awrun",
                    json={"kind": "comet-deploy", "service_name": "x"})
    assert r.status_code == 200
    body = r.json()
    assert body["error"]
    assert "comet-deploy" in body["error"]


def test_awrun_status_and_cancel(client, monkeypatch):
    import adk.builtin_tools as bt

    monkeypatch.setattr(bt, "queue_status",
                        lambda run_id: json.dumps({"id": run_id, "status": "running"}))
    monkeypatch.setattr(bt, "queue_cancel",
                        lambda run_id: json.dumps({"id": run_id, "status": "cancelled"}))
    assert client.get("/api/local/awrun/r-1").json()["status"] == "running"
    assert client.post("/api/local/awrun/r-1/cancel").json()["status"] == "cancelled"


# ── local image generation (Visual tab) ───────────────────────────────────


def _lane(lane_id="test", up=True):
    from adk.images import Lane

    return Lane(id=lane_id, label="Test Lane", port=0, kind="openai",
                up=up, status=200 if up else 0, note="fixture")


def test_image_backends_shape(client, monkeypatch):
    import adk.images as img

    async def fake_discover():
        return [_lane("comfyui", True), _lane("sana", False)]

    monkeypatch.setattr(img, "discover", fake_discover)
    r = client.get("/api/local/images/backends")
    assert r.status_code == 200
    body = r.json()
    assert body["usable"] == ["comfyui"]
    assert [b["id"] for b in body["backends"]] == ["comfyui", "sana"]
    assert body["backends"][0]["up"] is True


def test_image_generate_bad_size_400(client):
    r = client.post("/api/local/images/generations",
                    json={"prompt": "x", "size": "not-a-size"})
    assert r.status_code == 400
    assert "size must look like" in r.json()["detail"]


def test_image_generate_no_backend_503(client, monkeypatch):
    import adk.images as img

    async def fake_discover():
        return []

    async def fake_generate(req):
        raise img.ImageError("No local image backend is able to generate. Tried: 8188, 8202, 7860")

    monkeypatch.setattr(img, "discover", fake_discover)
    monkeypatch.setattr(img, "generate", fake_generate)
    r = client.post("/api/local/images/generations", json={"prompt": "x"})
    assert r.status_code == 503
    assert "No local image backend" in r.json()["detail"]


# ── mail (Mail tab) ───────────────────────────────────────────────────────


def test_mail_status_when_awmail_absent(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "awmail", None)
    monkeypatch.setitem(sys.modules, "awmail.client", None)
    r = client.get("/api/local/mail/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "pip install awmail" in body["fix"]


def test_mail_status_when_unconfigured(client, monkeypatch):
    awmail = pytest.importorskip("awmail")

    def raise_missing():
        raise RuntimeError("AWMAIL_FROM, AWMAIL_PASSWORD, AWMAIL_ALLOW are required")

    monkeypatch.setattr(awmail.client.Mailer, "from_env", staticmethod(raise_missing))
    monkeypatch.delenv("AWMAIL_FROM", raising=False)
    monkeypatch.delenv("AWMAIL_PASSWORD", raising=False)
    monkeypatch.delenv("AWMAIL_ALLOW", raising=False)
    r = client.get("/api/local/mail/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "AWMAIL_FROM" in body["fix"]


def test_mail_send_no_recipient(client, monkeypatch):
    """With mail CONFIGURED, a missing recipient is the domain answer. With
    mail UNCONFIGURED the config message wins instead (fix the config first —
    that is the more useful answer and the order the route pins)."""
    awmail = pytest.importorskip("awmail")

    class FakeMailer:
        def send(self, to, subject="", body=""):
            raise AssertionError("send must not be reached without a recipient")

    monkeypatch.setattr(awmail.client.Mailer, "from_env", staticmethod(lambda: FakeMailer()))
    r = client.post("/api/local/mail/send", json={"to": "", "subject": "s", "body": "b"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "recipient" in body["message"]


def test_mail_send_unconfigured_beats_recipient_check(client, monkeypatch):
    """When awmail is not configured, that answer must win over the recipient
    check — an unconfigured mailer would otherwise read as a form bug."""
    awmail = pytest.importorskip("awmail")

    def raise_missing():
        raise RuntimeError("AWMAIL_FROM, AWMAIL_PASSWORD, AWMAIL_ALLOW are required")

    monkeypatch.setattr(awmail.client.Mailer, "from_env", staticmethod(raise_missing))
    r = client.post("/api/local/mail/send", json={"to": "", "subject": "s", "body": "b"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "AWMAIL_FROM" in body["fix"]


def test_mail_send_maps_accepted_and_refused(client, monkeypatch):
    awmail = pytest.importorskip("awmail")
    from awmail.message import ACCEPTED, REFUSED, SendResult

    class FakeMailer:
        last = None

        def send(self, to, subject="", body=""):
            FakeMailer.last = (to, subject, body)
            return SendResult(status=ACCEPTED, detail="", accepted=[to])

    monkeypatch.setattr(awmail.client.Mailer, "from_env", staticmethod(lambda: FakeMailer()))
    r = client.post("/api/local/mail/send",
                    json={"to": "you@example.com", "subject": "hi", "body": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["accepted"] is True
    assert FakeMailer.last == ("you@example.com", "hi", "hello")

    class RefusingMailer:
        def send(self, to, subject="", body=""):
            return SendResult(status=REFUSED, detail="allowlist blocks you@example.com",
                              rejected={"you@example.com": "not allowed"})

    monkeypatch.setattr(awmail.client.Mailer, "from_env", staticmethod(lambda: RefusingMailer()))
    r = client.post("/api/local/mail/send",
                    json={"to": "you@example.com", "subject": "hi", "body": "hello"})
    body = r.json()
    assert body["ok"] is False
    assert "not allowed" in body["message"]


# ── saga forwarder (Play tab) ─────────────────────────────────────────────


def test_saga_unset_is_503_with_a_fix(client, monkeypatch):
    monkeypatch.delenv("AITHER_SAGA_URL", raising=False)
    r = client.get("/api/local/saga/status")
    assert r.status_code == 503, "a host with no Saga answers 'not here', never a 404"
    assert "fix" in r.json()["detail"]


def test_saga_non_loopback_is_refused(client, monkeypatch):
    monkeypatch.setenv("AITHER_SAGA_URL", "http://saga.example.com:18770")
    r = client.get("/api/local/saga/status")
    assert r.status_code == 503
    assert "this machine" in r.json()["detail"]["error"]


def test_saga_dot_segments_are_refused(client, monkeypatch):
    monkeypatch.setenv("AITHER_SAGA_URL", "http://127.0.0.1:18770")
    r = client.get("/api/local/saga/..%2Fstorygraph%2Fstats")
    assert r.status_code == 404


def test_saga_forwards_method_query_body_and_world_but_not_the_bearer(client, monkeypatch):
    import httpx

    monkeypatch.setenv("AITHER_SAGA_URL", "http://127.0.0.1:18770")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, url=str(request.url), body=request.content,
                    world=request.headers.get("x-saga-world"),
                    auth=request.headers.get("authorization"))
        return httpx.Response(201, json={"turn_id": "mem-1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = client.post("/api/local/saga/turn?world=x", json={"message": "hi"},
                    headers={"X-Saga-World": "private/alpha", "Authorization": "Bearer t"})
    assert r.status_code == 201
    assert r.json() == {"turn_id": "mem-1"}
    assert seen["method"] == "POST"
    assert seen["url"] == "http://127.0.0.1:18770/api/local/saga/turn?world=x"
    assert seen["world"] == "private/alpha"
    assert b"hi" in seen["body"]
    assert seen["auth"] is None, "the daemon's bearer must not travel to another process"


# ── studio remote lane (Studio tab) ───────────────────────────────────────


@pytest.fixture()
def studio_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    return tmp_path


def test_studio_remote_is_off_by_default(client, studio_home):
    assert client.get("/api/local/studio/status").json()["remote"]["enabled"] is False


def test_studio_refuses_before_building_any_client(client, studio_home, monkeypatch):
    import httpx

    def boom(**_kw):
        raise AssertionError("an HTTP client was built before consent was read")

    monkeypatch.setattr(httpx, "AsyncClient", boom)
    r = client.post("/api/local/studio/remote/op/remove_bg", json={})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "remote_disabled"


def test_studio_consent_must_be_explicit_and_stores_no_key(client, studio_home):
    r = client.post("/api/local/studio/remote/consent", json={"url": "http://127.0.0.1:8200"})
    assert r.status_code == 400
    r = client.post("/api/local/studio/remote/consent",
                    json={"url": "http://127.0.0.1:8200", "confirm": True},
                    headers={"X-Studio-Key": "sk-should-never-land"})
    assert r.status_code == 200
    stored = (studio_home / "studio-remote.json").read_text(encoding="utf-8")
    assert json.loads(stored)["auto"] is False
    assert "sk-should-never-land" not in stored


def test_studio_malformed_consent_is_off(client, studio_home):
    (studio_home / "studio-remote.json").write_text("{not json", encoding="utf-8")
    assert client.get("/api/local/studio/remote/ops").status_code == 403


def test_studio_forwards_to_the_consented_url_only_and_passes_ok_false_through(
        client, studio_home, monkeypatch):
    import httpx

    client.post("/api/local/studio/remote/consent",
                json={"url": "http://127.0.0.1:8200", "confirm": True})
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), auth=request.headers.get("authorization"),
                    body=request.content)
        return httpx.Response(200, json={"ok": False, "error": "restricted op"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = client.post("/api/local/studio/remote/op/remove_bg?url=http://evil.example",
                    json={"media_id": "m1"}, headers={"X-Studio-Key": "k-123"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "restricted op"}, "ok:false is not a success"
    assert seen["url"] == "http://127.0.0.1:8200/op/remove_bg"
    assert seen["auth"] == "Bearer k-123"
    assert b"m1" in seen["body"]
    bad = client.post("/api/local/studio/remote/op/..%2Fapi%2Fconfig", json={})
    assert bad.status_code == 404


def test_studio_revoke_turns_it_off(client, studio_home):
    client.post("/api/local/studio/remote/consent",
                json={"url": "http://127.0.0.1:8200", "confirm": True})
    assert client.delete("/api/local/studio/remote/consent").json() == {"enabled": False}
    assert client.get("/api/local/studio/status").json()["remote"]["enabled"] is False
