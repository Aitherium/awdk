"""A pip install finds the BUNDLED addon manifests, which carry no _schema.yaml."""

from __future__ import annotations

from pathlib import Path

from adk import addon_manager as am


def test_bundled_dir_without_schema_is_found(tmp_path, monkeypatch):
    bundled = tmp_path / "addon_manifests"
    bundled.mkdir()
    (bundled / "awsh.yaml").write_text("id: awsh\n", encoding="utf-8")
    monkeypatch.setattr(am, "_BUNDLED_DIR", bundled)
    monkeypatch.setattr(am, "_MANIFEST_DIRS", [str(tmp_path / "missing"), str(bundled)])
    assert am._find_manifest_dir() == bundled


def test_bundled_dir_with_only_underscore_files_is_not_a_manifest_dir(tmp_path, monkeypatch):
    bundled = tmp_path / "addon_manifests"
    bundled.mkdir()
    (bundled / "_notes.yaml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.setattr(am, "_BUNDLED_DIR", bundled)
    monkeypatch.setattr(am, "_MANIFEST_DIRS", [str(bundled)])
    assert am._find_manifest_dir() is None


def test_a_non_bundled_dir_still_needs_the_schema_marker(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "awsh.yaml").write_text("id: awsh\n", encoding="utf-8")
    monkeypatch.setattr(am, "_BUNDLED_DIR", tmp_path / "addon_manifests")
    monkeypatch.setattr(am, "_MANIFEST_DIRS", [str(other)])
    assert am._find_manifest_dir() is None
    (other / "_schema.yaml").write_text("x: 1\n", encoding="utf-8")
    assert am._find_manifest_dir() == other


def test_the_real_bundled_dir_resolves_from_the_package():
    # The shipped tree holds manifests; with the monorepo dirs removed it must still resolve.
    real = Path(am.__file__).parent / "addon_manifests"
    assert any(real.glob("*.yaml"))
