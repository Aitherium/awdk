"""Tests for server auth middleware and CLI."""

import os
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


class TestAuthMiddleware:
    """Server auth middleware validates Bearer tokens."""

    def _make_app(self, api_key=""):
        """Create a test app with optional auth."""
        with patch.dict(os.environ, {"AITHER_SERVER_API_KEY": api_key}, clear=False):
            from adk.server import create_app
            from adk.config import Config
            config = Config()
            config.gateway_url = ""
            config.aither_api_key = ""
            agent = MagicMock()
            agent.name = "test"
            agent.llm = MagicMock()
            agent.llm.provider_name = "test"
            agent._identity = MagicMock()
            agent._identity.name = "test"
            agent._identity.description = "Test"
            agent._identity.skills = []
            agent._tools = MagicMock()
            agent._tools.list_tools = MagicMock(return_value=[])
            agent._safety = None
            app = create_app(agent=agent, identity="test", config=config)
        return app

    def test_no_key_open_access(self):
        """No API key configured = open access."""
        app = self._make_app(api_key="")
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_valid_key_passes(self):
        """Valid Bearer token passes auth."""
        app = self._make_app(api_key="secret123")
        client = TestClient(app)
        resp = client.get("/health")
        # Health is in skip-auth paths
        assert resp.status_code == 200

    def test_invalid_key_401(self):
        """Invalid Bearer token returns 401."""
        app = self._make_app(api_key="secret123")
        client = TestClient(app)
        resp = client.get("/agents", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_missing_auth_header_401(self):
        """Missing auth header returns 401 when key is configured."""
        app = self._make_app(api_key="secret123")
        client = TestClient(app)
        resp = client.get("/agents")
        assert resp.status_code == 401

    def test_health_skips_auth(self):
        """Health endpoint skips auth even with key configured."""
        app = self._make_app(api_key="secret123")
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_docs_skips_auth(self):
        """Docs endpoint skips auth."""
        app = self._make_app(api_key="secret123")
        client = TestClient(app)
        resp = client.get("/docs")
        assert resp.status_code == 200

    def test_metrics_skips_auth(self):
        """Metrics endpoint skips auth."""
        app = self._make_app(api_key="secret123")
        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_correct_key_passes_protected(self):
        """Correct key grants access to protected endpoints."""
        app = self._make_app(api_key="secret123")
        client = TestClient(app)
        resp = client.get("/agents", headers={"Authorization": "Bearer secret123"})
        assert resp.status_code == 200


class TestCLI:
    """CLI scaffolding commands."""

    def test_init_creates_files(self):
        from adk.cli import cmd_init
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test-project")
            args = MagicMock()
            args.name = "test-agent"
            args.directory = target
            result = cmd_init(args)
            assert result == 0
            assert (Path(target) / "agent.py").exists()
            assert (Path(target) / "config.yaml").exists()
            assert (Path(target) / "tools.py").exists()

    def test_init_agent_content(self):
        from adk.cli import cmd_init
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test-project")
            args = MagicMock()
            args.name = "my-bot"
            args.directory = target
            cmd_init(args)
            content = (Path(target) / "agent.py").read_text()
            assert "my-bot" in content
            assert "AitherAgent" in content

    def test_init_config_content(self):
        from adk.cli import cmd_init
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "test-project")
            args = MagicMock()
            args.name = "my-bot"
            args.directory = target
            cmd_init(args)
            content = (Path(target) / "config.yaml").read_text()
            assert "my-bot" in content
            assert "builtin_tools" in content

    def test_init_existing_nonempty_fails(self):
        from adk.cli import cmd_init
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file so directory is non-empty
            Path(tmpdir, "existing.txt").touch()
            args = MagicMock()
            args.name = "test"
            args.directory = tmpdir
            result = cmd_init(args)
            assert result == 1

    def test_init_default_name(self):
        from adk.cli import cmd_init
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "default-proj")
            args = MagicMock()
            args.name = None
            args.directory = target
            result = cmd_init(args)
            assert result == 0
            content = (Path(target) / "agent.py").read_text()
            assert "my-agent" in content
