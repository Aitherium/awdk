"""The settings-hub bearer prefers the adk login session over an API key.

The hub resolves the caller through Identity, which rejects a gateway API key;
a machine that exported AITHERIUM_API_KEY used to sync as nobody (401).
"""

from __future__ import annotations

import json

import adk.auth as auth
import pytest
from adk.sync import settings

ENVS = ("AITHER_PORTAL_TOKEN", "AITHER_SYNC_TOKEN", "AITHERIUM_API_KEY", "AITHER_API_KEY")


@pytest.fixture
def signed_in(tmp_path, monkeypatch):
    for env in ENVS:
        monkeypatch.delenv(env, raising=False)
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({
        "version": auth.AUTH_VERSION,
        "active_profile": "cloud",
        "profiles": {"cloud": {"access_token": "login-session-token"}},
    }), encoding="utf-8")
    monkeypatch.setattr(auth, "AUTH_FILE", path)
    return path


def test_login_session_beats_api_key(signed_in, monkeypatch):
    monkeypatch.setenv("AITHERIUM_API_KEY", "gateway-api-key")
    monkeypatch.setenv("AITHER_API_KEY", "other-api-key")
    assert settings._resolve_token() == "login-session-token"


def test_explicit_sync_override_beats_login(signed_in, monkeypatch):
    monkeypatch.setenv("AITHER_SYNC_TOKEN", "override")
    assert settings._resolve_token() == "override"


def test_api_key_is_the_headless_fallback(tmp_path, monkeypatch):
    for env in ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "missing.json")
    monkeypatch.setenv("AITHERIUM_API_KEY", "gateway-api-key")
    assert settings._resolve_token() == "gateway-api-key"


def test_local_root_placeholder_is_not_a_credential(signed_in, monkeypatch):
    signed_in.write_text(json.dumps({
        "version": auth.AUTH_VERSION,
        "active_profile": "local",
        "profiles": {"local": {"access_token": "aither_root_local"}},
    }), encoding="utf-8")
    assert settings._resolve_token() == ""
