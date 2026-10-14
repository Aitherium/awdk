"""This PC listed and kept online in a company app's device list, with no pasted curl.

Measured 2026-10-14: the customer's only device row read 'enrolling' since 10-06.
The one thing that marked a row live was a curl he had to paste, and the Identity
node registration (`adk enroll`) answers 402 for a tenant without a device plan.
The setup page already starts `adk tenant watch` at logon for that app, with the
person's login and the app's API -- so the watcher registers the PC there once and
beats it with the device's own secret. Each test fails without adk/app_device.py
and its hook in tenant_watch.run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from adk import app_device
from adk import tenant_watch as tw

API = "https://acme-api.example"


class FakeApp:
    """The app's two device routes: enroll (login bearer) and heartbeat (secret)."""

    def __init__(self) -> None:
        self.devices: dict = {}
        self.enrolls: list = []
        self.beats: list = []
        self.enroll_status = 200
        self.beat_status = None  # None = real behaviour
        self.valid_bearer = "person-token"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/devices/enroll":
            self.enrolls.append(json.loads(request.content or b"{}"))
            if request.headers.get("authorization") != f"Bearer {self.valid_bearer}":
                return httpx.Response(401, json={"detail": "Authentication required"})
            if self.enroll_status != 200:
                return httpx.Response(self.enroll_status, json={"detail": "down"})
            dev_id = f"dev-{len(self.devices) + 1}"
            self.devices[dev_id] = {"secret": f"secret-{dev_id}", "last_seen": None}
            return httpx.Response(200, json={
                "device": {"id": dev_id, "name": self.enrolls[-1].get("name"),
                           "status": "enrolling"},
                "device_secret": f"secret-{dev_id}", "commands": {}})
        if path.startswith("/api/devices/") and path.endswith("/heartbeat"):
            dev_id = path.split("/")[3]
            self.beats.append((dev_id, request.headers.get("x-device-token")))
            if self.beat_status is not None:
                return httpx.Response(self.beat_status, json={})
            dev = self.devices.get(dev_id)
            if dev is None or request.headers.get("x-device-token") != dev["secret"]:
                return httpx.Response(404, json={"detail": "Device not found"})
            dev["last_seen"] = "now"
            return httpx.Response(200, json={"id": dev_id, "status": "online"})
        if path == "/api/documents/upload":
            return httpx.Response(200, json={"doc_id": "d1"})
        return httpx.Response(404, json={})


@pytest.fixture
def app():
    return FakeApp()


@pytest.fixture
def client(app):
    with httpx.Client(transport=httpx.MockTransport(app)) as c:
        yield c


@pytest.fixture
def state(tmp_path) -> Path:
    return tmp_path / "app_devices.json"


def test_registers_once_with_the_login_then_beats_as_the_device(app, client, state):
    beat = app_device.AppDeviceBeat(API, path=state, clock=lambda: 0.0)
    out = beat.tick(client, "person-token")
    assert out["ok"] and out["registered"] and out["status"] == "live"
    assert app.devices["dev-1"]["last_seen"] == "now"
    # the beat carried the device's own secret, not the person's login
    assert app.beats == [("dev-1", "secret-dev-1")]
    # a later tick (new process, same record) never registers a second device
    again = app_device.AppDeviceBeat(API, path=state, clock=lambda: 0.0)
    assert again.tick(client, "person-token")["status"] == "live"
    assert len(app.enrolls) == 1 and len(app.devices) == 1


def test_the_secret_is_kept_readable_by_this_user_only(app, client, state):
    app_device.ensure_registered(client, API, "person-token", path=state)
    rec = json.loads(state.read_text(encoding="utf-8"))[API]
    assert rec["device_id"] == "dev-1" and rec["token"] == "secret-dev-1"
    if os.name != "nt":
        assert (state.stat().st_mode & 0o077) == 0


def test_it_keeps_beating_after_the_login_lapses(app, client, state):
    t = [0.0]
    beat = app_device.AppDeviceBeat(API, path=state, clock=lambda: t[0])
    assert beat.tick(client, "person-token")["registered"]
    t[0] += app_device.BEAT_INTERVAL_S
    out = beat.tick(client, "")  # nobody signed in any more
    assert out["ok"] and out["status"] == "live"
    assert len(app.beats) == 2


def test_beats_are_throttled(app, client, state):
    t = [0.0]
    beat = app_device.AppDeviceBeat(API, path=state, clock=lambda: t[0])
    beat.tick(client, "person-token")
    t[0] += 5
    assert beat.tick(client, "person-token") is None
    assert len(app.beats) == 1


def test_a_refused_login_is_said_and_nothing_is_recorded(app, client, state):
    out = app_device.AppDeviceBeat(API, path=state).tick(client, "stale")
    assert not out["ok"] and out["status"] == "auth"
    assert not state.exists()
    assert "adk login" in app_device.say(out, "acme")


def test_an_app_that_cannot_serve_is_retried_not_dropped(app, client, state):
    app.enroll_status = 503
    t = [0.0]
    beat = app_device.AppDeviceBeat(API, path=state, clock=lambda: t[0])
    out = beat.tick(client, "person-token")
    assert not out["ok"] and out["status"] == "error" and "503" in out["detail"]
    app.enroll_status = 200
    t[0] += app_device.REGISTER_RETRY_S
    assert beat.tick(client, "person-token")["registered"]


def test_a_device_removed_in_the_app_is_not_registered_again(app, client, state):
    t = [0.0]
    beat = app_device.AppDeviceBeat(API, path=state, clock=lambda: t[0])
    beat.tick(client, "person-token")
    del app.devices["dev-1"]  # the owner pressed Remove
    t[0] += app_device.BEAT_INTERVAL_S
    out = beat.tick(client, "person-token")
    assert out["status"] == "removed"
    t[0] += app_device.REGISTER_RETRY_S * 100
    assert beat.tick(client, "person-token")["status"] == "removed"
    assert len(app.enrolls) == 1
    # --reset-device: forget, then a new registration
    assert app_device.forget(API, path=state)
    assert app_device.AppDeviceBeat(API, path=state).tick(client, "person-token")["registered"]


def test_a_5xx_beat_is_an_error_not_a_removal(app, client, state):
    t = [0.0]
    beat = app_device.AppDeviceBeat(API, path=state, clock=lambda: t[0])
    beat.tick(client, "person-token")
    app.beat_status = 503
    t[0] += app_device.BEAT_INTERVAL_S
    out = beat.tick(client, "person-token")
    assert out["status"] == "error"
    assert not app_device.record_for(API, path=state).get("removed")


# ── the logon watcher carries it ────────────────────────────────────────────


class _Ctx:
    def __init__(self, c):
        self.c = c

    def __call__(self):
        return self

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        return False


def test_the_logon_watcher_lists_this_pc_in_the_app(app, client, state, tmp_path, capsys):
    device = app_device.AppDeviceBeat(API, path=state, clock=lambda: 0.0)
    rc = tw.run("acme", tmp_path / "Inbox", API, lambda: "person-token", once=True,
                interval=5, as_json=False, client_factory=_Ctx(client),
                state_path=tmp_path / "watch.json", device=device)
    assert rc == 0
    assert app.devices["dev-1"]["last_seen"] == "now"
    assert "now listed in ACME" in capsys.readouterr().out


def test_the_watcher_beats_even_while_the_login_is_refused(app, client, state, tmp_path):
    app_device.ensure_registered(client, API, "person-token", path=state)
    device = app_device.AppDeviceBeat(API, path=state, clock=lambda: 0.0)
    rc = tw.run("acme", tmp_path / "Inbox", API, lambda: "", once=False,
                interval=5, as_json=True, client_factory=_Ctx(client),
                state_path=tmp_path / "watch.json", device=device,
                sleep=lambda s: None, max_passes=1)
    assert rc == 0
    assert app.beats == [("dev-1", "secret-dev-1")]


def test_cmd_tenant_wires_the_device_beat_unless_told_not_to(monkeypatch, tmp_path):
    seen = {}

    def fake_run(*a, **kw):
        seen["device"] = kw.get("device")
        return 0

    monkeypatch.setattr(tw, "run", fake_run)
    monkeypatch.setattr("adk.config.load_saved_config", lambda: {"access_token": "t"})

    class A:
        app = "acme"
        url = API
        token = ""
        dir = str(tmp_path / "Inbox")
        interval = 5.0
        once = True
        json = False
        no_device = False
        reset_device = False

    assert tw.cmd_tenant(type("T", (A,), {"tenant_action": "watch"})()) == 0
    assert isinstance(seen["device"], app_device.AppDeviceBeat)
    assert tw.cmd_tenant(type("T", (A,), {"tenant_action": "watch", "no_device": True})()) == 0
    assert seen["device"] is None
