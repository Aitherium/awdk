"""P5 — adk continual learning: recall-before + learn-after skill hooks."""
import pytest

from adk.skills import SkillExtractor, SkillStore


def test_learn_after_extracts_and_persists_multi_tool_skill(tmp_path):
    """A successful multi-tool run yields a saved, searchable skill (the learn-after path)."""
    store = SkillStore(data_dir=tmp_path)
    ext = SkillExtractor()
    skill = ext.extract_from_session(
        messages=[
            {"role": "user", "content": "Research competitors and build a comparison table"},
            {"role": "assistant", "content": "Searched the web then compiled results.",
             "tool_calls": [{"name": "web_search"}, {"name": "build_table"}]},
        ],
        tools_called=["web_search", "build_table"],
    )
    assert skill is not None
    store.save(skill)
    # searchable for recall-before on a later run
    hits = store.search("research competitors", k=5)
    assert any(h.name == skill.name for h in hits)


def test_touch_reinforces_reused_skill(tmp_path):
    store = SkillStore(data_dir=tmp_path)
    from adk.skills import Skill
    s = Skill(name="deploy flow", description="deploy", steps=["build", "ship"],
              tools_used=["git", "deploy"], success_count=1)
    store.save(s)
    s.touch()                      # reused on a later run → success_count++
    store.save(s)
    assert store.load("deploy flow").success_count == 2


@pytest.mark.parametrize("env,expect", [("true", True), ("false", False), ("0", False)])
def test_agent_wires_skills_by_flag(monkeypatch, tmp_path, env, expect):
    monkeypatch.setenv("AITHER_SKILLS", env)
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    try:
        from adk.agent import AitherAgent
    except Exception:
        pytest.skip("adk agent import unavailable in this env")
    agent = AitherAgent(name="learner_test", builtin_tools=False)
    assert hasattr(agent, "_learn_after")            # learn-after hook present
    assert (agent._skills is not None) is expect      # recall/learn store gated by flag
