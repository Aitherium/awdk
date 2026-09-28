"""One login per machine: adk.credentials is the one writer and reader of the bearer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

AWDK = Path(__file__).resolve().parents[1]
KEY_ENVS = ("AITHER_PORTAL_TOKEN", "AITHER_SYNC_TOKEN", "AITHERIUM_API_KEY",
            "AITHER_API_KEY", "AITHER_GATEWAY_KEY")


def _iso(delta_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def _write_store(path: Path, profiles: dict, active: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "active_profile": active,
                                "profiles": profiles}), encoding="utf-8")


ROOT = {"access_token": "aither_root_local", "token_type": "local", "expires_at": ""}


def test_sync_token_prints_the_profile_token_not_the_api_key(tmp_path):
    """AITHERIUM_API_KEY exported + a cloud login whose profile is not active
    (the root reset made 'local' active) -> the login, never the key."""
    home = tmp_path / "home"
    _write_store(home / ".aither" / "auth.json", {
        "local": ROOT,
        "idp.aitherium.com": {"access_token": "user-session-tok", "kind": "session",
                              "issuer": "https://idp.aitherium.com",
                              "expires_at": _iso(3600)},
    }, active="local")
    env = {k: v for k, v in os.environ.items() if k not in KEY_ENVS}
    env.update({"HOME": str(home), "USERPROFILE": str(home),
                "AITHERIUM_API_KEY": "aither_sk_live_x",
                "PYTHONPATH": str(AWDK) + os.pathsep + env.get("PYTHONPATH", "")})
    out = subprocess.run([sys.executable, "-m", "adk.sync.token"], env=env,
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() == "user-session-tok"


@pytest.fixture
def creds(tmp_path, monkeypatch):
    from adk import credentials as c

    for env in KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(c, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(c, "BEARER_FILE", tmp_path / "session-bearer")
    return c


def test_save_login_writes_both_files_keyed_by_issuer_host(creds, tmp_path):
    _write_store(creds.AUTH_FILE, {"local": ROOT}, active="local")
    name = creds.save_login("https://idp.aitherium.com/identity", "tok-1",
                            _iso(3600), "session", {"username": "ada"})
    assert name == "idp.aitherium.com"
    store = json.loads(creds.AUTH_FILE.read_text(encoding="utf-8"))
    assert store["active_profile"] == "idp.aitherium.com"
    assert store["profiles"]["local"] == ROOT  # the root profile is never overwritten
    assert store["profiles"]["idp.aitherium.com"]["access_token"] == "tok-1"
    assert creds.BEARER_FILE.read_text(encoding="utf-8") == "tok-1"
    if os.name == "posix":
        assert (creds.BEARER_FILE.stat().st_mode & 0o777) == 0o600
    assert creds.user_bearer() == "tok-1"


def test_local_is_reserved(creds):
    with pytest.raises(ValueError):
        creds.save_login("http://local:8001", "tok", "", "session", {})


@pytest.mark.parametrize("token", ["aither_sk_live_abc", "aither_ext_abc", "aither_root_local"])
def test_keys_are_never_the_user_bearer(creds, token):
    creds.save_login("https://idp.example", token, _iso(3600), "session", {})
    assert not creds.BEARER_FILE.exists()
    assert creds.user_bearer() == ""


def test_env_api_key_value_is_never_returned(creds, monkeypatch):
    creds.save_login("https://idp.example", "shared-value", _iso(3600), "session", {})
    monkeypatch.setenv("AITHERIUM_API_KEY", "shared-value")
    assert creds.user_bearer() == ""


def test_api_key_kind_is_stored_but_not_the_bearer(creds):
    creds.save_login("https://idp.example", "opaque", "", "api_key", {})
    assert not creds.BEARER_FILE.exists()
    assert creds.user_bearer() == ""


def test_pat_and_expiry(creds):
    creds.save_login("https://idp.example", "aither_pat_abc", _iso(-5), "pat", {})
    assert creds.user_bearer() == ""  # expired
    creds.save_login("https://idp.example", "aither_pat_abc", _iso(3600), "pat", {})
    assert creds.user_bearer(refresh=False) == "aither_pat_abc"


def test_past_half_life_refreshes_and_rewrites_both_files(creds):
    _write_store(creds.AUTH_FILE, {"idp.example": {
        "issuer": "https://idp.example", "kind": "session", "access_token": "old",
        "issued_at": _iso(-3000), "expires_at": _iso(600)}}, active="idp.example")
    calls = []

    def refresher(url, token, timeout):
        calls.append((url, token))
        return 200, {"access_token": "new", "expires_at": _iso(86400)}

    assert creds.user_bearer(refresher=refresher) == "new"
    assert calls == [("https://idp.example/auth/refresh", "old")]
    prof = json.loads(creds.AUTH_FILE.read_text(encoding="utf-8"))["profiles"]["idp.example"]
    assert prof["access_token"] == "new"
    assert creds.BEARER_FILE.read_text(encoding="utf-8") == "new"


def test_before_half_life_no_refresh_and_failed_refresh_keeps_token(creds):
    creds.save_login("https://idp.example", "tok", _iso(3600), "session", {})

    def boom(url, token, timeout):
        raise AssertionError("no refresh before half-life")

    assert creds.user_bearer(refresher=boom) == "tok"
    _write_store(creds.AUTH_FILE, {"idp.example": {
        "issuer": "https://idp.example", "kind": "session", "access_token": "tok",
        "issued_at": _iso(-3000), "expires_at": _iso(600)}}, active="idp.example")
    assert creds.user_bearer(refresher=lambda u, t, s: (401, {})) == "tok"
