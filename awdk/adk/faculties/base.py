#!/usr/bin/env python3
"""
BaseFacultyGraph - Abstract Base for All Faculty Graphs (ADK Standalone)
=========================================================================

Standalone adaptation of AitherOS BaseFacultyGraph for use in the ADK.
Provides the shared pattern across faculty graphs:

- Singleton lifecycle
- HMAC-validated pickle persistence
- Sync stub (no-op without AitherOS GraphSyncBus)

All pickle files are protected by HMAC-SHA256 sidecars. Set the
``AITHER_INTERNAL_SECRET`` environment variable to a strong secret
in production; a deterministic default is used when unset.

Data directory defaults to ``~/.aither/data`` (overridable via
``_get_data_dir(root_path=...)``).

Only stdlib imports are used: hashlib, hmac, os, pickle, time,
pathlib, dataclasses, typing, logging.
"""

import hashlib as _hashlib
import hmac as _hmac
import logging
import os
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("adk.faculties.base")


# ============================================================================
# SYNC CONFIGURATION
# ============================================================================

@dataclass
class GraphSyncConfig:
    """Configuration for syncing faculty graph data to a knowledge graph.

    In the standalone ADK, sync is always disabled. The dataclass is retained
    so that faculty graph subclasses ported from AitherOS compile without
    modification.
    """

    enabled: bool = False
    domain: str = ""           # e.g. "code", "memory", "service", "test"
    batch_size: int = 20       # Flush when this many nodes are queued
    flush_interval: float = 5.0  # Seconds between auto-flushes
    source_graph: str = ""     # e.g. "CodeGraph", "MemoryGraph"


# ============================================================================
# BASE CLASS
# ============================================================================

class BaseFacultyGraph:
    """Abstract base for all faculty graphs.

    Subclasses MAY set ``_sync_config`` but in the standalone ADK the sync
    path is a no-op (GraphSyncBus is not available outside AitherOS).

    All existing query/index APIs in subclasses remain unchanged.
    """

    _sync_config: GraphSyncConfig = GraphSyncConfig()
    _pending_sync: List[Dict[str, Any]] = []

    def __init__(self) -> None:
        # Each instance gets its own pending list (avoid class-level sharing)
        self._pending_sync = []

    # ------------------------------------------------------------------
    # SYNC STUBS (no-op in standalone ADK)
    # ------------------------------------------------------------------

    def _queue_sync(
        self, node_data: Dict[str, Any], tenant_id: str = "platform",
    ) -> None:
        """Queue a node for sync.

        In the standalone ADK this is a no-op. The method signature is kept
        so that subclasses ported from AitherOS work without changes.

        Args:
            node_data: Dict with at least ``id``, plus optional ``name``,
                       ``type``, and ``properties`` keys.
            tenant_id: Tenant scope (default ``"platform"``).
        """
        if not self._sync_config.enabled:
            return

        try:
            if not node_data.get("id"):
                return

            entry = {
                "id": str(node_data["id"]),
                "name": str(node_data.get("name", "")),
                "type": str(node_data.get("type", "unknown")),
                "properties": node_data.get("properties", {}),
                "_tenant_id": tenant_id,
            }
            self._pending_sync.append(entry)

            if len(self._pending_sync) >= self._sync_config.batch_size:
                self._flush_to_bus()
        except Exception as e:
            logger.debug("_queue_sync failed: %s", e)

    def _flush_to_bus(self) -> None:
        """Flush pending sync entries.

        No-op in the standalone ADK -- simply clears the queue.
        """
        self._pending_sync.clear()

    # ------------------------------------------------------------------
    # PICKLE PERSISTENCE HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _pickle_hmac_key() -> bytes:
        """Get the HMAC key for pickle integrity validation.

        Uses the ``AITHER_INTERNAL_SECRET`` environment variable. Falls back
        to a deterministic default when the variable is unset (logs a warning).

        Returns:
            UTF-8 encoded key bytes.
        """
        secret = os.environ.get("AITHER_INTERNAL_SECRET", "")
        if not secret:
            logger.warning(
                "AITHER_INTERNAL_SECRET not set -- using default pickle HMAC key. "
                "Set this env var in production!"
            )
            secret = "aither-pickle-hmac-default"
        return secret.encode()

    @staticmethod
    def _compute_file_hmac(path: str) -> str:
        """Compute HMAC-SHA256 of a file's contents.

        Args:
            path: Filesystem path to the file.

        Returns:
            Hex-encoded HMAC digest string.
        """
        key = BaseFacultyGraph._pickle_hmac_key()
        with open(path, "rb") as f:
            data = f.read()
        return _hmac.new(key, data, _hashlib.sha256).hexdigest()

    @staticmethod
    def _verify_pickle_hmac(path: str) -> bool:
        """Verify HMAC sidecar for a pickle file.

        Returns ``True`` only if the ``.hmac`` sidecar exists and its stored
        digest matches a freshly computed one. On mismatch the pickle *and*
        sidecar are deleted (fail-closed).

        Args:
            path: Filesystem path to the pickle file.

        Returns:
            ``True`` if verification passes, ``False`` otherwise.
        """
        hmac_path = path + ".hmac"
        if not os.path.exists(hmac_path):
            logger.warning(
                "No HMAC sidecar for %s -- refusing to load (rebuild required)", path
            )
            return False
        try:
            with open(hmac_path, "r") as f:
                stored = f.read().strip()
            computed = BaseFacultyGraph._compute_file_hmac(path)
            if not _hmac.compare_digest(stored, computed):
                logger.error(
                    "HMAC mismatch for %s -- cache tampered, deleting", path
                )
                os.unlink(path)
                os.unlink(hmac_path)
                return False
            return True
        except Exception as e:
            logger.error(
                "HMAC verification error for %s: %s -- refusing to load", path, e
            )
            return False

    @staticmethod
    def _write_pickle_hmac(path: str) -> None:
        """Write an HMAC sidecar (``<path>.hmac``) after saving a pickle file.

        Args:
            path: Filesystem path to the pickle file whose HMAC to write.
        """
        try:
            hmac_val = BaseFacultyGraph._compute_file_hmac(path)
            with open(path + ".hmac", "w") as f:
                f.write(hmac_val)
        except Exception as e:
            logger.warning("Failed to write HMAC sidecar: %s", e)

    @staticmethod
    def _load_pickle(path: str) -> Optional[Any]:
        """Load data from an HMAC-verified pickle file.

        Args:
            path: Filesystem path to the pickle file.

        Returns:
            Deserialized object, or ``None`` if the file is missing,
            HMAC verification fails, or deserialization raises.
        """
        if not os.path.exists(path):
            return None
        if not BaseFacultyGraph._verify_pickle_hmac(path):
            return None
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            logger.warning("Pickle load failed (%s): %s", path, e)
            return None

    @staticmethod
    def _save_pickle(path: str, data: Any) -> bool:
        """Save data to a pickle file with an HMAC sidecar.

        Creates parent directories as needed.

        Args:
            path: Filesystem path for the pickle file.
            data: Object to serialize.

        Returns:
            ``True`` on success, ``False`` on any error.
        """
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
            BaseFacultyGraph._write_pickle_hmac(path)
            return True
        except Exception as e:
            logger.warning("Pickle save failed (%s): %s", path, e)
            return False

    @staticmethod
    def _get_data_dir(root_path: Optional[str] = None) -> Path:
        """Get the data directory for faculty graph persistence.

        In the standalone ADK the default location is ``~/.aither/data``
        (as opposed to ``Library/Data`` relative to the AitherOS tree).

        Args:
            root_path: Optional override root. When provided the data
                       directory is ``<root_path>/.aither/data``.

        Returns:
            Resolved :class:`~pathlib.Path` (created if it did not exist).
        """
        if root_path:
            data_dir = Path(root_path) / ".aither" / "data"
        else:
            data_dir = Path(os.path.expanduser("~/.aither/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
