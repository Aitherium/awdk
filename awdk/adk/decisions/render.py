"""Render a decision card for a terminal.

This module is the direct answer to the complaint that created this feature: an
important question buried in a dense block of prose in a terminal. The rules the
layout follows are therefore about *scannability*, not decoration:

* **The headline is the first thing and it fits on one line.** If a card cannot be
  summarised in one line it is two cards.
* **Facts are separated from the ask.** Measurements the agent took go in their own
  block, so "what is true" and "what I want from you" are never one paragraph.
* **Options are numbered and one line each.** A phone-lock-screen glance has to be
  enough to pick.
* **The default is the last line.** It is what the reader needs if they intend to
  do nothing, which is the most common intent.

Colour is applied only when writing to a TTY that has not asked for plain output.
Everything degrades to clean ASCII, because these cards are also read from log
files, CI output, hook stderr and a phone over SSH.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Optional, TextIO

from adk.decisions.store import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    DecisionCard,
)

# ── colour ──────────────────────────────────────────────────────────────────────

_RESET = "\x1b[0m"
_STYLES = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
}

_URGENCY_STYLE = {
    "low": "dim",
    "normal": "cyan",
    "high": "yellow",
    "critical": "red",
}


def colour_enabled(stream: TextIO) -> bool:
    """True when ANSI is safe on ``stream``.

    ``NO_COLOR`` and a non-TTY both disable it. ``AITHER_DECISIONS_COLOR=always``
    forces it on, which is what lets a hook pipe a coloured card through to a
    terminal it does not itself own.
    """
    override = os.getenv("AITHER_DECISIONS_COLOR", "").strip().lower()
    if override in ("always", "1", "true", "yes"):
        return True
    if override in ("never", "0", "false", "no"):
        return False
    if os.getenv("NO_COLOR") is not None:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _paint(text: str, style: str, on: bool) -> str:
    if not on or not style:
        return text
    return f"{_STYLES.get(style, '')}{text}{_RESET}"


# ── layout helpers ──────────────────────────────────────────────────────────────


def _wrap(text: str, width: int) -> list[str]:
    """Greedy wrap preserving deliberate newlines.

    Words are kept whole *when they fit*. A token longer than the line — a URL, a
    long path, a hash, a base64 blob — is hard-split instead, because refusing to
    break it would push the row past the frame and shear the right border off. A
    card that renders a broken box gets read as broken output, not as a question.
    """
    width = max(8, int(width))
    lines: list[str] = []
    for paragraph in (text or "").splitlines() or [""]:
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            while len(word) > width:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:width])
                word = word[width:]
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current = f"{current} {word}"
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def human_age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"
    return f"{int(seconds // 86400)}d{int((seconds % 86400) // 3600):02d}h"


def _terminal_width(default: int = 78) -> int:
    try:
        return max(48, min(100, shutil.get_terminal_size((default, 24)).columns - 2))
    except OSError:  # pragma: no cover - exotic terminals
        return default


# ── the card ────────────────────────────────────────────────────────────────────


def render_card(card: DecisionCard, *, width: int = 0, colour: bool = False) -> str:
    """The full card, as a bordered block."""
    w = width or _terminal_width()
    inner = w - 4
    style = _URGENCY_STYLE.get(card.urgency, "cyan")

    header = {
        "decision": "DECISION NEEDED",
        "blocked": "BLOCKED ON YOU",
        "info": "YOU SHOULD KNOW",
    }.get(card.kind, "DECISION NEEDED")
    if card.status == STATUS_ANSWERED:
        header, style = f"ANSWERED · {card.answer}", "green"
    elif card.status == STATUS_EXPIRED:
        header, style = f"EXPIRED · default {card.answer} applied", "dim"
    elif card.status == STATUS_CANCELLED:
        header, style = "WITHDRAWN", "dim"

    # Content is collected as (text, style) pairs that are ALREADY wrapped and
    # already carry their own indentation. Framing happens once, at the end.
    # Wrapping inside the framing loop is what previously ate the indent on
    # continuation lines, so a two-line option consequence lost its alignment and
    # read as a separate bullet.
    body: list[tuple[str, str]] = []

    def line(text: str = "", row_style: str = "") -> None:
        body.append((text, row_style))

    def block(text: str, row_style: str = "", indent: str = "", first: str = "") -> None:
        prefix = first or indent
        for i, chunk in enumerate(_wrap(text, inner - len(prefix))):
            line((prefix if i == 0 else indent) + chunk, row_style)

    block(card.title, "bold")
    if card.summary:
        line()
        block(card.summary)

    if card.facts:
        line()
        line("What I measured:", "dim")
        for fact in card.facts:
            block(fact, "", indent="  ", first="· ")

    if card.detail:
        line()
        block(card.detail, "dim")

    if card.options:
        line()
        line("Your options:", "dim")
        for index, opt in enumerate(card.options, start=1):
            mark = "★" if opt.recommended else " "
            block(f"{opt.label}", "bold" if opt.recommended else "",
                  indent="      ", first=f"{mark}[{index}] {opt.key} — ")
            if opt.consequence:
                block(opt.consequence, "dim", indent="      ")

    src = card.source
    where = " · ".join(p for p in (src.agent, src.branch, src.cwd) if p)
    if where:
        line()
        block(where, "dim")

    if card.is_open:
        line()
        left = card.seconds_left
        if card.default_key and left is not None:
            block(f"If you say nothing: {card.default_key} in {human_age(left)}", style)
        elif card.default_key:
            block(f"If you say nothing: {card.default_key} (no deadline — it waits)", style)
        block(f"Answer:  adk decide answer {card.id} <option>", "dim")
    elif card.answer_note:
        line()
        block(card.answer_note, "dim")

    # ── frame ──────────────────────────────────────────────────────────────────
    # Every row is exactly ``inner + 4`` display columns wide:
    #   "│" + " " + inner + " " + "│". The header row is built to that same total
    #   so the right edge lines up; getting this arithmetic wrong by one is why
    #   the first version's top border overhung the box.
    age = human_age(card.age_seconds)
    right = f"{card.id} · {age} ago"
    lead = f"─ {header} "
    pad = max(1, inner - len(header) - len(right) - 4)
    out: list[str] = [_paint(f"┌{lead}{'─' * pad} {right} ─┐", style, colour)]
    for text, row_style in body:
        painted = _paint(text, row_style, colour) if row_style else text
        out.append(f"│ {painted}{' ' * max(0, inner - len(text))} │")
    out.append(_paint(f"└{'─' * (inner + 2)}┘", style, colour))
    return "\n".join(out)


def render_row(card: DecisionCard, *, colour: bool = False) -> str:
    """One card as a single line, for lists and cockpit grids."""
    style = _URGENCY_STYLE.get(card.urgency, "cyan")
    dot = {"open": "●", "answered": "✓", "expired": "○", "cancelled": "×"}.get(card.status, "?")
    age = human_age(card.age_seconds).rjust(5)
    title = card.title if len(card.title) <= 58 else card.title[:57] + "…"
    tail = ""
    if card.is_open and card.default_key:
        left = card.seconds_left
        tail = f"→ {card.default_key}" + (f" in {human_age(left)}" if left is not None else "")
    elif card.answer:
        tail = f"= {card.answer}"
    marker = _paint(dot, style, colour)
    suffix = _paint(tail, "dim", colour)
    return f"{marker} {card.id}  {age}  {title:<58} {suffix}"


def render_summary(cards: list[DecisionCard]) -> str:
    """The one line a toast or a status bar shows."""
    if not cards:
        return "no decisions waiting"
    if len(cards) == 1:
        return cards[0].title
    oldest = max(cards, key=lambda c: c.age_seconds)
    return (
        f"{len(cards)} decisions waiting · "
        f"oldest {human_age(oldest.age_seconds)}: {oldest.title}"
    )


def print_card(
    card: DecisionCard,
    stream: Optional[TextIO] = None,
    *,
    width: int = 0,
) -> None:
    """Write a card to a stream, choosing colour from that stream."""
    target = stream if stream is not None else sys.stdout
    text = render_card(card, width=width, colour=colour_enabled(target))
    target.write(text + "\n")
    target.flush()


def render_markdown(card: DecisionCard) -> str:
    """The card as Markdown — for the portal, a Relay message, or a GitHub issue."""
    lines = [f"### {card.title}", ""]
    if card.summary:
        lines += [card.summary, ""]
    if card.facts:
        lines.append("**Measured:**")
        lines += [f"- {f}" for f in card.facts]
        lines.append("")
    if card.detail:
        lines += [card.detail, ""]
    if card.options:
        lines.append("**Options:**")
        for opt in card.options:
            star = " *(recommended)*" if opt.recommended else ""
            detail = f" — {opt.consequence}" if opt.consequence else ""
            lines.append(f"- `{opt.key}` **{opt.label}**{star}{detail}")
        lines.append("")
    if card.is_open and card.default_key:
        left = card.seconds_left
        when = f" in {human_age(left)}" if left is not None else " (no deadline)"
        lines.append(f"_If nobody answers: **{card.default_key}**{when}._")
    elif card.answer:
        lines.append(f"_Answered **{card.answer}** via {card.answered_via or 'unknown'}._")
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(card.created_at))
    lines += ["", f"<sub>{card.id} · raised {stamp} · {card.source.host}</sub>"]
    return "\n".join(lines)
