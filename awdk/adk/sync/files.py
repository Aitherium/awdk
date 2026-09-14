"""Files Sync Client — Synchronize local user files to Strata warm tier.

This module enables push/pull of user files to the AitherOS Strata service,
stored in the warm tier under tenant-scoped paths. Files are skipped if
unchanged (checked via size and mtime).

Architecture:
  1. Local: ~/.aither/files/ stores user files (with mtime tracking)
  2. Cloud: Strata /strata/write endpoint (tenant-scoped warm tier)
  3. Client: FilesSyncClient (this module)
  4. Auth: Bearer token with "endpoint:strata" scope (tenant-scoped)
  5. Push: POST /strata/write with content to aither://warm/__t__/{tenant}/...
  6. Pull: GET /strata/read?path=aither://warm/__t__/{tenant}/...
  7. Skip: Only push if size or mtime differs from last sync

Supported modes:
  - Direct push: POST after a file is updated
  - Batch sync: Periodic sync of all local files (skip if unchanged)
  - Pull: Fetch remote files and merge (last-writer-wins)
  - Watermark: Resume pulls from the last sync point

Environment:
  - AITHER_FILES_SYNC=true/false — enable/disable sync (default: false)
  - AITHER_GATEWAY_URL=http://localhost:8001 — sync endpoint
  - AITHER_SYNC_TOKEN=Bearer ... — auth token override
  - AITHER_SYNC_TIMEOUT=30.0 — HTTP timeout in seconds

Best-effort: all network/auth errors are logged but do NOT raise;
local state is always authoritative.

SECURITY:
  - Tenant/user derived from Bearer token, never from caller headers
  - Paths automatically scoped to tenant (aither://warm/__t__/{tenant}/...)
  - Per-tenant quota tracking and audit logging (server-side)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

import httpx

log = logging.getLogger("adk.files_sync")


@dataclass
class FilesSyncConfig:
    """Configuration for files syncing."""
    enabled: bool = False  # Master switch: AITHER_FILES_SYNC
    gateway_url: str = "http://localhost:8001"  # AITHER_GATEWAY_URL
    timeout_seconds: float = 30.0  # HTTP timeout
    fail_soft: bool = True  # Don't raise on sync failures; only log


class FileEntry:
    """Metadata for a tracked file (size, mtime for skip-if-unchanged)."""

    def __init__(self, file_path: Path):
        self.path = file_path
        self.size = 0
        self.mtime = 0.0

        if file_path.exists():
            stat = file_path.stat()
            self.size = stat.st_size
            self.mtime = stat.st_mtime

    def __hash__(self):
        return hash((self.size, self.mtime))

    def __eq__(self, other):
        if not isinstance(other, FileEntry):
            return False
        return self.size == other.size and self.mtime == other.mtime

    def has_changed(self, other: FileEntry) -> bool:
        """Check if this entry has changed compared to another."""
        return self.size != other.size or self.mtime != other.mtime


class FilesSyncClient:
    """Client for pushing/pulling user files to/from Strata warm tier.

    Typical usage:
        client = FilesSyncClient(
            gateway_url="https://portal.aitherium.com",
            auth_token="Bearer ...",  # from ~/.aither/auth.json
        )
        # Batch-push all local files (skips unchanged)
        result = await client.sync_from_disk(files_dir)
        # Or push a single file
        result = await client.write_file(path, content)
    """

    def __init__(
        self,
        gateway_url: str = "http://localhost:8001",
        auth_token: Optional[str] = None,
        config: Optional[FilesSyncConfig] = None,
    ):
        """
        Initialize the files sync client.

        Args:
            gateway_url: Base URL of the portal/gateway
            auth_token: Bearer token with "endpoint:strata" scope
            config: Optional FilesSyncConfig; if None, loads from environment
        """
        self.gateway_url = gateway_url.rstrip("/")
        self.auth_token = auth_token or os.environ.get("AITHER_SYNC_TOKEN", "")
        self.config = config or self._load_config()
        self._file_manifest: Dict[str, FileEntry] = {}  # Tracks synced files

    @staticmethod
    def _load_config() -> FilesSyncConfig:
        """Load config from environment variables."""
        enabled = os.environ.get("AITHER_FILES_SYNC", "false").lower() in (
            "true",
            "1",
        )
        gateway_url = os.environ.get(
            "AITHER_GATEWAY_URL",
            "http://localhost:8001"
        )
        timeout = float(os.environ.get("AITHER_SYNC_TIMEOUT", "30.0"))

        return FilesSyncConfig(
            enabled=enabled,
            gateway_url=gateway_url,
            timeout_seconds=timeout,
        )

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers with authentication."""
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            if not self.auth_token.startswith("Bearer "):
                headers["Authorization"] = f"Bearer {self.auth_token}"
            else:
                headers["Authorization"] = self.auth_token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        binary_data: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        """
        Make an authenticated request to the Strata gateway.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: Endpoint path (e.g., "/strata/write")
            data: Request body as dict (will be JSON-encoded)
            binary_data: Raw bytes (for file uploads)

        Returns:
            Response JSON or error dict with "error" key set.
        """
        url = f"{self.gateway_url}{path}"
        headers = self._build_headers()

        body = None
        if data:
            body = json.dumps(data).encode("utf-8")
        elif binary_data:
            body = binary_data

        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds,
                verify=True,
            ) as client:
                if method == "GET":
                    resp = await client.get(url, headers=headers)
                elif method == "POST":
                    resp = await client.post(url, content=body, headers=headers)
                elif method == "DELETE":
                    resp = await client.delete(url, headers=headers)
                else:
                    resp = await client.request(method, url, content=body, headers=headers)

                if resp.status_code >= 400:
                    error_msg = resp.text[:500]
                    log.warning(
                        "Gateway %s %s returned %d: %s",
                        method, path, resp.status_code, error_msg
                    )
                    return {
                        "error": True,
                        "status": resp.status_code,
                        "detail": error_msg,
                    }

                try:
                    return resp.json()
                except Exception:
                    return {"ok": True, "raw": resp.text[:200]}

        except Exception as e:
            error_msg = str(e)
            log.warning("Request to %s %s failed: %s", method, path, error_msg)
            return {"error": True, "detail": error_msg}

    async def write_file(
        self,
        path: str,
        content: bytes | str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Write a single file to the warm tier.

        The path is automatically scoped to the tenant (aither://warm/__t__/{tenant}/...).
        The server validates the Bearer token and enforces tenant isolation.

        Args:
            path: Relative file path within the user's namespace (e.g., "documents/file.txt")
            content: Raw bytes or string
            metadata: Optional metadata dict (e.g., {"content_type": "application/json"})

        Returns:
            Response dict from /strata/write
            On error, returns {"error": True, ...}
        """
        if not self.config.enabled:
            log.debug("Files sync disabled — skipping write for %s", path)
            return {"skipped": True, "reason": "disabled"}

        try:
            # Convert string to bytes if needed
            if isinstance(content, str):
                content_bytes = content.encode("utf-8")
            else:
                content_bytes = content

            # Build the virtual path (server will scope to tenant)
            virtual_path = f"aither://warm/self/{path}"

            request_body = {
                "path": virtual_path,
                "content": content_bytes.hex(),  # Hex-encode binary data
                "metadata": metadata or {},
            }

            result = await self._request(
                "POST",
                "/strata/write",
                request_body
            )

            if result.get("error"):
                log.warning(
                    "Failed to write file %s: %s",
                    path, result.get("detail", "unknown error")
                )
                if not self.config.fail_soft:
                    raise RuntimeError(f"File write failed: {result.get('detail')}")
            else:
                log.debug("Wrote file %s (%d bytes)", path, len(content_bytes))

            return result

        except Exception as e:
            log.warning("File write raised exception: %s", e)
            if not self.config.fail_soft:
                raise
            return {"error": True, "detail": str(e)}

    async def read_file(self, path: str) -> Optional[bytes]:
        """
        Read a single file from the warm tier.

        Args:
            path: Relative file path within the user's namespace

        Returns:
            Raw bytes if found, None on error (fail-soft).
        """
        if not self.config.enabled:
            log.debug("Files sync disabled — skipping read for %s", path)
            return None

        try:
            virtual_path = f"aither://warm/self/{path}"
            result = await self._request(
                "GET",
                f"/strata/read?path={virtual_path}",
            )

            if result.get("error"):
                status = result.get("status", 500)
                # 404 = file not synced yet (benign). 403 = auth/scope DENIED —
                # surface loudly, never hide an auth failure at DEBUG.
                if status == 404:
                    log.debug("File read %s not found (404)", path)
                else:
                    log.warning(
                        "File read %s failed (HTTP %d): %s",
                        path, status, result.get("detail", "unknown")
                    )
                return None

            # Server returns hex-encoded content
            content_hex = result.get("content", "")
            if not content_hex:
                log.debug("File %s is empty", path)
                return b""

            try:
                content_bytes = bytes.fromhex(content_hex)
                log.debug("Read file %s (%d bytes)", path, len(content_bytes))
                return content_bytes
            except Exception as e:
                log.warning("Failed to decode hex content for %s: %s", path, e)
                return None

        except Exception as e:
            log.warning("File read failed: %s", e)
            return None

    async def list_files(self, prefix: str = "") -> List[str]:
        """
        List files under an optional prefix.

        Args:
            prefix: Optional prefix to filter by (e.g., "documents/")

        Returns:
            List of file paths, empty list on error (fail-soft).
        """
        if not self.config.enabled:
            log.debug("Files sync disabled — skipping list")
            return []

        try:
            virtual_path = f"aither://warm/self/{prefix}"
            result = await self._request(
                "GET",
                f"/strata/read?path={virtual_path}",
            )

            if result.get("error"):
                log.warning("Failed to list files: %s", result.get("detail"))
                return []

            # Server returns a list of matching paths
            files = result.get("files", result.get("keys", []))
            log.debug("Listed %d files", len(files))
            return files

        except Exception as e:
            log.warning("File list failed: %s", e)
            return []

    async def delete_file(self, path: str) -> bool:
        """
        Delete a file from the warm tier.

        Args:
            path: Relative file path within the user's namespace

        Returns:
            True if deletion succeeded, False on error (fail-soft).
        """
        if not self.config.enabled:
            log.debug("Files sync disabled — skipping delete for %s", path)
            return False

        try:
            virtual_path = f"aither://warm/self/{path}"
            result = await self._request(
                "DELETE",
                f"/strata/read?path={virtual_path}",
            )

            if result.get("error"):
                log.warning("Failed to delete file %s: %s", path, result.get("detail"))
                return False

            log.debug("Deleted file %s", path)
            return True

        except Exception as e:
            log.warning("File delete failed: %s", e)
            return False

    async def sync_from_disk(
        self,
        files_dir: Path | str,
    ) -> Dict[str, Any]:
        """
        Batch-sync all local files from disk (skip if unchanged).

        Reads all files from the local directory and pushes them to the remote
        warm tier, but skips files whose size and mtime haven't changed since
        the last sync.

        Args:
            files_dir: Path to ~/.aither/files/ (or custom dir)

        Returns:
            Summary: {"synced": N, "skipped": M, "failed": K, ...}
        """
        files_dir = Path(files_dir)
        if not files_dir.exists():
            log.warning("Files directory not found: %s", files_dir)
            return {"error": True, "detail": f"Directory not found: {files_dir}"}

        if not self.config.enabled:
            log.debug("Files sync disabled — skipping batch push")
            return {"skipped": True, "reason": "disabled"}

        synced = 0
        skipped = 0
        failed = 0

        for file_path in files_dir.rglob("*"):
            if not file_path.is_file():
                continue

            # Relative path from files_dir to file (use forward slashes)
            rel_path = file_path.relative_to(files_dir)
            rel_path_str = str(rel_path).replace("\\", "/")

            try:
                # Check if file has changed
                current_entry = FileEntry(file_path)
                last_entry = self._file_manifest.get(rel_path_str)

                if last_entry and not last_entry.has_changed(current_entry):
                    log.debug("Skipping unchanged file %s", rel_path_str)
                    skipped += 1
                    continue

                # File has changed — push it
                content = file_path.read_bytes()
                result = await self.write_file(rel_path_str, content)

                if result.get("error"):
                    failed += 1
                    log.warning(
                        "Failed to sync file %s: %s",
                        rel_path_str, result.get("detail")
                    )
                else:
                    synced += 1
                    # Update manifest
                    self._file_manifest[rel_path_str] = current_entry

            except Exception as e:
                failed += 1
                log.warning("Failed to read/sync file %s: %s", rel_path_str, e)

        log.info(
            "Files sync complete: synced=%d, skipped=%d, failed=%d",
            synced, skipped, failed
        )

        return {
            "synced": synced,
            "skipped": skipped,
            "failed": failed,
        }

    async def pull_files(
        self,
        watermark: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        Pull remote files newer than watermark (stub for future expansion).

        Currently returns an empty list. Future versions can implement
        watermark-based pulling and merging.

        Args:
            watermark: Unix timestamp (for future use)

        Returns:
            List of file dicts (currently empty, fail-soft).
        """
        if not self.config.enabled:
            log.debug("Files sync disabled — skipping pull")
            return []

        # Strata does not currently support watermark-based pulls for files.
        # This is a placeholder for future implementation.
        log.debug("Files pull not yet implemented (watermark=%.1f)", watermark)
        return []


async def create_files_sync_client_from_auth(
    auth_file: Optional[Path] = None,
    gateway_url: Optional[str] = None,
) -> Optional[FilesSyncClient]:
    """
    Create a FilesSyncClient from stored auth.json credentials.

    Args:
        auth_file: Path to ~/.aither/auth.json (default: ~/.aither/auth.json)
        gateway_url: Override gateway URL (default: from auth.json or env)

    Returns:
        FilesSyncClient or None if auth not found / sync disabled
    """
    if auth_file is None:
        auth_file = Path.home() / ".aither" / "auth.json"

    if not auth_file.exists():
        log.debug("No auth file at %s", auth_file)
        return None

    try:
        auth_data = json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Failed to read auth file: %s", e)
        return None

    # Extract access token from the active profile
    access_token = auth_data.get("profiles", {}).get("default", {}).get("access_token")
    if not access_token:
        # Fallback: check for flat 'access_token' key (legacy format)
        access_token = auth_data.get("access_token")

    if not access_token:
        log.debug("No access_token in auth.json")
        return None

    url = gateway_url or os.environ.get("AITHER_GATEWAY_URL", "http://localhost:8001")

    return FilesSyncClient(gateway_url=url, auth_token=access_token)
