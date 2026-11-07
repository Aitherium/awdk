"""The agent-facing Strata tools (strata_list / strata_read / strata_write).

These assert the three things that make the tool safe to hand a model:

* the TENANT comes from the agent's configured identity — a `tenant:` /
  `aither://` path prefix or a `tenant=` keyword naming another tenant is REFUSED
  and the client is never called;
* a large read is CLIPPED with a range hint instead of flooding the context;
* the category is OFF unless the agent asks for it, and writes are a second,
  separate opt-in.

Every arm uses a fake client, so nothing here touches a real backend.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adk.builtin_tools import (
    IDENTITY_DEFAULTS,
    STRATA_READ_CAP,
    STRATA_READ_CAP_MAX,
    STRATA_TOOLS_ENV,
    STRATA_WRITE_ENV,
    _register_strata_tools,
    categories_from_env,
    make_strata_tools,
    register_builtin_tools,
    strata_tenant_for,
)
from adk.strata import parse_path
from adk.tools import ToolRegistry


class FakeStrata:
    """The slice of the Strata API these tools use, keyed by (tenant, key)."""

    def __init__(self, files=None, default_tenant="default"):
        self.files = dict(files or {})
        self.calls = []
        self.default_tenant = default_tenant
        self.write_result = True

    def _split(self, path):
        return parse_path(path, default_tenant=self.default_tenant)

    async def read(self, path, **kwargs):
        tenant, key = self._split(path)
        self.calls.append(("read", path, tenant))
        value = self.files.get((tenant, key))
        if value is None:
            return None
        return value if isinstance(value, bytes) else value.encode("utf-8")

    async def write(self, path, data, **kwargs):
        tenant, key = self._split(path)
        self.calls.append(("write", path, tenant))
        if not self.write_result:
            return False
        self.files[(tenant, key)] = data if isinstance(data, str) else data.decode("utf-8")
        return True

    async def list(self, prefix="", tenant="", **kwargs):
        self.calls.append(("list", prefix, tenant))
        keys = [k for (t, k) in self.files
                if t == (tenant or self.default_tenant) and k.startswith(prefix)]
        return sorted(keys)


class FakeAgent:
    """A real ToolRegistry + a fake Strata client; nothing else is touched."""

    def __init__(self, name="tester", tenant="", files=None):
        self.name = name
        self._tools = ToolRegistry()
        self._identity = SimpleNamespace(raw={"tenant": tenant} if tenant else {})
        self.strata = FakeStrata(files=files)

    def names(self):
        return {t.name for t in self._tools.list_tools()}


def _registry(tools):
    reg = ToolRegistry()
    for fn in tools:
        reg.register(fn)
    return reg


def _run(reg, name, arguments=None):
    import asyncio

    raw = asyncio.run(reg.execute(name, arguments or {}))
    return json.loads(raw)


def _tools(fake, tenant="acme"):
    return make_strata_tools(tenant, client_getter=lambda: fake)


# ── happy arms ──────────────────────────────────────────────────────────────


class TestHappyArms:
    def test_list_returns_only_the_bound_tenants_keys(self):
        fake = FakeStrata(files={
            ("acme", "codegraph/index.json"): "{}",
            ("acme", "artifacts/run-1.md"): "x",
            ("other", "codegraph/index.json"): "secret",
        })
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_list")
        assert out["tenant"] == "acme"
        assert out["keys"] == ["artifacts/run-1.md", "codegraph/index.json"]
        assert out["count"] == 2
        assert out["truncated"] is False
        assert fake.calls == [("list", "", "acme")]

    def test_list_filters_by_prefix_and_caps_the_page(self):
        fake = FakeStrata(files={("acme", f"a/{n}.txt"): "x" for n in range(5)})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_list", {"prefix": "a/", "limit": 3})
        assert out["prefix"] == "a/"
        assert len(out["keys"]) == 3
        assert out["truncated"] is True

    def test_read_returns_content_and_metadata(self):
        fake = FakeStrata(files={("acme", "artifacts/run-1.md"): "hello strata"})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read", {"path": "artifacts/run-1.md"})
        assert out["content"] == "hello strata"
        assert out["chars"] == 12
        assert out["tenant"] == "acme"
        assert out["truncated"] is False
        assert "hint" not in out
        assert fake.calls == [("read", "tenant:acme/artifacts/run-1.md", "acme")]

    def test_read_missing_key_names_the_key_and_a_next_step(self):
        reg = _registry(_tools(FakeStrata()))
        out = _run(reg, "strata_read", {"path": "nope.md"})
        assert "no such key: nope.md" in out["error"]
        assert "strata_list" in out["hint"]

    def test_write_is_refused_without_the_opt_in(self, monkeypatch):
        monkeypatch.delenv(STRATA_WRITE_ENV, raising=False)
        fake = FakeStrata()
        tools = _tools(fake)
        reg = _registry(tools)
        out = _run(reg, "strata_write", {"path": "a.md", "content": "hi"})
        assert out["error"] == "strata_write is disabled"
        assert STRATA_WRITE_ENV in out["fix"]
        assert fake.calls == [], "a refused write must not reach the backend"

    def test_write_persists_under_the_bound_tenant_when_opted_in(self, monkeypatch):
        monkeypatch.setenv(STRATA_WRITE_ENV, "1")
        fake = FakeStrata()
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_write", {"path": "artifacts/x.md", "content": "body"})
        assert out["ok"] is True
        assert out["tenant"] == "acme"
        assert out["bytes"] == 4
        assert fake.files[("acme", "artifacts/x.md")] == "body"

    def test_write_reports_a_backend_that_refused(self, monkeypatch):
        monkeypatch.setenv(STRATA_WRITE_ENV, "1")
        fake = FakeStrata()
        fake.write_result = False
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_write", {"path": "a.md", "content": "hi"})
        assert "write failed: a.md" in out["error"]
        assert "ok" not in out


# ── the tenant is never a tool argument ─────────────────────────────────────


class TestTenantIsNotAnArgument:
    @pytest.mark.parametrize("path", ["tenant:other/secret.md", "aither://other/secret.md"])
    def test_a_foreign_tenant_prefix_is_refused_before_any_call(self, path):
        fake = FakeStrata(files={("other", "secret.md"): "leak"})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read", {"path": path})
        assert "names tenant 'other'" in out["error"]
        assert out["tenant"] == "acme"
        assert "content" not in out
        assert fake.calls == []

    def test_list_refuses_a_foreign_tenant_prefix(self):
        fake = FakeStrata(files={("other", "x"): "leak"})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_list", {"prefix": "tenant:other/"})
        assert "names tenant 'other'" in out["error"]
        assert fake.calls == []

    def test_the_agents_own_tenant_prefix_is_accepted(self):
        fake = FakeStrata(files={("acme", "ok.md"): "fine"})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read", {"path": "tenant:acme/ok.md"})
        assert out["content"] == "fine"
        assert out["path"] == "ok.md"

    def test_a_tenant_keyword_is_refused_not_dropped(self):
        fake = FakeStrata(files={("other", "secret.md"): "leak"})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read", {"path": "secret.md", "tenant": "other"})
        assert "tenant= is not accepted" in out["error"]
        assert out["tenant"] == "acme"
        assert fake.calls == []

    def test_tenant_keyword_is_refused_on_write_and_list_too(self, monkeypatch):
        monkeypatch.setenv(STRATA_WRITE_ENV, "1")
        fake = FakeStrata()
        reg = _registry(_tools(fake))
        assert "tenant= is not accepted" in _run(
            reg, "strata_write", {"path": "a.md", "content": "x", "tenant": "other"}
        )["error"]
        assert "tenant= is not accepted" in _run(
            reg, "strata_list", {"tenant": "other"}
        )["error"]
        assert fake.calls == []

    def test_tenant_comes_from_the_identity_before_the_environment(self, monkeypatch):
        monkeypatch.setenv("AITHER_TENANT_SLUG", "env-tenant")
        agent = FakeAgent(tenant="acme")
        assert strata_tenant_for(agent) == "acme"

    def test_tenant_falls_back_to_the_configured_workspace_slug(self, monkeypatch):
        monkeypatch.setenv("AITHER_TENANT_SLUG", "env-tenant")
        assert strata_tenant_for(FakeAgent(tenant="")) == "env-tenant"

    def test_a_traversal_key_is_refused(self):
        fake = FakeStrata()
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read", {"path": "../../etc/passwd"})
        assert "not a normalised Strata key" in out["error"]
        assert fake.calls == []

    def test_no_tenant_is_advertised_in_the_schema(self, monkeypatch):
        """The model is never SHOWN a tenant parameter to try filling in."""
        monkeypatch.setenv(STRATA_WRITE_ENV, "1")
        agent = FakeAgent(tenant="acme")
        _register_strata_tools(agent)
        for name in ("strata_list", "strata_read", "strata_write"):
            props = agent._tools.get(name).parameters["properties"]
            assert "tenant" not in props, name
            assert "tenant_slug" not in props, name
            assert "_ignored" not in props, name


# ── clipping (Inline & Spill) ───────────────────────────────────────────────


class TestClipping:
    def test_a_big_read_is_clipped_to_the_first_n_chars_with_a_range_hint(self):
        body = "x" * (STRATA_READ_CAP * 2 + 17)
        fake = FakeStrata(files={("acme", "big.txt"): body})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read", {"path": "big.txt"})
        assert out["chars"] == len(body)
        assert out["content"] == body[:STRATA_READ_CAP]
        assert out["truncated"] is True
        assert out["next_offset"] == STRATA_READ_CAP
        assert "Inline & Spill" in out["hint"]
        assert f"offset={STRATA_READ_CAP}" in out["hint"]

    def test_the_hint_range_actually_continues_the_read(self):
        body = "a" * STRATA_READ_CAP + "TAIL"
        fake = FakeStrata(files={("acme", "big.txt"): body})
        reg = _registry(_tools(fake))
        first = _run(reg, "strata_read", {"path": "big.txt"})
        second = _run(reg, "strata_read",
                      {"path": "big.txt", "offset": first["next_offset"]})
        assert second["content"] == "TAIL"
        assert second["truncated"] is False

    def test_max_chars_is_capped(self):
        body = "y" * 1000
        fake = FakeStrata(files={("acme", "big.txt"): body})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read",
                   {"path": "big.txt", "max_chars": STRATA_READ_CAP_MAX + 1_000_000})
        assert len(out["content"]) == 1000
        assert out["truncated"] is False

    def test_an_offset_past_the_end_is_empty_and_says_so(self):
        fake = FakeStrata(files={("acme", "small.txt"): "abc"})
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read", {"path": "small.txt", "offset": 99})
        assert out["content"] == ""
        assert out["truncated"] is False
        assert out["offset"] == 99

    def test_a_binary_object_is_reported_not_mangled(self):
        fake = FakeStrata()
        fake.files[("acme", "blob.bin")] = b"\xff\xfe\x00"
        reg = _registry(_tools(fake))
        out = _run(reg, "strata_read", {"path": "blob.bin"})
        assert "binary object" in out["error"]
        assert "content" not in out


# ── the category is off unless asked for ────────────────────────────────────


class TestCategoryOffByDefault:
    def test_no_identity_profile_carries_strata(self):
        for name, cats in IDENTITY_DEFAULTS.items():
            assert "strata" not in cats, name

    def test_the_env_name_is_accepted_as_a_category(self):
        assert categories_from_env("strata") == ["strata"]

    def test_a_default_agent_gets_no_strata_tools(self, monkeypatch):
        monkeypatch.delenv(STRATA_TOOLS_ENV, raising=False)
        monkeypatch.delenv(STRATA_WRITE_ENV, raising=False)
        from adk import AitherAgent

        agent = AitherAgent("test", user_mcp=False, builtin_tools=False)
        register_builtin_tools(agent)
        assert not any(n.startswith("strata_") for n in
                       {t.name for t in agent._tools.list_tools()})

    def test_the_category_can_be_requested_explicitly(self, monkeypatch):
        monkeypatch.delenv(STRATA_TOOLS_ENV, raising=False)
        monkeypatch.delenv(STRATA_WRITE_ENV, raising=False)
        from adk import AitherAgent

        agent = AitherAgent("test", user_mcp=False, builtin_tools=False)
        n = register_builtin_tools(agent, categories=["strata"])
        names = {t.name for t in agent._tools.list_tools()}
        assert n == 2, "read + list; write is a separate opt-in"
        assert {"strata_list", "strata_read"} <= names
        assert "strata_write" not in names

    def test_the_env_enables_the_category(self, monkeypatch):
        monkeypatch.setenv(STRATA_TOOLS_ENV, "1")
        monkeypatch.delenv(STRATA_WRITE_ENV, raising=False)
        from adk import AitherAgent

        agent = AitherAgent("test", user_mcp=False, builtin_tools=False)
        register_builtin_tools(agent)
        names = {t.name for t in agent._tools.list_tools()}
        assert {"strata_list", "strata_read"} <= names
        assert "strata_write" not in names

    def test_the_full_category_fallback_does_not_leak_strata(self, monkeypatch):
        """auto=False with no category env registers EVERY category; strata is opt-in.

        The `categories = list(TOOL_CATEGORIES.keys())` fallback used to include the
        empty "strata" slot, so full registration reached the `if cat == "strata"`
        branch and bound the tools without any opt-in.
        """
        monkeypatch.delenv(STRATA_TOOLS_ENV, raising=False)
        monkeypatch.delenv(STRATA_WRITE_ENV, raising=False)
        monkeypatch.delenv("ADK_BUILTIN_TOOL_CATEGORIES", raising=False)
        from adk import AitherAgent

        agent = AitherAgent("test", user_mcp=False, builtin_tools=False)
        register_builtin_tools(agent, auto=False)
        names = {t.name for t in agent._tools.list_tools()}
        assert not any(n.startswith("strata_") for n in names), sorted(names)

    def test_the_env_enables_strata_on_the_full_fallback_too(self, monkeypatch):
        """The opt-in still works on the auto=False path, not only the identity one."""
        monkeypatch.setenv(STRATA_TOOLS_ENV, "1")
        monkeypatch.delenv(STRATA_WRITE_ENV, raising=False)
        monkeypatch.delenv("ADK_BUILTIN_TOOL_CATEGORIES", raising=False)
        from adk import AitherAgent

        agent = AitherAgent("test", user_mcp=False, builtin_tools=False)
        register_builtin_tools(agent, auto=False)
        names = {t.name for t in agent._tools.list_tools()}
        assert {"strata_list", "strata_read"} <= names
        assert "strata_write" not in names

    def test_write_joins_with_its_own_opt_in_and_declares_the_write_class(
            self, monkeypatch):
        monkeypatch.setenv(STRATA_WRITE_ENV, "1")
        agent = FakeAgent(tenant="acme")
        assert _register_strata_tools(agent) == 3
        assert agent.names() == {"strata_list", "strata_read", "strata_write"}
        assert agent._tools.get("strata_write").action_class == "write"
        assert agent._tools.get("strata_read").action_class == "read"

    def test_registration_binds_the_identity_tenant(self, monkeypatch):
        monkeypatch.delenv("AITHER_TENANT_SLUG", raising=False)
        fake_files = {("acme", "only.md"): "mine"}
        agent = FakeAgent(tenant="acme", files=fake_files)
        _register_strata_tools(agent)
        out = _run(agent._tools, "strata_list")
        assert out["keys"] == ["only.md"]
        assert agent.strata.calls == [("list", "", "acme")]
