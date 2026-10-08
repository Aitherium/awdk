"""The one seam for "an approval needs a person": Hearth calls :func:`notify_approval`
every time a device approval is created or resolved; nothing else in Hearth sends
those notifications.

Until a notifier is installed it does nothing (the approval still waits in the store
and shows on the household's page). The push lane installs one at start-up::

    from adk.home.devices.notify import set_approval_notifier
    set_approval_notifier(my_push)          # my_push(approval: dict) -> None | Awaitable

``approval`` is the stored row: ``id``, ``status`` (pending | approved | denied |
expired | executed | failed), ``summary``, ``required``, ``approvals`` (yes so far),
``approvers`` (the pids who may answer), ``requested_by`` (pid + name), ``expires_at``
and ``household`` (an opaque key, set by the host). It never carries a token, and it
never carries device state beyond the one action being asked for.

A notifier that raises is logged and swallowed: a dead push channel must never turn
into "the lock did (or did not) open".
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("adk.home.devices.notify")

Notifier = Callable[[Dict[str, Any]], Any]
_notifier: Optional[Notifier] = None


def set_approval_notifier(fn: Optional[Notifier]) -> None:
    """Install (or, with None, remove) the function told about approvals."""
    global _notifier
    _notifier = fn


async def notify_approval(approval: Dict[str, Any]) -> bool:
    """Tell the installed notifier about ``approval``. True when one ran without error."""
    fn = _notifier
    if fn is None:
        return False
    try:
        out = fn(dict(approval))
        if inspect.isawaitable(out):
            await out
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a push failure is logged, never fatal
        logger.warning("approval notifier raised %s for %s", type(exc).__name__,
                       approval.get("id"))
        return False
