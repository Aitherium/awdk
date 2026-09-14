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
  recommendation, ``Ctrl+Enter`` sends the reply box, ``Esc`` snoozes,
  ``Ctrl+←/→`` walks the queue. Every binding is suppressed while the caret is in
  the reply box, so typing "1. do the thing" cannot answer the card.

* **stdlib tkinter, never PyQt.** Same rule as ``secure-human-input``: the
  surface that unblocks a human must not be able to fail on a missing dependency.

* **It is a VIEW over the store.** The window never holds the only copy of
  anything. Close it without answering and the card is still open everywhere
  else; answer it here and every other surface sees the answer.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Optional

from adk.decisions.store import CLOSED_STATUSES, DecisionCard, DecisionStore

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
#: terminal. Height is computed per card and capped against the screen.
WIDTH = 660
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
        #: "card" or "explore". The window is one surface with two views rather
        #: than two windows: a second window for context would have to be found,
        #: raised and closed separately, which is three more things to do while
        #: deciding.
        self._mode = "card"
        self._context: dict = {}

        self.queue: list[DecisionCard] = self._refresh_queue()
        self.index = 0
        if start_id:
            for position, entry in enumerate(self.queue):
                if entry.id == start_id:
                    self.index = position
                    break
        if not self.queue:
            raise RuntimeError("no open cards to show")

        root = tk.Tk()
        self.root = root
        if self._headless:
            # BEFORE any render, lift or focus_force — withdrawing afterwards is
            # what made the old self-test flash a window and take focus.
            root.withdraw()
        root.title(f"AitherOS · {KIND_HEADER.get(self.card.kind, 'DECISION')}")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        if use_native_chrome():
            _apply_dark_titlebar(root)
        else:
            root.overrideredirect(True)

        self.body_holder = tk.Frame(root, bg=BG)
        self.body_holder.pack(fill="both", expand=True)

        self._bind_keys()
        self._render()

    # ── queue ─────────────────────────────────────────────────────────────────

    @property
    def card(self) -> DecisionCard:
        return self.queue[min(self.index, len(self.queue) - 1)]

    def _refresh_queue(self) -> list[DecisionCard]:
        """Open cards, oldest first, re-read from the store on every render.

        Re-reading is what lets one window absorb cards raised while it is up —
        the alternative (snapshot at startup) is how a second window used to get
        spawned for a card that arrived thirty seconds later.
        """
        try:
            cards = self.store.list()
        except OSError:
            cards = []
        if self._injected:
            known = {c.id for c in cards}
            cards = [c for c in self._injected if c.id not in known] + cards
        return cards

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
        root.bind("<Control-Right>", lambda _e: self._step(1))
        root.bind("<Control-Left>", lambda _e: self._step(-1))
        for index in range(1, 10):
            root.bind(str(index), lambda _e, i=index: self._pick_index(i))

    def _on_escape(self, _event=None):
        if self._typing():
            self.root.focus_set()          # leave the box, keep the card open
            return "break"
        self._snooze()
        return "break"

    def _on_return(self, _event=None):
        if self._typing():
            return None                    # a newline in the reply box
        self._take_recommended()
        return "break"

    # ── layout ────────────────────────────────────────────────────────────────

    def _render(self) -> None:
        """Rebuild the window for the current card."""
        for child in self.body_holder.winfo_children():
            child.destroy()

        tk = self.tk
        card = self.card
        accent = URGENCY_COLOUR.get(card.urgency, ACCENT)

        outer = tk.Frame(self.body_holder, bg=EDGE, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=BG)
        inner.pack(fill="both", expand=True)

        self._chrome(inner, card, accent)
        self._scroll_body(inner, card, accent)
        self._reply_row(inner, card)
        self._footer(inner, card)
        if self._mode == "explore":
            self._drain_context()

        self.root.title(f"AitherOS · {KIND_HEADER.get(card.kind, 'DECISION')}")
        if not self._headless:
            self._place()

            if card.urgency in ("high", "critical"):
                self.root.lift()
                self.root.focus_force()
            else:
                self.root.lift()

        # Always poll, deadline or not. The first version only ticked when the
        # card had a deadline, so a card answered from the phone, the cockpit or
        # another terminal left this window sitting there offering buttons for a
        # decision that was already made — and clicking one produced a
        # lost-the-race error instead of just closing.
        self._tick()

    def _chrome(self, parent, card, accent) -> None:
        """Our own title bar: urgency stripe, kind, queue position, pin, close."""
        tk = self.tk
        stripe = tk.Frame(parent, bg=accent, height=3)
        stripe.pack(fill="x")

        bar = tk.Frame(parent, bg=CHROME, padx=14, pady=8)
        bar.pack(fill="x")

        tk.Label(bar, text=KIND_HEADER.get(card.kind, "DECISION NEEDED"),
                 bg=CHROME, fg=accent, font=(UI, 9, "bold")).pack(side="left")

        if len(self.queue) > 1:
            tk.Label(bar, text=f"   {self.index + 1} of {len(self.queue)}",
                     bg=CHROME, fg=MUTED, font=(UI, 9)).pack(side="left")
            self._chrome_button(bar, "›", lambda: self._step(1), "next card  (Ctrl+→)")
            self._chrome_button(bar, "‹", lambda: self._step(-1), "previous  (Ctrl+←)")

        self._chrome_button(bar, "✕", self._snooze, "close — the card stays open  (Esc)")
        if self._mode == "explore":
            self._chrome_button(bar, "‹ card", self._close_explore, "back to the decision")
        else:
            self._chrome_button(bar, "⌕ context", self._open_explore,
                                "explore the evidence behind these facts, live")
        self._chrome_button(bar, "◆" if self._pinned else "◇", self._toggle_pin,
                            "keep on top" if not self._pinned else "stop pinning")
        tk.Label(bar, text=card.id, bg=CHROME, fg=MUTED,
                 font=(MONO, 9)).pack(side="right", padx=(0, 10))

        # Drag anywhere on the bar — there is no OS frame to grab.
        for widget in (bar, stripe):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

    def _chrome_button(self, parent, glyph: str, command, tooltip: str) -> None:
        tk = self.tk
        button = tk.Label(parent, text=f" {glyph} ", bg=CHROME, fg=MUTED,
                          font=(UI, 10), cursor="hand2")
        button.pack(side="right")
        button.bind("<Button-1>", lambda _e: command())
        button.bind("<Enter>", lambda _e: (button.configure(fg=TEXT, bg=EDGE),
                                           self._flash(tooltip, MUTED)))
        button.bind("<Leave>", lambda _e: button.configure(fg=MUTED, bg=CHROME))

    def _scroll_body(self, parent, card, accent) -> None:
        """The card content, in a canvas so a long card scrolls instead of clipping."""
        tk = self.tk
        holder = tk.Frame(parent, bg=BG)
        holder.pack(fill="both", expand=True)

        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        canvas.pack(side="left", fill="both", expand=True)
        bar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview,
                           bg=PANEL, troughcolor=BG, activebackground=EDGE,
                           relief="flat", bd=0, width=10)
        body = tk.Frame(canvas, bg=BG, padx=18, pady=14)
        window = canvas.create_window((0, 0), window=body, anchor="nw", width=WIDTH - 12)
        self._canvas = canvas
        self._scrollbar = bar
        self._scroll_holder = holder
        self._content_frame = body

        def resized(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())
            # The bar appears only when there is something to scroll: a permanent
            # empty gutter on a short card reads as a broken layout.
            needed = body.winfo_reqheight() > canvas.winfo_height() + 2
            if needed and not bar.winfo_ismapped():
                bar.pack(side="right", fill="y")
            elif not needed and bar.winfo_ismapped():
                bar.pack_forget()

        body.bind("<Configure>", resized)
        canvas.bind("<Configure>", resized)

        def wheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        for widget in (canvas, body):
            widget.bind("<MouseWheel>", wheel)

        if self._mode == "explore":
            self._explore_content(body, wheel)
        else:
            self._content(body, card, accent, wheel)

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
        """A masked field whose value goes to the vault and nowhere else.

        The value's whole journey is: this widget -> ``secure_prompt.
        vault_credential`` -> the store that owns the card's
        ``credential_scope`` (platform vault, user or workspace
        lockbox). It is never put
        in a card field, never passed to ``store.steer`` (which copies text
        into session transcripts), never logged, and never sent as the answer
        — ``store.answer`` takes the CREDENTIAL_ANSWER marker only, and the
        push happens before it.

        The push is done on a WORKER THREAD. It is a network round trip, and
        doing it inline freezes the Tk event loop: the window stops
        repainting, which reads as the app hanging on the one interaction
        where the owner most needs to know what happened.
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
                 text="Typed here it goes straight to that store — it is not "
                      "stored on the card and never reaches a transcript.",
                 bg=BG, fg=MUTED, font=(UI, 9), wraplength=WIDTH - 60,
                 justify="left", anchor="w").pack(fill="x", pady=(2, 6))

        entry = tk.Entry(frame, show="•", bg=PANEL, fg=TEXT,
                         insertbackground=TEXT, relief="flat",
                         font=(MONO, 11), highlightthickness=1,
                         highlightbackground=EDGE, highlightcolor=ACCENT)
        entry.pack(fill="x", ipady=6)
        self._credential_entry = entry
        self._credential_status = tk.Label(
            frame, text="", bg=BG, fg=MUTED, font=(UI, 9),
            wraplength=WIDTH - 60, justify="left", anchor="w")
        self._credential_status.pack(fill="x", pady=(6, 0))

        row = tk.Frame(frame, bg=BG)
        row.pack(fill="x", pady=(8, 0))
        self._plain_button(row, "Vault it  (Ctrl+Enter)",
                           self._submit_credential, primary=True, side="left")

        # Reveal is HELD, never toggled, and starts masked. A toggle is a state
        # someone can leave on: the field then stays readable behind whatever
        # window comes next, and over a shoulder or a screen share the owner has
        # no signal that it is still showing. Holding a button is self-cancelling
        # — the secret is visible exactly as long as a finger is down.
        reveal = tk.Label(row, text="  👁 hold to reveal  ", bg=PANEL, fg=SOFT,
                          font=(UI, 9), padx=6, pady=5, cursor="hand2")
        reveal.pack(side="left", padx=(0, 8), pady=(0, 2))
        reveal.bind("<ButtonPress-1>", lambda _e: entry.configure(show=""))
        for unmask in ("<ButtonRelease-1>", "<Leave>"):
            # <Leave> as well as the release: if the pointer leaves the label
            # mid-hold the release can land elsewhere, and a field that stayed
            # unmasked because of a mouse gesture is the exact state this
            # control is shaped to make impossible.
            reveal.bind(unmask, lambda _e: entry.configure(show="•"))

        entry.bind("<Control-Return>", lambda _e: self._submit_credential())
        entry.focus_set()

    def _submit_credential(self) -> None:
        """Read the field, clear it, push on a worker, report the real outcome."""
        import threading

        entry = getattr(self, "_credential_entry", None)
        status = getattr(self, "_credential_status", None)
        if entry is None:
            return
        value = entry.get()
        # Cleared BEFORE anything else can fail: a secret left sitting in a
        # visible widget after a failed push is the same disclosure as a
        # successful one, and a retry would then double-submit it.
        entry.delete(0, "end")
        if not value:
            if status is not None:
                status.configure(text="Nothing entered — the card stays open.",
                                 fg=GOLD)
            return
        if status is not None:
            status.configure(text="Writing to the vault…", fg=SOFT)
        card_id = self.card.id

        def worker(secret: str) -> None:
            from adk.decisions.secure_prompt import vault_credential
            from adk.decisions.store import DecisionError

            try:
                code, detail = vault_credential(card_id, self.store, secret,
                                                via="popup-masked")
            except DecisionError as exc:
                code, detail = 3, str(exc)
            finally:
                del secret
            self.root.after(0, lambda: self._credential_done(code, detail))

        threading.Thread(target=worker, args=(value,), daemon=True).start()
        del value

    def _credential_done(self, code: int, detail: str) -> None:
        """Report the push outcome. Only code 0 closes the card."""
        status = getattr(self, "_credential_status", None)
        if code == 0:
            self.handled += 1
            self._advance()
            return
        # Every non-zero path leaves the card OPEN on purpose — a credential
        # ask that closes on a failed write loses the ask silently, and the
        # next person sees a resolved card and an absent secret.
        #
        # `detail` names WHICH door was shut. The version of this that just
        # said "vault write failed" was measured live and is what sent the
        # owner back to the terminal: three legs had failed for three
        # different reasons and the message distinguished none of them.
        messages = {
            1: "Vault write FAILED — card still OPEN, nothing lost. Retry "
               "after fixing: " + (detail or "no detail"),
            2: "Nothing entered — the card stays open.",
            3: detail or "That card is no longer a credential ask.",
        }
        if status is not None:
            status.configure(text=messages.get(code, f"Unexpected result {code}"),
                             fg=RED if code != 2 else GOLD)

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
            self._place()
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
        # Grow to fit what was just revealed. Without this the panel expands
        # INSIDE a window whose height was computed before the click, so the
        # rows the owner just opened sit below the bottom edge — an explorer
        # that hides what you asked it to show.
        self._place()

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
                               hint="types the reply box into the session, as if you had")

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

    def _footer(self, parent, card) -> None:
        tk = self.tk
        foot = tk.Frame(parent, bg=CHROME, padx=18, pady=9)
        foot.pack(fill="x")

        self.countdown = tk.Label(
            foot, text=(self._status or _fmt_left(card.seconds_left)), bg=CHROME,
            fg=(self._status_colour if self._status
                else (GOLD if card.deadline is not None else MUTED)),
            font=(UI, 8), anchor="w", wraplength=WIDTH - 220, justify="left",
        )
        self.countdown.pack(side="left")

        source = card.source
        where = " · ".join(p for p in (source.agent, source.branch) if p)
        meta = f"{_fmt_age(card.age_seconds)}" + (f"  ·  {where}" if where else "")
        tk.Label(foot, text=meta, bg=CHROME, fg=MUTED,
                 font=(UI, 8)).pack(side="right")

        # A resize grip, because a borderless window has no OS edge to drag.
        grip = tk.Label(parent, text="◢", bg=CHROME, fg=EDGE, font=(UI, 8),
                        cursor="bottom_right_corner")
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<Button-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)

    # ── window geometry ───────────────────────────────────────────────────────

    def _place(self) -> None:
        """Bottom-right for normal cards, centred for the ones that must interrupt."""
        root = self.root
        root.update_idletasks()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        width = WIDTH

        # Size the CANVAS to its content before measuring the window. A canvas
        # reports its own configured height, not the height of the frame inside
        # it, so asking the root for its requested size gives tkinter's default
        # canvas height and the card renders CLIPPED — options cut in half, the
        # terminal controls off the bottom edge. Measured: 452px for a card that
        # needs 720.
        canvas = getattr(self, "_canvas", None)
        content = getattr(self, "_content_frame", None)
        if canvas is not None and content is not None:
            ceiling = int(screen_h * 0.82)
            overhead = max(0, root.winfo_reqheight() - canvas.winfo_reqheight())
            canvas.configure(
                height=max(160, min(content.winfo_reqheight(), ceiling - overhead))
            )
            root.update_idletasks()

        # Grow to fit, but never past the screen: a card taller than the display
        # puts its own buttons off the bottom edge, which is a card that cannot
        # be answered at all.
        wanted = root.winfo_reqheight()
        height = max(320, min(wanted, int(screen_h * 0.82)))
        if getattr(self, "_manual_size", None):
            width, height = self._manual_size
        if self.card.urgency == "critical":
            x = (screen_w - width) // 2
            y = max(24, (screen_h - height) // 3)
        else:
            x = screen_w - width - 24
            y = max(24, screen_h - height - 72)  # clear of the taskbar
        root.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        root.minsize(420, 300)

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root, event.y_root,
                      self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if not getattr(self, "_drag", None):
            return
        start_x, start_y, win_x, win_y = self._drag
        self.root.geometry(
            f"+{win_x + event.x_root - start_x}+{win_y + event.y_root - start_y}"
        )

    def _resize_start(self, event) -> None:
        self._resize = (event.x_root, event.y_root,
                        self.root.winfo_width(), self.root.winfo_height())

    def _resize_move(self, event) -> None:
        if not getattr(self, "_resize", None):
            return
        start_x, start_y, width, height = self._resize
        new_w = max(420, width + event.x_root - start_x)
        new_h = max(300, height + event.y_root - start_y)
        self._manual_size = (new_w, new_h)
        self.root.geometry(f"{new_w}x{new_h}")

    def _toggle_pin(self) -> None:
        self._pinned = not self._pinned
        self.root.attributes("-topmost", True if self._pinned else True)
        self._flash("pinned — stays above everything" if self._pinned
                    else "unpinned (still on top until answered)", MUTED)
        self._render()

    # ── actions ───────────────────────────────────────────────────────────────

    def _step(self, delta: int) -> None:
        """Walk the queue WITHOUT answering anything."""
        self.queue = self._refresh_queue()
        if not self.queue:
            self.root.destroy()
            return
        self.index = (self.index + delta) % len(self.queue)
        # The panel is about ONE card's evidence; carrying it to the next card
        # would show the previous card's context under the new card's question.
        self._mode = "card"
        self._context = {}
        self._render()

    def _pick_index(self, index: int) -> None:
        if self._typing():
            return
        if 1 <= index <= len(self.card.options):
            self._answer(self.card.options[index - 1].key)

    def _take_recommended(self) -> None:
        key = self.card.recommended_key()
        if key:
            self._answer(key)

    def _reply_text(self) -> str:
        box = getattr(self, "reply", None)
        if box is None or self._reply_empty:
            return ""
        return box.get("1.0", "end").strip()

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
        from adk.decisions.store import DecisionError

        note = self._reply_text()
        try:
            self.store.answer(self.card.id, key, note=note, via="popup")
            self.answered = key
            self.handled += 1
        except DecisionError as exc:
            # Somebody answered it elsewhere first. Say so rather than closing
            # silently, which would read as "my click worked".
            self._flash(str(exc), RED)
            return
        self._advance()

    def _dismiss(self) -> None:
        """Close an optionless card for good, rather than leaving it pending."""
        from adk.decisions.store import DecisionError

        try:
            self.store.cancel(self.card.id, note="acknowledged from the card window")
            self.handled += 1
        except DecisionError as exc:
            self._flash(str(exc), RED)
            return
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
        self.queue = self._refresh_queue()
        if not self.queue:
            self.root.destroy()
            return
        self.index = min(self.index, len(self.queue) - 1)
        self._status = ""
        self._render()

    def _focus_terminal(self) -> None:
        from adk.decisions import terminal

        ok, why = terminal.focus(self.card.source.session_pid)
        self._flash(why, GREEN if ok else GOLD)

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
        ok, why = terminal.type_into_console(self.card.source.session_pid, text)
        if ok:
            self.reply.delete("1.0", "end")
            self._reply_empty = True
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
            showing = self.card.id
            self.queue = fresh
            self.index = next(
                (i for i, c in enumerate(fresh) if c.id == showing),
                min(self.index, len(fresh) - 1),
            )
            if arrived:
                # Say it rather than silently growing the counter: a card that
                # arrived behind the one on screen is invisible otherwise.
                plural = "s" if len(arrived) > 1 else ""
                self._status = (
                    f"+{len(arrived)} new card{plural} from another session"
                    f" — Ctrl+→ to see {'them' if plural else 'it'}"
                )
                self._status_colour = GOLD
            # _render() re-arms the tick, so return rather than scheduling a
            # second chain (see the note at the top of this method).
            self._rerender_preserving_reply()
            return

        try:
            card = self.store.get(self.card.id) or self.card
        except OSError:
            # An unreadable store must not close a card window that is showing a
            # real question. Keep the card on screen and try again next second.
            card = self.card
        if card.status in CLOSED_STATUSES:
            self._advance()
            return
        if not self._status:
            countdown = getattr(self, "countdown", None)
            if countdown is not None:
                countdown.configure(text=_fmt_left(card.seconds_left))
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
        if "…" in joined or "..." in joined:
            problems.append("something in the window is still truncated")
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
