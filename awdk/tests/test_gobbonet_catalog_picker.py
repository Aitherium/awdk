"""The catalog reaching GobboNet's picker, and swapping to a model you do not have yet.

The failure this pins is a SILENT ONE and it nearly shipped: `_catalog_entries()`
degrades to `[]` when the catalog module cannot be imported, which is correct
behaviour for a trimmed pack and is also exactly what a broken import looks
like. Every one of the pack's existing endpoint tests passes with the merge
completely inert, so "the suite is green" says nothing here. These assert the
catalog rows are actually PRESENT.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from adk.packs.gobbonet import catalog
from adk.packs.gobbonet import models as models_mod
from adk.packs.gobbonet.models import ModelManager


@pytest.fixture()
def models_dir(tmp_path: Path) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    (d / "Mistral-Small-Q4_K_M.gguf").write_bytes(b"GGUF")
    return d


def test_picker_offers_models_that_are_not_installed(models_dir):
    rows = ModelManager(models_dir=models_dir).models_list()["models"]
    offered = [r for r in rows if r.get("installed") is False]
    assert offered, "a fresh machine must see what it COULD have, not an empty list"
    assert len(rows) == 1 + len(catalog.entries())


def test_an_empty_models_folder_still_lists_the_catalog(tmp_path):
    """The whole point. An empty dropdown reads as 'no models exist'; the truth
    is 'nothing told this machine what exists', and they want opposite fixes."""
    d = tmp_path / "empty"
    d.mkdir()
    rows = ModelManager(models_dir=d).models_list()["models"]
    assert len(rows) == len(catalog.entries())
    assert all(r["installed"] is False for r in rows)


def test_catalog_rows_carry_every_key_gobbonets_builder_reads(models_dir):
    rows = ModelManager(models_dir=models_dir).models_list()["models"]
    for r in rows:
        for key in ("file", "name", "id", "family", "thinkingFormat", "active"):
            assert key in r, f"{r.get('file')} missing {key}"


def test_the_download_size_is_visible_in_the_name(models_dir):
    """A picker that offers a 16 GB download without saying so costs an evening."""
    rows = ModelManager(models_dir=models_dir).models_list()["models"]
    for r in rows:
        if r.get("installed") is False:
            assert "GB download" in r["name"]
            assert r["sizeGb"] > 0


def test_an_installed_model_is_not_also_offered_as_a_download(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    have = catalog.entries()[0].filename
    (d / have).write_bytes(b"GGUF")
    rows = ModelManager(models_dir=d).models_list()["models"]
    assert [r["file"] for r in rows].count(have) == 1
    assert next(r for r in rows if r["file"] == have)["installed"] is True


def test_swapping_to_a_catalog_model_is_accepted_and_reports_downloading(models_dir, monkeypatch):
    """Not installed is no longer a refusal — it is a download."""
    entry = catalog.entries()[0]
    calls = {}

    def fake_download(e, dest_dir=None, progress=None, **kw):
        calls["entry"] = e
        if progress:
            progress(e.size_bytes // 2, e.size_bytes)
        (Path(dest_dir) / e.filename).write_bytes(b"GGUF")
        return Path(dest_dir) / e.filename

    monkeypatch.setattr(catalog, "download", fake_download)
    m = ModelManager(models_dir=models_dir, _spawn=lambda *a, **k: None)
    monkeypatch.setattr(m, "_wait_until_answering", lambda *a, **k: True)

    accepted, msg = m.swap(entry.filename)
    assert accepted and msg == "downloading"
    _wait_for(m, "ready")
    assert calls["entry"].filename == entry.filename
    assert (models_dir / entry.filename).exists()


def test_a_failed_download_reaches_the_ui_as_an_error(models_dir, monkeypatch):
    entry = catalog.entries()[0]

    def boom(*a, **k):
        raise catalog.SizeMismatchError("got 5 bytes, expected 100")

    monkeypatch.setattr(catalog, "download", boom)
    m = ModelManager(models_dir=models_dir, _spawn=lambda *a, **k: None)
    assert m.swap(entry.filename)[0] is True
    st = _wait_for(m, "error")
    assert "download failed" in st["message"]
    assert "expected 100" in st["message"], "the reason must survive to the UI"


def test_download_progress_is_published_where_their_poller_shows_it(models_dir, monkeypatch):
    """Without this the UI sits on a bare 'loading' for a multi-GB transfer,
    which is indistinguishable from a hang — and a user who believes it hung
    kills it, discarding real progress."""
    entry = catalog.entries()[0]
    seen = []

    def slow_download(e, dest_dir=None, progress=None, **kw):
        progress(e.size_bytes // 4, e.size_bytes)
        seen.append(m.swap_status())
        (Path(dest_dir) / e.filename).write_bytes(b"GGUF")
        return Path(dest_dir) / e.filename

    monkeypatch.setattr(catalog, "download", slow_download)
    m = ModelManager(models_dir=models_dir, _spawn=lambda *a, **k: None)
    monkeypatch.setattr(m, "_wait_until_answering", lambda *a, **k: True)
    m.swap(entry.filename)
    _wait_for(m, "ready")
    assert seen and "25%" in seen[0]["message"]
    assert entry.label in seen[0]["message"]


# ── the poll-window projection, as a pure function ────────────────────────────
def test_no_warning_before_a_rate_can_be_measured():
    """Projecting off the first chunk reports nonsense in both directions."""
    assert models_mod.poll_window_note(elapsed=0.5, done=1, total=10 ** 10) == ""


def test_no_warning_for_a_download_that_beats_the_window():
    """An unwarranted warning trains people to ignore the one that matters."""
    # 100 MB in 4s => ~25 MB/s => the whole 400 MB lands in ~16s.
    assert models_mod.poll_window_note(elapsed=4.0, done=100 << 20, total=400 << 20) == ""


def test_warning_when_the_measured_rate_will_not_make_it():
    # 10 MB in 10s => 1 MB/s => 4 GB takes over an hour.
    note = models_mod.poll_window_note(elapsed=10.0, done=10 << 20, total=4096 << 20)
    assert "three-minute" in note


def test_the_same_size_warns_or_not_depending_on_the_connection():
    """The point of measuring instead of thresholding on bytes: one file, two
    machines, two correct answers."""
    total = 500 << 20
    fast = models_mod.poll_window_note(elapsed=4.0, done=200 << 20, total=total)
    slow = models_mod.poll_window_note(elapsed=4.0, done=2 << 20, total=total)
    assert fast == ""
    assert "three-minute" in slow


def test_swapping_to_an_unknown_file_still_refuses(models_dir):
    ok, msg = ModelManager(models_dir=models_dir).swap("not-a-real-model.gguf")
    assert ok is False
    assert "not-a-real-model.gguf" in msg


def _wait_for(manager, phase, timeout=10.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        st = manager.swap_status()
        if st["phase"] == phase:
            return st
        time.sleep(0.05)
    raise AssertionError(f"never reached {phase}: {manager.swap_status()}")
