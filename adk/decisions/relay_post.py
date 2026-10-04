"""Post decision cards to the relay as ONE structured message, and keep it current.

Every surface used to render its own copy of a card -- the card window, a DM, the
desk deck, the terminal -- and answering on one left the others asking a question
that was already settled. The relay is the one place the desk, the browser, a
phone and a terminal all read, so a card becomes a single ``decision_card``
message in the owner-only ``#decisions`` channel:

* **raise**  -- :func:`post_card` (called by ``notify``) sends the card. The relay
  stores it as one message whose options render as buttons; a reply in its
  thread or a button press is forwarded to the relay's daemon answer path --
  only when that daemon holds this very card (the relay checks; a card raised
  on another machine is shown there but answered on the machine that raised it).
* **close**  -- :func:`sync_card` (called by every store transition: answer,
  cancel, resolve, deadline) sends the new state, so the relay message flips to
  "Answered: ..." wherever it is open. The relay also polls for this itself, so a
  push lost to a closed laptop lid is repaired rather than permanent.

Constraints, same as the rest of this package:

* **stdlib only**, and it NEVER raises -- the card is durable before any of this
  runs, so a relay that is down costs a message, never a raise or an answer.
* **no secret in a card on the wire**: the payload is ``to_dict_safe()``, so a
  credential card's answer is the marker string and never a value; the relay
  additionally refuses to make a credential card answerable at all.
* **the bearer is the identity**: the same session bearer the ``awrelay`` CLI
  reads. A relay URL with no bearer is reported as skipped, never posted
  anonymously.
* **off under pytest** unless a test names the relay explicitly, because a
  developer's shell usually has ``AWRELAY_URL`` set and a test suite must never
  post into a real owner channel.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from adk.decisions.store import DecisionCard

#: What the relay calls this message.
MESSAGE_TYPE = "decision_card"

#: The relay route that creates or updates a card message (idempotent by id).
ROUTE = "/v1/decision-cards"

#: Seconds a post may take. Short on purpose: it runs on the raise path.
TIMEOUT_SECONDS = float(os.getenv("AITHER_DECISIONS_ROOM_TIMEOUT", "4"))

_OFF = ("0", "false", "no", "off")

#: An opener: (urllib Request, timeout) -> (status, body text). Injectable for tests.
Opener = Callable[[urllib.request.Request, float], "tuple[int, str]"]


def _under_pytest() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


def relay_url() -> str:
    """The relay origin, or "" when posting is off.

    ``AITHER_DECISIONS_ROOM_URL`` wins; otherwise the ``awrelay`` CLI's own
    ``AWRELAY_URL``, so a machine that can already post to the relay needs no new
    setting. ``AITHER_DECISIONS_ROOM=0`` turns the channel off outright.
    """
    if os.getenv("AITHER_DECISIONS_ROOM", "").strip().lower() in _OFF:
        return ""
    explicit = os.getenv("AITHER_DECISIONS_ROOM_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    if _under_pytest():
        return ""
    return os.getenv("AWRELAY_URL", "").strip().rstrip("/")


def _bearer_file() -> Path:
    return Path.home() / ".aither" / "session-bearer"


def relay_token() -> str:
    """The bearer to post with: explicit env first, then the session bearer file."""
    for name in ("AITHER_DECISIONS_ROOM_TOKEN", "AWRELAY_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    try:
        return _bearer_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def card_origin() -> str:
    """Which machine raised the card: ``AITHER_DECISIONS_ORIGIN`` or the hostname.

    The relay renders a card from ITS daemon's copy and answers it there; a card
    its daemon does not hold is shown as raised elsewhere and never withdrawn on
    a 404 -- the origin is how the owner can tell which machine is asking.
    """
    raw = os.getenv("AITHER_DECISIONS_ORIGIN", "").strip() or socket.gethostname()
    return "".join(ch for ch in raw if ch.isalnum() or ch in "_.:-")[:96]


def card_wire(card: DecisionCard) -> dict[str, Any]:
    """The card in the relay's wire shape, built from the SAFE serialisation."""
    data = card.to_dict_safe()
    source = data.get("source") or {}
    options = [
        {
            "key": o.get("key", ""),
            "label": o.get("label", ""),
            "consequence": o.get("consequence", ""),
            "recommended": bool(o.get("recommended")),
        }
        for o in data.get("options") or []
    ]
    return {
        "type": MESSAGE_TYPE,
        "id": data.get("id", ""),
        "title": data.get("title", ""),
        "summary": data.get("summary", ""),
        "facts": list(data.get("facts") or [])[:8],
        "kind": data.get("kind", "decision"),
        "urgency": data.get("urgency", "normal"),
        "options": options,
        "recommended": card.recommended_key(),
        "default": data.get("default_key", ""),
        "session": source.get("session_id", "") if isinstance(source, dict) else "",
        "origin": card_origin(),
        "agent": source.get("agent", "") if isinstance(source, dict) else "",
        "deadline": data.get("deadline"),
        "created_at": data.get("created_at"),
        "status": data.get("status", "open"),
        "answer": data.get("answer"),
        "answered_by": data.get("answered_by"),
        "answered_via": data.get("answered_surface") or data.get("answered_via"),
    }


def _default_opener(req: urllib.request.Request, timeout: float) -> "tuple[int, str]":
    # Default trust store (plus SSL_CERT_FILE when the host sets one) -- never an
    # unverified context: a card names what an agent is about to do.
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        return exc.code, body


def post_card(card: DecisionCard, *, opener: Optional[Opener] = None,
              url: Optional[str] = None, token: Optional[str] = None) -> Optional[str]:
    """Create or update the card's relay message. Returns an error string, or None.

    "skipped" reasons start with ``off:`` so a caller can tell "not configured"
    from "tried and failed" -- the second one is worth surfacing, the first is not.
    """
    base = relay_url() if url is None else url.rstrip("/")
    if not base:
        return "off: no relay configured"
    bearer = relay_token() if token is None else token
    if not bearer:
        return "off: no relay identity (session bearer missing)"
    try:
        payload = json.dumps({"card": card_wire(card)}).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return f"card not serialisable: {exc}"
    req = urllib.request.Request(base + ROUTE, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {bearer}")
    try:
        status, body = (opener or _default_opener)(req, TIMEOUT_SECONDS)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"relay unreachable: {exc}"
    if status >= 400:
        return f"relay HTTP {status}: {body[:160]}"
    return None


def sync_card(card: DecisionCard, *, wait: bool = False,
              opener: Optional[Opener] = None) -> Optional[threading.Thread]:
    """Push a state transition to the relay WITHOUT holding up the transition.

    Runs in a daemon thread: an answer from the card window must not wait on a
    network round trip. Losing the push is safe -- the relay reconciles open
    cards against the daemon on its own clock. ``wait=True`` is for tests.
    Returns the thread, or None when the channel is off.
    """
    if not relay_url():
        return None

    def _run() -> None:
        error = post_card(card, opener=opener)
        if error and not error.startswith("off:"):
            print(f"[decisions] relay sync for {card.id} failed: {error}", file=sys.stderr)

    thread = threading.Thread(target=_run, name=f"relay-sync-{card.id}", daemon=True)
    thread.start()
    if wait:
        thread.join(TIMEOUT_SECONDS + 1)
    return thread
