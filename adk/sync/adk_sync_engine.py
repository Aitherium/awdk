"""ADK Sync Engine — orchestrates three-way reconciliation for ~/.aither data.

This engine syncs adk's own local memory/graph/session data with the cloud
using the same three-way reconcile algorithm as awnode (drive_sync_core).

Adk's data directories:
  - ~/.aither/memory/* — persistent memory store (JSONL files)
  - ~/.aither/graph/* — knowledge graph (SQLite)
  - ~/.aither/config.yaml — session config

The engine is the coordinator between local filesystem, cloud drive, and
persistent base manifest. In reconcile_once():

  1. Scan local data files (compute SHA256 hashes)
  2. Get remote changes from cloud (via drive_client.list_changes)
  3. Load base from persistent storage
  4. Call drive_sync_core.reconcile() to compute actions
  5. Apply each action (UPLOAD, DOWNLOAD, DELETE, CONFLICT)
  6. Update base manifest atomically on success

Offline resilience: failed drive_client calls leave base unchanged so next
reconcile retries. CONFLICT actions preserve divergent content in a copy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from adk.sync.drive_client import DriveClient

log = logging.getLogger("adk.sync.adk_sync_engine")

# Lazy imports from AitherOS
_RECONCILE = None
_FILESTATE = None
_SYNCACTION = None
_ACTIONKIND = None


def _ensure_aitheros_path():
    """Inject AitherOS into sys.path if needed."""
    adk_dir = Path(__file__).parent.parent.parent  # awdk/
    aitheros_dir = adk_dir.parent / "AitherOS"
    if aitheros_dir.is_dir() and str(aitheros_dir) not in sys.path:
        sys.path.insert(0, str(aitheros_dir))


def _get_reconcile():
    """Lazy import reconcile from AitherOS."""
    global _RECONCILE
    if _RECONCILE is None:
        _ensure_aitheros_path()
        try:
            from lib.sync.drive_sync_core import reconcile
        except ImportError as exc:
            raise RuntimeError(
                "adk sync requires the AitherOS host environment "
                "(drive_sync_core is not shipped in the PyPI package): "
                f"{exc}") from exc
        _RECONCILE = reconcile
    return _RECONCILE


def _get_filestate():
    """Lazy import FileState from AitherOS."""
    global _FILESTATE
    if _FILESTATE is None:
        _ensure_aitheros_path()
        try:
            from lib.sync.drive_sync_core import FileState
        except ImportError as exc:
            raise RuntimeError(
                "adk sync requires the AitherOS host environment "
                "(drive_sync_core is not shipped in the PyPI package): "
                f"{exc}") from exc
        _FILESTATE = FileState
    return _FILESTATE


def _get_syncaction():
    """Lazy import SyncAction from AitherOS."""
    global _SYNCACTION
    if _SYNCACTION is None:
        _ensure_aitheros_path()
        try:
            from lib.sync.drive_sync_core import SyncAction
        except ImportError as exc:
            raise RuntimeError(
                "adk sync requires the AitherOS host environment "
                "(drive_sync_core is not shipped in the PyPI package): "
                f"{exc}") from exc
        _SYNCACTION = SyncAction
    return _SYNCACTION


def _get_actionkind():
    """Lazy import ActionKind from AitherOS."""
    global _ACTIONKIND
    if _ACTIONKIND is None:
        _ensure_aitheros_path()
        try:
            from lib.sync.drive_sync_core import ActionKind
        except ImportError as exc:
            raise RuntimeError(
                "adk sync requires the AitherOS host environment "
                "(drive_sync_core is not shipped in the PyPI package): "
                f"{exc}") from exc
        _ACTIONKIND = ActionKind
    return _ACTIONKIND


# ── SQLite-safe file IO ────────────────────────────────────────────────
# A live SQLite DB in WAL mode is THREE files: the main file lags the -wal
# until a checkpoint, and a raw byte copy taken mid-checkpoint is a torn,
# corrupt database. Sync therefore never reads a SQLite main file as raw
# bytes: it takes a transactionally consistent snapshot via the online
# backup API, and never syncs the -wal/-shm/-journal sidecars themselves.

_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def is_sqlite_sidecar(path: Path) -> bool:
    """True for a SQLite WAL/shared-memory/rollback-journal sidecar file."""
    return path.name.endswith(_SQLITE_SIDECAR_SUFFIXES)


def drop_sqlite_sidecars(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """``manifest`` without SQLite sidecar paths.

    Applied to the REMOTE and BASE manifests too, not only the local scan: a
    -wal/-shm uploaded before sidecars were excluded would otherwise be
    DOWNLOADED onto a fresh device, and because ``x.db`` sorts before
    ``x.db-wal`` it could land after the main DB's sidecar cleanup and be
    replayed onto the snapshot.
    """
    return {
        p: v for p, v in manifest.items()
        if not str(p).endswith(_SQLITE_SIDECAR_SUFFIXES)
    }


def is_sqlite_file(path: Path) -> bool:
    """True when the file starts with the SQLite 3 header magic."""
    try:
        with open(path, "rb") as f:
            return f.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
    except OSError:
        return False


def _sqlite_snapshot_bytes(path: Path) -> bytes:
    """Consistent point-in-time copy of a (possibly live, WAL-mode) SQLite DB."""
    with tempfile.TemporaryDirectory(prefix="adk-sync-") as tmp:
        dest_path = Path(tmp) / "snapshot.db"
        try:
            src = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            src = sqlite3.connect(str(path))
        try:
            dst = sqlite3.connect(str(dest_path))
            try:
                src.backup(dst)
                # A standalone snapshot must not claim WAL mode: its -wal
                # sidecar is never shipped with it.
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
        return _normalize_sqlite_header(dest_path.read_bytes())


# Header fields that ``backup()`` rewrites on every copy: the file change
# counter (offset 24), version-valid-for (offset 92) and SQLITE_VERSION_NUMBER
# of the library that last wrote the file (offset 96). Left as-is, a snapshot
# of a freshly DOWNLOADED snapshot hashes differently from what was downloaded
# -- and offset 96 differs between devices whose Python bundles a different
# SQLite -- so the next reconcile sees a local edit on top of the cloud copy
# and re-uploads (or conflicts) the DB every cycle. Offsets 24 and 92 are
# pinned to the same value, which keeps the in-header page count valid
# (trusted only when the two match); offset 96 is informational only.
_SQLITE_PINNED_HEADER_FIELDS = (
    (24, 28, 1),
    (92, 96, 1),
    (96, 100, 3000000),
)


def _normalize_sqlite_header(content: bytes) -> bytes:
    if len(content) < 100 or content[: len(_SQLITE_MAGIC)] != _SQLITE_MAGIC:
        return content
    buf = bytearray(content)
    for start, end, value in _SQLITE_PINNED_HEADER_FIELDS:
        buf[start:end] = value.to_bytes(end - start, "big")
    return bytes(buf)


def read_sync_bytes(path: Path) -> bytes:
    """Bytes to hash/upload for ``path``: a consistent snapshot for SQLite."""
    if is_sqlite_file(path):
        return _sqlite_snapshot_bytes(path)
    return path.read_bytes()


def write_sync_bytes(path: Path, content: bytes) -> None:
    """Write downloaded content atomically; drop stale SQLite sidecars.

    A leftover -wal/-shm from the PREVIOUS database would be replayed onto
    the newly downloaded main file on next open and corrupt it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError as exc:
            log.warning("could not remove temp file %s: %s", tmp_name, exc)
        raise
    if content[: len(_SQLITE_MAGIC)] == _SQLITE_MAGIC:
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
                log.info("removed stale SQLite sidecar %s", sidecar)


class BaseManifestDB:
    """Persist the base manifest to ~/.aither/sync/base_manifest.json.

    The base is the common ancestor in three-way merge — the manifest as it
    stood after the LAST successful sync.
    """

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the base manifest storage.

        Args:
            db_path: Override path (defaults to ~/.aither/sync/base_manifest.json)
        """
        if db_path is None:
            db_path = Path.home() / ".aither" / "sync" / "base_manifest.json"
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_base(self) -> Dict:
        """Load the base manifest from disk, or {} if never synced."""
        if not self.db_path.exists():
            return {}
        try:
            return json.loads(self.db_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Failed to load base manifest: {e}")
            return {}

    def set_base(self, manifest: Dict) -> None:
        """Persist the base manifest to disk."""
        try:
            self.db_path.write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.error(f"Failed to persist base manifest: {e}")


class ADKSyncEngine:
    """Orchestrates three-way file synchronization for adk's ~/.aither data."""

    def __init__(
        self,
        drive_client: "DriveClient",
        manifest_db: Optional[BaseManifestDB] = None,
        endpoint_name: str = "adk-device",
        include_dirs: Optional[List[str]] = None,
    ):
        """Initialize the ADKSyncEngine.

        Args:
            drive_client: DriveClient instance for cloud communication
            manifest_db: BaseManifestDB instance for base persistence
                (defaults to ~/.aither/sync/base_manifest.json)
            endpoint_name: Device name (used in conflict copy filenames)
            include_dirs: Directories under ~/.aither to sync.
                Defaults to ["memory", "graph"] (not config.yaml yet).
        """
        self.drive_client = drive_client
        self.manifest_db = manifest_db or BaseManifestDB()
        self.endpoint_name = endpoint_name
        self.aither_root = Path.home() / ".aither"
        self.include_dirs = include_dirs or ["memory", "graph"]

    def _get_sync_dirs(self) -> List[Path]:
        """Return the list of directories under ~/.aither to sync."""
        dirs = []
        for dir_name in self.include_dirs:
            d = self.aither_root / dir_name
            if d.is_dir():
                dirs.append(d)
        return dirs

    def scan_local(self) -> Dict[str, Any]:  # Dict[str, FileState]
        """Scan local adk data and compute content hashes.

        Recursively walks the configured ~/.aither subdirs, computing SHA256
        hashes for each file. Returns a manifest dict with relative paths
        (relative to ~/.aither).

        Returns:
            Dict[rel_path → FileState] with populated hash/size/mtime
        """
        FileState = _get_filestate()
        manifest = {}

        for sync_dir in self._get_sync_dirs():
            for local_path in sync_dir.rglob("*"):
                if local_path.is_dir():
                    continue
                if is_sqlite_sidecar(local_path):
                    # Folded into the main DB's snapshot; never synced alone.
                    continue

                # Compute path relative to ~/.aither
                try:
                    rel_path = str(local_path.relative_to(self.aither_root))
                except ValueError:
                    continue

                # Normalize path separators to forward slash
                rel_path = rel_path.replace("\\", "/")

                # Compute hash and size
                try:
                    content = read_sync_bytes(local_path)
                    file_hash = hashlib.sha256(content).hexdigest()
                    file_size = len(content)
                    file_mtime = local_path.stat().st_mtime

                    manifest[rel_path] = FileState(
                        hash=file_hash,
                        size=file_size,
                        mtime=file_mtime,
                        version=0,  # Local files have version=0 until uploaded
                        deleted=False,
                    )
                except (IOError, OSError, sqlite3.Error) as e:
                    log.warning(f"Failed to scan {local_path}: {e}")

        log.debug(f"Scanned local adk data: {len(manifest)} files")
        return manifest

    async def reconcile_once(self) -> List[Any]:  # List[SyncAction]
        """Run one full reconciliation cycle.

        Steps:
          1. Scan local filesystem
          2. Get remote changes from cloud
          3. Load base manifest from DB
          4. Call drive_sync_core.reconcile() to compute actions
          5. Apply each action
          6. Update base manifest atomically

        If any action fails, base is NOT updated, so next reconcile retries.

        Returns:
            List of applied SyncActions (for logging/UI)

        Raises:
            Exception: On unrecoverable errors
        """
        reconcile = _get_reconcile()
        ActionKind = _get_actionkind()

        # Step 1: Scan local (disk + SQLite snapshot IO -> off the event loop)
        local = await asyncio.to_thread(self.scan_local)

        # Step 2: Get remote changes (use full manifest from list_changes)
        try:
            _, remote = await self.drive_client.list_changes(since=0)
        except Exception as e:
            log.error(f"Failed to fetch remote changes: {e}")
            raise
        remote = drop_sqlite_sidecars(remote)

        # Step 3: Load base
        base_dict = drop_sqlite_sidecars(self.manifest_db.get_base())
        FileState = _get_filestate()
        base = {
            path: FileState(
                hash=fs.get("hash", ""),
                size=fs.get("size", 0),
                mtime=fs.get("mtime", 0.0),
                version=fs.get("version", 0),
                deleted=fs.get("deleted", False),
            )
            for path, fs in base_dict.items()
        }

        # Step 4: Reconcile
        actions = reconcile(
            local, remote, base, endpoint=self.endpoint_name
        )

        # Step 5: Apply each action
        applied_actions = []
        for action in actions:
            try:
                await self._apply_action(action)
                applied_actions.append(action)
            except Exception as e:
                log.error(f"Action {action.path} {action.kind} failed: {e}")
                # Stop on first failure — base NOT updated, retry on next cycle
                raise

        # Step 6: Update base manifest atomically on full success
        # Convert FileState objects back to dicts for JSON serialization
        new_base = {}
        for path, fs in local.items():
            new_base[path] = {
                "hash": fs.hash,
                "size": fs.size,
                "mtime": fs.mtime,
                "version": fs.version,
                "deleted": fs.deleted,
            }
        self.manifest_db.set_base(new_base)

        return applied_actions

    async def _apply_action(self, action: Any) -> None:  # SyncAction
        """Apply a single sync action.

        Args:
            action: The SyncAction to apply

        Raises:
            Exception: On application failures
        """
        ActionKind = _get_actionkind()
        path = action.path
        local_path = self.aither_root / path
        if is_sqlite_sidecar(local_path):
            # Defence in depth behind drop_sqlite_sidecars(): never sync one.
            log.info("SKIP %s %s (SQLite sidecar, never synced)", action.kind, path)
            return

        if action.kind == ActionKind.UPLOAD:
            # Upload local file to cloud
            log.info(f"UPLOAD {path}")
            content = await asyncio.to_thread(read_sync_bytes, local_path)
            await self.drive_client.upload(
                path, content, version=action.base_version
            )

        elif action.kind == ActionKind.DOWNLOAD:
            # Download cloud file to local
            log.info(f"DOWNLOAD {path}")
            content = await self.drive_client.download(path)
            write_sync_bytes(local_path, content)

        elif action.kind == ActionKind.DELETE_LOCAL:
            # Cloud deleted — remove local file
            log.info(f"DELETE_LOCAL {path}")
            if local_path.exists():
                local_path.unlink()

        elif action.kind == ActionKind.DELETE_REMOTE:
            # Local deleted — remove cloud file
            log.info(f"DELETE_REMOTE {path}")
            await self.drive_client.delete(path, version=action.base_version)

        elif action.kind == ActionKind.CONFLICT:
            # Both diverged — download cloud canonical, preserve local as conflict copy
            log.info(f"CONFLICT {path} → {action.conflict_copy}")

            # CRITICAL: Read original local content BEFORE overwriting local_path
            local_content: Optional[bytes] = None
            if local_path.exists():
                try:
                    local_content = await asyncio.to_thread(
                        read_sync_bytes, local_path
                    )
                except (IOError, OSError, sqlite3.Error) as e:
                    log.warning(
                        f"Failed to read local {path} before conflict "
                        f"resolution: {e}"
                    )

            # Download cloud version as canonical
            content = await self.drive_client.download(path)
            write_sync_bytes(local_path, content)

            # Preserve local divergent copy under conflict name
            # (only if we successfully read the original local version)
            if action.conflict_copy and local_content is not None:
                conflict_path = self.aither_root / action.conflict_copy
                write_sync_bytes(conflict_path, local_content)

        elif action.kind == ActionKind.NOOP:
            log.debug(f"NOOP {path}")
