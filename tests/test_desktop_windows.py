"""adk.desktop.windows against a fake backend -- user32 is never touched."""

import pytest

from adk.desktop import windows as dw
from adk.desktop.layout import Rect

WORK = Rect(0, 0, 1920, 1040)
FULL = Rect(0, 0, 1920, 1080)


class FakeBackend:
    def __init__(self, wins, elevated=None, fail_move=None):
        self.wins = {w.hwnd: w for w in wins}
        self.order = [w.hwnd for w in wins]
        self.elevated = elevated or {}
        self.fail_move = fail_move  # (hwnd, errno)
        self.calls = []
        self._err = 0

    def enum_top_level(self):
        return list(self.order)

    def window_info(self, hwnd):
        w = self.wins.get(hwnd)
        if w is None:
            return None
        return dw.WindowInfo(**{**w.__dict__})

    def monitors(self):
        return [dw.MonitorInfo(0, WORK, FULL, True),
                dw.MonitorInfo(1, Rect(-1920, 0, 1920, 1040), Rect(-1920, 0, 1920, 1080), False)]

    def set_window_pos(self, hwnd, x, y, w, h, activate=False):
        self.calls.append((hwnd, x, y, w, h))
        if self.fail_move and self.fail_move[0] == hwnd:
            self._err = self.fail_move[1]
            return False
        return True

    def is_elevated(self, pid):
        return self.elevated.get(pid, False)

    def last_error(self):
        return self._err


def win(hwnd, title, process="app.exe", rect=Rect(100, 100, 800, 600), **kw):
    base = dict(hwnd=hwnd, pid=hwnd * 10, process=process, title=title, rect=rect,
                visible=True, minimized=False, cloaked=False, taskbar=True, elevated=None)
    base.update(kw)
    return dw.WindowInfo(**base)


@pytest.fixture
def two():
    return FakeBackend([win(1, "Claude Code", "WindowsTerminal.exe"),
                        win(2, "Veil - Edge", "msedge.exe")])


def test_list_windows_drops_conpty_and_cloaked():
    be = FakeBackend([
        win(1, "Real"),
        win(2, ""),                                   # ConPTY ghost: empty title
        win(3, "Tiny", rect=Rect(0, 0, 150, 80)),     # too small to be a frame
        win(4, "Cloaked", cloaked=True),
        win(5, "Hidden", visible=False),
        win(6, "Tool", taskbar=False),
        win(7, "Parked", minimized=True, rect=Rect(-32000, -32000, 160, 28)),
    ])
    titles = [w.title for w in dw.list_windows(backend=be)]
    assert titles == ["Real", "Parked"]
    assert "Tool" in [w.title for w in dw.list_windows(backend=be, taskbar_only=False)]
    assert [w.title for w in dw.list_windows(backend=be, query="real")] == ["Real"]


def test_find_window_ambiguous_refuses():
    be = FakeBackend([win(1, "Notes one"), win(2, "Notes two")])
    with pytest.raises(dw.DesktopError) as exc:
        dw.find_window("notes", backend=be)
    assert exc.value.code == "ambiguous"
    with pytest.raises(dw.DesktopError) as exc:
        dw.find_window("missing", backend=be)
    assert exc.value.code == "not_found"
    assert dw.find_window("Notes one", backend=be).hwnd == 1
    assert dw.find_window("hwnd:2", backend=be).hwnd == 2


def test_arrange_dry_run_calls_no_set_window_pos(two):
    r = dw.arrange(["Claude Code", "Veil"], backend=two)
    assert r["ok"] is True and r["dry_run"] is True and r["moved"] == 0
    assert two.calls == []
    assert len(r["plan"]) == 2


def test_arrange_two_windows_side_by_side_exact_rects(two):
    r = dw.arrange(["Claude Code", "Veil"], gap=0, dry_run=False, backend=two)
    assert r["ok"] and r["moved"] == 2
    assert two.calls == [(1, 0, 0, 960, 1040), (2, 960, 0, 960, 1040)]


def test_gap_keeps_right_edge_exact(two):
    dw.arrange(["Claude Code", "Veil"], gap=8, dry_run=False, backend=two)
    assert two.calls == [(1, 0, 0, 956, 1040), (2, 964, 0, 956, 1040)]
    _, x, _, w, _ = two.calls[-1]
    assert x + w == 1920


def test_monitor_negative_origin(two):
    r = dw.arrange(["Claude Code", "Veil"], monitor=1, gap=0, backend=two)
    assert r["plan"][0]["to"] == {"x": -1920, "y": 0, "w": 960, "h": 1040}


def test_elevated_target_refused_atomically():
    be = FakeBackend([win(1, "Claude Code"), win(2, "Task Manager")], elevated={20: True})
    r = dw.arrange(["Claude Code", "Task Manager"], dry_run=False, backend=be)
    assert r["ok"] is False and r["needs_admin_lane"] is True and r["moved"] == 0
    assert be.calls == []
    assert r["refused"][0]["title"] == "Task Manager"
    assert r["refused"][0]["elevation"] == "elevated"


def test_unknown_elevation_treated_as_elevated():
    be = FakeBackend([win(1, "A"), win(2, "B")], elevated={10: None})
    r = dw.arrange(["A", "B"], dry_run=False, backend=be)
    assert r["ok"] is False and r["needs_admin_lane"] is True
    assert r["refused"][0]["elevation"] == "unknown"
    assert be.calls == []


def test_access_denied_on_move_maps_to_uipi():
    be = FakeBackend([win(1, "A"), win(2, "B")], fail_move=(1, 5))
    with pytest.raises(dw.DesktopError) as exc:
        dw.arrange(["A", "B"], dry_run=False, backend=be)
    assert exc.value.code == "access_denied"
    assert exc.value.needs_admin_lane is True
    be2 = FakeBackend([win(1, "A"), win(2, "B")], fail_move=(2, 87))
    with pytest.raises(dw.DesktopError) as exc:
        dw.arrange(["A", "B"], dry_run=False, backend=be2)
    assert exc.value.code == "move_failed" and not exc.value.needs_admin_lane


def test_activate_reuses_focus_window(two, monkeypatch):
    from adk.decisions import winproc

    raised = []
    monkeypatch.setattr(winproc, "focus_window", lambda h: raised.append(h) or True)
    r = dw.arrange(["Claude Code", "Veil"], dry_run=False, activate=True, backend=two)
    assert raised == [1] and r["activated"] is True
    dw.arrange(["Claude Code", "Veil"], dry_run=True, activate=True, backend=two)
    assert raised == [1]


def test_same_window_twice_refused(two):
    with pytest.raises(dw.DesktopError) as exc:
        dw.arrange(["Claude Code", "hwnd:1"], backend=two)
    assert exc.value.code == "bad_geometry"


def test_not_windows_raises(monkeypatch):
    monkeypatch.setattr(dw, "IS_WINDOWS", False)
    with pytest.raises(dw.DesktopError) as exc:
        dw.list_windows()
    assert exc.value.code == "not_windows"
