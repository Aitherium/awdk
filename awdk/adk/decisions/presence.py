"""Is the owner at the desk right now?

WHY THIS EXISTS. Cards deliver two ways at once: a popup window on the desktop
and a DM to the phone. That is correct when the owner is away and actively
annoying when they are not — the same card arrives on a screen they are looking
at AND buzzes in their pocket. Slack solved this years ago: your phone stays
quiet while the desktop client is active, and starts buzzing once you leave.
This is that rule.

THE FAILURE DIRECTIONS ARE NOT SYMMETRIC, AND THAT DECIDES THE DEFAULT.

Suppressing a DM that should have been sent means a decision waits, unseen,
possibly for hours — the exact failure decision cards exist to prevent. Sending
a DM that could have been suppressed costs one redundant buzz.

So this fails toward DELIVERING. `is_at_desk()` returns True only when idle
time was actually MEASURED and is under the window; every unmeasurable case —
no API, a non-Windows host, a permission error — returns False, meaning "assume
away, send the DM".

That is deliberately the opposite of the authorization rules next door in
`channels.py`, which fail CLOSED. The distinction is what the failure costs:
answering a card is an authorization decision where a wrong yes steers a coding
agent with filesystem access, while notifying is a courtesy where a wrong yes
costs a notification. Do not "make these consistent".

CRITICAL IS NEVER SUPPRESSED. Presence changes whether a REDUNDANT channel
fires, never whether an urgent card reaches somebody. A card that would wake
you at 3am is not the place to be clever about focus.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

#: How long after the last keystroke or mouse move the owner still counts as
#: "at the desk". Slack uses a comparable window. Long enough to cover reading
#: a card and thinking about it; short enough that walking away for a coffee
#: starts routing to the phone.
AT_DESK_WINDOW_S = 300.0

#: Opt out entirely — every card DMs regardless of presence.
DISABLE_ENV = "AITHER_DECISIONS_IGNORE_PRESENCE"

#: Distinct from None, and that distinction is load-bearing. `None` is a REAL
#: value here meaning UNMEASURED, so it cannot also mean "argument omitted" —
#: conflating them made an explicit `idle_seconds=None` fall through to a live
#: probe, so a caller could not say "I looked and got nothing". Caught by the
#: self-test rather than in production, which is the only reason it is a
#: sentinel and not a bug.
_UNSET = object()


def desktop_idle_seconds() -> Optional[float]:
    """Seconds since the last user input on this desktop, or None.

    None means UNMEASURED, never "idle forever" and never zero. A caller that
    treats None as either one is making up a presence signal, which is how a
    notification gets suppressed on evidence nobody gathered.

    Windows only for now: `GetLastInputInfo` is the only one of these that is
    reliable, cheap and needs no extra privilege. A Linux/macOS implementation
    would go here; until it exists those hosts correctly report None and always
    get the DM.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class _LastInput(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = _LastInput()
        info.cbSize = ctypes.sizeof(_LastInput)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
        # A negative or absurd delta means the tick counter wrapped (49.7 days)
        # or the call raced. Unmeasured beats a fabricated zero, which would
        # read as "at the desk" and silence the phone.
        if millis < 0 or millis > 7 * 24 * 3600 * 1000:
            return None
        return millis / 1000.0
    except Exception:  # noqa: BLE001 - unmeasured, see the module docstring
        return None


def is_at_desk(window_s: float = AT_DESK_WINDOW_S,
               idle_seconds: Any = _UNSET) -> bool:
    """True ONLY when measured idle time is inside the window.

    `idle_seconds` is injectable so the rule can be tested without a desktop —
    the arithmetic is the part worth pinning, and a test that needs a human to
    stop typing is not a test.
    """
    if os.environ.get(DISABLE_ENV, "").strip().lower() in ("1", "true", "yes"):
        return False
    idle = desktop_idle_seconds() if idle_seconds is _UNSET else idle_seconds
    if idle is None:
        return False  # unmeasured => assume away => deliver
    return idle < window_s


def presence_note(idle_seconds: Any = _UNSET) -> str:
    """One line for a log, so a suppressed DM is explainable after the fact.

    A notification that silently did not happen is indistinguishable from one
    that failed, which is why this exists rather than a bare boolean.
    """
    idle = desktop_idle_seconds() if idle_seconds is _UNSET else idle_seconds
    if idle is None:
        return "presence UNMEASURED — delivering (fail toward the notification)"
    if idle < AT_DESK_WINDOW_S:
        return (f"owner at desk ({idle:.0f}s idle < {AT_DESK_WINDOW_S:.0f}s) — "
                f"popup is enough, suppressing the DM")
    return f"owner away ({idle:.0f}s idle) — delivering"


def _self_test() -> int:
    ok = True

    def check(cond: bool, what: str) -> None:
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'} {what}")
        if not cond:
            ok = False

    check(is_at_desk(idle_seconds=10.0) is True,
          "10s idle counts as at the desk")
    check(is_at_desk(idle_seconds=AT_DESK_WINDOW_S + 1) is False,
          "past the window counts as away")
    check(is_at_desk(idle_seconds=None) is False,
          "UNMEASURED must mean away — suppressing on evidence nobody gathered "
          "is how a decision waits unseen for hours")

    os.environ[DISABLE_ENV] = "1"
    check(is_at_desk(idle_seconds=1.0) is False,
          "the opt-out forces delivery even while sitting at the desk")
    os.environ.pop(DISABLE_ENV, None)

    check("UNMEASURED" in presence_note(idle_seconds=None),
          "an unmeasured presence says so, so a suppressed DM is explainable")
    check("suppressing" in presence_note(idle_seconds=5.0),
          "the at-desk note names the suppression")

    live = desktop_idle_seconds()
    check(live is None or live >= 0,
          f"live probe returns None or a non-negative number (got {live})")

    print("self-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    idle = desktop_idle_seconds()
    print(f"idle_seconds={idle}")
    print(presence_note())
