"""The CNS context hub, as a library (design sections 2.2, 4 and 4.3).

* :class:`Op` -- one proposed record change (rescope / narrow / widen / infer) with its
  evidence, clock, neuron and ``event_id``.
* :func:`merge` -- the deterministic merge: ops ordered by ``(op_rank, key, neuron,
  event_id)``; a key narrowed in the same wave refuses its widen (logged ``superseded``);
  every change goes through the awdk :class:`Context` methods, so the hub cannot widen
  anything awdk would refuse.
* :func:`record_json` / :func:`record_hash` / :func:`load_record` -- the Strata form of
  a record. The wall-clock ``Fact.at`` is not part of it, so replaying the same event
  log yields a byte-identical ``record.json``.
* :class:`ContextHub` -- consumes the Flux stream through a consumer group
  (at-least-once), deduplicates on ``data.event_id``, runs Wave A and Wave B neurons
  (each under ``asyncio.wait_for`` with its budget; a neuron that fails contributes
  nothing), merges, saves the record to Strata and acks.

``permits()`` is NOT here: it runs in-process at every effect (design risk R3). The hub
keeps a permission table for observability only.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from ..reasoning.solve.context import (
    CARRIED,
    Context,
    Evidence,
    Fact,
    Invariants,
    Provenance,
    Request,
    permits,
)
from .bus import CTX_STREAM, InProcessFlux, MemoryStrata, canonical_json

__all__ = [
    "Op",
    "merge",
    "record_json",
    "record_hash",
    "load_record",
    "ContextHub",
    "Neuron",
    "record_path",
    "contracts_path",
]

OP_RANK = {"rescope": 0, "narrow": 1, "widen": 2, "infer": 3}


@dataclass(frozen=True)
class Op:
    op: str
    key: str
    value: Any = None
    provenance: str = "verified"
    evidence: Tuple[Tuple[str, str], ...] = ()
    clock: int = 0
    neuron: str = ""
    event_id: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "op": self.op,
            "key": self.key,
            "value": self.value,
            "provenance": self.provenance,
            "evidence": [list(e) for e in self.evidence],
            "clock": self.clock,
            "neuron": self.neuron,
            "event_id": self.event_id,
        }

    @classmethod
    def from_json(cls, d: Dict[str, Any], event_id: str = "") -> "Op":
        if d["op"] not in OP_RANK:
            raise ValueError("unknown op %r" % d["op"])
        return cls(
            d["op"],
            d["key"],
            d.get("value"),
            d.get("provenance", "verified"),
            tuple((str(k), str(r)) for k, r in d.get("evidence", ())),
            int(d.get("clock", 0)),
            d.get("neuron", ""),
            d.get("event_id") or event_id,
        )


def merge(ctx: Context, ops: Sequence[Op]) -> Dict[str, Any]:
    """Apply one wave's ops deterministically. Returns what happened to each."""
    ordered = sorted(ops, key=lambda o: (OP_RANK[o.op], o.key, o.neuron, o.event_id))
    narrowed = {o.key for o in ordered if o.op == "narrow"}
    rep: Dict[str, List[str]] = {"applied": [], "superseded": [], "refused": []}
    for o in ordered:
        ev = tuple(Evidence(k, r) for k, r in o.evidence)
        try:
            if o.op == "rescope":
                ctx.rescope(o.key)
            elif o.op == "narrow":
                ctx.narrow(o.key, ev[0] if ev else Evidence("audit", o.neuron), clock=o.clock)
            elif o.op == "widen":
                if o.key in narrowed:
                    rep["superseded"].append(o.key)
                    ctx._note("superseded", o.key, o.neuron, [e.ref for e in ev])
                    continue
                ctx.widen(
                    o.key, o.value, evidence=ev, provenance=Provenance(o.provenance), clock=o.clock
                )
            else:
                ctx.infer(o.key, o.value, source=o.neuron or "model")
            rep["applied"].append("%s:%s" % (o.op, o.key))
        except PermissionError as exc:
            rep["refused"].append("%s:%s (%s)" % (o.op, o.key, exc))
            ctx._note("refused", o.key, o.neuron, [str(exc)[:80]])
    return rep


def _fact_json(f: Fact) -> Dict[str, Any]:
    return {
        "value": f.value,
        "provenance": f.provenance.value,
        "scope": f.scope,
        "status": f.status,
        "since": f.since,
        "evidence": [[e.kind, e.ref, e.detail] for e in f.evidence],
    }


def record_json(ctx: Context) -> str:
    """The Strata ``record.json`` (canonical; excludes wall-clock timestamps)."""
    return canonical_json(
        {
            "scope": ctx.scope,
            "intent": ctx.intent,
            "domain": ctx.domain,
            "facts": {k: _fact_json(f) for k, f in sorted(ctx.facts.items())},
            "log": ctx.log,
        }
    )


def record_hash(ctx: Context) -> str:
    return hashlib.sha256(record_json(ctx).encode("utf-8")).hexdigest()


def load_record(raw: Dict[str, Any], *, as_carried: bool = True) -> Context:
    """A record back from Strata. ``as_carried``: every held fact comes back CARRIED
    (design 2.2 ``load``) -- it was true in the scope it was saved in, not here."""
    ctx = Context(raw["scope"], intent=raw.get("intent", ""))
    ctx.domain = dict(raw.get("domain") or {})
    ctx.log = list(raw.get("log") or [])
    for k, d in raw.get("facts", {}).items():
        status = d["status"]
        if as_carried and status == "held" and d["provenance"] != "inferred":
            status = CARRIED
        ctx.facts[k] = Fact(
            k,
            d["value"],
            Provenance(d["provenance"]),
            d["scope"],
            tuple(Evidence(*e) for e in d["evidence"]),
            status,
            int(d["since"]),
        )
    return ctx


def record_path(tenant: str, subject: str) -> str:
    return "aither://warm/context/%s/%s/record.json" % (tenant, subject)


def contracts_path(tenant: str, domain: str) -> str:
    return "aither://warm/contracts/%s/%s.jsonl" % (tenant, domain)


#: A neuron: ``(frozen snapshot view, event, hub) -> ops``. Pure: it never writes.
Neuron = Callable[[Dict[str, Any], Dict[str, Any], "ContextHub"], Awaitable[List[Op]]]


@dataclass
class _NeuronSpec:
    name: str
    fn: Neuron
    budget_s: float


async def _fire(
    spec: _NeuronSpec, snap: Dict[str, Any], event: Dict[str, Any], hub: "ContextHub"
) -> Tuple[str, List[Op], str]:
    try:
        ops = await asyncio.wait_for(spec.fn(snap, event, hub), spec.budget_s)
        return (
            spec.name,
            [o if o.neuron else Op(**{**o.__dict__, "neuron": spec.name}) for o in ops],
            "",
        )
    except Exception as exc:  # noqa: BLE001 - a failed neuron contributes zero ops
        return spec.name, [], "%s: %s" % (type(exc).__name__, exc)


async def _passthrough(snap: Dict[str, Any], event: Dict[str, Any], hub: "ContextHub") -> List[Op]:
    """Ops the event already carries (Sense /perceive, world model /rules/verify)."""
    eid = event["data"]["event_id"]
    return [Op.from_json(d, eid) for d in event["data"].get("ops", [])]


async def _permission_table(
    snap: Dict[str, Any], event: Dict[str, Any], hub: "ContextHub"
) -> List[Op]:
    ctx = hub.contexts.get(event["subject"])
    if ctx is not None:
        hub.permission_table[event["subject"]] = {
            k: permits(ctx, Request(k), invariants=hub.invariants, policy=hub.policy)
            for k in ("plan", "act", "report")
        }
    return []


@dataclass
class ContextHub:
    strata: MemoryStrata
    flux: InProcessFlux
    tenant: str = "slice"
    invariants: Invariants = field(default_factory=Invariants)
    policy: Optional[Dict[str, Any]] = None
    group: str = "cns-hub"
    wave_a: List[_NeuronSpec] = field(default_factory=list)
    wave_b: List[_NeuronSpec] = field(default_factory=list)
    on_rescope: Optional[Callable[[Context, Dict[str, Any], "ContextHub"], None]] = None

    def __post_init__(self) -> None:
        self.contexts: Dict[str, Context] = {}
        self.seen: set = set()
        self.applied: List[Dict[str, Any]] = []  # the event log, in apply order
        self.acks: List[Tuple[str, float]] = []  # (event_id, seconds from xadd-read to ack)
        self.duplicates: List[str] = []
        self.neuron_failures: List[str] = []
        self.permission_table: Dict[str, Dict[str, Any]] = {}
        self.flux.create_group(CTX_STREAM, self.group)
        if not self.wave_a:
            self.wave_a = [_NeuronSpec("passthrough", _passthrough, 0.05)]
        if not self.wave_b:
            self.wave_b = [_NeuronSpec("permission_table", _permission_table, 0.5)]

    def add_neuron(self, wave: str, name: str, fn: Neuron, budget_s: float) -> None:
        (self.wave_a if wave == "a" else self.wave_b).append(_NeuronSpec(name, fn, budget_s))

    # -- the record --------------------------------------------------------------
    def context(self, subject: str, scope: str = "", intent: str = "") -> Context:
        ctx = self.contexts.get(subject)
        if ctx is None:
            raw = self.strata.read_json(record_path(self.tenant, subject))
            ctx = load_record(raw) if raw else Context(scope or subject, intent=intent)
            self.contexts[subject] = ctx
        return ctx

    def save(self, subject: str) -> str:
        return self.strata.write(
            record_path(self.tenant, subject), record_json(self.contexts[subject])
        )

    # -- one event -----------------------------------------------------------------
    async def handle(self, event: Dict[str, Any]) -> Dict[str, Any]:
        data = event["data"]
        ctx = self.context(event["subject"], data.get("scope", ""))
        report: Dict[str, Any] = {"event_id": data["event_id"], "type": event["type"]}
        if event["type"] == "ctx.rescope":
            ctx.rescope(data["scope"])
            ctx.domain["level_transitions"] = 0
            if self.on_rescope is not None:
                self.on_rescope(ctx, event, self)
        if "level_transitions" in data:
            ctx.domain["level_transitions"] = int(data["level_transitions"])
        for wave_name, wave in (("a", self.wave_a), ("b", self.wave_b)):
            snap = ctx.view()
            fired = await asyncio.gather(*(_fire(s, snap, event, self) for s in wave))
            ops: List[Op] = []
            for name, got, err in fired:
                if err:
                    self.neuron_failures.append("%s@%s: %s" % (name, data["event_id"], err))
                    ctx._note("neuron_failed", name, err[:60], [data["event_id"]])
                ops.extend(got)
            report[wave_name] = merge(ctx, ops)
        self.applied.append(event)
        self.save(event["subject"])
        return report

    # -- the stream ----------------------------------------------------------------
    async def pump(self, redeliver: bool = False) -> List[Dict[str, Any]]:
        """Consume everything pending on the stream; returns the handled reports."""
        entries = self.flux.claim_pending(CTX_STREAM, self.group) if redeliver else []
        entries += self.flux.read_group(CTX_STREAM, self.group)
        out = []
        for e in entries:
            eid = e.event["data"]["event_id"]
            if eid in self.seen:
                self.duplicates.append(eid)
            else:
                out.append(await self.handle(e.event))
                self.seen.add(eid)
            if self.flux.ack(CTX_STREAM, self.group, e.entry_id):
                self.acks.append((eid, time.monotonic() - e.delivered_at))
        return out
