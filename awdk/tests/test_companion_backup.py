"""Portable companion-vault backup/restore tests.

A companion is normally pinned to one machine (the at-rest key mixes in the machine
fingerprint). A backup must break that pin SAFELY: restore on a different machine
with the passphrase, never readable by the operator (who has no passphrase).
"""
import json
from pathlib import Path

import pytest

import adk.private_companion as pc
from adk.private_companion import PrivateCompanionVault

pytestmark = pytest.mark.skipif(not pc.HAS_CRYPTO, reason="cryptography not installed")


def _point_lockbox(monkeypatch, base: Path) -> Path:
    """Redirect ALL vault path globals to an isolated lockbox dir (one 'machine')."""
    lb = base / "lockbox"
    lb.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pc, "LOCKBOX_DIR", lb)
    monkeypatch.setattr(pc, "VAULT_KEY_FILE", lb / ".vault_key")
    monkeypatch.setattr(pc, "MACHINE_ID_FILE", lb / ".machine_id")
    monkeypatch.setattr(pc, "SALT_FILE", lb / ".vault_salt")
    monkeypatch.setattr(pc, "MANIFEST_FILE", lb / ".manifest.json")
    monkeypatch.setattr(pc, "SAFETY_LEVEL_FILE", lb / ".safety_level")
    return lb


def test_export_import_roundtrip_across_machines(monkeypatch, tmp_path):
    # ── Machine A: create a companion, export a backup ──
    _point_lockbox(monkeypatch, tmp_path / "A")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-AAAA")
    vault_a = PrivateCompanionVault()
    vault_a.store_persona("avia", "You are Avia, auburn hair, green eyes.", "unrestricted")
    vault_a.set_safety_level("unrestricted")
    bundle = vault_a.export_backup("correct horse battery staple")

    # ── Machine B: a DIFFERENT machine, fresh empty vault ──
    _point_lockbox(monkeypatch, tmp_path / "B")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-BBBB")
    vault_b = PrivateCompanionVault()
    assert vault_b.list_personas() == []  # genuinely empty / different box

    result = vault_b.import_backup(bundle, "correct horse battery staple")
    assert result["count"] == 1
    # The persona decrypts correctly on the new machine (re-pinned to machine-BBBB).
    persona = vault_b.get_persona("avia")
    assert persona is not None
    assert persona.content == "You are Avia, auburn hair, green eyes."
    assert persona.safety_level == "unrestricted"
    assert vault_b.get_safety_level() == "unrestricted"
    # And it survives a fresh re-open on machine B (key re-wrapped to this box).
    reopened = PrivateCompanionVault()
    assert reopened.get_persona("avia").content == "You are Avia, auburn hair, green eyes."


def test_wrong_passphrase_rejected(monkeypatch, tmp_path):
    _point_lockbox(monkeypatch, tmp_path / "A")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-AAAA")
    vault_a = PrivateCompanionVault()
    vault_a.store_persona("avia", "secret persona body", "unrestricted")
    bundle = vault_a.export_backup("right-pass")

    _point_lockbox(monkeypatch, tmp_path / "B")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-BBBB")
    vault_b = PrivateCompanionVault()
    with pytest.raises(ValueError, match="passphrase"):
        vault_b.import_backup(bundle, "WRONG-pass")


def test_operator_blind_bundle_has_no_plaintext(monkeypatch, tmp_path):
    _point_lockbox(monkeypatch, tmp_path / "A")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-AAAA")
    vault = PrivateCompanionVault()
    secret = "auburn-hair-freckles-SECRET-MARKER-42"
    vault.store_persona("avia", f"You are Avia, {secret}.", "unrestricted")
    bundle = vault.export_backup("pw")
    # The operator (who holds the bundle but no passphrase) must not see the body.
    assert secret.encode() not in bundle
    assert b"auburn" not in bundle
    # It IS a structured, recognizable bundle though.
    parsed = json.loads(bundle)
    assert parsed["format"] == pc.BACKUP_FORMAT
    assert "wrapped_data_key" in parsed and "avia" in parsed["personas"]


def test_no_clobber_without_overwrite(monkeypatch, tmp_path):
    _point_lockbox(monkeypatch, tmp_path / "A")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-AAAA")
    src = PrivateCompanionVault()
    src.store_persona("avia", "persona A", "unrestricted")
    bundle = src.export_backup("pw")

    # Destination already has a companion — refuse to clobber by default.
    _point_lockbox(monkeypatch, tmp_path / "B")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-BBBB")
    dst = PrivateCompanionVault()
    dst.store_persona("existing", "do not lose me", "unrestricted")
    with pytest.raises(ValueError, match="overwrite"):
        dst.import_backup(bundle, "pw")
    # overwrite=True replaces it.
    result = dst.import_backup(bundle, "pw", overwrite=True)
    assert result["count"] == 1
    assert dst.get_persona("avia").content == "persona A"


def test_export_requires_passphrase(monkeypatch, tmp_path):
    _point_lockbox(monkeypatch, tmp_path / "A")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-AAAA")
    vault = PrivateCompanionVault()
    vault.store_persona("avia", "body", "unrestricted")
    with pytest.raises(ValueError, match="passphrase"):
        vault.export_backup("")


def test_file_roundtrip(monkeypatch, tmp_path):
    _point_lockbox(monkeypatch, tmp_path / "A")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-AAAA")
    vault_a = PrivateCompanionVault()
    vault_a.store_persona("avia", "file roundtrip body", "casual")
    out = vault_a.export_backup_to_file(tmp_path / "backup.acb", "pw")
    assert Path(out).exists()

    _point_lockbox(monkeypatch, tmp_path / "B")
    monkeypatch.setattr(pc, "get_machine_id", lambda: "machine-BBBB")
    vault_b = PrivateCompanionVault()
    vault_b.import_backup_from_file(out, "pw")
    assert vault_b.get_persona("avia").content == "file roundtrip body"
