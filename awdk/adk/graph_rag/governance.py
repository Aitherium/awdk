"""Portable "memory you can put on trial" governance layer.

A dependency-free port of the AitherOS governed-memory layer
(``AitherOS/lib/memory/governance/``), so the graph-RAG service-pack carries its
own audit + conflict + reversible-forget machinery and stays shippable outside
the monorepo. The AitherOS lib remains the reference design and an optional
heavier backend.

Four primitives:

* :class:`MutationLedger` — append-only JSONL audit trail (the *record*).
* :class:`ConflictDetector` — classifies a changed fact UPDATE vs CONTRADICTION
  (the *trial*).
* :class:`TombstoneStore` — reversible deletes (the *appeal*).
* :class:`StableNodeID` — content-independent ids keyed by a natural key, so a
  node keeps its identity (and its ledger history + edges) across content edits.

:class:`GovernedIngest` glues them onto a corpus (re-)ingestion run.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from adk.graph_memory import cosine_similarity


class MutationType(str, Enum):
    STORE = "store"          # new fact
    SUPERSEDE = "supersede"  # existing fact replaced by a newer/authoritative one
    UPDATE = "update"        # in-place re-tier/re-role (promote) — content unchanged
    FORGET = "forget"        # removed (reversible via tombstone)
    RECOVER = "recover"      # a forgotten fact restored
    ROLLBACK = "rollback"    # a mutation undone


@dataclass
class MemoryMutation:
    """One durable entry in the ledger."""

    mutation_type: MutationType
    node_id: str = ""
    before: Optional[dict] = None
    after: Optional[dict] = None
    reason: str = ""
    source: str = ""
    related_ids: list[str] = field(default_factory=list)
    mutation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mutation_type"] = self.mutation_type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryMutation":
        return cls(
            mutation_type=MutationType(d.get("mutation_type", "store")),
            node_id=d.get("node_id", ""),
            before=d.get("before"),
            after=d.get("after"),
            reason=d.get("reason", ""),
            source=d.get("source", ""),
            related_ids=list(d.get("related_ids", [])),
            mutation_id=d.get("mutation_id", uuid.uuid4().hex),
            timestamp=float(d.get("timestamp", time.time())),
        )


class MutationLedger:
    """Append-only JSONL ledger. Thread-safe; flushes each line to disk."""

    def __init__(self, path: str | Path, *, persist: bool = True) -> None:
        self._persist = persist
        self._path = Path(path)
        self._lock = threading.RLock()
        self._entries: list[MemoryMutation] = []
        if self._persist:
            self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._entries.append(MemoryMutation.from_dict(json.loads(line)))
                except (ValueError, KeyError):
                    # skip a corrupt line rather than lose the whole ledger
                    continue

    def _write_line(self, mutation: MemoryMutation) -> None:
        if not self._persist:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(mutation.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, mutation: MemoryMutation) -> MemoryMutation:
        with self._lock:
            self._entries.append(mutation)
            self._write_line(mutation)
        return mutation

    def record(self, mutation_type: MutationType, **kwargs: Any) -> MemoryMutation:
        return self.append(MemoryMutation(mutation_type=mutation_type, **kwargs))

    def all(self) -> list[MemoryMutation]:
        with self._lock:
            return list(self._entries)

    def recent(self, limit: int = 50) -> list[MemoryMutation]:
        with self._lock:
            return list(reversed(self._entries[-limit:]))

    def by_node(self, node_id: str) -> list[MemoryMutation]:
        """Full history for one node, oldest-first — the node's 'trial record'."""
        with self._lock:
            return [m for m in self._entries if m.node_id == node_id]

    def get(self, mutation_id: str) -> Optional[MemoryMutation]:
        with self._lock:
            for m in self._entries:
                if m.mutation_id == mutation_id:
                    return m
        return None

    def stats(self) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for m in self._entries:
                counts[m.mutation_type.value] = counts.get(m.mutation_type.value, 0) + 1
            return {
                "total": len(self._entries),
                "by_type": counts,
                "path": str(self._path) if self._persist else None,
            }


@dataclass
class ConflictEvent:
    """The verdict when a node's content changes on re-ingestion."""

    node_id: str
    kind: str            # "update" | "contradiction"
    similarity: float
    before: dict
    after: dict
    reason: str = ""


# crude markers that a change flips meaning rather than merely refining it
_NEGATIONS = ("not ", "no longer", "never", "deprecated", "removed", "disabled",
              "must not", "cannot", "forbidden", "banned")
_NUMERIC = tuple("0123456789")


class ConflictDetector:
    """Turn a silent overwrite into a classified, reviewable verdict."""

    def __init__(self, similarity_threshold: float = 0.85) -> None:
        self.threshold = similarity_threshold

    def detect(
        self,
        node_id: str,
        before: dict,
        after: dict,
        *,
        before_embedding: Optional[list[float]] = None,
        after_embedding: Optional[list[float]] = None,
    ) -> Optional[ConflictEvent]:
        """Return a ConflictEvent if before→after is a meaningful change, else None."""
        b_text = str(before.get("content", ""))
        a_text = str(after.get("content", ""))
        if b_text == a_text:
            return None  # nothing changed

        sim = 1.0
        if before_embedding and after_embedding:
            sim = cosine_similarity(before_embedding, after_embedding)

        kind = "update"
        reason = "content refined (high similarity)"
        if sim < self.threshold:
            kind = "contradiction"
            reason = f"content diverged (similarity {sim:.2f} < {self.threshold:.2f})"
        else:
            # even at high similarity, a flipped negation or a changed number is a contradiction
            b_low, a_low = b_text.lower(), a_text.lower()
            if any((n in a_low) != (n in b_low) for n in _NEGATIONS):
                kind = "contradiction"
                reason = "polarity changed (negation added/removed)"
            else:
                b_nums = [c for c in b_text if c in _NUMERIC]
                a_nums = [c for c in a_text if c in _NUMERIC]
                if b_nums and a_nums and b_nums != a_nums:
                    kind = "contradiction"
                    reason = "a numeric value changed"

        return ConflictEvent(
            node_id=node_id, kind=kind, similarity=sim,
            before=before, after=after, reason=reason,
        )


class TombstoneStore:
    """Reversible deletes: snapshot a node before removal; recover within retention."""

    def __init__(self, path: str | Path, *, persist: bool = True) -> None:
        self._persist = persist
        self._path = Path(path)
        self._lock = threading.RLock()
        self._tombs: dict[str, dict] = {}
        if self._persist and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self._tombs[rec["tombstone_id"]] = rec
                except (ValueError, KeyError):
                    continue

    def entomb(self, snapshot: dict, *, reason: str = "") -> str:
        tomb_id = uuid.uuid4().hex
        rec = {
            "tombstone_id": tomb_id,
            "node_id": snapshot.get("id", ""),
            "snapshot": snapshot,
            "reason": reason,
            "timestamp": time.time(),
        }
        with self._lock:
            self._tombs[tomb_id] = rec
            if self._persist:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
        return tomb_id

    def recover(self, tombstone_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._tombs.get(tombstone_id)
        return dict(rec["snapshot"]) if rec else None

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._tombs.values())

    def purge(self, tombstone_id: str) -> bool:
        """HARD-DELETE a tombstone: the snapshot (content, metadata, any edge
        capture) is irrecoverably removed and the persisted file is rewritten
        without it. This is the true-deletion endpoint for knowledge that has
        decayed past its retention window (see ``adk.memory_wiki.prune``) —
        after a purge, ``recover()`` returns None forever. Returns True when a
        tombstone was removed."""
        with self._lock:
            if tombstone_id not in self._tombs:
                return False
            del self._tombs[tombstone_id]
            if self._persist:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    for rec in self._tombs.values():
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                tmp.replace(self._path)
        return True


class StableNodeID:
    """Registry mapping a *natural key* (e.g. ``section:path#heading``) to a
    content-independent id, persisted so re-ingestion reuses the same id and the
    node's ledger history + edges survive content edits."""

    def __init__(self, path: str | Path | None = None, *, persist: bool = True) -> None:
        self._persist = persist and path is not None
        self._path = Path(path) if path is not None else None
        # natural_key -> {"id", "content_hash", "last_seen"}
        self._map: dict[str, dict] = {}
        if self._persist and self._path and self._path.exists():
            try:
                self._map = json.loads(self._path.read_text(encoding="utf-8"))
            except ValueError:
                self._map = {}

    @staticmethod
    def _mint(natural_key: str) -> str:
        return "n_" + hashlib.sha256(natural_key.encode("utf-8")).hexdigest()[:16]

    def id_for(self, natural_key: str) -> str:
        entry = self._map.get(natural_key)
        if entry:
            return entry["id"]
        nid = self._mint(natural_key)
        self._map[natural_key] = {"id": nid, "content_hash": "", "last_seen": 0.0}
        return nid

    def content_hash(self, natural_key: str) -> str:
        entry = self._map.get(natural_key)
        return entry["content_hash"] if entry else ""

    def mark_seen(self, natural_key: str, content: str) -> None:
        entry = self._map.setdefault(
            natural_key, {"id": self._mint(natural_key), "content_hash": "", "last_seen": 0.0}
        )
        entry["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        entry["last_seen"] = time.time()

    def known_keys(self) -> set[str]:
        return set(self._map.keys())

    def drop(self, natural_key: str) -> None:
        self._map.pop(natural_key, None)

    def save(self) -> None:
        if self._persist and self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._map, ensure_ascii=False, indent=2),
                                  encoding="utf-8")


@dataclass
class GovernanceArtifacts:
    """Where the trial layer persists, alongside the graph index."""

    ledger_path: Path
    tombstone_path: Path
    stable_id_path: Path

    @classmethod
    def beside(cls, graph_path: str | Path) -> "GovernanceArtifacts":
        p = Path(graph_path)
        stem = p.with_suffix("")
        return cls(
            ledger_path=Path(f"{stem}.ledger.jsonl"),
            tombstone_path=Path(f"{stem}.tombstones.jsonl"),
            stable_id_path=Path(f"{stem}.stable_ids.json"),
        )


class GovernedIngest:
    """Wraps a (re-)ingestion run with the trial layer.

    For every node it decides STORE (new) / SUPERSEDE (changed) / unchanged
    (no ledger noise); records a :class:`ConflictEvent` for changed nodes; and
    FORGETs (tombstones) nodes that disappeared from the corpus since last run.
    """

    def __init__(
        self,
        ledger: MutationLedger,
        detector: ConflictDetector,
        tombstones: TombstoneStore,
        stable_ids: StableNodeID,
        *,
        source: str = "ingest",
        enabled: bool = True,
        key_prefix: str = "",
    ) -> None:
        self.ledger = ledger
        self.detector = detector
        self.tombstones = tombstones
        self.stable_ids = stable_ids
        self.source = source
        self.enabled = enabled
        # FORGET detection is scoped to keys under this prefix, so ingesting one
        # namespace never tombstones another's nodes (they share the registry).
        self.key_prefix = key_prefix
        self.conflicts: list[ConflictEvent] = []
        self._seen_keys: set[str] = set()

    def observe(
        self,
        natural_key: str,
        node_id: str,
        content: str,
        snapshot: dict,
        *,
        prev_snapshot: Optional[dict] = None,
        embedding: Optional[list[float]] = None,
        prev_embedding: Optional[list[float]] = None,
    ) -> None:
        """Record one node from this ingestion run."""
        self._seen_keys.add(natural_key)
        if not self.enabled:
            return
        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        old_hash = self.stable_ids.content_hash(natural_key)
        if not old_hash:
            self.ledger.record(MutationType.STORE, node_id=node_id, after=snapshot,
                               source=self.source, reason="new node")
        elif old_hash != new_hash:
            verdict = self.detector.detect(
                node_id, prev_snapshot or {"content": ""}, snapshot,
                before_embedding=prev_embedding, after_embedding=embedding,
            )
            if verdict is not None:
                self.conflicts.append(verdict)
            self.ledger.record(
                MutationType.SUPERSEDE, node_id=node_id,
                before=prev_snapshot, after=snapshot, source=self.source,
                reason=verdict.reason if verdict else "content changed",
            )
        # else: unchanged → no ledger entry
        self.stable_ids.mark_seen(natural_key, content)

    def finalize(self, prior_snapshots: Optional[dict[str, dict]] = None) -> None:
        """FORGET + tombstone any node present last run but absent now."""
        if not self.enabled:
            self.stable_ids.save()
            return
        prior_snapshots = prior_snapshots or {}
        known = {k for k in self.stable_ids.known_keys() if k.startswith(self.key_prefix)}
        gone = known - self._seen_keys
        for key in gone:
            nid = self.stable_ids.id_for(key)
            snap = prior_snapshots.get(nid, {"id": nid})
            tomb = self.tombstones.entomb(snap, reason="removed from corpus")
            self.ledger.record(MutationType.FORGET, node_id=nid, before=snap,
                               source=self.source, reason="removed from corpus",
                               related_ids=[tomb])
            self.stable_ids.drop(key)
        self.stable_ids.save()
