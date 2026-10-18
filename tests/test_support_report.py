"""Device support reports: scrubbed before disk, opt-out honoured, uploaded with the
device's own secret, kept while offline, and driven by ``adk tenant watch``.

The transport is httpx.MockTransport, so the real request (path, headers, JSON) is
inspected rather than a stubbed helper.
"""

from __future__ import annotations

import json
import sys

import httpx

from adk import app_device
from adk import support_report as sr
from adk import tenant_watch as tw

API = "https://acme-api.example"
ON = {"AITHER_SUPPORT_REPORTS": "1"}


class _App:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"path": request.url.path, "headers": dict(request.headers),
                           "json": json.loads(request.read() or b"{}")})
        return httpx.Response(self.status, json={"ok": self.status < 300})


def _client(app):
    return httpx.Client(transport=httpx.MockTransport(app))


def _device(tmp_path):
    path = tmp_path / "app_devices.json"
    path.write_text(json.dumps({API: {"device_id": "dev1", "token": "s3cret"}}), encoding="utf-8")
    return path


def _queue(tmp_path, kind="error", **kw):
    rep = sr.build_report(kind, "awdk", kw.pop("title", "boom"), **kw)
    return sr.enqueue(rep, outbox=tmp_path / "box", env=ON)


def test_reports_are_on_by_default_and_off_by_setting_or_env(tmp_path):
    settings = tmp_path / "support.json"
    assert sr.enabled(settings, env={})
    sr.set_enabled(False, settings)
    assert not sr.enabled(settings, env={})
    assert sr.enabled(settings, env={"AITHER_SUPPORT_REPORTS": "1"})
    assert not sr.enabled(tmp_path / "none.json", env={"AITHER_SUPPORT_REPORTS": "off"})


def test_opted_out_nothing_is_written(tmp_path):
    rep = sr.build_report("error", "awdk", "x")
    assert sr.enqueue(rep, outbox=tmp_path / "box", env={"AITHER_SUPPORT_REPORTS": "0"}) is None
    assert not (tmp_path / "box").exists()


def test_secrets_and_emails_are_scrubbed_before_disk(tmp_path):
    path = _queue(tmp_path, message="key ghp_abcSECRET99 mail patuser@example.com",
                  logs=["Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y"])
    text = path.read_text(encoding="utf-8")
    for leak in ("ghp_abcSECRET99", "patuser@example.com", "eyJhbGciOiJIUzI1NiJ9"):
        assert leak not in text, leak


def test_no_scrubber_means_nothing_is_kept(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "awreport.redact", None)
    assert _queue(tmp_path) is None
    assert not list((tmp_path / "box").glob("*.json")) if (tmp_path / "box").exists() else True


def test_flush_uses_the_device_secret_and_empties_the_outbox(tmp_path):
    _queue(tmp_path)
    app = _App(200)
    with _client(app) as c:
        out = sr.flush(c, API, outbox=tmp_path / "box", device_path=_device(tmp_path), env=ON)
    assert out["sent"] == 1 and out["kept"] == 0
    assert app.calls[0]["path"] == "/api/devices/dev1/reports"
    assert app.calls[0]["headers"]["x-device-token"] == "s3cret"
    assert "tenant_id" not in app.calls[0]["json"]


def test_offline_or_5xx_keeps_the_report(tmp_path):
    _queue(tmp_path)
    with _client(_App(503)) as c:
        out = sr.flush(c, API, outbox=tmp_path / "box", device_path=_device(tmp_path), env=ON)
    assert out["sent"] == 0 and out["kept"] == 1 and "503" in out["stopped"]


def test_a_report_the_app_will_never_accept_is_set_aside(tmp_path):
    _queue(tmp_path)
    with _client(_App(422)) as c:
        out = sr.flush(c, API, outbox=tmp_path / "box", device_path=_device(tmp_path), env=ON)
    assert out["rejected"] == 1 and out["kept"] == 0
    assert list((tmp_path / "box" / "rejected").glob("*.json"))


def test_without_credentials_only_a_setup_result_goes_anonymously(tmp_path):
    _queue(tmp_path, kind="error")
    _queue(tmp_path, kind="setup", title="setup failed")
    app = _App(200)
    with _client(app) as c:
        out = sr.flush(c, API, outbox=tmp_path / "box", device_path=tmp_path / "none.json",
                       env=ON)
    assert [x["path"] for x in app.calls] == ["/api/support/setup-report"]
    assert out["sent"] == 1 and out["kept"] == 1


def test_the_login_bearer_is_used_when_the_pc_is_not_listed(tmp_path):
    _queue(tmp_path)
    app = _App(200)
    with _client(app) as c:
        sr.flush(c, API, bearer="tok", outbox=tmp_path / "box",
                 device_path=tmp_path / "none.json", env=ON)
    assert app.calls[0]["path"] == "/api/support/reports"
    assert app.calls[0]["headers"]["authorization"] == "Bearer tok"


def test_report_exception_never_raises(tmp_path):
    try:
        raise ValueError("password=hunter2abc broke")
    except ValueError as exc:
        path = sr.report_exception("awdk.enroll", exc, outbox=tmp_path / "box", env=ON)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source"] == "awdk.enroll" and "ValueError" in data["stack"]
    assert "hunter2abc" not in json.dumps(data)


def test_tenant_watch_drives_the_flusher_and_queues_a_failed_pass(tmp_path, monkeypatch):
    queued = []
    monkeypatch.setattr(tw, "_queue_failure", lambda exc: queued.append(exc))

    def _boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(tw, "watch_once", _boom)

    class _Support:
        ticks = 0

        def tick(self, client, bearer):
            type(self).ticks += 1

    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    rc = tw.run("acme", tmp_path / "in", API, lambda: "t", once=False, interval=1,
                as_json=True, client_factory=_Ctx, state_path=tmp_path / "s.json",
                sleep=lambda _s: None, max_passes=2, support=_Support())
    assert rc == 0
    assert _Support.ticks == 2
    assert len(queued) == 2 and isinstance(queued[0], RuntimeError)


def test_device_record_reader_is_the_one_the_beat_writes(tmp_path):
    path = _device(tmp_path)
    assert app_device.record_for(API, path)["token"] == "s3cret"


def test_turned_off_hand_sent_feedback_still_goes_but_automatic_reports_stay(tmp_path):
    _queue(tmp_path, kind="error")
    _queue(tmp_path, kind="feedback", title="please call me")
    app = _App(200)
    with _client(app) as c:
        out = sr.flush(c, API, outbox=tmp_path / "box", device_path=_device(tmp_path),
                       env={"AITHER_SUPPORT_REPORTS": "0"})
    assert [x["json"]["kind"] for x in app.calls] == ["feedback"]
    assert out["sent"] == 1 and out["kept"] == 1
