"""`adk kvholder phone`: the QR a phone scans and the browser adb opens."""

from __future__ import annotations

import io
import subprocess

import pytest

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


def test_usb_opens_only_named_phones(monkeypatch, tmp_path, capsys):
    import argparse
    import socket

    def free() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    linked: list = []
    monkeypatch.setattr(net, "adb_devices", lambda: ["A", "B", "C"])
    monkeypatch.setattr(net, "adb_link", lambda port, url, serial=None: linked.append(serial) or "")
    monkeypatch.setattr(net, "state_path", lambda: tmp_path / "relay.json")

    def stop(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(net.time, "sleep", stop)
    args = argparse.Namespace(
        via="usb", port=free(), web_port=free(), host="", token="tk", serial="", mesh=False
    )
    assert net.run_phone(args) == 0
    assert linked == []  # three phones, none named: open nothing, never guess
    assert "/swarm#m=tk" in capsys.readouterr().out
    args.port, args.web_port, args.serial = free(), free(), "all"
    net.run_phone(args)
    assert linked == ["A", "B", "C"]
    linked.clear()
    args.port, args.web_port, args.serial = free(), free(), "B"
    net.run_phone(args)
    assert linked == ["B"]


def test_config_drops_a_dead_link_and_succeeds():
    """A hidden or reloaded tab leaves a closed link: CONFIG must skip it, not fail the engine."""
    import socket
    import threading
    import time

    from adk import kvholder as kv

    np = pytest.importorskip("numpy")

    def free() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    ep, wp = free(), free()
    r, servers = net.start_relay("tk", ("127.0.0.1", ep), ("127.0.0.1", wp))
    try:
        for name in ("a", "b"):
            h = kv.KVHolder(8 << 20, device=name)
            threading.Thread(
                target=net.dial_holder,
                args=(f"ws://127.0.0.1:{wp}/holder", "tk", h, True),
                daemon=True,
            ).start()
        end = time.time() + 10
        while len(r.holders) < 2 and time.time() < end:
            time.sleep(0.05)
        assert len(r.holders) == 2
        dead, alive = r.holders[0].device, r.holders[1].device
        r.holders[0].ws.sock.close()  # the link died; the relay has not noticed yet
        c = kv.KVHolderClient("127.0.0.1", ep, timeout=30)
        c.configure(kv.Config.for_model(1, 1, "q8_0"))  # raises on the old behaviour
        assert [h.device for h in r.holders] == [alive] and r.holders[0].off == 0
        assert dead != alive
        k = np.ones((4, 1, kv.HD), np.float32)
        assert c.append(0, 0, k, k) == 4
        c.close()
    finally:
        net.stop_relay(r, servers)
