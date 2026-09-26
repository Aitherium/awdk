"""The debugging environment (``envs/debug.py``): make a failing test pass.

The first non-ARC domain. The world is a scratch copy of a source tree; staging a
patch is a REPL tool, applying one is an action, and every action re-runs the
tests. These tests drive it directly and through ``solve()`` with a scripted
ModelBackend (no network).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, List

import pytest

np = pytest.importorskip("numpy")

from adk.core.model import ModelResponse  # noqa: E402
from adk.reasoning.solve import Budget, LoopConfig, solve  # noqa: E402
from adk.reasoning.solve.conformance import check_environment  # noqa: E402
from adk.reasoning.solve.envs.debug import DEMO_FILES, FailingTestEnv  # noqa: E402

FIX_REPLY = """Fix the off-by-one in mean.
```python
print(read_file("calc.py"))
pid = stage_patch("calc.py", "(len(xs) + 1)", "len(xs)")
act(1, pid, 0)
```"""

WRONG_REPLY = """Try a different denominator.
```python
pid = stage_patch("calc.py", "(len(xs) + 1)", "(len(xs) + 2)")
act(1, pid, 0)
```"""


class Scripted:
    def __init__(self, replies: List[str]) -> None:
        self.name = self.model = "scripted"
        self.replies = replies
        self.calls: List[Any] = []

    async def generate(self, messages, *, temperature=0.7, max_tokens=None, **opts):
        self.calls.append(list(messages))
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return ModelResponse(
            text=text,
            model=self.model,
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 10},
        )

    async def stream(self, messages, **kw):  # pragma: no cover - unused
        raise NotImplementedError


def test_demo_conforms_and_starts_failing():
    with FailingTestEnv.demo() as env:
        assert check_environment(env, probe=True) == []
        s = env.observe().state
        assert s.shape == (1, 5) and s[0, 0] == 1 and s[0, 1] == 2 and s[0, 2] == 0
        assert env.available_actions() == [1, 2, 3] and env.needs_xy(1) and not env.needs_xy(3)
        assert sorted(env.list_files()) == sorted(DEMO_FILES)
        assert "stage_patch" in env.tools(None) and "debugging" in env.primer()


def test_correct_patch_wins_wrong_patch_does_not_and_revert_restores():
    with FailingTestEnv.demo() as env:
        bad = env.stage_patch("calc.py", "(len(xs) + 1)", "(len(xs) + 2)")
        o = env.act((1, bad, 0))
        assert not o.level_up and not env.done() and o.state[0, 0] == 1 and o.state[0, 3] == 1
        o = env.act((2, -1, -1))
        assert env.read_file("calc.py") == DEMO_FILES["calc.py"] and o.state[0, 3] == 0
        good = env.stage_patch("calc.py", "(len(xs) + 1)", "len(xs)")
        o = env.act((1, good, 0))
        assert o.level_up and o.done and o.info["won"] and env.done() and o.state[0, 2] == 2
        assert "tests PASS" in env.describe(type("T", (), {"i": 2})())


def test_patches_cannot_leave_the_tree_and_must_match_once():
    with FailingTestEnv.demo() as env:
        for path in ("../evil.py", "/etc/passwd", "C:/x.py", "a/../../b.py"):
            with pytest.raises(ValueError):
                env.stage_patch(path, "", "x = 1\n")
        with pytest.raises(ValueError, match="occurs 0 times"):
            env.stage_patch("calc.py", "not there", "x")
        with pytest.raises(ValueError, match="exists"):
            env.stage_patch("calc.py", "", "x")
        o = env.act((1, 99, 0))
        assert "no staged patch 99" in o.info["note"] and env.patches == []


def test_the_callers_tree_is_never_modified(tmp_path):
    for rel, text in DEMO_FILES.items():
        (tmp_path / rel).write_text(text, encoding="utf-8")
    env = FailingTestEnv(root=str(tmp_path))
    try:
        env.act((1, env.stage_patch("calc.py", "(len(xs) + 1)", "len(xs)"), 0))
        assert env.done()
    finally:
        env.close()
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == DEMO_FILES["calc.py"]
    assert not env.root.exists()


def test_the_test_process_gets_a_scrubbed_environment(monkeypatch):
    monkeypatch.setenv("ADK_DEBUG_SENTINEL_TOKEN", "must-not-leak")
    files = {
        "test_env.py": (
            "import os, unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_no_secret(self):\n"
            "        self.assertNotIn('ADK_DEBUG_SENTINEL_TOKEN', os.environ)\n"
        )
    }
    with FailingTestEnv(files) as env:
        assert env.observe().state[0, 0] == 0, env.last_output


def test_a_hanging_test_command_times_out():
    with FailingTestEnv(
        {"x.py": ""}, test_cmd=[sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5
    ) as env:
        assert env.observe().state[0, 0] == 1 and "TIMED OUT" in env._verdict()


@pytest.mark.parametrize("sase", [False, True])
def test_solve_fixes_the_failing_test_through_a_modelbackend(sase):
    env = FailingTestEnv.demo()
    model = Scripted([FIX_REPLY])
    res = asyncio.run(
        solve(
            env,
            model,
            goal="make test_calc.py pass",
            config=LoopConfig(sase=sase, budget=Budget(max_llm_calls=3)),
        )
    )
    env.close()
    assert res.finish_reason == "won" and res.won and res.exit_code == 0
    assert res.actions == 1 and res.llm_calls == 1
    assert "GOAL: make test_calc.py pass" in model.calls[0][0].content
    assert "stage_patch" in model.calls[0][0].content


def test_solve_with_only_wrong_patches_is_not_a_win():
    env = FailingTestEnv.demo()
    res = asyncio.run(
        solve(
            env,
            Scripted([WRONG_REPLY]),
            config=LoopConfig(sase=False, budget=Budget(max_llm_calls=2)),
        )
    )
    env.close()
    assert not res.won and res.exit_code == 1 and res.finish_reason == "budget:llm_calls"
    assert res.actions == 1  # the second turn's stage_patch finds no "(len(xs) + 1)" left
