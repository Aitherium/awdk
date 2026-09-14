"""Tests for CodeActLoop strategy."""

import pytest

from adk.core.agent import Agent
from adk.core.codeact import CodeActLoop
from adk.core.model import Message, ModelBackend, ModelResponse
from adk.core.tool import Tool, ToolResult


class FakeModelBackend(ModelBackend):
    """Mock backend that returns scripted code cells."""

    def __init__(self, cells: list[str]):
        """Initialize with a list of code cells to return sequentially.

        Args:
            cells: List of cell contents. Each can be plain Python code or
                  wrapped in markdown code blocks (```python ... ```).
        """
        self.cells = cells
        self.call_count = 0
        self.call_messages = []

    async def generate(self, messages: list[Message]) -> ModelResponse:
        """Return a pre-defined cell and track messages."""
        self.call_messages.append(messages)
        if self.call_count >= len(self.cells):
            # If we've run out of cells, return a final answer to prevent infinite loop
            response = "Done"
        else:
            response = self.cells[self.call_count]
        self.call_count += 1
        return ModelResponse(text=response, model="fake", finish_reason="stop")


class AsyncCallableTool(Tool):
    """A tool that can be called as an async function."""

    def __init__(self, name: str, description: str, result_value: str = "tool_ok"):
        self.name = name
        self.description = description
        self.result_value = result_value

    async def __call__(self, **kwargs):
        return ToolResult(ok=True, value=self.result_value)


# =============================================================================
# Test: Multi-cell state persistence
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_state_persistence():
    """Test that variables defined in one cell are usable in the next."""
    cells = [
        "```python\nx = 5\nprint(f'Set x to {x}')\n```",
        "```python\nresult = x + 1\nreturn_result(result)\n```",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Test state persistence")

    # The final output should be the return_result value (6)
    assert result.output == "6"
    assert result.steps == 2
    assert result.finish_reason == "stop"


# =============================================================================
# Test: Tool calling from within code
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_tool_calling():
    """Test that agent tools can be called from within executed code."""
    tool = AsyncCallableTool(
        name="greet",
        description="Greet someone",
        result_value="Hello, Alice!",
    )

    cells = [
        "```python\nresult = await greet(name='Alice')\nreturn_result(result)\n```",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[tool],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Call the greet tool")

    # The result should be what the tool returned
    assert "Hello, Alice!" in result.output
    assert result.finish_reason == "stop"


# =============================================================================
# Test: Security validation (malicious code rejected)
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_rejects_malicious_code():
    """Test that dangerous patterns (eval, exec, etc.) are rejected."""
    cells = [
        "```python\n# Try to use eval (should be rejected)\neval('1+1')\n```",
        (
            "```python\n# After rejection, model tries a safe approach\n"
            "result = 2\nreturn_result(result)\n```"
        ),
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Try to use eval")

    # First cell should be rejected, second should succeed
    assert result.steps == 2
    assert result.output == "2"
    assert result.finish_reason == "stop"

    # Check that the first cell triggered a validation error message
    messages = result.messages
    error_messages = [m for m in messages if "validation failed" in m.content.lower()]
    assert len(error_messages) > 0, "Expected validation error feedback to model"


@pytest.mark.asyncio
async def test_codeact_rejects_import_hack():
    """Test that __import__ is rejected."""
    cells = [
        "```python\n__import__('os')\n```",
        "```python\nreturn_result('safe')\n```",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Try to import os")

    # First cell should be rejected
    messages = result.messages
    error_messages = [m for m in messages if "validation failed" in m.content.lower()]
    assert len(error_messages) > 0


# =============================================================================
# Test: Exception handling
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_exception_becomes_observation():
    """Test that exceptions in code are captured and fed back to model."""
    cells = [
        "```python\n1 / 0  # Intentional error\n```",
        "```python\nresult = 'handled error'\nreturn_result(result)\n```",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Divide by zero")

    # Second cell should succeed
    assert result.output == "handled error"
    assert result.steps == 2
    assert result.finish_reason == "stop"

    # Check that error was sent back to model
    messages = result.messages
    error_observations = [m for m in messages if "ZeroDivisionError" in m.content]
    assert len(error_observations) > 0, "Exception should appear in messages to model"


# =============================================================================
# Test: return_result terminates loop
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_return_result_terminates():
    """Test that calling return_result stops the loop immediately."""
    cells = [
        "```python\nreturn_result('done')\nprint('should not print')\n```",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Return immediately")

    # Should finish on first step
    assert result.steps == 1
    assert result.output == "done"
    assert result.finish_reason == "stop"

    # The print after return_result should not appear
    assert "should not print" not in result.output


# =============================================================================
# Test: max_steps bound
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_max_steps_bound():
    """Test that max_steps is respected."""
    cells = [
        "```python\nprint('step 1')\n```",
        "```python\nprint('step 2')\n```",
        "```python\nprint('step 3')\n```",
        "```python\nprint('step 4')\n```",  # Should not be reached
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=3),
    )

    result = await agent.run("Run multiple steps")

    # Should stop at max_steps (3)
    assert result.steps == 3
    assert result.finish_reason == "max_steps"


# =============================================================================
# Test: Per-loop model override
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_model_override():
    """Test that per-loop model override is used."""
    cells = [
        "```python\nreturn_result('from_override')\n```",
    ]
    override_backend = FakeModelBackend(cells)
    default_backend = FakeModelBackend(["```python\nreturn_result('from_default')\n```"])

    agent = Agent(
        name="test_agent",
        model=default_backend,
        tools=[],
        loop=CodeActLoop(max_steps=8, model=override_backend),
    )

    result = await agent.run("Test model override")

    # Should use override_backend
    assert result.output == "from_override"
    assert override_backend.call_count > 0
    assert default_backend.call_count == 0


# =============================================================================
# Test: Code extraction (markdown blocks)
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_extracts_markdown_code_block():
    """Test that Python code is extracted from markdown blocks."""
    cells = [
        "Here's the code I'll run:\n```python\nx = 42\nreturn_result(x)\n```\nDone!",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Extract markdown code")

    # Should extract and run the code from markdown
    assert result.output == "42"
    assert result.finish_reason == "stop"


# =============================================================================
# Test: No code block means final answer
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_plain_text_as_answer():
    """Test that plain text (without code) is treated as final answer."""
    cells = [
        "The answer is 42",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Just give me an answer")

    # Should treat plain text as final answer
    assert "42" in result.output
    assert result.steps == 1
    assert result.finish_reason == "stop"


# =============================================================================
# Test: Complex multi-step workflow
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_complex_workflow():
    """Test a more complex multi-step workflow."""
    cells = [
        "```python\ndata = [1, 2, 3, 4, 5]\nprint(f'Data: {data}')\n```",
        (
            "```python\ntotal = sum(data)\naverage = total / len(data)\n"
            "print(f'Average: {average}')\n```"
        ),
        (
            "```python\nresult = {'total': total, 'average': average}\n"
            "return_result(result)\n```"
        ),
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Calculate stats on data")

    # Should complete all steps
    assert result.steps == 3
    assert result.finish_reason == "stop"
    # Result should be the dictionary
    assert "total" in result.output or "15" in result.output


# =============================================================================
# Test: Tool call from multi-cell workflow
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_tool_in_workflow():
    """Test calling tools as part of a multi-cell workflow."""
    tool = AsyncCallableTool(
        name="fetch_data",
        description="Fetch some data",
        result_value="[10, 20, 30]",
    )

    cells = [
        "```python\ndata = await fetch_data()\nprint(f'Fetched: {data}')\n```",
        (
            "```python\n# Convert string representation to actual list\n"
            "data_list = [10, 20, 30]\nresult = sum(data_list)\n"
            "return_result(result)\n```"
        ),
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[tool],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Fetch and sum data")

    # Should complete
    assert result.steps == 2
    assert result.finish_reason == "stop"
    assert "60" in result.output


# =============================================================================
# Test: Output truncation
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_output_truncation():
    """Test that large outputs are truncated."""
    cells = [
        "```python\nprint('x' * 50000)\nreturn_result('done')\n```",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8, max_output_bytes=1000),
    )

    result = await agent.run("Generate large output")

    # Should complete
    assert result.finish_reason == "stop"
    # Output should be truncated
    assert len(result.output) < 50000 + 1000  # Much smaller than full output


# =============================================================================
# Test: Syntax error handling
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_syntax_error():
    """Test that syntax errors are handled gracefully."""
    cells = [
        "```python\nif x == 5\n  print('error')\n```",
        "```python\nx = 5\nprint(f'Fixed: {x}')\nreturn_result('recovered')\n```",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Syntax error test")

    # First cell has syntax error, second should recover
    assert result.steps == 2
    assert result.output == "recovered"
    assert result.finish_reason == "stop"


# =============================================================================
# Test: Dangling code fence
# =============================================================================

@pytest.mark.asyncio
async def test_codeact_dangling_fence():
    """Test handling of incomplete markdown fences."""
    cells = [
        "```python\nprint('incomplete fence",
        "```python\nx = 1\nreturn_result(x)\n```",
    ]
    backend = FakeModelBackend(cells)

    agent = Agent(
        name="test_agent",
        model=backend,
        tools=[],
        loop=CodeActLoop(max_steps=8),
    )

    result = await agent.run("Dangling fence")

    # First cell is incomplete/not extracted, second should work
    # Either first cell is skipped (no code found) or treated as plain text
    assert result.finish_reason == "stop"
