"""The estate: the whole OS declared as data, and the planner that places it on a swarm.

``estate.yaml`` says WHAT runs (cells, replicas, constraints). The node inventory says
WHAT EXISTS (capacity, labels, current state). ``plan`` decides WHERE, ``diff`` turns
the decision into start/stop actions for the reconciler. Nothing is hand-pinned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

_SIZE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)
_UNIT = {"": 1.0, "K": 1 / 1024 / 1024, "M": 1 / 1024, "G": 1.0, "T": 1024.0}
_CMP = re.compile(r"^\s*(>=|<=|>|<|==|!=)\s*(.+)$")


class EstateError(ValueError):
    pass


class PlanError(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("estate cannot be placed:\n  " + "\n  ".join(problems))
        self.problems = problems


def to_gb(value: Any) -> float:
    """``"16G"`` -> 16.0, ``"512M"`` -> 0.5, ``8`` -> 8.0 (bare numbers are GB)."""
    if isinstance(value, bool):
        raise EstateError(f"not a size: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    match = _SIZE.match(str(value))
    if not match:
        raise EstateError(f"not a size: {value!r}")
    return float(match.group(1)) * _UNIT[match.group(2).upper()]


@dataclass
class Node:
    name: str
    cpu: float = 0.0
    mem_gb: float = 0.0
    gpus: list[float] = field(default_factory=list)  # VRAM per GPU, GB
    labels: dict[str, Any] = field(default_factory=dict)
    tags: set[str] = field(default_factory=set)  # live state, e.g. "owner-gaming"

    def label(self, key: str) -> Any:
        if key == "gpu.vram":
            return max(self.gpus, default=0.0)
        if key == "gpu.count":
            return len(self.gpus)
        return self.labels.get(key)


@dataclass
class CellPlan:
    name: str
    replicas: int = 1
    per_gpu: bool = False
    cpu: float = 0.5
    mem_gb: float = 0.5
    vram_gb: float = 0.0
    place: dict[str, Any] = field(default_factory=dict)
    spread: str | None = None
    yield_to: list[str] = field(default_factory=list)


@dataclass
class Estate:
    cells: dict[str, CellPlan]
    nodes: dict[str, Node]


def _match(actual: Any, wanted: Any) -> bool:
    if isinstance(wanted, str):
        cmp = _CMP.match(wanted)
        if cmp:
            op, rhs = cmp.groups()
            if actual is None:
                return False
            try:
                a, b = to_gb(actual), to_gb(rhs)
            except EstateError:
                a, b = str(actual), rhs.strip()
            return {
                ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b, "==": a == b, "!=": a != b,
            }[op]
    return actual == wanted


def load(estate_doc: dict[str, Any], nodes_doc: dict[str, Any]) -> Estate:
    cells: dict[str, CellPlan] = {}
    for name, raw in (estate_doc.get("cells") or {}).items():
        raw = raw or {}
        unknown = set(raw) - {
            "replicas", "per_gpu", "cpu", "mem", "vram", "place", "spread", "yield_to",
        }
        if unknown:
            raise EstateError(f"cell {name}: unknown keys {sorted(unknown)}")
        cells[name] = CellPlan(
            name=name,
            replicas=int(raw.get("replicas", 1)),
            per_gpu=bool(raw.get("per_gpu", False)),
            cpu=float(raw.get("cpu", 0.5)),
            mem_gb=to_gb(raw.get("mem", 0.5)),
            vram_gb=to_gb(raw.get("vram", 0)),
            place=dict(raw.get("place") or {}),
            spread=raw.get("spread"),
            yield_to=list(raw.get("yield_to") or []),
        )
    nodes: dict[str, Node] = {}
    for name, raw in (nodes_doc.get("nodes") or {}).items():
        raw = raw or {}
        nodes[name] = Node(
            name=name,
            cpu=float(raw.get("cpu", 0)),
            mem_gb=to_gb(raw.get("mem", 0)),
            gpus=[to_gb(v) for v in raw.get("gpus") or []],
            labels=dict(raw.get("labels") or {}),
            tags=set(raw.get("tags") or []),
        )
    return Estate(cells=cells, nodes=nodes)


def load_files(estate_path: str, nodes_path: str) -> Estate:
    with open(estate_path, encoding="utf-8") as fh:
        estate_doc = yaml.safe_load(fh) or {}
    with open(nodes_path, encoding="utf-8") as fh:
        nodes_doc = yaml.safe_load(fh) or {}
    return load(estate_doc, nodes_doc)


@dataclass
class _Free:
    cpu: float
    mem_gb: float
    gpus: list[float]


def plan(estate: Estate) -> dict[str, list[str]]:
    """Place every cell. Returns ``{cell: [node, ...]}`` with one entry per replica
    (a node appears once per GPU it serves for a ``per_gpu`` cell). Raises PlanError
    listing EVERY unplaceable replica, not just the first."""
    free = {n.name: _Free(n.cpu, n.mem_gb, list(n.gpus)) for n in estate.nodes.values()}
    placement: dict[str, list[str]] = {}
    problems: list[str] = []

    def eligible(cell: CellPlan, node: Node) -> str | None:
        for tag in cell.yield_to:
            if tag in node.tags:
                return f"yields to {tag}"
        for key, wanted in cell.place.items():
            if not _match(node.label(key), wanted):
                return f"{key}={node.label(key)!r} fails {wanted!r}"
        return None

    def fits(cell: CellPlan, f: _Free) -> int | None:
        """Index of the GPU it would take (-1 when no GPU is needed), or None."""
        if f.cpu < cell.cpu or f.mem_gb < cell.mem_gb:
            return None
        if cell.vram_gb <= 0:
            return -1
        best = [i for i, v in enumerate(f.gpus) if v >= cell.vram_gb]
        return min(best, key=lambda i: f.gpus[i]) if best else None

    def take(cell: CellPlan, node: str, gpu: int) -> None:
        f = free[node]
        f.cpu -= cell.cpu
        f.mem_gb -= cell.mem_gb
        if gpu >= 0:
            f.gpus[gpu] -= cell.vram_gb
        placement.setdefault(cell.name, []).append(node)

    order = sorted(estate.cells.values(), key=lambda c: (-c.vram_gb, -c.mem_gb, c.name))
    for cell in order:
        nodes = sorted(estate.nodes.values(), key=lambda n: n.name)
        reasons = {n.name: eligible(cell, n) for n in nodes}
        ok = [n for n in nodes if reasons[n.name] is None]

        if cell.per_gpu:
            for node in ok:
                f = free[node.name]
                for gpu, vram in enumerate(f.gpus):
                    if f.cpu < cell.cpu or f.mem_gb < cell.mem_gb or vram < cell.vram_gb:
                        problems.append(
                            f"{cell.name}: GPU {gpu} of {node.name} lacks capacity"
                        )
                        continue
                    take(cell, node.name, gpu)
            if not placement.get(cell.name):
                why = "; ".join(f"{k}: {v}" for k, v in reasons.items() if v) or "no GPU nodes"
                problems.append(f"{cell.name}: no eligible GPU node ({why})")
            continue

        used_nodes: set[str] = set()
        used_spread: set[Any] = set()
        for replica in range(cell.replicas):
            candidates = []
            for node in ok:
                if node.name in used_nodes:
                    continue
                if cell.spread and node.label(cell.spread) in used_spread:
                    continue
                gpu = fits(cell, free[node.name])
                if gpu is not None:
                    candidates.append((node, gpu))
            if not candidates:
                excluded = "; ".join(f"{k}: {v}" for k, v in reasons.items() if v)
                problems.append(
                    f"{cell.name} replica {replica + 1}/{cell.replicas}: no node fits"
                    + (f" (excluded {excluded})" if excluded else "")
                    + (f" (spread on {cell.spread})" if cell.spread else "")
                )
                continue
            node, gpu = max(candidates, key=lambda c: (free[c[0].name].mem_gb, c[0].name))
            take(cell, node.name, gpu)
            used_nodes.add(node.name)
            if cell.spread:
                used_spread.add(node.label(cell.spread))

    if problems:
        raise PlanError(problems)
    return {k: sorted(v) for k, v in sorted(placement.items())}


def diff(current: dict[str, list[str]], desired: dict[str, list[str]]) -> list[dict[str, str]]:
    """Reconciler actions that turn ``current`` into ``desired``. Starts come before
    stops so a moved cell is never at zero replicas mid-change."""
    starts, stops = [], []
    for cell in sorted(set(current) | set(desired)):
        have = list(current.get(cell, []))
        for node in desired.get(cell, []):
            if node in have:
                have.remove(node)
            else:
                starts.append({"action": "start", "cell": cell, "node": node})
        stops.extend({"action": "stop", "cell": cell, "node": node} for node in have)
    return starts + stops
