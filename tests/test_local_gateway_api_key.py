"""The loopback MCP gateway attach falls back to ~/.aither/session-bearer."""

from types import SimpleNamespace

import pytest
from adk.server import _local_gateway_api_key


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("AITHER_INTERNAL_KEY", raising=False)
    monkeypatch.delenv("AITHER_MCP_KEY", raising=False)
    return tmp_path


def _write_bearer(home, text):
    d = home / ".aither"
    d.mkdir()
    (d / "session-bearer").write_text(text, encoding="utf-8")


def test_falls_back_to_session_bearer_stripped(home):
    _write_bearer(home, "  tok-from-file \r\n")
    assert _local_gateway_api_key(SimpleNamespace(aither_api_key="")) == "tok-from-file"


def test_no_credential_anywhere_is_empty(home):
    assert _local_gateway_api_key(SimpleNamespace(aither_api_key="")) == ""


def test_configured_key_outranks_file(home):
    _write_bearer(home, "tok-from-file")
    assert _local_gateway_api_key(SimpleNamespace(aither_api_key="cfg")) == "cfg"


def test_env_keys_outrank_file(home, monkeypatch):
    _write_bearer(home, "tok-from-file")
    monkeypatch.setenv("AITHER_MCP_KEY", "mcp-env")
    assert _local_gateway_api_key(SimpleNamespace(aither_api_key="")) == "mcp-env"
    monkeypatch.setenv("AITHER_INTERNAL_KEY", "internal-env")
    assert _local_gateway_api_key(SimpleNamespace(aither_api_key="")) == "internal-env"
