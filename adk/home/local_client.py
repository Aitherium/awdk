"""A thin client for the ``local`` Hearth channel (:mod:`adk.home.transports.local`).

Reads the bearer AND the port from ``<home>/local.token`` (written by the running
``adk home serve`` once it is listening) and talks to it on ``127.0.0.1``. It
never starts an agent: every surface that uses it (``adk home say``, ``adk home
events``, awsh ``/hearth``) is a window onto the one serving process.

The bearer is never sent to an unproven listener: every call first asks
``GET /hello`` to answer a fresh nonce with ``HMAC(token, nonce|port)`` for the
port this client dialed, on the same connection pool, and refuses the server
when the proof is wrong (another program squatting the port).
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import httpx

from .config import HomeError
from .transports.local import (
    LOOPBACK,
    MAX_BODY,
    hello_proof,
    local_port,
    read_endpoint,
    read_token,
)

logger = logging.getLogger("adk.home.local_client")

#: One agent turn can take minutes (a slow local model, a web fetch).
TURN_TIMEOUT_S = 600.0


class LocalClientError(HomeError):
    """The local channel could not be reached or refused the request."""


class LocalClient:
    """``say`` / ``events`` / ``receipts`` against the local channel."""

    def __init__(self, url: str = "", token: str = "", root: Optional[Path] = None,
                 port: Optional[int] = None, timeout: float = TURN_TIMEOUT_S):
        recorded = 0
        try:
            if not token:
                if url:
                    token = read_token(root)
                else:
                    token, recorded = read_endpoint(root)
        except HomeError as exc:
            raise LocalClientError(str(exc)) from exc
        # The port the serve holding THIS token recorded wins over the env var.
        dial = port or recorded or local_port()
        self.url = (url or f"http://{LOOPBACK}:{dial}").rstrip("/")
        self._token = token
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return f"LocalClient({self.url})"

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _client(self, timeout: Any = None) -> httpx.Client:
        # trust_env=False: an HTTP(S)_PROXY must never carry the bearer off the box.
        return httpx.Client(timeout=timeout or self.timeout, trust_env=False)

    def _verify(self, http: httpx.Client) -> None:
        """Raise unless the listener proves it holds this token (bearer NOT sent)."""
        dialed = httpx.URL(self.url).port or 80
        nonce = secrets.token_hex(16)
        resp = http.get(f"{self.url}/hello", params={"nonce": nonce})
        proof = ""
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if isinstance(data, dict):
                proof = str(data.get("proof") or "")
        want = hello_proof(self._token, nonce, dialed)
        if not hmac.compare_digest(proof.encode("utf-8"), want.encode("utf-8")):
            logger.error("hearth: the listener on %s failed the local token proof; "
                         "the token was not sent", self.url)
            raise LocalClientError(
                f"the listener on {self.url} is not your `adk home serve` (it could "
                "not prove it holds this home's token); the token was NOT sent. "
                "Check what owns that port, or restart `adk home serve`")

    def _check(self, resp: httpx.Response) -> Dict[str, Any]:
        if resp.status_code == 401:
            raise LocalClientError("the local channel refused the token: the running "
                                   "serve belongs to another agent home (check "
                                   "AITHER_AGENT_HOME) or wrote a new token")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200:
            why = data.get("error") if isinstance(data, dict) else ""
            raise LocalClientError(f"local channel: HTTP {resp.status_code} "
                                   f"{why or resp.reason_phrase}")
        if not isinstance(data, dict):
            raise LocalClientError("local channel: the reply is not a JSON object")
        return data

    def _unreachable(self, exc: Exception) -> LocalClientError:
        return LocalClientError(f"nothing is serving on {self.url} ({type(exc).__name__}); "
                                "start `adk home serve` (the local channel is on unless "
                                "--no-local)")

    def say(self, text: str) -> Dict[str, Any]:
        """Send one message; the reply carries this turn's ``replies``."""
        body = json.dumps({"text": text}).encode("utf-8")
        if len(body) > MAX_BODY:
            raise LocalClientError(f"message too long ({len(body)} bytes; the limit is "
                                   f"{MAX_BODY})")
        try:
            with self._client() as http:
                self._verify(http)
                resp = http.post(f"{self.url}/message", content=body,
                                 headers={**self._headers(),
                                          "Content-Type": "application/json"})
        except httpx.TransportError as exc:
            raise self._unreachable(exc) from exc
        return self._check(resp)

    def receipts(self, n: int = 10) -> Dict[str, Any]:
        """The last ``n`` receipts and the log's verify verdict."""
        try:
            with self._client(30.0) as http:
                self._verify(http)
                resp = http.get(f"{self.url}/receipts", params={"n": max(1, int(n))},
                                headers=self._headers())
        except httpx.TransportError as exc:
            raise self._unreachable(exc) from exc
        return self._check(resp)

    def events(self, limit: int = 0) -> Iterator[Dict[str, Any]]:
        """Yield outbound messages as they arrive (``limit`` > 0 stops after that many)."""
        seen = 0
        timeout = httpx.Timeout(10.0, read=None)
        try:
            with self._client(timeout) as http:
                self._verify(http)
                with http.stream("GET", f"{self.url}/events",
                                 headers=self._headers()) as resp:
                    if resp.status_code != 200:
                        resp.read()
                        self._check(resp)
                    for event in parse_sse(resp.iter_lines()):
                        yield event
                        seen += 1
                        if limit and seen >= limit:
                            return
        except httpx.TransportError as exc:
            raise self._unreachable(exc) from exc


def parse_sse(lines: Iterator[str]) -> Iterator[Dict[str, Any]]:
    """JSON objects from ``data:`` lines of a server-sent-event stream."""
    data: List[str] = []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip():
            if data:
                raw = "\n".join(data)
                data = []
                try:
                    item = json.loads(raw)
                except ValueError:
                    logger.warning("hearth: skipped an unparseable local event")
                    continue
                if isinstance(item, dict):
                    yield item


# ── rendering (shared by the CLI and awsh) ────────────────────────────────────

def render_message(msg: Dict[str, Any]) -> str:
    text = str(msg.get("text") or "")
    if msg.get("missed"):
        text = f"(missed) {text}"
    if msg.get("card"):
        text += f"\n  -> answer with: yes {msg['card']}  |  no {msg['card']}"
    return text


def render_replies(data: Dict[str, Any]) -> str:
    replies = data.get("replies") or []
    if not replies:
        return "(no reply)" if data.get("handled", True) else "(not handled)"
    return "\n\n".join(render_message(r) for r in replies if isinstance(r, dict))


def render_receipts(data: Dict[str, Any]) -> str:
    verify = data.get("verify") or {}
    lines = [f"receipts: {verify.get('verdict', '?')} -- {verify.get('reason', '')}"]
    for r in data.get("rows") or []:
        lines.append(f"#{r.get('seq')} {r.get('ts')} {r.get('kind')} {r.get('name')} "
                     f"[{r.get('approval')}] {r.get('result_preview', '')}".rstrip())
    if len(lines) == 1:
        lines.append("(no receipts yet)")
    return "\n".join(lines)
