"""``adk devices add`` -- mint a pairing code, show it (+ QR), wait for the device.

A fake ``_send`` stands in for Identity. Pinned: the mint is ONE POST to
``/v1/nodes/pairing/init`` as the signed-in caller; the code and the ``adk pair <code>``
line are printed; the wait polls ``/pairing/status/<code>`` and exits 0 on confirm and
3 when the code expires unused; a refusal is printed verbatim and exits 1; a bare
``adk devices`` lists. Also the shared terminal QR helper the verbs print through.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adk import devices, term_qr


def _args(**kw):
    base = dict(devices_command="add", no_wait=False, timeout=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def identity(monkeypatch):
    st = SimpleNamespace(calls=[], confirm_after=1, mint_status=200)

    def send(method, url, headers, timeout, body=None):
        st.calls.append((method, url))
        if method == "POST" and url.endswith("/v1/nodes/pairing/init"):
            if st.mint_status != 200:
                return st.mint_status, json.dumps({"detail": {"code": "device_quota_exceeded"}})
            return 200, json.dumps({"code": "K7Q2ZP", "expires_in": 300})
        if method == "GET" and "/v1/nodes/pairing/status/K7Q2ZP" in url:
            polls = sum(1 for c in st.calls if "/pairing/status/" in c[1])
            done = polls > st.confirm_after
            return 200, json.dumps({"confirmed": done, "node_id": "node-deck" if done else None})
        if method == "GET" and url.endswith("/v1/nodes"):
            return 200, json.dumps({"endpoints": [], "online": 0})
        return 404, ""

    monkeypatch.setattr(devices, "_send", send)
    monkeypatch.setattr(devices, "resolve_bearer", lambda: "tok")
    monkeypatch.setattr(devices.fleet_enroll, "enroll_base_url", lambda: "https://id.example")
    return st


def test_add_mints_prints_and_waits_for_the_confirm(identity, capsys):
    rc = devices._add(_args(), sleep=lambda _s: None, clock=iter(range(1000)).__next__)
    out = capsys.readouterr().out
    assert rc == 0
    assert identity.calls[0] == ("POST", "https://id.example/v1/nodes/pairing/init")
    assert "adk pair K7Q2ZP" in out
    assert "?app=setup" in out
    assert "paired: node-deck" in out


def test_add_no_wait_never_polls(identity, capsys):
    assert devices._add(_args(no_wait=True)) == 0
    assert [c[0] for c in identity.calls] == ["POST"]


def test_an_unused_code_is_exit_3(identity, capsys):
    identity.confirm_after = 10**6
    rc = devices._add(_args(timeout=5), sleep=lambda _s: None,
                      clock=iter(range(1000)).__next__)
    assert rc == 3
    assert "run `adk devices add` again" in capsys.readouterr().out


def test_a_refused_mint_is_printed_verbatim(identity, capsys):
    identity.mint_status = 403
    assert devices._add(_args(no_wait=True)) == 1
    assert "device_quota_exceeded" in capsys.readouterr().out


def test_bare_devices_lists(identity, capsys):
    assert devices.cmd_devices(SimpleNamespace(devices_command=None, json=False)) == 0
    assert "No devices enrolled" in capsys.readouterr().out


def test_setup_link_follows_the_portal_env(monkeypatch):
    monkeypatch.setenv("AITHER_PORTAL_URL", "https://portal.example/")
    assert devices.setup_link() == "https://portal.example/?app=setup"


def test_qr_lines_renders_or_is_empty_never_raises():
    pytest.importorskip("qrcode")
    assert term_qr.qr_lines("https://aitherium.com/?app=setup", encoding="utf-8")
    # a console that cannot carry the block glyphs gets no QR, not a crash
    assert term_qr.qr_lines("https://aitherium.com/?app=setup", encoding="ascii") == []
    assert term_qr.qr_lines("") == []


def test_print_qr_reports_whether_it_printed(monkeypatch):
    seen = []
    monkeypatch.setattr(term_qr, "qr_lines", lambda _t: ["##", "##"])
    assert term_qr.print_qr("x", seen.append, indent="  ") is True
    assert seen == ["  ##\n  ##"]
    monkeypatch.setattr(term_qr, "qr_lines", lambda _t: [])
    assert term_qr.print_qr("x", seen.append) is False
