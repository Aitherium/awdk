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


@pytest.mark.parametrize("content,tools", [
    ("I stopped: I kept repeating the same call and it gave nothing new.",
     ["codegraph_search", "ask_reasoner"]),
    ("Here is where it is defined.", ["codegraph_search[failed]", "file_search"]),
    ("I couldn't find it: the code graph is down.", ["codegraph_search", "file_search"]),
])
def test_a_failed_turn_saves_and_reinforces_no_skill(monkeypatch, tmp_path, content, tools):
    # 2026-10-14: a question that failed 16 times was saved as a skill (success_count 12)
    # whose only step was the give-up message.
    import asyncio

    from adk.agent import AitherAgent
    from adk.skills import Skill

    monkeypatch.setenv("AITHER_SKILLS", "true")
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    agent = AitherAgent(name="learner_fail", builtin_tools=False)
    agent._skills = SkillStore(data_dir=tmp_path)  # never the real ~/.aither/skills
    reused = Skill(name="old way", description="d", steps=["a", "b"],
                   tools_used=["x", "y"], success_count=1)
    agent._skills.save(reused)
    agent._recalled_skills = [reused]
    asyncio.run(agent._learn_after("where is split_near_question defined?", content, tools))
    names = [s.name for s in agent._skills.search("split_near_question defined", k=10)]
    assert not any("split_near_question" in n for n in names)
    assert agent._skills.load("old way").success_count == 1
