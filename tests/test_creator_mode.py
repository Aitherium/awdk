"""Creator mode: one session scaffolds a pack, switches it on, calls its tool, switches it off.

Driven through the agent's real ToolRegistry -- the same path a model's tool
calls take -- with no stub loader: the pack is scaffolded by pack_author,
validated, file-loaded and registered for real.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_EMBED_AUTODEPLOY", "0")
    monkeypatch.setenv("AITHER_TYPED_MEMORY", "false")
    monkeypatch.setenv("AITHER_CREATOR_PACKS_DIR", str(tmp_path / "creator"))
    monkeypatch.setenv("AITHER_CREATOR_MODE", "1")
    import adk.private_companion as pc
    monkeypatch.setattr(pc, "get_companion_vault", lambda *a, **k: None, raising=False)
    from adk.approval import set_runtime_gates
    yield
    set_runtime_gates("creator-test", ())


def _agent():
    from adk.agent import AitherAgent
    return AitherAgent(name="creator-test", load_packs=False, builtin_tools=False)


async def _call(agent, tool, **args):
    return json.loads(await agent._tools.execute(tool, args))


@pytest.mark.asyncio
async def test_author_load_use_unload_in_one_session(tmp_path):
    agent = _agent()
    names = {t.name for t in agent._tools.list_tools()}
    assert {"aw_pack_new", "aw_pack_validate", "aw_pack_on", "aw_pack_off"} <= names

    made = await _call(agent, "aw_pack_new", pack_id="tester.gbdev")
    tools_py = next(f for f in made["files"] if f.endswith("tools.py"))
    src = open(tools_py, encoding="utf-8").read()
    # the edit an agent makes between new and on: the scaffold tool now says something new
    with open(tools_py, "w", encoding="utf-8") as fh:
        fh.write(src.replace("return text[::-1]", "return 'GB:' + text", 1))

    assert (await _call(agent, "aw_pack_validate", pack_id="tester.gbdev"))["ok"] is True
    on = await _call(agent, "aw_pack_on", pack_id="tester.gbdev")
    assert on["tools"] == ["gbdev_echo"], on
    out = await agent._tools.execute("gbdev_echo", {"text": "rom"})
    assert "GB:rom" in out

    # edit again; on reloads from source, not from the cached module
    with open(tools_py, "w", encoding="utf-8") as fh:
        fh.write(src.replace("return text[::-1]", "return 'GBC:' + text", 1))
    await _call(agent, "aw_pack_on", pack_id="tester.gbdev")
    assert "GBC:rom" in await agent._tools.execute("gbdev_echo", {"text": "rom"})

    off = await _call(agent, "aw_pack_off", pack_id="tester.gbdev")
    assert off["pack"] == "tester.gbdev"
    assert agent._tools.get("gbdev_echo") is None
    assert await _call(agent, "aw_pack_active") == []


def test_loading_code_is_approval_gated():
    from adk.approval import needs_approval
    agent = _agent()
    assert needs_approval(agent.name, "aw_pack_on") is True
    assert needs_approval(agent.name, "aw_pack_validate") is False


@pytest.mark.asyncio
async def test_invalid_pack_never_reaches_import(tmp_path):
    agent = _agent()
    made = await _call(agent, "aw_pack_new", pack_id="tester.broken")
    init = next(f for f in made["files"] if f.endswith("__init__.py"))
    with open(init, "w", encoding="utf-8") as fh:
        fh.write("raise SystemExit('imported!')\n")  # no register() -> validation fails
    res = await _call(agent, "aw_pack_on", pack_id="tester.broken")
    assert res.get("error") == "validation failed" and res["findings"], res


def test_creator_off_by_default(monkeypatch):
    monkeypatch.delenv("AITHER_CREATOR_MODE", raising=False)
    agent = _agent()
    assert agent._tools.get("aw_pack_on") is None
