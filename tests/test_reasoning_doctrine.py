"""The reasoning doctrine reaches every agent surface exactly once.

adk agents get it in ``Agent._turn_system_prompt``; foreign harnesses (claude,
codex, ...) get it in the daemon's ``system_prompt_append``; the agent harnesses
(aither, awdk, group) are skipped there because their own prompt builder adds it.
"""

from __future__ import annotations

from adk.reasoning_doctrine import doctrine_text, with_doctrine

# Reuse the daemon fixture from the sibling test module.
from tests.test_daemon_agent_skill_fields import _config, harness  # noqa: F401

MARK = "How to reason (the Six Pillars as habits, not modules)"


def test_doctrine_is_the_six_moves_without_markers():
    text = doctrine_text()
    assert text.startswith("## " + MARK)
    for move in ("Intent", "Context", "Reasoning", "Orchestration", "Creation", "Learning"):
        assert f"**{move}:" in text
    assert "reasoning-doctrine:" not in text


def test_with_doctrine_is_idempotent():
    once = with_doctrine("You are X.")
    assert once.startswith("You are X.") and MARK in once
    assert with_doctrine(once) == once
    assert with_doctrine("") == doctrine_text()


def test_env_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("ADK_REASONING_DOCTRINE", "0")
    assert doctrine_text() == ""
    assert with_doctrine("You are X.") == "You are X."


def test_agent_turn_prompt_carries_it_before_the_live_block(monkeypatch):
    from adk.agent import AitherAgent

    monkeypatch.setattr(
        AitherAgent, "_situation_suffix",
        lambda self, extra=None: "\n\n[AGENT HOST]live[/AGENT HOST]",
    )
    monkeypatch.setattr(
        AitherAgent, "system_prompt", property(lambda self: "You are the test agent."),
    )
    agent = AitherAgent.__new__(AitherAgent)
    prompt = agent._turn_system_prompt()
    assert prompt.startswith("You are the test agent.")
    assert prompt.index(MARK) < prompt.index("[AGENT HOST]")
    assert prompt.count(MARK) == 1


def test_foreign_harness_gets_it_agent_harness_does_not(harness):  # noqa: F811
    client = harness["client"]
    claude = client.post("/sessions", json={
        "harness": "claude", "cwd": "", "system_prompt_append": "Be terse.",
    })
    assert claude.status_code == 200, claude.text
    cfg = _config(harness, claude.json()["id"])
    assert cfg.system_prompt_append.startswith("Be terse.")
    assert cfg.system_prompt_append.count(MARK) == 1

    awdk = client.post("/sessions", json={"harness": "awdk", "cwd": "", "agent": "atlas"})
    assert awdk.status_code == 200, awdk.text
    assert MARK not in (_config(harness, awdk.json()["id"]).system_prompt_append or "")
