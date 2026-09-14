"""Per-agent knowledge ledgers that merge into one prime source of truth.

Every agent, persona, pack and subagent keeps its OWN awgit oplog and ships it
to a prime log. That is the whole model, and it is git's model applied to what
agents learn rather than to what they write: work locally, publish, merge, and
let the prime be the thing everyone reads.

WHY PER-AGENT AND NOT ONE SHARED LOG. A single shared log makes concurrent
agents contend on one file and, worse, destroys attribution — you get a stream
of changes with no reliable answer to "which agent decided this". awgit records
a verified actor per op, so keeping the logs separate and merging them keeps
provenance intact through the merge instead of flattening it at write time.

WHY MERGE AND NOT APPEND. Two agents can touch the same symbol. Appending both
gives the prime two contradictory statements about one node and no way to tell
which is current. `awgit.merge_ops` does a symbol-level three-way merge and
returns explicit `MergeConflict`s carrying a blast radius and a suggestion —
so a genuine disagreement is REPORTED rather than resolved by arrival order.

THE RULE THIS MODULE ENFORCES: **a conflict is never auto-resolved.** Silently
picking a side would make the prime confidently wrong, which is worse than
leaving two agents visibly disagreeing — one of them can be asked. Conflicts are
returned, counted, and left for a decision.

Idempotence matters more here than usual: agents publish on a timer, so the same
ops arrive repeatedly. Merging twice must not double-count, or the prime's
history becomes a function of how often the loop ran.

    from adk.agent_ledger import agent_log, publish, merge_to_prime

    log = agent_log("iris")           # ~/.aither/agents/iris/oplog
    path = publish("iris")            # ship it
    report = merge_to_prime(["iris", "hermes"])
    report.conflicts                  # decide these; nothing was guessed
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: Where an agent's own ledger lives. Per-agent directories rather than one
#: shared file: concurrent writers, and attribution that survives the merge.
AITHER_HOME = Path(os.environ.get("AITHER_HOME", Path.home() / ".aither"))
AGENTS_ROOT = AITHER_HOME / "agents"
PRIME_ROOT = AITHER_HOME / "prime"
OUTBOX = AITHER_HOME / "outbox"


@dataclass
class MergeReport:
    """What a merge actually did. Every field is countable on purpose."""

    merged: int = 0
    skipped_duplicates: int = 0
    conflicts: list = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.conflicts

    def summary(self) -> str:
        parts = [f"{self.merged} op(s) merged from {len(self.agents)} agent(s)"]
        if self.skipped_duplicates:
            parts.append(f"{self.skipped_duplicates} already present")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} CONFLICT(s) awaiting a decision")
        return "; ".join(parts)


def _oplog(root: Path) -> Optional[Any]:
    """An OpLog at `root`, or None when awgit is unavailable.

    None rather than raising: adk runs on machines without awgit, and an agent
    that cannot keep a ledger should still be able to run.
    """
    try:
        from awgit import OpLog
    except ImportError:
        return None
    root.mkdir(parents=True, exist_ok=True)
    try:
        return OpLog(root)
    except Exception:  # noqa: BLE001 - a store that will not open is absence
        return None


def agent_log(name: str) -> Optional[Any]:
    """The named agent's own ledger."""
    return _oplog(AGENTS_ROOT / _safe(name) / "oplog")


def prime_log() -> Optional[Any]:
    """The single source of truth every agent merges into."""
    return _oplog(PRIME_ROOT / "oplog")


def _safe(name: str) -> str:
    """A filesystem-safe agent name.

    Agent names come from packs and personas, which are user-authored: a name
    containing a separator would otherwise write outside the agents root.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name).strip("-")
    return cleaned or "unnamed"


def publish(name: str, dest: Optional[Path] = None) -> Optional[Path]:
    """Export an agent's ledger so it can be shipped. Returns the path written."""
    log = agent_log(name)
    if log is None:
        return None
    OUTBOX.mkdir(parents=True, exist_ok=True)
    target = dest or (OUTBOX / f"{_safe(name)}.oplog.json")
    try:
        log.export(target)
    except Exception:  # noqa: BLE001 - nothing to publish yet is normal
        return None
    return target


def merge_to_prime(names: list[str], *, repo_path: Optional[str] = None) -> MergeReport:
    """Merge each agent's ledger into prime. Conflicts are REPORTED, never resolved.

    Order is deliberate: agents are merged one at a time against the CURRENT
    prime, so the second agent is merged against the first agent's accepted work
    rather than against a stale base. Merging them all against the original base
    would hide disagreements between two agents that both changed the same
    symbol in the same round.
    """
    report = MergeReport()
    prime = prime_log()
    if prime is None:
        report.notes.append("awgit unavailable — nothing merged")
        return report

    try:
        from awgit import merge_ops
    except ImportError:
        report.notes.append("awgit.merge_ops unavailable — nothing merged")
        return report

    for name in names:
        # Asked BEFORE opening, because opening creates the directory: a typo'd
        # agent name would otherwise be handed a fresh empty ledger, merge
        # nothing, and say nothing — indistinguishable from a real agent that
        # had no work this round.
        if not (AGENTS_ROOT / _safe(name) / "oplog").exists():
            report.notes.append(f"{name}: no ledger — never published, or the name is wrong")
            continue

        log = agent_log(name)
        if log is None:
            report.notes.append(f"{name}: ledger could not be opened")
            continue

        try:
            incoming = list(log.all_ops())
        except Exception:  # noqa: BLE001
            report.notes.append(f"{name}: ledger unreadable")
            continue
        if not incoming:
            continue

        report.agents.append(name)

        # Idempotence: agents publish on a timer, so the same ops arrive again
        # and again. Without this the prime's history becomes a function of how
        # often the loop ran rather than of what changed.
        fresh = []
        for op in incoming:
            sha = getattr(op, "git_sha", None)
            try:
                known = bool(sha) and prime.has_commit(sha)
            except Exception:  # noqa: BLE001
                known = False
            if known:
                report.skipped_duplicates += 1
            else:
                fresh.append(op)
        if not fresh:
            continue

        try:
            base = list(prime.all_ops())
        except Exception:  # noqa: BLE001
            base = []

        try:
            result = merge_ops(base, fresh, repo_path=repo_path)
        except Exception as e:  # noqa: BLE001 - a failed merge must not half-apply
            report.notes.append(f"{name}: merge failed ({type(e).__name__}: {e})")
            continue

        conflicts = list(getattr(result, "conflicts", None) or [])
        if conflicts:
            # Do NOT import a conflicted set. A half-merged ledger is the one
            # outcome with no honest reading: prime would hold some of this
            # agent's work and silently omit the rest.
            report.conflicts.extend(conflicts)
            report.notes.append(f"{name}: {len(conflicts)} conflict(s) — not merged")
            continue

        for op in fresh:
            try:
                prime.append(op)
                report.merged += 1
            except Exception as e:  # noqa: BLE001
                report.notes.append(f"{name}: append failed ({type(e).__name__}: {e})")

    return report


def contributors(node_id: str) -> dict[str, int]:
    """Which agents touched a node, and how often — read from prime.

    The question a single shared log cannot answer, and the reason attribution
    is kept through the merge rather than flattened at write time.
    """
    prime = prime_log()
    if prime is None:
        return {}
    try:
        ops = list(prime.ops_for_node(node_id))
    except Exception:  # noqa: BLE001
        return {}

    counts: dict[str, int] = {}
    for op in ops:
        who = getattr(op, "verified_actor", None) or getattr(op, "actor", None) or "unknown"
        counts[who] = counts.get(who, 0) + 1
    return counts


def status() -> dict:
    """A plain census of the federation, for a human or a routine."""
    agents: dict[str, int] = {}
    if AGENTS_ROOT.is_dir():
        for d in sorted(AGENTS_ROOT.iterdir()):
            if not d.is_dir():
                continue
            log = _oplog(d / "oplog")
            try:
                agents[d.name] = len(list(log.all_ops())) if log else 0
            except Exception:  # noqa: BLE001
                agents[d.name] = 0

    prime = prime_log()
    try:
        prime_ops = len(list(prime.all_ops())) if prime else 0
    except Exception:  # noqa: BLE001
        prime_ops = 0

    return {"available": prime is not None, "agents": agents, "prime_ops": prime_ops}
