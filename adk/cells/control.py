"""The control loop: estate file -> measured nodes -> plan -> diff -> apply.

One ``tick`` reads the desired estate, asks every node what it has and what it runs,
places the estate on that, and applies the difference. A node that cannot be
measured is left out of placement AND out of the actions, so an unreachable machine
is never told to stop anything; the report names it. Running ``tick`` on an interval
is the whole reconciler.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .estate import PlanError, diff, load, plan
from .reconcile import Report, Runtime, apply, observe


class ControlledNode(Runtime, Protocol):
    name: str

    def inventory(self) -> dict[str, Any]: ...


@dataclass
class TickResult:
    placement: dict[str, list[str]] = field(default_factory=dict)
    actions: list[dict[str, str]] = field(default_factory=list)
    report: Report | None = None
    unreachable: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def converged(self) -> bool:
        return (not self.actions and not self.problems and not self.unreachable)

    def as_dict(self) -> dict[str, Any]:
        rep = self.report
        return {
            "converged": self.converged,
            "dry_run": self.dry_run,
            "placement": self.placement,
            "actions": self.actions,
            "done": rep.done if rep else [],
            "failed": rep.failed if rep else [],
            "held": rep.held if rep else [],
            "unreachable": self.unreachable,
            "problems": self.problems,
        }


def tick(estate_doc: dict[str, Any], nodes: list[ControlledNode], *,
         dry_run: bool = False) -> TickResult:
    result = TickResult(dry_run=dry_run)
    inventory: dict[str, Any] = {}
    live: dict[str, ControlledNode] = {}
    for node in nodes:
        try:
            inventory[node.name] = node.inventory()
            live[node.name] = node
        except Exception as exc:  # noqa: BLE001 - recorded, node excluded from the tick
            result.unreachable[node.name] = str(exc)[:300]

    try:
        result.placement = plan(load(estate_doc, {"nodes": inventory}))
    except PlanError as exc:
        result.problems = list(exc.problems)
        return result

    try:
        current = observe(dict(live))
    except Exception as exc:  # noqa: BLE001 - cannot diff what cannot be observed
        result.problems = [f"observe failed: {str(exc)[:300]}"]
        return result
    result.actions = diff(current, result.placement)
    if not dry_run and result.actions:
        result.report = apply(result.actions, dict(live))
    return result


def run(
    load_estate: Callable[[], dict[str, Any]],
    nodes: list[ControlledNode],
    *,
    interval: float,
    on_tick: Callable[[TickResult], None],
    ticks: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Tick forever (or ``ticks`` times). The estate is re-read every tick, so a merged
    change to the estate file is the deploy."""
    done = 0
    while ticks is None or done < ticks:
        on_tick(tick(load_estate(), nodes))
        done += 1
        if ticks is None or done < ticks:
            sleep(interval)
