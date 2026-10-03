"""Reversible pack activation: a pack switched off leaves the agent exactly as it was.

Asserts the deliverable, not the mechanism: after activate -> deactivate, the
tool registry and the system prompt are byte-identical to before; while active,
the pack's tools, persona fragment and skills are all visible to the model.
"""

from __future__ import annotations

import textwrap

import pytest

from adk.pack_activation import LOAD_SKILL_TOOL, activate, deactivate
from adk.tool_pack_loader import ToolPackLoader


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("AITHER_EMBED_AUTODEPLOY", "0")
    monkeypatch.setenv("AITHER_TYPED_MEMORY", "false")
    monkeypatch.setenv("AITHER_SKILL_DIRS", "")
    import adk.private_companion as pc
    monkeypatch.setattr(pc, "get_companion_vault", lambda *a, **k: None, raising=False)


def _write_pack(root, pack_id="gbdev", skills=("gb-rom",), overwrite_tool=None):
    d = root / pack_id
    (d / "skills" / "gb-rom").mkdir(parents=True)
    src = textwrap.dedent('''
        def gb_build(src: str) -> str:
            """Compile a Game Boy ROM."""
            return "rom:" + src

        def register(registry):
            registry.register(gb_build)
    ''')
    if overwrite_tool:
        src += (f"    def {overwrite_tool}(path: str) -> str:\n"
                f"        return 'pack'\n"
                f"    registry.register({overwrite_tool})\n")
    src += "    return 1\n"
    (d / "__init__.py").write_text(src, encoding="utf-8")
    skill_list = "".join(f"\n  - {s}" for s in skills)
    (d / ".toolpack.yaml").write_text(
        f"id: {pack_id}\nname: GB Dev\ntool_modules: [{pack_id}]\n"
        f"persona_fragments:\n  - You can build Game Boy ROMs with gb_build.\n"
        f"skills:{skill_list or ' []'}\n", encoding="utf-8")
    (d / "skills" / "gb-rom" / "SKILL.md").write_text(
        "---\nname: gb-rom\ndescription: Build and run a GBC ROM with RGBDS.\n---\n"
        "# GB ROM\nStep 1: rgbasm.\n", encoding="utf-8")
    return d


def _agent():
    from adk.agent import AitherAgent
    return AitherAgent(name="pack-activation-test", load_packs=False, builtin_tools=True)


def _snapshot(agent):
    return dict(agent._tools._tools), agent.system_prompt


def _loader(tmp_path):
    loader = ToolPackLoader(extra_dirs=[tmp_path], enforce_entitlements=False)
    loader.discover()
    return loader


def test_activate_then_deactivate_is_byte_identical(tmp_path):
    _write_pack(tmp_path)
    loader = _loader(tmp_path)
    agent = _agent()
    tools_before, prompt_before = _snapshot(agent)

    act = activate(agent, loader.load_packs(["gbdev"])[0], loader)
    names = set(agent._tools._tools)
    assert {"gb_build", LOAD_SKILL_TOOL} <= names
    assert "You can build Game Boy ROMs" in agent.system_prompt
    assert "[PACK SKILLS]" in agent.system_prompt and "gb-rom" in agent.system_prompt
    assert act.skills_added == ["gb-rom"] and not act.skills_missing

    deactivate(agent, "gbdev")
    tools_after, prompt_after = _snapshot(agent)
    assert tools_after == tools_before
    assert prompt_after == prompt_before


@pytest.mark.asyncio
async def test_skill_body_loads_only_while_active(tmp_path):
    _write_pack(tmp_path)
    loader = _loader(tmp_path)
    agent = _agent()
    activate(agent, loader.load_packs(["gbdev"])[0], loader)
    out = await agent._tools.execute(LOAD_SKILL_TOOL, {"name": "gb-rom"})
    assert "Step 1: rgbasm." in out
    deactivate(agent, "gbdev")
    assert agent._tools.get(LOAD_SKILL_TOOL) is None


def test_overwritten_tool_is_restored(tmp_path):
    _write_pack(tmp_path, overwrite_tool="file_read")
    loader = _loader(tmp_path)
    agent = _agent()
    original = agent._tools.get("file_read")
    assert original is not None
    activate(agent, loader.load_packs(["gbdev"])[0], loader)
    assert agent._tools.get("file_read") is not original
    deactivate(agent, "gbdev")
    assert agent._tools.get("file_read") is original


def test_missing_skill_is_reported_not_dropped(tmp_path):
    _write_pack(tmp_path, skills=("gb-rom", "does-not-exist"))
    loader = _loader(tmp_path)
    act = activate(_agent(), loader.load_packs(["gbdev"])[0], loader)
    assert act.skills_missing == ["does-not-exist"]


def test_agent_methods_round_trip(tmp_path):
    _write_pack(tmp_path)
    agent = _agent()
    tools_before, prompt_before = _snapshot(agent)
    on = agent.activate_pack("gbdev", packs_dir=str(tmp_path))
    assert "gb_build" in on["tools"] and on["skills"] == ["gb-rom"]
    off = agent.deactivate_pack("gbdev")
    assert off["pack"] == "gbdev"
    assert _snapshot(agent) == (tools_before, prompt_before)
    assert "error" in agent.deactivate_pack("gbdev")


def test_skill_entry_cannot_escape_pack(tmp_path):
    from adk.pack_activation import resolve_skill
    d = _write_pack(tmp_path)
    assert resolve_skill("../../etc/passwd", d) is None
    assert resolve_skill("/etc/passwd", d) is None
