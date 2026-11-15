"""Surprise-bus interrupts as attention-beam items.

The fleet's five surprise producers publish one schema to one ledger
(the platform's ``surprise_bus.py``). This is the reader that makes the
number matter: an event with ``interrupt=true`` becomes a card-shaped item that
:func:`adk.decisions.beam.build` ranks with every other ask. There is no second
ranking here -- the item carries ``kind``/``urgency``/``created_at`` and the beam's
own ``ask_rank`` decides where it lands (urgency ``normal``: never red by itself).

Rules, all in :func:`fold` (pure, no I/O):

* **dedupe** -- one item per ``(source, subject)``; subject is the event's entity
  key, else the object id its detail names, else its domain.
* **cooldown** -- a further interrupt inside :data:`COOLDOWN_SECONDS` of the item
  being raised joins it (count goes up, position does not); after the cooldown it
  re-raises the item (new id, ``created_at`` = that interrupt), so a subject that
  keeps firing resurfaces at most once per cooldown.
* **expiry** -- with no new interrupt for :data:`EXPIRE_SECONDS` the item is gone.

It reads the JSONL ledger, not the Flux topic: this daemon runs on the host with
no bus connection, and the ledger exists exactly so a reader works without one.
Field names and the file name mirror ``surprise_bus`` and are pinned by
the platform's ``check_surprise_bus.py``. The ledger is a file any local
process can append to, so everything read from it is treated as data: numbers are
re-parsed, strings are reduced to a safe alphabet and capped.

``AITHER_SURPRISE_BUS=0`` turns the source off (same switch as the producers).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

LEDGER_DIRNAME = "surprise_bus"
LEDGER_NAME = "surprise.jsonl"
SOURCES = ("surprise_detector", "lewm", "system_world", "prospector", "awm")

#: A repeat interrupt inside this window joins the raised item instead of re-raising.
COOLDOWN_SECONDS = 15 * 60
#: With no new interrupt for this long the item leaves the beam.
EXPIRE_SECONDS = 60 * 60
#: Bytes read from the end of each ledger generation per refresh.
TAIL_BYTES = 512 * 1024
KEY_PREFIX = "surprise:"

_SAFE = re.compile(r"[^A-Za-z0-9:._@/+-]+")
_TEST_DIR: Optional[Path] = None


@dataclass(frozen=True)
class SurpriseOption:
    key: str
    label: str


@dataclass(frozen=True)
class SurpriseCard:
    """The fields the beam and ``GET /attention`` read off a DecisionCard."""

    id: str
    title: str
    created_at: float
    dedupe_key: str
    last_at: float
    count: int
    summary: str = ""
    kind: str = "surprise"
    urgency: str = "normal"
    options: tuple = (SurpriseOption("look", "Look"),)
    source: dict = field(default_factory=dict)
    answered_at: Optional[float] = None


def enabled() -> bool:
    return os.environ.get("AITHER_SURPRISE_BUS", "1").strip() != "0"


def ledger_path() -> Path:
    """``AITHER_SURPRISE_BUS_DIR`` > ``$AITHER_DATA_DIR/surprise_bus`` >
    ``~/.aither/surprise_bus``. Under pytest with no explicit dir: a temp dir."""
    global _TEST_DIR
    explicit = os.environ.get("AITHER_SURPRISE_BUS_DIR", "").strip()
    if explicit:
        return Path(explicit) / LEDGER_NAME
    if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        if _TEST_DIR is None:
            _TEST_DIR = Path(tempfile.mkdtemp(prefix="aither-surprise-source-test-"))
        return _TEST_DIR / LEDGER_NAME
    root = os.environ.get("AITHER_DATA_DIR", "").strip() or os.path.expanduser("~/.aither")
    return Path(root) / LEDGER_DIRNAME / LEDGER_NAME


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _safe(value: Any, limit: int = 80) -> str:
    return _SAFE.sub("-", str(value or ""))[:limit].strip("-")


def _subject(ev: dict) -> str:
    detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
    return (_safe(ev.get("entity")) or _safe(detail.get("object_id"))
            or _safe(ev.get("domain")) or "unknown")


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def fold(events: Iterable[dict], now: float, cooldown: float = COOLDOWN_SECONDS,
         expire: float = EXPIRE_SECONDS) -> list[SurpriseCard]:
    """Interrupt events -> at most one live item per (source, subject), newest first."""
    rows = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("interrupt") is not True:
            continue
        ts = _num(ev.get("ts"))
        if ts is None or ts > now or ev.get("source") not in SOURCES:
            continue
        rows.append((ts, ev))
    rows.sort(key=lambda r: r[0])
    live: dict[tuple[str, str], dict] = {}
    for ts, ev in rows:
        key = (ev["source"], _subject(ev))
        cur = live.get(key)
        if cur is None or ts - cur["last"] > expire:
            cur = live[key] = {"raised": ts, "last": ts, "count": 0, "ev": ev}
        elif ts - cur["raised"] > cooldown:
            cur.update(raised=ts)                       # re-raise: back to the top once
        cur["last"] = ts
        cur["count"] += 1
        cur["ev"] = ev
    out: list[SurpriseCard] = []
    for (source, subject), cur in live.items():
        if now - cur["last"] > expire:
            continue
        ev = cur["ev"]
        digest = hashlib.sha1(
            f"{source}|{subject}|{cur['raised']:.3f}".encode()).hexdigest()[:8]
        times = f" x{cur['count']}" if cur["count"] > 1 else ""
        title = (f"Surprise: {subject} [{source}] {_fmt(_num(ev.get('raw')))} "
                 f"{_safe(ev.get('unit'), 10)}, z {_fmt(_num(ev.get('z')))}{times}")
        out.append(SurpriseCard(
            id=f"s-{digest}", title=title, created_at=cur["raised"],
            dedupe_key=f"{KEY_PREFIX}{source}:{subject}", last_at=cur["last"],
            count=cur["count"], source={"producer": source, "subject": subject}))
    out.sort(key=lambda c: (-c.created_at, c.id))
    return out


def _tail(path: Path, limit: int = TAIL_BYTES) -> list[dict]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            chunk = fh.read(limit)
    except OSError:
        return []
    lines = chunk.decode("utf-8", "replace").splitlines()
    if size > limit and lines:
        lines = lines[1:]                               # first line may be cut in half
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


_cache: dict[str, Any] = {"sig": None, "rows": []}


def read_interrupts(path: Optional[Path] = None) -> list[dict]:
    """Interrupt rows from both ledger generations. Re-read only when a file changed
    (``GET /attention`` long-polls this every couple of seconds)."""
    target = Path(path) if path is not None else ledger_path()
    files = (target.with_name(target.name + ".1"), target)
    sig = []
    for p in files:
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), 0, 0))
    if _cache["sig"] != sig:
        rows: list[dict] = []
        for p in files:
            rows.extend(r for r in _tail(p) if r.get("interrupt") is True)
        _cache.update(sig=sig, rows=rows)
    return list(_cache["rows"])


def attention_cards(now: Optional[float] = None,
                    path: Optional[Path] = None) -> list[SurpriseCard]:
    """What the beam should consider right now. Never raises: a broken ledger means
    no surprise items, never a broken ``GET /attention``."""
    if not enabled():
        return []
    try:
        return fold(read_interrupts(path), time.time() if now is None else now)
    except Exception as exc:  # noqa: BLE001 -- the beam outlives this source
        print(f"[attention] surprise source failed: {exc}", file=sys.stderr)
        return []


def item_key(card: Any) -> str:
    """``surprise:<id>`` for our items, the store's ``decision:<id>`` otherwise."""
    cid = str(getattr(card, "id", "") or "")
    return f"{KEY_PREFIX}{cid}" if isinstance(card, SurpriseCard) else f"decision:{cid}"


def rekey(segments: list[dict], cards: Iterable[Any]) -> list[dict]:
    """``beam.compress`` keys every segment ``decision:<id>``; a click on one of ours
    must not try to open a decision card that does not exist."""
    mine = {f"decision:{c.id}" for c in cards if isinstance(c, SurpriseCard)}
    return [dict(seg, key=KEY_PREFIX + seg["key"][len("decision:"):])
            if seg.get("key") in mine else seg for seg in segments]


__all__ = [
    "COOLDOWN_SECONDS", "EXPIRE_SECONDS", "LEDGER_DIRNAME", "LEDGER_NAME", "SOURCES",
    "SurpriseCard", "SurpriseOption", "attention_cards", "enabled", "fold", "item_key",
    "ledger_path", "read_interrupts", "rekey",
]
