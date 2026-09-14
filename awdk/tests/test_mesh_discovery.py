"""Mesh agent discovery: local-agents parsing, chat-protocol inference, merge,
and fail-closed behavior (no owner token -> cloud registry skipped, not fabricated)."""

import json

import pytest

from adk.mesh_discovery import AgentRef, _load_local_agents, _merge, discover_agents


def test_chat_protocol_inference():
    # raw inference backends -> openai protocol
    for hint in ("llamacpp", "ollama", "vllm", "lmstudio", "OpenAI"):
        assert AgentRef(name="x", provider_hint=hint).chat_protocol == "openai"
    # a registered adk agent (no raw-backend hint) -> adk protocol
    assert AgentRef(name="x", provider_hint="").chat_protocol == "adk"
    assert AgentRef(name="x", provider_hint="genesis").chat_protocol == "adk"
    # explicit override always wins
    assert AgentRef(name="x", provider_hint="llamacpp",
                    chat_protocol_override="adk").chat_protocol == "adk"


def test_load_local_agents_dict_form(tmp_path, monkeypatch):
    # agents.json as written by the adk daemon: a DICT keyed by name.
    f = tmp_path / "agents.json"
    f.write_text(json.dumps({"agents": {
        "optiplex": {"name": "optiplex", "invoke_url": "http://192.168.1.121:8090",
                     "model": "Bonsai-27B", "provider_hint": "llamacpp",
                     "chat_protocol": "openai", "status": "online"},
    }}), encoding="utf-8")
    monkeypatch.setenv("AITHER_AGENTS_FILE", str(f))
    agents = _load_local_agents()
    assert len(agents) == 1
    a = agents[0]
    assert a.name == "optiplex" and a.invoke_url.endswith(":8090")
    assert a.chat_protocol == "openai" and a.model == "Bonsai-27B"


def test_load_local_agents_list_form(tmp_path, monkeypatch):
    f = tmp_path / "agents.json"
    f.write_text(json.dumps([{"name": "a", "url": "http://h:1"}]), encoding="utf-8")
    monkeypatch.setenv("AITHER_AGENTS_FILE", str(f))
    agents = _load_local_agents()
    assert len(agents) == 1 and agents[0].invoke_url == "http://h:1"


def test_load_local_agents_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_AGENTS_FILE", str(tmp_path / "nope.json"))
    assert _load_local_agents() == []


def test_merge_registry_authoritative():
    a2a = [AgentRef(name="x", skills=["s1"], public_key="pk", source="a2a-fleet")]
    local = [AgentRef(name="x", invoke_url="http://local", source="local")]
    registry = [AgentRef(name="x", invoke_url="http://reg", reach="tunnel",
                         model="m", provider_hint="llamacpp", source="registry")]
    merged = _merge(a2a, local, registry)
    assert len(merged) == 1
    m = merged[0]
    assert m.source == "registry"            # registry wins the source label
    assert m.invoke_url == "http://reg"      # registry authoritative for invoke_url
    assert m.reach == "tunnel" and m.model == "m"
    assert m.skills == ["s1"] and m.public_key == "pk"  # a2a-fleet fills skills/key


@pytest.mark.asyncio
async def test_discover_fail_closed_no_token(tmp_path, monkeypatch):
    # No owner token -> cloud registry skipped with a warning, never fabricated.
    monkeypatch.delenv("AITHER_API_KEY", raising=False)
    monkeypatch.delenv("AITHER_PORTAL_TOKEN", raising=False)
    monkeypatch.setenv("AITHER_AGENTS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))       # no saved config token
    # load_saved_config() reads ~/.aither/config.json via Path.home() (NOT AITHER_HOME),
    # so on a dev box with a real saved token env-clearing is not enough — neutralize
    # the token resolver itself so the fail-closed assertion is deterministic.
    import adk.mesh_discovery as md
    monkeypatch.setattr(md, "_owner_token", lambda: "")
    # Point the portal at an unroutable host so a2a-fleet best-effort returns fast/empty.
    monkeypatch.setenv("AITHER_PORTAL_URL", "http://127.0.0.1:9")
    agents, warnings = await discover_agents()
    assert agents == []
    assert any("owner token" in w.lower() for w in warnings)
