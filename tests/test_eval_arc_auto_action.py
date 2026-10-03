"""ArcAgi3Environment.auto_action / candidates: the loop's explorer fallback on ARC.

Before these hooks the reasoning loop's auto() took zero actions on ARC (it stops
at the first None), so turns whose code only wrote hypotheses never moved.
"""
from __future__ import annotations

import numpy as np
import pytest

from adk.evalharness.arc_agi3 import env_arc
from adk.evalharness.arc_agi3.env_arc import ArcAgi3Environment, _component_centres


class _Raw:
    def __init__(self, acts):
        self.available_actions = acts


def _env(frame, acts):
    e = ArcAgi3Environment.__new__(ArcAgi3Environment)
    e._frame = frame
    e._raw = _Raw(acts)
    e.hud = lambda: None
    e.state_key = lambda s: "k%d" % int(np.asarray(s).sum())
    return e


def test_component_centres_skip_background_and_land_on_the_component():
    f = np.zeros((8, 8), dtype=int)
    f[1:3, 1:3] = 5          # 4 cells
    f[5, 5:8] = 7            # 3 cells
    got = _component_centres(f, None, 10)
    assert len(got) == 2
    x, y = got[0]            # largest first
    assert f[y, x] == 5
    x, y = got[1]
    assert f[y, x] == 7


def test_component_centres_respect_the_hud_mask_and_limit():
    f = np.zeros((4, 4), dtype=int)
    f[0, 0] = 3
    f[3, 3] = 4
    mask = np.zeros((4, 4), dtype=bool)
    mask[0, 0] = True
    assert _component_centres(f, mask, 10) == [(3, 3)]
    assert _component_centres(f, None, 1) and len(_component_centres(f, None, 1)) == 1


def test_auto_action_tries_every_candidate_before_repeating():
    f = np.zeros((6, 6), dtype=int)
    f[2, 2] = 9
    e = _env(f, [0, 1, 2, 6])
    cands = e.candidates()
    assert (0, -1, -1) not in cands, "RESET is never an exploration move"
    assert cands[:2] == [(1, -1, -1), (2, -1, -1)]
    assert (6, 2, 2) in cands
    picks = [e.auto_action() for _ in range(len(cands))]
    assert sorted(picks) == sorted(cands), "each candidate once before any repeat"


def test_auto_action_is_none_without_candidates():
    e = _env(np.zeros((4, 4), dtype=int), [])
    assert e.auto_action() is None


def test_live_ls20_auto_action_moves_the_engine():
    try:
        env_arc.require_arc()
        e = ArcAgi3Environment("ls20", seed=0)
    except Exception as exc:  # noqa: BLE001 - engine or games absent on this box
        pytest.skip("ARC engine/games unavailable: %s" % exc)
    before = e.actions
    for _ in range(5):
        a = e.auto_action()
        assert a is not None
        e.act(a, source="explore")
    assert e.actions == before + 5
