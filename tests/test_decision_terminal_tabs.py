"""A decision card's "Go to that terminal" selects the session's TAB, not just its window.

The tab rules are a pure function (:func:`winproc.match_tab`) over a list of tab
titles, so they are pinned here without a terminal. The focus flow in
``adk.decisions.terminal`` is exercised with the Windows primitives stubbed: the
assertions are on WHICH tab gets selected and on the fallback message, which is
where a regression would hide (a wrong tab reported as success).

The tab titles below are the owner's real ones, measured 2026-10-04: thirteen
Windows Terminal tabs, nine of which differ ONLY in the trailing session clock.
"""

from __future__ import annotations

import pytest

from adk.decisions import terminal, winproc

SPIN_A, SPIN_B, STAR = "◐", "◑", "✳"

OWNER_TABS = [
    f"{SPIN_A} AitherOS-Fresh develop 16:03",
    f"{SPIN_B} AitherOS-Fresh develop 22:09",
    f"{STAR} AitherOS-Fresh develop 09:29",
    f"{SPIN_A} How to give my aither/platform agents +",
    f"{SPIN_A} AitherOS-Fresh develop 15:45",
    f"{SPIN_B} AitherOS-Fresh develop 07:37",
    f"{SPIN_A} AitherOS-Fresh develop 08:21",
]


# ── the pure matcher ────────────────────────────────────────────────────────────


def test_exact_live_title_picks_its_tab():
    assert winproc.match_tab(OWNER_TABS, f"{SPIN_B} AitherOS-Fresh develop 07:37") == (
        5, "exact title")


def test_spinner_frame_change_between_reads_still_matches():
    index, how = winproc.match_tab(OWNER_TABS, f"{STAR} AitherOS-Fresh develop 07:37")
    assert index == 5
    assert "glyph" in how


def test_the_clock_is_identity_not_noise():
    # 07:38 is a DIFFERENT session; matching it to 07:37 would focus the wrong tab.
    index, _how = winproc.match_tab(OWNER_TABS, f"{SPIN_A} AitherOS-Fresh develop 07:38")
    assert index == -1


def test_two_identical_tabs_are_never_guessed_between():
    tabs = OWNER_TABS + [f"{SPIN_B} AitherOS-Fresh develop 07:37"]
    index, how = winproc.match_tab(tabs, f"{SPIN_B} AitherOS-Fresh develop 07:37")
    assert index == -1
    assert "2 tabs share" in how


def test_tabs_differing_only_in_glyph_are_a_tie_not_an_exact_match():
    # An exact-glyph match would choose by whichever spinner frame was showing.
    tabs = [f"{STAR} AitherOS-Fresh develop 07:37", f"{SPIN_A} AitherOS-Fresh develop 07:37"]
    index, how = winproc.match_tab(tabs, f"{STAR} AitherOS-Fresh develop 07:37")
    assert index == -1
    assert "share" in how


def test_hints_pick_a_unique_tab_only_when_the_title_is_unreadable():
    index, how = winproc.match_tab(OWNER_TABS, "", ("platform agents",))
    assert (index, how) == (3, "directory/branch in the title")


def test_a_readable_title_that_matches_nothing_never_falls_to_hints():
    # The session's tab does not carry its own title (the owner renamed it), so
    # the ONE tab naming the directory is another session's: selecting it would
    # report a wrong tab as success.
    index, how = winproc.match_tab(OWNER_TABS, "renamed by the owner", ("platform agents",))
    assert index == -1
    assert "no tab carries" in how


def test_a_matching_live_title_wins_over_hints():
    # A live title that matches wins over hints that would point elsewhere.
    index, _how = winproc.match_tab(OWNER_TABS, OWNER_TABS[0], ("platform agents",))
    assert index == 0


def test_hints_naming_many_tabs_pick_none():
    index, how = winproc.match_tab(OWNER_TABS, "", ("AitherOS-Fresh", "develop"))
    assert index == -1
    assert "6 tabs" in how


@pytest.mark.parametrize("titles,wanted", [([], "x"), (OWNER_TABS, ""), (OWNER_TABS, "zzz")])
def test_nothing_to_match_says_why(titles, wanted):
    index, how = winproc.match_tab(titles, wanted)
    assert index == -1
    assert how


def test_normalize_drops_only_the_leading_glyph():
    assert winproc.normalize_tab_title(f"  {SPIN_A}  AitherOS-Fresh   develop 07:37 ") == (
        "aitheros-fresh develop 07:37")
    assert winproc.normalize_tab_title(f"{STAR}") == ""


# ── the focus flow, Windows primitives stubbed ──────────────────────────────────


class FakeTerminal:
    """One WT process with two frames; records every select and focus."""

    def __init__(self, frames: dict[int, list[str]], live: str) -> None:
        self.frames = frames
        self.live = live
        self.selected: list[tuple[int, int, str]] = []
        self.focused: list[int] = []
        self.title_writes: list[str] = []
        self.focus_ok = True

    def install(self, monkeypatch) -> None:
        first = next(iter(self.frames))
        monkeypatch.setattr(terminal, "locate", lambda pid: terminal.TerminalTarget(
            pid=pid, alive=True, hwnd=first, owner=4242, title="active tab title"))
        monkeypatch.setattr(winproc, "terminal_frames", lambda owner: list(self.frames))
        monkeypatch.setattr(winproc, "list_tabs",
                            lambda hwnd: [(t, False) for t in self.frames.get(hwnd, [])])
        monkeypatch.setattr(winproc, "console_title", lambda pid: self.live)
        monkeypatch.setattr(winproc, "set_console_title", self._set_title)
        monkeypatch.setattr(winproc, "select_tab", self._select)
        monkeypatch.setattr(winproc, "focus_window", self._focus)
        monkeypatch.setattr(terminal.os, "name", "nt")

    def _set_title(self, pid: int, title: str) -> bool:
        # The session's tab is the LAST copy of the live title in frame 2.
        self.title_writes.append(title)
        tabs = self.frames[2]
        for index in range(len(tabs) - 1, -1, -1):
            if tabs[index] in (self.live, *self.title_writes[:-1]):
                tabs[index] = title
                break
        self.live = title
        return True

    def _select(self, hwnd: int, index: int, title: str) -> bool:
        tabs = self.frames.get(hwnd, [])
        if index >= len(tabs) or tabs[index] != title:
            return False
        self.selected.append((hwnd, index, title))
        return True

    def _focus(self, hwnd: int) -> bool:
        self.focused.append(hwnd)
        return self.focus_ok


def test_focus_selects_the_tab_in_whichever_window_holds_it(monkeypatch):
    live = f"{SPIN_B} AitherOS-Fresh develop 07:37"
    fake = FakeTerminal({1: OWNER_TABS[:3], 2: OWNER_TABS[3:]}, live)
    fake.install(monkeypatch)
    ok, why = terminal.focus(111)
    assert ok, why
    assert fake.selected == [(2, 2, live)]
    assert fake.focused == [2]  # the frame that HOLDS the tab, not the largest one
    assert why == f"focused the tab: {live}"


def test_focus_breaks_a_title_tie_with_a_temporary_marker(monkeypatch):
    live = f"{SPIN_B} AitherOS-Fresh develop 07:37"
    fake = FakeTerminal({1: [live], 2: ["other", live]}, live)
    fake.install(monkeypatch)
    ok, why = terminal.focus(111)
    assert ok, why
    marker = fake.title_writes[0]
    assert marker.startswith("aither-card 111 ")
    assert fake.selected == [(2, 1, marker)]
    # ...and the session's real title is put back afterwards.
    assert fake.title_writes[-1] == live
    assert why == f"focused the tab: {live}"


def test_focus_falls_back_to_the_window_and_says_so(monkeypatch):
    fake = FakeTerminal({1: ["conhost has no tabs"]}, "a title no tab carries")
    fake.install(monkeypatch)
    ok, why = terminal.focus(111)
    assert ok
    assert fake.selected == []
    assert fake.focused == [1]
    assert why.startswith("focused the window, not the tab")
    # The SESSION's tab is named, never the window title (= the active tab).
    assert why.endswith("look for the tab: a title no tab carries")
    assert "active tab title" not in why


def test_focus_fallback_names_nothing_when_the_title_is_unreadable(monkeypatch):
    fake = FakeTerminal({1: ["conhost has no tabs"]}, "")
    fake.install(monkeypatch)
    ok, why = terminal.focus(111)
    assert ok
    assert why.startswith("focused the window, not the tab")
    assert "look for the tab" not in why


def test_focus_never_uses_hints_over_a_readable_title(monkeypatch):
    fake = FakeTerminal({1: OWNER_TABS}, "renamed by the owner")
    fake.install(monkeypatch)
    ok, why = terminal.focus(111, ("platform agents",))
    assert ok
    assert fake.selected == []
    assert "look for the tab: renamed by the owner" in why


def test_focus_uses_hints_when_the_console_title_cannot_be_read(monkeypatch):
    fake = FakeTerminal({1: OWNER_TABS}, "")
    fake.install(monkeypatch)
    ok, why = terminal.focus(111, ("platform agents",))
    assert ok, why
    assert fake.selected == [(1, 3, OWNER_TABS[3])]


def test_focus_reports_a_refused_foreground_even_after_switching_tabs(monkeypatch):
    live = OWNER_TABS[5]
    fake = FakeTerminal({1: OWNER_TABS}, live)
    fake.install(monkeypatch)
    fake.focus_ok = False
    ok, why = terminal.focus(111)
    assert not ok
    assert "switched to the tab" in why


def test_focus_with_no_session_is_refused():
    ok, why = terminal.focus(0)
    assert not ok
    assert "no session process" in why


# ── "Type it into that terminal" must never press Enter ─────────────────────────


def test_type_button_lands_a_draft_without_enter(monkeypatch):
    from adk.decisions import popup

    calls: list[dict] = []

    def fake_type(pid, text, **kwargs):
        calls.append({"pid": pid, "text": text, **kwargs})
        return True, f"typed {len(text)} chars into the session"

    monkeypatch.setattr(terminal, "type_into_console", fake_type)

    class Reply:
        def delete(self, *_a):
            pass

    class Fake:
        card = type("C", (), {"source": type("S", (), {"session_pid": 77})()})()
        reply = Reply()
        _reply_empty = False
        flashed: list = []

        def _reply_text(self):
            return "run the tests"

        def _flash(self, message, colour=None):
            self.flashed.append(message)

    fake = Fake()
    popup.CardWindow._type_into_terminal(fake)
    assert calls == [{"pid": 77, "text": "run the tests", "submit": False}]
    assert "press Enter" in fake.flashed[-1]
    assert terminal.key_payload("run the tests", submit=False) == "run the tests"


def test_focus_raises_through_the_callers_hook(monkeypatch):
    live = OWNER_TABS[5]
    fake = FakeTerminal({1: OWNER_TABS}, live)
    fake.install(monkeypatch)
    raised: list[int] = []
    ok, why = terminal.focus(111, (), lambda hwnd: raised.append(hwnd) or True)
    assert ok, why
    assert raised == [1]
    assert fake.focused == []  # the default focuser was NOT used
