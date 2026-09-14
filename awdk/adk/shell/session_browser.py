"""
Interactive Claude Code session browser for AitherShell.
=========================================================

Full-screen TUI over ``adk.shell.claude_sessions``: every session in one
place, type-to-filter, deep content search, a live transcript preview pane,
and resume **in this terminal** (hop between sessions from one window) or as
Windows Terminal tabs/windows when you really want them.

Keys:
    type          filter sessions (title / cwd / branch / last prompt)
    up/down       select        pgup/pgdn  page
    enter         resume selected session HERE (in this terminal)
    ctrl-t        resume in a new Windows Terminal tab
    ctrl-n        resume in a new window
    ctrl-s        deep search: full-text over conversation content
    ctrl-r        restore the crashed session set
    f5            rescan
    esc           clear filter / leave search results / quit
    ctrl-q        quit

Requires prompt_toolkit (ships with the shell's platform deps); callers fall
back to the plain numbered list when it's missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from adk.shell import claude_sessions as cs

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import (
        FormattedTextControl,
        HSplit,
        Layout,
        VSplit,
        Window,
    )
    from prompt_toolkit.styles import Style
    HAS_PT = True
except ImportError:  # pragma: no cover - exercised via the CLI fallback path
    HAS_PT = False


_STYLE = {
    "header": "bold fg:ansicyan",
    "filter": "bold fg:ansiyellow",
    "count": "fg:ansibrightblack",
    "row": "",
    "row.sel": "reverse",
    "live": "bold fg:ansigreen",
    "age": "fg:ansibrightblack",
    "title": "bold",
    "branch": "fg:ansicyan",
    "cwd": "fg:ansibrightblack",
    "prompt": "fg:ansigreen",
    "snippet": "fg:ansigreen",
    "preview.user": "bold fg:ansiyellow",
    "preview.assistant": "fg:ansicyan",
    "preview.text": "",
    "footer": "fg:ansiblack bg:ansiwhite",
    "crash": "bold fg:ansiyellow",
}


@dataclass
class BrowserAction:
    kind: str                      # 'quit' | 'here' | 'tab' | 'window' | 'restore'
    session: Optional[cs.SessionMeta] = None


@dataclass
class _State:
    sessions: list = field(default_factory=list)     # base scan results
    hits: Optional[list] = None                      # SearchHit list when in deep-search mode
    filter_text: str = ""
    selected: int = 0
    preview_cache: dict = field(default_factory=dict)
    status: str = ""

    def rescan(self):
        self.sessions = cs.scan_sessions(scan=200, top=100)
        self.hits = None
        self.preview_cache.clear()
        self.selected = 0

    def visible(self) -> list:
        """Sessions currently shown (search hits, else filtered scan)."""
        if self.hits is not None:
            return [h.session for h in self.hits]
        if not self.filter_text:
            return self.sessions
        needle = self.filter_text.lower()
        return [
            s for s in self.sessions
            if needle in f"{s.title} {s.cwd} {s.branch} {s.last_prompt}".lower()
        ]

    def current(self) -> Optional[cs.SessionMeta]:
        rows = self.visible()
        if not rows:
            return None
        self.selected = max(0, min(self.selected, len(rows) - 1))
        return rows[self.selected]


def _shorten(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _render_list(st: _State):
    rows = st.visible()
    if not rows:
        return [("class:cwd", "\n  no sessions match")]
    st.selected = max(0, min(st.selected, len(rows) - 1))
    out = []
    hit_by_id = {h.session.id: h for h in (st.hits or [])}
    for i, s in enumerate(rows):
        sel = i == st.selected
        base = "class:row.sel " if sel else ""
        marker = "> " if sel else "  "
        live = " LIVE" if s.live else ""
        out.append((base + "class:row", marker))
        out.append((base + "class:age", f"{s.age:<9} "))
        out.append((base + "class:title", _shorten(s.title, 42)))
        if s.branch:
            out.append((base + "class:branch", f" ({_shorten(s.branch, 24)})"))
        if live:
            out.append((base + "class:live", live))
        hit = hit_by_id.get(s.id)
        if hit:
            out.append((base + "class:count", f"  [{hit.matches}]"))
        out.append(("", "\n"))
        out.append((base + "class:cwd", "    " + _shorten(s.cwd, 74) + "\n"))
    return out


def _render_preview(st: _State):
    s = st.current()
    if not s:
        return [("class:cwd", "nothing selected")]
    out = [
        ("class:title", _shorten(s.title, 70) + "\n"),
        ("class:cwd", s.cwd + ("\n" if not s.branch else f"   ({s.branch})\n")),
        ("class:count", "-" * 60 + "\n"),
    ]
    # Deep-search mode: show the matching snippets, not the tail.
    if st.hits is not None:
        for h in st.hits:
            if h.session.id == s.id:
                for snip in h.snippets:
                    out.append(("class:snippet", "> " + snip + "\n\n"))
                break
        out.append(("class:count", "-" * 60 + "\n"))
    turns = st.preview_cache.get(s.id)
    if turns is None:
        turns = cs.session_transcript(s.file, max_turns=20)
        st.preview_cache[s.id] = turns
    if not turns:
        out.append(("class:cwd", "(no conversation text recovered)"))
    for role, text in turns:
        who = "you" if role == "user" else "claude"
        style = "class:preview.user" if role == "user" else "class:preview.assistant"
        out.append((style, f"\n{who}:\n"))
        out.append(("class:preview.text", _shorten(text, 700) + "\n"))
    return out


def _render_header(st: _State):
    shown = len(st.visible())
    total = len(st.sessions)
    mode = "search" if st.hits is not None else "filter"
    parts = [
        ("class:header", "  Claude sessions  "),
        ("class:filter", f"{mode}: {st.filter_text}_"),
        ("class:count", f"   {shown}/{total}"),
    ]
    crash = cs.pending_crash()
    if crash:
        parts.append(("class:crash",
                      f"   ! crash: {len(crash.get('sessions', []))} lost (ctrl-r restores)"))
    if st.status:
        parts.append(("class:count", "   " + st.status))
    return parts


_FOOTER = (
    "  type=filter  enter=resume here  ctrl-t=tab  ctrl-n=window  "
    "ctrl-s=deep search  ctrl-r=restore crash  f5=rescan  esc=clear/quit  ctrl-q=quit"
)


def _build_app(st: _State, _input=None, _output=None) -> "Application":
    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        st.selected -= 1

    @kb.add("down")
    def _(event):
        st.selected += 1

    @kb.add("pageup")
    def _(event):
        st.selected -= 10

    @kb.add("pagedown")
    def _(event):
        st.selected += 10

    @kb.add("enter")
    def _(event):
        s = st.current()
        if s:
            event.app.exit(result=BrowserAction("here", s))

    @kb.add("c-t")
    def _(event):
        s = st.current()
        if s:
            event.app.exit(result=BrowserAction("tab", s))

    @kb.add("c-n")
    def _(event):
        s = st.current()
        if s:
            event.app.exit(result=BrowserAction("window", s))

    @kb.add("c-r")
    def _(event):
        event.app.exit(result=BrowserAction("restore"))

    @kb.add("c-s")
    def _(event):
        query = st.filter_text.strip()
        if not query:
            st.status = "type a query first, then ctrl-s"
            return
        st.status = "searching..."
        st.hits = cs.search_sessions(query, days=90, max_sessions=50)
        st.selected = 0
        st.status = ""

    @kb.add("f5")
    def _(event):
        st.rescan()
        st.status = "rescanned"

    @kb.add("escape")
    def _(event):
        if st.hits is not None:
            st.hits = None
            st.selected = 0
        elif st.filter_text:
            st.filter_text = ""
            st.selected = 0
        else:
            event.app.exit(result=BrowserAction("quit"))

    @kb.add("c-q")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=BrowserAction("quit"))

    @kb.add("backspace")
    def _(event):
        st.filter_text = st.filter_text[:-1]
        st.selected = 0

    @kb.add(Keys.Any)
    def _(event):
        ch = event.data
        if ch and ch.isprintable():
            st.filter_text += ch
            st.selected = 0
            if st.hits is not None:
                st.hits = None    # editing the query drops back to filter mode

    def cursor(_st=st):
        # Two rendered lines per session row; keeps the selection scrolled into view.
        return Point(0, max(0, _st.selected * 2))

    list_win = Window(
        content=FormattedTextControl(lambda: _render_list(st), get_cursor_position=cursor,
                                     focusable=True, show_cursor=False),
        wrap_lines=False,
        always_hide_cursor=True,
    )
    preview_win = Window(
        content=FormattedTextControl(lambda: _render_preview(st), show_cursor=False),
        wrap_lines=True,
    )
    root = HSplit([
        Window(content=FormattedTextControl(lambda: _render_header(st)), height=1),
        VSplit([
            list_win,
            Window(width=1, char="|", style="class:count"),
            preview_win,
        ]),
        Window(content=FormattedTextControl(lambda: [("class:footer", _FOOTER)]), height=1,
               style="class:footer"),
    ])
    return Application(
        layout=Layout(root, focused_element=list_win),
        key_bindings=kb,
        full_screen=True,
        style=Style.from_dict(_STYLE),
        refresh_interval=5.0,   # keeps ages from going stale while idle
        input=_input,
        output=_output,
    )


def browse() -> None:
    """Run the interactive browser until the user quits.

    Resume-here suspends the TUI, runs claude in this terminal, and returns
    to the browser when the session ends.
    """
    if not HAS_PT:
        raise RuntimeError(
            "prompt_toolkit is required for the interactive browser "
            "(pip install prompt_toolkit) — falling back callers should use "
            "`aither sessions --list`."
        )
    st = _State()
    st.rescan()
    while True:
        action = _build_app(st).run()
        if action is None or action.kind == "quit":
            return
        if action.kind == "here" and action.session:
            print(f"\n  resuming {action.session.title!r} in {action.session.cwd}\n"
                  f"  (exit Claude to come back to the browser)\n")
            cs.resume_here(action.session)
            st.rescan()
            continue
        if action.kind == "tab" and action.session:
            cs.launch_sessions([action.session])
            st.status = f"opened tab: {_shorten(action.session.title, 30)}"
            st.rescan()
            continue
        if action.kind == "window" and action.session:
            cs.launch_sessions([action.session], separate_windows=True)
            st.status = f"opened window: {_shorten(action.session.title, 30)}"
            st.rescan()
            continue
        if action.kind == "restore":
            snap = cs.pending_crash()
            if not snap:
                st.status = "no crash recorded"
                continue
            lines = cs.launch_sessions(cs.snapshot_sessions(snap))
            cs.clear_crash()
            st.status = f"restored {len(lines)} session(s)"
            st.rescan()
            continue
