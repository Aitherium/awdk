"""AitherADK sync — bidirectional sync of adk's own ~/.aither data.

This module provides:
  - Device mTLS identity enrollment and persistence
  - DriveClient for cloud change-feed polling
  - SyncEngine that reuses awnode's reconcile algorithm
  - Per-agent fabric for syncing memory/graph/session data
  - Brain, Files, Lockbox, Secrets, Session, Settings, and Watermark sync clients
    for various data planes (embeddings, files, encrypted secrets, etc.)

Reuses AitherOS's pure reconcile algorithm (drive_sync_core) without
reimplementing the three-way merge logic.
"""

__all__ = [
    # Existing submodules
    "device_identity",
    "drive_client",
    "adk_sync_engine",
    # New sync domain modules
    "brain",
    "files",
    "lockbox",
    "secrets",
    "sessions",
    "settings",
    "watermark",
]
