"""License signing-root rotation: adk trusts a SET of roots, additively.

2026-09-30: the private half of the first root (1a468a33...) existed nowhere, so the
shop could not sign. A new root was minted into the vault; every verifier must accept
BOTH, so a license sold under the first root keeps working. Throwaway roots stand in
for the real ones (the real private keys never enter a test).
"""

from __future__ import annotations

import base64
import json
import time

import pytest

import adk.licensing as lic

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")

FIRST_ROOT = "1a468a332d6cfc5378edf7083b6d845bcfdf141fbce28e6dbe521dd6b84e233f"
CURRENT_ROOT = "71f4e4da93095b3f1d29a3f01d27358d1398de9f0d5c0d8e3e0cb35199ac6980"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("AITHER_TENANT_SLUG", "AITHER_LICENSE_KEY", "AITHER_LICENSE_ENFORCE",
                "AITHER_LICENSE_PUBLIC_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AITHER_LICENSE_FILE", str(tmp_path / "license.json"))
    monkeypatch.setenv("AITHER_LICENSES_DIR", str(tmp_path / "licenses"))
    lic.reset_license_manager()
    yield
    lic.reset_license_manager()


def _root():
    sk = ed25519.Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes_raw().hex()


def _envelope(sk, packs=("deep-research",), tier="community"):
    payload = json.dumps({"tier": tier, "packs": list(packs), "tenant_id": "t",
                          "issued_at": time.time(), "expires_at": 0}).encode()
    return {"payload": base64.b64encode(payload).decode(), "signature": sk.sign(payload).hex()}


@pytest.fixture()
def roots(monkeypatch):
    """Throwaway 'current' + 'legacy' roots baked in place of the real ones."""
    cur_sk, cur_pub = _root()
    old_sk, old_pub = _root()
    monkeypatch.setattr(lic, "TRUSTED_LICENSE_PUBLIC_KEYS_HEX", (cur_pub, old_pub))
    return {"cur": cur_sk, "old": old_sk, "cur_pub": cur_pub, "old_pub": old_pub}


def test_baked_roots_are_current_first_and_keep_the_first_root():
    assert lic._LICENSE_PUBLIC_KEY_HEX == CURRENT_ROOT
    assert lic.TRUSTED_LICENSE_PUBLIC_KEYS_HEX[0] == CURRENT_ROOT
    assert FIRST_ROOT in lic.TRUSTED_LICENSE_PUBLIC_KEYS_HEX  # additive, never a swap
    assert lic.trusted_public_keys() == list(lic.TRUSTED_LICENSE_PUBLIC_KEYS_HEX)


def test_current_and_legacy_root_both_verify(roots):
    for who in ("cur", "old"):
        got = lic._license_from_envelope(_envelope(roots[who]), source="offline")
        assert got is not None and got.packs == ["deep-research"], who


def test_a_stranger_root_and_garbage_are_refused(roots):
    stranger, _ = _root()
    assert lic._license_from_envelope(_envelope(stranger), source="offline") is None
    env = _envelope(roots["cur"])
    assert lic._license_from_envelope({**env, "signature": "zz" * 64}, "offline") is None
    assert lic._license_from_envelope({**env, "signature": "00" * 64}, "offline") is None
    assert lic._verify_signature(b"anything", "not-hex") is False
    with pytest.raises(ValueError):
        lic.install_offline_license(_envelope(stranger))


def test_install_and_resolve_union_across_roots(roots):
    lic.install_offline_license(_envelope(roots["old"], packs=("saga",)))
    lic.install_offline_license(_envelope(roots["cur"], packs=("deep-research",)))
    lm = lic.get_license_manager()
    assert lm.is_pack_available("saga") and lm.is_pack_available("deep-research")


def test_env_override_replaces_the_baked_set(roots, monkeypatch):
    other_sk, other_pub = _root()
    monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", other_pub)
    assert lic._license_from_envelope(_envelope(other_sk), "offline") is not None
    assert lic._license_from_envelope(_envelope(roots["cur"]), "offline") is None
    # A list in the override is honoured too.
    monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", f"{other_pub}, {roots['old_pub']}")
    assert lic._license_from_envelope(_envelope(roots["old"]), "offline") is not None
    # Placeholder override verifies nothing (fail-closed) and says so.
    monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", "0" * 64)
    assert lic._public_key_is_placeholder() is True
    assert lic._license_from_envelope(_envelope(other_sk), "offline") is None
    # Blank override == unset: the baked roots answer.
    monkeypatch.setenv("AITHER_LICENSE_PUBLIC_KEY", "  ")
    assert lic._license_from_envelope(_envelope(roots["cur"]), "offline") is not None
