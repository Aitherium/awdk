"""``adk solve`` and ``adk eval arc``: parsing, the held-out seed guard, exit codes.

Every test goes through the real argparse tree (``adk.cli._register_commands``)
and, for exit codes, through ``adk.cli.main`` itself, so it fails if either verb
is not registered or not dispatched. Models are fakes; nothing touches a network.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List

import pytest
from adk import cli
from adk.commands import eval_arc
from adk.commands import solve as solve_cmd
from adk.core.model import ModelResponse
from adk.evalharness.arc_agi3.rhae import ActionLedger
from adk.reasoning.solve import Action, Obs


def _parse(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="adk")
    sub = parser.add_subparsers(dest="command")
    cli._register_commands(sub)
    return parser.parse_args(argv)


def _main(monkeypatch: pytest.MonkeyPatch, argv: List[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["adk"] + argv)
    monkeypatch.setattr(cli, "_cached_parser", None, raising=False)
    with pytest.raises(SystemExit) as ei:
        cli.main()
    return int(ei.value.code or 0)


def _no_backend(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("the backend must not be built")


# ------------------------------------------------------------------ parsing
def test_solve_parses_every_flag() -> None:
    a = _parse(
        [
            "solve",
            "--env",
            "gridwalk",
            "--mode",
            "plain",
            "--prism",
            "off",
            "--budget",
            "7",
            "--max-actions",
            "30",
            "--backend",
            "reasoning",
            "--model",
            "m1",
            "--json",
        ]
    )
    assert a.command == "solve" and a.env == "gridwalk" and a.mode == "plain"
    assert a.prism == "off" and a.budget == 7 and a.max_actions == 30
    assert a.backend == "reasoning" and a.model == "m1" and a.json is True
    d = _parse(["solve"])
    assert (d.env, d.mode, d.prism, d.budget, d.backend) == (
        "counter",
        "sase",
        "on",
        40,
        "microscheduler",
    )


def test_eval_arc_parses_every_flag() -> None:
    a = _parse(
        [
            "eval",
            "arc",
            "--games",
            "ls20,ft09",
            "--seeds",
            "0,70",
            "--policy",
            "llm",
            "--cap",
            "30",
            "--out",
            "runs",
            "--env-dir",
            "g",
        ]
    )
    assert a.command == "eval" and a.eval_command == "arc"
    assert a.games == "ls20,ft09" and a.seeds == "0,70" and a.policy == "llm"
    assert a.cap == 30 and a.out == "runs" and a.env_dir == "g"
    assert eval_arc.parse_seeds(a.seeds) == [0, 70] and eval_arc.parse_seeds("3") == 3
    assert eval_arc.parse_seeds("10") == 10  # a COUNT: seeds 0..9, all legal


@pytest.mark.parametrize(
    "argv",
    [
        ["solve", "--mode", "fancy"],
        ["solve", "--prism", "maybe"],
        ["solve", "--env", "nope"],
        ["eval", "arc", "--policy", "explorer"],
        ["eval", "arc", "--cap", "many"],
    ],
)
def test_bad_arguments_exit_2(argv: List[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as ei:
        _parse(argv)
    # rejected by THIS verb's option, not because the verb is unknown
    assert ei.value.code == 2 and "argument %s" % argv[-2] in capsys.readouterr().err


# ------------------------------------------------------------------ seed guard
@pytest.mark.parametrize("seeds", ["11", "0,42", "69", "10,"])
def test_eval_arc_refuses_heldout_seeds_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, seeds: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(eval_arc, "build_backend", _no_backend)
    monkeypatch.setattr(eval_arc, "MAKE_ENV", _no_backend)
    rc = _main(monkeypatch, ["eval", "arc", "--policy", "llm", "--seeds", seeds])
    assert rc == 2 and "held-out" in capsys.readouterr().out


def test_eval_arc_allows_the_band_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_games(monkeypatch)
    assert _main(monkeypatch, ["eval", "arc", "--seeds", "9,70", "--games", "fake"]) in (0, 1)


def test_solve_refuses_a_heldout_arc_seed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(solve_cmd, "build_backend", _no_backend)
    rc = _main(monkeypatch, ["solve", "--env", "arc", "--game", "ls20", "--seed", "42"])
    assert rc == 2 and "held-out" in capsys.readouterr().out


# ------------------------------------------------------------------ adk solve exit codes
class ScriptedModel:
    """An async ModelBackend replaying one reply (or failing)."""

    def __init__(self, reply: str = "", fail: bool = False) -> None:
        self.name = self.model = "scripted"
        self.reply = reply
        self.fail = fail
        self.calls = 0

    async def generate(
        self, messages: Any, *, temperature: float = 0.7, max_tokens: Any = None, **opts: Any
    ) -> ModelResponse:
        self.calls += 1
        if self.fail:
            raise ConnectionError("scheduler refused")
        return ModelResponse(
            text=self.reply,
            model="scripted",
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


WIN = "Count up.\n```python\nfor _ in range(5):\n    act(1)\n```"
LOSE = "Count down.\n```python\nfor _ in range(3):\n    act(2)\n```"


@pytest.mark.parametrize("reply,fail,want", [(WIN, False, 0), (LOSE, False, 1), ("", True, 2)])
def test_solve_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    reply: str,
    fail: bool,
    want: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("numpy")
    model = ScriptedModel(reply, fail=fail)
    monkeypatch.setattr(solve_cmd, "build_backend", lambda args: model)
    rc = _main(
        monkeypatch,
        [
            "solve",
            "--env",
            "counter",
            "--mode",
            "plain",
            "--prism",
            "off",
            "--budget",
            "3",
            "--json",
        ],
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == want and out["exit_code"] == want and model.calls >= 1
    if want == 0:
        assert out["won"] and out["levels"] == 2
    if want == 1:
        assert not out["won"] and out["finish_reason"].startswith("budget")


def test_solve_dead_backend_at_build_is_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("numpy")

    def dead(args: Any) -> Any:
        from adk.core.backends.microscheduler import SchedulerUnavailableError

        raise SchedulerUnavailableError("connection refused")

    monkeypatch.setattr(solve_cmd, "build_backend", dead)
    assert _main(monkeypatch, ["solve", "--env", "counter"]) == 2
    assert "cannot judge (backend)" in capsys.readouterr().out


# ------------------------------------------------------------------ adk eval arc exit codes
class FakeEnv:
    """One level: three ACTION1 clear it. ACTION2 does nothing."""

    def __init__(self) -> None:
        self.ledger = ActionLedger()
        self.ledger.record(0, 0)
        self.count = 0
        self.levels_done = 0

    @property
    def actions(self) -> int:
        return self.ledger.actions

    @property
    def level_actions(self) -> List[int]:
        return list(self.ledger.level_actions)

    def observe(self) -> Obs:
        return Obs(state=self.count, level=self.levels_done, done=self.done())

    def act(self, action: Action, source: str = "model") -> Obs:
        if action[0] == 1:
            self.count += 1
        if self.count >= 3:
            self.levels_done = 1
        self.ledger.record(action[0], self.levels_done)
        o = self.observe()
        o.level_up = self.levels_done == 1
        return o

    def available_actions(self) -> List[int]:
        return [1, 2]

    def done(self) -> bool:
        return self.levels_done >= 1


def _fake_games(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eval_arc, "MAKE_ENV", lambda g, s: FakeEnv())
    monkeypatch.setattr(eval_arc, "BASELINES", {"fake": [3]})


class ChatModel:
    """A blocking-``chat`` model (the MicroScheduler shape)."""

    def __init__(self, reply: str = "", dead: bool = False) -> None:
        self.model = "chat-fake"
        self.reply = reply
        self.dead = dead
        self.calls = 0

    def chat(self, messages: Any, max_tokens: int = 400, temperature: float = 0.3) -> Any:
        if self.dead:
            from adk.core.backends.microscheduler import SchedulerUnavailableError

            raise SchedulerUnavailableError("503")
        self.calls += 1
        return type("R", (), {"content": self.reply})()


@pytest.mark.parametrize(
    "model,want",
    [
        (ChatModel("A1\nA1\nA1"), 0),  # clears the level at baseline: RHAE 1.0
        (ChatModel("A2\nA2"), 1),  # never progresses: capped, RHAE 0
        (ChatModel(dead=True), 2),  # dead on every row
        (ScriptedModel(fail=True), 2),  # async-only backend, never answered
    ],
)
def test_eval_arc_llm_policy_exit_codes(
    monkeypatch: pytest.MonkeyPatch, model: Any, want: int, tmp_path: Any
) -> None:
    _fake_games(monkeypatch)
    monkeypatch.setattr(eval_arc, "build_backend", lambda args: model)
    rc = _main(
        monkeypatch,
        [
            "eval",
            "arc",
            "--policy",
            "llm",
            "--games",
            "fake",
            "--seeds",
            "2",
            "--cap",
            "12",
            "--out",
            str(tmp_path),
        ],
    )
    assert rc == want
    rows = [
        json.loads(x)
        for p in tmp_path.glob("*.jsonl")
        for x in p.read_text(encoding="utf-8").splitlines()
    ]
    assert [r["seed"] for r in rows] == [0, 1]


def test_eval_arc_random_policy_json_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_games(monkeypatch)
    monkeypatch.setattr(eval_arc, "build_backend", _no_backend)  # random needs no model
    rc = _main(
        monkeypatch, ["eval", "arc", "--games", "fake", "--seeds", "3", "--cap", "40", "--json"]
    )
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["rows"] == 3 and summary["policy"] == "random"
    assert rc == summary["exit_code"] and rc in (0, 1)


def test_eval_arc_without_games_is_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ADK_ARC_ENV_DIR", raising=False)
    assert _main(monkeypatch, ["eval", "arc", "--games", "ls20"]) == 2
    assert "cannot judge" in capsys.readouterr().out


def test_eval_arc_solve_policy_dead_backend_is_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("numpy")
    _fake_games(monkeypatch)
    monkeypatch.setattr(eval_arc, "build_backend", lambda args: ScriptedModel(fail=True))
    assert (
        _main(
            monkeypatch,
            ["eval", "arc", "--policy", "solve", "--games", "fake", "--budget", "2", "--cap", "10"],
        )
        == 2
    )
    assert "dead on every row" in capsys.readouterr().out


def test_eval_arc_self_test_needs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eval_arc, "build_backend", _no_backend)
    assert _main(monkeypatch, ["eval", "arc", "--self-test"]) == 0


def test_sync_chat_marks_a_never_answering_backend_dead() -> None:
    from adk.commands._model_profile import BackendDeadError, SyncChat

    chat = SyncChat(ScriptedModel(fail=True))
    with pytest.raises(BackendDeadError) as ei:
        chat.chat([{"role": "user", "content": "hi"}])
    assert getattr(ei.value, "backend_dead", False)
    ok = SyncChat(ScriptedModel("A1"))
    assert ok.chat([{"role": "user", "content": "hi"}]).content == "A1"
    assert ok.stats()["llm_calls"] == 1 and ok.stats()["prompt_tokens"] == 10
