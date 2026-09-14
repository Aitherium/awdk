"""Lockbox Sync Client — Synchronize local secrets to Strata lockbox.

This module enables push/pull of encrypted lockbox entries (secrets, keys, etc.)
to the AitherOS Strata service. All values are opaque blobs — no decryption
happens client-side; the server handles per-tenant encryption using the
authenticated token.

Architecture:
  1. Local: ~/.aither/private/lockbox/ stores entries (encrypted at rest)
  2. Cloud: Strata /lockbox/tenant/{tenant_id}/* endpoints
  3. Client: LockboxSyncClient (this module) + integration hooks
  4. Auth: Bearer token with "endpoint:lockbox" scope (tenant-scoped)
  5. Push: POST /lockbox/tenant/{tenant_id}/write (base64-encoded values)
  6. Pull: GET /lockbox/tenant/{tenant_id}/read/{path} (opaque blobs)

Supported modes:
  - Direct push: POST after a lockbox entry is updated
  - Batch sync: Periodic sync of all local entries
  - Pull: Fetch remote entries and merge (last-writer-wins)

Environment:
  - AITHER_LOCKBOX_SYNC=true/false — enable/disable sync (default: false)
  - AITHER_GATEWAY_URL=http://localhost:8001 — sync endpoint (default: local)
  - AITHER_SYNC_TOKEN=Bearer ... — auth token override
  - AITHER_SYNC_TIMEOUT=30.0 — HTTP timeout in seconds

Best-effort: all network/auth errors are logged but do NOT raise;
local state is always authoritative.

SECURITY:
  - Tenant/user derived from Bearer token, never from caller headers
  - Tenant-scoped tokens verify identity (validate_endpoint_token)
  - Values sent as-is (opaque blobs); no cleartext decryption
  - Per-tenant quota tracking and audit logging (server-side)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

import httpx

log = logging.getLogger("adk.lockbox_sync")


@dataclass
class LockboxSyncConfig:
    """Configuration for lockbox syncing."""
    enabled: bool = False  # Master switch: AITHER_LOCKBOX_SYNC
    gateway_url: str = "http://localhost:8001"  # AITHER_GATEWAY_URL
    timeout_seconds: float = 30.0  # HTTP timeout
    fail_soft: bool = True  # Don't raise on sync failures; only log


class LockboxSyncClient:
    """Client for pushing/pulling lockbox entries to/from Strata.

    Typical usage:
        client = LockboxSyncClient(
            gateway_url="https://portal.aitherium.com",
            auth_token="Bearer ...",  # from ~/.aither/auth.json
        )
        # Push after a lockbox entry is updated
        result = await client.write_entry(path, content, metadata)
        # Pull remote entries
        entries = await client.pull_entries(watermark=0.0)
    """

    def __init__(
        self,
        gateway_url: str = "http://localhost:8001",
        auth_token: Optional[str] = None,
        config: Optional[LockboxSyncConfig] = None,
    ):
        """
        Initialize the lockbox sync client.

        Args:
            gateway_url: Base URL of the portal/gateway
            auth_token: Bearer token with "endpoint:lockbox" scope
            config: Optional LockboxSyncConfig; if None, loads from environment
        """
        self.gateway_url = gateway_url.rstrip("/")
        self.auth_token = auth_token or os.environ.get("AITHER_SYNC_TOKEN", "")
        self.config = config or self._load_config()

    @staticmethod
    def _load_config() -> LockboxSyncConfig:
        """Load config from environment variables."""
        enabled = os.environ.get("AITHER_LOCKBOX_SYNC", "false").lower() in (
            "true",
            "1",
        )
        gateway_url = os.environ.get(
            "AITHER_GATEWAY_URL",
            "http://localhost:8001"
        )
        timeout = float(os.environ.get("AITHER_SYNC_TIMEOUT", "30.0"))

        return LockboxSyncConfig(
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

    def _extract_tenant_from_token(self) -> Optional[str]:
        """
        Extract tenant_id from the Bearer token (if possible).

        This is best-effort — the tenant is primarily derived server-side
        from the validated token. This is only for logging/debugging.

        Returns:
            Tenant ID string, or None if token format is unknown.
        """
        # Token format is opaque; we don't decode it client-side.
        # The server validates and extracts the tenant from the token.
        return None

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Make an authenticated request to the Strata gateway.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: Endpoint path (e.g., "/lockbox/tenant/{tenant_id}/write")
            data: Request body as dict (will be JSON-encoded)

        Returns:
            Response JSON or error dict with "error" key set.
        """
        url = f"{self.gateway_url}{path}"
        headers = self._build_headers()
        body = json.dumps(data).encode("utf-8") if data else None

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

    async def write_entry(
        self,
        path: str,
        content: bytes | str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Write a single lockbox entry.

        The content is sent as a base64-encoded string to the server.
        The server handles per-tenant encryption and validation.

        Args:
            path: Relative path within the lockbox (e.g., "secrets/api_key")
            content: Raw bytes or string (will be base64-encoded)
            metadata: Optional metadata dict (e.g., {"key_version": 1})

        Returns:
            Response dict from /lockbox/tenant/{tenant_id}/write
            On error, returns {"error": True, ...}
        """
        if not self.config.enabled:
            log.debug("Lockbox sync disabled — skipping write for %s", path)
            return {"skipped": True, "reason": "disabled"}

        try:
            # Convert content to base64 (server expects base64-encoded strings)
            if isinstance(content, str):
                content_bytes = content.encode("utf-8")
            else:
                content_bytes = content

            content_b64 = base64.b64encode(content_bytes).decode("ascii")

            # The tenant_id is derived server-side from the validated Bearer token.
            # We use a placeholder path; the server validates tenant-scoping.
            request_body = {
                "path": path,
                "content": content_b64,
                "metadata": metadata or {},
            }

            # Route: POST /lockbox/tenant/{tenant_id}/write
            # The server validates the Bearer token and extracts tenant_id
            result = await self._request(
                "POST",
                "/lockbox/tenant/self/write",  # 'self' placeholder; server extracts tenant
                request_body
            )

            if result.get("error"):
                log.warning(
                    "Failed to write lockbox entry %s: %s",
                    path, result.get("detail", "unknown error")
                )
                if not self.config.fail_soft:
                    raise RuntimeError(f"Lockbox write failed: {result.get('detail')}")
            else:
                log.debug("Wrote lockbox entry %s", path)

            return result

        except Exception as e:
            log.warning("Lockbox write raised exception: %s", e)
            if not self.config.fail_soft:
                raise
            return {"error": True, "detail": str(e)}

    async def read_entry(self, path: str) -> Optional[bytes]:
        """
        Read a single lockbox entry (raw bytes).

        Args:
            path: Relative path within the lockbox

        Returns:
            Raw bytes if found, None on error (fail-soft).
        """
        if not self.config.enabled:
            log.debug("Lockbox sync disabled — skipping read for %s", path)
            return None

        try:
            # Route: GET /lockbox/tenant/{tenant_id}/read/{path}
            # Server extracts tenant from Bearer token
            result = await self._request(
                "GET",
                f"/lockbox/tenant/self/read/{path}",
            )

            if result.get("error"):
                status = result.get("status", 500)
                # 404 = entry simply not synced yet (benign). 403 = auth/scope
                # DENIED — a real misconfiguration that must NOT hide at DEBUG,
                # or the whole sync silently does nothing and looks healthy.
                if status == 404:
                    log.debug("Lockbox read %s not found (404)", path)
                else:
                    log.warning(
                        "Lockbox read %s failed (HTTP %d): %s",
                        path, status, result.get("detail", "unknown")
                    )
                return None

            # Server returns base64-encoded content
            content_b64 = result.get("content", "")
            if not content_b64:
                log.debug("Lockbox entry %s is empty", path)
                return b""

            try:
                content_bytes = base64.b64decode(content_b64)
                log.debug("Read lockbox entry %s (%d bytes)", path, len(content_bytes))
                return content_bytes
            except Exception as e:
                log.warning("Failed to decode base64 content for %s: %s", path, e)
                return None

        except Exception as e:
            log.warning("Lockbox read failed: %s", e)
            return None

    async def list_entries(self, prefix: str = "") -> List[str]:
        """
        List lockbox entries under an optional prefix.

        Args:
            prefix: Optional prefix to filter by (e.g., "secrets/")

        Returns:
            List of entry paths, empty list on error (fail-soft).
        """
        if not self.config.enabled:
            log.debug("Lockbox sync disabled — skipping list")
            return []

        try:
            # Route: GET /lockbox/tenant/{tenant_id}/list?path={prefix}
            path_suffix = f"?path={prefix}" if prefix else ""
            result = await self._request(
                "GET",
                f"/lockbox/tenant/self/list{path_suffix}",
            )

            if result.get("error"):
                log.warning("Failed to list lockbox entries: %s", result.get("detail"))
                return []

            entries = result.get("entries", [])
            log.debug("Listed %d lockbox entries", len(entries))
            return entries

        except Exception as e:
            log.warning("Lockbox list failed: %s", e)
            return []

    async def delete_entry(self, path: str) -> bool:
        """
        Delete a lockbox entry.

        Args:
            path: Relative path within the lockbox

        Returns:
            True if deletion succeeded, False on error (fail-soft).
        """
        if not self.config.enabled:
            log.debug("Lockbox sync disabled — skipping delete for %s", path)
            return False

        try:
            # Route: DELETE /lockbox/tenant/{tenant_id}/delete
            request_body = {"path": path}
            result = await self._request(
                "DELETE",
                "/lockbox/tenant/self/delete",
                request_body
            )

            if result.get("error"):
                log.warning("Failed to delete lockbox entry %s: %s", path, result.get("detail"))
                return False

            log.debug("Deleted lockbox entry %s", path)
            return True

        except Exception as e:
            log.warning("Lockbox delete failed: %s", e)
            return False

    async def sync_from_disk(
        self,
        lockbox_dir: Path | str,
    ) -> Dict[str, Any]:
        """
        Batch-sync all local lockbox entries from disk.

        Reads all files from the local lockbox directory and pushes them
        to the remote lockbox. Skips if sync is disabled.

        Args:
            lockbox_dir: Path to ~/.aither/private/lockbox/ (or custom dir)

        Returns:
            Summary: {"synced": N, "failed": M, "skipped": K, ...}
        """
        lockbox_dir = Path(lockbox_dir)
        if not lockbox_dir.exists():
            log.warning("Lockbox directory not found: %s", lockbox_dir)
            return {"error": True, "detail": f"Directory not found: {lockbox_dir}"}

        if not self.config.enabled:
            log.debug("Lockbox sync disabled — skipping batch push")
            return {"skipped": True, "reason": "disabled"}

        synced = 0
        failed = 0
        skipped = 0

        for entry_file in lockbox_dir.rglob("*"):
            if not entry_file.is_file():
                continue

            # Relative path from lockbox_dir to file (use forward slashes)
            rel_path = entry_file.relative_to(lockbox_dir)
            rel_path_str = str(rel_path).replace("\\", "/")

            try:
                content = entry_file.read_bytes()
                result = await self.write_entry(rel_path_str, content)

                if result.get("error"):
                    failed += 1
                    log.warning(
                        "Failed to sync lockbox entry %s: %s",
                        rel_path_str, result.get("detail")
                    )
                else:
                    synced += 1

            except Exception as e:
                failed += 1
                log.warning("Failed to read/sync lockbox entry %s: %s", rel_path_str, e)

        return {
            "synced": synced,
            "failed": failed,
            "skipped": skipped,
        }

    async def pull_entries(
        self,
        watermark: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        Pull remote lockbox entries (stub for future expansion).

        Currently returns an empty list. Future versions can implement
        watermark-based pulling and merging.

        Args:
            watermark: Unix timestamp (for future use)

        Returns:
            List of entry dicts (currently empty, fail-soft).
        """
        if not self.config.enabled:
            log.debug("Lockbox sync disabled — skipping pull")
            return []

        # Strata does not currently support watermark-based pulls for lockbox.
        # This is a placeholder for future implementation.
        log.debug("Lockbox pull not yet implemented (watermark=%.1f)", watermark)
        return []


async def create_lockbox_sync_client_from_auth(
    auth_file: Optional[Path] = None,
    gateway_url: Optional[str] = None,
) -> Optional[LockboxSyncClient]:
    """
    Create a LockboxSyncClient from stored auth.json credentials.

    Args:
        auth_file: Path to ~/.aither/auth.json (default: ~/.aither/auth.json)
        gateway_url: Override gateway URL (default: from auth.json or env)

    Returns:
        LockboxSyncClient or None if auth not found / sync disabled
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

    return LockboxSyncClient(gateway_url=url, auth_token=access_token)
