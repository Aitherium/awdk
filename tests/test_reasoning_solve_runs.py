"""Hosted solve runs (``adk.reasoning.solve.runs``): the bookkeeping a server uses.

Start by environment id, look up by run id, steer, cancel, clamp budgets, cap
concurrency, clean up the environment -- all through a scripted ModelBackend.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

pytest.importorskip("numpy")

from adk.core.model import ModelResponse
from adk.reasoning.solve import Budget
from adk.reasoning.solve.runs import (
    ENVIRONMENTS,
    RunCapacityError,
    RunLimits,
    SolveRunRegistry,
    bounded_budget,
    get_run_registry,
    register_environment,
)

FIX_REPLY = """```python
pid = stage_patch("calc.py", "(len(xs) + 1)", "len(xs)")
act(1, pid, 0)
```"""

SPIN_REPLY = "spin\n```python\nwhile True:\n    pass\n```"

COUNT_REPLY = "```python\nfor _ in range(5):\n    act(1)\n```"


class Scripted:
    def __init__(self, replies: List[str], delay_s: float = 0.0) -> None:
        self.name = self.model = "scripted"
        self.replies = replies
        self.delay_s = delay_s
        self.calls: List[Any] = []

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        self.calls.append(list(messages))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return ModelResponse(
            text=text,
            model=self.model,
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 10},
        )

    async def stream(self, messages, **kw):  # pragma: no cover - unused
        raise NotImplementedError


def test_bounded_budget_always_sets_a_wall_clock_and_clamps_to_the_host():
    lim = RunLimits(max_llm_calls=10, max_actions=50, max_wall_s=100.0, default_wall_s=30.0)
    b = bounded_budget(None, lim)
    assert b.max_wall_s == 30.0 and b.max_actions == 50 and b.max_llm_calls == 10
    b = bounded_budget({"max_llm_calls": 1000, "max_wall_s": 10_000, "max_actions": 5}, lim)
    assert (b.max_llm_calls, b.max_wall_s, b.max_actions) == (10, 100.0, 5)
    assert isinstance(b, Budget)
    with pytest.raises(ValueError, match="unknown budget"):
        bounded_budget({"max_wall": 5}, lim)
    with pytest.raises(ValueError, match="positive"):
        bounded_budget({"max_actions": 0}, lim)


def test_named_environments_only():
    assert {"toy:counter", "toy:gridwalk", "debug:demo"} <= set(ENVIRONMENTS)
    with pytest.raises(ValueError):
        register_environment("", lambda: None)
    assert get_run_registry() is get_run_registry()


async def test_start_by_id_runs_to_a_win_and_closes_the_environment():
    reg = SolveRunRegistry()
    run = reg.start(
        "debug:demo", Scripted([FIX_REPLY]), goal="fix it", owner="u1", budget={"max_llm_calls": 3}
    )
    env = reg._runs[run.run_id].env
    assert reg.snapshot(run.run_id)["status"] == "running" and reg.owner(run.run_id) == "u1"
    res = await run.join(timeout=60)
    await asyncio.sleep(0)  # let the done-callback run
    snap = reg.snapshot(run.run_id)
    assert res.won and snap["status"] == "completed" and snap["env_id"] == "debug:demo"
    assert snap["result"]["exit_code"] == 0 and snap["result"]["finish_reason"] == "won"
    assert snap["budget"]["max_wall_s"] == RunLimits().default_wall_s
    assert not env.root.exists()  # the scratch tree was deleted
    assert reg.live_count() == 0 and reg.steer(run.run_id, "late") is False


async def test_unknown_ids_are_none_never_a_default():
    reg = SolveRunRegistry()
    with pytest.raises(KeyError):
        reg.start("arc:nope", Scripted([COUNT_REPLY]))
    assert reg.get("solve-missing") is None and reg.snapshot("solve-missing") is None
    assert reg.steer("solve-missing", "x") is False and reg.cancel("solve-missing") is False


async def test_capacity_cancel_and_steering():
    reg = SolveRunRegistry(RunLimits(max_concurrent=1))
    spin = reg.start("toy:counter", Scripted([SPIN_REPLY]), budget={"turn_s": 60})
    with pytest.raises(RunCapacityError):
        reg.start("toy:counter", Scripted([COUNT_REPLY]))
    await asyncio.sleep(0.3)
    assert reg.cancel(spin.run_id)
    res = await spin.join(timeout=10)
    assert res.finish_reason == "cancelled" and reg.snapshot(spin.run_id)["status"] == "cancelled"

    model = Scripted([COUNT_REPLY], delay_s=0.3)
    run = reg.start("toy:counter", model)  # capacity is free again
    while not model.calls:
        await asyncio.sleep(0.01)
    assert reg.steer(run.run_id, "count with action 1")
    res = await run.join(timeout=30)
    assert res.won and "count with action 1" in model.calls[1][-1].content
