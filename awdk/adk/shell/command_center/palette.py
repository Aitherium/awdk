"""
``aither palette`` — one fuzzy picker across everything.
=========================================================

Type a few characters, hit Enter, and the right thing happens — no need to
remember which subcommand owns what. Entry sources (each fail-soft):

- **actions** — hq, inbox, sessions, chat, watch, brief, docker-recover, ...
- **sessions** — recent Claude Code sessions (Enter resumes here)
- **services** — core fleet services (Enter prints current health JSON)
- **agents** — live roster from Genesis, when it reports one

Matching is subsequence-fuzzy ("sbr" hits "sessions browser"), ranked by
match tightness.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

from adk.shell.command_center.fleet_client import CORE_SERVICES, FleetClient

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
    from prompt_toolkit.styles import Style
    HAS_PT = True
except ImportError:  # pragma: no cover
    HAS_PT = False


@dataclass
class Entry:
    kind: str                 # action | session | service | agent
    label: str
    hint: str = ""
    run: Optional[Callable[[], None]] = None


def fuzzy_score(needle: str, hay: str) -> int:
    """Subsequence match score: -1 = no match; lower span = better (0 best)."""
    needle = needle.lower()
    hay = hay.lower()
    if not needle:
        return 0
    start = hay.find(needle[0])
    if start < 0:
        return -1
    i = start
    for ch in needle[1:]:
        i = hay.find(ch, i + 1)
        if i < 0:
            return -1
    return (i - start) - (len(needle) - 1) + (start // 4)


def _resume_session(meta) -> None:
    from adk.shell import claude_sessions as cs
    print(f"\n  resuming {meta.title!r} in {meta.cwd}\n")
    cs.resume_here(meta)


def _show_service(name: str, base: str) -> None:
    async def go():
        async with FleetClient() as fc:
            st = await fc.get_json(base + "/health")
            print(f"\n  {name} ({base})")
            print(json.dumps(st.data, indent=2)[:2000] if st.ok else f"  DOWN: {st.error}")
    asyncio.run(go())
    input("\n  (enter to continue) ")


def build_entries() -> list[Entry]:
    entries: list[Entry] = []

    def act(label, hint, fn):
        entries.append(Entry("action", label, hint, fn))

    def _lazy(module, attr, *args, **kwargs):
        def go():
            import importlib
            fn = getattr(importlib.import_module(module), attr)
            fn(*args, **kwargs)
        return go

    act("hq dashboard", "the command-center home screen",
        _lazy("adk.shell.command_center.hq", "run_hq"))
    act("inbox", "mail + relay mentions + alerts",
        _lazy("adk.shell.command_center.inbox", "run_inbox"))
    act("sessions browser", "browse/search/resume Claude Code sessions",
        _lazy("adk.shell.session_browser", "browse"))
    act("chat", "AitherShell chat REPL",
        lambda: subprocess.call([sys.executable, "-m", "adk.shell"]))
    act("watchtower", "fleet health stream + wedge detection",
        _lazy("adk.shell.command_center.watchtower", "run_watch"))
    act("executive brief", "run + render the executive briefing",
        _lazy("adk.shell.command_center.brief", "run_brief"))
    act("agents console", "roster + ask an agent",
        _lazy("adk.shell.command_center.agents_console", "run_agents"))
    act("recover docker", "hard-recover the Docker WSL2 wedge",
        _lazy("adk.shell.cli", "_docker_recover", verbose=True))
    act("sessions ingest", "sync Claude conversations into the brain",
        lambda: subprocess.call([sys.executable, "-m", "adk.shell",
                                 "sessions", "ingest"]))

    # Claude sessions — resume in place.
    try:
        from adk.shell import claude_sessions as cs
        for meta in cs.scan_sessions(scan=60, top=30):
            live = " [LIVE]" if meta.live else ""
            entries.append(Entry(
                "session", f"session: {meta.title}{live}",
                f"{meta.cwd} - {meta.age}",
                (lambda m=meta: _resume_session(m)),
            ))
    except Exception:
        pass

    for name, (base, _) in CORE_SERVICES.items():
        entries.append(Entry("service", f"service: {name}", base,
                             (lambda n=name, b=base: _show_service(n, b))))
    return entries


@dataclass
class _PaletteState:
    entries: list = field(default_factory=list)
    query: str = ""
    selected: int = 0

    def visible(self) -> list:
        if not self.query:
            return self.entries
        scored = []
        for e in self.entries:
            s = fuzzy_score(self.query, f"{e.label} {e.hint}")
            if s >= 0:
                scored.append((s, e))
        scored.sort(key=lambda t: t[0])
        return [e for _, e in scored]


_KIND_STYLE = {"action": "class:action", "session": "class:session",
               "service": "class:service", "agent": "class:agent"}

_STYLE = {
    "prompt": "bold fg:ansiyellow",
    "sel": "reverse",
    "action": "bold fg:ansicyan",
    "session": "fg:ansigreen",
    "service": "fg:ansimagenta",
    "agent": "fg:ansiyellow",
    "hint": "fg:ansibrightblack",
    "footer": "fg:ansiblack bg:ansiwhite",
}


def _render(st: _PaletteState):
    rows = st.visible()
    if not rows:
        return [("class:hint", "\n  nothing matches")]
    st.selected = max(0, min(st.selected, len(rows) - 1))
    out = []
    for i, e in enumerate(rows):
        sel = "class:sel " if i == st.selected else ""
        out.append((sel + _KIND_STYLE.get(e.kind, ""), ("> " if i == st.selected else "  ") + e.label))
        out.append((sel + "class:hint", f"   {e.hint}"[:80] + "\n"))
    return out


def run_palette() -> None:
    if not HAS_PT:
        raise RuntimeError("prompt_toolkit is required for the palette")
    st = _PaletteState(entries=build_entries())
    kb = KeyBindings()

    @kb.add("up")
    def _(e):
        st.selected -= 1

    @kb.add("down")
    def _(e):
        st.selected += 1

    @kb.add("backspace")
    def _(e):
        st.query = st.query[:-1]
        st.selected = 0

    @kb.add("enter")
    def _(e):
        rows = st.visible()
        if rows:
            e.app.exit(result=rows[st.selected])

    @kb.add("escape")
    @kb.add("c-c")
    def _(e):
        e.app.exit(result=None)

    @kb.add(Keys.Any)
    def _(e):
        if e.data and e.data.isprintable():
            st.query += e.data
            st.selected = 0

    body = Window(
        FormattedTextControl(
            lambda: _render(st),
            get_cursor_position=lambda: Point(0, st.selected),
            focusable=True, show_cursor=False),
        always_hide_cursor=True)
    root = HSplit([
        Window(FormattedTextControl(
            lambda: [("class:prompt", f"  > {st.query}_"),
                     ("class:hint", f"   ({len(st.visible())} matches)")]), height=1),
        body,
        Window(FormattedTextControl(
            lambda: [("class:footer", "  type=filter  enter=go  esc=quit")]),
            height=1, style="class:footer"),
    ])
    app = Application(layout=Layout(root, focused_element=body), key_bindings=kb,
                      full_screen=True, style=Style.from_dict(_STYLE))
    chosen = app.run()
    if chosen and chosen.run:
        chosen.run()
