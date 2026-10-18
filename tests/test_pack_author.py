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
