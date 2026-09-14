"""What CodeActLoop actually bounds — and what it provably does NOT.

Written after a live probe contradicted the claim that the loop "rejects all
malicious code": a cell really did write a file to disk, really did import `os`,
really did spawn a subprocess via `subprocess.run`, and `while True: pass` was
NEVER interrupted — `asyncio.wait_for` cannot preempt a coroutine that never
yields, so the documented per-cell timeout silently did not hold and the agent
hung indefinitely.

The timeout is now enforced by a line-level trace deadline as well. These tests
pin BOTH halves of the truth so neither can rot:
  * the liveness guarantee that now holds (a non-yielding cell IS interrupted)
  * the containment that does NOT exist (so nobody wires this to untrusted input
    believing it is a sandbox)
"""
from __future__ import annotations

import sys
import time

import pytest

from adk.core.agent import Agent
from adk.core.codeact import CellTimeout, CodeActLoop, _DeadlineTrace
from adk.core.model import Message, ModelBackend, ModelResponse
from adk.core.validator import CodeValidator


class _Cells(ModelBackend):
    def __init__(self, cells: list[str]):
        self.cells = cells
        self.i = 0

    async def generate(self, messages: list[Message]) -> ModelResponse:
        text = self.cells[self.i] if self.i < len(self.cells) else 'return_result("done")'
        self.i += 1
        return ModelResponse(text=text, model="fake", finish_reason="stop")


def _agent(cell: str, **kw) -> Agent:
    return Agent(
        name="limits",
        model=_Cells([f"```python\n{cell}\n```"]),
        tools=[],
        loop=CodeActLoop(max_steps=2, **kw),
    )


# ── the liveness guarantee that NOW holds ──────────────────────────────────


@pytest.mark.asyncio
async def test_non_yielding_infinite_loop_is_interrupted():
    """`while True: pass` must be bounded. It used to hang the agent forever."""
    t0 = time.monotonic()
    result = await _agent("while True:\n    pass", cell_timeout=1.0).run("go")
    elapsed = time.monotonic() - t0

    assert elapsed < 20.0, f"cell was not interrupted (took {elapsed:.1f}s)"
    # The loop survives the timeout and keeps going rather than dying.
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_timeout_is_reported_to_the_model_as_an_observation():
    """A timed-out cell must tell the model, not fail silently."""
    cells = ["```python\nwhile True:\n    pass\n```", "```python\nreturn_result('recovered')\n```"]
    agent = Agent(
        name="limits",
        model=_Cells(cells),
        tools=[],
        loop=CodeActLoop(max_steps=3, cell_timeout=1.0),
    )
    result = await agent.run("go")
    assert result.output == "recovered", "the model must get a turn after a timeout"


@pytest.mark.asyncio
async def test_a_cell_that_awaits_is_still_bounded():
    """The wait_for half of the bound must still work (I/O-bound cells)."""
    t0 = time.monotonic()
    await _agent("await asyncio.sleep(30)", cell_timeout=1.0).run("go")
    assert time.monotonic() - t0 < 20.0


def test_deadline_trace_raises_celltimeout_and_restores_the_previous_hook():
    """The loop must live in a frame ENTERED INSIDE the `with`.

    `sys.settrace` does not retroactively instrument the frame that arms it, so
    spinning inline here would hang forever (it did, while writing this). Compile
    under the cell filename because enforcement is scoped to cell frames.
    """
    ns: dict = {}
    exec(compile("def spin():\n    while True:\n        pass\n", "<cell>", "exec"), ns)

    prev = sys.gettrace()
    with pytest.raises(CellTimeout):
        with _DeadlineTrace(0.05):
            ns["spin"]()
    assert sys.gettrace() is prev, "the trace hook must be restored"


def test_deadline_trace_does_not_interrupt_non_cell_frames():
    """Scoping guard: an unscoped hook fired inside unrelated coroutines.

    `sys.settrace` is thread-global, so while a cell awaits, the event loop runs
    other tasks. Only frames compiled as the cell may be interrupted.
    """
    ns: dict = {}
    exec(compile("def other():\n    return sum(range(200000))\n", "<not-a-cell>", "exec"), ns)

    with _DeadlineTrace(0.0, filename="<cell>"):  # already expired
        # A non-cell frame must run to completion despite the blown deadline.
        assert ns["other"]() == sum(range(200000))


@pytest.mark.asyncio
async def test_normal_cells_are_unaffected_by_the_deadline_hook():
    """Tracing must not break ordinary execution or cross-cell persistence."""
    cells = [
        "```python\nvalues = [i * 2 for i in range(5)]\n```",
        "```python\nreturn_result(sum(values))\n```",
    ]
    agent = Agent(
        name="limits", model=_Cells(cells), tools=[], loop=CodeActLoop(max_steps=3)
    )
    result = await agent.run("go")
    assert result.output == "20"


# ── the containment that does NOT exist (documented, not aspirational) ─────


@pytest.mark.parametrize(
    "idiom",
    [
        'eval("1+1")',
        'exec("x=1")',
        '__import__("os").system("echo x")',
        'compile("1","<s>","eval")',
        '"".__class__.__mro__[1].__subclasses__()',
        'globals()["__builtins__"]',
    ],
)
def test_validator_rejects_the_escape_idioms_it_claims_to(idiom):
    assert CodeValidator().validate(idiom), f"expected {idiom!r} to be rejected"


@pytest.mark.parametrize(
    "permitted",
    [
        "import os",
        "import subprocess",
        'open("f.txt", "w")',
        "while True: pass",
    ],
)
def test_validator_does_NOT_contain_these(permitted):
    """Pins the real trust boundary so the docs cannot drift back to a lie.

    These pass validation by design — code-as-action needs imports and file I/O.
    If a future change starts rejecting one, this test fails and the CodeActLoop
    warning (which states these are permitted) must be updated with it.
    """
    assert CodeValidator().validate(permitted) == [], (
        f"{permitted!r} is now rejected — update the CodeActLoop security warning"
    )


def test_codeact_docstring_states_it_is_not_a_sandbox():
    """The warning is load-bearing: it is the only thing stopping misuse."""
    doc = CodeActLoop.__doc__ or ""
    assert "not a sandbox" in doc.lower()
    assert "untrusted" in doc.lower()
