"""CodeAct with real containment — the escapes that work in-process must fail.

`tests/test_codeact_limits.py` pins what in-process CodeAct canNOT contain: a cell
really writes host files, imports os, and spawns subprocesses. This file is the
other half: with `executor=DockerCellExecutor()` those same cells are contained.

The argv tests always run. Container tests are skipped (never failed) when Docker
or the local image is unavailable, so CI without a daemon stays green.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from adk.core.agent import Agent
from adk.core.cell_executors import (
    RESULT_SENTINEL,
    CellOutcome,
    DockerCellExecutor,
    docker_available,
    extract_result,
    isolated_prelude,
)
from adk.core.codeact import CodeActLoop
from adk.core.model import Message, ModelBackend, ModelResponse

IMAGE = "python:3.12-slim"
docker_required = pytest.mark.skipif(
    not docker_available(IMAGE), reason=f"docker daemon or local {IMAGE} unavailable"
)


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
        name="isolated",
        model=_Cells([f"```python\n{cell}\n```"]),
        tools=[],
        loop=CodeActLoop(
            max_steps=2, cell_timeout=90, executor=DockerCellExecutor(image=IMAGE), **kw
        ),
    )


# ── the lockdown flags are load-bearing, so assert they are present ────────


@pytest.mark.parametrize(
    "flag",
    [
        ("--network", "none"),
        ("--read-only",),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--memory",),
        ("--pids-limit",),
        ("--user",),
        ("-i",),
    ],
)
def test_container_argv_carries_the_lockdown(flag):
    argv = DockerCellExecutor()._argv("print(1)")
    if len(flag) == 1:
        assert flag[0] in argv, argv
    else:
        i = argv.index(flag[0])
        assert argv[i + 1] == flag[1], argv


def test_container_runs_python_isolated_mode():
    """`-I` isolates the interpreter from env/site so PYTHONPATH cannot inject."""
    argv = DockerCellExecutor()._argv("print(1)")
    # argv tail is: python -I -c <code>
    assert argv[-4:-1] == ["python", "-I", "-c"], argv


def test_loop_reports_whether_it_is_isolated():
    assert CodeActLoop().is_isolated is False
    assert CodeActLoop(executor=DockerCellExecutor()).is_isolated is True


# ── the result channel (no daemon needed) ──────────────────────────────────


def test_prelude_defines_return_result():
    src = isolated_prelude()
    assert "def return_result(" in src
    assert "_os._exit(0)" in src, "must not be swallowable by a bare except"


def test_extract_result_splits_value_from_stdout():
    out = f"chatter\n{RESULT_SENTINEL}42\nmore\n"
    value, kept = extract_result(out)
    assert value == "42"
    assert kept == "chatter\nmore"


def test_extract_result_returns_none_without_a_sentinel():
    value, kept = extract_result("just output\n")
    assert value is None and kept == "just output"


def test_outcome_ok_is_false_on_timeout_block_or_nonzero():
    assert CellOutcome(exit_code=0).ok is True
    assert CellOutcome(exit_code=1).ok is False
    assert CellOutcome(exit_code=0, timed_out=True).ok is False
    assert CellOutcome(exit_code=0, blocked_reason="no docker").ok is False


@pytest.mark.asyncio
async def test_missing_docker_blocks_rather_than_running_unsandboxed():
    """If the backend cannot start, the cell must NOT fall back to in-process."""
    ex = DockerCellExecutor()
    ex.extra_args = ["--this-flag-does-not-exist"]
    outcome = await ex.run("print(1)", timeout=60)
    assert outcome.ok is False


@pytest.mark.asyncio
async def test_empty_cell_is_blocked_not_executed():
    outcome = await DockerCellExecutor().run("   ", timeout=5)
    assert outcome.blocked_reason == "empty cell"


# ── containment, against a real container ──────────────────────────────────


@docker_required
@pytest.mark.asyncio
async def test_host_file_write_is_contained():
    """In-process this CREATES A REAL FILE. Here it must not."""
    marker = Path("test_isolated_escape_marker.txt")
    if marker.exists():
        marker.unlink()
    outcome = await DockerCellExecutor(image=IMAGE).run(
        f'open("/{marker.name}", "w").write("ESCAPED")', timeout=90
    )
    assert outcome.ok is False, "read-only rootfs must reject the write"
    assert not marker.exists(), "nothing may appear on the host filesystem"


@docker_required
@pytest.mark.asyncio
async def test_network_egress_is_contained():
    outcome = await DockerCellExecutor(image=IMAGE).run(
        'import urllib.request; urllib.request.urlopen("http://1.1.1.1", timeout=5)',
        timeout=90,
    )
    assert outcome.ok is False, "--network none must block egress"


@docker_required
@pytest.mark.asyncio
async def test_cell_does_not_run_as_root():
    outcome = await DockerCellExecutor(image=IMAGE).run(
        "import os; print(os.getuid())", timeout=90
    )
    assert outcome.ok
    assert outcome.stdout.strip() != "0", "a cell must not be root in the container"


@docker_required
@pytest.mark.asyncio
async def test_host_env_is_not_visible():
    outcome = await DockerCellExecutor(image=IMAGE).run(
        'import os; print([k for k in os.environ if "AITHER" in k or "TOKEN" in k])',
        timeout=90,
    )
    assert outcome.ok
    assert outcome.stdout.strip() == "[]", "host secrets must not reach the cell"


@docker_required
@pytest.mark.asyncio
async def test_infinite_loop_is_KILLED_not_merely_interrupted():
    """The guarantee in-process execution cannot make."""
    outcome = await DockerCellExecutor(image=IMAGE).run("while True: pass", timeout=8)
    assert outcome.timed_out is True
    assert "killed" in outcome.stderr.lower()


@docker_required
@pytest.mark.asyncio
async def test_legitimate_compute_still_works():
    outcome = await DockerCellExecutor(image=IMAGE).run("print(sum(range(100)))", timeout=90)
    assert outcome.ok and outcome.stdout.strip() == "4950"


# ── end-to-end through CodeActLoop ─────────────────────────────────────────


@docker_required
@pytest.mark.asyncio
async def test_loop_returns_a_result_across_the_boundary():
    """return_result() must survive being in a different process."""
    result = await _agent("x = sum(range(10))\nreturn_result(x)").run("go")
    assert result.output == "45"


@docker_required
@pytest.mark.asyncio
async def test_loop_does_not_eval_the_returned_repr():
    """The value crosses as text and must NOT be eval'd back into this process.

    eval()-ing attacker-influenced output would undo the entire isolation.
    """
    result = await _agent("return_result('__import__(\\'os\\').getcwd()')").run("go")
    assert "getcwd" in str(result.output), "value must come back as inert text"


@docker_required
@pytest.mark.asyncio
async def test_loop_survives_a_contained_escape_attempt():
    """A blocked cell becomes an observation; the loop keeps going."""
    marker = Path("test_isolated_loop_marker.txt")
    if marker.exists():
        marker.unlink()
    result = await _agent(f'open("/{marker.name}","w").write("X")\nreturn_result("wrote")').run("go")
    assert not marker.exists()
    assert result.finish_reason == "stop"
