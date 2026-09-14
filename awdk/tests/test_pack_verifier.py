"""Tests for client-side pack verification (adk SDK).

Covers:
  - Standalone verification without AitherOS imports
  - Ed25519 signature verification
  - Tamper detection
  - Graceful handling when verification not available
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def test_signing_key() -> tuple[str, str]:
    """Generate a test Ed25519 keypair.

    Returns: (private_key_hex, public_key_hex)
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519

        private_key = ed25519.Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes_raw()
        public_bytes = private_key.public_key().public_bytes_raw()

        return private_bytes.hex(), public_bytes.hex()
    except ImportError:
        pytest.skip("cryptography not available")


@pytest.fixture
def sample_pack_data() -> bytes:
    """Sample pack tarball data."""
    return b"fake pack tarball content\n" * 100


class TestPackVerifierBasics:
    """Test basic client-side pack verification."""

    def test_verify_bytes_valid_signature(
        self, sample_pack_data: bytes, test_signing_key
    ):
        """Test verification of a valid signature."""
        from adk.pack_verifier import verify_bytes

        priv_hex, pub_hex = test_signing_key

        # Sign with cryptography
        from cryptography.hazmat.primitives.asymmetric import ed25519

        private_key_bytes = bytes.fromhex(priv_hex)
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            private_key_bytes
        )
        signature = private_key.sign(sample_pack_data)
        sig_hex = signature.hex()

        # Verify with adk
        assert verify_bytes(sample_pack_data, sig_hex, pub_hex)

    def test_verify_bytes_invalid_signature(
        self, sample_pack_data: bytes, test_signing_key
    ):
        """Test verification fails with invalid signature."""
        from adk.pack_verifier import verify_bytes

        priv_hex, pub_hex = test_signing_key

        # Bad signature: all zeros
        bad_sig = "0" * 128
        assert not verify_bytes(sample_pack_data, bad_sig, pub_hex)

    def test_verify_bytes_tampered_data(
        self, sample_pack_data: bytes, test_signing_key
    ):
        """Test verification fails when data is tampered."""
        from adk.pack_verifier import verify_bytes
        from cryptography.hazmat.primitives.asymmetric import ed25519

        priv_hex, pub_hex = test_signing_key

        # Sign original data
        private_key_bytes = bytes.fromhex(priv_hex)
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            private_key_bytes
        )
        signature = private_key.sign(sample_pack_data)
        sig_hex = signature.hex()

        # Tamper with data
        tampered = sample_pack_data[:-10] + b"TAMPERED!!"

        # Should not verify
        assert not verify_bytes(tampered, sig_hex, pub_hex)

    def test_verify_bytes_no_public_key(self, sample_pack_data: bytes):
        """Test verification fails gracefully without public key."""
        from adk.pack_verifier import verify_bytes

        bad_sig = "a" * 128

        with patch.dict(os.environ, {}, clear=True):
            with patch("adk.pack_verifier.get_pack_public_key") as mock:
                mock.return_value = None
                result = verify_bytes(sample_pack_data, bad_sig)
                assert result is False


class TestPackVerifierTarball:
    """Test tarball-specific verification."""

    def test_verify_pack_tarball_no_signature_no_key(
        self, sample_pack_data: bytes, monkeypatch
    ):
        """Unsigned pack is accepted when no public key is configured (backward compat)."""
        from adk.pack_verifier import verify_pack_tarball

        monkeypatch.delenv("AITHER_PACK_REQUIRE_SIGNING", raising=False)
        monkeypatch.delenv("AITHER_PACK_PUBLIC_KEY", raising=False)

        verified, message = verify_pack_tarball(sample_pack_data, None)
        assert verified is True
        assert "unsigned" in message.lower() or "no signature" in message.lower()

    def test_verify_pack_tarball_no_signature_with_key_rejected(
        self, sample_pack_data: bytes, test_signing_key, monkeypatch
    ):
        """Unsigned pack is rejected when a public key is configured (anti-strip)."""
        from adk.pack_verifier import verify_pack_tarball

        _priv_hex, pub_hex = test_signing_key
        monkeypatch.delenv("AITHER_PACK_REQUIRE_SIGNING", raising=False)

        verified, message = verify_pack_tarball(sample_pack_data, None, pub_hex)
        assert verified is False
        assert "required" in message.lower()

    def test_verify_pack_tarball_valid_signature(
        self, sample_pack_data: bytes, test_signing_key
    ):
        """Test successful tarball verification."""
        from adk.pack_verifier import verify_pack_tarball
        from cryptography.hazmat.primitives.asymmetric import ed25519

        priv_hex, pub_hex = test_signing_key

        # Sign the data
        private_key_bytes = bytes.fromhex(priv_hex)
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            private_key_bytes
        )
        signature = private_key.sign(sample_pack_data)
        sig_hex = signature.hex()

        verified, message = verify_pack_tarball(sample_pack_data, sig_hex, pub_hex)
        assert verified is True
        assert "verified" in message.lower()

    def test_verify_pack_tarball_bad_signature(
        self, sample_pack_data: bytes, test_signing_key
    ):
        """Test that bad signatures are rejected."""
        from adk.pack_verifier import verify_pack_tarball

        priv_hex, pub_hex = test_signing_key

        bad_sig = "0" * 128

        verified, message = verify_pack_tarball(sample_pack_data, bad_sig, pub_hex)
        assert verified is False
        assert "failed" in message.lower()

    def test_verify_pack_tarball_tampered(
        self, sample_pack_data: bytes, test_signing_key
    ):
        """Test that tampered tarballs are rejected."""
        from adk.pack_verifier import verify_pack_tarball
        from cryptography.hazmat.primitives.asymmetric import ed25519

        priv_hex, pub_hex = test_signing_key

        # Sign original
        private_key_bytes = bytes.fromhex(priv_hex)
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            private_key_bytes
        )
        signature = private_key.sign(sample_pack_data)
        sig_hex = signature.hex()

        # Tamper
        tampered = sample_pack_data[:-10] + b"CORRUPTED!"

        verified, message = verify_pack_tarball(tampered, sig_hex, pub_hex)
        assert verified is False


class TestPackVerifierConfiguration:
    """Test configuration and key resolution."""

    def test_get_pack_public_key_from_env(self, test_signing_key):
        """Test reading public key from environment."""
        from adk.pack_verifier import get_pack_public_key

        priv_hex, pub_hex = test_signing_key

        with patch.dict(os.environ, {"AITHER_PACK_PUBLIC_KEY": pub_hex}):
            key = get_pack_public_key()
            assert key == pub_hex

    def test_get_pack_public_key_not_configured(self):
        """Test reading public key when not configured."""
        from adk.pack_verifier import get_pack_public_key

        with patch.dict(os.environ, {}, clear=True):
            with patch("adk.pack_verifier._DEFAULT_PUBLIC_KEY_HEX", "0" * 64):
                key = get_pack_public_key()
                assert key is None


class TestPackVerifierErrorHandling:
    """Test error handling in pack verifier."""

    def test_verify_bytes_malformed_key(self, sample_pack_data: bytes):
        """Test verification with malformed key returns False."""
        from adk.pack_verifier import verify_bytes

        sig_hex = "a" * 128
        # Invalid hex key
        result = verify_bytes(sample_pack_data, sig_hex, "not-hex")
        assert result is False

    def test_verify_bytes_malformed_signature(self, sample_pack_data: bytes, test_signing_key):
        """Test verification with malformed signature returns False."""
        from adk.pack_verifier import verify_bytes

        priv_hex, pub_hex = test_signing_key

        # Invalid hex signature
        bad_sig = "not-hex-signature"
        result = verify_bytes(sample_pack_data, bad_sig, pub_hex)
        assert result is False

    def test_verify_pack_tarball_malformed_key(self, sample_pack_data: bytes):
        """Test tarball verification with malformed key."""
        from adk.pack_verifier import verify_pack_tarball

        bad_sig = "a" * 128
        verified, message = verify_pack_tarball(
            sample_pack_data, bad_sig, "not-hex-key"
        )
        assert verified is False
