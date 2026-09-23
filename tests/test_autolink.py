"""Autolink is opt-in, and a device code always reaches the owner as a live link."""

import json

from adk import autolink


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("AITHER_AUTOLINK", raising=False)
    monkeypatch.delenv("AITHER_FLEET_ENROLL", raising=False)
    assert autolink.autolink_enabled() is False
    assert autolink.start_autolink("http://127.0.0.1:8362") is None


def test_opt_in_flags(monkeypatch):
    monkeypatch.delenv("AITHER_FLEET_ENROLL", raising=False)
    monkeypatch.setenv("AITHER_AUTOLINK", "1")
    assert autolink.autolink_enabled() is True
    monkeypatch.setenv("AITHER_AUTOLINK", "0")
    assert autolink.autolink_enabled() is False


def test_announce_writes_pending_and_raises_a_high_card(tmp_path, monkeypatch):
    pending = tmp_path / "pending-signin.json"
    monkeypatch.setattr(autolink, "PENDING_PATH", pending)
    monkeypatch.setattr(autolink.shutil, "which", lambda _n: "/bin/awask")
    calls = []
    class _P:
        stdout, stderr = "", ""

    monkeypatch.setattr(autolink.subprocess, "run", lambda cmd, **_k: calls.append(cmd) or _P())

    autolink.announce_code("ABCD-EFGH", "https://idp.example/link?code=ABCD-EFGH", 900)

    data = json.loads(pending.read_text(encoding="utf-8"))
    assert data["user_code"] == "ABCD-EFGH"
    assert data["verification_uri"].endswith("ABCD-EFGH")
    cmd = calls[0]
    assert cmd[cmd.index("--urgency") + 1] == "high", "the DM floor is high"
    assert any("idp.example/link" in part for part in cmd)
    assert cmd[cmd.index("--deadline") + 1] == "15m"


def test_announce_without_awask_still_writes_the_file(tmp_path, monkeypatch):
    pending = tmp_path / "pending-signin.json"
    monkeypatch.setattr(autolink, "PENDING_PATH", pending)
    monkeypatch.setattr(autolink.shutil, "which", lambda _n: None)
    autolink.announce_code("WXYZ-1234", "https://idp.example/link", 600)
    assert json.loads(pending.read_text(encoding="utf-8"))["user_code"] == "WXYZ-1234"


def test_device_flow_hands_the_code_to_the_caller(monkeypatch):
    from adk import cli

    monkeypatch.setattr(cli, "_post_json_resilient", lambda *_a, **_k: {
        "user_code": "CODE-0001", "device_code": "dev", "interval": 2,
        "expires_in": 600, "verification_uri_complete": "https://idp.example/link?c=1",
    })
    monkeypatch.setattr(cli, "_poll_device_token",
                        lambda *_a, **_k: {"access_token": "tok"})
    seen = []
    result = cli._device_flow_login("https://idp.example", on_code=lambda *a: seen.append(a))
    assert result == {"access_token": "tok"}
    assert seen == [("CODE-0001", "https://idp.example/link?c=1", 600)]


def test_one_tap_uri_uses_user_code_not_code():
    uri = autolink.one_tap_uri("https://idp.example/link?code=AB12-CD34", "AB12-CD34")
    assert uri == "https://idp.example/link?user_code=AB12-CD34"


def test_a_fresh_code_withdraws_the_previous_card(tmp_path, monkeypatch):
    pending = tmp_path / "pending-signin.json"
    pending.write_text('{"card": "d-old1"}', encoding="utf-8")
    monkeypatch.setattr(autolink, "PENDING_PATH", pending)
    monkeypatch.setattr(autolink.shutil, "which", lambda _n: "/bin/awask")
    calls = []

    class _P:
        stdout, stderr = "raised d-new2", ""

    def fake_run(cmd, **_k):
        calls.append(cmd)
        return _P()

    monkeypatch.setattr(autolink.subprocess, "run", fake_run)
    autolink.announce_code("NEW0-CODE", "https://idp.example/link?code=NEW0-CODE", 900)

    assert calls[0][1:3] == ["cancel", "d-old1"]
    assert calls[1][1] == "ask"
    import json
    assert json.loads(pending.read_text(encoding="utf-8"))["card"] == "d-new2"
