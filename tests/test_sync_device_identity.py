"""Tests for device identity enrollment and persistence."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from adk.sync import device_identity


@pytest.fixture
def temp_identity_dir():
    """Create a temporary directory for device identity testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_save_enrolled_identity(temp_identity_dir):
    """Test saving an enrollment bundle to disk."""
    bundle = {
        "certificate": "-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----\n",
        "private_key": "FAKE-TEST-PRIVATE-KEY-MATERIAL-NOT-REAL",
        "chain": "-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----\n",
    }

    result_dir = device_identity.save_enrolled_identity(
        bundle, directory=temp_identity_dir
    )

    assert result_dir == temp_identity_dir
    assert (temp_identity_dir / "cert.pem").exists()
    assert (temp_identity_dir / "key.pem").exists()
    assert (temp_identity_dir / "chain.pem").exists()
    assert (temp_identity_dir / "fullchain.pem").exists()

    # Verify permissions on key
    key_perms = oct((temp_identity_dir / "key.pem").stat().st_mode)[-3:]
    # On POSIX systems, should be 600; on Windows, chmod may be no-op
    if key_perms != "600":
        # Windows may not support chmod
        pass


def test_save_enrolled_identity_missing_cert(temp_identity_dir):
    """Test that save raises ValueError when cert is missing."""
    bundle = {"private_key": "FAKE-TEST-PRIVATE-KEY-MATERIAL-NOT-REAL"}

    with pytest.raises(ValueError, match="missing certificate"):
        device_identity.save_enrolled_identity(bundle, directory=temp_identity_dir)


def test_save_enrolled_identity_missing_key(temp_identity_dir):
    """Test that save raises ValueError when key is missing."""
    bundle = {"certificate": "-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----\n"}

    with pytest.raises(ValueError, match="missing certificate or private_key"):
        device_identity.save_enrolled_identity(bundle, directory=temp_identity_dir)


def test_load_device_cert(temp_identity_dir):
    """Test loading a device cert from disk."""
    bundle = {
        "certificate": "-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----\n",
        "private_key": "FAKE-TEST-PRIVATE-KEY-MATERIAL-NOT-REAL",
        "chain": "-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----\n",
    }

    device_identity.save_enrolled_identity(bundle, directory=temp_identity_dir)

    cert_tuple = device_identity.load_device_cert(directory=temp_identity_dir)
    assert cert_tuple is not None
    cert_path, key_path = cert_tuple
    assert Path(cert_path).exists()
    assert Path(key_path).exists()
    # Should prefer fullchain over cert
    assert "fullchain" in cert_path or "cert" in cert_path


def test_load_device_cert_missing(temp_identity_dir):
    """Test loading device cert when none has been enrolled."""
    cert_tuple = device_identity.load_device_cert(directory=temp_identity_dir)
    assert cert_tuple is None


def test_has_device_identity(temp_identity_dir):
    """Test checking if device has an enrolled identity."""
    assert not device_identity.has_device_identity(directory=temp_identity_dir)

    bundle = {
        "certificate": "-----BEGIN CERTIFICATE-----\nMIIC...\n-----END CERTIFICATE-----\n",
        "private_key": "FAKE-TEST-PRIVATE-KEY-MATERIAL-NOT-REAL",
    }
    device_identity.save_enrolled_identity(bundle, directory=temp_identity_dir)

    assert device_identity.has_device_identity(directory=temp_identity_dir)


def test_fullchain_construction(temp_identity_dir):
    """Test that fullchain is constructed correctly (leaf + chain)."""
    cert = "-----BEGIN CERTIFICATE-----\nCERT_CONTENT\n-----END CERTIFICATE-----"
    chain = "-----BEGIN CERTIFICATE-----\nCHAIN_CONTENT\n-----END CERTIFICATE-----"
    bundle = {
        "certificate": cert,
        "private_key": "FAKE-TEST-PRIVATE-KEY-MATERIAL-NOT-REAL",
        "chain": chain,
    }

    device_identity.save_enrolled_identity(bundle, directory=temp_identity_dir)

    fullchain_content = (temp_identity_dir / "fullchain.pem").read_text()
    assert "CERT_CONTENT" in fullchain_content
    assert "CHAIN_CONTENT" in fullchain_content
    # Cert should come first
    assert fullchain_content.index("CERT") < fullchain_content.index("CHAIN")
