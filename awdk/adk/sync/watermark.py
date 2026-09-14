"""Atomic persistent watermark store for resumable syncs.

This module provides a simple, atomic way to store high-water marks
(timestamps, sequence numbers, etc.) so that syncs can resume from
the last point without re-processing.

Architecture:
  - Watermarks stored in ~/.aither/sync_watermarks.json (one JSON dict)
  - All writes are atomic (temp file + rename)
  - Reads are fast (in-memory after first load)
  - No external dependencies (pure JSON)

Usage:
    watermark_store = SyncWatermarkStore()

    # Load the last sync point for lockbox
    last_sync = await watermark_store.get("lockbox")

    # Update after a successful sync
    await watermark_store.set("lockbox", time.time())

    # Query multiple keys at once
    timestamps = await watermark_store.get_all(["lockbox", "files"])
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("adk.sync_watermark")


class SyncWatermarkStore:
    """Atomic persistent watermark store for resumable syncs."""

    def __init__(self, watermark_file: Optional[Path | str] = None):
        """
        Initialize the watermark store.

        Args:
            watermark_file: Path to the watermark JSON file
                (default: ~/.aither/sync_watermarks.json)
        """
        if watermark_file is None:
            watermark_file = (
                Path.home() / ".aither" / "sync_watermarks.json"
            )
        else:
            watermark_file = Path(watermark_file)

        self._file = watermark_file
        self._data: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        """Load watermarks from disk if not already loaded."""
        if self._loaded:
            return

        if not self._file.exists():
            log.debug("Watermark file not found at %s (will create on first write)",
                      self._file)
            self._data = {}
            self._loaded = True
            return

        try:
            text = await asyncio.to_thread(self._file.read_text, encoding="utf-8")
            self._data = json.loads(text) or {}
            self._loaded = True
            log.debug("Loaded watermarks from %s", self._file)
        except Exception as e:
            log.warning("Failed to load watermarks from %s: %s", self._file, e)
            self._data = {}
            self._loaded = True

    async def _atomic_write(self, data: Dict[str, Any]) -> bool:
        """
        Write data atomically (temp file + rename).

        Returns True on success, False on failure (best-effort).
        """
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)

            # Write to a temporary file first
            temp_file = self._file.with_suffix(".json.tmp")

            def _write_sync():
                temp_file.write_text(
                    json.dumps(data, indent=2, default=str),
                    encoding="utf-8"
                )
                # Atomic rename
                temp_file.replace(self._file)

            await asyncio.to_thread(_write_sync)
            log.debug("Wrote watermarks to %s", self._file)
            return True

        except Exception as e:
            log.warning("Failed to write watermarks: %s", e)
            return False

    async def get(
        self,
        key: str,
        default: Any = None,
    ) -> Any:
        """
        Get a watermark value by key.

        Args:
            key: Watermark key (e.g., "lockbox", "files")
            default: Value to return if key not found

        Returns:
            The stored value, or default if not found.
        """
        async with self._lock:
            await self._ensure_loaded()
            return self._data.get(key, default)

    async def set(
        self,
        key: str,
        value: Any,
    ) -> bool:
        """
        Set a watermark value by key (atomic write).

        Args:
            key: Watermark key
            value: Value to store (should be JSON-serializable)

        Returns:
            True if write succeeded, False on error (best-effort, never raises).
        """
        async with self._lock:
            await self._ensure_loaded()
            self._data[key] = value

            # Write atomically to disk
            success = await self._atomic_write(self._data)
            if success:
                log.debug("Watermark %s set to %s", key, value)
            return success

    async def get_all(self, keys: list[str]) -> Dict[str, Any]:
        """
        Get multiple watermark values in one call.

        Args:
            keys: List of watermark keys to fetch

        Returns:
            Dict mapping key -> value (keys not found are omitted).
        """
        async with self._lock:
            await self._ensure_loaded()
            return {k: self._data[k] for k in keys if k in self._data}

    async def delete(self, key: str) -> bool:
        """
        Delete a watermark entry (atomic write).

        Args:
            key: Watermark key to delete

        Returns:
            True if deletion succeeded, False on error.
        """
        async with self._lock:
            await self._ensure_loaded()
            if key not in self._data:
                log.debug("Watermark %s not found (nothing to delete)", key)
                return True

            del self._data[key]
            success = await self._atomic_write(self._data)
            if success:
                log.debug("Deleted watermark %s", key)
            return success

    async def clear(self) -> bool:
        """
        Clear all watermarks (atomic write).

        Returns:
            True if clear succeeded, False on error.
        """
        async with self._lock:
            self._data = {}
            success = await self._atomic_write(self._data)
            if success:
                log.debug("Cleared all watermarks")
            return success


# Module-level singleton
_watermark_store: SyncWatermarkStore | None = None


async def get_sync_watermark_store(
    watermark_file: Optional[Path | str] = None,
) -> SyncWatermarkStore:
    """
    Get or create the module-level SyncWatermarkStore singleton.

    Args:
        watermark_file: Override the watermark file path (used only on first call)

    Returns:
        The global SyncWatermarkStore instance.
    """
    global _watermark_store
    if _watermark_store is None:
        _watermark_store = SyncWatermarkStore(watermark_file=watermark_file)
    return _watermark_store
