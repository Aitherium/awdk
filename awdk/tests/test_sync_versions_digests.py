"""The brew formula's own sha256 must not stay a placeholder after publish.

A version bump necessarily precedes the PyPI publish, so `sync_brew` writes
`PLACEHOLDER_SHA256` and someone is supposed to fill it afterwards. Nobody ever
did, so every release shipped a formula `brew install` cannot verify — and
`--check` passed anyway, which is what made it invisible.

`sync_brew_digest` fills it from PyPI and, crucially, FAILS `--check` when the
version is published but the digest is still a placeholder.

The fetcher is injected in every test — these must never touch the network.
"""
from __future__ import annotations

import importlib.util
import shutil
import urllib.error
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sync_versions", Path(__file__).resolve().parent.parent / "packaging" / "sync_versions.py"
)
sync_versions = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync_versions)

REAL_SHA = "a" * 64
FORMULA = Path(__file__).resolve().parent.parent / "packaging" / "brew" / "awdk.rb"


@pytest.fixture
def formula(tmp_path, monkeypatch):
    """Operate on a COPY — a test must never rewrite the real formula."""
    pkg = tmp_path / "brew"
    pkg.mkdir()
    target = pkg / "awdk.rb"
    shutil.copy(FORMULA, target)

    # sync_brew_digest resolves the path via __file__.parent / "brew"
    monkeypatch.setattr(sync_versions, "__file__", str(tmp_path / "sync_versions.py"))
    return target


def _set_sha(path: Path, value: str) -> None:
    text = path.read_text(encoding="utf-8")
    first = text.index('sha256 "')
    end = text.index('"', first + len('sha256 "'))
    path.write_text(text[: first + len('sha256 "')] + value + text[end:], encoding="utf-8")


def _first_sha(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    first = text.index('sha256 "') + len('sha256 "')
    return text[first : text.index('"', first)]


# ── filling ────────────────────────────────────────────────────────────────


def test_fills_placeholder_when_published(formula):
    _set_sha(formula, "PLACEHOLDER_SHA256")
    ok = sync_versions.sync_brew_digest("9.9.9", check=False, fetch=lambda v: REAL_SHA)
    assert ok
    assert _first_sha(formula) == REAL_SHA


def test_leaves_placeholder_when_not_published(formula):
    _set_sha(formula, "PLACEHOLDER_SHA256")
    ok = sync_versions.sync_brew_digest("9.9.9", check=False, fetch=lambda v: None)
    assert ok, "an unpublished version must not fail the release"
    assert _first_sha(formula) == "PLACEHOLDER_SHA256"


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("dns down"),
        urllib.error.HTTPError("u", 503, "unavailable", None, None),
        TimeoutError("slow"),
        OSError("socket died"),
    ],
)
def test_real_fetcher_swallows_network_failures(monkeypatch, exc):
    """A transient PyPI outage must yield None, never propagate — a release
    must not fail because pypi.org blinked."""

    def _raise(*a, **k):
        raise exc

    monkeypatch.setattr(sync_versions.urllib.request, "urlopen", _raise)
    assert sync_versions.fetch_sdist_sha256("9.9.9") is None


def test_wheel_only_release_yields_no_sdist_digest(monkeypatch):
    """No sdist on PyPI must be reported as None, not crash on a missing key."""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"urls": [{"packagetype": "bdist_wheel", "digests": {}}]}'

    monkeypatch.setattr(sync_versions.json, "load", lambda f: {
        "urls": [{"packagetype": "bdist_wheel", "digests": {"sha256": "x"}}]
    })
    monkeypatch.setattr(sync_versions.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert sync_versions.fetch_sdist_sha256("9.9.9") is None


# ── the check that used to pass silently ───────────────────────────────────


def test_check_FAILS_on_published_version_with_placeholder(formula):
    """The whole point: this used to pass and ship a broken formula."""
    _set_sha(formula, "PLACEHOLDER_SHA256")
    ok = sync_versions.sync_brew_digest("9.9.9", check=True, fetch=lambda v: REAL_SHA)
    assert ok is False


def test_check_FAILS_on_stale_digest(formula):
    """A digest left over from the previous release is just as broken."""
    _set_sha(formula, "b" * 64)
    ok = sync_versions.sync_brew_digest("9.9.9", check=True, fetch=lambda v: REAL_SHA)
    assert ok is False


def test_check_passes_when_digest_matches(formula):
    _set_sha(formula, REAL_SHA)
    ok = sync_versions.sync_brew_digest("9.9.9", check=True, fetch=lambda v: REAL_SHA)
    assert ok is True


def test_check_passes_when_unpublished(formula):
    _set_sha(formula, "PLACEHOLDER_SHA256")
    ok = sync_versions.sync_brew_digest("9.9.9", check=True, fetch=lambda v: None)
    assert ok is True


# ── it must touch ONLY the formula's own digest ────────────────────────────


def test_resource_digests_are_never_rewritten(formula):
    """The dependency `resource` blocks carry their own real digests."""
    before = formula.read_text(encoding="utf-8")
    resource_shas = before.split('sha256 "')[2:]  # skip preamble + the first sha

    _set_sha(formula, "PLACEHOLDER_SHA256")
    sync_versions.sync_brew_digest("9.9.9", check=False, fetch=lambda v: REAL_SHA)

    after = formula.read_text(encoding="utf-8")
    assert after.split('sha256 "')[2:] == resource_shas, (
        "dependency resource digests must be untouched"
    )
    assert after.count(REAL_SHA) == 1


def test_real_fetcher_returns_none_for_a_nonexistent_version():
    """Exercises the real network path once, tolerating offline CI."""
    result = sync_versions.fetch_sdist_sha256("0.0.0-does-not-exist", timeout=10.0)
    assert result is None
