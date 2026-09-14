"""Tests for the OO agent model (adk.core.oo).

The contract under test, adapted from NOOA: subclassing OOAgent makes the
class the agent — class docstring = instructions, plain methods = tools,
async ``...`` methods = LLM-dispatched with the return annotation as a
validated contract.
"""

import json

import pytest
from adk.core.agent import ReActLoop
from adk.core.model import Message, ModelResponse
from adk.core.oo import AgenticReturnError, OOAgent, _BoundMethodTool
from adk.ellipsis import strategy
from pydantic import BaseModel


class ScriptedBackend:
    """ModelBackend returning scripted responses in order."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls: list[list[Message]] = []

    async def generate(self, messages, **opts):
        self.calls.append(list(messages))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return ModelResponse(
            text=self.responses[idx], model=self.model, finish_reason="stop"
        )

    def stream(self, messages, **opts):  # pragma: no cover - protocol filler
        raise NotImplementedError


class Ticket(BaseModel):
    category: str
    urgent: bool


class SupportAgent(OOAgent):
    """You are a support agent for AcmeCo."""

    refund_window_days: int = 30

    def is_refund_eligible(self, days_since_delivery: int) -> bool:
        """Check refund eligibility."""
        return days_since_delivery <= self.refund_window_days

    async def triage(self, message: str) -> Ticket:
        """Create a support ticket for the message."""
        ...

    async def summarize(self, message: str) -> str:
        """Summarize the message in one line."""
        ...

    async def count_words(self, message: str) -> int:
        """Count the words."""
        ...


@pytest.mark.asyncio
async def test_typed_pydantic_return():
    backend = ScriptedBackend([json.dumps({"category": "billing", "urgent": True})])
    agent = SupportAgent(model=backend)
    ticket = await agent.triage("my order never arrived")
    assert isinstance(ticket, Ticket)
    assert ticket.category == "billing"
    assert ticket.urgent is True


@pytest.mark.asyncio
async def test_untyped_str_return_passthrough():
    backend = ScriptedBackend(["A one-line summary."])
    agent = SupportAgent(model=backend)
    out = await agent.summarize("long text here")
    assert out == "A one-line summary."


@pytest.mark.asyncio
async def test_scalar_return_via_rootmodel():
    backend = ScriptedBackend(["7"])
    agent = SupportAgent(model=backend)
    n = await agent.count_words("seven words are in this test sentence")
    assert n == 7
    assert isinstance(n, int)


@pytest.mark.asyncio
async def test_validation_retry_then_success():
    backend = ScriptedBackend(
        ["not json at all", json.dumps({"category": "tech", "urgent": False})]
    )
    agent = SupportAgent(model=backend)
    ticket = await agent.triage("it crashed")
    assert ticket.category == "tech"
    assert len(backend.calls) == 2  # one failed attempt + one retry


@pytest.mark.asyncio
async def test_validation_exhausted_raises():
    backend = ScriptedBackend(["nope", "still nope", "never json"])
    agent = SupportAgent(model=backend)
    with pytest.raises(AgenticReturnError):
        await agent.triage("hi")


def test_plain_methods_become_tools():
    backend = ScriptedBackend(["x"])
    agent = SupportAgent(model=backend)
    tool_names = {t.name for t in agent.tools}
    assert "is_refund_eligible" in tool_names
    # Agentic methods must NOT be tools.
    assert "triage" not in tool_names
    assert "summarize" not in tool_names


@pytest.mark.asyncio
async def test_bound_tool_uses_instance_state():
    backend = ScriptedBackend(["x"])
    agent = SupportAgent(model=backend)
    agent.refund_window_days = 10
    tool = next(t for t in agent.tools if t.name == "is_refund_eligible")
    assert isinstance(tool, _BoundMethodTool)
    result = await tool(days_since_delivery=5)
    assert result.ok and result.value is True
    result = await tool(days_since_delivery=15)
    assert result.ok and result.value is False


def test_bound_tool_schema_derives_json_types():
    class TypedTools(OOAgent):
        """Agent with typed deterministic methods."""

        def lookup(self, item: str, count: int, ratio: float, flag: bool,
                   tags: list[str], meta: dict[str, str], opt: str = "x") -> str:
            """Look things up."""
            return item

    backend = ScriptedBackend(["x"])
    agent = TypedTools(model=backend)
    tool = next(t for t in agent.tools if t.name == "lookup")
    schema = tool.schema()
    props = schema["parameters"]["properties"]
    assert props["item"]["type"] == "string"
    assert props["count"]["type"] == "integer"
    assert props["ratio"]["type"] == "number"
    assert props["flag"]["type"] == "boolean"
    assert props["tags"]["type"] == "array"
    assert props["meta"]["type"] == "object"
    # Defaulted params are not required; the rest are.
    assert "opt" not in schema["parameters"]["required"]
    assert "count" in schema["parameters"]["required"]


def test_class_docstring_becomes_instructions():
    backend = ScriptedBackend(["x"])
    agent = SupportAgent(model=backend)
    assert agent.instructions == "You are a support agent for AcmeCo."
    assert agent.name == "SupportAgent"


def test_missing_model_raises():
    with pytest.raises(ValueError, match="no model"):
        SupportAgent()


@pytest.mark.asyncio
async def test_arguments_rendered_into_prompt_not_lost():
    backend = ScriptedBackend([json.dumps({"category": "b", "urgent": False})])
    agent = SupportAgent(model=backend)
    await agent.triage("NEEDLE-ARG-VALUE")
    user_msgs = [m for m in backend.calls[0] if m.role == "user"]
    assert any("NEEDLE-ARG-VALUE" in m.content for m in user_msgs)
    # Schema contract is stated to the model.
    assert any("JSON schema" in m.content for m in user_msgs)


@pytest.mark.asyncio
async def test_self_attr_templating_expands_state_only():
    class Templated(OOAgent):
        """Agent with templated docstring."""

        limit: int = 42

        async def act(self, message: str) -> str:
            """Limit is {self.limit}; message template {message} stays literal."""
            ...

    backend = ScriptedBackend(["ok"])
    agent = Templated(model=backend)
    await agent.act("hello")
    user = [m for m in backend.calls[0] if m.role == "user"][0]
    assert "Limit is 42" in user.content
    assert "{message}" in user.content  # param templates are NOT expanded


@pytest.mark.asyncio
async def test_strategy_decorator_overrides_loop():
    class Routed(OOAgent):
        """Agent with a per-method loop override."""

        @strategy(loop=ReActLoop(max_steps=1))
        async def act(self) -> str:
            """Do the thing."""
            ...

    backend = ScriptedBackend(["Final Answer: done"])
    agent = Routed(model=backend)
    out = await agent.act()
    assert "done" in out


@pytest.mark.asyncio
async def test_codeact_cells_can_call_self_methods():
    """CodeActLoop injects the live agent as `self` — generated code calls
    deterministic methods directly (NOOA parity, issue item 2)."""
    from adk.core.codeact import CodeActLoop

    class CodeActAgent(OOAgent):
        """Agent whose agentic method runs via CodeAct."""

        refund_window_days: int = 30

        def is_refund_eligible(self, days_since_delivery: int) -> bool:
            """Check refund eligibility."""
            return days_since_delivery <= self.refund_window_days

        @strategy(loop=CodeActLoop(max_steps=3))
        async def decide(self, days: int) -> str:
            """Decide refund eligibility for the given days."""
            ...

    cell = (
        "```python\n"
        "ok = self.is_refund_eligible(days_since_delivery=5)\n"
        "return_result('eligible' if ok else 'denied')\n"
        "```"
    )
    backend = ScriptedBackend([cell])
    agent = CodeActAgent(model=backend)
    out = await agent.decide(5)
    assert "eligible" in out


@pytest.mark.asyncio
async def test_iterative_loop_prompt_carries_api_doc():
    """@strategy methods on iterative loops see the typed API contract
    (doc(self)), not just tool names (issue item 3)."""

    class Documented(OOAgent):
        """Documented agent."""

        threshold: int = 9

        def score_item(self, item: str) -> int:
            """Score one item."""
            return len(item)

        @strategy(loop=ReActLoop(max_steps=1))
        async def act(self) -> str:
            """Act on the items."""
            ...

    backend = ScriptedBackend(["Final Answer: done"])
    agent = Documented(model=backend)
    await agent.act()
    all_text = "\n".join(m.content for m in backend.calls[0])
    assert "Your API" in all_text
    assert "score_item" in all_text  # the typed contract names the method


def test_class_level_model_binding():
    backend = ScriptedBackend(["x"])

    class Bound(OOAgent, model=backend):
        """Bound at class level."""

        async def act(self) -> str:
            """Act."""
            ...

    agent = Bound()
    assert agent.model is backend


@pytest.mark.asyncio
async def test_orchestrator_method_with_real_body_is_not_dispatched():
    calls = []

    class Orchestrator(OOAgent):
        """Orchestrator."""

        async def act(self) -> str:
            """Real body — must run as plain Python, no LLM."""
            calls.append("ran")
            return "python-result"

    backend = ScriptedBackend(["SHOULD-NEVER-APPEAR"])
    agent = Orchestrator(model=backend)
    out = await agent.act()
    assert out == "python-result"
    assert calls == ["ran"]
    assert backend.calls == []
