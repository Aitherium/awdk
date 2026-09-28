"""The license follows the Aitherium account: sync, union with offline keys, app sign-in.

Pins the contract that replaced "paste a license":

* ``~/.aither/license.json`` is the ACCOUNT license (written by login / sync);
  offline keys live under ``~/.aither/licenses/`` and are never overwritten.
* adk.licensing unions the packs of every verified license; highest tier wins.
* An account sync replaces a stale ACCOUNT license (a refund must drop the pack)
  but moves a pre-existing OFFLINE license aside instead of destroying it.
* ``AccountLink`` drives the device flow for a product's web UI (mocked here).
"""

from __future__ import annotations

import argparse
import base64
import json
import time

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from adk import account_license as acct  # noqa: E402
from adk import licensing  # noqa: E402


@pytest.fixture
def key(monkeypatch, tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", pub)
    monkeypatch.setenv("AITHER_LICENSE_FILE", str(tmp_path / "license.json"))
    for var in ("AITHER_LICENSE_KEY", "AITHER_LICENSES_DIR", "AITHER_TENANT_SLUG",
                "AITHER_LICENSE_SYNC", "AITHER_LICENSE_MAX_AGE_HOURS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_LICENSE_ENFORCE", "1")
    licensing.reset_license_manager()
    yield priv
    licensing.reset_license_manager()


def _env(priv, packs, tier="community", account=False, expires_at=0.0):
    payload = {"tier": tier, "packs": packs, "issued_at": time.time(),
               "expires_at": expires_at}
    if account:
        payload["issued_via"] = "account"
    pb = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {"payload": base64.b64encode(pb).decode(), "signature": priv.sign(pb).hex()}


def _outer(env):
    return base64.b64encode(json.dumps(env).encode()).decode()


def _packs():
    return set(licensing.LicenseManager().license.packs)


# ── union ────────────────────────────────────────────────────────────────────

def test_account_and_offline_packs_are_unioned_highest_tier_wins(key, tmp_path):
    acct.save_account_license(_outer(_env(key, ["saga"], tier="starter", account=True)))
    licensing.install_offline_license(_env(key, ["deep-research"], tier="professional"))
    lic = licensing.LicenseManager().license
    assert set(lic.packs) == {"saga", "deep-research"}
    assert lic.tier is licensing.Tier.PROFESSIONAL


def test_forged_offline_license_adds_nothing(key, tmp_path):
    forged = _env(Ed25519PrivateKey.generate(), ["saga"])
    with pytest.raises(ValueError):
        licensing.install_offline_license(forged)
    ldir = tmp_path / "licenses"
    ldir.mkdir()
    (ldir / "x.json").write_text(json.dumps(forged))   # planted by hand: still ignored
    assert _packs() == set()


def test_expired_offline_license_is_ignored(key, tmp_path):
    ldir = tmp_path / "licenses"
    ldir.mkdir()
    (ldir / "old.json").write_text(json.dumps(_env(key, ["saga"], expires_at=time.time() - 5)))
    assert _packs() == set()


# ── account save never clobbers offline ──────────────────────────────────────

def test_sync_moves_a_preexisting_offline_license_aside(key, tmp_path):
    (tmp_path / "license.json").write_text(json.dumps(_env(key, ["agent-home"])))
    acct.save_account_license(_outer(_env(key, ["saga"], account=True)))
    assert _packs() == {"saga", "agent-home"}
    assert len(list((tmp_path / "licenses").glob("*.json"))) == 1


def test_refund_drops_the_pack_old_account_license_is_not_preserved(key, tmp_path):
    acct.save_account_license(_outer(_env(key, ["saga", "deep-research"], account=True)))
    acct.save_account_license(_outer(_env(key, ["saga"], account=True)))   # refund of DR
    assert _packs() == {"saga"}
    assert not (tmp_path / "licenses").exists()


def test_legacy_hmac_string_is_not_saved(key, tmp_path):
    assert acct.save_account_license("pro:user-1:unlimited:deadbeef") == ""
    assert not (tmp_path / "license.json").exists()


# ── sync ─────────────────────────────────────────────────────────────────────

def test_sync_saves_the_account_license(key, tmp_path):
    seen = {}

    def fetch(url, token, timeout):
        seen.update(url=url, token=token)
        return {"license_key": _outer(_env(key, ["saga"], account=True)),
                "tier": "community", "packs": ["saga"]}

    res = acct.sync_account_license("http://idp.test", token="tok", fetch=fetch)
    assert res == {"ok": True, "tier": "community", "packs": ["saga"], "error": ""}
    assert seen == {"url": "http://idp.test/auth/license", "token": "tok"}
    assert _packs() == {"saga"}
    assert acct.license_age_seconds() is not None


def test_sync_failure_keeps_what_is_installed(key, tmp_path):
    acct.save_account_license(_outer(_env(key, ["saga"], account=True)))

    class UnauthorizedError(Exception):
        code = 401

    def fetch(url, token, timeout):
        raise UnauthorizedError()

    res = acct.sync_account_license("http://idp.test", token="tok", fetch=fetch)
    assert res["ok"] is False and "sign in again" in res["error"]
    assert _packs() == {"saga"}


def test_sync_without_a_session_says_so(key, monkeypatch):
    monkeypatch.setattr(acct, "_saved_token", lambda: "")
    res = acct.sync_account_license("http://idp.test")
    assert res["ok"] is False and "not signed in" in res["error"]


def test_maybe_sync_only_when_stale(key, monkeypatch):
    calls = []
    monkeypatch.setattr(acct, "sync_account_license",
                        lambda **kw: calls.append(kw) or {"ok": True})
    monkeypatch.setattr(acct, "_saved_token", lambda: "tok")
    assert acct.maybe_sync(max_age_hours=12) == {"ok": True}         # never synced
    acct._write_sync_state(synced_at=time.time())
    assert acct.maybe_sync(max_age_hours=12) is None                 # fresh
    acct._write_sync_state(synced_at=time.time() - 13 * 3600, attempted_at=time.time())
    assert acct.maybe_sync(max_age_hours=12) is None                 # just attempted
    monkeypatch.setenv("AITHER_LICENSE_SYNC", "0")
    acct._write_sync_state(synced_at=0, attempted_at=0)
    assert acct.maybe_sync(max_age_hours=12) is None                 # disabled
    assert len(calls) == 1


def test_maybe_sync_not_signed_in_is_a_noop(key, monkeypatch):
    monkeypatch.setattr(acct, "_saved_token", lambda: "")
    assert acct.maybe_sync(max_age_hours=0) is None


# ── AccountLink: the "Sign in with Aitherium" button ─────────────────────────

class _FakeLink(acct.AccountLink):
    def __init__(self, result=None, poll_error=None, **kw):
        super().__init__(identity_url="http://idp.test", **kw)
        self.result, self.poll_error = result, poll_error

    def _request_code(self, identity_url):
        return {"user_code": "ABCD-1234", "device_code": "dev-1",
                "verification_uri": "http://idp.test/device",
                "verification_uri_complete": "http://idp.test/device?code=ABCD-1234",
                "expires_in": 60, "interval": 2}

    def _poll(self, identity_url, device_code, interval, expires_in):
        assert device_code == "dev-1" and expires_in <= 60
        if self.poll_error:
            raise RuntimeError(self.poll_error)
        return self.result

    def _complete(self, identity_url, result):
        return "pip"


def test_account_link_shows_code_then_unlocks(key, monkeypatch):
    monkeypatch.setattr(acct, "_identity_url", lambda url="": "http://idp.test")
    linked = []
    link = _FakeLink(result={"access_token": "tok",
                             "license_key": _outer(_env(key, ["saga"], account=True))},
                     on_linked=linked.append)
    shown = link.start()
    assert shown["state"] == "pending" and shown["user_code"] == "ABCD-1234"
    assert shown["verification_uri_complete"].endswith("code=ABCD-1234")
    final = link.wait(5)
    assert final["state"] == "linked" and final["username"] == "pip" and final["synced"]
    assert _packs() == {"saga"}
    assert linked and linked[0]["state"] == "linked"


def test_account_link_falls_back_to_sync_for_an_older_identity(key, monkeypatch):
    monkeypatch.setattr(acct, "_identity_url", lambda url="": "http://idp.test")
    monkeypatch.setattr(acct, "sync_account_license",
                        lambda url, token: {"ok": True, "tier": "starter", "error": ""})
    link = _FakeLink(result={"access_token": "tok", "license_key": "free:u:2027:hmac"})
    link.start()
    final = link.wait(5)
    assert final["state"] == "linked" and final["tier"] == "starter"


def test_account_link_reports_a_refused_code(key, monkeypatch):
    monkeypatch.setattr(acct, "_identity_url", lambda url="": "http://idp.test")
    link = _FakeLink(poll_error="device code access_denied")
    link.start()
    final = link.wait(5)
    assert final["state"] == "error" and "access_denied" in final["error"]


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_license_sync_and_install(key, monkeypatch, capsys):
    from adk import cli

    monkeypatch.setattr(acct, "sync_account_license",
                        lambda url="": {"ok": True, "tier": "starter", "packs": ["saga"],
                                        "error": ""})
    assert cli.cmd_license(argparse.Namespace(license_command="sync", portal_url="",
                                              json=False)) == 0
    assert "tier 'starter'" in capsys.readouterr().out
    good = _outer(_env(key, ["deep-research"]))
    assert cli.cmd_license(argparse.Namespace(license_command="install",
                                              license_key=good)) == 0
    assert "deep-research" in _packs()
    assert cli.cmd_license(argparse.Namespace(license_command="install",
                                              license_key="garbage")) == 1
