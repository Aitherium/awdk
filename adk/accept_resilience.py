"""Keep a Windows asyncio server listening when one client vanishes mid-accept.

WHY: on Windows the proactor event loop accepts with ``AcceptEx``. When a client
disconnects before that completes, the accept future fails with WinError 64
(ERROR_NETNAME_DELETED). CPython's ``BaseProactorEventLoop._start_serving`` treats
ANY ``OSError`` there as fatal: it logs "Accept failed on a socket" and CLOSES THE
LISTENING SOCKET. The process stays alive and keeps logging, ``/health`` goes dark,
and every client (awsh, the OS, the watchdog) sees a dead daemon. Measured on one
desktop host: 63 such deaths of the :9001 daemon between 2026-10-09 and 2026-10-12,
one of them between two consecutive awsh turns.

The fix retries the accept on the listener when the failure belongs to the departed
client, so one aborted connection costs that connection only. Any other error still
reaches the event loop unchanged.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("adk.accept_resilience")

#: Errors that mean "the peer went away before accept finished", not "the listener
#: is broken": ERROR_NETNAME_DELETED (64), ERROR_CONNECTION_ABORTED (1236).
CLIENT_GONE_WINERRORS = frozenset({64, 1236})

#: A burst of client-gone errors with no success in between is no longer a client
#: problem; let the loop see the error as it did before.
MAX_CONSECUTIVE_RETRIES = 64


def client_gone(exc: BaseException | None) -> bool:
    """True when ``exc`` is an accept failure caused by the client disconnecting."""
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in CLIENT_GONE_WINERRORS


def resilient_accept(orig_accept):
    """Wrap ``IocpProactor.accept`` so a client-gone failure re-arms the accept."""

    def accept(self, listener):
        outer = self._loop.create_future()
        current: list = [None]
        retries = [0]

        def _cancel_inner(fut) -> None:
            if fut.cancelled() and current[0] is not None and not current[0].done():
                current[0].cancel()

        outer.add_done_callback(_cancel_inner)

        def _arm() -> None:
            try:
                inner = orig_accept(self, listener)
            except OSError as exc:
                if not outer.done():
                    outer.set_exception(exc)
                return
            current[0] = inner
            inner.add_done_callback(_done)

        def _done(inner) -> None:
            if outer.done():
                return
            if inner.cancelled():
                outer.cancel()
                return
            exc = inner.exception()
            if exc is None:
                outer.set_result(inner.result())
                return
            if client_gone(exc) and retries[0] < MAX_CONSECUTIVE_RETRIES:
                retries[0] += 1
                logger.info("accept: client left before it completed (%s) - listener kept",
                            getattr(exc, "winerror", exc))
                _arm()
                return
            outer.set_exception(exc)

        _arm()
        return outer

    accept._adk_resilient = True  # type: ignore[attr-defined]
    return accept


def install() -> bool:
    """Patch the Windows proactor once. Returns True when the patch is in place."""
    if sys.platform != "win32":
        return False
    try:
        from asyncio import windows_events
    except ImportError:
        return False
    current = windows_events.IocpProactor.accept
    if getattr(current, "_adk_resilient", False):
        return True
    windows_events.IocpProactor.accept = resilient_accept(current)
    return True
