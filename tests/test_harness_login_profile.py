"""A `uses_login` model profile binds models only: no endpoint, no token."""

from __future__ import annotations

import types

import pytest

from adk.harnesses import models


@pytest.fixture
def fake_tool(monkeypatch):
    tool = types.SimpleNamespace(
        load_profiles=lambda: {
            "fable": {
                "transport": "native",
                "uses_login": True,
                "model": "claude-fable-5-2",
                "opus_model": "claude-opus-5-5",
                "context_window": 1000000,
            },
            "nomodel": {"transport": "native", "uses_login": True},
        },
        resolve_auth=lambda profile: (None, "must not be asked"),
    )
    monkeypatch.setattr(models, "load_profile_tool", lambda: tool)
    return tool


def test_login_binding_names_models_only(fake_tool):
    binding = models.resolve_binding("fable")
    assert binding.env["ANTHROPIC_MODEL"] == "claude-fable-5-2"
    assert binding.env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "claude-fable-5-2"
    assert binding.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claude-opus-5-5"
    assert binding.env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-fable-5-2"
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_DISABLE_ARTIFACT"):
        assert var not in binding.env
    # The user settings file (where a machine-wide router env lives) is dropped.
    assert binding.claude_setting_sources() == "project,local"


def test_login_binding_strips_inherited_router_env(fake_tool):
    binding = models.resolve_binding("fable")
    env = models.apply_binding(
        {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": "x", "PATH": "p"},
        binding,
    )
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["PATH"] == "p"


def test_login_binding_without_model_is_loud(fake_tool):
    with pytest.raises(models.ProfileError):
        models.resolve_binding("nomodel")


def test_snapshot_finds_the_profile_tool_through_the_recorded_repo_root(tmp_path, monkeypatch):
    """A deployed snapshot has no AitherOS/ beside it; ~/.aither/repo-root names the checkout."""
    repo = tmp_path / "repo"
    tool = repo / "AitherOS" / "dev" / "tools" / "claude_model_profile.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("# stub\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".aither").mkdir(parents=True)
    (home / ".aither" / "repo-root").write_text(str(repo) + "\n", encoding="utf-8")
    snap = tmp_path / "snap" / "awdk" / "adk" / "harnesses" / "models.py"
    monkeypatch.setattr(models, "__file__", str(snap))
    monkeypatch.setattr(models.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("AITHEROS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert models.find_profile_tool() == tool
