"""PredictLoop (NOOA single-shot strategy) + per-method model override.

Increment 2 of the NOOA adoption path: a fast single-shot strategy alongside the
iterative ReActLoop, and a loop-level `model=` override so a fast model can serve
classification while the agent's default model serves open-ended work.
"""
import asyncio

from adk.core.agent import AgentLoop, PredictLoop, ReActLoop, Strategy
from adk.core.model import Message, ModelResponse


class FakeModel:
    """Minimal ModelBackend that records calls."""

    def __init__(self, name: str = "fake", text: str = "hello", finish: str | None = "stop"):
        self.name = name
        self.model = name
        self._text = text
        self._finish = finish
        self.calls = 0
        self.last_messages: list[Message] | None = None

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        self.calls += 1
        self.last_messages = list(messages)  # snapshot at call time (loop mutates the list after)
        return ModelResponse(text=self._text, model=self.model, finish_reason=self._finish)

    async def stream(self, messages, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError


def _agent(model, loop):
    from adk.core.agent import Agent
    return Agent(name="t", model=model, loop=loop)


# --- Strategy alias -----------------------------------------------------------

def test_strategy_is_agentloop_alias():
    assert Strategy is AgentLoop


# --- PredictLoop single-shot contract -----------------------------------------

def test_predict_makes_exactly_one_call():
    m = FakeModel(text="positive")
    agent = _agent(m, PredictLoop())
    result = asyncio.run(agent.run("classify: great product"))
    assert m.calls == 1
    assert result.steps == 1
    assert result.tool_calls == []
    assert result.output == "positive"
    assert result.finish_reason == "stop"


def test_predict_strips_output():
    agent = _agent(FakeModel(text="  trimmed \n"), PredictLoop())
    result = asyncio.run(agent.run("x"))
    assert result.output == "trimmed"


def test_predict_finish_reason_defaults_to_stop():
    agent = _agent(FakeModel(finish=None), PredictLoop())
    result = asyncio.run(agent.run("x"))
    assert result.finish_reason == "stop"


def test_predict_sends_system_and_user_messages():
    m = FakeModel()
    agent = _agent(m, PredictLoop())
    asyncio.run(agent.run("hello there"))
    roles = [msg.role for msg in m.last_messages]
    assert roles == ["system", "user"]
    assert m.last_messages[1].content == "hello there"


# --- Per-method model override ------------------------------------------------

def test_predict_model_override_uses_loop_model():
    weak = FakeModel(name="weak", text="weak-ans")
    strong = FakeModel(name="strong", text="strong-ans")
    agent = _agent(weak, PredictLoop(model=strong))
    result = asyncio.run(agent.run("x"))
    assert strong.calls == 1 and weak.calls == 0
    assert result.output == "strong-ans"


def test_predict_defaults_to_agent_model():
    agent_model = FakeModel(name="agent", text="agent-ans")
    agent = _agent(agent_model, PredictLoop())  # no override
    result = asyncio.run(agent.run("x"))
    assert agent_model.calls == 1
    assert result.output == "agent-ans"


def test_react_model_override_uses_loop_model():
    weak = FakeModel(name="weak", text="Final Answer: from-strong")
    strong = FakeModel(name="strong", text="Final Answer: from-strong")
    agent = _agent(weak, ReActLoop(model=strong))
    result = asyncio.run(agent.run("x"))
    assert strong.calls >= 1 and weak.calls == 0
    assert result.output == "from-strong"
