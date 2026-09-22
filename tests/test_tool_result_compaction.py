"""Append-time compaction of tool results in ToolRegistry.execute.

Every tool result -- native loop, ReAct loop, streaming -- passes through
`ToolRegistry.execute`, so a long result is shrunk THERE, before it becomes a
message. That is what keeps this cache-neutral: nothing earlier in the history is
edited, so no prompt-cache prefix is invalidated (the failure mode of history-
editing compaction hooks). The lines that decide what happens next -- the error,
the summary, the exit code -- are kept by rule, with or without a door.
"""

from __future__ import annotations

import asyncio

import pytest

from adk.tools import ToolRegistry

PYTEST_LOG = (
    ["============================= test session starts ============================="]
    + [f"tests/test_x.py::test_{i} PASSED                                    [ {i}%]" for i in range(300)]
    + [
        "___________________________ test_boom ___________________________",
        "E   AssertionError: boom",
        "1 failed, 300 passed in 4.20s",
    ]
)


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    def run_tests(command: str = "pytest -q") -> str:
        return "\n".join(PYTEST_LOG)

    def short(command: str = "") -> str:
        return "ok\n"

    reg.register(run_tests, name="bash", description="run a shell command")
    reg.register(short, name="short", description="short")
    return reg


def test_long_result_is_compacted_and_keeps_the_lines_that_matter(monkeypatch):
    monkeypatch.delenv("AITHER_COMPACT_TOOL_RESULTS", raising=False)
    out = asyncio.run(_registry().execute("bash", {"command": "pytest -q tests"}))
    assert out.count("\n") < len(PYTEST_LOG) - 50, "a 300-line PASSED run should shrink"
    assert "AssertionError: boom" in out
    assert "1 failed, 300 passed" in out


def test_short_result_untouched(monkeypatch):
    monkeypatch.delenv("AITHER_COMPACT_TOOL_RESULTS", raising=False)
    assert asyncio.run(_registry().execute("short", {})) == "ok\n"


@pytest.mark.parametrize("off", ["0", "false", "off"])
def test_kill_switch_returns_the_original(monkeypatch, off):
    monkeypatch.setenv("AITHER_COMPACT_TOOL_RESULTS", off)
    out = asyncio.run(_registry().execute("bash", {"command": "pytest -q"}))
    assert out == "\n".join(PYTEST_LOG)


def test_compactor_failure_never_eats_the_result(monkeypatch):
    monkeypatch.delenv("AITHER_COMPACT_TOOL_RESULTS", raising=False)
    import adk.shell.mods.compact_tool_output as mod

    def boom(*_a, **_k):
        raise RuntimeError("door on fire")

    monkeypatch.setattr(mod, "on_tool_result", boom)
    out = asyncio.run(_registry().execute("bash", {"command": "pytest -q"}))
    assert out == "\n".join(PYTEST_LOG)
