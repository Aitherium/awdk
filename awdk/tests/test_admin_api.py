"""Tests for the built-in admin/settings console API (adk/admin_api.py).

Covers the Athena blocking fixes (auth gating, config allowlist / RCE guard,
MCP SSRF guard, secret/log redaction) plus the settings-sync secret policy.
"""

import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from adk.admin_api import (
    _CONFIG_ALLOWLIST,
    _mask_config,
    _mask_key,
    _redact_log_line,
    _url_is_safe,
)

TOKEN = "test-bearer-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _make_client(api_key=TOKEN, graph=None):
    """Build a TestClient over a create_app() with a mocked agent."""
    env = {
        "AITHER_SERVER_API_KEY": api_key,
        "AITHER_OFFLINE": "1",
        "AITHER_SETTINGS_SYNC": "false",
    }
    with patch.dict(os.environ, env, clear=False):
        from adk.config import Config
        from adk.server import create_app

        config = Config()
        config.gateway_url = ""
        config.aither_api_key = ""
        agent = MagicMock()
        agent.name = "test"
        agent.llm = MagicMock()
        agent.llm.provider_name = "anthropic"
        agent.llm._model = "claude-opus-4-8"
        agent.llm.switch_backend = MagicMock()
        agent._identity = MagicMock(name="id")
        agent._identity.name = "test"
        agent._identity.description = "Test"
        agent._identity.skills = []
        agent._tools = MagicMock()
        agent._tools.list_tools = MagicMock(return_value=[])
        agent._graph = graph
        app = create_app(agent=agent, identity="test", config=config)
    return TestClient(app)


class TestAdminAuth:
    def test_admin_requires_bearer(self):
        c = _make_client()
        assert c.get("/admin/config").status_code == 401
        assert c.get("/admin/llm/status").status_code == 401

    def test_admin_accepts_valid_bearer(self):
        c = _make_client()
        assert c.get("/admin/config", headers=AUTH).status_code == 200

    def test_admin_rejects_wrong_bearer(self):
        c = _make_client()
        assert c.get("/admin/config", headers={"Authorization": "Bearer nope"}).status_code == 401


class TestConfigAllowlist:
    def test_rejects_rce_field(self):
        """Writing a pack-search dir would be arbitrary code execution."""
        c = _make_client()
        r = c.patch("/admin/config", headers=AUTH, json={"config": {"aither_toolpack_dirs": "/tmp/x"}})
        assert r.status_code == 403
        assert r.json()["error"] == "field_not_writable"
        assert "aither_toolpack_dirs" in r.json()["rejected"]

    def test_rejects_base_url_field(self):
        c = _make_client()
        r = c.patch("/admin/config", headers=AUTH, json={"config": {"openai_base_url": "http://evil"}})
        assert r.status_code == 403

    def test_accepts_allowlisted_field(self):
        c = _make_client()
        with patch("adk.admin_api.save_saved_config") as save:
            r = c.patch("/admin/config", headers=AUTH, json={"config": {"model": "gpt-4o"}})
        assert r.status_code == 200
        save.assert_called_once()

    def test_allowlist_excludes_dangerous_names(self):
        for bad in ("aither_toolpack_dirs", "dgx_url", "openai_base_url", "cluster_url"):
            assert bad not in _CONFIG_ALLOWLIST


class TestMcpSsrfGuard:
    def test_prepare_rejects_loopback(self):
        c = _make_client()
        r = c.post("/admin/mcp/servers/prepare", headers=AUTH,
                   json={"url": "http://127.0.0.1:8150/rpc"})
        assert r.status_code == 400
        assert r.json()["error"] == "unsafe_url"

    def test_prepare_rejects_metadata_ip(self):
        c = _make_client()
        r = c.post("/admin/mcp/servers/prepare", headers=AUTH,
                   json={"url": "http://169.254.169.254/latest/meta-data"})
        assert r.status_code == 400

    def test_prepare_rejects_localhost_name(self):
        c = _make_client()
        r = c.post("/admin/mcp/servers/prepare", headers=AUTH,
                   json={"url": "http://localhost:8001/mcp"})
        assert r.status_code == 400

    def test_url_is_safe_unit(self):
        ok, _ = _url_is_safe("http://127.0.0.1/x")
        assert ok is False
        ok, _ = _url_is_safe("http://10.0.0.5/x")
        assert ok is False
        ok, reason = _url_is_safe("ftp://example.com/x")
        assert ok is False and "scheme" in reason

    def test_allow_private_override(self):
        with patch.dict(os.environ, {"AITHER_ALLOW_PRIVATE_MCP": "1"}, clear=False):
            ok, _ = _url_is_safe("http://127.0.0.1:8001/mcp")
        assert ok is True


class TestRedaction:
    def test_mask_key(self):
        assert _mask_key("sk-abcdef123456") == "sk-a...3456"
        assert _mask_key("short") == "***"
        assert _mask_key("") == ""

    def test_mask_config_hides_secrets(self):
        masked = _mask_config({"openai_api_key": "sk-supersecretvalue", "model": "gpt-4o"})
        assert masked["openai_api_key"] == "sk-s...alue"
        assert masked["model"] == "gpt-4o"

    def test_log_redaction_scrubs_bearer_and_keys(self):
        line = "auth Bearer abc123XYZ.def used with sk-secretkey123456 and #k=frag-token99"
        out = _redact_log_line(line)
        assert "abc123XYZ" not in out
        assert "sk-secretkey123456" not in out
        assert "frag-token99" not in out
        assert "[REDACTED]" in out

    def test_logs_endpoint_redacts(self, tmp_path):
        c = _make_client()
        logdir = tmp_path / ".aither" / "logs"
        logdir.mkdir(parents=True)
        (logdir / "agent.log").write_text("line1 Bearer leaky-token-here\nline2 ok\n")
        with patch("adk.admin_api.Path.home", return_value=tmp_path):
            r = c.get("/admin/logs/tail", headers=AUTH)
        assert r.status_code == 200
        joined = "\n".join(r.json()["lines"])
        assert "leaky-token-here" not in joined


class TestConfigSurface:
    def test_config_includes_effective(self):
        c = _make_client()
        r = c.get("/admin/config", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert "effective" in body and isinstance(body["effective"], dict)
        # Effective config surfaces far more than the writable allowlist.
        assert len(body["effective"]) > len(body["writable_fields"])

    def test_effective_config_masks_secrets(self):
        from dataclasses import dataclass

        from adk.admin_api import _dump_effective_config

        @dataclass
        class Cfg:
            openai_api_key: str = "sk-supersecretvalue123"
            model: str = "gpt-4o"
        dumped = _dump_effective_config(Cfg())
        assert dumped["openai_api_key"] == "sk-s...e123"
        assert dumped["model"] == "gpt-4o"


class TestConsoleMetaAndCli:
    def test_meta(self):
        c = _make_client()
        r = c.get("/admin/meta", headers=AUTH)
        assert r.status_code == 200
        assert "version" in r.json() and r.json()["cli_enabled"] is True

    def test_cli_commands_listed(self):
        c = _make_client()
        r = c.get("/admin/cli/commands", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["total"] > 0

    def test_cli_exec_rejects_unknown_command(self):
        c = _make_client()
        r = c.post("/admin/cli/exec", headers=AUTH, json={"command": "rm -rf /", "args": []})
        assert r.status_code == 400
        assert "unknown command" in r.json()["error"]

    def test_cli_disabled_returns_403(self):
        with patch.dict(os.environ, {"AITHER_ADMIN_CLI": "0"}, clear=False):
            c = _make_client()
            assert c.get("/admin/cli/commands", headers=AUTH).status_code == 403
            assert c.post("/admin/cli/exec", headers=AUTH,
                          json={"command": "status", "args": []}).status_code == 403

    def test_cli_requires_auth(self):
        c = _make_client()
        assert c.post("/admin/cli/exec", json={"command": "status"}).status_code == 401

    def test_routes_introspection(self):
        """API explorer uses /admin/routes (openapi.json 500s on this server)."""
        c = _make_client()
        r = c.get("/admin/routes", headers=AUTH)
        assert r.status_code == 200
        routes = r.json()["routes"]
        assert r.json()["total"] > 20
        paths = {x["path"] for x in routes}
        assert "/admin/config" in paths and "/health" in paths
        assert all("method" in x and "path" in x for x in routes)


class TestGraphAndSessions:
    def test_graph_search_no_graph(self):
        c = _make_client(graph=None)
        r = c.get("/admin/graph/search?q=test", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["nodes"] == []

    def test_sessions_list(self):
        c = _make_client()
        r = c.get("/admin/sessions", headers=AUTH)
        assert r.status_code == 200
        assert "sessions" in r.json()


class TestSettingsSyncSecretPolicy:
    def test_snapshot_excludes_provider_keys_and_mcp_headers(self):
        from adk.sync import settings as settings_sync

        saved = {"llm_provider": "openai", "model": "gpt-4o", "model_api_key": "sk-SECRET",
                 "required_packs": ["p1"]}
        servers = [{"id": "m1", "url": "https://x/mcp", "name": "x",
                    "headers": {"Authorization": "Bearer SECRET-HEADER"}}]
        with patch.object(settings_sync, "load_saved_config", return_value=saved), \
             patch.object(settings_sync, "_load_mcp_servers", return_value=servers):
            snap = settings_sync.build_snapshot()
        blob = str(snap)
        assert "SECRET" not in blob
        assert "SECRET-HEADER" not in blob
        assert snap["llm"]["provider"] == "openai"
        assert snap["mcp_servers"][0]["url"] == "https://x/mcp"
        assert "headers" not in snap["mcp_servers"][0]
