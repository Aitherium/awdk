"""Runtime typed-I/O validation for AgentResult + Agent.run (NOOA increment 1).

Verifies the fail-closed contract added to adk/core/agent.py:
  - AgentResult.__post_init__ rejects wrong field types at the loop's return boundary
  - AgentResult stays mutable (continuation.py sets result.finish_reason post-construction)
  - Agent.run guards its input and validates the pluggable loop's return type
"""
import asyncio

import pytest

from adk.core.agent import Agent, AgentResult


# --- AgentResult.__post_init__ ------------------------------------------------

def test_valid_agent_result_constructs():
    r = AgentResult(output="hi", messages=[], tool_calls=[], steps=2, finish_reason="stop")
    assert r.output == "hi" and r.steps == 2


def test_defaults_are_valid():
    r = AgentResult(output="ok")
    assert r.messages == [] and r.tool_calls == [] and r.steps == 0 and r.finish_reason == "stop"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"output": 123},                       # output not str
        {"output": "x", "messages": "no"},     # messages not list
        {"output": "x", "tool_calls": {}},     # tool_calls not list
        {"output": "x", "steps": "3"},         # steps not int
        {"output": "x", "steps": True},        # bool is not a valid count
        {"output": "x", "finish_reason": 0},   # finish_reason not str
    ],
)
def test_malformed_agent_result_raises(kwargs):
    with pytest.raises(TypeError):
        AgentResult(**kwargs)


def test_result_stays_mutable_for_continuation():
    # adk/llm/continuation.py mutates result.finish_reason after construction —
    # the contract must NOT be frozen.
    r = AgentResult(output="x")
    r.finish_reason = "length"
    assert r.finish_reason == "length"


# --- Agent.run boundary guards ------------------------------------------------

class _StubLoop:
    """A pluggable loop that returns whatever it is told, to exercise the guards."""

    def __init__(self, to_return):
        self._to_return = to_return

    async def run(self, agent, prompt):  # noqa: ARG002
        return self._to_return


def _agent(loop):
    # model is never touched because we inject a stub loop; object() is enough.
    return Agent(name="t", model=object(), loop=loop)


def test_run_rejects_non_str_prompt():
    agent = _agent(_StubLoop(AgentResult(output="ok")))
    with pytest.raises(TypeError):
        asyncio.run(agent.run(123))  # type: ignore[arg-type]


def test_run_rejects_empty_prompt():
    agent = _agent(_StubLoop(AgentResult(output="ok")))
    with pytest.raises(ValueError):
        asyncio.run(agent.run("   "))


def test_run_validates_loop_return_type():
    agent = _agent(_StubLoop({"output": "not an AgentResult"}))
    with pytest.raises(TypeError):
        asyncio.run(agent.run("hello"))


def test_run_happy_path_returns_result():
    expected = AgentResult(output="done", steps=1)
    agent = _agent(_StubLoop(expected))
    got = asyncio.run(agent.run("hello"))
    assert got is expected and got.output == "done"
