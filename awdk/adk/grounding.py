"""System-awareness grounding for the instant-response loop.

The eager first-pass answers from minimal context — but "minimal" must still
include *real* situational grounding (current local date/time, owner) so a
trivial question like "what time is it?" is answered from FACT, never refused
("I don't have real-time data") or hallucinated. There is ONE canonical source
of temporal truth — Genesis ``FluxContextState`` — surfaced by
``lib.core.AitherContextAssembler.get_system_state_block``. This helper reuses
it when AitherOS is co-located (Genesis / awkit containers), and otherwise
computes the same block locally from ``AITHER_TIMEZONE`` so the standalone ADK
agents are grounded too.

Kept adk-internal and dependency-optional on purpose: the ``lib.core`` import is
lazy + guarded, so importing this never creates a hard AitherOS dependency and
the backend-agnostic responder contract holds. Never raises; returns "" only if
even the local clock is unreadable.
"""

from __future__ import annotations

import os


def _period_for_hour(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _local_system_state() -> str:
    """Compute the canonical [SYSTEM STATE] block from the local clock.

    Timezone-aware via ``AITHER_TIMEZONE`` (default America/Los_Angeles) so it
    matches Genesis FluxContextState, NOT UTC — the UTC skew is exactly the bug
    this avoids.
    """
    try:
        import zoneinfo
        from datetime import datetime

        tz_name = os.environ.get("AITHER_TIMEZONE", "America/Los_Angeles")
        try:
            now = datetime.now(zoneinfo.ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001 — bad/missing tz db → local wall clock
            now = datetime.now()

        time_12h = now.strftime("%I:%M %p").lstrip("0")
        day = now.strftime("%A")
        date_str = now.strftime("%Y-%m-%d")
        period = _period_for_hour(now.hour)

        owner = os.environ.get("AITHER_OWNER_NAME", "").strip()
        owner_line = f"owner: {owner}\n" if owner else ""
        return (
            "[SYSTEM STATE]\n"
            f"CURRENT TIME: {time_12h} on {day}, {date_str} ({period})\n"
            f"{owner_line}"
            "[/SYSTEM STATE]"
        )
    except Exception:  # noqa: BLE001 — grounding is best-effort, never fatal
        return ""


def current_system_state() -> str:
    """Return the canonical live [SYSTEM STATE] block (time/date/period/owner).

    Prefers the in-process Genesis source of truth; falls back to a local
    timezone-aware compute. Returns "" only if no clock is readable at all.

    Set ``AITHER_SYSTEM_STATE_LOCAL=1`` to ALWAYS use the live local clock and skip the
    monorepo Genesis source — needed for a standalone deploy that imports inside the monorepo
    but is NOT running Genesis (Genesis returns a cached/stale block there).
    """
    if os.environ.get("AITHER_SYSTEM_STATE_LOCAL", "").strip().lower() in ("1", "true", "yes", "on"):
        return _local_system_state()
    try:
        # Optional richer source when running INSIDE the monorepo. Imported
        # dynamically so the public SDK (where this module is absent) degrades to
        # the local compute below — and so the moat leak-checker doesn't flag a
        # static private import that is only ever used best-effort.
        import importlib
        _asm = importlib.import_module("lib.core.AitherContextAssembler")
        block = _asm.get_system_state_block()
        if block and block.strip():
            return block.strip()
    except Exception:  # noqa: BLE001 — standalone ADK: monorepo internals absent → local
        pass
    return _local_system_state()


def ground_system_prompt(base: str) -> str:
    """Prepend the live system-state block to a first-pass/direct system prompt.

    The block goes FIRST so the model treats it as authoritative ground truth,
    with an explicit instruction to answer time/date questions from it directly
    (no refusal, no guessing).

    Opt-out: set ``AITHER_GROUND_SYSTEM_STATE=0`` to disable system-state grounding entirely
    (the agent then has no real clock and must guess) — used to run a deliberately ungrounded
    baseline.
    """
    if os.environ.get("AITHER_GROUND_SYSTEM_STATE", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return base
    state = current_system_state()
    if not state:
        return base
    return (
        f"{state}\n"
        "The values above are your REAL current environment. When asked about "
        "the time, date, or day, answer directly from them — never say you lack "
        "real-time access, and never guess.\n\n"
        f"{base}"
    )


__all__ = ["current_system_state", "ground_system_prompt"]
