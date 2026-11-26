"""Local harness daemon (:8362) auto-start, ensure, and read-only health UX."""
from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace

import pytest

from adk.harnesses import cli as harness_cli


def test_is_local_loopback_url():
    assert harness_cli.is_local_loopback_url("http://127.0.0.1:8362") is True
    assert harness_cli.is_local_loopback_url("http://localhost:8362") is True
    assert harness_cli.is_local_loopback_url("http://[::1]:8362") is True
    assert harness_cli.is_local_loopback_url("http://10.77.0.2:8362") is False
    assert harness_cli.is_local_loopback_url("https://api.aitherium.com") is False


def test_ensure_local_daemon_refuses_remote_url():
    args = SimpleNamespace(url="http://10.77.0.5:8362", token="")
    ok, detail = harness_cli.ensure_local_daemon(args)
    assert ok is False
    assert "non-loopback" in detail


def test_health_command_is_strictly_read_only_and_exits_2_when_down(monkeypatch, capsys):
    called_ensure = []
    monkeypatch.setattr(harness_cli, "ensure_local_daemon", lambda *a, **k: called_ensure.append(1) or (True, "up"))
    monkeypatch.setattr(
        harness_cli.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("Connection refused")),
    )
    args = SimpleNamespace(shell_command="health", url="http://127.0.0.1:18362", token="", json=True)
    rc = harness_cli.cmd_shell(args)
    assert rc == 2
    assert called_ensure == [], "health probe must never auto-start the daemon"
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and data["status"] == 0


def test_request_auto_starts_loopback_daemon_once_and_retries(monkeypatch):
    calls = {"urlopen": 0, "ensure": 0}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"harnesses": []}'

    def fake_urlopen(req, data=None, timeout=60.0):
        calls["urlopen"] += 1
        if calls["urlopen"] == 1:
            raise urllib.error.URLError("Connection refused")
        return _FakeResp()

    def fake_ensure(args, timeout_s=8.0):
        calls["ensure"] += 1
        return True, "started (spawned pid 4242; HTTP 200)"

    monkeypatch.delenv("AITHER_HARNESS_NO_AUTOSTART", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_AUTOSTART", raising=False)
    monkeypatch.setattr(harness_cli.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(harness_cli, "ensure_local_daemon", fake_ensure)

    args = SimpleNamespace(url="", token="", auto_start=False, no_auto_start=False)
    status, payload = harness_cli._request(args, "/harnesses?versions=false")
    assert status == 200
    assert payload == {"harnesses": []}
    assert calls["ensure"] == 1
    assert calls["urlopen"] == 2


def test_request_respects_no_auto_start_and_remote_url(monkeypatch):
    ensure_calls = []
    monkeypatch.setattr(harness_cli, "ensure_local_daemon", lambda *a, **k: ensure_calls.append(1) or (True, "up"))
    monkeypatch.setattr(
        harness_cli.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("Connection refused")),
    )

    # 1. Explicit --no-auto-start skips ensure
    args_no = SimpleNamespace(url="", token="", no_auto_start=True)
    st1, _ = harness_cli._request(args_no, "/harnesses")
    assert st1 == 0 and ensure_calls == []

    # 2. Remote URL skips ensure even when auto-start is enabled
    args_remote = SimpleNamespace(url="http://192.168.1.9:8362", token="", auto_start=True, no_auto_start=False)
    st2, _ = harness_cli._request(args_remote, "/harnesses")
    assert st2 == 0 and ensure_calls == []


def test_cmd_ensure_reports_json_status(monkeypatch, capsys):
    monkeypatch.setattr(harness_cli, "ensure_local_daemon", lambda args, timeout_s=8.0: (True, "already running (HTTP 200)"))
    args = SimpleNamespace(shell_command="ensure", url="", token="", timeout=5.0, json=True)
    rc = harness_cli.cmd_shell(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and "already running" in out["detail"]
