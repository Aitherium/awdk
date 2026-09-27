"""The first vertical slice of the cognition frameworks design (section 9), in process.

One synthetic PUSH suite (``--families push --n 5 --suite-seed cog-slice-1``, levels
0-1, rung C0) runs through the nine hops, each with a check that can fail:

    H0 classroom -> H1 Flux -> H2 Sense -> H3 CNS -> H4 world model -> H5 Daydream
    -> H6 act -> H7 Slumber -> H8 Evolution

Fleet planes are replaced by in-process stand-ins with the same method names
(:mod:`.bus`): the Flux Stream consumer group and a Strata path store. Everything that
decides anything -- the record, ``permits()``, the invariants, replay verification --
is the real awdk code. This is the SCRIPTED arm (hand-written rules isolate the
plumbing); the local-model arm (gemma4-12b through MicroScheduler) is a measurement
that needs the fleet and is not run here.

``run_slice()`` returns ``{hop: {"ok": bool, ...}}``; ``python -m adk.cognition.slice``
prints it and exits 0 only when all nine hops are green.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..reasoning.solve._context_gate import ARC_POLICY
from ..reasoning.solve._vendor.memory import score_prediction
from ..reasoning.solve.context import (
    CARRIED,
    CONTRADICTED,
    Context,
    Evidence,
    Fact,
    Invariants,
    Provenance,
    Request,
    permits,
)
from .bus import InProcessFlux, MemoryStrata, canonical_json, make_event
from .classroom._vendor import rules
from .classroom._vendor.generate import game_spec
from .classroom._vendor.solve import bfs, replay
from .classroom.env import PushEnv
from .daydream import audit_contract, plan
from .hub import ContextHub, Op, contracts_path, record_hash
from .perceive import COLOUR_ROLES, DIRS, find_single, perceive
from .slumber import consolidate_contracts, refuted_path
from .worldmodel import (
    PUSH_GOAL,
    PUSH_RULE,
    bind,
    compile_rule,
    heldout_accuracy,
    verify,
    world_binding,
)

__all__ = [
    "SUITE_SEED",
    "FAMILY",
    "LEVELS",
    "N_GAMES",
    "slice_invariants",
    "generate_suite",
    "oracle_check",
    "new_hub",
    "Agent",
    "run_slice",
    "score_config",
]

SUITE_SEED = "cog-slice-1"
FAMILY = "push"
N_GAMES = 5
LEVELS = (0, 1)
EXPLORE_BUDGET = 16
H2_WINDOW = 10  # hop H2 judges perception on the first 10 transitions
LEVEL_SUPPORT = 2
RULE_KEY = "rule:push"
GOAL_KEY = "goal:boxes_on_targets"
LOCAL_MODELS = frozenset({"gemma4-12b"})
ROLE_OF = {
    "wall": rules.R_WALL,
    "floor": rules.R_FLOOR,
    "player": rules.R_PLAYER,
    "box": rules.R_BOX,
    "goal": rules.R_TARGET,
    "box_on": rules.R_BOX_ON,
}


def slice_invariants() -> Invariants:
    """What ``config/cognition_invariants.yaml`` will hold for the slice: local models
    only, a reserved seed, measured evidence for every report."""
    return Invariants(
        local_only=True,
        local_models=LOCAL_MODELS,
        reserved_seeds=frozenset({"arc-reserved-0"}),
        purpose="dev",
    )


# ---------------------------------------------------------------------------- H0
def suite_path(name: str) -> str:
    return "aither://warm/classroom/%s/%s/%s" % (FAMILY, SUITE_SEED, name)


def generate_suite(strata: MemoryStrata, n: int = N_GAMES) -> List[Dict[str, Any]]:
    specs = [game_spec(i, SUITE_SEED, FAMILY) for i in range(n)]
    for s in specs:
        strata.write_json(suite_path("%s.json" % s["short_id"]), s)
    strata.write_json(
        suite_path("manifest.json"),
        {
            "family": FAMILY,
            "suite_seed": SUITE_SEED,
            "levels": list(LEVELS),
            "games": [
                {
                    "id": s["short_id"],
                    "optimal_actions": [s["optimal_actions"][i] for i in LEVELS],
                    "baseline_actions": [s["baseline_actions"][i] for i in LEVELS],
                }
                for s in specs
            ],
        },
    )
    return specs


def oracle_check(specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The oracle (BFS over the game's own ``rules.py``) must win every level in
    exactly ``optimal_actions`` -- otherwise the generator and oracle drifted."""
    bad = []
    for s in specs:
        for li in LEVELS:
            path = bfs(s["levels"][li])
            if (
                path is None
                or len(path) != s["optimal_actions"][li]
                or replay(s["levels"][li], path) != rules.SY_WIN
            ):
                bad.append("%s/L%d" % (s["short_id"], li))
    return {"ok": not bad, "levels": len(specs) * len(LEVELS), "drift": bad}


# ------------------------------------------------------------------ hub wiring
def _colours(ctx: Context) -> Dict[str, Optional[int]]:
    """Colour facts that describe the palette (held OR carried: a binding, not a
    permission -- permission comes from the rule being held)."""
    out: Dict[str, Optional[int]] = {}
    for role in COLOUR_ROLES:
        f = ctx.facts.get("colour:" + role)
        out[role] = int(f.value) if f is not None and f.status in ("held", CARRIED) else None
    return out


async def _audit_neuron(snap: Dict[str, Any], event: Dict[str, Any], hub: ContextHub) -> List[Op]:
    """Wave A contract audit (design section 5): every carried rule is tested on the new
    scope's first frame and replays its stored samples; failures narrow."""
    if event["type"] != "ctx.rescope":
        return []
    ctx = hub.contexts[event["subject"]]
    first = np.asarray(event["data"]["first_frame"], dtype=np.int8)
    world = world_binding(_colours(ctx), [first])
    ops: List[Op] = []
    scope, clock, eid = event["data"]["scope"], event["data"]["clock"], event["data"]["event_id"]
    for key in sorted(
        k for k, f in snap["facts"].items() if k.startswith("rule:") and f["status"] == CARRIED
    ):
        ok, why = audit_contract(ctx.facts[key].value, first, world, key)
        if ok:
            ops.append(
                Op(
                    "infer",
                    "audit:" + key,
                    "pass: " + why,
                    "inferred",
                    (),
                    clock,
                    "contract_audit",
                    eid,
                )
            )
        else:
            ops.append(
                Op(
                    "narrow",
                    key,
                    None,
                    "verified",
                    (("audit", "%s/first: %s" % (scope, why)),),
                    clock,
                    "contract_audit",
                    eid,
                )
            )
    return ops


def _load_contracts(ctx: Context, event: Dict[str, Any], hub: ContextHub) -> None:
    """Hub ``load`` step at a rescope: contracts enter CARRIED (design 4.4)."""
    for c in hub.strata.read_lines(contracts_path(hub.tenant, FAMILY)):
        if c.get("status") != "held":
            continue
        key, old = c["key"], ctx.facts.get(c["key"])
        if old is not None and old.status == CONTRADICTED:
            continue
        value = {"name": key, "source": c["source"], "samples": c.get("samples", [])}
        if old is not None:
            if old.status == CARRIED and isinstance(old.value, dict):
                old.value = dict(
                    old.value, samples=value["samples"] or old.value.get("samples", [])
                )
            ctx._note("contract_loaded", key, FAMILY, ["merged"])
            continue
        ev = tuple(Evidence("replay", r) for r in c.get("evidence", [])[:3]) + (
            Evidence("audit", "contract:" + FAMILY),
        )
        ctx.facts[key] = Fact(
            key, value, Provenance.VERIFIED, (c.get("scopes") or ["?"])[-1], ev, CARRIED, 0, 0.0
        )
        ctx._note("contract_loaded", key, FAMILY, [c.get("fingerprint", "")])


def new_hub(strata: MemoryStrata, flux: InProcessFlux, mode: str = "enforce") -> ContextHub:
    hub = ContextHub(
        strata,
        flux,
        invariants=slice_invariants(),
        policy=None if mode == "off" else dict(ARC_POLICY),
        on_rescope=_load_contracts,
    )
    hub.add_neuron("a", "contract_audit", _audit_neuron, 0.5)
    return hub


# ----------------------------------------------------------------------- agent
def _bfs_path(
    frame: np.ndarray, start: Tuple[int, int], dst: Tuple[int, int], passable: set
) -> Optional[List[int]]:
    h, w = frame.shape
    par: Dict[Tuple[int, int], Tuple[Tuple[int, int], int]] = {start: (start, 0)}
    q = deque([start])
    while q:
        cur = q.popleft()
        if cur == dst:
            out = []
            while cur != start:
                cur, a = par[cur][0], par[cur][1]
                out.append(a)
            return out[::-1]
        for a, (dx, dy) in DIRS.items():
            n = (cur[0] + dx, cur[1] + dy)
            if n in par or not (0 <= n[0] < w and 0 <= n[1] < h):
                continue
            if n != dst and int(frame[n[1], n[0]]) not in passable:
                continue
            par[n] = (cur, a)
            q.append(n)
    return None


class Agent:
    """The acting loop for one game: explore -> perceive -> verify -> plan -> act,
    every context change going Flux -> hub -> record."""

    def __init__(self, hub: ContextHub, spec: Dict[str, Any], mode: str = "enforce") -> None:
        self.hub, self.spec, self.mode = hub, spec, mode
        self.subject = spec["short_id"]
        self.clock = 0
        self.levels: Dict[int, Dict[str, Any]] = {}
        self.status_trace: List[Tuple[str, str]] = []
        self.checks: Dict[str, Any] = {}

    # -- plumbing -----------------------------------------------------------------
    @property
    def ctx(self) -> Context:
        return self.hub.contexts[self.subject]

    def scope(self, level: int) -> str:
        return "%s-s1/%s/level:%d" % (FAMILY, self.subject, level)

    async def publish(self, kind: str, scope: str, source: str, **data: Any) -> Dict[str, Any]:
        ev = make_event(kind, self.subject, scope, self.clock, source, **data)
        self.hub.flux.publish(ev)
        await self.hub.pump()
        if self.subject in self.hub.contexts:
            st = self.ctx.status(RULE_KEY)
            if not self.status_trace or self.status_trace[-1][1] != st:
                self.status_trace.append((kind, st))
        return ev

    def _permits(self, req: Request):
        return permits(self.ctx, req, invariants=self.hub.invariants, policy=self.hub.policy)

    # -- the steps (each a service call in the fleet) --------------------------------
    def explore(
        self, env: PushEnv, scope: str, budget: int = EXPLORE_BUDGET
    ) -> List[Tuple[np.ndarray, int, np.ndarray]]:
        """Experiments that disambiguate colour roles, within ``budget`` actions: move
        until the frame changes (finds the player), then walk into each unknown object
        and back (a box gets pushed, a goal gets stood on -- prefer pushes onto floor),
        then try any action not yet seen to change the frame."""
        first = env.frame()
        trans: List[Tuple[np.ndarray, int, np.ndarray]] = []

        def go(actions: Sequence[int]) -> None:
            for a in actions:
                if len(trans) >= budget or env.status != rules.SY_PLAY:
                    return
                b, af, _ = env.step(a)
                trans.append((b, a, af))

        for a in (1, 2, 3, 4):
            go((a,))
            if trans and (trans[-1][0] != trans[-1][2]).any():
                break
        back = {1: 2, 2: 1, 3: 4, 4: 3}
        tried: set = set()
        for _ in range(8):
            if len(trans) >= budget or env.status != rules.SY_PLAY:
                break
            p = perceive(first, trans, scope)
            pc, fl, wl = p.colour("player"), p.colour("floor"), p.colour("wall")
            if pc is None or fl is None or wl is None:
                break
            if p.colour("box") is not None and p.colour("goal") is not None:
                break
            frame = env.frame()
            me = find_single(frame, pc)
            if me is None:
                break
            known = {pc, fl, wl} | {
                v for v in (p.colour("box"), p.colour("goal"), p.colour("box_on")) if v is not None
            }
            passable = {fl} | ({p.colour("goal")} if p.colour("goal") is not None else set())
            h, w = frame.shape
            objs = [(int(x), int(y)) for y, x in zip(*np.nonzero(~np.isin(frame, list(known))))]
            best: Optional[Tuple[Tuple[int, int], List[int]]] = None
            for o in objs:
                for a, (dx, dy) in DIRS.items():
                    st, far = (o[0] - dx, o[1] - dy), (o[0] + dx, o[1] + dy)
                    if (o, a) in tried or not (0 <= st[0] < w and 0 <= st[1] < h):
                        continue
                    if st != me and int(frame[st[1], st[0]]) not in passable:
                        continue
                    path = [] if st == me else _bfs_path(frame, me, st, passable)
                    if path is None:
                        continue
                    far_c = (
                        int(frame[far[1], far[0]]) if 0 <= far[0] < w and 0 <= far[1] < h else wl
                    )
                    if p.colour("goal") is not None and far_c == p.colour("goal"):
                        continue  # an experiment never pushes onto a goal: it would end the level
                    # far floor: a box moves onto floor (safe, informative); far wall: a
                    # goal is stood on; far unknown: might push a box onto a goal
                    far_rank = 0 if far_c == fl else (1 if far_c == wl else 2)
                    cand = path + [a, back[a]]
                    rank = (far_rank, len(cand))
                    if best is None or rank < best[0]:
                        best = (rank, cand)
                        chosen = (o, a)
            if best is None:
                break
            tried.add(chosen)
            go(best[1])
        seen = {int(a) for b, a, af in trans if (b != af).any()}
        go([a for a in (1, 2, 3, 4) if a not in seen])
        return trans

    async def sense(self, first: np.ndarray, trans, scope: str, clock0: int) -> Dict[str, Any]:
        p = perceive(first, trans, scope, clock0)
        ops = [
            Op(
                "widen",
                k,
                v,
                "verified",
                tuple(("observation", r) for r in p.refs[k][:3]),
                self.clock,
                "sense",
            ).to_json()
            for k, v in sorted(p.facts.items())
        ]
        await self.publish(
            "ctx.fact", scope, "sense:%d" % clock0, ops=ops, level_transitions=len(trans)
        )
        return p.facts

    async def verify_rule(
        self, env: PushEnv, trans, frames, scope: str, clock0: int
    ) -> Dict[str, Any]:
        old = self.ctx.facts.get(RULE_KEY)
        source = (
            old.value["source"] if old is not None and isinstance(old.value, dict) else PUSH_RULE
        )
        world = world_binding(_colours(self.ctx), frames)
        rep = verify(source, RULE_KEY, world, trans, level=env.level, min_support=LEVEL_SUPPORT)
        ref = "%s/t%d..t%d" % (scope, clock0, clock0 + len(trans) - 1)
        if rep["status"] == "verified":
            samples = [
                {
                    "ref": "%s/t%d" % (scope, clock0 + i),
                    "before": b.tolist(),
                    "action": int(a),
                    "after": af.tolist(),
                    "world": world,
                }
                for i, (b, a, af) in enumerate(trans)
                if int((b != af).sum()) > 2
            ][:1]
            samples = samples or ([] if old is None else list(old.value.get("samples", [])))
            op = Op(
                "widen",
                RULE_KEY,
                {"name": RULE_KEY, "source": source, "samples": samples},
                "verified",
                (("replay", ref),),
                self.clock,
                "worldmodel",
            )
            await self.publish("ctx.fact", scope, "worldmodel:%d" % clock0, ops=[op.to_json()])
        else:
            op = Op(
                "narrow", RULE_KEY, None, "verified", (("replay", ref),), self.clock, "worldmodel"
            )
            await self.publish(
                "ctx.contradiction", scope, "worldmodel:%d" % clock0, ops=[op.to_json()]
            )
        # the goal: consistent in this scope = not already satisfied on the first frame
        # and no false positive on any frame of this scope
        g = compile_rule(PUSH_GOAL, GOAL_KEY)
        vals = [g(f, world) for f in frames]
        if vals and all(v is False for v in vals):
            gop = Op(
                "widen",
                GOAL_KEY,
                {"source": PUSH_GOAL},
                "verified",
                (("audit", "%s/frames:%d" % (scope, len(frames))),),
                self.clock,
                "worldmodel",
            )
            await self.publish("ctx.fact", scope, "goal:%d" % clock0, ops=[gop.to_json()])
        rep["world"] = world
        return rep

    async def play_level(self, level: int) -> Dict[str, Any]:
        env = PushEnv(self.spec, level)
        scope = self.scope(level)
        first = env.frame()
        out: Dict[str, Any] = {
            "level": level,
            "scope": scope,
            "optimal": env.optimal,
            "baseline": env.baseline,
        }
        await self.publish("ctx.rescope", scope, "env", first_frame=first.tolist())
        out["after_rescope"] = {
            k: self.ctx.status(k) for k in sorted(self.ctx.facts) if k.startswith("rule:")
        }
        carried_only = self.mode == "off" and self.ctx.status(RULE_KEY) == CARRIED
        trans: List[Tuple[np.ndarray, int, np.ndarray]] = []
        if not carried_only:
            clock0 = self.clock
            trans = self.explore(env, scope)
            self.clock += len(trans)
            out["explore_actions"] = len(trans)
            out["first_frame"], out["explore"] = first, trans
            out["perceived"] = await self.sense(first, trans, scope, clock0)
            out["pre_verify_plan"] = self._permits(
                Request("plan", args={"goal_name": GOAL_KEY[5:]})
            )
            out["pre_verify_route"] = self._permits(Request("model_route", name="cloud-sonnet"))
            frames = [first] + [af for _b, _a, af in trans]
            out["verify"] = await self.verify_rule(env, trans, frames, scope, clock0)
        else:
            out["explore_actions"] = 0
        if env.won:
            out.update(solved=True, play_actions=0, misses=0, plan=[])
            return self._finish(out, env)
        start = env.reset()
        frames = [first] + [af for _b, _a, af in trans]
        world = world_binding(_colours(self.ctx), frames)
        out["world"] = world
        decision, path, nodes = plan(
            self.ctx,
            start,
            RULE_KEY,
            GOAL_KEY,
            world,
            invariants=self.hub.invariants,
            policy=self.hub.policy,
        )
        out.update(
            plan_decision=decision,
            plan=path,
            plan_nodes=nodes,
            carried_only_plan=carried_only and path is not None,
        )
        if path is None:
            out.update(solved=False, play_actions=0, misses=0)
            return self._finish(out, env)
        predict = bind(compile_rule(self.ctx.facts[RULE_KEY].value["source"], RULE_KEY), world)
        misses = hits = 0
        acted: List[Tuple[np.ndarray, int, np.ndarray]] = []
        clock0 = self.clock
        for a in path:
            if env.status != rules.SY_PLAY:
                break
            expect = predict(env.frame(), a)
            b, af, st = env.step(a)
            acted.append((b, a, af))
            verdict, why = score_prediction(expect, b, af)
            self.clock += 1
            if verdict == "wrong":
                misses += 1
                op = Op(
                    "narrow",
                    RULE_KEY,
                    None,
                    "verified",
                    (("observation", "%s/t%d: %s" % (scope, self.clock, why)),),
                    self.clock,
                    "act",
                )
                await self.publish(
                    "ctx.contradiction", scope, "act:%d" % self.clock, ops=[op.to_json()]
                )
                break
            hits += verdict == "ok"
        await self.publish("ctx.outcome", scope, "act:%d" % self.clock, hits=hits, misses=misses)
        if acted:  # the win frame teaches box_on; perception runs on every transition
            await self.sense(first, trans + acted, scope, clock0 - len(trans))
        out.update(solved=env.won, play_actions=len(acted), misses=misses, hits=hits)
        return self._finish(out, env)

    def _finish(self, out: Dict[str, Any], env: PushEnv) -> Dict[str, Any]:
        out["total_actions"] = env.actions
        out["record_hash"] = record_hash(self.ctx)
        self.levels[out["level"]] = out
        return out


# --------------------------------------------------------------------- H8
async def score_config(mode: str, specs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """One config over the suite: the graduation vector of design section 8.3."""
    strata, flux = MemoryStrata(), InProcessFlux()
    hub = new_hub(strata, flux, mode)
    solved = within = opt = actual = hits = made = carried_plans = 0
    per = []
    for s in specs:
        ag = Agent(hub, s, mode)
        for li in LEVELS:
            r = await ag.play_level(li)
            solved += bool(r["solved"])
            within += bool(r["solved"]) and r["play_actions"] <= r["baseline"]
            opt += r["optimal"]
            actual += r["total_actions"]
            hits += r.get("hits", 0)
            made += r.get("hits", 0) + r.get("misses", 0)
            carried_plans += bool(r.get("carried_only_plan"))
            per.append(
                [s["short_id"], li, bool(r["solved"]), r["explore_actions"], r["play_actions"]]
            )
    return {
        "config": {"context": mode},
        "suite_seed": SUITE_SEED,
        "family": FAMILY,
        "levels": len(per),
        "solved": solved,
        "solved_within_baseline": within,
        "efficiency": round(opt / float(actual), 6) if actual else 0.0,
        "calibration": round(hits / float(made), 6) if made else 0.0,
        "carried_only_plans": carried_plans,
        "neuron_failures": len(hub.neuron_failures),
        "per_level": per,
    }


# ------------------------------------------------------------------ the slice
async def _run(strata: MemoryStrata, flux: InProcessFlux) -> Dict[str, Dict[str, Any]]:
    hops: Dict[str, Dict[str, Any]] = {}
    specs = generate_suite(strata)
    hops["H0"] = oracle_check(specs)
    hops["H0"]["stored"] = strata.read_json(suite_path("manifest.json")) is not None
    hops["H0"]["ok"] = hops["H0"]["ok"] and hops["H0"]["stored"]

    hub = new_hub(strata, flux)
    g0, g1 = specs[0], specs[1]
    a0 = Agent(hub, g0)

    # H1: the rescope for level 0 is delivered once, acked by event_id, within 2 s
    env0 = PushEnv(g0, 0)
    ev = await a0.publish("ctx.rescope", a0.scope(0), "env", first_frame=env0.frame().tolist())
    eid = ev["data"]["event_id"]
    h_before = record_hash(a0.ctx)
    flux.publish(ev)  # an at-least-once redelivery of the same event
    await hub.pump()
    acked = [(e, s) for e, s in hub.acks if e == eid]
    hops["H1"] = {
        "event_id": eid,
        "acks": len(acked),
        "ack_s": max((s for _e, s in acked), default=None),
        "applied_once": sum(1 for e in hub.applied if e["data"]["event_id"] == eid) == 1,
        "dedup_unchanged": record_hash(a0.ctx) == h_before,
        "pending": flux.pending("flux:events:ctx", hub.group),
    }
    hops["H1"]["ok"] = (
        len(acked) == 2
        and all(s is not None and s < 2.0 for _e, s in acked)
        and hops["H1"]["applied_once"]
        and hops["H1"]["dedup_unchanged"]
        and not hops["H1"]["pending"]
    )
    # the agent's own level-0 run re-announces the scope (a new clock -> a new event)
    a0.clock += 1
    r0 = await a0.play_level(0)

    # H2: every fact perceived on the first 10 transitions matches the embedded GAME
    # spec (precision 1.0); the whole exploration must also recover every role the
    # later hops need (recall, else H4-H6 would fail for the wrong reason)
    truth = {"colour:" + r: g0["colors"][ROLE_OF[r]] for r in ROLE_OF}
    truth.update({"action:%d:effective" % a: True for a in g0["actions"]})
    window = perceive(r0["first_frame"], r0["explore"][:H2_WINDOW], a0.scope(0)).facts
    got = r0.get("perceived") or {}
    wrong = sorted(k for k, v in list(window.items()) + list(got.items()) if truth.get(k) != v)
    need = ["colour:player", "colour:wall", "colour:box", "colour:goal"] + [
        "action:%d:effective" % a for a in (1, 2, 3, 4)
    ]
    hops["H2"] = {
        "window_facts": sorted(window),
        "emitted": len(got),
        "wrong": wrong,
        "missing": [k for k in need if k not in got],
        "precision": (len(window) - len([k for k in window if truth.get(k) != window[k]]))
        / float(len(window))
        if window
        else 0.0,
        "transitions": r0["explore_actions"],
    }
    hops["H2"]["ok"] = (
        bool(window) and "colour:player" in window and not wrong and not hops["H2"]["missing"]
    )

    # H3: deterministic record; plan refused by context before a rule is verified;
    # a cloud route refused by the invariant layer
    hashes = []
    for _ in range(2):
        h2 = new_hub(MemoryStrata(), InProcessFlux())
        for e in hub.applied:
            if e["subject"] == a0.subject:
                await h2.handle(e)
        hashes.append(record_hash(h2.contexts[a0.subject]))
    pv, pr = r0["pre_verify_plan"], r0["pre_verify_route"]
    hops["H3"] = {
        "replay_hashes": hashes,
        "live_hash": record_hash(a0.ctx),
        "plan_before_verify": [pv.allowed, pv.layer, pv.rule],
        "cloud_route": [pr.allowed, pr.layer, pr.rule],
        "record_saved": strata.read(
            "aither://warm/context/%s/%s/record.json" % (hub.tenant, a0.subject)
        )
        is not None,
    }
    hops["H3"]["ok"] = (
        hashes[0] == hashes[1] == hops["H3"]["live_hash"]
        and not pv.allowed
        and pv.layer == "context"
        and not pr.allowed
        and pr.layer == "invariant"
        and pr.rule == "local_only"
        and hops["H3"]["record_saved"]
    )

    # H4: the rule is verified on this level and exact on 200 held-out pairs
    vr = r0["verify"]
    ho = heldout_accuracy(PUSH_RULE, RULE_KEY, r0["world"], g0["levels"][0], g0["colors"], n=200)
    hops["H4"] = {
        "status": vr["status"],
        "cell_claims": vr.get("cell_claims"),
        "wrong_in_history": vr.get("wrong"),
        "heldout": ho,
    }
    hops["H4"]["ok"] = (
        vr["status"] == "verified"
        and vr.get("cell_claims", 0) >= LEVEL_SUPPORT
        and vr.get("wrong", 1) == 0
        and ho["wrong"] == 0
        and ho["ok"] + ho["abstain"] == 200
        and ho["ok"] >= 150
    )

    # H5: plan permitted now; it wins in the TRUE rules within baseline
    pd, path = r0.get("plan_decision"), r0.get("plan") or []
    true_status = replay(g0["levels"][0], [(a, None) for a in path])
    hops["H5"] = {
        "permits_plan": None if pd is None else [pd.allowed, pd.layer, pd.rule],
        "plan_len": len(path),
        "baseline": r0["baseline"],
        "optimal": r0["optimal"],
        "true_rules_win": true_status == rules.SY_WIN,
        "nodes": r0.get("plan_nodes"),
    }
    hops["H5"]["ok"] = (
        bool(pd and pd.allowed) and true_status == rules.SY_WIN and len(path) <= r0["baseline"]
    )

    # H6: executing the plan completes the level with zero prediction misses
    hops["H6"] = {
        "solved": r0["solved"],
        "misses": r0["misses"],
        "hits": r0.get("hits"),
        "play_actions": r0["play_actions"],
        "explore_actions": r0["explore_actions"],
        "baseline": r0["baseline"],
    }
    hops["H6"]["ok"] = r0["solved"] and r0["misses"] == 0 and r0["play_actions"] <= r0["baseline"]

    # H7: consolidate (held in two scopes) -> contract; level 1 loads it carried, audits
    # it, holds it only after replay there; a planted false contract is narrowed + pruned
    a1 = Agent(hub, g1)
    r1 = await a1.play_level(0)
    cons = consolidate_contracts(strata, hub.tenant, FAMILY)
    contracts = {c["key"]: c for c in strata.read_lines(contracts_path(hub.tenant, FAMILY))}
    rule_c = contracts.get(RULE_KEY) or {}
    planted_src = PUSH_RULE.replace(
        "            out[by, bx] = B if B is not None else -1", "            return out"
    )
    assert planted_src != PUSH_RULE
    strata.append_line(
        contracts_path(hub.tenant, FAMILY),
        {
            "key": "rule:push_box_sticks",
            "domain": FAMILY,
            "status": "held",
            "provenance": "verified",
            "fingerprint": "planted",
            "source": planted_src,
            "samples": rule_c.get("samples", []),
            "scopes": ["planted"],
            "evidence": ["planted"],
        },
    )
    a0.status_trace = []
    r01 = await a0.play_level(1)
    hub.save(a0.subject)
    cons2 = consolidate_contracts(strata, hub.tenant, FAMILY)
    after = {c["key"] for c in strata.read_lines(contracts_path(hub.tenant, FAMILY))}
    refuted = strata.read_lines(refuted_path(hub.tenant, FAMILY))
    trace = [s for _k, s in a0.status_trace]
    hops["H7"] = {
        "first_consolidation": cons,
        "contract_scopes": rule_c.get("scopes"),
        "contract_evidence": rule_c.get("evidence"),
        "g1_level0_solved": r1["solved"],
        "level1_after_rescope": r01["after_rescope"],
        "rule_status_trace": trace,
        "planted_status": a0.ctx.status("rule:push_box_sticks"),
        "second_consolidation": cons2,
        "refuted": [r["key"] for r in refuted],
        "level1_solved": r01["solved"],
        "level1_misses": r01["misses"],
    }
    hops["H7"]["ok"] = (
        RULE_KEY in cons["promoted"]
        and len(rule_c.get("scopes") or []) >= 2
        and bool(rule_c.get("evidence"))
        and r01["after_rescope"].get(RULE_KEY) == CARRIED
        and trace[:2] == [CARRIED, "held"]
        and CONTRADICTED not in trace
        and a0.ctx.status("rule:push_box_sticks") == CONTRADICTED
        and "rule:push_box_sticks" in cons2["pruned"]
        and "rule:push_box_sticks" not in after
        and "rule:push_box_sticks" in [r["key"] for r in refuted]
        and RULE_KEY in after
        and r01["solved"]
        and r01["misses"] == 0
    )

    # H8: two configs scored over the 5 games, stored with the seed, rerun identical;
    # a report with no measured evidence is refused by the invariant layer
    scores = {m: await score_config(m, specs) for m in ("off", "enforce")}
    rerun = {m: await score_config(m, specs) for m in ("off", "enforce")}
    paths = {}
    for m, sc in scores.items():
        paths[m] = "aither://warm/curriculum/%s/%s/context-%s.json" % (FAMILY, SUITE_SEED, m)
        strata.write_json(paths[m], sc)
    bare = permits(
        a0.ctx, Request("report", name="curriculum"), invariants=hub.invariants, policy=hub.policy
    )
    cited = permits(
        a0.ctx,
        Request("report", name="curriculum", evidence=(Evidence("test", paths["enforce"]),)),
        invariants=hub.invariants,
        policy=hub.policy,
    )
    hops["H8"] = {
        "scores": {
            m: {k: v for k, v in sc.items() if k != "per_level"} for m, sc in scores.items()
        },
        "stored": sorted(paths.values()),
        "rerun_identical": canonical_json(scores) == canonical_json(rerun),
        "bare_report": [bare.allowed, bare.layer, bare.rule],
        "cited_report": [cited.allowed, cited.layer],
    }
    hops["H8"]["ok"] = (
        hops["H8"]["rerun_identical"]
        and all(strata.read_json(p)["suite_seed"] == SUITE_SEED for p in paths.values())
        and not bare.allowed
        and bare.layer == "invariant"
        and bare.rule == "no_fabrication"
        and cited.allowed
    )
    return hops


def run_slice() -> Dict[str, Dict[str, Any]]:
    return asyncio.run(_run(MemoryStrata(), InProcessFlux()))


def _jsonable(o: Any) -> Any:
    if hasattr(o, "allowed"):
        return [o.allowed, o.layer, o.rule]
    if hasattr(o, "tolist"):
        return o.tolist()
    return str(o)


def main(argv: Optional[List[str]] = None) -> int:
    hops = run_slice()
    for hop in sorted(hops):
        print("%s %s" % (hop, "PASS" if hops[hop]["ok"] else "FAIL"))
    if argv and "--json" in argv:
        print(json.dumps(hops, indent=1, sort_keys=True, default=_jsonable))
    return 0 if all(h["ok"] for h in hops.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
