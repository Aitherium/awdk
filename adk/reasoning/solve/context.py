"""Context dictates what behaviour is permissible: the record, the one permission
function, and the invariant layer above both (design:
``docs/context-permission-design.md``).

Three pieces, stdlib only (importable without numpy, 3.10-compatible):

* :class:`Context` -- what an agent currently knows, each :class:`Fact` carrying its
  :class:`Provenance` (``verified`` by evidence, ``owner`` on record, or ``inferred``),
  its scope, and its status (``held`` / ``carried`` / ``contradicted``). Context only
  WIDENS through evidence (:meth:`Context.widen` refuses a fact without it) and NARROWS
  on contradiction (:meth:`Context.narrow`). An ``inferred`` fact is kept -- the model
  may say anything -- but it never bears permission.
* :func:`permits` -- the ONE function from (context, request) to a :class:`Decision`.
  Invariants first; a refusal there is final. Then the domain policy for the request's
  kind. Then the default.
* :class:`Invariants` -- frozen at construction by the HOST, never derived from context.
  No fact, of any provenance, is consulted when an invariant refuses: an agent cannot
  grant itself permission by reframing, and not even an owner statement inside the run
  unlocks one (the owner changes an invariant by changing the host's configuration).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple

__all__ = [
    "Provenance",
    "Evidence",
    "Fact",
    "OwnerRecord",
    "OwnerAuthority",
    "Context",
    "Request",
    "Decision",
    "Invariants",
    "InvariantViolation",
    "Policy",
    "permits",
    "HELD",
    "CARRIED",
    "CONTRADICTED",
]

HELD = "held"  # permission-bearing in the current scope
CARRIED = "carried"  # true in an earlier scope; must re-earn evidence here
CONTRADICTED = "contradicted"  # evidence broke it; widening again needs NEWER evidence

#: Evidence kinds that can make a fact ``verified`` (the harness measured them).
VERIFYING_KINDS = frozenset({"replay", "test", "observation", "audit"})


class Provenance(str, Enum):
    VERIFIED = "verified"  # the harness measured it (replay, test, observation, audit)
    OWNER = "owner"  # an owner record on file, verified by the host's authority
    INFERRED = "inferred"  # the model or a heuristic said so; never bears permission


@dataclass(frozen=True)
class Evidence:
    kind: str  # "replay" | "test" | "observation" | "audit" | "owner_record" | "model_text"
    ref: str  # e.g. "t12..t18", a decision-card id, a test node id
    detail: str = ""


@dataclass
class Fact:
    key: str
    value: Any
    provenance: Provenance
    scope: str
    evidence: Tuple[Evidence, ...] = ()
    status: str = HELD
    since: int = 0  # domain clock (e.g. transition index) at the last status change
    at: float = field(default_factory=time.time)

    @property
    def bears_permission(self) -> bool:
        return self.status == HELD and self.provenance in (Provenance.VERIFIED, Provenance.OWNER)


@dataclass(frozen=True)
class OwnerRecord:
    """An owner authorisation ON RECORD (a decision card answered by the owner), signed by
    the host. The model never holds the key, so it cannot mint one."""

    action_class: str  # e.g. "fleet.restart", "model_route.cloud"
    card_id: str
    answered_by: str
    expires_at: float = 0.0  # 0 = no expiry
    sig: str = ""

    def payload(self) -> bytes:
        return (
            "%s|%s|%s|%.0f" % (self.action_class, self.card_id, self.answered_by, self.expires_at)
        ).encode("utf-8")


class OwnerAuthority:
    """Host-side signer/verifier of :class:`OwnerRecord` (HMAC-SHA256). Built by the host
    from a key it loads outside the agent's reach; only :meth:`verify` is handed down."""

    def __init__(self, key: bytes) -> None:
        if not key:
            raise ValueError("an owner authority needs a key")
        self.__key = bytes(key)

    def sign(self, rec: OwnerRecord) -> OwnerRecord:
        sig = hmac.new(self.__key, rec.payload(), hashlib.sha256).hexdigest()
        return OwnerRecord(rec.action_class, rec.card_id, rec.answered_by, rec.expires_at, sig)

    def verify(self, rec: Any) -> bool:
        if not isinstance(rec, OwnerRecord) or not rec.sig:
            return False
        if rec.expires_at and rec.expires_at < time.time():
            return False
        want = hmac.new(self.__key, rec.payload(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, rec.sig)


@dataclass(frozen=True)
class Request:
    """What an agent wants to do. ``kind`` selects the policy rule."""

    kind: str  # "act" | "plan" | "goal" | "strategy" | "model_route" | "episode"
    # | "report" | "tool" | "destructive"
    name: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    evidence: Tuple[Evidence, ...] = ()
    owner_record: Optional[OwnerRecord] = None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    layer: str = "default"  # "invariant" | "context" | "default"
    rule: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class InvariantViolation(Exception):  # noqa: N818 - a policy refusal, named as the doc names it
    """An invariant refused a request. ``fatal`` makes the reasoning core stop using the
    model endpoint (the same path a cross-model route takes)."""

    fatal = True

    def __init__(self, decision: Decision) -> None:
        super().__init__("invariant %s: %s" % (decision.rule, decision.reason))
        self.decision = decision
        self.status = 0


@dataclass(frozen=True)
class Invariants:
    """Above all context. Frozen; built by the host from ITS configuration.

    ``check`` reads only the request and these fields -- never a :class:`Context` -- so
    no fact, however it was obtained, can change its answer.
    """

    local_only: bool = False
    local_models: FrozenSet[str] = frozenset()
    reserved_seeds: FrozenSet[str] = frozenset()
    purpose: str = "dev"  # "eval" is the only purpose that may play a reserved seed
    destructive: FrozenSet[str] = frozenset()  # request names that need a verified owner record
    owner_verifier: Optional[Callable[[Any], bool]] = None

    def check(self, req: Request) -> Optional[Decision]:
        """A refusal, or None when no invariant applies."""
        if req.kind == "model_route" and self.local_only and req.name not in self.local_models:
            return Decision(
                False,
                "local-only models: %r is not a local model (allowed: %s)"
                % (req.name, ", ".join(sorted(self.local_models)) or "none"),
                "invariant",
                "local_only",
            )
        if req.kind == "episode" and req.name in self.reserved_seeds and self.purpose != "eval":
            return Decision(
                False,
                "seed %r is reserved for evaluation (purpose=%s)" % (req.name, self.purpose),
                "invariant",
                "reserved_seed",
            )
        if req.kind == "report":
            bad = [e for e in req.evidence if e.kind not in VERIFYING_KINDS]
            if not req.evidence or bad:
                return Decision(
                    False,
                    "a reported result must cite measured evidence (got %s)"
                    % (", ".join(e.kind for e in req.evidence) or "none"),
                    "invariant",
                    "no_fabrication",
                )
        if req.kind == "destructive" or req.name in self.destructive:
            rec = req.owner_record
            ok = (
                rec is not None
                and self.owner_verifier is not None
                and self.owner_verifier(rec)
                and rec.action_class == req.name
            )
            if not ok:
                return Decision(
                    False,
                    "destructive action %r needs a verified owner record" % req.name,
                    "invariant",
                    "owner_record",
                )
        return None


#: A policy maps a request kind to a rule ``(ctx, req) -> Decision | None`` (None = no opinion).
Policy = Mapping[str, Callable[["Context", Request], Optional[Decision]]]


class Context:
    """The context record of one agent. Mutated only by the HOST's code paths; the model
    reaches it through :meth:`infer` (never permission-bearing) and read-only views."""

    def __init__(
        self,
        scope: str,
        *,
        intent: str = "",
        owner_verifier: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self.scope = scope
        self.intent = intent
        self.domain: Dict[str, Any] = {}
        self.facts: Dict[str, Fact] = {}
        self.log: List[Dict[str, Any]] = []
        self._owner_verifier = owner_verifier

    # -- widening: evidence only ---------------------------------------------------
    def widen(
        self,
        key: str,
        value: Any,
        *,
        evidence: Tuple[Evidence, ...],
        provenance: Provenance = Provenance.VERIFIED,
        clock: int = 0,
        owner_record: Optional[OwnerRecord] = None,
    ) -> Fact:
        """Hold ``key`` in the current scope. Raises ``PermissionError`` when the evidence
        cannot carry the provenance claimed, or when ``key`` was contradicted at a clock
        later than every piece of evidence offered (stale evidence never re-widens)."""
        if provenance == Provenance.INFERRED:
            raise PermissionError("inferred facts never widen context; use infer()")
        if not evidence:
            raise PermissionError("widening %r needs evidence" % key)
        if provenance == Provenance.VERIFIED and not all(
            e.kind in VERIFYING_KINDS for e in evidence
        ):
            raise PermissionError(
                "verified facts need measured evidence (%s)" % ", ".join(sorted(VERIFYING_KINDS))
            )
        if provenance == Provenance.OWNER:
            if (
                owner_record is None
                or self._owner_verifier is None
                or not self._owner_verifier(owner_record)
            ):
                raise PermissionError("an owner fact needs an owner record the host can verify")
        old = self.facts.get(key)
        if old is not None and old.status == CONTRADICTED and clock <= old.since:
            raise PermissionError(
                "%r was contradicted at %d; evidence up to %d is stale" % (key, old.since, clock)
            )
        f = Fact(key, value, provenance, self.scope, tuple(evidence), HELD, clock)
        self.facts[key] = f
        self._note("widen", key, provenance.value, [e.ref for e in evidence])
        return f

    def infer(self, key: str, value: Any, source: str = "model") -> Fact:
        """Record what the model (or a heuristic) claims. Kept for the record; never
        bears permission, and never overwrites a verified or owner fact."""
        old = self.facts.get(key)
        f = Fact(
            key, value, Provenance.INFERRED, self.scope, (Evidence("model_text", source),), HELD
        )
        if old is None or old.provenance == Provenance.INFERRED:
            self.facts[key] = f
        self._note("infer", key, source, [])
        return f

    # -- narrowing: automatic on contradiction -------------------------------------
    def narrow(self, key: str, evidence: Evidence, *, clock: int = 0) -> Optional[Fact]:
        f = self.facts.get(key)
        if f is None:
            f = Fact(key, None, Provenance.INFERRED, self.scope)
            self.facts[key] = f
        f.status = CONTRADICTED
        f.since = int(clock)
        f.evidence = f.evidence + (evidence,)
        self._note("narrow", key, evidence.kind, [evidence.ref])
        return f

    def rescope(self, scope: str, *, keep: Callable[[Fact], bool] = lambda f: False) -> List[str]:
        """Enter a new scope: every held fact not kept by ``keep`` becomes CARRIED (true
        before, unproven here). Returns the carried keys."""
        carried = []
        for f in self.facts.values():
            if f.status == HELD and f.provenance != Provenance.INFERRED and not keep(f):
                f.status = CARRIED
                carried.append(f.key)
        self._note("rescope", scope, self.scope, carried)
        self.scope = scope
        return carried

    # -- reading -------------------------------------------------------------------
    def holds(self, key: str) -> bool:
        f = self.facts.get(key)
        return f is not None and f.bears_permission

    def held(self, prefix: str = "") -> List[str]:
        return sorted(
            k for k, f in self.facts.items() if k.startswith(prefix) and f.bears_permission
        )

    def status(self, key: str) -> str:
        f = self.facts.get(key)
        return f.status if f is not None else "absent"

    def view(self) -> Dict[str, Any]:
        """A read-only snapshot (what the model may see)."""
        return {
            "scope": self.scope,
            "intent": self.intent,
            "facts": {
                k: {
                    "provenance": f.provenance.value,
                    "status": f.status,
                    "scope": f.scope,
                    "evidence": [e.ref for e in f.evidence][-3:],
                }
                for k, f in sorted(self.facts.items())
            },
        }

    def _note(self, op: str, key: str, how: str, refs: List[str]) -> None:
        self.log.append({"op": op, "key": key, "how": how, "refs": refs[-3:], "scope": self.scope})
        if len(self.log) > 400:
            del self.log[:100]


def permits(
    ctx: Context,
    req: Request,
    *,
    invariants: Optional[Invariants] = None,
    policy: Optional[Policy] = None,
    default: bool = True,
) -> Decision:
    """THE permission function. Invariants first (final, and blind to ``ctx``), then the
    policy rule for ``req.kind``, then ``default``."""
    if invariants is not None:
        refusal = invariants.check(req)
        if refusal is not None:
            return refusal
    rule = (policy or {}).get(req.kind)
    if rule is not None:
        d = rule(ctx, req)
        if d is not None:
            return d
    return Decision(
        bool(default), "no rule for %r" % req.kind if not default else "", "default", ""
    )
