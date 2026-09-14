"""Strata virtual filesystem client (port 8136)."""

import base64
from typing import Optional

from adk.client._base import ServiceClient


class StrataClient(ServiceClient):
    """Client for the Strata virtual filesystem service."""

    async def write(
        self,
        path: str,
        content: str,
        tier: str = "warm",
        metadata: Optional[dict] = None,
    ) -> dict:
        """Write content to a Strata path."""
        payload = {"path": path, "content": content, "tier": tier}
        if metadata:
            payload["metadata"] = metadata
        return await self._post("/strata/write", json=payload, timeout=30.0)

    async def read(self, path: str) -> bytes:
        """Read raw bytes from a Strata path (streaming response)."""
        client = await self._get_client()
        resp = await client.get(f"{self._base_url}/strata/read/{path}", timeout=30.0)
        if resp.status_code == 200:
            return resp.content
        return b""

    async def list_artifacts(self, tier: str = "warm", prefix: str = "artifacts") -> dict:
        """List artifacts in a Strata tier."""
        return await self._get(f"/strata/list/{tier}/{prefix}")

    # -- Sync-oriented methods ------------------------------------------------

    async def upload_file(
        self,
        path: str,
        data: bytes,
        tier: str = "warm",
        metadata: Optional[dict] = None,
    ) -> dict:
        """Upload binary file data to a Strata path (base64-encoded)."""
        payload: dict = {
            "path": path,
            "content": base64.b64encode(data).decode("ascii"),
            "tier": tier,
        }
        if metadata:
            payload["metadata"] = metadata
        return await self._post("/strata/write", json=payload, timeout=60.0)

    async def download_file(self, path: str) -> bytes:
        """Download file data from a Strata path. Returns raw bytes."""
        return await self.read(path)

    async def list_dir(self, prefix: str, tier: str = "warm") -> dict:
        """List files under a prefix in Strata."""
        return await self._get(f"/strata/list/{tier}/{prefix}")

    async def delete(self, path: str) -> dict:
        """Delete a file from Strata."""
        return await self._delete(f"/strata/delete/{path}")
