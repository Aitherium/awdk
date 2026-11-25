"""adk pack new|validate|dev|build -- the author half of packs."""
from __future__ import annotations

import hashlib
import os
import tarfile
from pathlib import Path

import pytest
from adk import pack_author as pa


def test_scaffold_validates_loads_and_builds(tmp_path):
    pack = pa.scaffold("dev.hello", tmp_path, author="tester")
    rep = pa.validate(pack)
    assert rep.ok, rep.findings
    res = pa.dev_load(pack)
    assert res.ok, res.error
    assert res.registered == 1
    assert res.tool_names == ["hello_echo"]
    built = pa.build(pack, tmp_path / "out")
    assert built.tarball.name == "dev.hello-0.1.0.tar.gz"
    assert hashlib.sha256(built.tarball.read_bytes()).hexdigest() == built.sha256
    with tarfile.open(built.tarball) as tf:
        names = tf.getnames()
    assert "dev.hello-0.1.0/.toolpack.yaml" in names
    assert not any("__pycache__" in n for n in names)


def test_build_is_reproducible(tmp_path):
    pack = pa.scaffold("dev.repro", tmp_path)
    a = pa.build(pack, tmp_path / "a").sha256
    os.utime(pack / "tools.py", (1, 1))  # mtime must not reach the bytes
    b = pa.build(pack, tmp_path / "b").sha256
    assert a == b


def test_scaffold_refuses_reserved_and_existing(tmp_path):
    with pytest.raises(ValueError):
        pa.scaffold("aither.clock", tmp_path)
    pa.scaffold("dev.once", tmp_path)
    with pytest.raises(FileExistsError):
        pa.scaffold("dev.once", tmp_path)


def _codes(rep):
    return {f.code for f in rep.findings}


def test_validate_catches_each_defect(tmp_path):
    pack = pa.scaffold("dev.bad", tmp_path)
    mf = pack / pa.MANIFEST
    text = mf.read_text("utf-8")

    mf.write_text(text.replace("id: dev.bad", "id: aither.bad"), "utf-8")
    assert "PKA003" in _codes(pa.validate(pack))
    assert "PKA003" not in _codes(pa.validate(pack, community=False))

    mf.write_text(text.replace("version: 0.1.0", "version: one"), "utf-8")
    assert "PKA004" in _codes(pa.validate(pack))

    mf.write_text(text.replace("tier: community", "tier: gold"), "utf-8")
    assert "PKA009" in _codes(pa.validate(pack))

    mf.write_text(text, "utf-8")
    (pack / "__init__.py").write_text("x = 1\n", "utf-8")
    assert "PKA005" in _codes(pa.validate(pack))


def test_validate_flags_secret_without_echoing_it(tmp_path, capsys):
    pack = pa.scaffold("dev.leaky", tmp_path)
    fake = "ghp_" + "A" * 36
    (pack / "config.py").write_text(f'TOKEN = "{fake}"\n', "utf-8")
    rep = pa.validate(pack)
    assert "PKA008" in _codes(rep)
    assert all(fake not in str(f) for f in rep.findings)
    with pytest.raises(ValueError):
        pa.build(pack, tmp_path / "out")


def test_validate_missing_manifest_and_dir(tmp_path):
    assert pa.validate(tmp_path).exit_code == 1
    missing = pa.validate(tmp_path / "nope")
    assert missing.exit_code == 2


def test_dev_load_fails_when_register_returns_zero(tmp_path):
    pack = pa.scaffold("dev.empty", tmp_path)
    (pack / "__init__.py").write_text("def register(registry):\n    return 0\n", "utf-8")
    res = pa.dev_load(pack)
    assert not res.ok and "0 tools" in res.error


def test_cli_exit_codes(tmp_path, capsys):
    class A:
        pass

    a = A()
    a.pack_id, a.dest, a.author = "dev.cli", str(tmp_path), "t"
    assert pa.cli("new", a) == 0
    v = A()
    v.pack_dir, v.first_party = str(Path(tmp_path, "dev.cli")), False
    assert pa.cli("validate", v) == 0
    assert pa.cli("dev", v) == 0
    v.output = str(tmp_path / "dist")
    assert pa.cli("build", v) == 0
    assert pa.cli("frobnicate", v) == 2


def _built(tmp_path):
    root = pa.scaffold("me.localpack", tmp_path / "src", author="t")
    return pa.build(root, tmp_path / "dist")


def test_install_local_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PACK_REQUIRE_SIGNING", raising=False)
    monkeypatch.delenv("AITHER_PACK_PUBLIC_KEY", raising=False)
    res = _built(tmp_path)
    assert pa.is_local_bundle(str(res.tarball)) and not pa.is_local_bundle("me.localpack")
    packs = tmp_path / "packs"
    target = pa.install_local(res.tarball, packs)
    assert target == (packs / "me.localpack").resolve()
    assert (target / ".toolpack.yaml").is_file() and (target / pa.LOCAL_MARKER).is_file()
    # a re-install replaces its own previous local install
    assert pa.install_local(res.tarball, packs) == target


def test_install_local_refuses_bad_sum_unsigned_and_foreign_target(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PACK_PUBLIC_KEY", raising=False)
    res = _built(tmp_path)
    packs = tmp_path / "packs"
    (packs / "me.localpack").mkdir(parents=True)  # a marketplace pack already there
    monkeypatch.setenv("AITHER_PACK_REQUIRE_SIGNING", "false")
    with pytest.raises(ValueError, match="not installed from a local bundle"):
        pa.install_local(res.tarball, packs)
    monkeypatch.setenv("AITHER_PACK_REQUIRE_SIGNING", "true")
    with pytest.raises(ValueError, match="signature"):
        pa.install_local(res.tarball, tmp_path / "other")
    monkeypatch.setenv("AITHER_PACK_REQUIRE_SIGNING", "false")
    side = res.tarball.with_name(res.tarball.name + ".sha256")
    side.write_text("0" * 64 + "  x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256 mismatch"):
        pa.install_local(res.tarball, tmp_path / "other")


def test_keygen_sign_and_verify_roundtrip(tmp_path, monkeypatch):
    monkeypatch.delenv("AITHER_PACK_PUBLIC_KEY", raising=False)
    kp = pa.keygen(tmp_path / "keys")
    assert kp.private_path.is_file() and kp.public_path.is_file()
    assert kp.public_path.read_text("utf-8").strip() == kp.public_key_hex
    assert len(kp.fingerprint.split(":")) == 4

    pack = pa.scaffold("dev.signed", tmp_path)
    res = pa.sign(pack, key_path=kp.private_path, out_dir=tmp_path / "out")
    assert res.signature_path.name == res.tarball.name + ".sig"
    assert res.fingerprint == kp.fingerprint

    ok, why = pa.verify(res.tarball, public_key=kp.public_key_hex)
    assert ok, why


def test_verify_rejects_tampering_and_a_foreign_key(tmp_path):
    kp = pa.keygen(tmp_path / "keys")
    other = pa.keygen(tmp_path / "other-keys")
    pack = pa.scaffold("dev.tamper", tmp_path)
    res = pa.sign(pack, key_path=kp.private_path, out_dir=tmp_path / "out")

    # the same bytes verify under the key that signed them
    assert pa.verify(res.tarball, public_key=kp.public_key_hex)[0]

    # one changed byte in the bundle fails
    blob = bytearray(res.tarball.read_bytes())
    blob[-1] ^= 0xFF
    res.tarball.write_bytes(bytes(blob))
    ok, _ = pa.verify(res.tarball, public_key=kp.public_key_hex)
    assert not ok, "a tampered bundle verified"

    # a bundle signed by a different key fails the first key
    res2 = pa.sign(pa.scaffold("dev.tamper2", tmp_path), key_path=other.private_path,
                   out_dir=tmp_path / "out2")
    ok, _ = pa.verify(res2.tarball, public_key=kp.public_key_hex)
    assert not ok, "a foreign key's signature verified"

    # and no sidecar at all is a refusal, never a pass
    res2.signature_path.unlink()
    ok, why = pa.verify(res2.tarball, public_key=kp.public_key_hex)
    assert not ok and "signature" in why.lower()


def test_keygen_refuses_to_replace_an_existing_key(tmp_path):
    pa.keygen(tmp_path / "keys")
    with pytest.raises(FileExistsError):
        pa.keygen(tmp_path / "keys")


def test_signed_bundle_installs_under_required_signing(tmp_path, monkeypatch):
    kp = pa.keygen(tmp_path / "keys")
    pack = pa.scaffold("dev.installable", tmp_path)
    res = pa.sign(pack, key_path=kp.private_path, out_dir=tmp_path / "out")

    monkeypatch.setenv("AITHER_PACK_PUBLIC_KEY", kp.public_key_hex)
    monkeypatch.setenv("AITHER_PACK_REQUIRE_SIGNING", "true")
    target = pa.install_local(res.tarball, tmp_path / "packs")
    assert target == (tmp_path / "packs" / "dev.installable").resolve()
    assert (target / pa.LOCAL_MARKER).is_file()


def test_verify_returns_a_verdict_when_the_sidecar_is_unreadable(tmp_path, monkeypatch):
    """`verify` promises it never raises. An unreadable .sig is a verdict, not a crash."""
    kp = pa.keygen(tmp_path / "keys")
    res = pa.sign(pa.scaffold("dev.unreadable", tmp_path), key_path=kp.private_path,
                  out_dir=tmp_path / "out")
    real = Path.read_text

    def boom(self, *a, **k):
        if self.name.endswith(".sig"):
            raise PermissionError("denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    ok, why = pa.verify(res.tarball, public_key=kp.public_key_hex)
    assert not ok, "an unreadable sidecar verified"
    assert "cannot read" in why


def test_sign_reports_an_unwritable_output_instead_of_crashing(tmp_path, monkeypatch):
    """A read-only dist directory must be a clean error the CLI can print, not a traceback."""
    kp = pa.keygen(tmp_path / "keys")
    pack = pa.scaffold("dev.readonly", tmp_path)
    real = Path.write_text

    def boom(self, *a, **k):
        if self.name.endswith(".sig"):
            raise PermissionError("read-only file system")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(ValueError, match="cannot write the signature"):
        pa.sign(pack, key_path=kp.private_path, out_dir=tmp_path / "out")


def test_sign_refuses_bytes_that_are_not_a_tarball(tmp_path):
    """Signing junk produces a signature that verifies perfectly and installs nothing."""
    kp = pa.keygen(tmp_path / "keys")
    junk = tmp_path / "not-a-bundle.tar.gz"
    junk.write_bytes(b"this is not a tar archive")
    with pytest.raises(ValueError, match="not a readable tar.gz"):
        pa.sign(junk, key_path=kp.private_path)


def test_verify_returns_a_verdict_when_the_bundle_is_unreadable(tmp_path, monkeypatch):
    """The last way `verify` could still raise: the bundle itself cannot be read."""
    kp = pa.keygen(tmp_path / "keys")
    res = pa.sign(pa.scaffold("dev.unreadable-bundle", tmp_path), key_path=kp.private_path,
                  out_dir=tmp_path / "out")
    real = Path.read_bytes

    def boom(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    ok, why = pa.verify(res.tarball, public_key=kp.public_key_hex)
    assert not ok, "an unreadable bundle verified"
    assert "cannot read" in why
    monkeypatch.setattr(Path, "read_bytes", real)
