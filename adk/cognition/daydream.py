"""Daydream as a library (design section 3): plan inside the VERIFIED model, gated by
``permits(plan)``, and the contract audit that runs at every rescope (section 5).

* :func:`plan` -- breadth-first search over frames predicted by the held rule toward the
  held goal, within a node budget. It refuses to search unless ``permits`` allows the
  ``plan`` request (the ARC policy: a rule replay-verified in THIS scope, and a goal
  consistent in it). The planner never touches the true environment.
* :func:`audit_contract` -- a carried rule must (1) return a well-formed prediction on
  the new scope's first frame and (2) replay every stored sample transition it carries
  under that sample's own binding. A failure narrows it; a pass leaves it CARRIED -- it
  becomes held only after replay in the new scope.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ..reasoning.solve.context import Context, Decision, Invariants, Request, permits
from .worldmodel import bind, compile_rule

__all__ = ["plan", "audit_contract", "PLAN_BUDGET"]

PLAN_BUDGET = 4000


def plan(
    ctx: Context,
    frame: np.ndarray,
    rule_key: str,
    goal_key: str,
    world: Dict[str, Any],
    *,
    invariants: Optional[Invariants] = None,
    policy: Optional[Dict[str, Any]] = None,
    budget: int = PLAN_BUDGET,
    actions: Tuple[int, ...] = (1, 2, 3, 4),
) -> Tuple[Decision, Optional[List[int]], int]:
    """``(decision, actions or None, nodes expanded)``."""
    d = permits(
        ctx,
        Request("plan", args={"goal_name": goal_key.split(":", 1)[1]}),
        invariants=invariants,
        policy=policy,
    )
    if not d.allowed:
        return d, None, 0
    rule = ctx.facts[rule_key].value
    goal = ctx.facts[goal_key].value
    predict = bind(compile_rule(rule["source"], rule_key), world)
    goal_fn = compile_rule(goal["source"], goal_key)
    start = np.asarray(frame)
    if goal_fn(start, world):
        return d, [], 0
    parent: Dict[bytes, Tuple[Optional[bytes], int]] = {start.tobytes(): (None, 0)}
    frames = {start.tobytes(): start}
    q = deque([start])
    nodes = 0
    while q and nodes < budget:
        cur = q.popleft()
        nodes += 1
        for a in actions:
            nxt = predict(cur, a)
            if nxt is None:
                continue
            nxt = np.asarray(nxt)
            key = nxt.tobytes()
            if key in parent:
                continue
            parent[key] = (cur.tobytes(), a)
            frames[key] = nxt
            if goal_fn(nxt, world):
                path: List[int] = []
                k: Optional[bytes] = key
                while k is not None and parent[k][0] is not None:
                    prev, act = parent[k]
                    path.append(act)
                    k = prev
                return d, path[::-1], nodes
            q.append(nxt)
    return d, None, nodes


def audit_contract(
    fact_value: Dict[str, Any], first_frame: np.ndarray, world: Dict[str, Any], name: str
) -> Tuple[bool, str]:
    """``(passed, why)`` for one carried rule (design section 5, steps 3a and 3c)."""
    try:
        fn = compile_rule(fact_value["source"], name)
    except Exception as exc:  # noqa: BLE001 - a rule that does not compile fails the audit
        return False, "does not compile: %s" % exc
    first = np.asarray(first_frame)
    shaped = 0
    for a in (1, 2, 3, 4):
        try:
            p = fn(first, a, world)
        except Exception as exc:  # noqa: BLE001
            return False, "raises on the first frame: %s: %s" % (type(exc).__name__, exc)
        if p is not None:
            if np.asarray(p).shape != first.shape:
                return False, "prediction shape %s != frame %s" % (np.asarray(p).shape, first.shape)
            shaped += 1
    if not shaped:
        return False, "no well-formed prediction on the first frame"
    for i, s in enumerate(fact_value.get("samples") or []):
        b = np.asarray(s["before"], dtype=np.int8)
        af = np.asarray(s["after"], dtype=np.int8)
        p = fn(b, int(s["action"]), s["world"])
        if p is None:
            return False, "abstains on stored sample %d" % i
        p = np.asarray(p)
        m = (p != af) & (p != -1)
        if p.shape != af.shape or m.any():
            return False, "stored sample %d (%s) replays wrong on %d cells" % (
                i,
                s.get("ref", "?"),
                int(m.sum()),
            )
    return True, "well-formed on the first frame; %d stored samples replay" % len(
        fact_value.get("samples") or []
    )
