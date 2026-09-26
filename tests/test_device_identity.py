"""`adk enroll` gives the device an identity and turns on what needs one.

The device's own signing key (awseal) is created once and its public half rides
in the enrollment payload; after enrollment, awsettings is pointed at the user's
settings store and device list with adk's login as its credential helper, and the
Claude Code hooks are installed at user level.
"""

from __future__ import annotations

import json

import pytest

from adk import device_identity

awseal = pytest.importorskip("awseal")
awsettings_cli = pytest.importorskip("awsettings.cli")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("AWSETTINGS_HOME", str(tmp_path / ".awsettings"))
    monkeypatch.setenv(awseal.keys.KEY_PATH_ENV, str(tmp_path / "seal" / "signing.key"))
    monkeypatch.delenv("AITHER_SETTINGS_SYNC", raising=False)
    return tmp_path


def test_the_key_is_created_once_and_its_public_half_is_stable(home):
    first = device_identity.seal_public_key()
    assert len(bytes.fromhex(first)) == 32
    assert (home / "seal" / "signing.key").exists()
    assert device_identity.seal_public_key() == first


def test_registration_carries_the_public_key(home):
    fields = device_identity.registration_fields()
    assert fields == {"seal_pubkey": device_identity.seal_public_key()}


def test_enrollment_payload_includes_the_key(home):
    from adk import enrollment
    assert enrollment._identity_fields()["seal_pubkey"] == device_identity.seal_public_key()


def test_enroll_turns_on_signed_sync_and_installs_user_hooks(home, monkeypatch):
    import httpx

    def offline(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", offline)          # device list fetched later
    own = device_identity.seal_public_key()               # the key enroll creates
    rc, line = device_identity.configure_settings_sync(
        "https://portal.invalid", "https://idp.invalid")
    assert rc == 0, line
    cfg = json.loads((home / ".awsettings" / "config.json").read_text(encoding="utf-8"))
    assert cfg["url"] == "https://portal.invalid/api/settings/preferences"
    assert cfg["keys_url"] == "https://idp.invalid/v1/nodes/seal-keys"
    assert "adk.sync.token" in cfg["token_command"]
    assert cfg["sign"] is True and cfg["public_key"] == own
    # The device list is offline, so no seal is required yet: requiring one with
    # nobody trusted would refuse every pull.
    assert "require_seal" not in cfg
    hooks = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
    assert "SessionStart" in hooks and "PostToolUse" in hooks


def test_the_opt_out_is_honoured(home, monkeypatch):
    monkeypatch.setenv("AITHER_SETTINGS_SYNC", "0")
    rc, line = device_identity.configure_settings_sync("https://p.invalid", "https://i.invalid")
    assert rc == 0 and "off" in line
    assert not (home / ".awsettings" / "config.json").exists()


def test_token_helper_prints_the_login_and_fails_when_signed_out(monkeypatch, capsys):
    from adk.sync import settings, token
    monkeypatch.setattr(settings, "_resolve_token", lambda: "tok-abc")
    assert token.main() == 0 and capsys.readouterr().out.strip() == "tok-abc"
    monkeypatch.setattr(settings, "_resolve_token", lambda: "")
    assert token.main() == 1 and capsys.readouterr().out == ""
