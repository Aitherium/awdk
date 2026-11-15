"""G2 — a registry pack's persona must reach the awdk runtime.

The pack importer writes the authored prompt to ``brain_pack.yaml`` and keeps
``agent.yaml`` as a portable manifest whose model/tools sit under ``spec``. The
runtime read only a TOP-LEVEL ``system_prompt``, so an agent installed from a
registry pack silently answered as the generic ``AitherAgent``. These tests call
the resolver ``adk.server.get_agent`` now uses.
"""

from __future__ import annotations

import yaml

from adk.server import _load_persona_spec


def _write_pack(root, name, *, agent_yaml, brain):
    pack = root / name
    pack.mkdir(parents=True)
    (pack / "agent.yaml").write_text(
        yaml.safe_dump(agent_yaml, sort_keys=False), encoding="utf-8")
    if brain is not None:
        (pack / "brain_pack.yaml").write_text(
            yaml.safe_dump(brain, sort_keys=False), encoding="utf-8")
    return pack


def test_brain_pack_persona_is_used_when_agent_yaml_has_none(tmp_path):
    pack = _write_pack(
        tmp_path,
        "recipe-agent",
        agent_yaml={
            "apiVersion": "aitheros/v1",
            "kind": "AgentPack",
            "metadata": {"name": "recipe-agent"},
            "spec": {"runtime": {"model": "claude-haiku-4-5-20251001"}},
        },
        brain={"identity": "recipe-agent", "system_prompt": "You are the recipe agent."},
    )

    spec = _load_persona_spec(pack, "recipe-agent")

    assert spec["system_prompt"] == "You are the recipe agent."


def test_brain_pack_of_another_identity_is_refused(tmp_path):
    pack = _write_pack(
        tmp_path,
        "recipe-agent",
        agent_yaml={"name": "recipe-agent"},
        brain={"identity": "someone-else", "system_prompt": "NOT THIS AGENT"},
    )

    spec = _load_persona_spec(pack, "recipe-agent")

    assert "system_prompt" not in spec


def test_top_level_prompt_still_wins(tmp_path):
    pack = _write_pack(
        tmp_path,
        "recipe-agent",
        agent_yaml={"name": "recipe-agent", "system_prompt": "TOP-LEVEL"},
        brain={"identity": "recipe-agent", "system_prompt": "BRAIN-PACK"},
    )

    spec = _load_persona_spec(pack, "recipe-agent")

    assert spec["system_prompt"] == "TOP-LEVEL"


def test_missing_brain_pack_is_not_an_error(tmp_path):
    pack = _write_pack(
        tmp_path,
        "recipe-agent",
        agent_yaml={"name": "recipe-agent"},
        brain=None,
    )

    assert _load_persona_spec(pack, "recipe-agent") == {"name": "recipe-agent"}
