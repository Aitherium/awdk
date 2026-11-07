"""Lead-with-the-next-action rendering for human CLI output.

Status-style commands (``adk doctor``, ``adk claude doctor``, ``adk jobs``,
``adk grid status``) share one layout so a reader who sees only the first and
last line still knows what to do:

1. Line 1 is an action computed from real state -- ``Next: run `adk X```, or
   ``Nothing needs you.`` when nothing does.
2. Multi-step work is numbered, one bounded action per step.
3. A visible list holds at most five ranked items, then ``+N more (<cmd>)``.
   The cap shapes PRESENTATION only; callers keep every item and the rest is
   one command away (``--all``).
4. A count is paired with its remedy: ``3 checks failing — run `adk setup```.
5. Every line fits 80 columns.

Machine output (``--json``) never goes through this module.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple, TypeVar, Union

T = TypeVar("T")

#: Terminal width every rendered line must fit.
WIDTH = 80
#: Most items one visible group may hold before ``+N more``.
MAX_VISIBLE = 5
#: Line 1 when nothing needs the reader.
IDLE = "Nothing needs you."

_ELLIPSIS = "..."


def fit(text: str, width: int = WIDTH) -> str:
    """Return ``text`` cut to ``width`` columns, marking the cut with ``...``.

    Args:
        text: One line of output (no newlines expected).
        width: Column budget.

    Returns:
        ``text`` unchanged when it fits, else a truncated copy ending in ``...``.
    """
    text = text.rstrip()
    if len(text) <= width:
        return text
    return text[: max(0, width - len(_ELLIPSIS))].rstrip() + _ELLIPSIS


def run(command: str) -> str:
    """Phrase a shell command as an action: ``run `adk setup```."""
    return f"run `{command}`"


def next_line(action: Optional[str], why: str = "", *, idle: str = IDLE,
              width: int = WIDTH) -> str:
    """The first (and usually last) line of a status screen.

    Args:
        action: What the reader should do now, e.g. ``run(\"adk setup\")``.
            ``None`` or empty means nothing needs them.
        why: Short reason appended in parentheses (dropped first when the line
            would overflow).
        idle: Line to print when there is no action.
        width: Column budget.

    Returns:
        ``Next: <action> (<why>)`` fitted to ``width``, or ``idle``.
    """
    if not action:
        return fit(idle, width)
    head = f"Next: {action}"
    if why:
        full = f"{head} ({why})"
        if len(full) <= width:
            return full
        # Keep the action whole; the reason is what gives way.
        room = width - len(head) - 3
        if room >= 12:
            return f"{head} ({fit(why, room)})"
    return fit(head, width)


def cap(items: Sequence[T], limit: int = MAX_VISIBLE,
        show_all: bool = False) -> Tuple[List[T], int]:
    """Split ranked ``items`` into (shown, hidden_count).

    Nothing is dropped: the caller still holds ``items``; ``hidden_count`` is
    what the ``+N more`` line must report.
    """
    seq = list(items)
    if show_all or len(seq) <= limit:
        return seq, 0
    return seq[:limit], len(seq) - limit


def more_line(hidden: int, more_cmd: str = "", indent: str = "  ",
              width: int = WIDTH) -> str:
    """``+N more (<more_cmd>)`` -- the remainder, one action away."""
    tail = f" ({more_cmd})" if more_cmd else ""
    return fit(f"{indent}+{hidden} more{tail}", width)


def count_line(n: int, noun: str, remedy: str = "", *, width: int = WIDTH) -> str:
    """A count paired with its remedy: ``3 checks failing — run `adk setup```.

    Args:
        n: The count.
        noun: What is counted, already phrased for ``n`` (``"checks failing"``).
        remedy: The action that starts fixing it; omitted when ``n`` is 0.
        width: Column budget.
    """
    base = f"{n} {noun}"
    if remedy and n:
        return fit(f"{base} — {remedy}", width)
    return fit(base, width)


def numbered(lines: Sequence[Union[str, Tuple[str, str]]], *, limit: int = MAX_VISIBLE,
             show_all: bool = False, more_cmd: str = "", indent: str = "  ",
             width: int = WIDTH, start: int = 1) -> List[str]:
    """Render ranked ``lines`` as ``1. ...`` steps, capped, each fitted to ``width``.

    Args:
        lines: Already-ranked items, most important first. An item is a display
            string, or ``(text, remedy)`` -- the remedy prints on its own
            ``-> ...`` line under the step, so a count never loses its fix.
        limit: Visible cap (ignored when ``show_all``).
        show_all: Render every line (the ``--all`` path).
        more_cmd: Command that shows the rest, printed on the ``+N more`` line.
        indent: Left margin.
        width: Column budget.
        start: First step number.

    Returns:
        Display lines; a final ``+N more (<more_cmd>)`` when anything was capped.
    """
    shown, hidden = cap(lines, limit, show_all)
    out: List[str] = []
    for i, item in enumerate(shown, start):
        text, detail = (item, "") if isinstance(item, str) else item
        prefix = f"{indent}{i}. "
        out.append(fit(prefix + text, width))
        if detail:
            out.append(fit(" " * len(prefix) + "-> " + detail, width))
    if hidden:
        out.append(more_line(hidden, more_cmd, indent, width))
    return out


def names_line(label: str, names: Sequence[str], *, limit: int = MAX_VISIBLE,
               show_all: bool = False, more_cmd: str = "", width: int = WIDTH) -> str:
    """One line listing names, capped: ``Passed (8): a, b, c, d, e, +3 more (cmd)``."""
    shown, hidden = cap(names, limit, show_all)
    body = ", ".join(shown)
    if hidden:
        body += f", +{hidden} more" + (f" ({more_cmd})" if more_cmd else "")
    return fit(f"{label} ({len(names)}): {body}", width)


__all__ = [
    "IDLE", "MAX_VISIBLE", "WIDTH",
    "cap", "count_line", "fit", "more_line", "names_line", "next_line", "numbered", "run",
]
