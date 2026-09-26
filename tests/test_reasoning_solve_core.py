"""h30 core: game-agnostic by construction (AST import scan), the intent
classifier, PRISM rotation on a stall, Six Pillars prediction learning, and
the SASE loop on a toy environment that is not ARC.

Parity port of h30 ``agent/tests/test_h30_core.py`` at c076671233: the same tests, run
against the vendored core (only the import paths changed).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, List

import pytest

np = pytest.importorskip("numpy")

from adk.reasoning.solve._vendor.intent import (  # noqa: E402
    CONTRADICTION,
    EXPLORING,
    NEAR_GOAL,
    NEW_LEVEL,
    STUCK,
    IntentClassifier,
    Signals,
)
from adk.reasoning.solve._vendor.interfaces import (  # noqa: E402
    ChatReply,
    FileMemoryBackend,
    InMemoryBackend,
    Obs,
)
from adk.reasoning.solve._vendor.learning import PredictionLearner  # noqa: E402
from adk.reasoning.solve._vendor.loop import LoopConfig, ReasoningLoop  # noqa: E402
from adk.reasoning.solve._vendor.memory import Episodic, HypothesisStore  # noqa: E402
from adk.reasoning.solve._vendor.prism import Prism, diagnose  # noqa: E402
from adk.reasoning.solve._vendor.sase import parse_reply  # noqa: E402

CORE = Path(__file__).resolve().parents[1] / "adk" / "reasoning" / "solve" / "_vendor"


# ---------------------------------------------------------------- game-agnostic
def test_core_imports_nothing_from_arc_or_the_explorer():
    allowed_roots = {
        "__future__",
        "ast",
        "builtins",
        "contextlib",
        "dataclasses",
        "hashlib",
        "json",
        "logging",  # the SEAM(adk) logged swallows in loop.py / evidence.py
        "os",
        "re",
        "sys",
        "threading",
        "time",
        "traceback",
        "typing",
        "collections",
        "numpy",
        "math",
        "heapq",
        "itertools",
        "queue",
        "random",
        "functools",
    }
    files = sorted(CORE.glob("*.py"))
    assert len(files) >= 8
    bad = []
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] not in allowed_roots:
                        bad.append("%s: import %s" % (f.name, a.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or "").split(".")[0] not in allowed_roots:
                    bad.append("%s: from %s" % (f.name, node.module))
                if node.level > 1:  # ``from ..x`` leaves the core package
                    bad.append("%s: from %s%s" % (f.name, "." * node.level, node.module or ""))
    assert bad == [], bad


# ---------------------------------------------------------------- intent
def test_intent_classifier_priorities_and_budgets():
    ic = IntentClassifier(stuck_actions=30, stuck_turns=3)
    assert ic.classify(Signals(first_turn=True)).intent == NEW_LEVEL
    assert ic.classify(Signals(levels_gained=1, refuted_since_last=["r"])).intent == NEW_LEVEL
    d = ic.classify(Signals(refuted_since_last=["move_rule"], actions_since_novel=99))
    assert d.intent == CONTRADICTION and "move_rule" in d.reason and d.turn_actions <= 6
    assert ic.classify(Signals(actions_since_novel=30)).intent == STUCK
    assert ic.classify(Signals(turns_without_progress=3)).intent == STUCK
    assert ic.classify(Signals(goal_progress=0.6)).intent == NEAR_GOAL
    assert ic.classify(Signals(goal_progress=1.0)).intent == EXPLORING  # satisfied is not "near"
    assert ic.classify(Signals()).intent == EXPLORING
    assert ic.counts[STUCK] == 2


# ---------------------------------------------------------------- PRISM
def test_prism_rotates_on_stall_and_never_repeats_within_an_episode():
    p = Prism(InMemoryBackend())
    first = p.active.id
    assert p.active.kind == "llm"
    seen = [first]
    for _ in range(4):
        s = p.rotate(
            diagnose(p.active.id, actions=20, novel=0, verified=0, refuted=0, errors=0, turns=2)
        )
        assert s.id not in seen
        seen.append(s.id)
    assert len(set(seen)) == 5 and p.rotations == 4
    assert "no new states reached" in p.diagnoses[-1].gaps
    # all exhausted -> a new cycle, never the strategy that just stalled
    nxt = p.rotate(diagnose(p.active.id, 5, 0, 0, 0, 0, 1))
    assert nxt.id != seen[-1]
    overlay = p.overlay()
    assert "STRATEGY" in overlay and "do NOT repeat" in overlay


def test_prism_scores_persist_so_a_later_game_starts_with_the_best(tmp_path):
    be = FileMemoryBackend(str(tmp_path))
    p = Prism(be)
    p.record("goal_first", actions=40, levels=2, verified=1)
    p.record("rule_first", actions=100, levels=0, verified=1)
    p.save()
    later = Prism(FileMemoryBackend(str(tmp_path)))
    assert later.active.id == "goal_first"
    assert later.score("goal_first") > later.score("rule_first")
    later.record("goal_first", actions=10, levels=1, verified=0)
    later.save()
    data = json.loads((tmp_path / "procedural.json").read_text(encoding="utf-8"))
    assert (
        data["prism"]["goal_first"]["levels"] == 3 and data["prism"]["goal_first"]["actions"] == 50
    )


# ---------------------------------------------------------------- Six Pillars learning
def _dot(r: int, c: int) -> np.ndarray:
    f = np.zeros((8, 8), np.int8)
    f[r, c] = 3
    return f


def test_prediction_mismatch_is_logged_as_refuted_evidence_and_a_match_raises_confidence():
    hs = HypothesisStore(min_support=1)
    ep = Episodic()
    ep.add(0, (4, -1, -1), _dot(2, 2), _dot(2, 3))

    def right(frame, action):
        if action[0] != 4:
            return None
        ys, xs = np.nonzero(frame == 3)
        out = np.zeros_like(frame)
        out[ys[0], xs[0] + 1] = 3
        return out

    assert hs.propose("right", right, "", "predict", ep)["status"] == "verified"
    lr = PredictionLearner(hs)
    # a match: the verified rule and an inline claim both hold
    preds = lr.before(_dot(2, 3), (4, -1, -1), expect={"changed": True, "cells": {(2, 4): 3}})
    assert {p.source for p in preds} == {"inline", "right"}
    lr.after(preds, _dot(2, 3), _dot(2, 4), False, 1, (4, -1, -1), turn=1, level=0)
    assert hs.active["right"].confidence == 1 and lr.ledger.hits == 2
    # a wall: nothing moves -> both predictions miss
    preds = lr.before(_dot(2, 5), (4, -1, -1), expect={"changed": True})
    outs = lr.after(preds, _dot(2, 5), _dot(2, 5), False, 2, (4, -1, -1), turn=2, level=0)
    assert [o.ok for o in outs] == [False, False]
    assert "right" not in hs.active
    names = [h.name for h in hs.refuted]
    assert "right" in names and "claim@t2" in names
    claim = next(h for h in hs.refuted if h.name == "claim@t2")
    assert "predicted changed=True, got False" in claim.reason and "t#2" in claim.reason
    assert "prediction missed" in next(h for h in hs.refuted if h.name == "right").reason
    assert lr.stats()["calibration"] == 0.5
    assert "REFUTED (do not retry) claim@t2" in hs.digest()


def test_sase_reply_parsing_assigns_blocks_to_phases():
    reply = (
        "SITUATION: a dot at (2,2)\nANALYSIS: none\nSYNTHESIS:\n"
        "```python\ndef p(f, a):\n    return None\n"
        "```\n**EXECUTION:**\n```python\nact(1)\n```"
    )
    r = parse_reply(reply)
    assert r.found == ["SITUATION", "ANALYSIS", "SYNTHESIS", "EXECUTION"]
    assert [ph for ph, _c in r.blocks] == ["SYNTHESIS", "EXECUTION"]


# ---------------------------------------------------------------- the loop on a non-ARC environment
class LineWorld:
    """A dot on a 1x10 line; action 1 = left, 2 = right, 3 = nothing.
    Reaching cell 9 wins the level; two levels."""

    def __init__(self) -> None:
        self.pos, self.level, self.over = 0, 0, False
        self.autos: List[Any] = []

    def _state(self) -> np.ndarray:
        s = np.zeros((1, 10), np.int8)
        s[0, self.pos] = 1
        return s

    def observe(self) -> Obs:
        return Obs(self._state(), level=self.level)

    def act(self, action, source="model") -> Obs:
        a = action[0]
        self.pos = max(0, min(9, self.pos + (-1 if a == 1 else 1 if a == 2 else 0)))
        if self.pos == 9:
            win = self._state()
            self.level += 1
            self.pos = 0
            self.over = self.level >= 2
            return Obs(
                self._state(), level=self.level, level_up=True, done=self.over, win_state=win
            )
        return Obs(self._state(), level=self.level)

    def available_actions(self):
        return [1, 2, 3]

    def done(self) -> bool:
        return self.over

    def auto_action(self):
        self.autos.append(1)
        return (2, -1, -1)  # a "policy" that walks right


class ScriptLLM:
    def __init__(self, replies: List[str]) -> None:
        self.replies, self.n = replies, 0

    def chat(self, messages, max_tokens=1000, temperature=0.4, extra=None):
        r = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        self.last_user = messages[-1]["content"]
        return ChatReply(r, {"prompt_tokens": 10, "completion_tokens": 5})


STALL = (
    "SITUATION: nothing\nANALYSIS: none\nSYNTHESIS: none\nEXECUTION:\n```python\n"
    "for _ in range(3):\n    act(3, expect={'changed': False})\n```"
)


def test_sase_loop_rotates_to_the_handoff_when_stuck_and_learns_from_predictions():
    env = LineWorld()
    cfg = LoopConfig(
        mode="sase", prism=True, max_calls=6, stuck_actions=5, stuck_turns=2, turn_s=5.0
    )
    llm = ScriptLLM([STALL])
    loop = ReasoningLoop(env, llm, cfg, backend=InMemoryBackend(), episode_id="line")
    loop.run()
    s = loop.summary()
    assert env.over and s["levels"] == 2
    assert s["intents"].get(STUCK, 0) >= 1 and s["intents"].get(NEW_LEVEL, 0) >= 1
    assert s["prism"]["rotations"] >= 1 and s["auto_turns"] >= 1
    assert s["actions_explore"] >= 9  # the hand-off walked the dot home
    assert s["predictions"]["made"] >= 3 and s["predictions"]["hits"] >= 3  # "no change" held
    assert s["sase_turns_all_phases"] >= 1
    lv = [a for a in s["level_attribution"]]
    assert lv and lv[0]["source"] == "explore" and lv[0]["strategy"] in ("handoff", "click_scan")
    assert "INTENT: " in llm.last_user and "STRATEGY" in llm.last_user


def test_plain_mode_has_no_intent_prism_or_phase_protocol():
    env = LineWorld()
    llm = ScriptLLM(["go\n```python\nfor _ in range(9):\n    act(2)\n```"])
    loop = ReasoningLoop(
        env, llm, LoopConfig(mode="plain", prism=False, max_calls=4), episode_id="line"
    )
    loop.run()
    s = loop.summary()
    assert env.over and s["levels"] == 2 and s["actions_model"] == 18
    assert s["intents"] == {} and s["prism"] is None and s["predictions"]["made"] == 0
    assert (
        "INTENT" not in llm.last_user
        and "SITUATION" not in loop.system.split("{protocol}")[0][-200:]
    )


def test_a_bad_expect_never_costs_the_actions():
    env = LineWorld()
    reply = (
        "SITUATION: x\nANALYSIS: none\nSYNTHESIS: none\nEXECUTION:\n```python\n"
        "def mine(s, a):\n    out = s.copy(); out[0, :] = 0; out[0, 1] = 1\n    return out\n"
        "act(2, expect='no_such_rule')\nact(2, expect=mine)\n```"
    )
    loop = ReasoningLoop(
        env,
        ScriptLLM([reply]),
        LoopConfig(mode="sase", prism=False, max_calls=1),
        episode_id="line",
    )
    loop.run()
    s = loop.summary()
    assert s["actions_model"] == 2 and s["turn_errors"] == 0
    assert (
        s["predictions"]["made"] == 1 and s["predictions"]["misses"] == 1
    )  # mine() predicted cell 1, got 2


def test_auto_strategies_hand_back_to_the_model_when_every_frame_is_exhausted():
    env = LineWorld()
    env.auto_action = lambda: (3, -1, -1)  # an auto policy that never wins
    loop = ReasoningLoop(
        env,
        ScriptLLM([STALL]),
        LoopConfig(mode="sase", prism=True, max_calls=1),
        backend=InMemoryBackend(),
        episode_id="line",
    )
    loop.obs = env.observe()
    p = loop.prism
    p.exhausted = ["rule_first", "goal_first", "analogy"]
    p.active = p.rotate(diagnose("analogy", 10, 0, 0, 0, 0, 1))
    assert p.active.kind == "auto"
    for _ in range(loop.cfg.auto_turns):
        loop.turn += 1
        loop._auto_turn(p.active, set(), loop.level)
    assert p.active.kind == "llm"


class FlakyLLM(ScriptLLM):
    """Refuses the first ``down`` calls (an endpoint outage), then answers."""

    def __init__(self, replies, down: int) -> None:
        super().__init__(replies)
        self.down = down
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        if self.calls <= self.down:
            raise ConnectionError("refused")
        return super().chat(*a, **k)


def test_a_dead_endpoint_is_reprobed_so_a_transient_outage_is_not_permanent():
    class Far(LineWorld):
        def auto_action(self):
            return (3, -1, -1)  # the fallback policy never wins

    env = Far()
    llm = FlakyLLM(["go\n```python\nfor _ in range(9):\n    act(2)\n```"], down=3)
    loop = ReasoningLoop(
        env, llm, LoopConfig(mode="plain", prism=False, max_calls=4), episode_id="line"
    )
    loop.run()
    s = loop.summary()
    assert s["llm_errors"] == 3 and s["llm_calls"] >= 1 and env.over
