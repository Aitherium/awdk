"""The local daemon runs the default agent (2026-10-25: it answered "I am adk-daemon,
an AI agent" with no persona, will or default packs)."""
from pathlib import Path

from adk.identity import load_identity


def test_daemon_name_loads_the_default_identity_and_keeps_its_name(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no user override
    ident = load_identity("adk-daemon")
    aither = load_identity("aither")
    assert ident.name == "adk-daemon"
    assert ident.system_prompt and ident.system_prompt == aither.system_prompt
    assert ident.priority == aither.priority and ident.skills == aither.skills
    # the default packs come with it (agent.py reads identity.raw tools.packs)
    assert ident.raw.get("tools", {}).get("packs") == aither.raw["tools"]["packs"]


def test_an_unknown_name_still_gets_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ident = load_identity("no-such-agent-9f3f")
    assert ident.name == "no-such-agent-9f3f" and not ident.system_prompt
