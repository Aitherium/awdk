"""awsh_solve (the awsh MCP tool) and /solve (the shell plugin) over adk.reasoning.solve._shell.

Pins the MCP schema, the bounded child argv (limits clamped), the summary shape from a
fake ``adk solve --json`` run, and the exit-2 paths (no solve verb, no JSON, timeout).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import types

import pytest
from adk.harnesses import mcp_stdio
from adk.reasoning.solve import _shell

_RESULT = {
    "finish_reason": "won",
    "won": True,
    "levels": 2,
    "level_actions": [3, 4],
    "actions": 7,
    "turns": 3,
    "llm_calls": 3,
    "tokens": {"in": 10, "out": 5},
    "wall_s": 1.25,
    "calibration": 0.9,
    "strategy_trace": ["x"],
    "log_path": None,
    "error": None,
    "stats": {"big": list(range(50))},
    "hypotheses": [{"status": "active"}, {"status": "active"}, {"status": "refuted"}],
}


class _Runner:
    def __init__(self, rc=0, stdout="", stderr="", exc=None):
        self.rc, self.stdout, self.stderr, self.exc = rc, stdout, stderr, exc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc is not None:
            raise self.exc
        return types.SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr=self.stderr)


def test_tool_is_listed_with_its_schema():
    resp = mcp_stdio._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert "awsh_solve" in tools
    props = tools["awsh_solve"]["inputSchema"]["properties"]
    assert props["env"]["enum"] == ["toy", "arc"]
    for key in ("game", "max_calls", "max_actions", "wall_s", "tier", "backend", "model"):
        assert key in props, key
    assert "bounded" in tools["awsh_solve"]["description"].lower()


def test_argv_is_bounded_and_clamped():
    argv = _shell.build_solve_argv(
        {
            "env": "arc",
            "game": "ls20",
            "max_calls": 10_000,
            "max_actions": 7,
            "wall_s": 99_999,
            "tier": "reasoning",
        },
        python="PY",
    )
    assert argv[:4] == ["PY", "-m", "adk.cli", "solve"]
    assert argv[argv.index("--max-calls") + 1] == str(_shell.MAX_CALLS)
    assert argv[argv.index("--max-actions") + 1] == "7"
    assert float(argv[argv.index("--wall-s") + 1]) == _shell.MAX_WALL_S
    assert "--json" in argv and argv[argv.index("--game") + 1] == "ls20"
    assert argv[argv.index("--tier") + 1] == "reasoning"


def test_defaults_are_the_toy_env_and_finite_limits():
    argv = _shell.build_solve_argv({}, python="PY")
    assert argv[argv.index("--env") + 1] == "toy"
    assert int(argv[argv.index("--max-calls") + 1]) <= _shell.MAX_CALLS


@pytest.mark.parametrize("bad", [{"env": "chess"}, {"max_calls": 0}, {"wall_s": "soon"}])
def test_bad_args_are_exit_2_and_spawn_nothing(bad):
    runner = _Runner()
    out = _shell.run_bounded_solve(bad, runner=runner)
    assert out["exit_code"] == 2 and "bad arguments" in out["error"]
    assert runner.calls == []


def test_fake_run_through_the_mcp_call_returns_the_summary(monkeypatch):
    runner = _Runner(rc=0, stdout="progress line\n" + json.dumps(_RESULT) + "\n")
    real = _shell.run_bounded_solve
    monkeypatch.setattr(_shell, "run_bounded_solve", lambda a: real(a, runner=runner))
    resp = mcp_stdio._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "awsh_solve", "arguments": {"env": "toy", "max_calls": 3}},
        }
    )
    out = json.loads(resp["result"]["content"][0]["text"])
    assert out["won"] is True and out["exit_code"] == 0 and out["levels"] == 2
    assert out["hypotheses"] == {"active": 2, "refuted": 1}
    assert "stats" not in out  # the summary stays small
    argv, kw = runner.calls[0]
    assert argv[0] == sys.executable and argv[argv.index("--max-calls") + 1] == "3"
    assert kw["timeout"] == float(argv[argv.index("--wall-s") + 1]) + _shell.GRACE_S


def test_a_lost_run_keeps_the_child_exit_code():
    doc = dict(_RESULT, won=False, finish_reason="budget")
    out = _shell.run_bounded_solve({}, runner=_Runner(rc=1, stdout=json.dumps(doc)))
    assert out["exit_code"] == 1 and out["finish_reason"] == "budget"


def test_adk_without_the_solve_verb_is_exit_2_named():
    err = "adk: error: argument command: invalid choice: 'solve' (choose from 'start', ...)"
    out = _shell.run_bounded_solve({}, runner=_Runner(rc=2, stderr=err))
    assert out["exit_code"] == 2 and "no `solve` verb" in out["error"]


def test_no_json_is_never_success():
    out = _shell.run_bounded_solve({}, runner=_Runner(rc=0, stdout="hello\n"))
    assert out["exit_code"] == 2 and "no JSON" in out["error"]


def test_timeout_is_exit_2():
    exc = subprocess.TimeoutExpired(cmd="adk", timeout=1)
    out = _shell.run_bounded_solve({"wall_s": 5}, runner=_Runner(exc=exc))
    assert out["exit_code"] == 2 and "did not finish" in out["error"]


def test_solve_plugin_parses_and_renders(monkeypatch):
    from adk.shell.plugins.builtins import solve as plugin

    assert plugin.parse_solve_args(["arc", "ls20", "--max-calls", "5"]) == {
        "env": "arc",
        "game": "ls20",
        "max_calls": "5",
    }
    with pytest.raises(ValueError):
        plugin.parse_solve_args(["--nope", "1"])
    seen = {}

    def fake(args):
        seen.update(args)
        return {
            "exit_code": 0,
            "won": True,
            "finish_reason": "won",
            "levels": 1,
            "actions": 4,
            "turns": 2,
            "llm_calls": 2,
            "wall_s": 0.5,
            "hypotheses": {"active": 1},
        }

    monkeypatch.setattr(_shell, "run_bounded_solve", fake)
    p = plugin.SolvePlugin()
    assert p.name == "solve"
    text = asyncio.run(p.run(["toy", "--wall-s", "30"], {}))
    assert seen == {"env": "toy", "wall_s": "30"}
    assert "WON" in text and "active=1" in text


def test_arc_plugin_still_owns_arc():
    from adk.shell.plugins.builtins.arc import ArcPlugin

    assert ArcPlugin().name == "arc"
