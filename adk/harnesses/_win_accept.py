"""Keep a Windows asyncio server listening through a client reset.

CPython's proactor accept loop (``BaseProactorEventLoop._start_serving``) CLOSES
the listening socket on any ``OSError`` from ``AcceptEx``. A client that resets
mid-handshake surfaces as WinError 64 ("network name is no longer available"),
so one flaky peer leaves the process alive, the port closed, and every caller
refused -- the daemon looks up to a supervisor that only checks the PID.

``install()`` wraps ``IocpProactor.accept`` so a transient accept error re-arms
the accept on the same listener instead of reaching that ``sock.close()``.
Anything else still propagates unchanged. No-op off Windows.
"""

from __future__ import annotations

import sys

# WSAENETNAMEDELETED, ERROR_NETNAME_DELETED, WSAECONNABORTED, WSAECONNRESET,
# ERROR_SEM_TIMEOUT: the peer went away between SYN and AcceptEx completion.
TRANSIENT_WINERRORS = frozenset({64, 10053, 10054, 121})

_installed = False


def is_transient(exc: BaseException | None) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in TRANSIENT_WINERRORS


def install() -> bool:
    """Patch the IOCP proactor once. Returns True when the patch is active."""
    global _installed
    if _installed:
        return True
    if sys.platform != "win32":
        return False
    from asyncio import windows_events

    original = windows_events.IocpProactor.accept

    def accept(self, listener):
        outer = self._loop.create_future()

        def arm() -> None:
            inner = original(self, listener)
            inner.add_done_callback(settle)

            def cancel_inner(fut) -> None:
                if fut.cancelled() and not inner.done():
                    inner.cancel()

            outer.add_done_callback(cancel_inner)

        def settle(inner) -> None:
            if outer.done():
                return
            if inner.cancelled():
                outer.cancel()
                return
            exc = inner.exception()
            if exc is None:
                outer.set_result(inner.result())
            elif is_transient(exc) and listener.fileno() != -1:
                arm()
            else:
                outer.set_exception(exc)

        arm()
        return outer

    windows_events.IocpProactor.accept = accept
    _installed = True
    return True
