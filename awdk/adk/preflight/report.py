"""Preflight capability report — dataclasses + a plain-ASCII renderer.

Phase 0 of the Aither self-bootstrap PREFLIGHT layer. This module is pure
data + formatting; the live probing lives in ``probe.py``. It is deliberately
NOT a @tool module, so ``from __future__ import annotations`` and ``X | None``
are fine here.

The renderer is honest by construction:
  * a hosted slot that is merely *reachable* is NEVER printed as an unqualified
    ``OK`` — its note says spend/tier is unverified until the first real call;
  * if any REQUIRED slot is not OK (and is not an optional slot that is simply
    UNSUPPORTED on this build), an ``ABORT:`` block names the slot and the
    honest reason, so a harness can decide to stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Slot status vocabulary.
STATUS_OK = "OK"
STATUS_MISSING = "MISSING"
STATUS_UNREACHABLE = "UNREACHABLE"
STATUS_AUTH = "AUTH"
STATUS_TIER_DENIED = "TIER_DENIED"
STATUS_UNSUPPORTED = "UNSUPPORTED"

# Source vocabulary — where the capability is actually served from.
SOURCE_LOCAL = "local"
SOURCE_HOSTED = "hosted"
SOURCE_OFFLINE = "offline"
SOURCE_NONE = "none"


@dataclass
class SlotHealth:
    """Health of a single capability slot (primary, reasoning, embeddings, ...)."""

    slot: str
    status: str = STATUS_MISSING
    provider: str = ""
    model: str = ""
    base_url: str = ""
    latency_ms: int = 0
    source: str = SOURCE_NONE
    note: str = ""


@dataclass
class CapabilityReport:
    """The full preflight picture the harness/operator gets to see."""

    machine: dict = field(default_factory=dict)
    slots: dict = field(default_factory=dict)          # slot name -> SlotHealth
    entitlements: dict = field(default_factory=lambda: {"tier": "unknown"})
    offline: bool = False
    degraded: list = field(default_factory=list)       # slot names off preferred source
    task: str = ""
    # Requiredness is decided by the orchestrator (from a spec's llm.roles), not
    # by the probe. These two fields let ``render`` produce the ABORT block and
    # let a harness read the decision without re-deriving it.
    required: list = field(default_factory=list)       # required slot names
    abort: bool = False
    abort_reasons: list = field(default_factory=list)  # ["slot: reason", ...]


# Slots that are OPTIONAL by nature: an honest MISSING/UNSUPPORTED here is not
# an abort condition even if it somehow ended up in the required set.
_OPTIONAL_SLOTS = {"ml_teach", "voice", "vision"}


def _display_status(sh: SlotHealth) -> str:
    """A reachable hosted slot is shown as REACHABLE, never bare OK — its spend
    and tier are unverified until the first real call."""
    if sh.status == STATUS_OK and sh.source == SOURCE_HOSTED:
        return "REACHABLE"
    return sh.status


def _abort_reason(sh: SlotHealth) -> str:
    if sh.status == STATUS_MISSING:
        return "not resolved (no backend found)"
    if sh.status == STATUS_UNREACHABLE:
        return "resolved but did not answer within the probe budget"
    if sh.status == STATUS_AUTH:
        return "authentication required / rejected"
    if sh.status == STATUS_TIER_DENIED:
        return "license tier / balance denies access"
    if sh.status == STATUS_UNSUPPORTED:
        return "no send path for this capability in this build"
    return f"status={sh.status}"


def compute_abort(report: CapabilityReport) -> tuple[bool, list]:
    """Return (abort, reasons). A required slot triggers ABORT unless it is OK
    (REACHABLE counts as OK) or it is an optional slot that is merely
    UNSUPPORTED."""
    reasons: list = []
    for name in report.required:
        sh = report.slots.get(name)
        if sh is None:
            reasons.append(f"{name}: required but never probed")
            continue
        if sh.status == STATUS_OK:
            continue
        if name in _OPTIONAL_SLOTS and sh.status == STATUS_UNSUPPORTED:
            continue
        reasons.append(f"{name}: {_abort_reason(sh)}")
    return (len(reasons) > 0), reasons


def render(report: CapabilityReport) -> str:
    """Render an aligned ASCII table + machine/entitlements/offline/task lines,
    plus an ABORT block when a required slot is not satisfied. ASCII only."""
    lines: list = []
    lines.append("=" * 78)
    lines.append("AITHER PREFLIGHT  -  capability report")
    lines.append("=" * 78)

    # Context lines.
    m = report.machine or {}
    machine_bits = []
    for k in ("os", "arch", "cpu", "ram_gb", "gpu", "ollama"):
        if k in m and m[k] not in (None, ""):
            machine_bits.append(f"{k}={m[k]}")
    lines.append("machine      : " + ("  ".join(machine_bits) if machine_bits else "unknown"))
    ent = report.entitlements or {}
    lines.append("entitlements : tier=%s" % ent.get("tier", "unknown"))
    lines.append("offline      : %s" % ("YES (no primary inference)" if report.offline else "no"))
    lines.append("task         : " + (report.task or "(unset)"))
    if report.degraded:
        lines.append("degraded     : " + ", ".join(report.degraded))
    lines.append("")

    # Table.
    headers = ("SLOT", "STATUS", "SRC", "PROVIDER", "MODEL", "ms", "NOTE")
    rows: list = []
    for name, sh in report.slots.items():
        rows.append((
            sh.slot or name,
            _display_status(sh),
            sh.source or "-",
            sh.provider or "-",
            (sh.model or "-")[:22],
            str(sh.latency_ms or 0),
            sh.note or "",
        ))

    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    # Cap the NOTE column so it does not blow up line width; it is last.
    widths[6] = min(widths[6], 46)

    def _fmt(cells) -> str:
        out = []
        for i, cell in enumerate(cells):
            s = str(cell)
            if i == 6:
                s = s[: widths[6]]
            out.append(s.ljust(widths[i]))
        return "  ".join(out).rstrip()

    lines.append(_fmt(headers))
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        lines.append(_fmt(r))
    lines.append("")

    # Abort block (honest).
    abort, reasons = (report.abort, report.abort_reasons)
    if not reasons:
        abort, reasons = compute_abort(report)
    if abort and reasons:
        lines.append("!" * 78)
        lines.append("ABORT: required capability(ies) not satisfied -")
        for r in reasons:
            lines.append("   - " + r)
        lines.append("!" * 78)
    else:
        lines.append("PREFLIGHT OK: all required slots satisfied (hosted slots "
                     "reachable; spend/tier verified on first call).")

    return "\n".join(lines)
