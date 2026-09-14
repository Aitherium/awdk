"""Containment tests for AitherShell's file browser.

A browser-reachable file explorer is a security surface. Every test here is a
containment proof, and each names the escape it blocks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from adk.harnesses.fs import FsDeniedError, browse_roots, list_dir, read_file, resolve_within


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("hello world", encoding="utf-8")
    (root / "sub" / "b.py").write_text("print(1)\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secrets.env").write_text("TOKEN=sk-live-do-not-leak", encoding="utf-8")
    return {"root": root, "outside": outside, "tmp": tmp_path}


def test_no_roots_means_no_browsing(tree):
    # Fail-closed: an unconfigured browser refuses rather than defaulting to /.
    with pytest.raises(FsDeniedError, match="no browsable root"):
        resolve_within(str(tree["root"]), [])


def test_path_outside_every_root_is_refused(tree):
    roots = [tree["root"].resolve()]
    with pytest.raises(FsDeniedError, match="outside every browsable root"):
        resolve_within(str(tree["outside"] / "secrets.env"), roots)


@pytest.mark.parametrize("attack", ["..", "../outside", "../outside/secrets.env",
                                    "sub/../../outside"])
def test_dot_dot_traversal_is_refused(tree, attack):
    roots = [tree["root"].resolve()]
    with pytest.raises(FsDeniedError):
        resolve_within(str(tree["root"] / attack), roots)


def _make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory link by whatever mechanism this host allows.

    Windows `os.symlink` needs privilege, but a JUNCTION does not — and a
    junction is resolved by `Path.resolve()` exactly like a symlink, so it
    exercises the same escape. Without this the single most important
    containment test silently skips on every unprivileged Windows box, which is
    the same "green because it did not run" failure this suite exists to stop.
    """
    reasons: list[str] = []
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError) as exc:
        # Windows symlinks need privilege. Keep the reason and fall through to a
        # junction; a swallowed error here would make a total failure look like
        # "this host just cannot", with nothing saying why.
        reasons.append(f"symlink: {exc}")
    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0 and link.exists():
            return True
        reasons.append(f"mklink /J: {result.stderr.strip() or result.stdout.strip()}")
    print("could not create a directory link:", "; ".join(reasons))
    return False


def test_symlink_escape_is_refused(tree):
    """resolve() happens BEFORE the containment check.

    A prefix test on the unresolved string would pass `<root>/link` while it
    points at a directory holding credentials. This is the classic hole.
    """
    link = tree["root"] / "escape"
    assert _make_dir_link(link, tree["outside"]), (
        "could not create a directory link by symlink OR junction; this host "
        "cannot run the containment test that matters most"
    )
    roots = [tree["root"].resolve()]
    with pytest.raises(FsDeniedError, match="outside every browsable root"):
        resolve_within(str(link / "secrets.env"), roots)


def test_listing_hides_noise_dirs_and_sorts_dirs_first(tree):
    roots = [tree["root"].resolve()]
    listing = list_dir(str(tree["root"]), roots)
    names = [e["name"] for e in listing["entries"]]
    assert ".git" not in names, "credential-bearing noise dirs must not be listed"
    assert names.index("sub") < names.index("a.txt")
    assert listing["truncated"] is False


def test_reading_a_file_inside_a_root_works(tree):
    roots = [tree["root"].resolve()]
    got = read_file(str(tree["root"] / "a.txt"), roots)
    assert got["content"] == "hello world"
    assert got["binary"] is False


def test_binary_is_described_not_decoded(tree):
    blob = tree["root"] / "blob.bin"
    blob.write_bytes(b"\x00\x01\x02binary")
    roots = [tree["root"].resolve()]
    got = read_file(str(blob), roots)
    # Decoding with errors="replace" yields plausible garbage that reads as a
    # corrupted source file rather than "this is a binary".
    assert got["binary"] is True
    assert got["content"] == ""
    assert got["reason"] == "binary file"


def test_oversized_file_is_reported_not_truncated_silently(tree, monkeypatch):
    import adk.harnesses.fs as fsmod

    monkeypatch.setattr(fsmod, "MAX_READ_BYTES", 4)
    roots = [tree["root"].resolve()]
    got = fsmod.read_file(str(tree["root"] / "a.txt"), roots)
    assert got["truncated"] is True and got["content"] == ""
    assert "at most 4" in got["reason"]


def test_browse_roots_prefers_explicit_env_then_falls_back_to_session_cwds(tree, monkeypatch):
    monkeypatch.delenv("AITHER_HARNESS_BROWSE_ROOTS", raising=False)
    monkeypatch.delenv("AITHER_HARNESS_ALLOWED_ROOTS", raising=False)
    # No env, no sessions -> nothing browsable at all.
    assert browse_roots([]) == []
    # A live session's cwd becomes browsable; nothing else does.
    assert browse_roots([str(tree["root"])]) == [tree["root"].resolve()]
    monkeypatch.setenv("AITHER_HARNESS_BROWSE_ROOTS", str(tree["outside"]))
    assert browse_roots([str(tree["root"])]) == [tree["outside"].resolve()]
