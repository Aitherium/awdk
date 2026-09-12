"""Headless one-shot over the AitherTunnel PTY gateway (``adk ssh -x``).

Same wire protocol as the interactive ``adk ssh`` / ``aither connect`` clients
(``wss://<tunnel>/tunnel/ssh?token=<jwt>[&container=<ws>]``, JSON frames), but
no TTY, no raw mode, no POSIX-only ``termios``: send the command line(s),
collect ``output`` frames, return plain text plus an exit code. That is what
CI, cron, PowerShell and coding agents (Claude Code, Codex) need — none of
them have a TTY, and until 2026-09-12 the only way through the tunnel from any
of them was a hand-rolled websocket client.

Two server modes, decided by whether ``container`` is set:

* container → tmux-backed bash. We append ``echo <marker>$?`` to the line and
  stop when the marker comes back, so the real exit code is captured.
* no container → the tunnel's restricted allow-listed shell (``docker``,
  ``curl``, ``cat``, ``grep``, …). It runs ONE allow-listed command per input
  line, rejects ``;``/``&&``, and never reports an exit code. Each command is
  sent as its own line, then ``echo <marker>0`` (``echo`` is allow-listed) as
  the end-of-run signal, with a quiet-timeout fallback if the marker never
  arrives.

Mirrors ``src/terminal-exec.ts`` in awsh — keep the two contracts identical.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.parse import urlencode

EXEC_MARKER = "__ADK_EXEC_DONE_"
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")

CLOSE_CODE_TEXT = {
    4001: "Authentication failed — run `adk login` and retry.",
    4003: "Terminal access denied (your role lacks the `terminal` capability).",
    4004: "Container not running.",
}


def strip_ansi(text: str) -> str:
    """Strip CSI and OSC escape sequences so headless callers get plain text."""
    return _ANSI_OSC.sub("", _ANSI_CSI.sub("", text))


def resolve_tunnel_host(explicit: str | None = None) -> str:
    raw = explicit or os.environ.get("AITHER_TUNNEL_URL") or "tunnel.aitherium.com"
    return re.sub(r"^(wss?|https?)://", "", raw).split("/", 1)[0].rstrip("/")


def build_exec_url(token: str, host: str | None = None, container: str | None = None) -> str:
    qs = {"token": token}
    if container:
        qs["container"] = container
    return f"wss://{resolve_tunnel_host(host)}/tunnel/ssh?{urlencode(qs)}"


def build_input_lines(commands: Sequence[str], marker: str, pty: bool) -> list[str]:
    """The exact ``input`` frames a run sends, in order (pure — unit-tested)."""
    if pty:
        # `;` so the marker still fires when the command fails.
        return [f"{'; '.join(commands)}; echo {marker}$?\n"]
    return [f"{c}\n" for c in commands] + [f"echo {marker}0\n"]


def clean_output(buf: str) -> str:
    """ANSI-stripped output with our own marker line(s) removed."""
    return "\n".join(
        line for line in strip_ansi(buf).split("\n") if EXEC_MARKER not in line
    ).replace("\r", "")


@dataclass
class ExecResult:
    #: remote ``$?`` in container mode; 0 for a clean restricted-shell run;
    #: 1 on auth/transport failure; 13 on capability denial; 124 on timeout.
    code: int
    output: str
    #: 'marker' | 'quiet' | 'timeout' | 'closed' | 'error'
    reason: str


async def exec_remote(
    commands: Sequence[str],
    *,
    token: str,
    container: str | None = None,
    host: str | None = None,
    timeout_s: float = 120.0,
    quiet_s: float = 2.5,
    sink: Callable[[str], None] | None = None,
) -> ExecResult:
    """Run *commands* through the tunnel and return their output + exit code."""
    if not token:
        return ExecResult(1, "Not authenticated. Run: adk login\n", "error")
    try:
        import websockets
    except ImportError:
        return ExecResult(1, "adk ssh -x needs the 'websockets' package: pip install websockets\n", "error")

    pty = bool(container)
    marker = f"{EXEC_MARKER}{int(time.time() * 1000):x}_"
    marker_re = re.compile(re.escape(marker) + r"(\d+)")
    url = build_exec_url(token, host, container)
    buf: list[str] = []
    last_rx = time.monotonic()

    def _cleaned() -> str:
        return clean_output("".join(buf))

    try:
        async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
            await ws.send(json.dumps({"type": "resize", "cols": 200, "rows": 50}))
            for line in build_input_lines(commands, marker, pty):
                await ws.send(json.dumps({"type": "input", "data": line}))

            deadline = time.monotonic() + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ExecResult(124, _cleaned(), "timeout")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(0.25, remaining))
                except asyncio.TimeoutError:
                    if buf and time.monotonic() - last_rx > quiet_s:
                        return ExecResult(0, _cleaned(), "quiet")
                    continue
                except websockets.exceptions.ConnectionClosed as exc:
                    text = CLOSE_CODE_TEXT.get(getattr(exc.rcvd, "code", None) or 0)
                    if text:
                        buf.append(text + "\n")
                        return ExecResult(13 if exc.rcvd.code == 4003 else 1, _cleaned(), "error")
                    return ExecResult(0 if buf else 1, _cleaned(), "closed")

                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                mtype = msg.get("type")
                if mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif mtype == "output":
                    text = str(msg.get("data", ""))
                    buf.append(text)
                    last_rx = time.monotonic()
                    # Never stream our own marker line to the caller.
                    if sink and EXEC_MARKER not in text:
                        sink(text)
                    m = marker_re.search(strip_ansi("".join(buf)))
                    if m:
                        return ExecResult(int(m.group(1)), _cleaned(), "marker")
                elif msg.get("error"):
                    buf.append(str(msg["error"]) + "\n")
                    return ExecResult(1, _cleaned(), "error")
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a result, never a traceback
        buf.append(f"{type(exc).__name__}: {exc}\n")
        return ExecResult(1, _cleaned(), "error")


def exec_remote_sync(commands: Sequence[str], **kw) -> ExecResult:
    return asyncio.run(exec_remote(commands, **kw))
