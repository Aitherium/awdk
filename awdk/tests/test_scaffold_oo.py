"""Tests for the OO scaffold style — the generated agent must actually run."""

import importlib.util
import json
import sys

import pytest
from adk.core.model import Message, ModelResponse
from adk.core.scaffold import _class_name, scaffold_agent


class ScriptedBackend:
    name = "scripted"
    model = "scripted-1"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def generate(self, messages: list[Message], **opts) -> ModelResponse:
        self.calls.append(list(messages))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return ModelResponse(text=self.responses[idx], model=self.model, finish_reason="stop")

    def stream(self, messages, **opts):  # pragma: no cover - protocol filler
        raise NotImplementedError


def _import_file(path, module_name):
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(module_name, None)
    return mod


def test_oo_is_the_default_style(tmp_path):
    root = scaffold_agent(tmp_path, name="my-bot")
    assert (root / "agent.py").exists()
    assert not (root / "agent.yaml").exists()


def test_yaml_style_still_available(tmp_path):
    root = scaffold_agent(tmp_path, name="my-bot", style="yaml")
    assert (root / "agent.yaml").exists()
    assert (root / "tools.py").exists()
    assert not (root / "agent.py").exists()


def test_invalid_style_rejected(tmp_path):
    with pytest.raises(ValueError, match="style"):
        scaffold_agent(tmp_path, name="x", style="jinja")


def test_class_name_derivation():
    assert _class_name("my-bot") == "MyBot"
    assert _class_name("support_agent") == "SupportAgent"
    assert _class_name("3bot") == "Agent3bot"


@pytest.mark.asyncio
async def test_scaffolded_oo_agent_runs_end_to_end(tmp_path):
    """The generated agent.py imports, instantiates, and serves a typed call."""
    root = scaffold_agent(tmp_path, name="my-bot", description="Test bot.")
    mod = _import_file(root / "agent.py", "scaffolded_agent_under_test")

    backend = ScriptedBackend([json.dumps({"summary": "fine", "confidence": 0.9})])
    agent = mod.MyBot(model=backend)

    # Deterministic method is a tool and works as plain Python.
    assert agent.hello(who="dev") == "hello dev"
    assert "hello" in {t.name for t in agent.tools}

    # Agentic method returns the validated typed contract.
    answer = await agent.answer("is everything ok?")
    assert answer.summary == "fine"
    assert answer.confidence == 0.9

    # Instructions came from the class docstring.
    assert "my-bot" in agent.instructions
