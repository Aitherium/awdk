"""Smoke: the reasoning loop solves a 2-level toy game through a ModelBackend.

A scripted fake ``ModelBackend`` (async ``generate``, no network) answers every
turn with the same reply. It goes through the real bridge
(``_bridge.SyncModel``), the vendored ``ReasoningLoop`` and the public
``solve()`` in both modes:

* ``sase`` -- four phases; SYNTHESIS proposes a ``predict`` rule, EXECUTION acts
  with predictions; the rule must end ACTIVE (replayed over all history) and the
  ledger must score the predictions;
* ``plain`` -- one python block, no intent / PRISM / predictions.

Plus the controls a real run needs: budget, cancel, steering, a dead backend.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, List

import pytest

np = pytest.importorskip("numpy")

from adk.core.model import ModelResponse  # noqa: E402
from adk.reasoning.solve import Budget, LoopConfig, SolveRun, solve  # noqa: E402
from adk.reasoning.solve._bridge import SyncModel  # noqa: E402
from adk.reasoning.solve._run import build_core_loop  # noqa: E402
from adk.reasoning.solve.envs.toy import Counter1D  # noqa: E402
from adk.reasoning.solve.memory import InMemory  # noqa: E402

SASE_REPLY = """SITUATION: a counter at 0; the level target is unknown.
ANALYSIS: none
SYNTHESIS:
```python
def step_rule(state, action):
    out = state.copy()
    if action[0] == 1:
        out[0, 0] = state[0, 0] + 1
    elif action[0] == 2:
        out[0, 0] = state[0, 0] - 1
    return out
hypothesize(step_rule, "predict")
```
EXECUTION:
```python
for _ in range(5):
    act(1, expect={"changed": True})
```"""

PLAIN_REPLY = """Count up.
```python
for _ in range(5):
    act(1)
```"""

SPIN_REPLY = "spin\n```python\nwhile True:\n    pass\n```"


class ScriptedModel:
    """A ModelBackend that replays a script and records every message list."""

    def __init__(self, replies: List[str], *, fail: bool = False, delay_s: float = 0.0) -> None:
        self.name = self.model = "scripted"
        self.replies = replies
        self.fail = fail
        self.delay_s = delay_s
        self.calls: List[List[Any]] = []

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        self.calls.append(list(messages))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail:
            raise ConnectionError("scheduler refused")
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return ModelResponse(
            text=text,
            model=self.model,
            finish_reason="stop",
            usage={"prompt_tokens": 50, "completion_tokens": 20},
        )

    async def stream(self, messages, **kw):  # pragma: no cover - unused
        raise NotImplementedError


# ------------------------------------------------------------- the core loop directly
@pytest.mark.parametrize("sase", [True, False])
def test_reasoning_loop_solves_two_levels_through_the_modelbackend_bridge(sase):
    env = Counter1D(target=5, levels=2)
    model = ScriptedModel([SASE_REPLY if sase else PLAIN_REPLY])
    loop = build_core_loop(
        env,
        SyncModel(model),
        LoopConfig(sase=sase, prism=sase),
        memory=InMemory(),
        episode_id="toy",
    )
    loop.run()
    s = loop.summary()
    assert env.over and s["levels"] == 2
    assert s["actions_model"] == 10 and env.taken == [(1, -1, -1)] * 10
    assert s["llm_calls"] == 2 and s["llm_errors"] == 0 and len(model.calls) == 2
    assert s["prompt_tokens"] == 100 and s["completion_tokens"] == 40
    if sase:
        assert s["verified_names"] == ["step_rule"]
        assert s["sase_turns_all_phases"] == 2
        assert s["predictions"]["made"] >= 8 and s["predictions"]["misses"] == 0
        assert s["intents"].get("new_level", 0) == 2
        assert "SITUATION / ANALYSIS / SYNTHESIS / EXECUTION" in model.calls[0][-1].content
    else:
        assert s["intents"] == {} and s["prism"] is None and s["predictions"]["made"] == 0
        assert "INTENT" not in model.calls[0][-1].content


# ------------------------------------------------------------- the public solve()
@pytest.mark.parametrize("sase", [True, False])
def test_solve_wins_the_toy_game(sase):
    env = Counter1D(target=5, levels=2)
    model = ScriptedModel([SASE_REPLY if sase else PLAIN_REPLY])
    res = asyncio.run(solve(env, model, config=LoopConfig(sase=sase)))
    assert res.finish_reason == "won" and res.won and res.exit_code == 0
    assert res.levels == 2 and res.level_actions == [5, 5] and res.actions == 10
    assert res.llm_calls == 2 and res.tokens["total"] == 140 and not res.tokens["estimated"]
    if sase:
        active = [h for h in res.hypotheses if h.status == "active"]
        assert [h.name for h in active] == ["step_rule"] and active[0].support >= 3
        assert len(active[0].id) == 16 and res.calibration == 1.0
    else:
        assert res.hypotheses == [] and res.calibration is None


async def test_solve_inside_a_running_loop_emits_ordered_events(tmp_path):
    env = Counter1D(target=5, levels=2)
    run = SolveRun(env, ScriptedModel([SASE_REPLY]), config=LoopConfig(run_dir=str(tmp_path)))
    run.start()
    res = await run.join(timeout=30)
    assert res.won
    kinds = [e["event"] for e in run.session._event_log]
    assert kinds[0] == "start" and kinds[-1] == "finish" and "turn" in kinds
    assert res.log_path and (tmp_path / ("%s.jsonl" % run.run_id)).exists()


def test_action_budget_stops_the_run():
    env = Counter1D(target=50, levels=1)
    res = asyncio.run(
        solve(
            env,
            ScriptedModel([PLAIN_REPLY]),
            config=LoopConfig(sase=False, budget=Budget(max_actions=7)),
        )
    )
    assert res.finish_reason == "budget:actions" and res.actions == 7 and res.exit_code == 1


def test_dead_backend_is_not_success():
    env = Counter1D()
    res = asyncio.run(
        solve(env, ScriptedModel([PLAIN_REPLY], fail=True), config=LoopConfig(sase=False))
    )
    assert res.finish_reason == "llm_error" and not res.won and res.exit_code == 2
    assert res.llm_calls == 0 and res.actions == 0


async def test_cancel_stops_an_infinite_cell_and_the_worker_exits():
    env = Counter1D()
    run = SolveRun(
        env, ScriptedModel([SPIN_REPLY]), config=LoopConfig(sase=False, budget=Budget(turn_s=60.0))
    )
    run.start()
    await asyncio.sleep(0.5)
    t0 = time.monotonic()
    run.cancel()
    res = await run.join(timeout=5)
    assert res.finish_reason == "cancelled" and time.monotonic() - t0 < 2.0
    run.thread.join(timeout=2)
    assert not run.thread.is_alive() and run.thread not in threading.enumerate()


async def test_steering_reaches_the_next_model_call():
    env = Counter1D(target=5, levels=2)
    model = ScriptedModel([PLAIN_REPLY], delay_s=0.3)
    run = SolveRun(env, model, config=LoopConfig(sase=False))
    run.start()
    while not model.calls:
        await asyncio.sleep(0.01)
    assert run.steer("try action 2 first")
    res = await run.join(timeout=30)
    assert res.won
    assert "try action 2 first" in model.calls[1][-1].content
    assert any(e.get("event") == "steer" for e in run.session._event_log)


async def test_solveloop_is_an_agentloop():
    import json

    from adk.core.agent import Agent
    from adk.reasoning.solve import SolveLoop

    agent = Agent(
        name="t",
        model=ScriptedModel([PLAIN_REPLY]),
        loop=SolveLoop(Counter1D(), config=LoopConfig(sase=False)),
    )
    out = await agent.run("count to five, twice")
    assert out.finish_reason == "won" and out.steps == 2
    assert json.loads(out.output)["levels"] == 2
