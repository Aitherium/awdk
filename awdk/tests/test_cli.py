"""Tests for CLI commands — register, connect, and config persistence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk.cli import cmd_register, cmd_connect, cmd_init
from adk.config import Config, load_saved_config, save_saved_config
from adk.elysium import Elysium


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    """Build a minimal argparse.Namespace for CLI commands."""
    return argparse.Namespace(**kwargs)


def _mock_httpx_client(get_side_effect=None, post_side_effect=None):
    """Return a patched httpx.AsyncClient context-manager mock."""
    client = AsyncMock()
    if get_side_effect:
        client.get = AsyncMock(side_effect=get_side_effect)
    else:
        # Default: everything returns 404
        default_resp = MagicMock(status_code=404, json=MagicMock(return_value={}))
        client.get = AsyncMock(return_value=default_resp)
    if post_side_effect:
        client.post = AsyncMock(side_effect=post_side_effect)
    else:
        default_resp = MagicMock(status_code=404, json=MagicMock(return_value={}))
        client.post = AsyncMock(return_value=default_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# Config helpers (load_saved_config / save_saved_config)
# ---------------------------------------------------------------------------


class TestConfigHelpers:
    def test_load_missing_file(self, tmp_path):
        """load_saved_config returns {} when the file does not exist."""
        assert load_saved_config(tmp_path / "nope.json") == {}

    def test_save_and_load(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        save_saved_config({"api_key": "sk_test", "tier": "pro"}, cfg_path)
        loaded = load_saved_config(cfg_path)
        assert loaded["api_key"] == "sk_test"
        assert loaded["tier"] == "pro"

    def test_save_merges(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        save_saved_config({"api_key": "sk_1"}, cfg_path)
        save_saved_config({"tenant_id": "t-acme"}, cfg_path)
        loaded = load_saved_config(cfg_path)
        assert loaded["api_key"] == "sk_1"
        assert loaded["tenant_id"] == "t-acme"

    def test_save_overwrites_key(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        save_saved_config({"api_key": "old"}, cfg_path)
        save_saved_config({"api_key": "new"}, cfg_path)
        assert load_saved_config(cfg_path)["api_key"] == "new"

    def test_load_corrupt_file(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("NOT JSON!", encoding="utf-8")
        assert load_saved_config(cfg_path) == {}

    def test_save_creates_parent_dirs(self, tmp_path):
        cfg_path = tmp_path / "deep" / "nested" / "config.json"
        save_saved_config({"key": "val"}, cfg_path)
        assert cfg_path.exists()
        assert load_saved_config(cfg_path)["key"] == "val"


# ---------------------------------------------------------------------------
# Config.from_env tenant_id backfill
# ---------------------------------------------------------------------------


class TestConfigTenantBackfill:
    def test_tenant_id_from_env(self, monkeypatch):
        monkeypatch.setenv("AITHER_TENANT_ID", "t-env")
        cfg = Config()
        assert cfg.tenant_id == "t-env"

    def test_tenant_id_default_empty(self, monkeypatch):
        monkeypatch.delenv("AITHER_TENANT_ID", raising=False)
        cfg = Config()
        assert cfg.tenant_id == ""

    def test_from_env_backfills_tenant_from_saved(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AITHER_TENANT_ID", raising=False)
        monkeypatch.delenv("AITHER_API_KEY", raising=False)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"tenant_id": "t-saved", "api_key": "sk_saved"}))

        with patch("adk.config.load_saved_config", return_value={"tenant_id": "t-saved", "api_key": "sk_saved"}):
            cfg = Config.from_env()
        assert cfg.tenant_id == "t-saved"
        assert cfg.aither_api_key == "sk_saved"

    def test_env_var_wins_over_saved(self, monkeypatch):
        monkeypatch.setenv("AITHER_TENANT_ID", "t-env")
        with patch("adk.config.load_saved_config", return_value={"tenant_id": "t-saved"}):
            cfg = Config.from_env()
        assert cfg.tenant_id == "t-env"


# ---------------------------------------------------------------------------
# Elysium.fetch_tenant_info
# ---------------------------------------------------------------------------


class TestFetchTenantInfo:
    @pytest.mark.asyncio
    async def test_success(self):
        e = Elysium(api_key="sk_test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "user_id": "u-42",
            "tenant_id": "t-acme",
            "tier": "pro",
            "plan": "pro",
            "role": "admin",
            "permissions": ["read", "write", "admin"],
        }

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            info = await e.fetch_tenant_info()

        assert info["user_id"] == "u-42"
        assert info["tenant_id"] == "t-acme"
        assert info["tier"] == "pro"
        assert info["role"] == "admin"

    @pytest.mark.asyncio
    async def test_nested_user_object(self):
        """The endpoint may nest info under a 'user' key."""
        e = Elysium(api_key="sk_test")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "user": {
                "user_id": "u-99",
                "tenant_id": "t-nested",
                "tier": "enterprise",
                "role": "owner",
            },
        }

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            info = await e.fetch_tenant_info()

        assert info["tenant_id"] == "t-nested"
        assert info["tier"] == "enterprise"

    @pytest.mark.asyncio
    async def test_returns_empty_on_401(self):
        e = Elysium(api_key="bad_key")
        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            info = await e.fetch_tenant_info()

        assert info == {}

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self):
        e = Elysium(api_key="sk_test")

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=Exception("connection refused"))
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            info = await e.fetch_tenant_info()

        assert info == {}


# ---------------------------------------------------------------------------
# cmd_register
# ---------------------------------------------------------------------------


class TestCmdRegister:
    def test_register_non_interactive(self, tmp_path):
        """Non-interactive register with --email and --password flags."""
        args = _make_args(email="alice@example.com", password="secret123")

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "ok": True,
            "user_id": "u-alice",
            "api_key": "aither_sk_live_abc",
        }
        mock_resp.raise_for_status = MagicMock()

        cfg_path = tmp_path / "config.json"

        with patch("httpx.AsyncClient") as MockClient, \
             patch("adk.cli.save_saved_config") as mock_save:
            # Wire the mock so Elysium.register() works
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            rc = cmd_register(args)

        assert rc == 0
        # Verify save was called with the API key
        mock_save.assert_called_once()
        call_data = mock_save.call_args[0][0]
        assert call_data["api_key"] == "aither_sk_live_abc"
        assert call_data["email"] == "alice@example.com"

    def test_register_no_api_key_in_response(self):
        """Register works even if the server returns no api_key."""
        args = _make_args(email="bob@example.com", password="pw")

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"ok": True, "user_id": "u-bob"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as MockClient, \
             patch("adk.cli.save_saved_config") as mock_save:
            client = AsyncMock()
            client.post = AsyncMock(return_value=mock_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            rc = cmd_register(args)

        assert rc == 0
        # save_saved_config should NOT be called when there's no api_key
        mock_save.assert_not_called()

    def test_register_network_error(self):
        """Register handles network errors gracefully."""
        args = _make_args(email="fail@example.com", password="pw")

        with patch("httpx.AsyncClient") as MockClient:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=ConnectionError("connection refused"))
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            rc = cmd_register(args)

        assert rc == 1

    def test_register_empty_email(self):
        """Register fails if email is empty (after interactive prompt returns empty)."""
        args = _make_args(email="", password="pw")
        # Mock input() to return empty string (user just hits Enter)
        with patch("builtins.input", return_value=""):
            rc = cmd_register(args)
        assert rc == 1

    def test_register_empty_password(self):
        """Register fails if password is empty (after interactive prompt returns empty)."""
        args = _make_args(email="x@example.com", password="")
        with patch("getpass.getpass", return_value=""):
            rc = cmd_register(args)
        assert rc == 1


# ---------------------------------------------------------------------------
# cmd_connect — tenant info fetch and config save
# ---------------------------------------------------------------------------


class TestCmdConnect:
    def _make_connect_args(self, api_key="", save=True):
        return _make_args(api_key=api_key, save=save)

    def test_connect_no_key_shows_register_hint(self, capsys, monkeypatch):
        """When no API key is available, connect suggests 'aither register'."""
        monkeypatch.delenv("AITHER_API_KEY", raising=False)
        args = self._make_connect_args(api_key="")

        with patch("httpx.AsyncClient") as MockClient, \
             patch("adk.cli.load_saved_config", return_value={}), \
             patch("adk.cli.save_saved_config"):
            client = _mock_httpx_client()
            MockClient.return_value = client

            rc = cmd_connect(args)

        assert rc == 0
        out = capsys.readouterr().out
        assert "aither register" in out

    def test_connect_saves_tenant_id(self, capsys, monkeypatch):
        """When gateway returns tenant info, it is saved to config."""
        monkeypatch.delenv("AITHER_API_KEY", raising=False)
        api_key = "aither_sk_live_test999"
        args = self._make_connect_args(api_key=api_key, save=True)

        call_index = 0

        async def mock_get(url, **kwargs):
            nonlocal call_index
            call_index += 1
            resp = MagicMock()

            if "/health" in url:
                resp.status_code = 200
                resp.json.return_value = {"ok": True}
            elif "/billing/balance" in url:
                resp.status_code = 200
                resp.json.return_value = {"plan": "pro", "balance": 5000}
            elif "/v1/models" in url:
                resp.status_code = 200
                resp.json.return_value = {"data": [{"id": "m1", "accessible": True}]}
            elif "/v1/auth/me" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "user_id": "u-42",
                    "tenant_id": "t-acme",
                    "tier": "pro",
                    "role": "admin",
                }
            elif "/v1/mesh/status" in url:
                resp.status_code = 200
                resp.json.return_value = {"total_nodes": 5}
            else:
                resp.status_code = 404
                resp.json.return_value = {}
            return resp

        with patch("httpx.AsyncClient") as MockClient, \
             patch("adk.cli.load_saved_config", return_value={}), \
             patch("adk.cli.save_saved_config") as mock_save:
            client = AsyncMock()
            client.get = AsyncMock(side_effect=mock_get)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            rc = cmd_connect(args)

        assert rc == 0
        # Verify tenant_id was included in the save call
        mock_save.assert_called_once()
        save_data = mock_save.call_args[0][0]
        assert save_data["tenant_id"] == "t-acme"
        assert save_data["api_key"] == api_key

        out = capsys.readouterr().out
        assert "Tenant: t-acme" in out
        assert "Tier: pro" in out
        assert "Role: admin" in out

    def test_connect_no_save_flag(self, capsys, monkeypatch):
        """When --no-save is used, config is not written."""
        monkeypatch.delenv("AITHER_API_KEY", raising=False)
        args = self._make_connect_args(api_key="", save=False)

        with patch("httpx.AsyncClient") as MockClient, \
             patch("adk.cli.load_saved_config", return_value={}), \
             patch("adk.cli.save_saved_config") as mock_save:
            client = _mock_httpx_client()
            MockClient.return_value = client

            rc = cmd_connect(args)

        assert rc == 0
        mock_save.assert_not_called()

    def test_connect_reads_api_key_from_saved_config(self, capsys, monkeypatch):
        """When no --api-key flag or env var, falls back to saved config."""
        monkeypatch.delenv("AITHER_API_KEY", raising=False)
        args = self._make_connect_args(api_key="", save=False)

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 404
            resp.json.return_value = {}
            return resp

        with patch("httpx.AsyncClient") as MockClient, \
             patch("adk.cli.load_saved_config", return_value={"api_key": "aither_sk_live_from_file"}):
            client = AsyncMock()
            client.get = AsyncMock(side_effect=mock_get)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            rc = cmd_connect(args)

        assert rc == 0
        out = capsys.readouterr().out
        # Should show the saved key (truncated to first 16 chars)
        assert "aither_sk_live_f" in out

    def test_connect_gateway_down_skips_tenant(self, capsys, monkeypatch):
        """If gateway health fails, tenant info fetch is skipped."""
        monkeypatch.delenv("AITHER_API_KEY", raising=False)
        args = self._make_connect_args(api_key="aither_sk_live_x", save=False)

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            # Everything fails
            resp.status_code = 503
            resp.json.return_value = {}
            return resp

        with patch("httpx.AsyncClient") as MockClient, \
             patch("adk.cli.load_saved_config", return_value={}):
            client = AsyncMock()
            client.get = AsyncMock(side_effect=mock_get)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = client

            rc = cmd_connect(args)

        assert rc == 0
        out = capsys.readouterr().out
        # Tenant line should NOT appear
        assert "Tenant:" not in out


# ---------------------------------------------------------------------------
# cmd_init (basic sanity)
# ---------------------------------------------------------------------------


class TestCmdInit:
    def test_init_creates_files(self, tmp_path):
        target = tmp_path / "test-agent"
        args = _make_args(name="test-agent", directory=str(target))
        rc = cmd_init(args)
        assert rc == 0
        assert (target / "agent.py").exists()
        assert (target / "config.yaml").exists()
        assert (target / "tools.py").exists()

    def test_init_rejects_non_empty_dir(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        (target / "file.txt").write_text("something")
        args = _make_args(name="existing", directory=str(target))
        rc = cmd_init(args)
        assert rc == 1
