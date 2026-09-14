"""License startup messaging tests — three cases: (a) no license, (b) invalid sig, (c) corrupt."""

from __future__ import annotations

import base64
import json

import pytest

from adk.license_startup import gate_startup
from adk.licensing import Tier, reset_license_manager


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts from a pristine environment."""
    for var in (
        "AITHER_TENANT_SLUG", "AITHER_LICENSE_KEY", "AITHER_LICENSE_FILE",
        "AITHER_LICENSE_ENFORCE", "AITHER_LICENSE_PUBLIC_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_license_manager()
    yield
    reset_license_manager()


def test_startup_case_a_no_license(caplog):
    """Case (a): no license anywhere — community, specific guidance."""
    import logging
    caplog.set_level(logging.INFO)

    lic = gate_startup("test_product_a")
    assert lic.tier == Tier.COMMUNITY
    assert "no license" in caplog.text.lower()
    assert "100k tokens/month" in caplog.text
    assert "sovereign=$1000/perpetual" in caplog.text


def test_startup_case_b_invalid_signature(monkeypatch, tmp_path, caplog):
    """Case (b): license file provided but signature invalid — warning."""
    import logging
    caplog.set_level(logging.WARNING)

    payload = {
        "tier": "enterprise",
        "tenant_id": "hacker",
        "entitlements": {"fleet": True},
    }
    envelope = {
        "payload": base64.b64encode(json.dumps(payload).encode()).decode(),
        "signature": "deadbeef" * 8,  # bogus
    }
    lf = tmp_path / "license.json"
    lf.write_text(json.dumps(envelope), encoding="utf-8")
    monkeypatch.setenv("AITHER_LICENSE_FILE", str(lf))

    lic = gate_startup("test_product_b")
    assert lic.tier == Tier.COMMUNITY
    # Either the key is not configured ("...NO verification key...") or the
    # signature failed against a real baked-in key ("signature INVALID"); both
    # are loud warnings that fall back to the free tier.
    _t = caplog.text.lower()
    assert "present" in _t or "key" in _t or "invalid" in _t


def test_startup_case_c_valid_license(monkeypatch, tmp_path, caplog):
    """Case (c): valid license — standard log with tier info."""
    import logging
    caplog.set_level(logging.INFO)

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        pub_hex = pub.public_bytes_raw().hex()

        payload = {
            "tier": "professional",
            "tenant_id": "cust-123",
            "entitlements": {"max_effort": 10},
        }
        payload_bytes = json.dumps(payload).encode()
        sig_hex = priv.sign(payload_bytes).hex()

        envelope = {
            "payload": base64.b64encode(payload_bytes).decode(),
            "signature": sig_hex,
        }
        lf = tmp_path / "license.json"
        lf.write_text(json.dumps(envelope), encoding="utf-8")
        monkeypatch.setenv("AITHER_LICENSE_FILE", str(lf))
        monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", pub_hex)

        lic = gate_startup("test_product_c")
        assert lic.tier == Tier.PROFESSIONAL
        assert "tier=professional" in caplog.text
        assert "cust-123" in caplog.text
    except ImportError:
        pytest.skip("cryptography not installed")


def test_startup_sovereign_shows_unlimited(monkeypatch, tmp_path, caplog):
    """SOVEREIGN tier startup shows unlimited tokens."""
    import logging
    caplog.set_level(logging.INFO)

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        pub_hex = pub.public_bytes_raw().hex()

        payload = {
            "tier": "sovereign",
            "tenant_id": "consumer-001",
        }
        payload_bytes = json.dumps(payload).encode()
        sig_hex = priv.sign(payload_bytes).hex()

        envelope = {
            "payload": base64.b64encode(payload_bytes).decode(),
            "signature": sig_hex,
        }
        lf = tmp_path / "license.json"
        lf.write_text(json.dumps(envelope), encoding="utf-8")
        monkeypatch.setenv("AITHER_LICENSE_FILE", str(lf))
        monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", pub_hex)

        lic = gate_startup("test_product_sovereign")
        assert lic.tier == Tier.SOVEREIGN
        assert "monthly_tokens=unlimited" in caplog.text
    except ImportError:
        pytest.skip("cryptography not installed")
