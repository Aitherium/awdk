"""Pair the Awconnect browser extension with this harness daemon.

The extension cannot read ``~/.aither/harness_token`` (that bearer can spawn a
coding agent with filesystem access) and it must not be handed it. Instead it
asks for a NARROW token, and the owner approves the request somewhere the
extension cannot reach:

1. ``POST /pair/start`` -- only from an exact pinned extension origin
   (``AITHER_TRUSTED_EXTENSION_IDS``) on a loopback Host. Returns a random
   ``pair_id`` and a 6-digit ``code``, valid for :data:`PAIR_TTL_S` seconds. The
   daemon raises a decision card carrying the code.
2. The owner approves: the card, ``adk harness pair approve <code>`` or awdesk.
   Approval needs the OWNER credential; a scoped token cannot approve.
3. ``POST /pair/poll {pair_id}`` returns the token ONCE, then forgets the pair.

The token is a registry principal (``mint_scoped_token``), stored hashed, with
``label: "awconnect"`` so ``adk harness pair revoke`` can remove every one.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from typing import Any, FrozenSet, Optional

#: The Awconnect id every unpacked install has: derived from the manifest
#: "key" in the extension's public/manifest.json. Mirrors
#: adk.extension_id.PINNED_EXTENSION_ID (which also names the store id).
PINNED_EXTENSION_ID = "hlmfknhcfhjjngckfpacgleffckpmphe"
_ID_RE = re.compile(r"^[a-p]{32}$")

PAIR_TTL_S = 120
#: Pending pairs at once. A page cannot start pairs (origin gate), but a
#: misbehaving extension could loop; this caps the cards it can raise.
MAX_PENDING = 3
LABEL = "awconnect"

#: What a paired extension may reach. "METHOD /path" entries match that method
#: only, so the sessions list and streams are readable but
#: /sessions/{id}/input, /submit, /message and DELETE are refused.
#: decisions:read/write, sessions:read, rooms:read.
AWCONNECT_PATHS = ("/decisions", "GET /sessions", "GET /rooms")
AWCONNECT_TTL_DAYS = 90


def trusted_extension_origins() -> FrozenSet[str]:
    """Exact ``chrome-extension://<id>`` origins allowed to start a pairing.

    ``AITHER_TRUSTED_EXTENSION_IDS`` (comma-separated ids) when set, else the
    pinned id. Malformed entries (``*``, patterns, typos) are dropped, so the
    set can never be widened by accident.
    """
    try:  # the shared definition, when this build carries it
        from adk.extension_id import trusted_extension_origins as shared
    except ImportError:
        shared = None  # older build: the same rule, defined below
    if shared is not None:
        return frozenset(shared())
    env = os.environ.get("AITHER_TRUSTED_EXTENSION_IDS")
    raw = env.split(",") if env is not None else [PINNED_EXTENSION_ID]
    return frozenset(
        f"chrome-extension://{i.strip()}" for i in raw if _ID_RE.match(i.strip())
    )


def is_loopback_host(host: str) -> bool:
    """Host header names loopback. Guards DNS rebinding: a page on a hostile
    name that resolves to 127.0.0.1 still sends ITS name here."""
    h = (host or "").strip().lower()
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h in ("127.0.0.1", "localhost", "::1")


class PairingBook:
    """Pending pairings, in memory. A restart forgets them (they live 2 min)."""

    def __init__(self, ttl_s: float = PAIR_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._pairs: dict[str, dict[str, Any]] = {}

    def _sweep(self, now: float) -> None:
        for pid in [p for p, e in self._pairs.items() if e["expires_at"] <= now]:
            self._pairs.pop(pid, None)

    def start(self, origin: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sweep(now)
            if len(self._pairs) >= MAX_PENDING:
                raise OverflowError("too many pending pairings; approve or wait")
            codes = {e["code"] for e in self._pairs.values()}
            code = f"{secrets.randbelow(1_000_000):06d}"
            while code in codes:
                code = f"{secrets.randbelow(1_000_000):06d}"
            pair_id = secrets.token_urlsafe(24)
            self._pairs[pair_id] = {
                "code": code,
                "origin": origin,
                "status": "pending",
                "card_id": "",
                "expires_at": now + self.ttl_s,
            }
            return {"pair_id": pair_id, "code": code, "expires_in": int(self.ttl_s)}

    def attach_card(self, pair_id: str, card_id: str) -> None:
        with self._lock:
            if pair_id in self._pairs:
                self._pairs[pair_id]["card_id"] = card_id

    def approve(self, code: str) -> Optional[str]:
        """Approve the pending pair holding *code*. Returns its origin or None."""
        code = (code or "").strip()
        now = time.time()
        with self._lock:
            self._sweep(now)
            for entry in self._pairs.values():
                if entry["status"] == "pending" and secrets.compare_digest(entry["code"], code):
                    entry["status"] = "approved"
                    return str(entry["origin"])
        return None

    def deny(self, pair_id: str) -> None:
        with self._lock:
            if pair_id in self._pairs:
                self._pairs[pair_id]["status"] = "denied"

    def peek(self, pair_id: str) -> Optional[dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._sweep(now)
            entry = self._pairs.get(pair_id)
            return dict(entry) if entry else None

    def take(self, pair_id: str) -> Optional[dict[str, Any]]:
        """Remove and return an APPROVED pair (the token is issued exactly once)."""
        with self._lock:
            entry = self._pairs.get(pair_id)
            if entry and entry["status"] == "approved":
                return self._pairs.pop(pair_id)
        return None

    def pending(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._sweep(now)
            return [
                {"code": e["code"], "origin": e["origin"], "status": e["status"],
                 "expires_in": max(0, int(e["expires_at"] - now))}
                for e in self._pairs.values()
            ]
