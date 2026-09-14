"""
``aither hq`` — the AitherShell command center home screen.
============================================================

One full-screen cockpit: fleet health, LLM queue, alerts, sessions, inbox —
refreshed live — with one-key jumps into every deeper surface:

    c  chat REPL          s  sessions browser     i  inbox
    a  agents console     b  executive brief      w  watchtower
    r  recover docker     f5 refresh now          q  quit

Every pane reads through FleetClient and degrades independently: a wedged
service is a red tile, never a dead cockpit.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

from adk.shell.command_center.fleet_client import FleetClient, SourceState

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import (
        FormattedTextControl,
        HSplit,
        Layout,
        VSplit,
        Window,
    )
    from prompt_toolkit.styles import Style
    HAS_PT = True
except ImportError:  # pragma: no cover
    HAS_PT = False


_STYLE = {
    "title": "bold fg:ansicyan",
    "ok": "bold fg:ansigreen",
    "bad": "bold fg:ansired",
    "warn": "bold fg:ansiyellow",
    "dim": "fg:ansibrightblack",
    "val": "bold",
    "footer": "fg:ansiblack bg:ansiwhite",
}

_FOOTER = ("  c=chat  s=sessions  i=inbox  a=agents  b=brief  w=watch  "
           "r=recover-docker  f5=refresh  q=quit")

REFRESH_SECS = 5.0


@dataclass
class HQState:
    services: dict = field(default_factory=dict)
    genesis_services: Optional[SourceState] = None
    queue: Optional[SourceState] = None
    alerts: Optional[SourceState] = None
    mail: Optional[SourceState] = None
    relay: Optional[SourceState] = None
    sessions_live: int = 0
    sessions_total: int = 0
    crash_pending: bool = False
    refreshed_at: Optional[_dt.datetime] = None
    refreshing: bool = False
    error: str = ""


async def refresh(state: HQState, fc: FleetClient) -> None:
    state.refreshing = True
    try:
        snap_t = asyncio.create_task(fc.snapshot())
        gsvc_t = asyncio.create_task(fc.genesis_services())
        alerts_t = asyncio.create_task(fc.alerts())
        mail_t = asyncio.create_task(fc.mail_inbox(limit=1))
        nick = fc.default_nick()
        relay_t = asyncio.create_task(fc.relay_unread(nick)) if nick else None
        snap = await snap_t
        state.services = snap.services
        state.queue = snap.llm_queue
        state.sessions_live = snap.sessions_live
        state.sessions_total = snap.sessions_total
        state.crash_pending = snap.crash_pending
        state.genesis_services = await gsvc_t
        state.alerts = await alerts_t
        state.mail = await mail_t
        state.relay = (await relay_t) if relay_t else SourceState.fail("no nick (aither login)")
        state.refreshed_at = _dt.datetime.now()
        state.error = ""
    except Exception as exc:  # defensive: refresh must never kill the app
        state.error = f"{type(exc).__name__}: {exc}"
    finally:
        state.refreshing = False


# ─── pane renderers ──────────────────────────────────────────────────────────

def _tile(label: str, st: Optional[SourceState]) -> list:
    if st is None:
        return [("class:dim", f"  {label:<15} ...\n")]
    if st.ok:
        return [("class:dim", f"  {label:<15} "),
                ("class:ok", "UP"),
                ("class:dim", f"  {st.latency_ms}ms\n")]
    return [("class:dim", f"  {label:<15} "),
            ("class:bad", "DOWN"),
            ("class:dim", f"  {st.error}\n")]


def render_fleet(state: HQState) -> list:
    out = [("class:title", "  FLEET\n")]
    for name, st in (state.services or {}).items():
        out.extend(_tile(name, st))
    g = state.genesis_services
    if g and g.ok and isinstance(g.data, dict):
        svcs = g.data.get("services") or {}
        degraded = [n for n, s in svcs.items()
                    if isinstance(s, dict) and s.get("status") not in ("running", "healthy")]
        out.append(("class:dim", f"  registry: {len(svcs)} tracked"))
        if degraded:
            out.append(("class:warn", f", {len(degraded)} degraded"))
        out.append(("", "\n"))
    return out


def render_llm(state: HQState) -> list:
    out = [("class:title", "  LLM\n")]
    q = state.queue
    if not q:
        return out + [("class:dim", "  ...\n")]
    if not q.ok:
        return out + [("class:bad", f"  queue: {q.error}\n")]
    d = q.data or {}
    queued = d.get("queued", d.get("pending_tasks", "?"))
    processing = d.get("processing", d.get("running_tasks", "?"))
    failed = d.get("failed_total", d.get("failed_tasks", 0))
    out.append(("class:dim", "  queued "))
    out.append(("class:warn" if queued else "class:val", str(queued)))
    out.append(("class:dim", "   in-flight "))
    out.append(("class:val", str(processing)))
    out.append(("class:dim", "   failed "))
    out.append(("class:bad" if failed else "class:val", str(failed)))
    out.append(("", "\n"))
    models = d.get("models_loaded") or []
    if models:
        out.append(("class:dim", "  models: " + ", ".join(map(str, models[:4])) + "\n"))
    vram_used, vram_free = d.get("vram_used_mb"), d.get("vram_available_mb")
    if vram_used is not None:
        out.append(("class:dim",
                    f"  vram: {vram_used:.0f}MB used / {vram_free:.0f}MB free\n"))
    return out


def render_alerts(state: HQState) -> list:
    out = [("class:title", "  ALERTS\n")]
    a = state.alerts
    if not a:
        return out + [("class:dim", "  ...\n")]
    if not a.ok:
        return out + [("class:bad", f"  pulse: {a.error}\n")]
    alerts = (a.data or {}).get("alerts") or []
    if not alerts:
        return out + [("class:ok", "  quiet — no active alerts\n")]
    for al in alerts[:6]:
        sev = float(al.get("severity", 0) or 0)
        style = "class:bad" if sev >= 0.7 else "class:warn"
        title = str(al.get("title") or al.get("message") or "?")[:56]
        out.append((style, f"  [{sev:.1f}] "))
        out.append(("", title + "\n"))
    if len(alerts) > 6:
        out.append(("class:dim", f"  ... and {len(alerts) - 6} more\n"))
    return out


def render_sessions(state: HQState) -> list:
    out = [("class:title", "  CLAUDE SESSIONS\n")]
    out.append(("class:dim", "  live "))
    out.append(("class:ok", str(state.sessions_live)))
    out.append(("class:dim", f" of {state.sessions_total} recent\n"))
    if state.crash_pending:
        out.append(("class:warn", "  ! crash recorded — press s, then ctrl-r\n"))
    return out


def render_inbox(state: HQState) -> list:
    out = [("class:title", "  INBOX\n")]
    m = state.mail
    if m is None:
        out.append(("class:dim", "  mail ...\n"))
    elif m.ok:
        unread = (m.data or {}).get("unread", "?")
        style = "class:warn" if unread else "class:dim"
        out.append((style, f"  mail: {unread} unread\n"))
    else:
        out.append(("class:dim", f"  mail: {m.error}\n"))
    r = state.relay
    if r is None:
        out.append(("class:dim", "  relay ...\n"))
    elif r.ok:
        unread = (r.data or {}).get("unread") or {}
        total = sum(v for v in unread.values() if isinstance(v, int))
        style = "class:warn" if total else "class:dim"
        out.append((style, f"  relay: {total} unread"
                    + (f" ({', '.join(list(unread)[:3])})" if total else "") + "\n"))
    else:
        out.append(("class:dim", f"  relay: {r.error}\n"))
    return out


def _render_header(state: HQState) -> list:
    when = state.refreshed_at.strftime("%H:%M:%S") if state.refreshed_at else "--"
    parts = [("class:title", "  AITHER HQ "),
             ("class:dim", f"  refreshed {when}"
              + ("  (refreshing...)" if state.refreshing else ""))]
    if state.error:
        parts.append(("class:bad", f"  {state.error}"))
    return parts


# ─── app ─────────────────────────────────────────────────────────────────────

def _build_app(state: HQState, fc: FleetClient, _input=None, _output=None):
    kb = KeyBindings()
    actions = {}

    def act(key, name):
        @kb.add(key)
        def _(event):
            event.app.exit(result=name)

    for key, name in [("c", "chat"), ("s", "sessions"), ("i", "inbox"),
                      ("a", "agents"), ("b", "brief"), ("w", "watch"),
                      ("r", "recover"), ("q", "quit"), ("c-c", "quit"),
                      ("escape", "quit")]:
        act(key, name)

    @kb.add("f5")
    def _(event):
        if not state.refreshing:
            asyncio.ensure_future(_refresh_and_redraw(state, fc, event.app))

    left = HSplit([
        Window(FormattedTextControl(lambda: render_fleet(state)), wrap_lines=False),
    ])
    right = HSplit([
        Window(FormattedTextControl(lambda: render_llm(state)), height=6),
        Window(FormattedTextControl(lambda: render_sessions(state)), height=4),
        Window(FormattedTextControl(lambda: render_inbox(state)), height=4),
        Window(FormattedTextControl(lambda: render_alerts(state))),
    ])
    root = HSplit([
        Window(FormattedTextControl(lambda: _render_header(state)), height=1),
        VSplit([left, Window(width=1, char="|", style="class:dim"), right]),
        Window(FormattedTextControl(lambda: [("class:footer", _FOOTER)]),
               height=1, style="class:footer"),
    ])
    return Application(layout=Layout(root), key_bindings=kb, full_screen=True,
                       style=Style.from_dict(_STYLE), refresh_interval=1.0,
                       input=_input, output=_output)


async def _refresh_and_redraw(state: HQState, fc: FleetClient, app) -> None:
    await refresh(state, fc)
    app.invalidate()


async def _auto_refresh(state: HQState, fc: FleetClient, app) -> None:
    while True:
        await refresh(state, fc)
        app.invalidate()
        await asyncio.sleep(REFRESH_SECS)


def run_hq() -> None:
    """The HQ loop: run the dashboard, dispatch hotkey jumps, come back."""
    if not HAS_PT:
        raise RuntimeError("prompt_toolkit is required for aither hq")
    state = HQState()
    while True:
        fc = FleetClient()
        app = _build_app(state, fc)

        async def _run(app=app, fc=fc):
            task = asyncio.ensure_future(_auto_refresh(state, fc, app))
            try:
                return await app.run_async()
            finally:
                task.cancel()
                await fc.close()

        action = asyncio.run(_run())
        if action in (None, "quit"):
            return
        if action == "chat":
            subprocess.call([sys.executable, "-m", "adk.shell"])
        elif action == "sessions":
            from adk.shell.session_browser import browse
            browse()
        elif action == "inbox":
            from adk.shell.command_center.inbox import run_inbox
            run_inbox()
        elif action == "agents":
            from adk.shell.command_center.agents_console import run_agents
            run_agents()
        elif action == "brief":
            from adk.shell.command_center.brief import run_brief
            run_brief()
            input("\n  (enter to return to HQ) ")
        elif action == "watch":
            from adk.shell.command_center.watchtower import run_watch
            run_watch()
        elif action == "recover":
            from adk.shell.cli import _docker_recover
            print("\n  recovering docker...\n")
            _docker_recover(verbose=True)
            input("\n  (enter to return to HQ) ")
