"""Live WAL-mode SQLite graph DBs must sync as consistent snapshots.

Before the fix, adk sync read ``graph/*.db`` as raw bytes: committed rows that
still lived in the ``-wal`` sidecar were missing from the upload, and the
``-wal``/``-shm`` sidecars were synced as independent files.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from adk.sync import adk_sync_engine as eng
from adk.sync.adk_sync_engine import ADKSyncEngine, BaseManifestDB


class _Kind:
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE_LOCAL = "delete_local"
    DELETE_REMOTE = "delete_remote"
    CONFLICT = "conflict"
    NOOP = "noop"


@pytest.fixture
def aither_root():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "graph").mkdir()
        (root / "memory").mkdir()
        (root / "sync").mkdir()
        yield root


@pytest.fixture
def live_wal_db(aither_root):
    """A WAL DB whose committed rows sit un-checkpointed in the -wal file."""
    db = aither_root / "graph" / "kg.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.executemany("INSERT INTO nodes (name) VALUES (?)", [(f"n{i}",) for i in range(200)])
    conn.commit()
    assert (aither_root / "graph" / "kg.db-wal").stat().st_size > 0
    yield db, conn
    conn.close()


def _engine(root, drive):
    e = ADKSyncEngine(drive_client=drive, manifest_db=BaseManifestDB(root / "sync" / "b.json"))
    e.aither_root = root
    return e


def _rows(blob: bytes, tmpdir: Path) -> int:
    p = tmpdir / "restored.db"
    p.write_bytes(blob)
    c = sqlite3.connect(str(p))
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        c.close()


def test_upload_of_live_wal_db_carries_committed_rows(aither_root, live_wal_db):
    drive = AsyncMock()
    engine = _engine(aither_root, drive)
    action = SimpleNamespace(kind=_Kind.UPLOAD, path="graph/kg.db", base_version=0)
    with patch.object(eng, "_get_actionkind", return_value=_Kind):
        asyncio.run(engine._apply_action(action))
    uploaded = drive.upload.call_args[0][1]
    with tempfile.TemporaryDirectory() as tmp:
        assert _rows(uploaded, Path(tmp)) == 200


def test_scan_skips_sidecars_and_hashes_the_snapshot(aither_root, live_wal_db):
    engine = _engine(aither_root, AsyncMock())
    fs = lambda **kw: SimpleNamespace(**kw)  # noqa: E731
    with patch.object(eng, "_get_filestate", return_value=fs):
        first = engine.scan_local()
        second = engine.scan_local()
    assert set(first) == {"graph/kg.db"}, first.keys()
    # Snapshot is deterministic for unchanged content -> no spurious re-uploads.
    assert first["graph/kg.db"].hash == second["graph/kg.db"].hash


def test_download_over_sqlite_drops_stale_sidecars(aither_root, live_wal_db):
    db, conn = live_wal_db
    snapshot = eng.read_sync_bytes(db)
    conn.close()
    stale_wal = aither_root / "graph" / "kg.db-wal"
    stale_wal.write_bytes(b"stale wal from the previous database")
    drive = AsyncMock()
    drive.download = AsyncMock(return_value=snapshot)
    engine = _engine(aither_root, drive)
    action = SimpleNamespace(kind=_Kind.DOWNLOAD, path="graph/kg.db", base_version=0)
    with patch.object(eng, "_get_actionkind", return_value=_Kind):
        asyncio.run(engine._apply_action(action))
    assert not stale_wal.exists()
    c = sqlite3.connect(str(db))
    try:
        assert c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 200
    finally:
        c.close()


def test_non_sqlite_files_are_read_verbatim(aither_root):
    p = aither_root / "memory" / "m.jsonl"
    p.write_bytes(b'{"a": 1}\n')
    assert eng.read_sync_bytes(p) == b'{"a": 1}\n'


def test_remote_and_base_sidecars_never_reach_reconcile(aither_root):
    """A -wal already in the cloud manifest (uploaded before the fix) must not be
    DOWNLOADED onto a fresh device, where it would be replayed onto the DB."""
    fs = lambda **kw: SimpleNamespace(**kw)  # noqa: E731
    remote_state = fs(hash="h", size=1, mtime=0.0, version=3, deleted=False)
    drive = AsyncMock()
    drive.list_changes = AsyncMock(return_value=(0, {
        "graph/kg.db": remote_state,
        "graph/kg.db-wal": remote_state,
        "graph/kg.db-shm": remote_state,
    }))
    engine = _engine(aither_root, drive)
    engine.manifest_db.set_base({
        "graph/kg.db-wal": {"hash": "h", "size": 1, "mtime": 0.0, "version": 2, "deleted": False},
    })
    seen = {}

    def fake_reconcile(local, remote, base, endpoint=None):
        seen["remote"], seen["base"] = set(remote), set(base)
        return []

    with patch.object(eng, "_get_filestate", return_value=fs), \
            patch.object(eng, "_get_reconcile", return_value=fake_reconcile), \
            patch.object(eng, "_get_actionkind", return_value=_Kind):
        asyncio.run(engine.reconcile_once())
    assert seen["remote"] == {"graph/kg.db"}
    assert seen["base"] == set()


def _remote_with_prefix_sidecar(aither_root, live_wal_db):
    """Remote as left by a pre-fix client: kg.db plus an uploaded kg.db-wal."""
    db, conn = live_wal_db
    snapshot = eng.read_sync_bytes(db)
    conn.close()
    fresh = aither_root.parent / (aither_root.name + "-fresh-device")
    for sub in ("graph", "memory", "sync"):
        (fresh / sub).mkdir(parents=True)
    blobs = {"graph/kg.db": snapshot, "graph/kg.db-wal": b"stale wal from a pre-fix client"}
    return fresh, blobs


def _run_cycles(fresh, blobs, reconcile, filestate, kind, cycles=2):
    import hashlib

    remote = {
        p: filestate(hash=hashlib.sha256(b).hexdigest(), size=len(b), mtime=1.0, version=1)
        for p, b in blobs.items()
    }
    drive = AsyncMock()
    drive.list_changes = AsyncMock(return_value=(1, remote))
    drive.download = AsyncMock(side_effect=lambda p: blobs[p])
    engine = _engine(fresh, drive)
    with patch.object(eng, "_get_reconcile", return_value=reconcile), \
         patch.object(eng, "_get_filestate", return_value=filestate), \
         patch.object(eng, "_get_actionkind", return_value=kind):
        for _ in range(cycles):
            asyncio.run(engine.reconcile_once())
    return [c.args[0] for c in drive.download.call_args_list]


def test_remote_sidecar_is_never_downloaded(aither_root, live_wal_db):
    """D-7 download direction, engine-level: whatever reconcile is handed,
    a sidecar path never reaches it and is never written locally."""
    fresh, blobs = _remote_with_prefix_sidecar(aither_root, live_wal_db)
    seen = []

    def fake_reconcile(local, remote, base, endpoint=""):
        seen.append(set(remote) | set(base))
        return [SimpleNamespace(kind=_Kind.DOWNLOAD, path=p, base_version=1)
                for p in remote if p not in local]

    fs = lambda **kw: SimpleNamespace(**{"deleted": False, **kw})  # noqa: E731
    downloads = _run_cycles(fresh, blobs, fake_reconcile, fs, _Kind)
    assert downloads == ["graph/kg.db"], downloads
    assert all("graph/kg.db-wal" not in s for s in seen)
    assert not (fresh / "graph" / "kg.db-wal").exists()


def test_sidecar_actions_are_skipped_by_apply(aither_root):
    drive = AsyncMock()
    engine = _engine(aither_root, drive)
    for kind in (_Kind.DOWNLOAD, _Kind.UPLOAD, _Kind.DELETE_REMOTE):
        action = SimpleNamespace(kind=kind, path="graph/kg.db-wal", base_version=1)
        with patch.object(eng, "_get_actionkind", return_value=_Kind):
            asyncio.run(engine._apply_action(action))
    drive.download.assert_not_called()
    drive.upload.assert_not_called()
    drive.delete.assert_not_called()
    assert not (aither_root / "graph" / "kg.db-wal").exists()


def test_remote_sidecar_with_real_reconcile(aither_root, live_wal_db):
    """The reviewer's repro with the real drive_sync_core.reconcile."""
    try:
        reconcile = eng._get_reconcile()
        filestate = eng._get_filestate()
        kind = eng._get_actionkind()
    except RuntimeError as exc:
        pytest.skip(f"AitherOS host environment unavailable: {exc}")
    fresh, blobs = _remote_with_prefix_sidecar(aither_root, live_wal_db)
    downloads = _run_cycles(fresh, blobs, reconcile, filestate, kind, cycles=3)
    assert "graph/kg.db-wal" not in downloads, downloads
    assert downloads.count("graph/kg.db") == 1, downloads
    assert not (fresh / "graph" / "kg.db-wal").exists()


def test_snapshot_of_a_downloaded_snapshot_hashes_identically(aither_root, live_wal_db):
    """backup() bumps header counters; without normalisation a downloaded DB
    rescans as a local edit and is re-synced every cycle."""
    db, conn = live_wal_db
    snapshot = eng.read_sync_bytes(db)
    conn.close()
    other = aither_root / "graph" / "copy.db"
    eng.write_sync_bytes(other, snapshot)
    assert eng.read_sync_bytes(other) == snapshot
    with tempfile.TemporaryDirectory() as tmp:
        assert _rows(snapshot, Path(tmp)) == 200


def test_snapshot_is_stable_across_sqlite_library_versions(aither_root, live_wal_db):
    """Header offset 96 is SQLITE_VERSION_NUMBER of the library that last
    wrote the file; backup() restamps it with the local library's version.
    A peer whose Python bundles a different SQLite must produce the same
    snapshot bytes, or two devices re-upload graph/*.db to each other forever."""
    db, conn = live_wal_db
    local = eng.read_sync_bytes(db)
    conn.close()
    # What a peer on SQLite 3.45.1 gets out of backup() for the same content,
    # after its own engine's post-processing.
    foreign = bytearray(local)
    foreign[96:100] = (3045001).to_bytes(4, "big")
    peer_snapshot = eng._normalize_sqlite_header(bytes(foreign))
    assert peer_snapshot == local
    downloaded = aither_root / "graph" / "from-peer.db"
    eng.write_sync_bytes(downloaded, peer_snapshot)
    assert eng.read_sync_bytes(downloaded) == peer_snapshot
    with tempfile.TemporaryDirectory() as tmp:
        assert _rows(peer_snapshot, Path(tmp)) == 200
