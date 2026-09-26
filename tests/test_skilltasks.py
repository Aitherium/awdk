"""adk.skilltasks: the acceptance gate can fail, freeze refuses late tests, the env conforms."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from adk.skilltasks import FreezeError, check_freeze, freeze, gate_task, load_task, reward_of
from adk.skilltasks.env import SUBMIT, SkillTaskEnv
from adk.skilltasks.gate import _make_task, self_test
from adk.skilltasks.rubric import parse_rubric


def test_gate_self_test_catches_every_planted_defect(capsys):
    assert self_test() == 0


def test_reward_is_zero_unless_every_outcome_test_passes():
    assert reward_of({"a": True, "b": False}, ["b"]) == (0.0, ["b"])
    assert reward_of({"a": True, "b": False}, ["a"]) == (0.5, [])
    assert reward_of({"a": True, "b": True}, ["a"]) == (1.0, [])


def test_good_task_is_accepted_with_oracle_one_and_nop_zero(tmp_path):
    rep = gate_task(_make_task(tmp_path, "good"))
    assert rep.accepted, rep.errors
    assert rep.oracle["reward"] == 1.0 and rep.nop["reward"] == 0.0
    assert rep.probes == {"clobber": pytest.approx(0.5)}  # wrote out.txt, lost the seed


def test_freeze_detects_an_edit_and_refuses_a_present_solution(tmp_path):
    t = _make_task(tmp_path, "t")
    assert check_freeze(t) == []
    (t / "tests" / "test.py").write_text("print('{}')\n", encoding="utf-8")
    assert any("changed after the freeze" in p for p in check_freeze(t))
    with pytest.raises(FreezeError):
        freeze(t)  # solution/ exists now


def test_freeze_ignores_crlf_only_drift(tmp_path):
    t = _make_task(tmp_path, "t")
    p = t / "tests" / "rubric.md"
    lf = p.read_bytes().replace(b"\r\n", b"\n")
    p.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert check_freeze(t) == []


def test_rubric_requires_three_sections_and_citations(tmp_path):
    p = tmp_path / "rubric.md"
    p.write_text("## Must-do\n- x [test: a]\n\n## Must-avoid\n- y\n\n## Best-practice\n- z\n",
                 encoding="utf-8")
    rub = parse_rubric(p)
    assert any("Must-avoid bullet cites no" in e for e in rub.problems)
    p.write_text("## Must-avoid\n- y [test: a]\n## Must-do\n- x [test: a]\n## Best-practice\n- z\n",
                 encoding="utf-8")
    assert any("exactly" in e for e in parse_rubric(p).problems)


def test_env_is_structurally_an_environment_and_scores_the_final_state(tmp_path):
    task = load_task(_make_task(tmp_path, "t"))
    env = SkillTaskEnv(task, max_submits=2)
    try:
        for name in ("observe", "act", "available_actions", "done", "primer", "tools", "render"):
            assert callable(getattr(env, name))
        assert env.available_actions() == [SUBMIT] and env.done() is False
        obs = env.act((SUBMIT, -1, -1))
        assert obs.level_up is False and obs.info["passed"] == 1 and not env.done()
        tools = env.tools(None)
        assert set(tools) == {"sh", "read", "write", "ls"}
        before = os.stat(env.ws.path / "seed.txt").st_ino
        assert "wrote" in env.write("seed.txt", "seed\n")
        assert os.stat(env.ws.path / "seed.txt").st_ino == before  # in place
        assert "escapes" in env.read("../private/x")
        env.write("out.txt", "42\n")
        obs = env.act((SUBMIT, -1, -1))
        assert obs.level_up and obs.done and obs.info["won"] is True
        assert env.final().reward == 1.0
    finally:
        env.close()


def test_env_conforms_to_the_reasoning_protocol_when_the_loop_is_installed(tmp_path):
    conformance = pytest.importorskip("adk.reasoning.solve.conformance")
    env = SkillTaskEnv(load_task(_make_task(tmp_path, "t")))
    try:
        assert conformance.check_environment(env, probe=True) == []
    finally:
        env.close()


def test_env_ends_the_episode_after_max_submits(tmp_path):
    env = SkillTaskEnv(load_task(_make_task(tmp_path, "t")), max_submits=1)
    try:
        obs = env.act((SUBMIT, -1, -1))
        assert obs.died and obs.done and obs.info["won"] is False
    finally:
        env.close()


def test_run_refuses_a_non_local_model(tmp_path):
    from adk.skilltasks.run import RunRefused, run_task

    with pytest.raises(RunRefused):
        run_task(_make_task(tmp_path, "t"), model="deepseek-chat")


def test_cli_gate_exit_codes(tmp_path, capsys):
    from adk.skilltasks.__main__ import main

    assert main(["gate", str(tmp_path)]) == 2  # nothing to judge
    _make_task(tmp_path, "good")
    capsys.readouterr()
    assert main(["gate", str(tmp_path / "good"), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["exit_code"] == 0 and doc["tasks"][0]["accepted"] is True
    bad = tmp_path / "bad"
    bad.mkdir()
    _make_task(bad, "late", edit_after_freeze=True)
    assert main(["gate", str(bad)]) == 1
    assert Path(tmp_path / "good" / "tests" / "freeze.json").is_file()
