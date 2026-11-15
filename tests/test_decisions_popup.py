"""adk.decisions.popup -- the fixed frame contract and the list-first view.

Pins the four things the owner reported on 2026-11-07: the cards were all
different sizes, there was no way to list them, the Next arrow kept moving, and
Esc threw the window away instead of stepping back. Every test here drives a
REAL Tk window (withdrawn, or mapped off-screen) because the defects were in
the widget tree, not in the arithmetic.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time

import pytest

# exc_type=ImportError: a Python built with Tk support on a host without libtk
# raises ImportError (not ModuleNotFoundError), which pytest 9 no longer skips.
tkinter = pytest.importorskip("tkinter", exc_type=ImportError)

from adk.decisions.popup import (  # noqa: E402
    MAX_HEIGHT,
    NOT_NOW_SECONDS,
    SCREEN_FRACTION,
    WIDTH,
    CardWindow,
)
from adk.decisions.store import (  # noqa: E402
    DecisionCard,
    DecisionOption,
    DecisionSource,
    DecisionStore,
)


def _tk_available() -> bool:
    """Whether a window can be created here, WITHOUT creating (and tearing down)
    a throwaway Tk root at import time: that leaves the next real root unable to
    find Tcl's library on Windows."""
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


pytestmark = pytest.mark.skipif(not _tk_available(), reason="no Tk display")

#: One Tcl interpreter for the whole module. Creating and destroying a fresh
#: ``tk.Tk()`` per test leaks Tcl's library state on Windows -- by the tenth root
#: ``source .../tcl8.6/init.tcl`` fails with "No error", which reads as a broken
#: Tk install and is not the code under test.
@pytest.fixture(scope="module")
def _tk_app():
    root = tkinter.Tk()
    root.withdraw()
    yield root
    with contextlib.suppress(Exception):   # teardown is best effort
        root.destroy()


@pytest.fixture(autouse=True)
def _card_surfaces(_tk_app, monkeypatch):
    """``CardWindow`` asks for ``tk.Tk()``; hand it a Toplevel of the shared app,
    so every test still gets its own window it can destroy and re-create."""
    monkeypatch.setattr(tkinter, "Tk", lambda *a, **k: tkinter.Toplevel(_tk_app))


def _alive(win) -> bool:
    """Tk raises rather than returning 0 once the root is gone."""
    try:
        return bool(win.root.winfo_exists())
    except Exception:  # noqa: BLE001 - TclError on a destroyed application
        return False


def _destroy(win) -> None:
    # Already destroyed BY the code under test is the normal case, not an error.
    with contextlib.suppress(Exception):
        win.root.destroy()


def _options() -> list[DecisionOption]:
    return [
        DecisionOption(key="a", label="Accept", consequence="does a thing",
                       recommended=True),
        DecisionOption(key="b", label="Decline"),
    ]


def _card(store: DecisionStore, title: str, *, kind: str = "decision",
          urgency: str = "normal", created_at: float | None = None,
          session_id: str = "sess", session_pid: int = 0) -> DecisionCard:
    kwargs = dict(
        id="", title=title, kind=kind, urgency=urgency,
        source=DecisionSource(session_id=session_id, session_pid=session_pid),
    )
    if kind == "credential":
        kwargs.update(credential_format="custom", credential_description="need it",
                      secret_name="TEST_SECRET")
    else:
        kwargs.update(default_key="a", options=_options())
    if created_at is not None:
        kwargs["created_at"] = created_at
    return store.create(DecisionCard(**kwargs))


@pytest.fixture
def store(tmp_path):
    return DecisionStore(tmp_path / "cards")


@pytest.fixture
def window(store):
    _card(store, "the only card")
    win = CardWindow(store, headless=True)
    yield win
    _destroy(win)


def _texts(widget) -> str:
    out = []

    def walk(node):
        for child in node.winfo_children():
            try:
                text = child.cget("text")
            except Exception:  # noqa: BLE001 - not every widget has -text
                text = ""
            if text:
                out.append(str(text))
            walk(child)

    walk(widget)
    return "\n".join(out)


# ── the fixed frame ───────────────────────────────────────────────────────────

def test_the_frame_size_is_chosen_from_the_screen_not_the_card(store):
    _card(store, "short")
    win = CardWindow(store, headless=True)
    screen_h = win.root.winfo_screenheight()
    assert win._size == (WIDTH, min(MAX_HEIGHT, int(screen_h * SCREEN_FRACTION)))
    assert win._size[0] == 660
    _destroy(win)


def test_six_cards_and_an_answer_leave_the_frame_and_next_exactly_in_place(store):
    """Owner: "they are all different sizes ... the arrow that kept moving"."""
    for index, title in enumerate(
            ["one", "a much longer title " * 6, "three", "four " * 30, "five", "six"]):
        _card(store, title, urgency=("normal", "high", "critical", "normal",
                                     "low", "normal")[index])
    win = CardWindow(store, headless=True)
    # Map OFF-SCREEN so Tk's geometry managers run for real without putting
    # anything on the owner's desktop.
    win.root.geometry(f"{WIDTH}x760+-4000+0")
    win.root.deiconify()
    win.root.update()

    def snap():
        next_button = win._controls["next"]["widget"]
        return ((win.root.winfo_width(), win.root.winfo_height(),
                 win.root.winfo_x(), win.root.winfo_y()),
                (next_button.winfo_rootx(), next_button.winfo_rooty(),
                 next_button.winfo_width(), next_button.winfo_height()))

    base = snap()
    assert base[0][0] == WIDTH
    assert base[1][2] > 1 and base[1][3] > 1, "the Next control was never laid out"
    for card in win._refresh_queue():
        win._open_card(card.id)
        win.root.update()
        assert snap() == base, "the frame or a header control moved between cards"
    win._answer_card(win._refresh_queue()[0], "a")
    win.root.update()
    assert snap() == base
    win._show_list()
    win.root.update()
    assert snap() == base
    win.root.withdraw()
    _destroy(win)


def test_render_never_resizes_the_window(store, window):
    """``_render`` may repaint; it may not call geometry()."""
    calls = []
    window.root.geometry = lambda *a, **k: calls.append(a)  # type: ignore[assignment]
    window._render()
    assert calls == []


def test_a_disabled_control_dims_in_place_instead_of_disappearing(store, window):
    next_button = window._controls["next"]["widget"]
    window._set_control("next", False)
    window.root.update_idletasks()
    assert window._controls["next"]["widget"] is next_button
    assert next_button.winfo_manager() == "pack"
    assert next_button.cget("text") == window._controls["next"]["glyph"]
    window._set_control("next", True)
    assert next_button.winfo_manager() == "pack"


def test_the_header_text_drags_the_frameless_window(store, window):
    """The title and count labels cover almost the whole header, so they MUST drag.

    Owner, 2026-11-08: "I can't even move them". The window has no OS frame, and
    only the bar Frame and the stripe were bound, so every click on the header
    landed on a Label that did nothing. The controls keep their own click.
    """
    for label in (window._title, window._count):
        assert "_drag_start" in label.bind("<Button-1>")
        assert "_drag_move" in label.bind("<B1-Motion>")
        assert "_drag_end" in label.bind("<ButtonRelease-1>")
    for name, control in window._controls.items():
        click = control["widget"].bind("<Button-1>")
        assert click and "_drag_start" not in click, name


# ── list first ────────────────────────────────────────────────────────────────

def test_the_default_view_is_the_list_of_every_open_card(store):
    for title in ("alpha", "beta", "gamma"):
        _card(store, title)
    win = CardWindow(store, headless=True)
    assert win._mode == "list"
    rendered = _texts(win._content_frame)
    for title in ("alpha", "beta", "gamma"):
        assert title in rendered
    assert "Needs you (3)" in rendered
    assert win._count.cget("text") == "3 open · 3 need you"
    _destroy(win)


def test_needs_you_is_capped_with_a_real_more_row(store):
    for index in range(7):
        _card(store, f"card {index}")
    win = CardWindow(store, headless=True)
    rendered = _texts(win._content_frame)
    assert "Needs you (7)" in rendered
    assert "2 more" in rendered
    # The cap is display only: the cursor still walks the whole queue.
    assert len(win._view()["open"]) == 7
    _destroy(win)


def test_a_protected_card_never_falls_into_the_old_group(store):
    old = time.time() - 20 * 86400
    _card(store, "ancient credential", kind="credential", created_at=old)
    _card(store, "ancient decision", created_at=old)
    win = CardWindow(store, headless=True)
    view = win._view()
    assert [c.title for c in view["needs_you"]] == ["ancient credential"]
    assert [c.title for c in view["older"]] == ["ancient decision"]
    _destroy(win)


def test_opening_a_card_expands_it_in_place(store):
    first = _card(store, "alpha")
    _card(store, "beta")
    win = CardWindow(store, headless=True)
    win._open_card(first.id)
    assert win._mode == "card"
    assert win.card.id == first.id
    assert "Accept" in _texts(win._content_frame)
    _destroy(win)


def test_esc_collapses_then_closes_and_answers_nothing(store):
    card = _card(store, "one")
    _card(store, "two")
    win = CardWindow(store, headless=True)
    win._open_card(card.id)
    win._on_escape()
    assert win._mode == "list"
    assert _alive(win)
    assert store.get(card.id).status == "open"
    win._on_escape()
    assert not _alive(win)
    assert store.get(card.id).status == "open"


# ── not now / dismiss stale ───────────────────────────────────────────────────

def test_not_now_is_a_local_hide_that_never_touches_the_store(store):
    card = _card(store, "in the way")
    win = CardWindow(store, headless=True)
    win._not_now()
    assert store.get(card.id).status == "open"
    assert card.id not in {c.id for c in win._refresh_queue()}
    assert win._hidden[card.id] > time.time() + NOT_NOW_SECONDS - 60
    assert win._hidden[card.id] <= time.time() + NOT_NOW_SECONDS
    _destroy(win)


def test_dismiss_all_stale_skips_credential_and_blocked(store):
    old = time.time() - 9 * 86400
    stale = _card(store, "stale decision", created_at=old)
    credential = _card(store, "stale credential", kind="credential", created_at=old)
    blocked = _card(store, "stale blocked", kind="blocked", created_at=old)
    fresh = _card(store, "fresh one")

    win = CardWindow(store, headless=True)
    win._dismiss_all_stale()
    if win._dismiss_job is not None:
        win.root.after_cancel(win._dismiss_job)
        win._dismiss_job = None
    guard = 0
    while win._dismiss_remaining and guard < 20:
        guard += 1
        win._dismiss_step()
        if win._dismiss_job is not None:
            win.root.after_cancel(win._dismiss_job)
            win._dismiss_job = None

    assert store.get(stale.id).status == "cancelled"
    assert store.get(credential.id).status == "open"
    assert store.get(blocked.id).status == "open"
    assert store.get(fresh.id).status == "open"
    _destroy(win)


def test_cancel_stops_the_dismissal_after_the_card_in_flight(store):
    old = time.time() - 9 * 86400
    cards = [_card(store, f"stale {i}", created_at=old) for i in range(3)]
    win = CardWindow(store, headless=True)
    win._dismiss_all_stale()
    if win._dismiss_job is not None:
        win.root.after_cancel(win._dismiss_job)
        win._dismiss_job = None
    win._dismiss_step()          # cancels exactly one, then reschedules
    if win._dismiss_job is not None:
        win.root.after_cancel(win._dismiss_job)
        win._dismiss_job = None
    win._cancel_dismiss()
    statuses = sorted(store.get(c.id).status for c in cards)
    assert statuses.count("cancelled") == 1
    assert statuses.count("open") == 2
    _destroy(win)


# ── keyboard ──────────────────────────────────────────────────────────────────

def test_arrows_clamp_instead_of_wrapping(store):
    first = _card(store, "first")
    second = _card(store, "second")
    win = CardWindow(store, headless=True)
    win._open_card(second.id)
    win._move(1)
    assert win.card.id == second.id, "the queue wrapped from the bottom to the top"
    win._move(-1)
    assert win.card.id == first.id
    _destroy(win)


def test_number_keys_answer_the_cursor_row_in_the_list(store):
    card = _card(store, "pick me")
    win = CardWindow(store, headless=True)
    win._cursor_id = card.id
    win._pick_index(2)
    answered = store.get(card.id)
    assert answered.status == "answered"
    assert answered.answer == "b"
    _destroy(win)
