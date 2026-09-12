"""`adk ssh -x` — headless exec over the tunnel PTY gateway.

Found 2026-09-12 while troubleshooting arc.aitherium.com from a Windows laptop:
`adk ssh` was TTY-only AND POSIX-only, so from PowerShell, CI, or a coding agent
there was no way through the tunnel at all. These tests pin the frame contract
(shared with awsh's terminal-exec.ts) against a fake websocket: marker-based
exit codes in container mode, one-line-per-command in restricted mode, marker
lines never leaking, close-code mapping, and the root-local token guard.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from adk import tunnel_exec as te


def test_strip_ansi_removes_csi_and_osc():
    assert te.strip_ansi("\x1b[1;33mwarn\x1b[0m \x1b]0;title\x07ok") == "warn ok"


def test_resolve_tunnel_host_strips_scheme_and_path(monkeypatch):
    assert te.resolve_tunnel_host("wss://t.example.com/tunnel/ssh") == "t.example.com"
    monkeypatch.setenv("AITHER_TUNNEL_URL", "https://env.example.com/")
    assert te.resolve_tunnel_host(None) == "env.example.com"
    monkeypatch.delenv("AITHER_TUNNEL_URL")
    assert te.resolve_tunnel_host(None) == "tunnel.aitherium.com"


def test_build_url_carries_token_and_container():
    url = te.build_exec_url("tok", None, "devws-a")
    assert url.startswith("wss://tunnel.aitherium.com/tunnel/ssh?")
    assert "token=tok" in url and "container=devws-a" in url


def test_input_lines_container_mode_is_one_line_with_marker():
    lines = te.build_input_lines(["echo hi", "false"], "M_", pty=True)
    assert lines == ["echo hi; false; echo M_$?\n"]


def test_input_lines_restricted_mode_is_one_per_command_no_semicolons():
    lines = te.build_input_lines(["hostname", "docker ps"], "M_", pty=False)
    assert lines == ["hostname\n", "docker ps\n", "echo M_0\n"]
    assert not any(";" in line for line in lines[:-1])


def test_clean_output_drops_marker_lines_and_cr():
    raw = "\x1b[33m⚠ banner\x1b[0m\r\nhello\r\n__ADK_EXEC_DONE_abc_0\r\n"
    assert te.clean_output(raw) == "⚠ banner\nhello\n"


# ── fake websocket ──────────────────────────────────────────────────────────

class _Closed(Exception):
    def __init__(self, code):
        self.rcvd = types.SimpleNamespace(code=code)


class _FakeWS:
    """Scripted server: `script(sent_frame, reply, close)` runs on every send()."""

    def __init__(self, script):
        self._script = script
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        f = json.loads(raw)
        self.sent.append(f)
        self._script(f, lambda r: self._inbox.put_nowait(json.dumps(r)),
                     lambda code: self._inbox.put_nowait(_Closed(code)))

    async def recv(self):
        item = await self._inbox.get()
        if isinstance(item, _Closed):
            raise item
        return item


def _install_fake(monkeypatch, script):
    ws = _FakeWS(script)
    mod = types.ModuleType("websockets")
    mod.connect = lambda *a, **k: ws
    mod.exceptions = types.SimpleNamespace(ConnectionClosed=_Closed)
    monkeypatch.setitem(sys.modules, "websockets", mod)
    return ws


def test_container_mode_returns_remote_exit_code_and_hides_marker(monkeypatch):
    def script(f, reply, _close):
        if f.get("type") != "input":
            return
        marker = f["data"].split("echo ")[-1].split("$?")[0]
        reply({"type": "output", "data": "hello\r\n"})
        reply({"type": "output", "data": f"{marker}7\r\n"})

    ws = _install_fake(monkeypatch, script)
    r = te.exec_remote_sync(["echo hello", "false"], token="t", container="devws-x")
    assert (r.code, r.reason) == (7, "marker")
    assert r.output.strip() == "hello"
    assert len([f for f in ws.sent if f["type"] == "input"]) == 1


def test_restricted_mode_one_line_per_command_code_zero(monkeypatch):
    def script(f, reply, _close):
        if f.get("type") != "input":
            return
        if f["data"].startswith("echo __ADK"):
            reply({"type": "output", "data": f["data"].replace("echo ", "")})
        else:
            reply({"type": "output", "data": f"ran:{f['data'].strip()}\n"})

    ws = _install_fake(monkeypatch, script)
    r = te.exec_remote_sync(["hostname", "docker ps"], token="t")
    assert (r.code, r.reason) == (0, "marker")
    assert r.output.strip().splitlines() == ["ran:hostname", "ran:docker ps"]
    assert len([f for f in ws.sent if f["type"] == "input"]) == 3


def test_sink_streams_output_but_never_the_marker(monkeypatch):
    def script(f, reply, _close):
        if f.get("type") != "input":
            return
        marker = f["data"].split("echo ")[-1].split("$?")[0]
        reply({"type": "output", "data": "a\n"})
        reply({"type": "output", "data": f"{marker}0\n"})

    _install_fake(monkeypatch, script)
    chunks: list[str] = []
    te.exec_remote_sync(["true"], token="t", container="c", sink=chunks.append)
    assert chunks == ["a\n"]


@pytest.mark.parametrize("code,expected", [(4001, 1), (4003, 13), (4004, 1)])
def test_close_codes_map_to_exit_codes(monkeypatch, code, expected):
    _install_fake(monkeypatch, lambda f, _r, close: close(code) if f.get("type") == "resize" else None)
    r = te.exec_remote_sync(["x"], token="t")
    assert (r.code, r.reason) == (expected, "error")
    assert te.CLOSE_CODE_TEXT[code].split(" ")[0] in r.output


def test_quiet_timeout_ends_restricted_run_without_marker(monkeypatch):
    _install_fake(monkeypatch, lambda f, reply, _c: reply({"type": "output", "data": "partial\n"})
                  if f.get("type") == "input" and not f["data"].startswith("echo __ADK") else None)
    r = te.exec_remote_sync(["hostname"], token="t", quiet_s=0.3, timeout_s=5)
    assert (r.code, r.reason) == (0, "quiet")
    assert r.output.strip() == "partial"


def test_no_token_is_an_immediate_error():
    r = te.exec_remote_sync(["x"], token="")
    assert r.code == 1 and "login" in r.output
