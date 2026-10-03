"""`adk kvholder phone`: the QR a phone scans and the browser adb opens."""

from __future__ import annotations

import io
import subprocess

from adk import kvholder_net as net


class _Out(io.StringIO):
    def __init__(self, encoding: str):
        super().__init__()
        self._enc = encoding

    @property
    def encoding(self):  # type: ignore[override]
        return self._enc


def test_phone_qr_draws_on_utf8_and_degrades_on_cp1252(monkeypatch):
    url = "http://192.168.1.2:50063/#t=abc"
    monkeypatch.setattr(net.sys, "stdout", _Out("utf-8"))
    qr = net.phone_qr(url)
    assert qr.count("\n") > 10 and "█" in qr
    monkeypatch.setattr(net.sys, "stdout", _Out("cp1252"))
    assert net.phone_qr(url) == ""  # no crash: the printed URL is enough


def test_adb_link_opens_chrome_not_the_default_browser(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "Starting: Intent", "")

    monkeypatch.setattr(net, "adb_path", lambda: "adb")
    monkeypatch.setattr(net.subprocess, "run", fake_run)
    assert net.adb_link(50063, "http://localhost:50063/#t=x") == ""
    assert calls[0][1:3] == ["reverse", "tcp:50063"]
    assert calls[1][-2:] == ["-p", "com.android.chrome"] and len(calls) == 2


def test_adb_link_falls_back_when_no_chrome(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "-p" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "Error: Activity not started", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(net, "adb_path", lambda: "adb")
    monkeypatch.setattr(net.subprocess, "run", fake_run)
    assert net.adb_link(50063, "http://localhost:50063/#t=x") == ""
    assert "-p" not in calls[-1] and len(calls) == 2 + len(net.PHONE_BROWSERS)


def test_tunnel_url_skips_cloudflares_own_api():
    assert net._tunnel_url("https://api.trycloudflare.com/tunnel:") == ""
    assert net._tunnel_url("http://x.example") == ""
    good = "https://scout-vintage-population-order.trycloudflare.com"
    assert net._tunnel_url(good + ".") == good
    assert net._tunnel_url("|" + good + "|") == good


def test_relay_answers_only_for_a_relay():
    import socket

    def free() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    ep, wp = free(), free()
    r, servers = net.start_relay("tok", ("127.0.0.1", ep), ("127.0.0.1", wp))
    try:
        assert net._relay_answers(f"http://127.0.0.1:{wp}", 2.0)
        assert not net._relay_answers(f"http://127.0.0.1:{free()}", 0.0)
    finally:
        net.stop_relay(r, servers)


def test_a_second_relay_on_the_same_port_fails_loudly():
    import socket

    import pytest

    def free() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    ep, wp = free(), free()
    r, servers = net.start_relay("a", ("127.0.0.1", ep), ("127.0.0.1", wp))
    try:
        ep2 = free()
        with pytest.raises(OSError):
            net.start_relay("b", ("127.0.0.1", ep2), ("127.0.0.1", wp))
        s = socket.socket()
        s.bind(("127.0.0.1", ep2))  # the failed relay let its engine port go
        s.close()
    finally:
        net.stop_relay(r, servers)
