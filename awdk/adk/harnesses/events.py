"""Normalized event schema shared by every coding harness.

The whole point of AitherShell-as-a-shell-for-shells is that a Claude Code turn,
a Gemini CLI turn and an AitherOS Genesis turn arrive at the UI in the SAME
shape. Each harness adapter translates its native output into these events; the
transport, the daemon and every front-end speak only this vocabulary.

Design rules learned the hard way elsewhere in this repo:

- **Nothing is ever dropped.** An adapter that cannot classify a line emits
  ``RAW`` rather than discarding it. A silently swallowed line is how a broken
  harness reads as "the model had nothing to say".
- **Errors are events, not exceptions.** A harness that dies mid-turn must reach
  the UI as ``ERROR`` + ``SESSION_EXITED``; a UI waiting forever on a stream
  that quietly ended is indistinguishable from a slow model.
- **Every event carries a monotonic ``seq``** so a client that reconnects can
  resume with ``?since=`` instead of replaying the world.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class EventKind(str, Enum):
    """Every event a harness session can emit."""

    SESSION_STARTING = "session.starting"
    SESSION_READY = "session.ready"
    TURN_STARTED = "turn.started"
    TEXT_DELTA = "text.delta"
    THINKING_DELTA = "thinking.delta"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TURN_COMPLETED = "turn.completed"
    USAGE = "usage"
    NOTICE = "notice"
    ERROR = "error"
    SESSION_EXITED = "session.exited"
    RAW = "raw"


#: Kinds after which no further events can arrive for a session.
TERMINAL_KINDS = frozenset({EventKind.SESSION_EXITED})


@dataclass
class HarnessEvent:
    """One normalized event from a harness session.

    ``seq`` is assigned by the session (never by the adapter) so it is
    monotonic across every source that feeds one session — stdout, stderr and
    lifecycle events alike.
    """

    kind: EventKind
    seq: int = 0
    session_id: str = ""
    ts: float = field(default_factory=time.time)

    #: Human-facing text for TEXT_DELTA / THINKING_DELTA / NOTICE / ERROR.
    text: str = ""
    #: Tool name for TOOL_CALL / TOOL_RESULT.
    tool: str = ""
    #: Harness-assigned id correlating a TOOL_CALL with its TOOL_RESULT.
    tool_use_id: str = ""
    #: Structured payload — tool input, tool output, usage numbers, raw line.
    data: dict[str, Any] = field(default_factory=dict)
    #: Turn number this event belongs to (0 before the first turn).
    turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form. ``kind`` flattens to its string value."""
        out = asdict(self)
        out["kind"] = self.kind.value
        return out

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS


def text_delta(text: str, *, turn: int = 0) -> HarnessEvent:
    return HarnessEvent(kind=EventKind.TEXT_DELTA, text=text, turn=turn)


def thinking_delta(text: str, *, turn: int = 0) -> HarnessEvent:
    return HarnessEvent(kind=EventKind.THINKING_DELTA, text=text, turn=turn)


def notice(text: str, **data: Any) -> HarnessEvent:
    return HarnessEvent(kind=EventKind.NOTICE, text=text, data=dict(data))


def error(text: str, **data: Any) -> HarnessEvent:
    return HarnessEvent(kind=EventKind.ERROR, text=text, data=dict(data))


def tool_call(
    tool: str,
    tool_use_id: str = "",
    tool_input: Optional[dict[str, Any]] = None,
    *,
    turn: int = 0,
) -> HarnessEvent:
    return HarnessEvent(
        kind=EventKind.TOOL_CALL,
        tool=tool,
        tool_use_id=tool_use_id,
        data={"input": tool_input or {}},
        turn=turn,
    )


def tool_result(
    tool_use_id: str = "",
    output: Any = None,
    *,
    is_error: bool = False,
    tool: str = "",
    turn: int = 0,
) -> HarnessEvent:
    return HarnessEvent(
        kind=EventKind.TOOL_RESULT,
        tool=tool,
        tool_use_id=tool_use_id,
        data={"output": output, "is_error": is_error},
        turn=turn,
    )


def raw(line: str, **data: Any) -> HarnessEvent:
    """Passthrough for a line no adapter could classify.

    Emitting this is deliberate: an unclassified line is information about a
    harness whose output shape changed, and dropping it makes that invisible.
    """
    payload = {"line": line}
    payload.update(data)
    return HarnessEvent(kind=EventKind.RAW, text=line, data=payload)
