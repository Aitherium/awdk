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

import hashlib
import json
import logging
import sys
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

                # Compute path relative to ~/.aither
                try:
                    rel_path = str(local_path.relative_to(self.aither_root))
                except ValueError:
                    continue

                # Normalize path separators to forward slash
                rel_path = rel_path.replace("\\", "/")

                # Compute hash and size
                try:
                    with open(local_path, "rb") as f:
                        content = f.read()
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
                except (IOError, OSError) as e:
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

        # Step 1: Scan local
        local = self.scan_local()

        # Step 2: Get remote changes (use full manifest from list_changes)
        try:
            _, remote = await self.drive_client.list_changes(since=0)
        except Exception as e:
            log.error(f"Failed to fetch remote changes: {e}")
            raise

        # Step 3: Load base
        base_dict = self.manifest_db.get_base()
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

        if action.kind == ActionKind.UPLOAD:
            # Upload local file to cloud
            log.info(f"UPLOAD {path}")
            with open(local_path, "rb") as f:
                content = f.read()
            await self.drive_client.upload(
                path, content, version=action.base_version
            )

        elif action.kind == ActionKind.DOWNLOAD:
            # Download cloud file to local
            log.info(f"DOWNLOAD {path}")
            content = await self.drive_client.download(path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(content)

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
                    local_content = local_path.read_bytes()
                except (IOError, OSError) as e:
                    log.warning(
                        f"Failed to read local {path} before conflict "
                        f"resolution: {e}"
                    )

            # Download cloud version as canonical
            content = await self.drive_client.download(path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(content)

            # Preserve local divergent copy under conflict name
            # (only if we successfully read the original local version)
            if action.conflict_copy and local_content is not None:
                conflict_path = self.aither_root / action.conflict_copy
                conflict_path.parent.mkdir(parents=True, exist_ok=True)
                conflict_path.write_bytes(local_content)

        elif action.kind == ActionKind.NOOP:
            log.debug(f"NOOP {path}")
