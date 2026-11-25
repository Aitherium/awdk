"""Guaranteed, recorded delivery for HIGH-URGENCY decision cards, from ANY source.

WHY THIS EXISTS
---------------
Measured 2026-11-18: card ``d-43nk``, urgency ``high``, was raised by the awdk
daemon agent (no session id, no steer channel) and never reached the owner.
``d-v9hh`` on the same day had the same fate. The card was durable in the ONE
store, and every delivery surface existed -- desk inbox, card window, relay,
Genesis -- but a raise only ever *attempted* ``notify()`` and nobody read the
verdict. When every channel was skipped (popup disabled, desk filing a card
into an inbox the owner was not looking at, relay env absent on the raising
process) the card simply sat there, and nothing said so.

This module is the missing half of that contract:

* **deliver on raise.** :func:`deliver_on_raise` is what every raise door calls
  instead of bare ``notify()``. For a high/critical card, "the desk filed it"
  is NOT delivery -- it must reach a real push surface (relay message, card
  window, toast, webhook, or the desk fairy carrying it). If ``notify()``
  reported none of those, the relay is re-posted explicitly, which is the same
  push path session-raised cards use.
* **record it.** Every attempt appends one line to
  ``<decisions dir>/delivery-receipts.jsonl`` -- the ledger the owner's plane
  already uses (surface, ok, card ids, note). A receipt with ``ok`` is the
  delivery truth; ``delivered()`` reads it back.
* **escalate undelivered cards.** :func:`start_delivery_sweep` runs in the
  harness daemon, which serves the ONE store, so cards raised by a local
  ``awask``, a hook, an MCP caller or the daemon itself are all covered. An
  open high/critical card with no successful delivery is re-pushed after a
  short window, then at a slower retry cadence, and the re-push is recorded.
* **retire cards nobody can answer.** Before each escalation pass the sweep
  calls :meth:`adk.decisions.store.DecisionStore.retire_gone`: a permission
  grant whose session has died closes at once (reason "requester gone") and any
  other non-credential card past its kind's TTL with a dead requester closes
  too. Measured 2026-11-20: an inbox of 58 cards, several of them permission
  grants for sessions that had ended, and a live ask could never be the one
  surfaced. A retired card is closed in the store, so no surface counts it and
  no later pass can push it again.

Constraints, same as ``notify``: stdlib only, never raises, never blocks a
raise on the network, and no secret ever reaches the ledger (card ids, surface
names and short reasons only). Who may ANSWER a card is untouched -- this
module only decides how a card reaches him.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from adk.decisions.store import decisions_dir

#: Urgencies that must actually reach the owner, not merely be filed.
HIGH_URGENCY = ("high", "critical")

#: A channel that puts the card in front of him. ``awdesk`` alone is the desk
#: FILING the card (its Inbox and badge), which is not delivery for an urgent ask
#: -- the owner asked for pushes to the phone and a Discord DM, and the relay
#: message is the push every other surface (desk, browser, phone, terminal) reads.
STRONG_SURFACES = ("relay", "card window", "toast", "webhook", "awdesk fairy")

#: The receipts ledger already used by the owner's plane; one JSON object per line.
RECEIPT_NAME = "delivery-receipts.jsonl"

SURFACE_RAISE = "raise-push"
SURFACE_ESCALATE = "raise-escalation"

#: An undelivered high-urgency card is re-pushed after this many seconds.
ESCALATE_AFTER_SECONDS = float(os.getenv("AITHER_CARD_ESCALATE_AFTER", "180"))
#: ... and then no more often than this, so a dead relay is not hammered.
ESCALATE_RETRY_SECONDS = float(os.getenv("AITHER_CARD_ESCALATE_RETRY", "900"))
#: The daemon sweep's period.
SWEEP_INTERVAL_SECONDS = float(os.getenv("AITHER_CARD_SWEEP_INTERVAL", "60"))

_OFF = ("0", "false", "no", "off")
_LEDGER_LOCK = threading.Lock()


class DeliveryResult:
    """What a delivery attempt actually achieved. Reported, never raised."""

    def __init__(self, delivered: list[str], skipped: list[str],
                 errors: list[str], ok: bool) -> None:
        self.delivered = delivered
        self.skipped = skipped
        self.errors = errors
        self._ok = bool(ok)

    @property
    def ok(self) -> bool:
        return self._ok

    def describe(self) -> str:
        parts = []
        if self.delivered:
            parts.append("notified via " + ", ".join(self.delivered))
        if self.skipped:
            parts.append("skipped " + ", ".join(self.skipped))
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        return " · ".join(parts) or "no notification channels available"


def receipts_path() -> Path:
    """The delivery ledger, inside whatever decisions dir this process uses."""
    return decisions_dir() / RECEIPT_NAME


def _append_receipt(record: dict[str, Any]) -> None:
    """One JSON line, best-effort. A ledger write must never break a raise."""
    try:
        line = json.dumps(record, sort_keys=True)
    except (TypeError, ValueError):
        return
    try:
        path = receipts_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER_LOCK, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return


def record_attempt(card_id: str, *, ok: bool, channels: list[str], surface: str,
                   urgency: str = "", note: str = "") -> None:
    """Record ONE delivery attempt for ONE card (idempotent read-back, not write)."""
    if not card_id:
        return
    _append_receipt({
        "at": time.time(),
        "surface": surface,
        "ok": bool(ok),
        "card_ids": [card_id],
        "channels": list(channels or []),
        "urgency": urgency,
        "note": (note or "")[:800],
    })


def receipts(limit: int = 5000) -> list[dict[str, Any]]:
    """Tail of the ledger, oldest first. Bad lines are skipped, never fatal."""
    try:
        lines = receipts_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _rows_for(card_id: str) -> list[dict[str, Any]]:
    return [r for r in receipts()
            if card_id in (r.get("card_ids") or [])]


def delivered(card_id: str) -> bool:
    """True when a SUCCESSFUL delivery receipt names this card.

    Only an ``ok`` receipt counts. A receipt saying ``ok: false`` is the honest
    record of a miss and must keep the escalation ladder armed.
    """
    return any(bool(r.get("ok")) for r in _rows_for(card_id))


def last_escalation_at(card_id: str) -> float:
    """When this card was last re-pushed by the sweep (0.0 = never)."""
    stamps = [float(r.get("at") or 0) for r in _rows_for(card_id)
              if r.get("surface") == SURFACE_ESCALATE]
    return max(stamps) if stamps else 0.0


def _post_relay(card: Any) -> Optional[str]:
    """Re-post the card to the relay. Error string, or None. Never raises.

    Thin wrapper on purpose: it is the one seam a test (or an older build with
    no relay module) can replace without touching the raise path.
    """
    try:
        from adk.decisions.relay_post import post_card
    except ImportError as exc:  # pragma: no cover - relay module ships with awask
        return f"relay poster unavailable: {exc}"
    try:
        return post_card(card)
    except Exception as exc:  # noqa: BLE001 - a notification must never fail a raise
        return f"{type(exc).__name__}: {exc}"


def _urgency(card: Any) -> str:
    return str(getattr(card, "urgency", "normal") or "normal").strip().lower()


def push(card: Any, store: Any = None) -> DeliveryResult:
    """Tell the owner about ``card``, and say whether a real push landed.

    The same path a session-raised card uses -- ``notify()`` (desk, card window,
    toast, webhook, relay) -- plus one guarantee: for a high/critical card, a
    delivery in which only the desk INBOX was filed does not count, and the
    relay is re-posted explicitly. Never raises.
    """
    # Module attribute lookup, not a bound import: tests and surfaces replace
    # ``adk.decisions.notify.notify`` and the replacement must be what runs.
    from adk.decisions import notify as notify_mod

    try:
        result = notify_mod.notify(card, store)
    except Exception as exc:  # noqa: BLE001 - a notifier must never fail a raise
        return DeliveryResult([], [], [f"{type(exc).__name__}: {exc}"], False)

    delivered = list(getattr(result, "delivered", None) or [])
    skipped = list(getattr(result, "skipped", None) or [])
    errors = list(getattr(result, "errors", None) or [])

    if _urgency(card) not in HIGH_URGENCY:
        return DeliveryResult(delivered, skipped, errors, bool(delivered))

    strong = [d for d in delivered if d in STRONG_SURFACES]
    if not strong:
        error = _post_relay(card)
        if error is None:
            delivered.append("relay (forced)")
            strong.append("relay (forced)")
        else:
            skipped.append(f"forced relay ({error})")
    return DeliveryResult(delivered, skipped, errors, bool(strong))


def deliver_on_raise(card: Any, store: Any = None) -> DeliveryResult:
    """Deliver a newly-raised card and RECORD the outcome.

    This is the call every raise door should use in place of bare ``notify()``:
    the daemon's ``POST /decisions``, ``agent_tools.raise_card`` and the ``awask
    ask`` CLI. High/critical cards always leave a receipt (delivered or not), so
    the sweep can see an undelivered one; other cards record only a success.
    """
    result = push(card, store)
    if _urgency(card) in HIGH_URGENCY or result.ok:
        record_attempt(getattr(card, "id", "") or "", ok=result.ok,
                       channels=result.delivered, surface=SURFACE_RAISE,
                       urgency=_urgency(card),
                       note="; ".join(result.skipped + result.errors))
    return result


def age_seconds(card: Any, now: Optional[float] = None) -> float:
    created = float(getattr(card, "created_at", 0) or 0)
    if not created:
        return 0.0
    return max(0.0, (now if now is not None else time.time()) - created)


def requester_gone(card: Any) -> bool:
    """True only when the RAISER is provably gone; an unmeasurable one is not gone.

    A named seam rather than a bare import in a handler: the liveness module is
    the only way to answer, and a build that cannot import it must return "not
    gone" (keep the card pushing) rather than silently swallow an error. Never
    raises.
    """
    try:
        from adk.decisions import liveness
    except ImportError as exc:  # pragma: no cover - packaging accident
        print(f"[decisions] liveness unavailable ({exc}); requester liveness "
              "cannot be judged, so the card keeps its place", file=sys.stderr)
        return False
    try:
        return bool(liveness.raiser_gone(card))
    except Exception as exc:  # noqa: BLE001 - a liveness read must never break a sweep
        print(f"[decisions] requester liveness unreadable "
              f"({type(exc).__name__}: {exc}); keeping the card", file=sys.stderr)
        return False


def due_for_escalation(card: Any, now: Optional[float] = None) -> bool:
    """Should the sweep re-push this card right now?

    Open, high/critical, past the short window, no successful delivery receipt,
    and not re-pushed within the retry cadence. A card whose REQUESTER is
    provably gone is never re-pushed: nobody is left to read the answer, and
    :func:`retire_gone` is about to close it. An unmeasurable requester is not
    "gone" and keeps its place.
    """
    if not getattr(card, "is_open", False):
        return False
    if _urgency(card) not in HIGH_URGENCY:
        return False
    if requester_gone(card):
        return False
    now = now if now is not None else time.time()
    if age_seconds(card, now) < ESCALATE_AFTER_SECONDS:
        return False
    if delivered(getattr(card, "id", "") or ""):
        return False
    last = last_escalation_at(getattr(card, "id", "") or "")
    return not last or (now - last) >= ESCALATE_RETRY_SECONDS


def retire_gone(store: Any = None, now: Optional[float] = None) -> list[str]:
    """Close every open card whose requester is provably gone. Returns their ids.

    The store owns the rule (:meth:`DecisionStore.retire_gone`); this wrapper is
    what keeps the sweep from having to know it, and it is deliberately total:
    a store that cannot answer must not end the pass, so a failure is reported
    as one empty result rather than an exception.
    """
    if store is None:
        from adk.decisions.store import get_store

        store = get_store()
    try:
        retired = store.retire_gone(now)
    except Exception:  # noqa: BLE001 - a bad pass must not end the sweep
        return []
    return [str(getattr(c, "id", "") or "") for c in retired if getattr(c, "id", "")]


def sweep(store: Any = None, now: Optional[float] = None) -> list[dict[str, Any]]:
    """One pass: retire dead-requester cards, then re-push open undelivered ones.

    Reads the ONE store, so it covers cards from ANY source -- including a local
    ``awask ask`` that ran while the daemon (and its raise-time delivery) was
    down. The retirement pass runs FIRST so a dead card can never be re-pushed
    on its way out, and so the list it walks already holds only cards that still
    have someone to answer them. Returns one summary per re-push. Never raises.
    """
    if store is None:
        from adk.decisions.store import STATUS_OPEN, get_store

        store = get_store()
    else:
        from adk.decisions.store import STATUS_OPEN

    retire_gone(store, now)
    try:
        cards = store.list(status=STATUS_OPEN)
    except Exception as exc:  # noqa: BLE001 - one bad pass must not end the sweep
        return [{"error": f"{type(exc).__name__}: {exc}"}]

    out: list[dict[str, Any]] = []
    for card in cards:
        if not due_for_escalation(card, now):
            continue
        result = push(card, store)
        record_attempt(getattr(card, "id", "") or "", ok=result.ok,
                       channels=result.delivered, surface=SURFACE_ESCALATE,
                       urgency=_urgency(card),
                       note="re-push of an undelivered card; " +
                            "; ".join(result.skipped + result.errors))
        out.append({"card": getattr(card, "id", ""), "delivered": result.ok,
                    "channels": result.delivered})
    return out


def start_delivery_sweep(interval_s: Optional[float] = None) -> Any:
    """Run :func:`sweep` every ``interval_s`` on a daemon thread.

    Returns the thread, or None when it is off: ``AITHER_CARD_DELIVERY_SWEEP=0``
    or under pytest (a test stores cards in temp dirs and must not push them).
    The first pass runs at once, so a daemon restart re-delivers cards raised
    while it was down.
    """
    if os.environ.get("AITHER_CARD_DELIVERY_SWEEP", "1").strip() in _OFF:
        return None
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    every = float(interval_s or SWEEP_INTERVAL_SECONDS)
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                sweep()
            except Exception as exc:  # noqa: BLE001 - one bad pass must not end it
                print(f"[decisions] delivery sweep pass failed: {type(exc).__name__}",
                      file=__import__("sys").stderr)
            stop.wait(every)

    thread = threading.Thread(target=_loop, name="card-delivery-sweep", daemon=True)
    thread.stop = stop  # type: ignore[attr-defined]
    thread.start()
    return thread
