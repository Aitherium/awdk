"""In-process stand-ins for the two fleet planes the cognition slice touches.

* :class:`InProcessFlux` -- the Flux Redis **Stream** with a consumer group
  (at-least-once): ``xadd`` appends, ``read_group`` delivers and keeps the entry
  PENDING until ``ack``; an unacked entry is redelivered by ``claim_pending``. The CNS
  hub deduplicates on ``data.event_id`` (design section 3, risk R4), which is what makes
  at-least-once delivery safe.
* :class:`MemoryStrata` -- a path -> bytes store under ``aither://warm/...`` paths.
  Provenance lives inside the content (design risk R6), so the store needs no metadata.

Both have the method names of the real clients the services will use, so a hop test
written against them runs against the fleet by swapping the object, not the test.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "event_id",
    "make_event",
    "InProcessFlux",
    "StreamEntry",
    "MemoryStrata",
    "canonical_json",
]

CTX_STREAM = "flux:events:ctx"
CTX_TYPES = frozenset(
    {
        "ctx.rescope",
        "ctx.fact",
        "ctx.contradiction",
        "ctx.owner_record",
        "ctx.outcome",
        "ctx.novelty",
        "ctx.updated",
    }
)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def _default(o: Any) -> Any:
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError("not JSON-serialisable: %s" % type(o).__name__)


def event_id(scope: str, kind: str, clock: int, source: str) -> str:
    """``sha256(scope, kind, clock, source)[:16]`` (design section 3, Flux row)."""
    raw = "%s|%s|%d|%s" % (scope, kind, int(clock), source)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_event(
    kind: str, subject: str, scope: str, clock: int, source: str, **data: Any
) -> Dict[str, Any]:
    if kind not in CTX_TYPES:
        raise ValueError("unknown context event type %r" % kind)
    body = dict(data)
    body.update(
        {
            "scope": scope,
            "clock": int(clock),
            "source": source,
            "event_id": event_id(scope, kind, clock, source),
        }
    )
    return {"type": kind, "subject": subject, "data": body}


@dataclass
class StreamEntry:
    entry_id: str
    event: Dict[str, Any]
    deliveries: int = 0
    delivered_at: float = 0.0


@dataclass
class _Group:
    last: int = 0  # index of the next never-delivered entry
    pending: Dict[str, StreamEntry] = field(default_factory=dict)
    acked: List[Tuple[str, float]] = field(default_factory=list)


class InProcessFlux:
    """A Redis Stream + consumer groups, in memory. Entries are JSON round-tripped on
    ``xadd`` so a consumer never shares a mutable object with the producer."""

    def __init__(self) -> None:
        self.streams: Dict[str, List[StreamEntry]] = {}
        self.groups: Dict[Tuple[str, str], _Group] = {}
        self._seq = 0

    def xadd(self, stream: str, event: Dict[str, Any]) -> str:
        data = event.get("data") or {}
        if not data.get("event_id"):
            raise ValueError("a context event must carry data.event_id")
        self._seq += 1
        entry_id = "%d-0" % self._seq
        self.streams.setdefault(stream, []).append(
            StreamEntry(entry_id, json.loads(canonical_json(event)))
        )
        return entry_id

    def publish(self, event: Dict[str, Any]) -> str:
        return self.xadd(CTX_STREAM, event)

    def create_group(self, stream: str, group: str) -> None:
        self.groups.setdefault((stream, group), _Group())

    def read_group(self, stream: str, group: str, count: int = 100) -> List[StreamEntry]:
        g = self.groups[(stream, group)]
        entries = self.streams.get(stream, [])
        out = []
        while g.last < len(entries) and len(out) < count:
            e = entries[g.last]
            g.last += 1
            e.deliveries += 1
            e.delivered_at = time.monotonic()
            g.pending[e.entry_id] = e
            out.append(e)
        return out

    def claim_pending(self, stream: str, group: str) -> List[StreamEntry]:
        """Redeliver every unacked entry (XAUTOCLAIM with min-idle 0)."""
        g = self.groups[(stream, group)]
        out = list(g.pending.values())
        for e in out:
            e.deliveries += 1
            e.delivered_at = time.monotonic()
        return out

    def ack(self, stream: str, group: str, entry_id: str) -> bool:
        g = self.groups[(stream, group)]
        if g.pending.pop(entry_id, None) is None:
            return False
        g.acked.append((entry_id, time.monotonic()))
        return True

    def pending(self, stream: str, group: str) -> List[str]:
        return sorted(self.groups[(stream, group)].pending)


class MemoryStrata:
    """``aither://warm/...`` path -> bytes."""

    PREFIX = "aither://warm/"

    def __init__(self) -> None:
        self.blobs: Dict[str, bytes] = {}

    def _check(self, path: str) -> str:
        if not path.startswith(self.PREFIX):
            raise ValueError("strata path must start with %s: %r" % (self.PREFIX, path))
        return path

    def write(self, path: str, data: Any) -> str:
        raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
        self.blobs[self._check(path)] = raw
        return hashlib.sha256(raw).hexdigest()

    def write_json(self, path: str, obj: Any) -> str:
        return self.write(path, canonical_json(obj))

    def read(self, path: str) -> Optional[bytes]:
        return self.blobs.get(self._check(path))

    def read_json(self, path: str) -> Any:
        raw = self.read(path)
        return None if raw is None else json.loads(raw.decode("utf-8"))

    def append_line(self, path: str, obj: Any) -> None:
        old = self.read(path) or b""
        self.write(path, old + canonical_json(obj).encode("utf-8") + b"\n")

    def read_lines(self, path: str) -> List[Dict[str, Any]]:
        raw = self.read(path) or b""
        return [json.loads(x) for x in raw.decode("utf-8").splitlines() if x.strip()]

    def list(self, prefix: str) -> List[str]:
        return sorted(p for p in self.blobs if p.startswith(prefix))
