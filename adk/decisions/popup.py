"""The decision card as an actual window you can act on.

This replaces the toast, which failed for the reason toasts usually fail: it is a
passive, disposable strip that queues alongside mail and chat, it does not
interrupt, and clicking it does nothing. A notification that competes with
notification noise loses to notification noise.

So this is a **card, not an alert**. It states the ask, shows the measurements
behind it, and puts the options on screen AS BUTTONS — clicking one answers the
card, records the choice, and delivers it to the blocked session's steering
mailbox. The window is the control surface, not an advert for one somewhere else.

Deliberate behaviours:

* **No native title bar.** The window draws its own dark chrome. Tk inherits the
  OS frame, which on Windows is a bright title bar wrapped around a near-black
  card — the one bit of the surface we do not control was the loudest thing on
  screen. The frame is redrawn here (drag, pin, close, resize grip) rather than
  left to the platform, with `AITHER_DECISIONS_CHROME=native` as the escape
  hatch for a window manager that dislikes override-redirect windows.

* **Nothing is truncated.** The first version cut facts at 200 characters and
  option labels at 70, so the card routinely showed a sentence ending in `…`
  with the decisive clause on the wrong side of the ellipsis. A card exists to
  make an ask decidable; a truncated ask is not decidable. The body scrolls
  instead, and the window grows to the tallest it can be on this screen.

* **The owner can TYPE.** Buttons answer a closed question, and the owner's real
  answer is often "neither — do this instead". The reply box steers the raising
  session (see ``adk.decisions.steerback``) without closing the card, and text
  left in the box when an option is clicked rides along as the answer note.

* **It can reach the terminal.** The card names a session; the controls act on
  it — focus its window, open a terminal at its directory, or type into it. What
  each control can actually do is labelled BEFORE it is clicked
  (``adk.decisions.terminal``), never discovered by clicking a dead button.

* **Graded focus.** ``critical``/``high`` take focus, because that is the whole
  point of the tier. ``normal`` appears on top WITHOUT stealing focus, so a card
  raised while you are mid-sentence cannot eat the sentence — the keystroke-theft
  failure of quality gate 1t, which this repo has already been bitten by.

* **Keyboard first.** ``1``–``9`` pick an option, ``Enter`` takes the
  recommendation (on a credential card it opens the card's masked field in the
  awdesk Inbox), ``Ctrl+Enter`` sends the reply box, ``Esc`` snoozes,
  ``Ctrl+←/→`` walks the queue. Every binding is suppressed while the caret is in
  the reply box, so typing "1. do the thing" cannot answer the card.

* **stdlib tkinter, never PyQt.** Same rule as ``secure-human-input``: the
  surface that unblocks a human must not be able to fail on a missing dependency.

* **It is a VIEW over the store.** The window never holds the only copy of
  anything. Close it without answering and the card is still open everywhere
  else; answer it here and every other surface sees the answer.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Optional

from adk.decisions.quiet import is_quiet
from adk.decisions.store import (
    CLOSED_STATUSES,
    DecisionCard,
    DecisionStore,
    queue_order,
)

#: While quiet (Do-not-disturb, or a full-screen game/app in front) a window that
#: was about to appear stays withdrawn and re-asks this often. Owner, 2026-09-23:
#: "cards popping up on my main screen while im playing games".
QUIET_POLL_MS = 3000

# ── palette (matches the AitherShell app in AitherVeil) ─────────────────────────

BG = "#07070f"
PANEL = "#0d0d1c"
CHROME = "#0a0a16"
EDGE = "#1e1e38"
TEXT = "#d7d7e6"
SOFT = "#a8aec2"
MUTED = "#6b7280"
ACCENT = "#38bdf8"
ACCENT_DIM = "#0e3a52"
GOLD = "#fbbf24"
RED = "#f87171"
GREEN = "#34d399"

URGENCY_COLOUR = {
    "low": MUTED,
    "normal": ACCENT,
    "high": GOLD,
    "critical": RED,
}

KIND_HEADER = {
    "decision": "DECISION NEEDED",
    "blocked": "BLOCKED ON YOU",
    "info": "YOU SHOULD KNOW",
    # A credential card carried no header of its own, so it announced itself
    # as "DECISION NEEDED" while offering nothing to decide — see _content().
    "credential": "SECRET NEEDED",
}

UI = "Segoe UI"
MONO = "Consolas"

#: The card is wide enough for a full sentence of consequence text without
#: wrapping every option onto four lines, and narrow enough not to cover a
#: terminal. The HEIGHT is chosen ONCE per session (see ``MAX_HEIGHT``) instead
#: of measured from each card's content: a frame that grows and shrinks per card
#: is what made the Next control move out from under the pointer between clicks
#: (owner, 2026-11-07: "had to manually click the arrow that kept moving").
WIDTH = 660
#: Tallest the frame ever gets; on a small screen it is 82% of the display.
MAX_HEIGHT = 760
SCREEN_FRACTION = 0.82
#: Fixed pixel heights of the chrome. Every slot is built once and only its text
#: changes, so a control keeps its x/y for the life of the window.
STRIPE_H = 3
HEADER_H = 44
FOOTER_H = 30
ROW_H = 28
SCROLLBAR_W = 12
#: Widths (in characters) of the header controls. Fixed so that "List" growing
#: or "Next" disabling cannot reflow its neighbours.
CONTROL_WIDTH = {"list": 6, "prev": 3, "next": 3, "pin": 3, "close": 3}
#: "Not now" is a LOCAL, per-surface hide. It never writes to the store: the card
#: stays open on every other surface and on this one after the window closes.
NOT_NOW_SECONDS = 4 * 3600
#: Rows rendered per expanded context section. The harvester caps at 40; this
#: caps what is drawn, so one section cannot bury the other five.
SECTION_ROWS = 12
REPLY_PLACEHOLDER = (
    "Type an instruction and press Ctrl+Enter — steers the session, keeps the card open"
)


def _fmt_left(seconds: Optional[float]) -> str:
    if seconds is None:
        return "no deadline — it waits for you"
    if seconds <= 0:
        return "deadline passed — default applying"
    if seconds < 60:
        return f"default in {int(seconds)}s"
    if seconds < 3600:
        return f"default in {int(seconds // 60)}m"
    return f"default in {int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def use_native_chrome() -> bool:
    """Draw our own frame unless the owner (or a hostile WM) asked otherwise."""
    return os.getenv("AITHER_DECISIONS_CHROME", "").strip().lower() == "native"


def _apply_dark_titlebar(root) -> None:
    """Make the NATIVE title bar dark, for the ``chrome=native`` path.

    Only reachable when custom chrome is off. It is a cosmetic call into DWM and
    it fails on older Windows builds, so it is guarded — but not silently: a
    failure means the bright bar is back, which the owner will see anyway.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # 20 on 20H1+, 19 on earlier builds
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
    except (OSError, AttributeError) as exc:
        sys.stderr.write(f"[decision-card] dark title bar unavailable: {exc}\n")


class CardWindow:
    """ONE window over the whole queue of open cards.

    Deliberately not one-window-per-card. A burst of cards used to mean a burst
    of windows fighting for the same corner of the screen; now the window is a
    view that re-renders, so answering advances to the next card in place and
    ``Ctrl+→`` skips one without answering it.
    """

    def __init__(
        self,
        store: DecisionStore,
        *,
        start_id: str = "",
        card: Optional[DecisionCard] = None,
        headless: bool = False,
    ) -> None:
        import tkinter as tk

        self.tk = tk
        #: Build the window without ever putting it on the owner's screen.
        #: The self-test drives a REAL Tk window — that is the point, it is what
        #: catches a widget option tkinter rejects — but it used to withdraw the
        #: root only AFTER the constructor had rendered, lifted and (for a `high`
        #: urgency card) called focus_force(). So every run flashed a window on
        #: the desktop and ate the owner's keystrokes: reported live on
        #: 2026-08-10 as "one popup then it completely disappeared immediately",
        #: twice, while this file's own tests were running. That is the
        #: focus-stealing class quality gate 1t (STW001) and DC007 exist to
        #: stop, produced by the checker for the surface those rules protect —
        #: and the skill tells people to run it, so it was everyone's desktop.
        self._headless = headless
        self.store = store
        self.answered: Optional[str] = None
        self.handled = 0
        self._injected = [card] if card is not None else []
        self._pinned = False
        self._status: str = ""
        self._status_colour: str = MUTED
        self._reply_focused = False
        self._reply_empty = True
        #: "list" (the default), "card" (one card expanded IN PLACE) or
        #: "explore". One frame, three views -- never a second window.
        self._mode = "card" if start_id else "list"
        self._context: dict = {}
        #: Which row the keyboard cursor is on in the list view, by card id.
        self._cursor_id = ""
        #: The "Older than N days" group starts collapsed and is only expanded
        #: by the owner (or by clicking its header).
        self._older_open = False
        #: The Needs-you cap; "L" lifts it for this session. Never a silent drop:
        #: the "N more" row is drawn from the true total either way.
        self._needs_cap = 5
        #: "Not now" is a LOCAL hide: id -> time.time() when it comes back. It is
        #: never written to the store, so the card stays open on every other
        #: surface -- closing a surface must not look like an answer.
        self._hidden: dict[str, float] = {}
        #: The dismissal run, its progress and its Cancel flag. It walks the
        #: store one audited cancel at a time via after(), so the window stays
        #: responsive and says how far it has got.
        self._dismiss_remaining: list = []
        self._dismiss_total = 0
        self._dismiss_cancel = False
        self._dismiss_job = None
        #: Whether the window WANTS to be above everything (on by default; the
        #: pin button owns it). Quiet overrides it for an unpinned window, so a
        #: game started while the card is open is not covered by it.
        self._topmost = True
        self._topmost_applied: Optional[bool] = None
        #: Non-empty while the window is HELD: it was due to appear while quiet,
        #: so it stays withdrawn until quiet ends. The cards are untouched.
        self._held_reason = ""
        self._hold_job = None

        self.queue: list[DecisionCard] = self._refresh_queue()
        self.index = 0
        if start_id:
            for position, entry in enumerate(self.queue):
                if entry.id == start_id:
                    self.index = position
                    break
        if not self.queue:
            raise RuntimeError("no open cards to show")
        self._cursor_id = self._mode == "card" and self.card.id or self.queue[0].id

        root = tk.Tk()
        self.root = root
        if self._headless:
            # BEFORE any render, lift or focus_force — withdrawing afterwards is
            # what made the old self-test flash a window and take focus.
            root.withdraw()
        else:
            # Same rule, same moment: BEFORE any render or lift. Whoever spawned
            # this window (a raise, the desk, a `show`), it does not land on top
            # of a game -- it waits, withdrawn, and appears when quiet ends.
            quiet, why = is_quiet()
            if quiet:
                root.withdraw()
                self._held_reason = why or "quiet"
        root.title(f"AitherOS · {KIND_HEADER.get(self.card.kind, 'DECISION')}")
        root.configure(bg=BG)
        self._apply_topmost(bool(self._held_reason))
        if use_native_chrome():
            _apply_dark_titlebar(root)
        else:
            root.overrideredirect(True)

        # The FRAME is built once. Every later render replaces text inside fixed
        # slots, so a control keeps its x/y for the life of the window -- the
        # defect was a per-card geometry() recompute that moved Next between
        # clicks (owner, 2026-11-07: "the arrow that kept moving").
        self._build_frame()
        self._size = self._choose_size()

        self._bind_keys()
        self._place()
        self._render()
        if self._held_reason:
            self._hold_job = root.after(QUIET_POLL_MS, self._release_when_not_quiet)

    # ── quiet ─────────────────────────────────────────────────────────────────

    def _apply_topmost(self, quiet: bool) -> None:
        """Above everything only when wanted AND not quiet -- or when pinned."""
        want = bool(self._pinned or (self._topmost and not quiet))
        if want != self._topmost_applied:
            self.root.attributes("-topmost", want)
            self._topmost_applied = want

    def _toggle_pin(self) -> None:
        """Keep the window above everything (or stop). Survives quiet.

        The glyph is a FIXED-WIDTH slot like every other control: it changes
        ``◆``/``◇`` in place, so a pin never moves the buttons beside it.
        """
        self._pinned = not self._pinned
        # Unpin used to be `True if self._pinned else True` -- a no-op that left
        # the window above everything while saying it was unpinned.
        self._topmost = self._pinned
        entry = self._controls.get("pin")
        if entry is not None:
            entry["widget"].configure(text="◆" if self._pinned else "◇")
        self._apply_topmost(is_quiet()[0])
        self._flash("pinned — stays above everything" if self._pinned
                    else "unpinned — other windows can cover it", MUTED)

    def _release_when_not_quiet(self) -> None:
        """Poll while held; show the window once quiet ends. Never answers anything."""
        self._hold_job = None
        quiet, why = is_quiet()
        if quiet:
            self._held_reason = why or self._held_reason
            self._hold_job = self.root.after(QUIET_POLL_MS, self._release_when_not_quiet)
            return
        held_for = self._held_reason
        self._held_reason = ""
        waiting = len(self.queue)
        if not self._status:
            self._status = (f"held while {held_for} — "
                            f"{waiting} decision{'s' if waiting != 1 else ''} waiting")
            self._status_colour = GOLD
        self._apply_topmost(False)
        self.root.deiconify()
        self._rerender_preserving_reply()

    # ── queue ─────────────────────────────────────────────────────────────────

    @property
    def card(self) -> DecisionCard:
        return self.queue[min(self.index, len(self.queue) - 1)]

    def _refresh_queue(self) -> list[DecisionCard]:
        """Open cards in queue order (store.rank), re-read from the store on every render.

        Re-reading is what lets one window absorb cards raised while it is up —
        the alternative (snapshot at startup) is how a second window used to get
        spawned for a card that arrived thirty seconds later.

        Cards hidden with "Not now" are filtered HERE, from a local dict -- the
        store is never told, so the card stays open on every other surface.
        """
        try:
            cards = self.store.list()
        except OSError:
            cards = []
        if self._injected:
            known = {c.id for c in cards}
            cards = [c for c in self._injected if c.id not in known] + cards
        now = time.time()
        for card_id, until in list(self._hidden.items()):
            if until <= now:
                self._hidden.pop(card_id, None)
        if self._hidden:
            cards = [c for c in cards if c.id not in self._hidden]
        # One ordering for every surface: credential/blocked asks lead, then
        # urgency; fresh cards before stragglers; oldest first within a tier.
        # Measured 2026-10-07: the queue opened on a 27-day-old card every time.
        return queue_order(cards)

    # ── keyboard ──────────────────────────────────────────────────────────────

    def _typing(self) -> bool:
        """True when the caret is in the reply box.

        Without this, typing "1" into a reply answers the card — the accelerator
        would fire under the text widget. Every root binding checks it first.

        The state is tracked from the reply box's own FocusIn/FocusOut bindings
        rather than read from ``focus_get()``. ``focus_get()`` returns None for
        an unmapped window and raises for a foreign one, so a check built on it
        answers "not typing" in exactly the situations where it is least sure —
        and an accelerator firing under the caret eats the sentence, which is
        the failure this method exists to prevent.
        """
        return bool(getattr(self, "_reply_focused", False))

    def _bind_keys(self) -> None:
        root = self.root
        root.bind("<Escape>", self._on_escape)
        root.bind("<Return>", self._on_return)
        root.bind("<Control-e>", lambda _e: (self._close_explore()
                                            if self._mode == "explore"
                                            else self._open_explore()))
        root.bind("<Control-Right>", lambda _e: (self._move(1), "break")[1])
        root.bind("<Control-Left>", lambda _e: (self._move(-1), "break")[1])
        # Arrows and j/k walk the list (and the queue inside an expanded card).
        for key, delta in (("<Down>", 1), ("<Up>", -1), ("j", 1), ("k", -1)):
            root.bind(key, lambda _e, d=delta: (self._key_move(d), "break")[1])
        # n = "Not now": a LOCAL 4h hide that never touches the store.
        root.bind("n", lambda _e: (self._key_not_now(), "break")[1])
        # L lifts the Needs-you cap for this session; the true total stays said.
        root.bind("l", lambda _e: (self._key_show_all(), "break")[1])
        for index in range(1, 10):
            root.bind(str(index), lambda _e, i=index: self._pick_index(i))

    def _key_move(self, delta: int) -> None:
        if self._typing():
            return
        self._move(delta)

    def _key_not_now(self) -> None:
        if self._typing():
            return
        self._not_now()

    def _key_show_all(self) -> None:
        if self._typing():
            return
        self._show_all()

    def _on_escape(self, _event=None):
        """Esc is TWO stages: collapse the card, then close the window.

        Neither stage answers, snoozes or hides a card. The first version
        destroyed the window on one Esc, which read as "I dealt with it" while
        the ask was still open.
        """
        if self._typing():
            self.root.focus_set()          # leave the box, keep the card open
            return "break"
        if self._mode != "list":
            self._show_list()
            return "break"
        self._snooze()
        return "break"

    def _on_return(self, _event=None):
        if self._typing():
            return None                    # a newline in the reply box
        if self._mode == "list":
            card = self._cursor_card()
            if card is not None:
                self._open_card(card.id)
            return "break"
        if self._is_credential():
            # A credential card has no recommendation to take; Enter opens
            # its masked field in the desk, the card's one primary action.
            self._open_in_desk()
            return "break"
        self._take_recommended()
        return "break"

    # ── layout ────────────────────────────────────────────────────────────────

    def _choose_size(self) -> tuple[int, int]:
        """The ONE frame size for this session: 660 x min(760, 82% of the screen).

        Chosen against the SCREEN, never against a card's content. A frame that
        grew to fit the tallest card and shrank back moved every control below
        the body between renders -- the Next arrow that kept moving under the
        pointer (owner, 2026-11-07).
        """
        try:
            screen_h = self.root.winfo_screenheight()
        except Exception:  # noqa: BLE001 - a withdrawn Tk still answers this
            screen_h = 1080
        return WIDTH, max(320, min(MAX_HEIGHT, int(screen_h * SCREEN_FRACTION)))

    def _build_frame(self) -> None:
        """Build the fixed chrome ONCE: stripe, header slots, body, footer."""
        tk = self.tk
        self.body_holder = tk.Frame(self.root, bg=BG)
        self.body_holder.pack(fill="both", expand=True)

        outer = tk.Frame(self.body_holder, bg=EDGE, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG)
        inner.pack(fill="both", expand=True)
        self._inner = inner

        # Urgency stripe: RECOLOURED per card, in the SAME frame -- a critical
        # card is louder without being a different shape.
        self.stripe = tk.Frame(inner, bg=ACCENT, height=STRIPE_H)
        self.stripe.pack(fill="x")
        self.stripe.pack_propagate(False)

        self._build_header(inner)
        self._build_footer(inner)
        self._build_body(inner)

    def _build_header(self, parent) -> None:
        """Fixed slots: title (clips in place) | count | [List][Prev][Next][Close].

        The controls are created once with fixed character widths and never
        re-created, so a disable or a changed count cannot move them: that is
        the whole contract of this header.
        """
        tk = self.tk
        bar = tk.Frame(parent, bg=CHROME, height=HEADER_H, padx=12)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        bar.grid_propagate(False)
        bar.columnconfigure(0, weight=1)
        bar.rowconfigure(0, weight=1)

        self._title = tk.Label(bar, text="", bg=CHROME, fg=TEXT, anchor="w",
                               font=(UI, 11, "bold"))
        self._title.grid(row=0, column=0, sticky="ew")
        self._count = tk.Label(bar, text="", bg=CHROME, fg=MUTED, width=18,
                               font=(UI, 9), anchor="e")
        self._count.grid(row=0, column=1, sticky="e", padx=(8, 10))

        controls = tk.Frame(bar, bg=CHROME)
        controls.grid(row=0, column=2, sticky="e")
        self._controls: dict[str, dict] = {}
        for name, glyph, command in (
            ("list", "List", self._show_list),
            ("prev", "‹", lambda: self._move(-1)),
            ("next", "›", lambda: self._move(1)),
            ("pin", "◇", self._toggle_pin),
            ("close", "✕", self._snooze),
        ):
            widget = tk.Label(controls, text=glyph, width=CONTROL_WIDTH[name],
                              bg=CHROME, fg=MUTED, font=(UI, 9), cursor="hand2")
            widget.pack(side="left", padx=(2, 0), fill="y")
            widget.bind("<Button-1>", lambda _e, n=name: self._control(n))
            widget.bind("<Enter>", lambda _e, n=name: self._hover(n, True))
            widget.bind("<Leave>", lambda _e, n=name: self._hover(n, False))
            self._controls[name] = {"widget": widget, "command": command,
                                    "glyph": glyph, "enabled": True}

        # Drag by the header: there is no OS frame to grab. The position is
        # remembered at the END of a drag, never on every motion event.
        #
        # The title and count labels fill almost the whole bar (the title is
        # sticky ew with weight 1), so a click on the header lands on a LABEL,
        # not the bar -- binding the bar alone left the window impossible to
        # move (owner, 2026-11-08: "I can't even move them"). The control labels
        # keep their own click; only the passive text drags.
        for widget in (bar, self.stripe, self._title, self._count):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)

    def _build_footer(self, parent) -> None:
        """Fixed height. Left = status/timer slot; right = the key hints."""
        tk = self.tk
        foot = tk.Frame(parent, bg=CHROME, height=FOOTER_H, padx=12)
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)
        self.countdown = tk.Label(foot, text="", bg=CHROME, fg=MUTED, font=(UI, 8),
                                  anchor="w")
        self.countdown.pack(side="left")
        self._hint = tk.Label(
            foot, text="↑↓ move · Enter open · 1–9 answer · Esc back",
            bg=CHROME, fg=MUTED, font=(UI, 8), anchor="e")
        self._hint.pack(side="right")

    def _build_body(self, parent) -> None:
        """The ONLY scroll region, with a permanently reserved scrollbar gutter.

        The bar used to pack and unpack itself whenever content crossed the
        canvas height, narrowing the body by 10px mid-render and reflowing every
        line under the pointer. It now always occupies its gutter.
        """
        tk = self.tk
        holder = tk.Frame(parent, bg=BG)
        holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0,
                           width=WIDTH - SCROLLBAR_W - 6)
        bar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview,
                           bg=PANEL, troughcolor=BG, activebackground=EDGE,
                           relief="flat", bd=0, width=SCROLLBAR_W)
        bar.pack(side="right", fill="y")      # ALWAYS mapped: the gutter is reserved
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=BG, padx=16, pady=10)
        window_item = canvas.create_window((0, 0), window=body, anchor="nw",
                                           width=WIDTH - SCROLLBAR_W - 10)
        self._canvas = canvas
        self._scrollbar = bar
        self._scroll_holder = holder
        self._content_frame = body

        def resized(event=None):
            # The embedded frame tracks the canvas WIDTH only (never its height:
            # height is the content's own). A fixed item id is what keeps the
            # text from clipping at the gutter when the frame is created at one
            # width and the canvas settles at another.
            if event is not None and event.widget is canvas:
                canvas.itemconfigure(window_item, width=max(1, event.width))
            canvas.configure(scrollregion=canvas.bbox("all"))

        body.bind("<Configure>", resized)
        canvas.bind("<Configure>", resized)

        def wheel(event):
            try:
                canvas.yview_scroll(-1 * (event.delta // 120), "units")
            except Exception:  # noqa: BLE001 - a destroyed canvas must not raise
                return

        self._wheel = wheel
        for widget in (canvas, body):
            widget.bind("<MouseWheel>", wheel)

    def _render(self) -> None:
        """Repaint the CURRENT view inside the fixed frame.

        NEVER calls geometry(): the frame was sized once in ``_place()``. Every
        slot here is fixed and only its TEXT changes.
        """
        body = self._content_frame
        for child in body.winfo_children():
            child.destroy()

        view = self._view()
        card = self._focus_card()
        accent = URGENCY_COLOUR.get(card.urgency if card is not None else "normal",
                                    ACCENT)
        self.stripe.configure(bg=accent)
        self._update_header(view, card, accent)

        if self._mode == "explore":
            self._explore_content(body, self._wheel)
            if card is not None:
                self._reply_row(body, card)
        elif self._mode == "card" and card is not None:
            self._content(body, card, accent, self._wheel)
            self._reply_row(body, card)
        else:
            self._render_list(body, view)
        self._reset_scroll()
        self._update_footer(card)

        title = (KIND_HEADER.get(card.kind, "DECISION") if card is not None
                 else "DECISIONS")
        self.root.title(f"AitherOS · {title}")
        if not self._headless and not self._held_reason:
            # Never lift or focus while quiet: a re-render (a new card from
            # another session, an answer, a steer) used to jump the window back
            # above a full-screen game every time.
            quiet = is_quiet()[0]
            self._apply_topmost(quiet)
            if not quiet:
                self.root.lift()
                if card is not None and card.urgency in ("high", "critical"):
                    self.root.focus_force()

        # Always poll, deadline or not. The first version only ticked when the
        # card had a deadline, so a card answered from the phone, the cockpit or
        # another terminal left this window sitting there offering buttons for a
        # decision that was already made — and clicking one produced a
        # lost-the-race error instead of just closing.
        self._tick()

    # ── fixed-slot updates ────────────────────────────────────────────────────

    def _focus_card(self) -> Optional[DecisionCard]:
        """The card the expanded views act on, or None in list mode."""
        if self._mode == "list" or not self.queue:
            return None
        return self.card

    def _view(self) -> dict:
        """The shared list/ranking partition for the current queue."""
        from adk.decisions import triage

        return triage.stale_view(self.queue, cap=self._needs_cap)

    def _update_header(self, view: dict, card: Optional[DecisionCard],
                       accent: str) -> None:
        """Write the fixed header slots. Nothing here can resize or reflow."""
        if self._mode == "list":
            title = "Decisions"
        elif self._mode == "explore":
            title = "Context"
        else:
            title = KIND_HEADER.get(card.kind if card else "", "DECISION NEEDED")
        self._title.configure(text=title, fg=accent)
        self._count.configure(
            text=f"{view['total_open']} open · {view['needs_you_total']} need you")
        self._set_control("list", self._mode != "list")
        self._set_control("prev", bool(view["open"]) and self._mode != "list"
                          or len(view["open"]) > 1)
        self._set_control("next", len(view["open"]) > 1)

    def _update_footer(self, card: Optional[DecisionCard]) -> None:
        if self._status:
            self.countdown.configure(text=self._status[:160], fg=self._status_colour)
        elif card is not None:
            self.countdown.configure(text=_fmt_left(card.seconds_left),
                                     fg=GOLD if card.deadline is not None else MUTED)
        else:
            self.countdown.configure(
                text=f"{len(self.queue)} open — Enter opens the first",
                fg=MUTED)

    def _set_control(self, name: str, enabled: bool) -> None:
        """Enable/disable a control IN PLACE: it dims, it never disappears."""
        entry = self._controls.get(name)
        if entry is None or entry["enabled"] == bool(enabled):
            return
        entry["enabled"] = bool(enabled)
        entry["widget"].configure(fg=MUTED if enabled else EDGE)

    def _control(self, name: str) -> None:
        entry = self._controls.get(name)
        if entry is None or not entry["enabled"]:
            return
        if self._typing() and name != "close":
            return
        entry["command"]()

    def _hover(self, name: str, inside: bool) -> None:
        entry = self._controls.get(name)
        if entry is None:
            return
        entry["widget"].configure(
            bg=EDGE if inside else CHROME,
            fg=TEXT if inside and entry["enabled"] else
            (MUTED if entry["enabled"] else EDGE))

    # ── the list view ─────────────────────────────────────────────────────────

    def _render_list(self, body, view: dict) -> None:
        """Every open card as one fixed-height row, ranked by the store's rule.

        The list is the DEFAULT because "there is no way to list them" was the
        owner's complaint: the window used to show one card at a time and the
        only way to see another was to click an arrow, which moved.
        """
        tk = self.tk
        wheel = self._wheel
        ranked = view["open"]
        self._row_ids = {c.id: i for i, c in enumerate(ranked)}

        if not ranked:
            tk.Label(body, text="No open cards.", bg=BG, fg=SOFT,
                     font=(UI, 10), anchor="w").pack(fill="x", pady=6)
            return

        self._group_label(body, f"Needs you ({view['needs_you_total']})", wheel)
        if not view["needs_you"]:
            tk.Label(body, text="  nothing needs you right now", bg=BG, fg=MUTED,
                     font=(UI, 9), anchor="w").pack(fill="x")
        for card in view["needs_you"]:
            self._list_row(body, card, wheel)
        if view["needs_you_more"]:
            more = tk.Label(
                body, text=f"  {view['needs_you_more']} more — press L for the whole "
                           f"queue (the count above is the true total)",
                bg=BG, fg=GOLD, font=(UI, 9), anchor="w", cursor="hand2")
            more.pack(fill="x", pady=(2, 6))
            more.bind("<Button-1>", lambda _e: self._show_all())
            more.bind("<MouseWheel>", wheel)

        count = view["older_count"]
        days = self._stale_days()
        arrow = "▾" if self._older_open else "▸"
        head = tk.Label(
            body,
            text=f"{arrow}  Older than {days:g} days ({count})"
                 + ("  ·  collapsed — click to expand" if count and not self._older_open
                    else ""),
            bg=BG, fg=MUTED, font=(UI, 9, "bold"), anchor="w", cursor="hand2")
        head.pack(fill="x", pady=(10, 2))
        head.bind("<Button-1>", lambda _e: self._toggle_older())
        head.bind("<MouseWheel>", wheel)

        if count and self._older_open:
            for card in view["older"]:
                self._list_row(body, card, wheel)
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=(6, 0))
            self._plain_button(
                row, "Dismiss all stale", self._dismiss_all_stale, side="left",
                hint="cancels each of these in the store, one audited cancel at a "
                     "time; credential and blocked cards are never eligible")
            if self._dismiss_remaining or self._dismiss_job is not None:
                self._plain_button(row, "Cancel", self._cancel_dismiss, side="left",
                                   hint="stop after the card being cancelled now")

    def _group_label(self, body, text: str, wheel) -> None:
        label = self.tk.Label(body, text=text, bg=BG, fg=MUTED,
                              font=(UI, 8, "bold"), anchor="w")
        label.pack(fill="x", pady=(2, 4))
        label.bind("<MouseWheel>", wheel)

    def _list_row(self, body, card: DecisionCard, wheel) -> None:
        """ONE fixed 28px row: urgency dot, title, source, age, recommended key.

        Fixed height and fixed-width trailing slots, so a long title clips in
        place instead of pushing the row's controls sideways.
        """
        tk = self.tk
        cursor = card.id == self._cursor_id
        bg = PANEL if cursor else BG
        row = tk.Frame(body, bg=bg, height=ROW_H)
        row.pack(fill="x", pady=(0, 1))
        row.pack_propagate(False)

        urgency = (card.urgency or "normal").strip().lower()
        dot = tk.Label(row, text="●", bg=bg, fg=URGENCY_COLOUR.get(urgency, ACCENT),
                       font=(UI, 8), width=2)
        dot.pack(side="left", fill="y")

        answer = self._row_answer(row, card, bg)
        age = tk.Label(row, text=_fmt_age(card.age_seconds), bg=bg, fg=MUTED,
                       font=(UI, 8), width=8, anchor="e")
        age.pack(side="right", fill="y", padx=(6, 0))
        age.bind("<MouseWheel>", wheel)

        source = tk.Label(row, text=self._source_label(card), bg=bg, fg=MUTED,
                          font=(UI, 8), width=16, anchor="e")
        source.pack(side="right", fill="y", padx=(6, 0))
        source.bind("<MouseWheel>", wheel)

        title = tk.Label(row, text=card.title, bg=bg,
                         fg=TEXT if cursor else SOFT, font=(UI, 9),
                         anchor="w")
        title.pack(side="left", fill="both", expand=True)

        for widget in (row, dot, title):
            widget.bind("<Button-1>", lambda _e, c=card: self._open_card(c.id))
            widget.bind("<MouseWheel>", wheel)
        if answer is not None:
            answer.bind("<MouseWheel>", wheel)

    def _row_answer(self, parent, card: DecisionCard, bg: str):
        """The recommended answer, one click, fixed 28px: the row's own Enter."""
        key = card.recommended_key()
        text = f"★ {key}" if key else ("Open" if card.kind == "credential" else "Expand")
        button = self.tk.Label(parent, text=text, bg=bg, fg=ACCENT_DIM if key else MUTED,
                               font=(UI, 8), width=8, anchor="center", cursor="hand2")
        button.pack(side="right", fill="y", padx=(6, 0))
        if key:
            button.configure(fg=ACCENT)
            button.bind("<Button-1>",
                        lambda _e, c=card, k=key: self._answer_card(c, k))
        else:
            button.bind("<Button-1>", lambda _e, c=card: self._open_card(c.id))
        return button

    def _source_label(self, card: DecisionCard) -> str:
        source = card.source
        name = (source.agent or source.session_id or "").strip()
        return name[:16]

    def _stale_days(self) -> float:
        from adk.decisions import store as _store

        return _store.stale_after_seconds() / 86400.0

    def _show_all(self) -> None:
        if self._typing():
            return
        self._needs_cap = len(self.queue) or 5
        self._status = ""
        self._render()

    def _toggle_older(self) -> None:
        self._older_open = not self._older_open
        self._render()

    def _open_card(self, card_id: str) -> None:
        """Expand ONE card in place (never a second window)."""
        self.queue = self._refresh_queue()
        if not self.queue:
            self.root.destroy()
            return
        self.index = next((i for i, c in enumerate(self.queue) if c.id == card_id),
                          min(self.index, len(self.queue) - 1))
        self._cursor_id = card_id
        self._mode = "card"
        self._context = {}
        self._status = ""
        self._render()

    def _show_list(self) -> None:
        """Back to the list. Collapsing is NOT answering, snoozing or hiding."""
        self._mode = "list"
        self._context = {}
        self._status = ""
        self._render()

    # ── "Not now" and "Dismiss all stale" ─────────────────────────────────────

    def _not_now(self) -> None:
        """"Not now" = a LOCAL 4h hide. Never a store write.

        The card stays open on every other surface and here again after the
        window closes and reopens, so a surface being in the way can never be
        mistaken for the owner having answered.
        """
        if self._typing():
            return
        card = self._focus_card()
        if card is None and self.queue:
            card = next((c for c in self.queue if c.id == self._cursor_id),
                        self.queue[0])
        if card is None:
            return
        self._hidden[card.id] = time.time() + NOT_NOW_SECONDS
        self._status = (f"“{card.title[:48]}” hidden on THIS surface for 4h — "
                        f"still open everywhere else")
        self._status_colour = MUTED
        self.queue = self._refresh_queue()
        if not self.queue:
            self.root.destroy()
            return
        self._cursor_id = self.queue[0].id
        self._mode = "list"
        self._render()

    def _dismiss_all_stale(self) -> None:
        """Cancel the stale group ONE audited cancel at a time, with progress.

        A silent bulk delete of the owner's queue is not allowed to exist: this
        walks the store, names each card, shows "n of N" and offers Cancel.
        Credential / blocked / blocker cards are not eligible at all.
        """
        from adk.decisions.triage import PROTECTED_KINDS

        stale = [c for c in self._view()["older"]
                 if (c.kind or "").strip().lower() not in PROTECTED_KINDS]
        if not stale:
            self._status = "nothing stale to dismiss"
            self._status_colour = MUTED
            self._render()
            return
        self._dismiss_remaining = list(stale)
        self._dismiss_total = len(stale)
        self._dismiss_cancel = False
        self._status = f"dismissing {len(stale)} stale card(s)… 0 of {len(stale)}"
        self._status_colour = GOLD
        self._render()
        self._dismiss_job = self.root.after(60, self._dismiss_step)

    def _cancel_dismiss(self) -> None:
        # Take the job BEFORE cancelling: the flag must survive whatever Tk says.
        job, self._dismiss_job = self._dismiss_job, None
        if job is not None:
            # ``suppress``, not ``except: pass``: an already-fired job (ValueError)
            # or a destroyed interpreter (RuntimeError) means there is nothing to
            # stop -- and the cancellation below is what the owner asked for, so
            # it runs either way.
            with contextlib.suppress(ValueError, RuntimeError):
                self.root.after_cancel(job)
        self._dismiss_cancel = True
        self._finish_dismiss()

    def _reset_scroll(self) -> None:
        """Back to the top on every view change. No canvas, nothing to reset."""
        try:
            self._canvas.yview_moveto(0)
        except Exception:  # noqa: BLE001 - a destroyed widget during teardown
            return

    def _dismiss_step(self) -> None:
        from adk.decisions.store import DecisionError

        self._dismiss_job = None
        if self._dismiss_cancel or not self._dismiss_remaining:
            self._finish_dismiss()
            return
        card = self._dismiss_remaining.pop(0)
        done = self._dismiss_total - len(self._dismiss_remaining)
        try:
            self.store.cancel(card.id, note="dismissed as stale from the card list",
                              via="owner")
            self.handled += 1
        except DecisionError:
            # Answered or cancelled on another surface: the card is already off
            # the queue, which is exactly what "dismiss stale" wanted for it.
            self.handled += 1
        except OSError as exc:
            self._status = f"stopped after {done} of {self._dismiss_total}: {exc}"
            self._status_colour = RED
            self._finish_dismiss()
            return
        self._status = (f"dismissing {self._dismiss_total} stale card(s)… "
                        f"{done} of {self._dismiss_total}")
        self._status_colour = GOLD
        self.queue = self._refresh_queue()
        self._render()
        self._dismiss_job = self.root.after(60, self._dismiss_step)

    def _finish_dismiss(self) -> None:
        done = self._dismiss_total - len(self._dismiss_remaining)
        self._dismiss_remaining = []
        self._dismiss_total = 0
        self._status = (f"dismissed {done} stale card(s)" if done
                        else "dismissal cancelled")
        self._status_colour = MUTED
        self.queue = self._refresh_queue()
        self._render()

    def _content(self, body, card, accent, wheel) -> None:
        tk = self.tk
        wrap = WIDTH - 60

        def label(text, *, fg=TEXT, font=(UI, 10), pad=(0, 0), parent=None, indent=0):
            widget = tk.Label(parent or body, text=text, bg=BG, fg=fg, font=font,
                              wraplength=wrap - indent, justify="left", anchor="w")
            widget.pack(fill="x", pady=pad, padx=(indent, 0))
            widget.bind("<MouseWheel>", wheel)
            return widget

        label(card.title, font=(UI, 14, "bold"))
        if card.summary:
            label(card.summary, fg=SOFT, pad=(8, 0))

        if card.facts:
            label("WHAT I MEASURED", fg=MUTED, font=(UI, 8, "bold"), pad=(14, 2))
            for fact in card.facts:
                # Every fact, in full. This used to be facts[:6] with each one
                # truncated upstream; the clause that decides the answer is as
                # likely to be at the end of a fact as the start.
                label(f"·  {fact}", fg="#8b93a8", font=(MONO, 9), pad=(3, 0))

        if card.detail:
            label(card.detail, fg=MUTED, font=(UI, 9), pad=(12, 0))

        if card.notes:
            label("YOU ALREADY SAID", fg=MUTED, font=(UI, 8, "bold"), pad=(14, 2))
            for note in card.notes:
                mark = "sent" if note.delivered_live else "queued"
                label(f"“{note.text}”  ({mark})", fg=GREEN, font=(UI, 9), pad=(3, 0))

        if (card.kind or "").strip().lower() == "credential":
            # 🚨 THIS BRANCH DID NOT EXIST, and its absence was silent.
            # A credential card carries NO options by design — store.py
            # refuses them, because an option list would put the secret in the
            # card's durable JSON. So every credential card fell through to
            # the optionless `else` below and rendered as
            # "Nothing to click — this is context, not a question."
            # The card announced itself, looked correct, and could not do the
            # one thing it was raised for; the only working door was a
            # terminal command, which is precisely the trip the card exists to
            # remove. Measured live 2026-09-05 on d-66n8 (GITHUB_CLIENT_SECRET)
            # with 4 credential cards sitting in a 574-deep open queue.
            self._credential_row(body, card, wheel)
        elif card.options:
            opts = tk.Frame(body, bg=BG)
            opts.pack(fill="x", pady=(16, 0))
            opts.bind("<MouseWheel>", wheel)
            for index, opt in enumerate(card.options, start=1):
                self._option_button(opts, index, opt, wheel)
        else:
            # A card with no options is a notification with context. Say what to
            # do instead of leaving a dead window.
            where = card.source.cwd or "the session's terminal"
            label(f"Nothing to click — this is context, not a question.\n{where}",
                  fg=GOLD, font=(MONO, 9), pad=(14, 0))
            self._plain_button(body, "Got it — dismiss", self._dismiss, primary=True)

        self._terminal_row(body, card, wheel)

    def _credential_row(self, body, card, wheel) -> None:
        """WHICH secret, WHERE it goes -- and the one door: the masked field in awdesk.

        This window takes no value. Owner, 2026-10-28: credential intake goes
        through awdesk/awconnect, "not one-off Python scripts"; a masked field in a
        Tk window is the same thing to the owner, whichever script drew it. The
        desk Inbox's CredentialRow pipes the value into the vault's stdin door,
        which reads it back and closes this card, so the raising session hears
        through the ordinary answer mailbox.
        """
        tk = self.tk
        label_text = card.secret_name or card.id

        frame = tk.Frame(body, bg=BG)
        frame.pack(fill="x", pady=(16, 0))
        frame.bind("<MouseWheel>", wheel)

        scope = (card.credential_scope or "platform").strip().lower()
        where = {
            "platform": "the PLATFORM vault — every platform admin can read it",
            "user": "your PERSONAL lockbox — nobody else can read it",
            "workspace": "the WORKSPACE lockbox — this workspace's members "
                         "can read it",
        }.get(scope, f"scope {scope!r}, which has no store — this will refuse")

        tk.Label(frame, text=f"SECRET · {label_text}", bg=BG, fg=GOLD,
                 font=(UI, 8, "bold"), anchor="w").pack(fill="x")
        # The DESTINATION is named on the card, not just the fact that it is
        # "secure". The owner is being asked to hand over a credential; which
        # store it lands in decides who can read it afterwards, and that is
        # the one thing they cannot find out later by looking at the card.
        tk.Label(frame, text=f"Goes to {where}.",
                 bg=BG, fg=SOFT, font=(UI, 9), wraplength=WIDTH - 60,
                 justify="left", anchor="w").pack(fill="x", pady=(2, 0))
        tk.Label(frame,
                 text="Enter it in the awdesk Inbox: the masked field there goes "
                      "straight to that store, and this card closes once the vault "
                      "confirms the write.",
                 bg=BG, fg=MUTED, font=(UI, 9), wraplength=WIDTH - 60,
                 justify="left", anchor="w").pack(fill="x", pady=(2, 6))
        self._credential_status = tk.Label(
            frame, text="", bg=BG, fg=MUTED, font=(UI, 9),
            wraplength=WIDTH - 60, justify="left", anchor="w")
        self._credential_status.pack(fill="x", pady=(6, 0))

        row = tk.Frame(frame, bg=BG)
        row.pack(fill="x", pady=(8, 0))
        # THE door, the same ladder every surface runs (secure_prompt.
        # open_credential_surface): the running desk's Inbox, or the desk started
        # with its own launcher. Enter triggers it (see _on_return).
        self._plain_button(row, "Open in awdesk  (Enter)",
                           self._open_in_desk, primary=True, side="left",
                           hint="brings the awdesk Inbox forward on this secret's "
                                "masked field, starting awdesk if it is not running")

    def _is_credential(self) -> bool:
        return (self.card.kind or "").strip().lower() == "credential"

    def _open_in_desk(self) -> None:
        """Put this card's masked field in front of the owner, in the desk.

        On a WORKER THREAD: starting the desk waits for its heartbeat, and doing
        that inline would freeze this window. The outcome is reported either way;
        a button that silently does nothing sends the owner hunting.
        """
        import threading

        status = getattr(self, "_credential_status", None)
        if status is not None:
            status.configure(text="Opening it in awdesk…", fg=SOFT)
        card = self.card

        def worker() -> None:
            try:
                from adk.decisions.secure_prompt import open_credential_surface

                shown = open_credential_surface(card, owner_initiated=True)
                ok, why = shown.surface == "desk", shown.detail
            except ImportError as exc:  # pragma: no cover - packaging accident
                ok, why = False, f"the desk door is unavailable: {exc}"
            self.root.after(0, lambda: self._desk_outcome(ok, why))

        threading.Thread(target=worker, daemon=True).start()

    def _desk_outcome(self, ok: bool, why: str) -> None:
        status = getattr(self, "_credential_status", None)
        if status is not None:
            status.configure(text=why, fg=GREEN if ok else GOLD)
        self._flash(why, GREEN if ok else GOLD)

    # ── the explorable context panel ──────────────────────────────────────────

    def _explore_content(self, body, wheel) -> None:
        """The card's facts, drillable. One row per source, filled as they land.

        The panel is built ONCE with a placeholder per source and then updated in
        place. Re-rendering the window on every arriving result would be simpler
        and would also collapse whatever the owner had just expanded, which is
        the opposite of exploring.
        """
        tk = self.tk
        tk.Label(body, text="CONTEXT — live, harvested just now", bg=BG, fg=ACCENT,
                 font=(UI, 9, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text="Everything the card asserts, with the evidence behind it. "
                            "Click a section to open it; click a row for detail.",
                 bg=BG, fg=MUTED, font=(UI, 9), anchor="w", justify="left",
                 wraplength=WIDTH - 60).pack(fill="x", pady=(2, 10))

        self._source_rows = {}
        from adk.decisions import context as ctx

        for name, _fn in ctx.HARVESTERS:
            frame = tk.Frame(body, bg=PANEL)
            frame.pack(fill="x", pady=(0, 6))
            head = tk.Frame(frame, bg=PANEL, padx=10, pady=7)
            head.pack(fill="x")
            title = tk.Label(head, text=name.title(), bg=PANEL, fg=TEXT,
                             font=(UI, 10, "bold"), anchor="w")
            title.pack(side="left")
            state = tk.Label(head, text="harvesting…", bg=PANEL, fg=MUTED,
                             font=(UI, 9), anchor="e")
            state.pack(side="right")
            holder = tk.Frame(frame, bg=PANEL)
            self._source_rows[name] = {
                "frame": frame, "head": head, "title": title,
                "state": state, "holder": holder, "open": False, "source": None,
            }
            for widget in (frame, head, title, state):
                widget.bind("<Button-1>", lambda _e, n=name: self._toggle_source(n))
                widget.bind("<MouseWheel>", wheel)

        # Anything that already arrived (a re-render mid-harvest) is painted now.
        for name, row in self._source_rows.items():
            existing = self._context.get(name)
            if existing is not None:
                self._paint_source(name, existing)

    def _paint_source(self, name: str, source) -> None:
        row = getattr(self, "_source_rows", {}).get(name)
        if row is None:
            return
        row["source"] = source
        self._context[name] = source
        colour = {"ok": GREEN, "empty": MUTED, "dead": GOLD}.get(source.status, MUTED)
        row["title"].configure(text=source.title)
        row["state"].configure(text=f"{source.headline}   ({source.took_ms}ms)",
                               fg=colour)

    def _toggle_source(self, name: str) -> None:
        row = getattr(self, "_source_rows", {}).get(name)
        if row is None or row["source"] is None:
            return
        tk = self.tk
        if row["open"]:
            row["holder"].pack_forget()
            for child in row["holder"].winfo_children():
                child.destroy()
            row["open"] = False
            return
        # Accordion: opening one closes the others. Six sections each capable of
        # forty rows is a scroll, not an explorer — with 2,180 changed files the
        # first expansion pushed every other section off the bottom and the
        # owner lost the map they came for.
        for other, entry in self._source_rows.items():
            if other != name and entry["open"]:
                entry["holder"].pack_forget()
                for child in entry["holder"].winfo_children():
                    child.destroy()
                entry["open"] = False

        source = row["source"]
        shown = source.items[:SECTION_ROWS]
        hidden = len(source.items) - len(shown)
        for item in shown:
            line = tk.Frame(row["holder"], bg=BG, padx=12, pady=3)
            line.pack(fill="x")
            tk.Label(line, text=item.label, bg=BG, fg=SOFT, font=(MONO, 9),
                     anchor="w", justify="left",
                     wraplength=WIDTH - 90).pack(fill="x")
            if item.body:
                tk.Label(line, text=item.body, bg=BG, fg=MUTED, font=(UI, 8),
                         anchor="w", justify="left",
                         wraplength=WIDTH - 90).pack(fill="x")
        dropped = source.more + hidden
        if dropped:
            # Say what was dropped, counting BOTH the harvester's cap and this
            # panel's. A silently shortened list is the defect this whole
            # feature was raised over, and two caps in series is exactly how a
            # count goes quietly wrong.
            tk.Label(row["holder"], text=f"   … and {dropped} more not shown",
                     bg=BG, fg=GOLD, font=(UI, 8), anchor="w").pack(fill="x", pady=(2, 4))
        if not source.items:
            tk.Label(row["holder"], text=f"   {source.headline}", bg=BG, fg=GOLD,
                     font=(UI, 9), anchor="w", justify="left",
                     wraplength=WIDTH - 90).pack(fill="x", pady=(2, 4))
        row["holder"].pack(fill="x")
        row["open"] = True
        # The frame is FIXED: what was just revealed scrolls inside it. Growing
        # the window here is what used to move the header controls. The canvas
        # scrollregion follows from the <Configure> binding.

    def _open_explore(self) -> None:
        """Switch to the context panel and start harvesting in the background."""
        import queue
        import threading

        self._mode = "explore"
        self._context = {}
        self._context_queue = queue.Queue()

        def worker(card):
            from adk.decisions import context as ctx

            for name, harvester in ctx.HARVESTERS:
                started = time.time()
                try:
                    found = harvester(card)
                except Exception as exc:  # noqa: BLE001 - one bad source, not the panel
                    found = ctx.ContextSource(name=name, title=name.title(),
                                              status=ctx.STATUS_DEAD,
                                              detail=f"harvester raised: {exc}")
                # Stamped HERE, not in explore(): this worker calls the
                # harvesters directly, so a source timed only by explore()
                # reports 0ms for every row and the panel looks instantaneous
                # while the owner is watching it take five seconds.
                found.took_ms = int((time.time() - started) * 1000)
                self._context_queue.put((name, found))
            self._context_queue.put((None, None))

        # A thread, because harvesting takes seconds (docker alone can eat five)
        # and doing it on the Tk loop freezes the window mid-click — the card
        # would look hung at exactly the moment the owner engaged with it.
        threading.Thread(target=worker, args=(self.card,), daemon=True).start()
        self._render()

    def _drain_context(self) -> None:
        """Paint results as they arrive. Scheduled from the Tk loop only."""
        import queue as _queue

        pending = getattr(self, "_context_queue", None)
        if pending is None or self._mode != "explore":
            return
        done = False
        while True:
            try:
                name, source = pending.get_nowait()
            except _queue.Empty:
                break
            if name is None:
                done = True
                break
            self._paint_source(name, source)
        if not done:
            self._context_job = self.root.after(200, self._drain_context)

    def _close_explore(self) -> None:
        self._mode = "card"
        self._render()

    def _option_button(self, parent, index: int, opt, wheel) -> None:
        tk = self.tk
        recommended = opt.recommended
        border = ACCENT if recommended else EDGE
        wrap = tk.Frame(parent, bg=border, padx=1, pady=1)
        wrap.pack(fill="x", pady=(0, 7))

        inner = tk.Frame(wrap, bg=PANEL)
        inner.pack(fill="both", expand=True)

        def choose(_event=None):
            self._answer(opt.key)

        row = tk.Frame(inner, bg=PANEL, padx=12, pady=9)
        row.pack(fill="x")

        key = tk.Label(row, text=f"{index}", bg=(ACCENT_DIM if recommended else EDGE),
                       fg=(ACCENT if recommended else MUTED), font=(MONO, 10, "bold"),
                       width=3, pady=2)
        key.pack(side="left", padx=(0, 10))

        text = tk.Frame(row, bg=PANEL)
        text.pack(side="left", fill="x", expand=True)

        label = tk.Label(
            text, text=opt.label + ("   ★ recommended" if recommended else ""),
            bg=PANEL, fg=(TEXT if recommended else "#b6bccd"),
            font=(UI, 10, "bold" if recommended else "normal"),
            anchor="w", justify="left", wraplength=WIDTH - 130,
        )
        label.pack(fill="x")

        cons = None
        if opt.consequence:
            cons = tk.Label(text, text=opt.consequence, bg=PANEL, fg=MUTED,
                            font=(UI, 9), anchor="w", justify="left",
                            wraplength=WIDTH - 130)
            cons.pack(fill="x")

        parts = [w for w in (wrap, inner, row, text, label, key, cons) if w is not None]

        def enter(_e=None):
            for widget in parts:
                if widget is not key and widget is not wrap:
                    widget.configure(bg=EDGE)

        def leave(_e=None):
            for widget in parts:
                if widget is not key and widget is not wrap:
                    widget.configure(bg=PANEL)

        for widget in parts:
            widget.bind("<Button-1>", choose)
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)
            widget.bind("<MouseWheel>", wheel)

    def _terminal_row(self, body, card, wheel) -> None:
        """Controls that act on the SESSION, not just on the card.

        What each one can do is resolved before it is drawn, so an unavailable
        control is labelled rather than silently inert — the failure mode this
        whole row would otherwise be a textbook example of.
        """
        tk = self.tk
        try:
            from adk.decisions import terminal

            caps = terminal.capabilities(card.source.session_pid, card.source.cwd)
        except ImportError as exc:  # pragma: no cover - packaging accident
            caps = {"focus": f"unavailable — {exc}", "open": "unavailable",
                    "type": "unavailable", "tab": "", "chain": ""}

        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(16, 0))
        row.bind("<MouseWheel>", wheel)

        tab = (caps.get("tab") or "").strip()
        tk.Label(row, text=("THE TERMINAL THIS IS ABOUT" if tab else "THIS SESSION"),
                 bg=BG, fg=MUTED, font=(UI, 8, "bold"), anchor="w").pack(fill="x")
        if tab:
            tk.Label(row, text=tab, bg=BG, fg=SOFT, font=(UI, 9), anchor="w",
                     wraplength=WIDTH - 60, justify="left").pack(fill="x", pady=(2, 0))
        if card.source.cwd:
            tk.Label(row, text=card.source.cwd, bg=BG, fg=MUTED, font=(MONO, 8),
                     anchor="w", wraplength=WIDTH - 60,
                     justify="left").pack(fill="x", pady=(1, 0))

        buttons = tk.Frame(row, bg=BG)
        buttons.pack(fill="x", pady=(8, 0))
        ready = caps.get("focus") == "ready"
        self._plain_button(buttons, "Go to that terminal", self._focus_terminal,
                           enabled=ready, side="left",
                           hint=caps.get("focus", ""))
        self._plain_button(buttons, "Open a terminal here", self._open_terminal,
                           enabled=caps.get("open") == "ready", side="left",
                           hint=caps.get("open", ""))
        self._plain_button(buttons, "Copy path", self._copy_cwd,
                           enabled=bool(card.source.cwd), side="left",
                           hint="copy the working directory")
        # Only drawn when it is armed. Measured 2026-08-10 to really work on a
        # ConPTY (Windows Terminal) tab as well as a classic console — see
        # `python -m adk.decisions.terminal --live-console --conpty` — but it is
        # the one control that can land characters in a prompt the owner is
        # mid-way through typing, so it stays opt-in and it stays labelled.
        if caps.get("type") == "ready":
            self._plain_button(buttons, "Type it into that terminal",
                               self._type_into_terminal, side="left",
                               hint="types the reply box into the session's prompt"
                                    " without pressing Enter")

    def _plain_button(self, parent, text, command, *, primary=False, enabled=True,
                      side="top", hint: str = "") -> None:
        tk = self.tk
        fg = TEXT if (enabled and primary) else (SOFT if enabled else MUTED)
        bg = ACCENT_DIM if primary else PANEL
        button = tk.Label(parent, text=f"  {text}  ", bg=bg, fg=fg, font=(UI, 9),
                          padx=6, pady=5, cursor=("hand2" if enabled else "arrow"))
        button.pack(side=side, padx=(0, 8), pady=(0, 2), fill=("x" if side == "top" else None))
        if enabled:
            button.bind("<Button-1>", lambda _e: command())
            button.bind("<Enter>", lambda _e: (button.configure(bg=EDGE, fg=TEXT),
                                               self._flash(hint, MUTED) if hint else None))
            button.bind("<Leave>", lambda _e: button.configure(bg=bg, fg=fg))
        elif hint:
            # A disabled control must say WHY, on hover and without a click —
            # "nothing happened" is the outcome this row exists to avoid.
            button.bind("<Enter>", lambda _e: self._flash(hint, GOLD))

    def _reply_row(self, parent, card) -> None:
        """The text box. This is the control the first version did not have."""
        tk = self.tk
        holder = tk.Frame(parent, bg=BG, padx=18, pady=0)
        holder.pack(fill="x")

        box = tk.Frame(holder, bg=EDGE, padx=1, pady=1)
        box.pack(fill="x")
        self.reply = tk.Text(box, height=3, bg=PANEL, fg=TEXT, insertbackground=ACCENT,
                             font=(UI, 10), relief="flat", wrap="word", bd=0,
                             padx=10, pady=8, highlightthickness=0)
        self.reply.pack(fill="x")
        self.reply.insert("1.0", REPLY_PLACEHOLDER)
        self.reply.configure(fg=MUTED)
        self._reply_empty = True
        self._reply_focused = False

        def focus_in(_event=None):
            self._reply_focused = True
            if self._reply_empty:
                self.reply.delete("1.0", "end")
                self.reply.configure(fg=TEXT)
                self._reply_empty = False

        def focus_out(_event=None):
            self._reply_focused = False
            if not self.reply.get("1.0", "end").strip():
                self.reply.delete("1.0", "end")
                self.reply.insert("1.0", REPLY_PLACEHOLDER)
                self.reply.configure(fg=MUTED)
                self._reply_empty = True

        self.reply.bind("<FocusIn>", focus_in)
        self.reply.bind("<FocusOut>", focus_out)
        self.reply.bind("<Control-Return>", lambda _e: (self._send(), "break")[1])

        actions = tk.Frame(holder, bg=BG)
        actions.pack(fill="x", pady=(6, 0))
        self._plain_button(actions, "Send  (Ctrl+Enter)", self._send,
                           primary=True, side="left",
                           hint="steer the session; the card stays open")
        if card.options:
            self._plain_button(
                actions, "Send & answer recommended", self._send_and_recommend,
                side="left", hint="send the text, then take the ★ option",
            )

    # ── window geometry ───────────────────────────────────────────────────────

    def _pos_path(self):
        return self.store.path / ".window.pos"

    def _read_position(self) -> Optional[tuple[int, int]]:
        """The last position the owner dragged this window to, if any."""
        try:
            raw = self._pos_path().read_text(encoding="utf-8").strip()
            x, _, y = raw.partition(",")
            return int(x), int(y)
        except (OSError, ValueError):
            return None

    def _place(self) -> None:
        """Position the frame ONCE: remembered position, else top-right, 24px in.

        Called at init and after a drag. NEVER from ``_render()`` and never with
        a size computed from a card's content -- that recompute is what moved
        the controls between cards.
        """
        root = self.root
        width, height = self._size
        try:
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
        except Exception:  # noqa: BLE001 - a withdrawn root still answers
            screen_w, screen_h = 1920, 1080
        remembered = self._read_position()
        if remembered is None:
            x = max(0, screen_w - width - 24)
            y = 24
        else:
            x, y = remembered
        x = max(0, min(x, max(0, screen_w - width)))
        y = max(0, min(y, max(0, screen_h - 60)))
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.minsize(320, 240)

    def _save_position(self) -> None:
        # A read-only or vanished home is not fatal: the remembered corner is a
        # convenience, and refusing to close over it would cost the owner a window.
        with contextlib.suppress(Exception):
            self._pos_path().write_text(
                f"{self.root.winfo_x()},{self.root.winfo_y()}", encoding="utf-8")

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root, event.y_root,
                      self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if not getattr(self, "_drag", None):
            return
        start_x, start_y, win_x, win_y = self._drag
        # Move only: the width/height are never touched here, so the frame
        # cannot be resized by accident and the header slots cannot reflow.
        self.root.geometry(
            f"+{win_x + event.x_root - start_x}+{win_y + event.y_root - start_y}"
        )

    def _drag_end(self, _event=None) -> None:
        if getattr(self, "_drag", None):
            self._drag = None
            self._save_position()

    # ── actions ───────────────────────────────────────────────────────────────

    def _move(self, delta: int) -> None:
        """Walk the queue WITHOUT answering anything. Clamps; never wraps.

        Wrapping is what made the arrow feel like it moved on its own: at the
        bottom of the list one more click jumped to the top.
        """
        self.queue = self._refresh_queue()
        if not self.queue:
            self.root.destroy()
            return
        if self._mode == "list":
            ranked = self._view()["open"]
            if not ranked:
                return
            here = next((i for i, c in enumerate(ranked) if c.id == self._cursor_id), 0)
            here = max(0, min(len(ranked) - 1, here + delta))
            self._cursor_id = ranked[here].id
            self._render()
            return
        here = next((i for i, c in enumerate(self.queue) if c.id == self.card.id),
                    self.index)
        self.index = max(0, min(len(self.queue) - 1, here + delta))
        self._cursor_id = self.card.id
        # The panel is about ONE card's evidence; carrying it to the next card
        # would show the previous card's context under the new card's question.
        self._mode = "card"
        self._context = {}
        self._status = ""
        self._render()

    def _step(self, delta: int) -> None:
        self._move(delta)

    def _pick_index(self, index: int) -> None:
        if self._typing():
            return
        card = self._cursor_card() if self._mode == "list" else self._focus_card()
        if card is None:
            return
        if 1 <= index <= len(card.options):
            self._answer_card(card, card.options[index - 1].key,
                              note=self._reply_text() if self._mode != "list" else "")

    def _cursor_card(self) -> Optional[DecisionCard]:
        ranked = self._view()["open"]
        return next((c for c in ranked if c.id == self._cursor_id),
                    ranked[0] if ranked else None)

    def _take_recommended(self) -> None:
        card = self._focus_card()
        key = card.recommended_key() if card is not None else ""
        if key:
            self._answer(key)

    def _reply_text(self) -> str:
        box = getattr(self, "reply", None)
        if box is None or self._reply_empty:
            return ""
        try:
            return box.get("1.0", "end").strip()
        except Exception:  # noqa: BLE001 - a dead widget has no text
            return ""

    def _send(self) -> None:
        """Steer the session with the owner's own words. The card stays open."""
        from adk.decisions.store import DecisionError

        text = self._reply_text()
        if not text:
            self._flash("type something first", GOLD)
            return
        try:
            self.store.steer(self.card.id, text, via="popup")
        except DecisionError as exc:
            self._flash(str(exc), RED)
            return
        live = bool(self.store.get(self.card.id) and
                    self.store.get(self.card.id).notes[-1].delivered_live)
        self.reply.delete("1.0", "end")
        self._reply_empty = True
        # Two different facts, said differently on purpose: one means the agent
        # has it now, the other means it will see it at its next prompt.
        self._status = ("sent — the session has it now" if live
                        else "queued — the session picks this up at its next prompt")
        self._status_colour = GREEN if live else GOLD
        self.queue = self._refresh_queue()
        self._render()

    def _send_and_recommend(self) -> None:
        if self._reply_text():
            self._send()
        self._take_recommended()

    def _answer(self, key: str) -> None:
        """Answer the card the expanded view is showing."""
        card = self._focus_card()
        if card is None:
            return
        self._answer_card(card, key, note=self._reply_text())

    def _answer_card(self, card: DecisionCard, key: str, *, note: str = "") -> None:
        """Answer ONE card, from the list row or the expanded view."""
        from adk.decisions.store import DecisionError

        try:
            self.store.answer(card.id, key, note=note, via="popup")
            self.answered = key
            self.handled += 1
        except DecisionError as exc:
            # Somebody answered it elsewhere first. Say so rather than closing
            # silently, which would read as "my click worked".
            self._flash(str(exc), RED)
            return
        self._status = f"answered “{card.title[:48]}” — {key}"
        self._status_colour = GREEN
        self._advance()

    def _dismiss(self) -> None:
        """Close an optionless card for good, rather than leaving it pending."""
        from adk.decisions.store import DecisionError

        card = self._focus_card()
        if card is None:
            return
        try:
            self.store.cancel(card.id, note="acknowledged from the card window",
                              via="owner")
            self.handled += 1
        except DecisionError as exc:
            self._flash(str(exc), RED)
            return
        self._status = f"dismissed “{card.title[:48]}”"
        self._status_colour = MUTED
        self._advance()

    def _rerender_preserving_reply(self) -> None:
        """Re-render, and give the owner back the sentence they were typing.

        ``_render()`` rebuilds the body, which reinstates the placeholder in the
        reply box. Called from the tick — i.e. at an arbitrary moment the owner
        did not choose — a plain re-render would eat a half-typed steer. Losing
        the owner's words to absorb somebody else's card would trade one silent
        loss for another.
        """
        text = ""
        box = getattr(self, "reply", None)
        if box is not None and not self._reply_empty:
            try:
                text = box.get("1.0", "end").strip()
            except Exception:  # noqa: BLE001 - a dead widget must not kill the tick
                text = ""
        had_focus = self._reply_focused

        self._render()

        if not text:
            return
        box = getattr(self, "reply", None)
        if box is None:
            return
        try:
            box.delete("1.0", "end")
            box.insert("1.0", text)
            box.configure(fg=TEXT)
            self._reply_empty = False
            if had_focus:
                box.focus_set()
        except Exception:  # noqa: BLE001 - same
            return

    def _advance(self) -> None:
        """Move on after an answer: stay in the view the owner was using."""
        self.queue = self._refresh_queue()
        if not self.queue:
            self.root.destroy()
            return
        if self._mode == "list":
            self._cursor_id = self.queue[0].id
            self._render()
            return
        self.index = min(self.index, len(self.queue) - 1)
        self._cursor_id = self.card.id
        self._render()

    def _focus_terminal(self) -> None:
        from adk.decisions import terminal

        source = self.card.source
        # Hints only matter when the session's live title cannot be read; they
        # never override it (terminal.focus / winproc.match_tab).
        hints = tuple(h for h in (os.path.basename(source.cwd.rstrip("\\/")),
                                  source.branch) if h)
        pid = source.session_pid
        self._flash("finding that terminal's tab…", GOLD)

        import threading

        from adk.decisions import winproc

        def raise_on_ui_thread(hwnd: int) -> bool:
            # The tab search runs on the worker; the foreground change stays on
            # the Tk thread — the thread that owns the window the owner clicked,
            # which is the one Windows lets hand focus away.
            done = threading.Event()
            result = [False]

            def run() -> None:
                try:
                    result[0] = winproc.focus_window(hwnd)
                finally:
                    done.set()

            self.root.after(0, run)
            return done.wait(5.0) and result[0]

        def worker() -> None:
            # Off the Tk thread: reading every tab through UI Automation, and the
            # marker tie-break above all (up to 1.5 s), would freeze the card.
            try:
                ok, why = terminal.focus(pid, hints, raise_on_ui_thread)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                ok, why = False, f"focusing the terminal failed: {exc}"
            # The card may have closed while we searched; then there is no one
            # left to tell, and that is not an error.
            with contextlib.suppress(RuntimeError):
                self.root.after(0, lambda: self._flash(why, GREEN if ok else GOLD))

        threading.Thread(target=worker, daemon=True).start()

    def _open_terminal(self) -> None:
        from adk.decisions import terminal

        ok, why = terminal.open_terminal(self.card.source.cwd)
        self._flash(why, GREEN if ok else GOLD)

    def _type_into_terminal(self) -> None:
        """Type the reply box straight into the session's console.

        Deliberately separate from Send. Send is delivery — mailbox always, live
        tier when one accepts. This is the owner saying "put these keystrokes in
        that tab", which is a different act with a different failure mode, and
        conflating them would hide which one happened.
        """
        from adk.decisions import terminal

        text = self._reply_text()
        if not text:
            self._flash("type something first", GOLD)
            return
        # submit=False: the text lands as a DRAFT in that prompt, no Enter. The
        # owner may be half-way through typing there; pressing Enter for them
        # would send their half-typed line with ours glued on.
        ok, why = terminal.type_into_console(self.card.source.session_pid, text,
                                             submit=False)
        if ok:
            self.reply.delete("1.0", "end")
            self._reply_empty = True
            why = f"{why} — not sent; press Enter in that tab"
        self._flash(why, GREEN if ok else GOLD)

    def _copy_cwd(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.card.source.cwd)
        self._flash("path copied", GREEN)

    def _snooze(self) -> None:
        """Close the window, leave the card open. Explicitly NOT an answer."""
        self.root.destroy()

    def _flash(self, message: str, colour: str = RED) -> None:
        self._status = message
        self._status_colour = colour
        countdown = getattr(self, "countdown", None)
        if countdown is not None:
            countdown.configure(text=message[:160], fg=colour)

    def _tick(self) -> None:
        # One timer, not one per render. _render() runs on every answer, steer
        # and queue step, so a self-rescheduling tick started there multiplies:
        # after five renders the store is polled five times a second and five
        # chains race to call _advance().
        job = getattr(self, "_tick_job", None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except (ValueError, RuntimeError):
                self._tick_job = None

        # Absorb cards raised by OTHER sessions while this window sits open.
        #
        # This is the fix for the defect that made the whole channel look
        # single-session. ``show_queue`` takes a HARD lock, so a second session's
        # raise prints "a decision window is already open; it will pick this up"
        # and exits 0 — but the queue was only re-read on answer/steer/navigate,
        # never on the tick. An idle window therefore SUPPRESSED every other
        # session's card while reporting that it had them: measured 2026-08-10,
        # the owner saw cards from one session at a time and concluded the
        # feature only worked in one tab. That is the silent-no-op pattern of
        # `.claude/rules/security-review-patterns.md` §5 living inside the
        # surface whose entire job is to not lose an ask.
        try:
            fresh = self._refresh_queue()
        except OSError:
            fresh = []
        if fresh and {c.id for c in fresh} != {c.id for c in self.queue}:
            arrived = [c for c in fresh if c.id not in {q.id for q in self.queue}]
            showing = self._cursor_id or (self.card.id if self.queue else "")
            self.queue = fresh
            if self._mode == "card":
                self.index = next(
                    (i for i, c in enumerate(fresh) if c.id == showing),
                    min(self.index, len(fresh) - 1),
                )
                self._cursor_id = self.card.id
            else:
                self._cursor_id = next(
                    (c.id for c in fresh if c.id == showing), fresh[0].id)
            if arrived:
                # Say it rather than silently growing the counter: a card that
                # arrived behind the one on screen is invisible otherwise.
                plural = "s" if len(arrived) > 1 else ""
                self._status = (
                    f"+{len(arrived)} new card{plural} from another session"
                    f" — press ↓ / Ctrl+→ to see {'them' if plural else 'it'}"
                )
                self._status_colour = GOLD
            # _render() re-arms the tick, so return rather than scheduling a
            # second chain (see the note at the top of this method).
            self._rerender_preserving_reply()
            return
        if not fresh and not self.queue:
            self.root.destroy()
            return

        card = self._focus_card()
        if card is not None:
            try:
                current = self.store.get(card.id) or card
            except OSError:
                # An unreadable store must not close a card window that is
                # showing a real question. Keep it on screen, retry next second.
                current = card
            if current.status in CLOSED_STATUSES:
                self._advance()
                return
        self._update_footer(card)
        if not self._headless and not self._held_reason:
            # A game started while the card is open: stop sitting above it
            # (unless pinned), and come back on top when it ends. Never a lift.
            self._apply_topmost(is_quiet()[0])
        self._tick_job = self.root.after(1000, self._tick)

    def run(self) -> Optional[str]:
        self.root.mainloop()
        return self.answered


def _lock_path(store: DecisionStore) -> "os.PathLike[str]":
    return store.path / ".window.lock"


def _pid_alive(pid: int) -> bool:
    """Is this pid a live process? Used to spot a lock left by a crash."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION — succeeds only for a live process.
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def acquire_window_lock(store: DecisionStore) -> bool:
    """True if this process may own THE window. False means one already exists.

    A hard lock, not a timing heuristic. The first version relied on a "was a
    window raised in the last N seconds" quiet window, and two cards raised inside
    the same second both passed that test and both opened a window — which is
    precisely the duplicate-window spam that made the surface unusable. A stale
    lock left by a crashed process is detected by liveness and taken over, so a
    kill -9 cannot permanently suppress the window.
    """
    lock = _lock_path(store)
    try:
        if lock.exists():
            raw = lock.read_text(encoding="utf-8").strip()
            if raw.isdigit() and _pid_alive(int(raw)) and int(raw) != os.getpid():
                return False
        lock.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        # If the lock cannot be read or written, prefer showing the card: a
        # duplicate window is annoying, a silently suppressed one loses the ask.
        return True
    return True


def release_window_lock(store: DecisionStore) -> None:
    lock = _lock_path(store)
    try:
        if lock.exists() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink()
    except OSError:
        return


def show_queue(store: Optional[DecisionStore] = None, *, start_id: str = "") -> int:
    """Open THE window on the queue of open cards.

    One window, one process, one lock. It walks the queue internally — answering
    advances, ``Ctrl+→`` skips — and re-reads the store on every render, so a
    card raised while it is up appears without a second window being spawned.
    """
    store = store or DecisionStore()
    if start_id:
        try:
            card = store.get(start_id)
        except Exception:  # noqa: BLE001 - a bad id must not lose the queue
            card = None
        if card is not None and card.status in CLOSED_STATUSES:
            sys.stderr.write(f"card {start_id} is already {card.status}\n")
            return 2
    if not acquire_window_lock(store):
        # Another window owns the screen. It re-reads the store between renders,
        # so the card that triggered this call appears there — no second window.
        print("a decision window is already open; it will pick this up")
        return 0
    try:
        try:
            window = CardWindow(store, start_id=start_id)
        except RuntimeError:
            print("no decisions waiting")
            return 0
        except Exception as exc:  # noqa: BLE001 - a GUI failure must not lose the card
            sys.stderr.write(f"could not open the card window: {exc}\n")
            return 2
        window.run()
        return 0 if window.handled else 1
    finally:
        release_window_lock(store)


def show(card_id: str, store: Optional[DecisionStore] = None) -> int:
    """Show one card (kept for callers that want a single id). 0 if answered."""
    return show_queue(store, start_id=card_id)


def _hold_window(card_id: str, seconds: float, report: str) -> int:
    """Child half of ``--live-multisession``: BE session A's window, for real.

    Takes the real lock, builds a real Tk window, runs a real mainloop so the
    real ``after()`` tick fires on its own — none of which the in-process
    self-test exercises, because it calls ``_tick()`` by hand in the same
    interpreter. Headless so it never reaches the owner's screen; what is being
    proved is the cross-PROCESS absorb, not the pixels.
    """
    import json
    from pathlib import Path

    store = DecisionStore()
    out: dict = {"pid": os.getpid(), "seen": [], "held_lock": False}
    if not acquire_window_lock(store):
        out["error"] = "could not take the window lock"
        Path(report).write_text(json.dumps(out), encoding="utf-8")
        return 2
    out["held_lock"] = True
    try:
        window = CardWindow(store, start_id=card_id, headless=True)
    except Exception as exc:  # noqa: BLE001 - report it rather than dying mute
        out["error"] = f"could not build the window: {exc}"
        Path(report).write_text(json.dumps(out), encoding="utf-8")
        release_window_lock(store)
        return 2

    seen: set[str] = set()
    deadline = time.time() + seconds

    def sample() -> None:
        for entry in window.queue:
            seen.add(entry.id)
        if time.time() >= deadline:
            window.root.destroy()
            return
        window.root.after(200, sample)

    window.root.after(200, sample)
    try:
        window.root.mainloop()
    finally:
        release_window_lock(store)
    out["seen"] = sorted(seen)
    Path(report).write_text(json.dumps(out), encoding="utf-8")
    return 0


def _live_multisession() -> int:
    """Two REAL processes: does session A's open window absorb session B's card?

    The in-process self-test drives ``_tick()`` by hand, which proves the logic
    and not the situation. This is the situation: process A holds the lock and
    runs its own timer, process B raises through the real ``adk decide ask``
    path, and the question is whether B's card ever reaches A's queue — because
    when it does not, B's raise still exits 0 and says the card was picked up.
    That combination is what made the owner believe cards only worked in one
    terminal tab.

    Windowless and safe to re-run. Exit 0 works, 1 does not, 2 could not judge.
    """
    import json
    import subprocess
    import tempfile
    from pathlib import Path

    from adk.decisions.store import DecisionOption, DecisionSource

    root = Path(__file__).resolve().parents[2]
    problems: list[str] = []
    # CREATE_NO_WINDOW. Both spawns below are console programs, and this probe
    # can itself be launched from the detached card path — where a child with no
    # console gets a NEW one and flashes on the desktop (gate 1t / DC007). A
    # check for focus-stealing windows that opened one would be self-defeating.
    no_window = 0x08000000 if os.name == "nt" else 0

    with tempfile.TemporaryDirectory() as tmp:
        cards = Path(tmp) / "cards"
        env = dict(os.environ)
        env["AITHER_DECISIONS_DIR"] = str(cards)
        env["AITHER_STEER_DIR"] = str(Path(tmp) / "steer")
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(root), env.get("PYTHONPATH", "")) if p
        )
        store = DecisionStore(cards)

        first = store.create(DecisionCard(
            id="", title="Session A is showing this one", kind="decision",
            default_key="n",
            options=[DecisionOption(key="y", label="Yes"),
                     DecisionOption(key="n", label="No")],
            source=DecisionSource(session_id="session-a"),
        ))

        report = Path(tmp) / "report.json"
        # Spawned by IMPORT, not `-m`. `python -m adk.decisions.popup` executes a
        # second copy of this file as `__main__`, so the child would run a
        # different CardWindow class than the one every other caller imports —
        # which also makes the probe untestable, because a patch applied to the
        # importable module never reaches the process being measured.
        child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c",
             "import sys; from adk.decisions.popup import _hold_window; "
             "sys.exit(_hold_window(sys.argv[1], float(sys.argv[2]), sys.argv[3]))",
             first.id, "10", str(report)],
            cwd=str(root), env=env, creationflags=no_window,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        lock = cards / ".window.lock"
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                taken = (lock.exists()
                         and lock.read_text(encoding="utf-8").strip() == str(child.pid))
            except OSError:
                # Mid-write by the child. Not a failure — look again next pass.
                taken = False
            if taken:
                break
            if child.poll() is not None:
                break
            time.sleep(0.2)
        else:
            child.kill()
            print("FAIL session A never took the window lock — cannot judge")
            return 2

        if child.poll() is not None:
            print(f"FAIL session A died before holding the window: {child.communicate()[1]}")
            return 2

        # With A holding the lock, B genuinely cannot open its own window. That
        # is the half that makes a swallowed card unrecoverable rather than
        # merely late, so assert it rather than assuming it.
        if acquire_window_lock(store):
            problems.append("session B could have opened a second window — the lock did not hold")

        raise_proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "adk.decisions.cli", "ask",
             "Raised by session B while session A's window was up",
             "--kind", "blocked", "--urgency", "high",
             "--option", "y|Yes|it happens", "--option", "n|No|nothing changes",
             "--default", "n", "--session", "session-b", "--quiet", "--json"],
            cwd=str(root), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90, creationflags=no_window,
        )
        try:
            second_id = str(json.loads(raise_proc.stdout or "{}").get("id") or "")
        except ValueError:
            second_id = ""
        if not second_id:
            child.kill()
            print(f"FAIL session B's raise produced no card id: "
                  f"{raise_proc.stdout!r} {raise_proc.stderr!r}")
            return 2
        if raise_proc.returncode != 0:
            problems.append(f"session B's raise exited {raise_proc.returncode}")

        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            print("FAIL session A's window never exited — cannot judge")
            return 2

        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"FAIL session A wrote no verdict ({exc}) — cannot judge")
            return 2

        if data.get("error"):
            print(f"FAIL session A: {data['error']}")
            return 2
        if not data.get("held_lock"):
            print("FAIL session A never held the lock — cannot judge")
            return 2

        seen = set(data.get("seen") or [])
        if first.id not in seen:
            problems.append("session A never saw its OWN card — the probe is not measuring")
        if second_id not in seen:
            problems.append(
                f"session A's open window NEVER absorbed session B's card ({second_id}) — "
                f"B's raise exited 0 and the ask was lost"
            )

    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    print("live multi-session check passed - session A's open window absorbed the card "
          "session B raised while A held the lock")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print("usage: python -m adk.decisions.popup <card-id> | --next | --self-test "
              "| --live-multisession")
        return 0
    if args[0] == "--self-test":
        return _self_test()
    if args[0] == "--live-multisession":
        return _live_multisession()
    if args[0] == "--_hold":  # internal: the child half of --live-multisession
        return _hold_window(args[1], float(args[2]), args[3])
    store = DecisionStore()
    if args[0] == "--next":
        return show_queue(store)
    return show_queue(store, start_id=args[0])


def _self_test() -> int:
    """Prove the window builds, that clicking answers, and that TYPING steers.

    Builds a real Tk window off-screen and drives it. This catches the failures
    that actually happen — a bad geometry string, a widget option tkinter
    rejects, a font that does not resolve, an accelerator that fires while the
    caret is in the reply box — none of which are visible from reading the code.
    """
    import tempfile
    from pathlib import Path

    from adk.decisions.store import DecisionOption, DecisionSource

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = str(Path(tmp) / "cards")
        os.environ["AITHER_STEER_DIR"] = str(Path(tmp) / "steer")
        store = DecisionStore(Path(tmp) / "cards")
        long_fact = (
            "the six workstreams are executing in background workflow wmxuw5c0t and I "
            "will commit and gate them on completion; CI attribution for AitherVeil "
            "quality and cross-ref validation is deliberately deferred until the "
            "workflow lands, which is the part that decides the answer"
        )
        card = store.create(DecisionCard(
            id="", title="Self-test card with a deliberately long title that must wrap "
                         "cleanly inside the window rather than clipping",
            summary="Every widget in the card is exercised here.",
            facts=[long_fact, "a much longer fact " * 6, "third fact", "fourth",
                   "fifth", "sixth", "seventh fact — proves facts are NOT capped at six"],
            detail="Some additional detail.",
            kind="decision", urgency="high", default_key="a",
            options=[
                DecisionOption(key="a", label="First option " + "x" * 90,
                               consequence="does a thing", recommended=True),
                DecisionOption(key="b", label="Second option", consequence="does another"),
            ],
            source=DecisionSource(session_id="selftest", agent="selftest", branch="main",
                                  cwd=tmp, session_pid=os.getpid()),
            deadline=time.time() + 600,
        ))
        # A second card, so the queue behaviour and the "answered elsewhere"
        # path are both exercised rather than assumed.
        second = store.create(DecisionCard(
            id="", title="The next card in the queue", kind="decision", default_key="n",
            options=[DecisionOption(key="y", label="Yes"), DecisionOption(key="n", label="No")],
            source=DecisionSource(session_id="selftest"),
        ))

        problems: list[str] = []
        try:
            window = CardWindow(store, start_id=card.id, headless=True)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL could not build the window: {exc}")
            return 1
        window.root.update()

        # The test must never reach the owner's screen. The card above is
        # `urgency="high"`, which calls focus_force() on render — so before
        # `headless` existed, every run of this self-test flashed a window and
        # ate keystrokes. Assert it, because "I withdrew it afterwards" is what
        # was believed to be true while it was happening.
        if window.root.winfo_ismapped():
            problems.append("the self-test put a real window on the owner's screen")

        if window.root.winfo_reqwidth() < 400:
            problems.append(f"window too narrow: {window.root.winfo_reqwidth()}")
        if window.root.winfo_reqheight() < 200:
            problems.append(f"window too short: {window.root.winfo_reqheight()}")

        # NOTHING may be truncated. The rendered text of every fact and option
        # must be the string that was stored, character for character.
        rendered: list[str] = []

        def collect(widget):
            for child in widget.winfo_children():
                try:
                    text = child.cget("text")
                except Exception:  # noqa: BLE001 - not every widget has -text
                    text = ""
                if text:
                    rendered.append(str(text))
                collect(child)

        collect(window.root)
        joined = "\n".join(rendered)
        if long_fact not in joined:
            problems.append("a long fact was not rendered in full")
        # The session row shows the REAL terminal tab title, and Windows Terminal
        # truncates its own titles with an ellipsis before we ever read them
        # (measured 2026-11-07: "◑ Https://...wave2…"). That string is not card
        # content and is shown verbatim on purpose -- exempting it keeps this
        # rule testing OUR truncation instead of the owner's window title.
        harvested: set[str] = set()
        with contextlib.suppress(Exception):
            from adk.decisions import terminal as _terminal

            tab = (_terminal.capabilities(card.source.session_pid,
                                          card.source.cwd) or {}).get("tab") or ""
            if tab.strip():
                harvested.add(tab.strip())
        truncated = [
            text for text in rendered
            if ("…" in text or "..." in text) and text.strip() not in harvested
        ]
        if truncated:
            problems.append(
                f"something in the window is still truncated: {truncated[0][:80]!r}")
        if card.options[0].label not in joined:
            problems.append("a long option label was not rendered in full")
        if "seventh fact" not in joined:
            problems.append("facts are still capped — the seventh is missing")

        # Typing must steer without closing the card. FocusIn is generated
        # rather than focus_set(): the window is withdrawn for the test, and an
        # unmapped window never really takes focus — so driving the binding is
        # the only way to exercise the code path the owner hits.
        window.reply.event_generate("<FocusIn>")
        window.root.update()
        window.reply.delete("1.0", "end")
        window.reply.insert("1.0", "actually do the OTHER thing first")
        window._send()  # noqa: SLF001 - exercising the Send path
        steered = store.get(card.id)
        if steered is None or not steered.notes:
            problems.append("Send did not record a steer")
        elif steered.notes[-1].text != "actually do the OTHER thing first":
            problems.append("the steer text did not survive")
        if steered is not None and not steered.is_open:
            problems.append("sending text CLOSED the card — steering is not answering")
        box = Path(tmp) / "steer" / "selftest"
        if not (box.exists() and list(box.glob("*-steer.md"))):
            problems.append("the steer never reached the session mailbox")

        # An accelerator must not fire while the caret is in the reply box.
        window.reply.event_generate("<FocusIn>")
        window.root.update()
        if not window._typing():  # noqa: SLF001
            problems.append("_typing() did not notice the caret in the reply box")
        window._pick_index(1)  # noqa: SLF001 - must be a no-op while typing
        if (store.get(card.id) or card).status != "open":
            problems.append("typing '1' into the reply box answered the card")
        window._on_return()  # noqa: SLF001 - Enter must also be inert while typing
        if (store.get(card.id) or card).status != "open":
            problems.append("Enter in the reply box took the recommended option")

        # Answering through the window must really close the card in the store,
        # and must carry whatever was left in the reply box.
        window.reply.delete("1.0", "end")
        window.reply.insert("1.0", "and do it on staging")
        window._answer("b")  # noqa: SLF001 - exercising the click path deliberately
        refreshed = store.get(card.id)
        if refreshed is None or refreshed.answer != "b":
            problems.append("clicking an option did not record the answer")
        elif refreshed.status != "answered":
            problems.append(f"status after click was {refreshed.status}")
        elif refreshed.answer_note != "and do it on staging":
            problems.append("text left in the reply box was dropped from the answer")

        answers = list(box.glob("*-answer*.md")) if box.exists() else []
        if len(answers) != 1:
            problems.append(f"the answer produced {len(answers)} mailbox files, expected 1")
        elif "and do it on staging" not in answers[0].read_text(encoding="utf-8"):
            problems.append("the mailbox did not carry the owner's note")

        def alive() -> bool:
            """Tk raises rather than returning 0 once the root is gone."""
            try:
                return bool(window.root.winfo_exists())
            except Exception:  # noqa: BLE001 - TclError only lives on the tk module
                return False

        # Answering the first card must advance to the second, not close.
        window.root.update()
        if not alive():
            problems.append("answering one card closed a window with another still queued")
        elif window.card.id != second.id:
            problems.append(f"did not advance to the next card (showing {window.card.id})")

        # A card raised by ANOTHER SESSION while this window sits open must be
        # absorbed by it. ``show_queue``'s lock makes this window the only one
        # that can show anything, so a queue that only refreshed on answer/steer
        # meant every other session's card was written to disk, reported as
        # delivered, and never seen — the defect that made the owner believe
        # cards only worked in one terminal tab. The tick is what must notice.
        window.reply.event_generate("<FocusIn>")
        window.root.update()
        window.reply.delete("1.0", "end")
        window.reply.insert("1.0", "half-typed steer that must survive")
        window._reply_empty = False  # noqa: SLF001 - FocusIn already did this for a real user
        third = store.create(DecisionCard(
            id="", title="Raised by a DIFFERENT session while the window was up",
            kind="blocked", urgency="high", default_key="n",
            options=[DecisionOption(key="y", label="Yes"), DecisionOption(key="n", label="No")],
            source=DecisionSource(session_id="another-session"),
        ))
        window._tick()  # noqa: SLF001 - the poll that must absorb it
        window.root.update()
        if not alive():
            problems.append("absorbing another session's card destroyed the window")
        else:
            if third.id not in {c.id for c in window.queue}:
                problems.append(
                    "a card raised by another session was NOT absorbed — the window "
                    "swallowed it while holding the lock that stops it opening its own"
                )
            if window.card.id != second.id:
                problems.append(
                    "absorbing a new card yanked the owner off the card they were reading"
                )
            if "another session" not in (window._status or ""):  # noqa: SLF001
                problems.append("the new card arrived with nothing on screen saying so")
            if window._reply_text() != "half-typed steer that must survive":  # noqa: SLF001
                problems.append("absorbing a card ate the sentence the owner was typing")

        # Put the queue back to one card so the close-on-answer check below is
        # asserting what it says it is.
        store.cancel(third.id, note="self-test")
        window._tick()  # noqa: SLF001

        # A card answered on ANOTHER surface (phone, cockpit, another terminal)
        # must close this window rather than leaving buttons for a decision that
        # is already made — clicking one would then lose the race and error.
        store.answer(second.id, "y", via="another-surface")
        window._tick()  # noqa: SLF001 - the poll that notices it
        if alive():
            problems.append("a card answered elsewhere left the window open")
            try:
                window.root.destroy()
            except Exception as exc:  # noqa: BLE001
                print(f"  note: the test window did not destroy cleanly: {exc}")

        if problems:
            for problem in problems:
                print(f"FAIL {problem}")
            return 1
    print("popup self-test passed - full text renders, typing steers, clicking answers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
