"""The fingerprint must change on a real edit and stay put on a no-op rewrite."""

from __future__ import annotations

from pathlib import Path

from adk import code_fingerprint


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "pkg"
    (root / "sub").mkdir(parents=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "sub" / "b.py").write_text("y = 2\n", encoding="utf-8")
    return root


def test_shape_and_count(tmp_path: Path) -> None:
    fp = code_fingerprint.compute(_tree(tmp_path))
    scheme, digest, count = fp.split(":")
    assert scheme == "sha256"
    assert len(digest) == 24
    assert count == "2"


def test_identical_rewrite_is_stable(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    before = code_fingerprint.compute(root)
    # Rewriting the same bytes bumps mtime; the fingerprint must not move.
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert code_fingerprint.compute(root) == before


def test_content_edit_changes_it(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    before = code_fingerprint.compute(root)
    (root / "sub" / "b.py").write_text("y = 3\n", encoding="utf-8")
    assert code_fingerprint.compute(root) != before


def test_rename_and_addition_change_it(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    before = code_fingerprint.compute(root)
    (root / "a.py").rename(root / "c.py")
    renamed = code_fingerprint.compute(root)
    assert renamed != before
    (root / "d.py").write_text("", encoding="utf-8")
    assert code_fingerprint.compute(root) != renamed


def test_pycache_is_ignored(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    before = code_fingerprint.compute(root)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.cpython-312.py").write_text("junk", encoding="utf-8")
    assert code_fingerprint.compute(root) == before


def test_startup_matches_a_fresh_compute() -> None:
    # Nothing edits the package during a test run, so the launch value equals disk.
    assert code_fingerprint.STARTUP == code_fingerprint.compute()
