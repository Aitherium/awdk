"""adk link: one sign-in for the machine; the role only ever comes from the server."""

from __future__ import annotations

import json

import pytest
from adk import link


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_IDENTITY_URL", "https://idp.example")
    monkeypatch.setenv("AITHER_PORTAL_URL", "https://portal.example")
    return tmp_path


OWNER = {"role": "owner", "identity": {"username": "owner"}, "endpoints": {"local": {}}}
USER = {"role": "user", "identity": {"username": "someone"}, "endpoints": {}}


def test_start_returns_the_code_and_approve_link(monkeypatch):
    calls = []

    def fake_post(url, body, timeout=15.0):
        calls.append((url, body))
        return 200, {
            "device_code": "dc",
            "user_code": "AB-12",
            "verification_uri_complete": "https://idp/link?c=AB-12",
        }

    monkeypatch.setattr(link, "_post", fake_post)
    res = link.start()
    assert calls == [("https://idp.example/auth/device/code", {"client_name": "adk-link"})]
    assert (
        res["ok"]
        and res["user_code"] == "AB-12"
        and res["approve_url"] == "https://idp/link?c=AB-12"
    )


def test_an_unreachable_identity_is_an_error_not_a_code(monkeypatch):
    def boom(url, body, timeout=15.0):
        raise OSError("down")

    monkeypatch.setattr(link, "_post", boom)
    assert link.start()["ok"] is False


@pytest.mark.parametrize(
    "reply,status",
    [
        ((400, {"detail": "authorization_pending"}), "authorization_pending"),
        ((400, {"detail": "slow_down"}), "slow_down"),
        ((400, {"detail": "expired_token"}), "expired"),
        ((400, {"detail": "access_denied"}), "denied"),
    ],
)
def test_poll_outcomes(monkeypatch, reply, status):
    monkeypatch.setattr(link, "_post", lambda url, body, timeout=10: reply)
    assert link.poll("dc")["status"] == status


def test_approval_persists_the_sign_in_and_fetches_the_bundle(monkeypatch):
    saved = {}
    monkeypatch.setattr(
        link,
        "_post",
        lambda url, body, timeout=10: (200, {"access_token": "tok", "user": {"username": "owner"}}),
    )
    import adk.cli as cli

    monkeypatch.setattr(
        cli,
        "complete_device_login",
        lambda base, data, sync=True: saved.update(base=base, sync=sync) or "owner",
    )
    monkeypatch.setattr(
        link,
        "refresh",
        lambda token=None, fetch=None: (
            {"ok": True, "bundle": OWNER} if token == "tok" else {"ok": False}
        ),
    )
    res = link.poll("dc")
    assert res == {
        "ok": True,
        "status": "complete",
        "username": "owner",
        "role": "owner",
        "bundle_error": None,
    }
    assert saved == {"base": "https://idp.example", "sync": False}


def test_refresh_stores_a_real_bundle_and_status_reports_the_role(monkeypatch):
    monkeypatch.setattr(link, "_stored_token", lambda: "tok")
    seen = []
    res = link.refresh(fetch=lambda url, token: seen.append((url, token)) or (200, USER))
    assert res["ok"]
    assert seen == [("https://portal.example/api/bridge/genesis/v1/link/bundle", "tok")]
    st = link.status()
    assert (st["linked"], st["signed_in"], st["role"], st["username"]) == (
        True,
        True,
        "user",
        "someone",
    )
    assert "tok" not in link.bundle_file().read_text(encoding="utf-8")


def test_a_refused_credential_clears_the_stored_role(monkeypatch):
    link.bundle_file().parent.mkdir(parents=True, exist_ok=True)
    link.bundle_file().write_text(json.dumps(OWNER), encoding="utf-8")
    res = link.refresh(token="old", fetch=lambda url, token: (401, {}))
    assert res["ok"] is False
    assert not link.bundle_file().exists()
    assert link.status()["role"] is None


def test_a_network_failure_keeps_the_last_good_bundle():
    link.bundle_file().parent.mkdir(parents=True, exist_ok=True)
    link.bundle_file().write_text(json.dumps(OWNER), encoding="utf-8")

    def down(url, token):
        raise OSError("reset")

    assert link.refresh(token="t", fetch=down)["ok"] is False
    assert json.loads(link.bundle_file().read_text(encoding="utf-8"))["role"] == "owner"


def test_a_response_that_is_not_a_bundle_is_never_stored():
    res = link.refresh(token="t", fetch=lambda url, token: (200, {"role": "owner"}))
    assert res["ok"] is False and not link.bundle_file().exists()


def test_status_when_never_linked(monkeypatch):
    monkeypatch.setattr(link, "_stored_token", lambda: "")
    st = link.status()
    assert st["linked"] is False and st["signed_in"] is False and st["role"] is None


def test_a_stored_key_without_a_bundle_is_signed_in_not_linked(monkeypatch):
    # e.g. the local-only token a fleet install writes: aitherium.com never saw it
    monkeypatch.setattr(link, "_stored_token", lambda: "local-token")
    st = link.status()
    assert (st["linked"], st["signed_in"]) == (False, True)


def test_a_tenant_bundle_homes_the_device_on_the_tenant_portal(monkeypatch):
    monkeypatch.setattr(link, "_stored_token", lambda: "tok")
    garg = {
        "role": "user",
        "identity": {"username": "gamer"},
        "tenant": {"id": "garg", "name": "GARG", "portal": "https://garg.aitherium.com"},
        "endpoints": {"portal": "https://garg.aitherium.com"},
    }
    assert link.refresh(fetch=lambda url, token: (200, garg))["ok"]
    st = link.status()
    assert (st["tenant"], st["tenant_name"], st["portal"]) == (
        "garg",
        "GARG",
        "https://garg.aitherium.com",
    )


def test_a_platform_bundle_has_no_tenant(monkeypatch):
    monkeypatch.setattr(link, "_stored_token", lambda: "tok")
    assert link.refresh(fetch=lambda url, token: (200, USER))["ok"]
    st = link.status()
    assert st["tenant"] is None and st["portal"] == "https://portal.example"
