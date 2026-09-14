"""Base class for service sub-clients."""

from typing import Any, Callable, Awaitable, Optional

import httpx


class ServiceClient:
    """Base for service-specific sub-clients.

    Shares the parent AitherClient's httpx client and auth headers.
    """

    def __init__(self, base_url: str, get_client: Callable[[], Awaitable[httpx.AsyncClient]]):
        self._base_url = base_url.rstrip("/")
        self._get_client = get_client

    async def _get(self, path: str, *, timeout: Optional[float] = None, **kwargs) -> dict:
        client = await self._get_client()
        kw = {}
        if timeout:
            kw["timeout"] = timeout
        resp = await client.get(f"{self._base_url}{path}", **kw, **kwargs)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    async def _post(self, path: str, json: Any = None, *, timeout: Optional[float] = None, **kwargs) -> dict:
        client = await self._get_client()
        kw = {}
        if timeout:
            kw["timeout"] = timeout
        resp = await client.post(f"{self._base_url}{path}", json=json, **kw, **kwargs)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    async def _delete(self, path: str, *, timeout: Optional[float] = None, **kwargs) -> dict:
        client = await self._get_client()
        kw = {}
        if timeout:
            kw["timeout"] = timeout
        resp = await client.delete(f"{self._base_url}{path}", **kw, **kwargs)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    async def health(self) -> dict:
        """Check service health."""
        try:
            return await self._get("/health", timeout=5.0)
        except Exception:
            return {"status": "unreachable"}
