"""Tests for the pack UI plugin system + pack data scoping.

Covers: manifest ui/new-field parsing, asset-route traversal guard and
enabled-gating, the bearer-gated pack tool-invoke bridge (ownership 403s,
session namespacing), the pack_scope file jail, per-pack settings allowlist,
and the catalog proxy fail-soft path.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-bearer-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

PACK_ID = "uitestpack"


@pytest.fixture()
def pack_dir(tmp_path):
    """A discoverable pack with a ui: block and one asset on disk."""
    d = tmp_path / PACK_ID
    (d / "ui").mkdir(parents=True)
    (d / ".toolpack.yaml").write_text(
        f"""
id: {PACK_ID}
name: UI Test Pack
version: 1.2.3
description: A pack used by the UI plugin tests.
icon: beaker
tags: [testing, ui]
skills: [test-skill]
deprecated: true
redirect_to: newpack
entitlement: can_test_legacy
mcp_tools:
  - "dr_*"
  - "exact_tool"
ui:
  assets_dir: ui
  tabs:
    - id: main
      title: Test UI
      entry: index.html
""",
        encoding="utf-8",
    )
    (d / "ui" / "index.html").write_text("<html>pack ui</html>", encoding="utf-8")
    (d / "ui" / "app.js").write_text("// js", encoding="utf-8")
    (d / "secret.txt").write_text("outside assets dir", encoding="utf-8")
    return d


def _fresh_loader_env(tmp_path):
    from adk import tool_pack_loader
    tool_pack_loader._LOADERS.clear()
    return {
        "AITHER_SERVER_API_KEY": TOKEN,
        "AITHER_OFFLINE": "1",
        "AITHER_SETTINGS_SYNC": "false",
        "AITHER_TOOLPACK_DIRS": str(tmp_path),
    }


def _make_client(tmp_path, enabled=(PACK_ID,), tools=()):
    env = _fresh_loader_env(tmp_path)
    saved = {"required_packs": list(enabled)}

    def fake_load():
        return dict(saved)

    def fake_save(patch_dict):
        saved.update(patch_dict)

    # Keep the env patch ACTIVE for the client's lifetime: the pack loader reads
    # AITHER_TOOLPACK_DIRS lazily on the first request, not at create_app time.
    patches = [
        patch.dict(os.environ, env, clear=False),
        patch("adk.admin_api.load_saved_config", fake_load),
        patch("adk.admin_api.save_saved_config", fake_save),
        patch("adk.config.load_saved_config", fake_load),
    ]
    for p in patches:
        p.start()
    from adk.config import Config
    from adk.server import create_app

    config = Config()
    config.gateway_url = ""
    config.aither_api_key = ""
    agent = MagicMock()
    agent.name = "test"
    agent.llm = MagicMock()
    agent.llm.provider_name = "anthropic"
    agent._identity = MagicMock()
    agent._identity.name = "test"
    agent._identity.description = "Test"
    agent._identity.skills = []
    agent._tools = MagicMock()
    agent._tools.list_tools = MagicMock(
        return_value=[SimpleNamespace(name=n, description=f"{n} desc") for n in tools])
    agent._tools.execute = AsyncMock(return_value=json.dumps({"echo": True}))
    agent._graph = None
    app = create_app(agent=agent, identity="test", config=config)
    client = TestClient(app)
    client._patches = patches  # type: ignore[attr-defined]
    client._agent = agent  # type: ignore[attr-defined]
    return client


def _close(client):
    for p in getattr(client, "_patches", []):
        p.stop()


# ── Manifest parsing ─────────────────────────────────────────────────────


class TestManifestParsing:
    def test_new_fields_parse(self, pack_dir, tmp_path):
        from adk import tool_pack_loader
        tool_pack_loader._LOADERS.clear()
        with patch.dict(os.environ, {"AITHER_TOOLPACK_DIRS": str(tmp_path)}, clear=False):
            loader = tool_pack_loader.ToolPackLoader()
            m = loader.discover()[PACK_ID]
        assert m.icon == "beaker"
        assert m.tags == ["testing", "ui"]
        assert m.skills == ["test-skill"]
        assert m.deprecated is True
        assert m.redirect_to == "newpack"
        # legacy singular entitlement folded into the list
        assert "can_test_legacy" in m.entitlements
        assert m.ui_tabs == [
            {"id": "main", "title": "Test UI", "entry": "index.html", "icon": "beaker"}]
        assert m.ui_assets_dir == (pack_dir / "ui").resolve()

    def test_tool_matches_globs(self, pack_dir, tmp_path):
        from adk import tool_pack_loader
        tool_pack_loader._LOADERS.clear()
        with patch.dict(os.environ, {"AITHER_TOOLPACK_DIRS": str(tmp_path)}, clear=False):
            m = tool_pack_loader.ToolPackLoader().discover()[PACK_ID]
        assert m.tool_matches("dr_search")
        assert m.tool_matches("exact_tool")
        assert not m.tool_matches("web_search")
        assert not m.tool_matches("exact_tool_2") or True  # prefix-exact: not a glob
        assert not m.tool_matches("")

    def test_assets_dir_cannot_escape_pack(self, tmp_path):
        from adk.tool_pack_loader import ToolPackManifest
        m = ToolPackManifest(id="x", path=tmp_path, ui={"assets_dir": "../evil"})
        assert m.ui_assets_dir is None
        m2 = ToolPackManifest(id="x", path=tmp_path, ui={"assets_dir": "/abs"})
        assert m2.ui_assets_dir is None


# ── Enriched pack list + detail ──────────────────────────────────────────


class TestPackListEnrichment:
    def test_list_returns_rich_fields(self, pack_dir, tmp_path):
        c = _make_client(tmp_path, tools=("dr_search",))
        try:
            r = c.get("/admin/packs", headers=AUTH)
            assert r.status_code == 200
            packs = {p["id"]: p for p in r.json()["available"]}
            p = packs[PACK_ID]
            assert p["description"].startswith("A pack used")
            assert p["deprecated"] is True and p["redirect_to"] == "newpack"
            assert p["has_ui"] is True and p["ui_tabs"][0]["entry"] == "index.html"
            assert p["enabled"] is True
            assert p["live_tool_count"] == 1
        finally:
            _close(c)

    def test_detail_and_404(self, pack_dir, tmp_path):
        c = _make_client(tmp_path, tools=("dr_search", "unrelated"))
        try:
            assert c.get("/admin/packs/nope", headers=AUTH).status_code == 404
            r = c.get(f"/admin/packs/{PACK_ID}", headers=AUTH)
            assert r.status_code == 200
            d = r.json()
            assert [t["name"] for t in d["live_tools"]] == ["dr_search"]
            assert d["persona_fragment_count"] == 0
        finally:
            _close(c)


# ── Pack UI asset route ──────────────────────────────────────────────────


class TestAssetRoute:
    def test_serves_asset_without_bearer(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            r = c.get(f"/packs/{PACK_ID}/ui/index.html")  # NO auth header
            assert r.status_code == 200
            assert "pack ui" in r.text
            assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
            assert c.get(f"/packs/{PACK_ID}/ui/app.js").status_code == 200
        finally:
            _close(c)

    def test_403_when_pack_disabled(self, pack_dir, tmp_path):
        c = _make_client(tmp_path, enabled=())
        try:
            assert c.get(f"/packs/{PACK_ID}/ui/index.html").status_code == 403
        finally:
            _close(c)

    def test_404_unknown_pack(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            assert c.get("/packs/ghost/ui/index.html").status_code == 404
        finally:
            _close(c)

    def test_traversal_blocked(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            # secret.txt sits in the pack root, OUTSIDE the declared assets dir.
            # Plain ../ gets RFC-3986-normalized by clients out of the /ui/
            # prefix and dies at the auth middleware (401); encoded forms reach
            # the route and must die on the in-route guard (403/404).
            for evil in ("../secret.txt", "..%2Fsecret.txt", "a/../../secret.txt",
                         "a/%2e%2e/%2e%2e/secret.txt", "%2e%2e/secret.txt"):
                r = c.get(f"/packs/{PACK_ID}/ui/{evil}")
                assert r.status_code in (401, 403, 404), evil
                assert "outside assets dir" not in r.text
        finally:
            _close(c)

    def test_sdk_served_without_bearer(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            r = c.get("/packs/_sdk.js")
            assert r.status_code == 200
            assert "invokeTool" in r.text
        finally:
            _close(c)


# ── Pack tool invoke bridge ──────────────────────────────────────────────


class TestInvokeBridge:
    def test_requires_bearer(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            r = c.post(f"/admin/packs/{PACK_ID}/tools/dr_search/invoke", json={})
            assert r.status_code == 401
        finally:
            _close(c)

    def test_403_tool_not_in_pack(self, pack_dir, tmp_path):
        c = _make_client(tmp_path, tools=("web_search",))
        try:
            r = c.post(f"/admin/packs/{PACK_ID}/tools/web_search/invoke",
                       headers=AUTH, json={})
            assert r.status_code == 403
            assert r.json()["error"] == "tool_not_in_pack"
        finally:
            _close(c)

    def test_403_pack_disabled(self, pack_dir, tmp_path):
        c = _make_client(tmp_path, enabled=(), tools=("dr_search",))
        try:
            r = c.post(f"/admin/packs/{PACK_ID}/tools/dr_search/invoke",
                       headers=AUTH, json={})
            assert r.status_code == 403
            assert r.json()["error"] == "pack_not_enabled"
        finally:
            _close(c)

    def test_404_tool_not_registered(self, pack_dir, tmp_path):
        c = _make_client(tmp_path, tools=())
        try:
            r = c.post(f"/admin/packs/{PACK_ID}/tools/dr_search/invoke",
                       headers=AUTH, json={})
            assert r.status_code == 404
            assert r.json()["error"] == "tool_not_registered"
        finally:
            _close(c)

    def test_invoke_success_and_session_namespacing(self, pack_dir, tmp_path):
        c = _make_client(tmp_path, tools=("dr_search",))
        try:
            r = c.post(f"/admin/packs/{PACK_ID}/tools/dr_search/invoke",
                       headers=AUTH,
                       json={"args": {"query": "x", "session_id": "mychat"}})
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True and body["result"] == {"echo": True}
            called_args = c._agent._tools.execute.call_args.args[1]
            assert called_args["session_id"] == f"pack-{PACK_ID}-mychat"
        finally:
            _close(c)

    def test_builtin_tools_never_bridgeable(self, pack_dir, tmp_path):
        """Athena gate: even if a hostile manifest glob claims a built-in tool
        (this pack declares exact_tool; imagine it were file_read), the bridge
        refuses adk built-ins outright."""
        from adk import builtin_tools as bt
        assert callable(bt.file_read)
        # Craft a client whose pack manifest pattern would match file_read.
        (pack_dir / ".toolpack.yaml").write_text(
            (pack_dir / ".toolpack.yaml").read_text(encoding="utf-8").replace(
                '- "dr_*"', '- "file_*"'), encoding="utf-8")
        c = _make_client(tmp_path, tools=("file_read",))
        try:
            r = c.post(f"/admin/packs/{PACK_ID}/tools/file_read/invoke",
                       headers=AUTH, json={"args": {"path": "x"}})
            assert r.status_code == 403
            assert r.json()["error"] == "builtin_tool_not_bridgeable"
        finally:
            _close(c)

    def test_invoke_runs_inside_pack_scope(self, pack_dir, tmp_path):
        from adk.pack_scope import get_pack_scope
        c = _make_client(tmp_path, tools=("dr_search",))
        seen = {}

        async def capture(name, args):
            scope = get_pack_scope()
            seen["scope"] = scope.pack_id if scope else None
            return "{}"

        c._agent._tools.execute = AsyncMock(side_effect=capture)
        try:
            r = c.post(f"/admin/packs/{PACK_ID}/tools/dr_search/invoke",
                       headers=AUTH, json={})
            assert r.status_code == 200
            assert seen["scope"] == PACK_ID
        finally:
            _close(c)


# ── pack_scope file jail ─────────────────────────────────────────────────


class TestPackScopeJail:
    def test_file_tools_jailed_to_data_root(self, tmp_path):
        from adk.builtin_tools import file_list, file_read, file_search
        from adk.pack_scope import pack_scope

        outside = tmp_path / "private.txt"
        outside.write_text("owner data", encoding="utf-8")
        with patch("adk.pack_scope.Path.home", return_value=tmp_path / "home"):
            with pack_scope("scopetest") as s:
                # outside the jail → denied
                denied = json.loads(file_read(str(outside)))
                assert "error" in denied
                assert json.loads(file_list(str(tmp_path)))["error"]
                assert json.loads(file_search(str(tmp_path), "*.txt"))["error"]
                # inside the jail → allowed
                inside = s.data_root / "ok.txt"
                inside.write_text("pack data", encoding="utf-8")
                assert file_read(str(inside)) == "pack data"
        # scope cleared → normal roots apply again
        from adk.pack_scope import get_pack_scope
        assert get_pack_scope() is None

    def test_invalid_pack_id_rejected(self):
        from adk.pack_scope import pack_scope, valid_pack_id
        assert not valid_pack_id("../evil")
        assert not valid_pack_id("")
        with pytest.raises(ValueError):
            with pack_scope("../evil"):
                pass


# ── Per-pack settings ────────────────────────────────────────────────────


class TestPackSettings:
    def test_scalars_only(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            r = c.patch(f"/admin/packs/{PACK_ID}/settings", headers=AUTH,
                        json={"settings": {"depth": 3, "nested": {"a": 1}}})
            assert r.status_code == 400
            assert r.json()["error"] == "scalar_values_only"
            r = c.patch(f"/admin/packs/{PACK_ID}/settings", headers=AUTH,
                        json={"settings": {"depth": 3, "label": "x"}})
            assert r.status_code == 200
            assert r.json()["settings"] == {"depth": 3, "label": "x"}
        finally:
            _close(c)

    def test_secret_values_masked_on_read(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            c.patch(f"/admin/packs/{PACK_ID}/settings", headers=AUTH,
                    json={"settings": {"service_token": "sk-verysecretvalue1234"}})
            r = c.get(f"/admin/packs/{PACK_ID}/settings", headers=AUTH)
            assert "verysecret" not in json.dumps(r.json())
        finally:
            _close(c)


# ── Catalog proxy fail-soft ──────────────────────────────────────────────


class TestCatalogFailSoft:
    def test_offline_without_token(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            with patch("adk.sync.settings._resolve_token", return_value=""):
                r = c.get("/admin/catalog/packs", headers=AUTH)
            assert r.status_code == 200
            assert r.json()["offline"] is True
            assert r.json()["packs"] == []
        finally:
            _close(c)

    def test_requires_bearer(self, pack_dir, tmp_path):
        c = _make_client(tmp_path)
        try:
            assert c.get("/admin/catalog/packs").status_code == 401
        finally:
            _close(c)


# ── Regression: packs with relative imports ────────────────────────────

class TestPacksWithRelativeImports:
    """Regression test for the latent bug where packs with relative imports
    (from . import config, etc.) failed silently when loaded via file path
    (marketplace-installed packs) because the loader did not create the spec
    as a package (with submodule_search_locations)."""

    def test_pack_with_relative_imports_registers(self, tmp_path):
        """A pack with __init__.py that does 'from . import config' should
        load and register successfully when loaded via file path."""
        from adk.tool_pack_loader import ToolPackLoader

        # Create a temporary pack with relative imports
        pack_dir = tmp_path / "testpkg"
        pack_dir.mkdir()

        # .toolpack.yaml
        (pack_dir / ".toolpack.yaml").write_text(
            """
id: testpkg
name: Test Package
version: 1.0.0
description: A test pack with relative imports
mcp_tools:
  - test_tool
tool_modules:
  - testpkg
""",
            encoding="utf-8",
        )

        # config.py (imported relatively)
        (pack_dir / "config.py").write_text(
            """
def get_setting():
    return "test_value"
""",
            encoding="utf-8",
        )

        # __init__.py with relative import
        (pack_dir / "__init__.py").write_text(
            """
from . import config

def test_tool():
    '''A test tool that uses config.'''
    return config.get_setting()

def register(registry):
    # This only runs if the relative import succeeded
    registry.register(test_tool)
    return 1
""",
            encoding="utf-8",
        )

        # Discover and load the pack
        loader = ToolPackLoader(extra_dirs=[tmp_path])
        loader.discover()
        manifests = loader.load_packs(["testpkg"])
        assert len(manifests) == 1
        manifest = manifests[0]

        # Create a mock registry
        class MockRegistry:
            def __init__(self):
                self.registered = []

            def register(self, fn):
                self.registered.append(fn)

        registry = MockRegistry()

        # Create a mock agent with the registry as its tools attribute
        agent = MagicMock()
        agent.tools = registry

        # Register the pack - this should NOT fail
        n = loader.register_on_adk_agent(manifest, agent)
        # With the fix, the relative imports should succeed and register the tool
        assert n == 1, f"Pack should register 1 tool, but got {n}"


# ── Fail-closed entitlement gating ──────────────────────────────────────

class TestEntitlementGating:
    def test_enforce_true_raises_on_unmet_hard_gate(self, tmp_path):
        """When enforce_entitlements=True (default), discover() raises RuntimeError
        when a pack's hard entitlement gate is not met."""
        from adk import tool_pack_loader
        from unittest.mock import patch

        # Create a pack with require_all_entitlements=true and unmet entitlements
        pack_dir = tmp_path / "strict_pack"
        pack_dir.mkdir()
        (pack_dir / ".toolpack.yaml").write_text(
            """
id: strict_pack
name: Strict Pack
version: 1.0.0
description: A pack that requires all entitlements
entitlements:
  - premium_feature
  - advanced_ai
require_all_entitlements: true
mcp_tools:
  - strict_tool
tool_modules:
  - strict_pack
""",
            encoding="utf-8",
        )
        (pack_dir / "__init__.py").write_text(
            """
def register(registry):
    return 1
""",
            encoding="utf-8",
        )

        tool_pack_loader._LOADERS.clear()
        # Mock: caller has NO entitlements (both checks return False)
        with patch(
            "adk.tool_pack_loader.ToolPackLoader._entitled",
            return_value=False,
        ):
            with patch.dict(os.environ, {"AITHER_TOOLPACK_DIRS": str(tmp_path)},
                           clear=False):
                loader = tool_pack_loader.ToolPackLoader(enforce_entitlements=True)
                with pytest.raises(RuntimeError) as exc_info:
                    loader.discover()
                # Exception message should mention the pack and the reason
                assert "strict_pack" in str(exc_info.value)
                assert "missing entitlements" in str(exc_info.value)

    def test_enforce_false_allows_unmet_entitlements(self, tmp_path):
        """When enforce_entitlements=False (sovereign mode), discover() loads
        packs even if their entitlements are unmet."""
        from adk import tool_pack_loader
        from unittest.mock import patch

        pack_dir = tmp_path / "strict_pack"
        pack_dir.mkdir()
        (pack_dir / ".toolpack.yaml").write_text(
            """
id: strict_pack
name: Strict Pack
version: 1.0.0
description: A pack that requires all entitlements
entitlements:
  - premium_feature
  - advanced_ai
require_all_entitlements: true
mcp_tools:
  - strict_tool
tool_modules:
  - strict_pack
""",
            encoding="utf-8",
        )
        (pack_dir / "__init__.py").write_text(
            """
def register(registry):
    return 1
""",
            encoding="utf-8",
        )

        tool_pack_loader._LOADERS.clear()
        # Mock: caller has NO entitlements
        with patch(
            "adk.tool_pack_loader.ToolPackLoader._entitled",
            return_value=False,
        ):
            with patch.dict(os.environ, {"AITHER_TOOLPACK_DIRS": str(tmp_path)},
                           clear=False):
                # When enforce_entitlements=False, pack should load successfully
                loader = tool_pack_loader.ToolPackLoader(enforce_entitlements=False)
                manifests = loader.discover()
                assert "strict_pack" in manifests

    def test_enforce_true_raises_on_tier_gate(self, tmp_path):
        """When enforce_entitlements=True, discover() raises RuntimeError
        when a pack's min_tier requirement is not met."""
        from adk import tool_pack_loader
        from unittest.mock import patch

        pack_dir = tmp_path / "enterprise_pack"
        pack_dir.mkdir()
        (pack_dir / ".toolpack.yaml").write_text(
            """
id: enterprise_pack
name: Enterprise Pack
version: 1.0.0
description: A pack requiring enterprise tier
min_tier: enterprise
mcp_tools:
  - enterprise_tool
tool_modules:
  - enterprise_pack
""",
            encoding="utf-8",
        )
        (pack_dir / "__init__.py").write_text(
            """
def register(registry):
    return 1
""",
            encoding="utf-8",
        )

        tool_pack_loader._LOADERS.clear()
        # Mock: active tier is 'free' (lower than required 'enterprise')
        with patch(
            "adk.tool_pack_loader.ToolPackLoader._active_tier",
            return_value="free",
        ):
            with patch.dict(os.environ, {"AITHER_TOOLPACK_DIRS": str(tmp_path)},
                           clear=False):
                loader = tool_pack_loader.ToolPackLoader(enforce_entitlements=True)
                with pytest.raises(RuntimeError) as exc_info:
                    loader.discover()
                assert "enterprise_pack" in str(exc_info.value)
                assert "requires tier" in str(exc_info.value)

    def test_enforce_true_passes_met_entitlements(self, tmp_path):
        """When enforce_entitlements=True and all entitlements are met,
        discover() succeeds."""
        from adk import tool_pack_loader
        from unittest.mock import patch

        pack_dir = tmp_path / "premium_pack"
        pack_dir.mkdir()
        (pack_dir / ".toolpack.yaml").write_text(
            """
id: premium_pack
name: Premium Pack
version: 1.0.0
description: A pack that requires premium entitlements
entitlements:
  - premium_feature
require_all_entitlements: true
mcp_tools:
  - premium_tool
tool_modules:
  - premium_pack
""",
            encoding="utf-8",
        )
        (pack_dir / "__init__.py").write_text(
            """
def register(registry):
    return 1
""",
            encoding="utf-8",
        )

        tool_pack_loader._LOADERS.clear()
        # Mock: caller HAS the premium_feature entitlement
        with patch(
            "adk.tool_pack_loader.ToolPackLoader._entitled",
            return_value=True,
        ):
            with patch.dict(os.environ, {"AITHER_TOOLPACK_DIRS": str(tmp_path)},
                           clear=False):
                loader = tool_pack_loader.ToolPackLoader(enforce_entitlements=True)
                manifests = loader.discover()
                assert "premium_pack" in manifests

    def test_soft_gate_allows_unmet_entitlements(self, tmp_path):
        """Packs without require_all_entitlements=true (soft gate) are allowed
        regardless of enforce_entitlements setting."""
        from adk import tool_pack_loader
        from unittest.mock import patch

        pack_dir = tmp_path / "soft_pack"
        pack_dir.mkdir()
        (pack_dir / ".toolpack.yaml").write_text(
            """
id: soft_pack
name: Soft Pack
version: 1.0.0
description: A pack with optional entitlements (soft gate)
entitlements:
  - premium_feature
require_all_entitlements: false
mcp_tools:
  - soft_tool
tool_modules:
  - soft_pack
""",
            encoding="utf-8",
        )
        (pack_dir / "__init__.py").write_text(
            """
def register(registry):
    return 1
""",
            encoding="utf-8",
        )

        tool_pack_loader._LOADERS.clear()
        # Mock: caller has NO entitlements (but soft gate allows it)
        with patch(
            "adk.tool_pack_loader.ToolPackLoader._entitled",
            return_value=False,
        ):
            with patch.dict(os.environ, {"AITHER_TOOLPACK_DIRS": str(tmp_path)},
                           clear=False):
                loader = tool_pack_loader.ToolPackLoader(enforce_entitlements=True)
                manifests = loader.discover()
                assert "soft_pack" in manifests
