"""Shared plumbing for webhook transports: the uvicorn server, ordered dispatch
into :class:`HearthCore`, and replay/duplicate suppression.

A webhook must answer fast (Meta and Twilio retry on a slow 2xx), while one
agent turn can take a minute, so a verified message is queued and the HTTP
handler returns at once. Messages are handed to the core one at a time, in
arrival order, so a "yes" to an approval card is never raced by the next text.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from typing import Any, List, Optional, Set

logger = logging.getLogger("adk.home.transports")

#: Largest webhook body read before the signature check (bytes). Both providers
#: send a few KB; anything larger is refused unread past this cap.
MAX_BODY = 256 * 1024

_DIGITS = re.compile(r"^[0-9]{6,15}$")


def e164_digits(value: Any) -> str:
    """``+1 (555) 010-0200`` / ``whatsapp:+15550100200`` -> ``15550100200``;
    "" when what is left is not 6-15 digits (E.164 without the plus)."""
    raw = str(value or "")
    if raw.lower().startswith("whatsapp:"):
        raw = raw[len("whatsapp:"):]
    digits = re.sub(r"[\s()+.-]", "", raw)
    return digits if _DIGITS.match(digits) else ""


class InboundDispatcher:
    """Serialises verified inbound messages into ``core.on_message``."""

    def __init__(self, channel: str, seen_cap: int = 1024):
        self.channel = channel
        self.core: Any = None
        self._lock = asyncio.Lock()
        self._tasks: Set["asyncio.Task[Any]"] = set()
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._seen_cap = seen_cap

    def seen(self, message_id: str) -> bool:
        """True when ``message_id`` was already accepted (a provider retry)."""
        if not message_id:
            return False
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > self._seen_cap:
            self._seen.popitem(last=False)
        return False

    def submit(self, user_id: str, text: str) -> None:
        task = asyncio.ensure_future(self._deliver(user_id, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, user_id: str, text: str) -> None:
        async with self._lock:
            if self.core is None:
                logger.warning("hearth: %s message dropped (transport not started)",
                               self.channel)
                return
            try:
                await self.core.on_message(self.channel, user_id, text)
            except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the server
                logger.error("hearth: %s turn failed: %s", self.channel, type(exc).__name__)

    async def drain(self) -> None:
        """Wait until every queued message has been handled."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


class UvicornRunner:
    """Run an ASGI app on uvicorn inside the current loop (as WebhookAdapter does)."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._server: Any = None
        self._task: Optional["asyncio.Task[Any]"] = None

    async def start(self, app: Any, label: str, timeout: float = 15.0,
                    public: bool = True) -> None:
        """Return only once the port is bound; raise :class:`OSError` otherwise.

        uvicorn reports a bind failure with ``sys.exit(1)`` from inside ``serve()``;
        left alone that ``SystemExit`` escapes ``asyncio.run`` and takes every
        other channel down with it. It is turned into an ``OSError`` here, so the
        caller drops only this channel.
        """
        import uvicorn

        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning",
                                access_log=False)
        server = uvicorn.Server(config)
        where = f"{self.host}:{self.port}"

        async def _serve() -> None:
            try:
                await server.serve()
            except SystemExit as exc:
                raise OSError(f"{label}: cannot listen on {where} (is the port in use?; "
                              f"uvicorn exited {exc.code})") from None

        task = asyncio.ensure_future(_serve())
        self._server, self._task = server, task
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not server.started:
            if task.done():
                self._server = self._task = None
                exc = None if task.cancelled() else task.exception()
                raise exc if isinstance(exc, OSError) else OSError(
                    f"{label}: the webhook server on {where} stopped before listening"
                    + (f" ({type(exc).__name__})" if exc else ""))
            if loop.time() > deadline:
                await self.stop()
                raise OSError(f"{label}: the webhook server on {where} did not start "
                              f"within {timeout:.0f}s")
            await asyncio.sleep(0.02)
        if public:
            logger.info("hearth: %s webhook listening on %s (it needs a public https URL: "
                        "`awtunnel up --port %d`)", label, where, self.port)
        else:
            logger.info("hearth: %s listening on %s (this machine only)", label, where)

    @property
    def bound_port(self) -> int:
        """The port actually bound (differs from ``port`` when that was 0); 0 = none."""
        for srv in getattr(self._server, "servers", None) or []:
            for sock in getattr(srv, "sockets", None) or []:
                try:
                    return int(sock.getsockname()[1])
                except (OSError, IndexError, TypeError) as exc:
                    logger.debug("hearth: cannot read the bound port: %s", exc)
        return 0

    @property
    def bound_hosts(self) -> List[str]:
        """The address of every listening socket ([] when nothing is bound)."""
        hosts: List[str] = []
        for srv in getattr(self._server, "servers", None) or []:
            for sock in getattr(srv, "sockets", None) or []:
                try:
                    hosts.append(str(sock.getsockname()[0]))
                except (OSError, IndexError, TypeError) as exc:
                    logger.debug("hearth: cannot read a bound address: %s", exc)
        return hosts

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                logger.debug("hearth: webhook server did not stop cleanly: %s", exc)
        self._server = self._task = None


async def read_capped(request: Any, limit: int = MAX_BODY) -> Optional[bytes]:
    """The request body, or None when it exceeds ``limit`` (default :data:`MAX_BODY`)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
