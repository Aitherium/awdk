"""The outbound reverse link a device holds open to AitherTunnel.

WHY THIS EXISTS
`adk enroll` puts a device in the fleet registry and identity hands the owner a
`public_url` of the form `https://gateway.aitherium.com/nodes/{node_id}`. That URL
is served by AitherTunnel's reverse proxy, which forwards to the node over
WireGuard. **A phone has no `wg` module and cannot get one**, so for the device
class the whole feature was built for, the advertised address could never resolve.

The honest transport for a machine that can only make OUTBOUND connections is an
outbound connection it holds open. This module is that connection: a WebSocket to
``wss://tunnel.aitherium.com/tunnel/nodes/{node_id}/attach``, authenticated with
the device's own bearer, over which the tunnel multiplexes HTTP requests that this
process forwards to the node's LOCAL inference server.

WireGuard stays the default wherever `wg` exists -- it is faster and it is a real
network. This is the fallback, not the replacement.

cloudflared was REJECTED for this lane: it needs a per-device Cloudflare tunnel
token, which couples every customer device to our Cloudflare account and makes
"remove this device" an operation in someone else's dashboard.

THE SECURITY PROPERTY THAT MATTERS
This process is one end of a socket whose other end is on the public internet, and
it forwards to the customer's own loopback. So it enforces its OWN allowlist --
:data:`NODE_LINK_ALLOWED_PATHS` -- and never forwards a path the tunnel merely
said was fine. The tunnel enforces the same list. One side trusting the other is
exactly how a reverse link becomes an SSRF primitive aimed at the device's
loopback, where `adk` also runs a harness daemon and (on a dev box) a dozen other
services.

The link carries NO credential outward: the bearer goes in the handshake and
nowhere else, and the request headers the tunnel sends are re-filtered here.
"""

from __future__ import annotations

__all__ = [
    "NodeLink",
    "NODE_LINK_ALLOWED_PATHS",
    "HARNESS_PREFIX",
    "HARNESS_ALLOWED_PREFIXES",
    "path_target",
    "tunnel_ws_url",
    "default_tunnel_url",
]

import asyncio
import base64
import json
import logging
import os
import random
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("adk.node_link")

#: EXACT paths this device will forward to its local inference server. Not
#: prefixes: `v1/` also carries routes awnode serves that have no business on a
#: public socket. The tunnel server enforces the SAME set; if the two ever drift,
#: the narrower one wins, because both sides refuse independently.
NODE_LINK_ALLOWED_PATHS = frozenset({
    "v1/chat/completions",
    "v1/models",
    "health",
    "status",
    "chat",
})

#: Requests under this prefix go to the local harness daemon (`adk rc`), not the
#: inference server. Stripped before forwarding.
HARNESS_PREFIX = "harness/"

#: What a harness-scoped request may reach on the daemon. The daemon ALSO gates
#: this with a scoped token, so a bug here is not a bypass -- it is the first of
#: two independent gates.
HARNESS_ALLOWED_PREFIXES = ("sessions", "decisions", "desk/fleet/status")

#: Reconnect backoff. Capped, and jittered so a fleet that lost the tunnel does
#: not return as a synchronised thundering herd.
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0

#: Hard cap on one frame's body in either direction.
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: Keepalive. Mobile networks drop an idle socket silently, and a half-open link
#: is indistinguishable from a working one until the first request times out.
PING_INTERVAL_S = 30.0


def tunnel_ws_url(base: str, node_id: str) -> str:
    """Build the attach URL, coercing http(s) to ws(s).

    Refuses to silently downgrade: a plain-http base yields `ws://`, which is what
    a local test harness wants and what a production misconfiguration must LOOK
    like rather than quietly becoming `wss://` and appearing to work.
    """
    b = (base or "").strip().rstrip("/")
    if b.startswith("https://"):
        b = "wss://" + b[len("https://"):]
    elif b.startswith("http://"):
        b = "ws://" + b[len("http://"):]
    elif not b.startswith(("ws://", "wss://")):
        b = "wss://" + b
    return f"{b}/tunnel/nodes/{node_id}/attach"


def path_target(path: str) -> Tuple[str, str]:
    """Classify a requested path.

    Returns ``(target, local_path)`` where target is ``inference``, ``harness`` or
    ``""`` (refused). The refusal is the default: an unrecognised path is never
    forwarded, so adding a route to the tunnel does not implicitly open one here.
    """
    clean = (path or "").split("?", 1)[0].strip("/")
    low = clean.lower()
    if low.startswith(HARNESS_PREFIX):
        rest = clean[len(HARNESS_PREFIX):].strip("/")
        rest_low = rest.lower()
        if not rest:
            return "", ""
        if ".." in rest.split("/"):
            return "", ""
        for allowed in HARNESS_ALLOWED_PREFIXES:
            if rest_low == allowed or rest_low.startswith(allowed + "/"):
                return "harness", rest
        return "", ""
    if low in NODE_LINK_ALLOWED_PATHS:
        return "inference", clean
    return "", ""


class NodeLink:
    """Holds the reverse link and forwards allowlisted requests locally.

    Not started automatically by anything: ``adk enroll`` and ``adk rc`` own the
    decision, because a process that silently opens a public-facing socket is not
    something a CLI should do on a user's behalf without being asked.
    """

    def __init__(
        self,
        *,
        tunnel_url: str,
        node_id: str,
        token: str,
        inference_url: str = "",
        harness_url: str = "",
        harness_token: str = "",
    ) -> None:
        self.tunnel_url = tunnel_url
        self.node_id = node_id
        self.token = token
        self.inference_url = (inference_url or "").rstrip("/")
        self.harness_url = (harness_url or "").rstrip("/")
        self.harness_token = harness_token or ""
        self.connected = False
        #: Why the last attempt ended. Surfaced by `adk devices status` and the
        #: heartbeat rather than being swallowed: a link that cannot attach is
        #: the difference between "enrolled" and "usable".
        self.last_error = ""
        self._attempts = 0

    # ── what the heartbeat reports ────────────────────────────────────────
    def reach_kind(self) -> str:
        """``ws`` while the link is up, ``none`` otherwise.

        Deliberately not sticky. A phone that loses its link must stop claiming
        reach within one heartbeat, or the owner's page offers a device that
        cannot answer.
        """
        return "ws" if self.connected else "none"

    # ── the loop ──────────────────────────────────────────────────────────
    async def run(self, *, max_attempts: Optional[int] = None) -> None:
        """Connect, serve, reconnect forever. Never raises.

        ``max_attempts`` bounds the loop for tests; ``None`` runs until cancelled.
        """
        try:
            import websockets  # noqa: F401
        except ImportError:
            # LOUD, once. A silently absent transport is how "the phone is
            # enrolled but unreachable" becomes a mystery.
            self.last_error = "websockets is not installed; the reverse link cannot start"
            log.error("%s", self.last_error)
            return

        backoff = _BACKOFF_START
        while max_attempts is None or self._attempts < max_attempts:
            self._attempts += 1
            try:
                await self._serve_once()
                backoff = _BACKOFF_START          # a clean session resets the ramp
            except asyncio.CancelledError:
                log.info("Node link cancelled")
                raise
            except Exception as e:  # noqa: BLE001 -- a link failure is never fatal
                self.last_error = str(e)[:200]
                log.warning("Node link attempt %d failed: %s", self._attempts, self.last_error)
            finally:
                self.connected = False
            if max_attempts is not None and self._attempts >= max_attempts:
                return
            # Jitter so a fleet that lost the tunnel does not come back in lockstep.
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _serve_once(self) -> None:
        import websockets

        url = tunnel_ws_url(self.tunnel_url, self.node_id)
        # The bearer rides in the handshake and NOWHERE else. websockets renamed
        # this parameter, so try both rather than silently falling back to an
        # unauthenticated connect (which would 4401 and read as "tunnel down").
        try:
            conn = websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {self.token}"},
                max_size=MAX_FRAME_BYTES * 2, open_timeout=20,
            )
        except TypeError:
            conn = websockets.connect(
                url, extra_headers={"Authorization": f"Bearer {self.token}"},
                max_size=MAX_FRAME_BYTES * 2, open_timeout=20,
            )
        async with conn as ws:
            self.connected = True
            self.last_error = ""
            log.info("Node link attached: %s", url)
            pinger = asyncio.ensure_future(self._ping_loop(ws))
            try:
                async for raw in ws:
                    await self._on_frame(ws, raw)
            finally:
                pinger.cancel()

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            await ws.send(json.dumps({"t": "ping"}))

    async def _on_frame(self, ws: Any, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        try:
            frame = json.loads(raw)
        except ValueError:
            log.warning("Tunnel sent an unparseable frame; ignoring")
            return
        if not isinstance(frame, dict):
            return
        if frame.get("t") == "pong":
            return
        if frame.get("t") != "req":
            return
        rid = str(frame.get("id") or "")
        if not rid:
            return
        try:
            await self._handle_request(ws, rid, frame)
        except Exception as e:  # noqa: BLE001 -- one bad request never drops the link
            log.warning("Node link request %s failed: %s", rid, e)
            await self._send(ws, {"t": "err", "id": rid, "status": 502,
                                  "detail": str(e)[:200]})

    async def _handle_request(self, ws: Any, rid: str, frame: Dict[str, Any]) -> None:
        raw_path = str(frame.get("path") or "")
        target, local_path = path_target(raw_path)
        if not target:
            # REFUSED HERE, not just at the tunnel. This is the gate that holds if
            # the tunnel is ever wrong or compromised.
            log.warning("Refusing to forward %r over the node link", raw_path[:120])
            await self._send(ws, {"t": "err", "id": rid, "status": 403,
                                  "detail": "path not permitted on this link"})
            return

        if target == "harness":
            base, bearer = self.harness_url, self.harness_token
            if not base:
                await self._send(ws, {"t": "err", "id": rid, "status": 503,
                                      "detail": "no harness on this device"})
                return
        else:
            base, bearer = self.inference_url, ""
            if not base:
                await self._send(ws, {"t": "err", "id": rid, "status": 503,
                                      "detail": "no local inference server"})
                return

        url = f"{base}/{local_path}"
        query = str(frame.get("query") or "")
        if query:
            url += f"?{query}"
        method = str(frame.get("method") or "GET").upper()
        headers = self._local_headers(frame.get("headers") or {}, bearer)
        body = _decode_body(frame.get("body_b64"))

        import httpx

        # Streaming is the point, not an optimisation: `v1/chat/completions` with
        # `stream: true` is SSE, and buffering it turns a token stream into a
        # blank page that fills in at the end.
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
            async with client.stream(method, url, headers=headers, content=body) as resp:
                await self._send(ws, {
                    "t": "res", "id": rid, "status": resp.status_code,
                    "headers": dict(resp.headers), "stream": True,
                })
                async for chunk in resp.aiter_raw():
                    if not chunk:
                        continue
                    await self._send(ws, {
                        "t": "chunk", "id": rid,
                        "body_b64": base64.b64encode(chunk).decode("ascii"),
                    })
                await self._send(ws, {"t": "end", "id": rid})

    @staticmethod
    def _local_headers(incoming: Dict[str, Any], bearer: str) -> Dict[str, str]:
        """Rebuild the headers for the LOCAL request.

        Allowlisted, not filtered: the tunnel already strips the caller's
        credentials, but this process is the one talking to loopback and must not
        depend on that. Only the few headers a chat/completions call actually
        needs survive, and the harness bearer is added HERE -- it never crosses
        the link.
        """
        keep = ("content-type", "accept", "accept-encoding", "user-agent")
        out = {
            str(k): str(v) for k, v in incoming.items()
            if str(k).lower() in keep
        }
        out.setdefault("content-type", "application/json")
        if bearer:
            out["authorization"] = f"Bearer {bearer}"
        return out

    @staticmethod
    async def _send(ws: Any, frame: Dict[str, Any]) -> None:
        await ws.send(json.dumps(frame))


def _decode_body(raw: Any) -> bytes:
    """Decode a base64 frame body, refusing anything oversized or malformed."""
    if not isinstance(raw, str) or not raw:
        return b""
    if len(raw) > MAX_FRAME_BYTES * 2:
        log.warning("Dropping an oversized request body from the tunnel")
        return b""
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001
        log.warning("Tunnel frame body was not valid base64; dropping it")
        return b""


def default_tunnel_url() -> str:
    """Where the reverse link dials. Env first, then the public tunnel."""
    explicit = (os.environ.get("AITHER_TUNNEL_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    gateway = (os.environ.get("AITHER_GATEWAY_URL") or "").strip()
    if gateway:
        host = urlsplit(gateway if "//" in gateway else f"https://{gateway}").hostname or ""
        if host.startswith("gateway."):
            return "https://tunnel." + host[len("gateway."):]
    return "https://tunnel.aitherium.com"
