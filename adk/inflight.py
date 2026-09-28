"""Count the chat turns a daemon is serving RIGHT NOW, for /health and the watchdog.

The :9001 watchdog (``check_adk_daemon_capability.py --watchdog``) replaces the daemon
whenever a probe fails — and, since 2026-09-07, whenever the code on disk drifted from
the code running. Both are correct for an idle daemon and destructive for a busy one:
measured 2026-09-27, it killed the daemon mid-turn at 19:45, 19:50, 19:51 and 20:01,
each replacement taking 75 s+ to rebind. The watchdog cannot tell "wedged" from "busy"
unless the daemon says so, so /health now reports ``chat.inflight``.

A pure-ASGI middleware, deliberately not ``BaseHTTPMiddleware``: a streamed turn
(``/stream``, ``/chat/stream``, a streamed ``/v1/chat/completions``) returns its
headers long before its last byte, and ``call_next`` returns at the headers. The count
here is held until the final ``http.response.body`` message (``more_body`` false) or
the app raising, so a 3-minute stream counts for all 3 minutes.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Iterable

Scope = dict
Message = dict
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: POST routes that run an agent turn. Exact paths — a prefix would also count
#: ``/chat/steer`` (a 1 ms enqueue) and the static ``GET /chat`` page.
CHAT_PATHS: frozenset[str] = frozenset({
    "/chat",
    "/stream",
    "/chat/stream",
    "/v1/chat/completions",
})


class InflightTracker:
    """Thread-unsafe by design: every mutation happens on the one event loop."""

    def __init__(self) -> None:
        self._active: dict[int, float] = {}
        self._next_id = 0
        self.total_started = 0

    def begin(self) -> int:
        """Record a turn starting. Returns the token to pass to :meth:`end`."""
        self._next_id += 1
        self._active[self._next_id] = time.time()
        self.total_started += 1
        return self._next_id

    def end(self, token: int) -> None:
        """Record a turn finishing. Idempotent: a second call for a token is a no-op."""
        self._active.pop(token, None)

    @property
    def count(self) -> int:
        return len(self._active)

    def snapshot(self) -> dict[str, Any]:
        """The ``chat`` block /health reports."""
        now = time.time()
        oldest = min(self._active.values()) if self._active else None
        return {
            "inflight": self.count,
            "oldest_age_s": round(now - oldest, 1) if oldest is not None else 0.0,
            "total_started": self.total_started,
        }


class InflightChatMiddleware:
    """Hold an :class:`InflightTracker` slot for the full life of every chat request."""

    def __init__(
        self,
        app: ASGIApp,
        tracker: InflightTracker,
        paths: Iterable[str] = CHAT_PATHS,
    ) -> None:
        self.app = app
        self.tracker = tracker
        self.paths = frozenset(paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope.get("type") != "http" or scope.get("method") != "POST"
                or scope.get("path") not in self.paths):
            await self.app(scope, receive, send)
            return
        token = self.tracker.begin()

        async def _send(message: Message) -> None:
            try:
                await send(message)
            finally:
                if (message.get("type") == "http.response.body"
                        and not message.get("more_body", False)):
                    self.tracker.end(token)

        try:
            await self.app(scope, receive, _send)
        finally:
            # Covers the app raising, the client disconnecting mid-stream, and a
            # handler that never sent a final body — a leaked slot would pin
            # `inflight` above zero and disarm the watchdog forever.
            self.tracker.end(token)


__all__ = ["CHAT_PATHS", "InflightChatMiddleware", "InflightTracker"]
