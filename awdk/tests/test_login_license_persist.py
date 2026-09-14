"""adk login must persist the account's signed license so pack entitlements flow
from the user's AitherIdentity/ACTA account into the runtime (previously dropped)."""

import base64
import json
import unittest.mock as mock
from pathlib import Path

import adk.cli as cli


def _envelope_key(tier: str = "professional") -> str:
    """A base64 outer envelope like AitherIdentity returns as `license_key`."""
    payload = base64.b64encode(
        json.dumps({"tier": tier, "entitlements": {"can_use_formbridge": True}}).encode()
    ).decode()
    envelope = {"payload": payload, "signature": "sig"}
    return base64.b64encode(json.dumps(envelope).encode()).decode()


def test_valid_license_written_as_envelope(tmp_path):
    with mock.patch.object(Path, "home", return_value=tmp_path):
        tier = cli._save_account_license({"license_key": _envelope_key(), "tier": "professional"})
    assert tier == "professional"
    lic = tmp_path / ".aither" / "license.json"
    assert lic.exists()
    # LicenseManager's file path expects the decoded {payload, signature} envelope.
    assert set(json.loads(lic.read_text()).keys()) == {"payload", "signature"}


def test_absent_license_is_noop(tmp_path):
    with mock.patch.object(Path, "home", return_value=tmp_path):
        assert cli._save_account_license({}) == ""
        assert cli._save_account_license({"license_key": ""}) == ""
    assert not (tmp_path / ".aither" / "license.json").exists()


def test_garbage_license_fails_soft(tmp_path):
    with mock.patch.object(Path, "home", return_value=tmp_path):
        # non-base64 / non-envelope must never raise and never write a bad file
        assert cli._save_account_license({"license_key": "not-base64!!!"}) == ""
        assert cli._save_account_license({"license_key": base64.b64encode(b'"notdict"').decode()}) == ""
    assert not (tmp_path / ".aither" / "license.json").exists()
