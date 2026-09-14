"""Regression tests for the /secret plugin credential resolution.

Guards the fix that made `adk secret get <platform-secret>` work as the
logged-in owner (2026-08-08):
  - URL defaults to the host genesis proxy (127.0.0.1:8001), not the vault's
    internal port (localhost:8111) which is not exposed to the host.
  - Headers authenticate with the session bearer (~/.aither/session-bearer)
    when no admin key is present — that is the owner's elevation.
  - An explicit admin key (X-API-Key) still takes precedence.

A regression to localhost:8111, or dropping the session-bearer fallback,
fails these assertions.
"""
from pathlib import Path

from adk.shell.plugins.builtins.secret import _headers, _resolve_bearer, _resolve_url


def _write_session_bearer(tmp_path, token="the-owner-token"):
    session = tmp_path / ".aither" / "session-bearer"
    session.parent.mkdir(exist_ok=True)
    session.write_text(token, encoding="utf-8")
    return session


def _clear_admin_env(monkeypatch):
    for var in ("AITHER_ADMIN_KEY", "AITHER_INTERNAL_SECRET", "AITHER_MASTER_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_resolve_url_defaults_to_genesis_proxy(monkeypatch):
    monkeypatch.delenv("AITHER_SECRETS_URL", raising=False)
    monkeypatch.delenv("AITHERSECRETS_URL", raising=False)
    assert _resolve_url() == "http://127.0.0.1:8001"


def test_resolve_url_respects_env_override(monkeypatch):
    monkeypatch.setenv("AITHER_SECRETS_URL", "https://aitheros-secrets:8111")
    assert _resolve_url() == "https://aitheros-secrets:8111"


def test_resolve_bearer_reads_session_file(monkeypatch, tmp_path):
    _write_session_bearer(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_bearer() == "the-owner-token"


def test_resolve_bearer_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_bearer() is None


def test_headers_use_session_bearer_without_admin_key(monkeypatch, tmp_path):
    _write_session_bearer(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _clear_admin_env(monkeypatch)
    headers = _headers()
    assert headers.get("Authorization") == "Bearer the-owner-token"
    assert "X-API-Key" not in headers


def test_headers_prefer_admin_key_over_bearer(monkeypatch, tmp_path):
    _write_session_bearer(tmp_path, token="session-token")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("AITHER_ADMIN_KEY", "admin-secret")
    monkeypatch.delenv("AITHER_INTERNAL_SECRET", raising=False)
    monkeypatch.delenv("AITHER_MASTER_KEY", raising=False)
    headers = _headers()
    assert headers.get("X-API-Key") == "admin-secret"
    assert "Authorization" not in headers
