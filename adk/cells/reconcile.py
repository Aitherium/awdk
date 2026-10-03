"""Apply planner actions to nodes. The reconciler is the only thing that starts or
stops a cell, and it holds two rules:

- **Immutable images.** A cell runs from ``image@sha256:<digest>`` only. A tag or a
  bind-mounted source tree means "what is running?" has no exact answer.
- **Never drop to zero.** Starts run first. If a cell's start fails anywhere, none of
  that cell's stops run, so a failed move leaves the old replicas serving.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

CELL_LABEL = "aither.cell"
_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")

Runner = Callable[[list[str]], str]


class ReconcileError(ValueError):
    pass


class Runtime(Protocol):
    """One node's container runtime."""

    def start(self, cell: str) -> None: ...

    def stop(self, cell: str) -> None: ...

    def running(self) -> list[str]: ...


@dataclass
class Report:
    done: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    held: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.held


def apply(actions: list[dict[str, str]], runtimes: dict[str, Runtime]) -> Report:
    """Run ``estate.diff`` actions. Every node an action names must have a runtime;
    that is checked before anything changes."""
    missing = sorted({a["node"] for a in actions} - set(runtimes))
    if missing:
        raise ReconcileError(f"no runtime for node(s): {', '.join(missing)}")

    report = Report()
    failed_cells: set[str] = set()
    for action in [a for a in actions if a["action"] == "start"]:
        try:
            runtimes[action["node"]].start(action["cell"])
            report.done.append(action)
        except Exception as exc:  # noqa: BLE001 - any runtime failure holds the stops
            report.failed.append({**action, "error": str(exc)[:300]})
            failed_cells.add(action["cell"])
    for action in [a for a in actions if a["action"] == "stop"]:
        if action["cell"] in failed_cells:
            report.held.append({**action, "reason": "a start for this cell failed"})
            continue
        try:
            runtimes[action["node"]].stop(action["cell"])
            report.done.append(action)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            report.failed.append({**action, "error": str(exc)[:300]})
    return report


def observe(runtimes: dict[str, Runtime]) -> dict[str, list[str]]:
    """What runs now, as ``{cell: [node, ...]}`` - the ``current`` input to ``diff``."""
    current: dict[str, list[str]] = {}
    for node, runtime in sorted(runtimes.items()):
        for cell in runtime.running():
            current.setdefault(cell, []).append(node)
    return current


class PodmanRuntime:
    """Runs cells as podman containers named ``cell-<name>``, labelled with the cell,
    from digest-pinned images only. ``run`` executes a command and returns stdout;
    pass a remote runner (ssh, an agent API) to drive another node."""

    def __init__(
        self,
        images: dict[str, str],
        run: Runner,
        *,
        network: str | None = None,
        args: dict[str, list[str]] | None = None,
    ):
        bad = sorted(c for c, ref in images.items() if not _DIGEST.match(ref))
        if bad:
            raise ReconcileError(
                f"images must be pinned by digest (name@sha256:...): {', '.join(bad)}"
            )
        self.images = dict(images)
        self.run = run
        self.network = network
        self.args = {c: list(a) for c, a in (args or {}).items()}

    def start(self, cell: str) -> None:
        if cell not in self.images:
            raise ReconcileError(f"no image declared for cell {cell!r}")
        cmd = ["podman", "run", "-d", "--replace", "--name", f"cell-{cell}",
               "--label", f"{CELL_LABEL}={cell}", "--restart=on-failure"]
        if self.network:
            cmd += ["--network", self.network]
        self.run([*cmd, self.images[cell], *self.args.get(cell, [])])

    def stop(self, cell: str) -> None:
        self.run(["podman", "rm", "-f", "--time", "30", f"cell-{cell}"])

    def running(self) -> list[str]:
        out = self.run(["podman", "ps", "--filter", f"label={CELL_LABEL}",
                        "--format", "json"])
        cells = []
        for row in json.loads(out or "[]"):
            name = (row.get("Labels") or {}).get(CELL_LABEL)
            if name:
                cells.append(name)
        return sorted(cells)
