"""ADK_BUILTIN_TOOL_CATEGORIES narrows the builtin tool set for a whole process.

Measured 2026-09-21: the daemon identity registers 14 categories (48-55 tools) whose
OpenAI schema is ~5.5k tokens, a third of a 16,384-token slot, on every call. A named
toolpack drops that tax without touching the agent's code."""
from __future__ import annotations

from adk.builtin_tools import (
    TOOL_CATEGORIES,
    TOOLPACK_ALIASES,
    categories_from_env,
    register_builtin_tools,
)


def test_unset_or_blank_means_the_identity_default():
    assert categories_from_env("") is None
    assert categories_from_env("  ,  ") is None


def test_an_alias_expands_and_plain_categories_pass_through():
    assert categories_from_env("coding") == TOOLPACK_ALIASES["coding"]
    assert categories_from_env("file_io, git") == ["file_io", "git"]
    assert categories_from_env("coding,web") == TOOLPACK_ALIASES["coding"] + ["web"]
    assert categories_from_env("file_io,file_io") == ["file_io"], "no duplicates"


def test_unknown_names_are_dropped_and_a_dead_entry_is_none():
    assert categories_from_env("file_io,nope") == ["file_io"]
    assert categories_from_env("nope") is None, "a typo never leaves an agent with zero tools"
    for alias, cats in TOOLPACK_ALIASES.items():
        assert all(c in TOOL_CATEGORIES or c == "self" for c in cats), alias


def test_env_narrows_the_registered_set(monkeypatch):
    from adk import AitherAgent

    monkeypatch.delenv("ADK_BUILTIN_TOOL_CATEGORIES", raising=False)
    full = AitherAgent("adk-daemon", user_mcp=False, builtin_tools=False)
    n_full = register_builtin_tools(full)
    monkeypatch.setenv("ADK_BUILTIN_TOOL_CATEGORIES", "coding")
    narrow = AitherAgent("adk-daemon", user_mcp=False, builtin_tools=False)
    n_narrow = register_builtin_tools(narrow)
    assert 0 < n_narrow < n_full, (n_narrow, n_full)
    names = {t.name for t in narrow.tools.list_tools()}
    assert {"file_read", "file_edit"} <= names
    assert not any(n.startswith("workspace_") for n in names), "workspace tools are out"
    # explicit categories still beat the env
    monkeypatch.setenv("ADK_BUILTIN_TOOL_CATEGORIES", "coding")
    explicit = AitherAgent("adk-daemon", user_mcp=False, builtin_tools=False)
    assert register_builtin_tools(explicit, categories=["file_io"]) < n_narrow
