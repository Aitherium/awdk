"""DriveClient — thin httpx wrapper over cloud /drive endpoints.

Handles all HTTP communication with the cloud drive router. Sends tenant/
workspace headers and (optionally) bearer token for authentication.
Implements list_changes(since) — the primary interface for syncing.

Reuses AitherOS's drive_sync_core.FileState to represent remote manifest
entries.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, Union

if TYPE_CHECKING:
    import httpx

log = logging.getLogger("adk.sync.drive_client")

# Lazy import from AitherOS (inject parent path if needed)
_FILESTATE = None


def _get_filestate():
    """Lazy import FileState from AitherOS."""
    global _FILESTATE
    if _FILESTATE is None:
        try:
            from lib.sync.drive_sync_core import FileState as FS
            _FILESTATE = FS
        except ImportError:
            # Add parent AitherOS to path
            adk_dir = Path(__file__).parent.parent.parent  # aiter-adk/
            aitheros_dir = adk_dir.parent / "AitherOS"
            if aitheros_dir.is_dir():
                sys.path.insert(0, str(aitheros_dir))
            try:
                from lib.sync.drive_sync_core import FileState as _filestate_cls
            except ImportError as exc:
                raise RuntimeError(
                    "adk sync requires the AitherOS host environment "
                    "(drive_sync_core is not shipped in the PyPI package): "
                    f"{exc}") from exc
            _FILESTATE = _filestate_cls
    return _FILESTATE


# Trust the internal CA for HTTPS via adk's own TLS resolver — the public
# package must not reach into the AitherOS monorepo (lib.security.*).
try:
    from adk._tls import tls_verify
    _TLS_VERIFY = tls_verify()
except Exception:  # noqa: BLE001
    _TLS_VERIFY = True


class DriveClient:
    """Thin HTTP wrapper over cloud /drive endpoints."""

    def __init__(
        self,
        base_url: str,
        tenant_id: str,
        workspace_id: str,
        token: Optional[str] = None,
        client_cert: Optional[Union[str, Tuple[str, str]]] = None,
    ):
        """Initialize DriveClient for a specific tenant/workspace.

        Args:
            base_url: Root URL of communication-core
                (e.g., "https://aitheros-communication-core:8205")
            tenant_id: Tenant ID (sent in X-Tenant-ID header as fallback;
                cryptographically verified by mTLS cert CN if present).
            workspace_id: Workspace ID (sent in X-Workspace-ID header)
            token: Optional Bearer token for authentication
            client_cert: The enrolled device mTLS client cert, as httpx accepts it:
                a path to a combined cert+key PEM, or a ``(cert_path, key_path)``
                tuple. When present, the device authenticates CRYPTOGRAPHICALLY.
        """
        import httpx

        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.token = token
        self.client_cert = client_cert

        # Trust the internal CA (never disable verification). Present the device
        # client cert for mTLS when one was provisioned at enrollment.
        _kwargs: dict = {"verify": _TLS_VERIFY, "timeout": 30.0}
        if client_cert is not None:
            _kwargs["cert"] = client_cert
        self.client = httpx.AsyncClient(**_kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> Any:  # httpx.Response at runtime
        """Make an HTTP request with tenant/workspace headers.

        Args:
            method: HTTP method (GET, POST, etc)
            path: API path (e.g., "/drive/changes")
            **kwargs: Additional arguments for httpx.request()

        Returns:
            httpx.Response object

        Raises:
            httpx.RequestError: On network or timeout errors
        """
        import httpx

        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {}) or {}

        # Add tenant/workspace headers
        headers["X-Tenant-ID"] = self.tenant_id
        headers["X-Workspace-ID"] = self.workspace_id

        # Add bearer token if provided
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            return await self.client.request(method, url, headers=headers, **kwargs)
        except Exception as e:
            log.error(f"DriveClient request failed: {method} {path}: {e}")
            raise

    async def list_changes(
        self,
        since: int = 0,
    ) -> Tuple[int, Dict[str, Any]]:  # dict[rel_path -> FileState]
        """Poll cloud for changes since a version cursor.

        Args:
            since: Version cursor (files with version > since are returned)

        Returns:
            (new_cursor, changes_dict) where changes_dict maps rel_path → FileState
            Changes include new files, modified files, and tombstones (deleted=True).

        Raises:
            Exception: On network or API errors
        """
        FileState = _get_filestate()

        resp = await self._request("GET", "/drive/changes", params={"since": since})

        if resp.status_code != 200:
            log.warning(f"list_changes failed: {resp.status_code} {resp.text}")
            raise ValueError(f"list_changes returned {resp.status_code}")

        data = resp.json()
        new_cursor = data.get("cursor", since)
        changes_list = data.get("changes", [])

        # Convert list of changes to dict[path -> FileState]
        changes = {}
        for entry in changes_list:
            path = entry.get("path", "").lstrip("/")
            if not path:
                continue
            changes[path] = FileState(
                hash=entry.get("hash", ""),
                size=entry.get("size", 0),
                mtime=0.0,  # Not used by reconcile
                version=entry.get("version", 0),
                deleted=entry.get("deleted", False),
            )

        log.debug(f"list_changes({since}) → cursor={new_cursor}, {len(changes)} changes")
        return new_cursor, changes

    async def download(self, path: str) -> bytes:
        """Download a file from the cloud.

        Args:
            path: Relative path to file

        Returns:
            File content as bytes

        Raises:
            Exception: On network or API errors
        """
        resp = await self._request("GET", f"/drive/files/{path}")
        if resp.status_code != 200:
            raise ValueError(f"download {path} returned {resp.status_code}")
        return resp.content

    async def upload(
        self, path: str, content: bytes, version: int = 0
    ) -> Dict:
        """Upload a file to the cloud.

        Args:
            path: Relative path to file
            content: File content
            version: Base version (for conflict detection)

        Returns:
            Response dict with new version

        Raises:
            Exception: On network or API errors
        """
        headers = {"If-Match": str(version)} if version else {}
        resp = await self._request(
            "PUT", f"/drive/files/{path}", content=content, headers=headers
        )
        if resp.status_code not in (200, 201):
            raise ValueError(f"upload {path} returned {resp.status_code}")
        return resp.json()

    async def delete(self, path: str, version: int = 0) -> None:
        """Delete a file from the cloud.

        Args:
            path: Relative path to file
            version: Base version (for conflict detection)

        Raises:
            Exception: On network or API errors
        """
        headers = {"If-Match": str(version)} if version else {}
        resp = await self._request(
            "DELETE", f"/drive/files/{path}", headers=headers
        )
        if resp.status_code not in (200, 204):
            raise ValueError(f"delete {path} returned {resp.status_code}")

    async def close(self) -> None:
        """Close the underlying httpx client."""
        if self.client and not self.client.is_closed:
            await self.client.aclose()
