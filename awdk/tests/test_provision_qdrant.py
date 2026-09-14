"""Coverage for _provision_qdrant (adk stack qdrant) — the three behaviors the
gate flagged: docker-start validated, env-var precedence respected, honest exit code."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from adk import cli


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Keep config writes + generated key off the real ~/.aither.
    monkeypatch.setattr(cli, "load_saved_config", lambda: {})
    monkeypatch.setattr(cli, "save_saved_config", lambda *_a, **_k: None)
    monkeypatch.delenv("AITHER_FLEET_QDRANT_URL", raising=False)
    monkeypatch.delenv("AITHER_FLEET_QDRANT_API_KEY", raising=False)
    yield


def _run(returncode: int):
    def _fake(cmd, *a, **k):
        r = MagicMock()
        r.returncode = returncode
        r.stderr = b"boom" if returncode else b""
        return r
    return _fake


def test_success_wires_env_and_returns_true():
    with patch("subprocess.run", _run(0)):
        ok, url, key = cli._provision_qdrant()
    assert ok is True
    assert url == "http://localhost:6333"
    assert os.environ.get("AITHER_FLEET_QDRANT_URL") == url
    assert os.environ.get("AITHER_FLEET_QDRANT_API_KEY") == key


def test_docker_start_failure_returns_false():
    """A non-zero `docker start` must NOT be reported as success."""
    with patch("subprocess.run", _run(1)):
        ok, url, _key = cli._provision_qdrant()
    assert ok is False
    assert url == ""
    # Must not have wired a URL when start failed.
    assert os.environ.get("AITHER_FLEET_QDRANT_URL") is None


def test_existing_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("AITHER_FLEET_QDRANT_URL", "http://my-qdrant:9999")
    monkeypatch.setenv("AITHER_FLEET_QDRANT_API_KEY", "customer-key")
    with patch("subprocess.run", _run(0)):
        ok, _url, _key = cli._provision_qdrant()
    assert ok is True
    # Customer's explicit setup is preserved, not clobbered.
    assert os.environ["AITHER_FLEET_QDRANT_URL"] == "http://my-qdrant:9999"
    assert os.environ["AITHER_FLEET_QDRANT_API_KEY"] == "customer-key"


def test_docker_unavailable_returns_false():
    with patch("subprocess.run", side_effect=OSError("no docker")):
        ok, url, _key = cli._provision_qdrant()
    assert ok is False
    assert url == ""


def test_cmd_stack_qdrant_exit_code_reflects_failure():
    """cmd_stack qdrant must exit non-zero when provisioning fails."""
    args = MagicMock()
    args.service = "qdrant"
    with patch.object(cli, "_provision_qdrant", return_value=(False, "", "k")):
        assert cli.cmd_stack(args) == 1
    with patch.object(cli, "_provision_qdrant", return_value=(True, "http://localhost:6333", "k")):
        assert cli.cmd_stack(args) == 0
