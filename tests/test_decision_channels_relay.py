"""A relay block in channels.json loads (it used to break the whole file, Discord too)."""
from __future__ import annotations

import json

from adk.decisions import channels as ch


def _write(tmp_path, monkeypatch, body):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path))
    (tmp_path / "channels.json").write_text(json.dumps(body), encoding="utf-8")


def test_relay_entry_loads_alongside_discord(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {
        "discord": {"enabled": True, "owner_user_id": "123", "require_direct_message": True},
        "relay": {"enabled": True, "owner_user_id": "david", "require_direct_message": False},
    })
    cfgs = ch.load_config()
    assert sorted(cfgs) == ["discord", "relay"]
    assert ch.authorize(cfgs["relay"], user_id="david", is_direct_message=False).allowed
    # an agent session nick is not the owner
    assert not ch.authorize(cfgs["relay"], user_id="david+76ce0bd1",
                            is_direct_message=False).allowed
    # discord's DM rule is untouched
    assert not ch.authorize(cfgs["discord"], user_id="123", is_direct_message=False).allowed


def test_unknown_platform_still_refused(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, {"pager": {"enabled": True}})
    try:
        ch.load_config()
    except ch.ChannelConfigError:
        return
    raise AssertionError("an unknown platform must still be refused")
