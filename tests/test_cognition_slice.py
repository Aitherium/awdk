"""The first vertical slice of the cognition frameworks design (section 9), in process.

One test per hop asserts that hop's check; the ``*_can_fail`` tests prove each check
still fails on the defect it exists to catch. No fleet, no network: Flux and Strata are
the in-process stand-ins in ``adk.cognition.bus``.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from adk.cognition import slice as sl  # noqa: E402
from adk.cognition.bus import InProcessFlux, MemoryStrata, make_event  # noqa: E402
from adk.cognition.classroom import _provenance  # noqa: E402
from adk.cognition.classroom._vendor.generate import game_spec  # noqa: E402
from adk.cognition.classroom.env import PushEnv  # noqa: E402
from adk.cognition.hub import ContextHub, Op, merge, record_hash  # noqa: E402
from adk.cognition.perceive import perceive  # noqa: E402
from adk.cognition.slumber import consolidate_contracts  # noqa: E402
from adk.cognition.worldmodel import PUSH_RULE, heldout_accuracy  # noqa: E402
from adk.reasoning.solve.context import Context, Evidence, Request, permits  # noqa: E402

HOPS = ["H0", "H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8"]


@pytest.fixture(scope="module")
def hops():
    return sl.run_slice()


@pytest.mark.parametrize("hop", HOPS)
def test_hop_is_green(hops, hop):
    assert hops[hop]["ok"], {k: v for k, v in hops[hop].items() if k != "scores"}


def test_slice_numbers_are_the_measured_ones(hops):
    # H4: exact on every held-out claim; H6: at the optimum; H8: enforce never plans
    # on a carried-only rule, off does
    assert hops["H4"]["heldout"]["wrong"] == 0
    assert hops["H6"]["play_actions"] == hops["H5"]["optimal"]
    assert hops["H8"]["scores"]["enforce"]["carried_only_plans"] == 0
    assert hops["H8"]["scores"]["off"]["carried_only_plans"] > 0
    assert hops["H7"]["rule_status_trace"][:2] == ["carried", "held"]


# ---------------------------------------------------------------- provenance
def _lf_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def test_vendored_classroom_is_pinned():
    here = Path(_provenance.__file__).parent / "_vendor"
    for name, pin in _provenance.PINNED.items():
        assert _lf_sha(here / name) == pin["vendored_sha256"], name
    up = os.environ.get("ADK_SYNTH_DIR")
    if up:
        for name, pin in _provenance.PINNED.items():
            assert _lf_sha(Path(up) / name) == pin["upstream_sha256"], name


# ---------------------------------------------------------- checks can fail
def test_h0_oracle_check_catches_drift():
    specs = [game_spec(0, sl.SUITE_SEED, sl.FAMILY)]
    assert sl.oracle_check(specs)["ok"]
    bad = copy.deepcopy(specs)
    bad[0]["optimal_actions"][0] += 1
    rep = sl.oracle_check(bad)
    assert not rep["ok"] and rep["drift"] == ["p000/L0"]


def test_h1_event_without_id_is_refused_and_redelivery_is_deduplicated():
    flux = InProcessFlux()
    with pytest.raises(ValueError):
        flux.publish({"type": "ctx.rescope", "subject": "x", "data": {}})
    hub = ContextHub(MemoryStrata(), flux)
    ev = make_event("ctx.rescope", "p9", "push-s1/p9/level:0", 0, "env")
    flux.publish(ev)
    # delivered but never acked (a consumer crash) -> redelivered, applied once
    flux.read_group("flux:events:ctx", hub.group)
    asyncio.run(hub.pump(redeliver=True))
    flux.publish(ev)
    asyncio.run(hub.pump())
    assert sum(e["data"]["event_id"] == ev["data"]["event_id"] for e in hub.applied) == 1
    assert hub.duplicates == [ev["data"]["event_id"]]
    assert flux.pending("flux:events:ctx", hub.group) == []


def test_h2_perception_is_sound_over_the_whole_suite():
    """Precision 1.0 on every level of the suite, not only the slice's level."""
    wrong = []
    for i in range(sl.N_GAMES):
        spec = game_spec(i, sl.SUITE_SEED, sl.FAMILY)
        truth = {"colour:" + r: spec["colors"][sl.ROLE_OF[r]] for r in sl.ROLE_OF}
        truth.update({"action:%d:effective" % a: True for a in spec["actions"]})
        for li in sl.LEVELS:
            env = PushEnv(spec, li)
            first = env.frame()
            hub = sl.new_hub(MemoryStrata(), InProcessFlux())
            trans = sl.Agent(hub, spec).explore(env, "s")
            facts = perceive(first, trans, "s").facts
            wrong += [
                "%s/L%d %s" % (spec["short_id"], li, k)
                for k, v in facts.items()
                if truth.get(k) != v
            ]
    assert wrong == []


def test_h2_perception_emits_nothing_it_cannot_derive():
    spec = game_spec(0, sl.SUITE_SEED, sl.FAMILY)
    env = PushEnv(spec, 0)
    first = env.frame()
    assert perceive(first, [], "s").facts.keys() <= {"colour:wall", "colour:floor"}


def test_h3_narrow_beats_widen_in_one_wave_and_hash_ignores_wall_clock():
    ctx = Context("s")
    ops = [
        Op("widen", "k", 1, "verified", (("replay", "t1"),), 5, "wm", "e1"),
        Op("narrow", "k", None, "verified", (("observation", "t2"),), 5, "act", "e2"),
    ]
    rep = merge(ctx, ops)
    assert rep["superseded"] == ["k"] and ctx.status("k") == "contradicted"
    assert merge(ctx, [Op("widen", "j", 1, "verified", (), 0, "x", "e3")])["refused"]
    a, b = Context("s"), Context("s")
    for c in (a, b):
        merge(c, [Op("widen", "k", 1, "verified", (("replay", "t1"),), 1, "wm", "e1")])
    b.facts["k"].at += 1000.0
    assert record_hash(a) == record_hash(b)


def test_h4_a_rule_that_fits_history_but_is_wrong_fails_heldout():
    spec = game_spec(0, sl.SUITE_SEED, sl.FAMILY)
    bad = PUSH_RULE.replace(
        "if not (0 <= bx < w and 0 <= by < h) or int(frame[by, bx]) in [W] + boxes:",
        "if not (0 <= bx < w and 0 <= by < h):",
    )
    assert bad != PUSH_RULE
    world = {
        "colours": {
            r: spec["colors"][sl.ROLE_OF[r]]
            for r in ("wall", "floor", "player", "box", "goal", "box_on")
        },
        "targets": [list(t) for t in spec["levels"][0]["targets"]],
    }
    good = heldout_accuracy(PUSH_RULE, "good", world, spec["levels"][0], spec["colors"], n=200)
    assert good["wrong"] == 0 and good["ok"] == 200
    wrong = heldout_accuracy(bad, "bad", world, spec["levels"][0], spec["colors"], n=200)
    assert wrong["wrong"] > 0


def test_h7_one_scope_is_not_enough_to_promote():
    strata = MemoryStrata()
    hub = sl.new_hub(strata, InProcessFlux())
    spec = game_spec(0, sl.SUITE_SEED, sl.FAMILY)
    r = asyncio.run(sl.Agent(hub, spec).play_level(0))
    assert r["solved"]
    rep = consolidate_contracts(strata, hub.tenant, sl.FAMILY)
    assert rep["promoted"] == []
    assert consolidate_contracts(strata, hub.tenant, sl.FAMILY, min_scopes=1)["promoted"] == [
        sl.RULE_KEY
    ]


def test_h8_reports_need_measured_evidence():
    ctx, inv = Context("s"), sl.slice_invariants()
    model_said = permits(
        ctx, Request("report", evidence=(Evidence("model_text", "llm"),)), invariants=inv
    )
    assert not model_said.allowed and model_said.rule == "no_fabrication"
    assert permits(
        ctx, Request("report", evidence=(Evidence("test", "x"),)), invariants=inv
    ).allowed
