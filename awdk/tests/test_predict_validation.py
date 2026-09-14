"""Structured output validation for PredictLoop (NOOA increment 2).

Validates the retry-on-failure contract added to PredictLoop:
  - PredictLoop(output_model=M) validates each response against schema M
  - On ValidationError/JSONDecodeError, appends error msg and retries up to max_retries
  - On success, output is validated.model_dump_json()
  - On all failures, finish_reason='validation_failed' and output is last raw text
  - output_model=None → original behavior unchanged (single call, no validation)
"""
import asyncio

from pydantic import BaseModel, Field

from adk.core.agent import Agent, PredictLoop


class ColorModel(BaseModel):
    """A simple schema for testing."""

    color: str = Field(description="The color name")
    brightness: int = Field(ge=0, le=100, description="Brightness 0-100")


class FakeModel:
    """Minimal ModelBackend that records calls and returns configurable responses."""

    def __init__(
        self,
        name: str = "fake",
        texts: list[str] | None = None,
        finish: str | None = "stop",
    ):
        self.name = name
        self.model = name
        self._texts = texts or ["hello"]
        self._finish = finish
        self.calls = 0
        self.last_messages = None

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        text = self._texts[self.calls] if self.calls < len(self._texts) else self._texts[-1]
        self.calls += 1
        self.last_messages = list(messages)
        from adk.core.model import ModelResponse

        return ModelResponse(text=text, model=self.model, finish_reason=self._finish)

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError


def _agent(model, loop):
    return Agent(name="test", model=model, loop=loop)


# --- Validation Success Path ---------------------------------------------------


def test_predict_validation_valid_json_on_first_try():
    """Valid JSON on first call should succeed and return model_dump_json."""
    valid_json = '{"color": "blue", "brightness": 75}'
    m = FakeModel(texts=[valid_json])
    agent = _agent(m, PredictLoop(output_model=ColorModel))
    result = asyncio.run(agent.run("what color?"))

    assert m.calls == 1
    assert result.steps == 1
    assert result.finish_reason == "stop"
    # Output should be the model_dump_json, not the raw JSON
    import json

    output_dict = json.loads(result.output)
    assert output_dict["color"] == "blue"
    assert output_dict["brightness"] == 75


def test_predict_validation_strips_whitespace():
    """Whitespace-padded JSON should still validate."""
    valid_json = """
    {
      "color": "green",
      "brightness": 50
    }
    """
    m = FakeModel(texts=[valid_json])
    agent = _agent(m, PredictLoop(output_model=ColorModel))
    result = asyncio.run(agent.run("color?"))

    assert m.calls == 1
    assert result.finish_reason == "stop"
    import json

    output_dict = json.loads(result.output)
    assert output_dict["color"] == "green"


# --- Validation Retry Path ---------------------------------------------------


def test_predict_validation_invalid_then_valid_retries():
    """Invalid JSON on first call, valid on second should retry and succeed."""
    m = FakeModel(
        texts=[
            'not valid json at all',
            '{"color": "red", "brightness": 80}',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=2))
    result = asyncio.run(agent.run("color?"))

    assert m.calls == 2, f"Expected 2 calls, got {m.calls}"
    assert result.steps == 2
    assert result.finish_reason == "stop"
    import json

    output_dict = json.loads(result.output)
    assert output_dict["color"] == "red"
    assert output_dict["brightness"] == 80


def test_predict_validation_model_error_then_valid():
    """Pydantic validation error (e.g., out of range) retries and succeeds."""
    m = FakeModel(
        texts=[
            '{"color": "purple", "brightness": 150}',  # out of range
            '{"color": "purple", "brightness": 50}',   # valid
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=2))
    result = asyncio.run(agent.run("color?"))

    assert m.calls == 2
    assert result.finish_reason == "stop"
    import json

    output_dict = json.loads(result.output)
    assert output_dict["brightness"] == 50


def test_predict_validation_appends_error_to_messages():
    """Error message should be appended to messages for the model to see."""
    m = FakeModel(
        texts=[
            'not json',
            '{"color": "yellow", "brightness": 60}',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=1))
    result = asyncio.run(agent.run("color?"))

    # Check that error message was added to conversation
    error_msgs = [msg for msg in result.messages if "Validation error" in msg.content]
    assert len(error_msgs) > 0, (
        f"No validation error message found in {[m.content for m in result.messages]}"
    )


# --- Validation Exhaustion Path ---------------------------------------------------


def test_predict_validation_all_retries_exhausted():
    """When all retries fail, should set finish_reason='validation_failed'."""
    m = FakeModel(
        texts=[
            'not json 1',
            'not json 2',
            'not json 3',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=2))
    result = asyncio.run(agent.run("color?"))

    # Should make 1 initial + 2 retries = 3 calls
    assert m.calls == 3, f"Expected 3 calls (1 initial + 2 retries), got {m.calls}"
    assert result.steps == 3
    assert result.finish_reason == "validation_failed"
    # Output should be the last raw text, not model_dump_json
    assert result.output == "not json 3"


def test_predict_validation_max_retries_zero():
    """max_retries=0 should make exactly 1 call and fail if invalid."""
    m = FakeModel(texts=['not json'])
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=0))
    result = asyncio.run(agent.run("color?"))

    assert m.calls == 1
    assert result.finish_reason == "validation_failed"
    assert result.output == "not json"


def test_predict_validation_single_failure():
    """Single failure with max_retries=1 should retry once more."""
    m = FakeModel(
        texts=[
            'bad json',
            '{"color": "orange", "brightness": 40}',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=1))
    result = asyncio.run(agent.run("color?"))

    assert m.calls == 2
    assert result.finish_reason == "stop"


# --- Backward Compatibility ---------------------------------------------------


def test_predict_no_output_model_unchanged():
    """output_model=None should use original single-call behavior."""
    m = FakeModel(texts=["just a plain string"])
    agent = _agent(m, PredictLoop(output_model=None))
    result = asyncio.run(agent.run("hello?"))

    assert m.calls == 1
    assert result.steps == 1
    assert result.finish_reason == "stop"
    assert result.output == "just a plain string"


def test_predict_default_output_model_is_none():
    """By default, output_model=None."""
    m = FakeModel(texts=["plain output"])
    agent = _agent(m, PredictLoop())  # no output_model arg
    result = asyncio.run(agent.run("hello?"))

    assert m.calls == 1
    assert result.output == "plain output"


def test_predict_default_max_retries_is_two():
    """By default, max_retries=2."""
    m = FakeModel(
        texts=[
            'fail 1',
            'fail 2',
            '{"color": "pink", "brightness": 30}',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel))  # default max_retries=2
    result = asyncio.run(agent.run("color?"))

    # 1 initial + 2 retries = 3 calls; should succeed on 3rd
    assert m.calls == 3
    assert result.finish_reason == "stop"


# --- Tracing & Edge Cases ---------------------------------------------------


def test_predict_validation_sets_steps_correctly():
    """steps should reflect actual number of LLM calls."""
    m = FakeModel(
        texts=[
            'bad',
            'bad',
            '{"color": "cyan", "brightness": 90}',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=2))
    result = asyncio.run(agent.run("color?"))

    assert result.steps == 3


def test_predict_validation_empty_json():
    """Empty JSON object should fail validation (missing fields)."""
    m = FakeModel(
        texts=[
            '{}',
            '{"color": "teal", "brightness": 10}',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=1))
    result = asyncio.run(agent.run("color?"))

    assert m.calls == 2
    assert result.finish_reason == "stop"


def test_predict_validation_partial_json():
    """Partial JSON (missing required fields) should fail and retry."""
    m = FakeModel(
        texts=[
            '{"color": "navy"}',  # missing brightness
            '{"color": "navy", "brightness": 20}',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=1))
    result = asyncio.run(agent.run("color?"))

    assert m.calls == 2
    assert result.finish_reason == "stop"


def test_predict_validation_markdown_wrapped():
    """JSON wrapped in markdown code blocks should fail (raw JSON required)."""
    m = FakeModel(
        texts=[
            '```json\n{"color": "red", "brightness": 50}\n```',
            '{"color": "red", "brightness": 50}',
        ]
    )
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=1))
    result = asyncio.run(agent.run("color?"))

    # First call fails (markdown wrapped), second succeeds
    assert m.calls == 2
    assert result.finish_reason == "stop"


# --- Multiple models with different schemas ---


class PersonModel(BaseModel):
    """A different schema for testing."""

    name: str
    age: int = Field(ge=0, le=150)


def test_predict_validation_different_schemas():
    """Different PredictLoop instances with different schemas should work."""
    color_model = FakeModel(texts=['{"color": "blue", "brightness": 75}'])
    person_model = FakeModel(texts=['{"name": "Alice", "age": 30}'])

    color_agent = _agent(color_model, PredictLoop(output_model=ColorModel))
    person_agent = _agent(person_model, PredictLoop(output_model=PersonModel))

    color_result = asyncio.run(color_agent.run("color?"))
    person_result = asyncio.run(person_agent.run("person?"))

    import json

    color_dict = json.loads(color_result.output)
    person_dict = json.loads(person_result.output)

    assert color_dict["color"] == "blue"
    assert person_dict["name"] == "Alice"


# --- Finish reason correctness ---


def test_predict_validation_finish_reason_stop_on_success():
    """Successful validation should have finish_reason='stop'."""
    m = FakeModel(texts=['{"color": "red", "brightness": 50}'])
    agent = _agent(m, PredictLoop(output_model=ColorModel))
    result = asyncio.run(agent.run("color?"))

    assert result.finish_reason == "stop"


def test_predict_validation_finish_reason_validation_failed():
    """Failed validation should have finish_reason='validation_failed'."""
    m = FakeModel(texts=['not json'])
    agent = _agent(m, PredictLoop(output_model=ColorModel, max_retries=0))
    result = asyncio.run(agent.run("color?"))

    assert result.finish_reason == "validation_failed"
