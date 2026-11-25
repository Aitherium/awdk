"""How the engine reaches a runtime. Two backends, one interface.

``call(method, path, json) -> Reply`` is the whole contract. Everything the engine
creates on the platform goes through a route that already exists and already
authorises the caller, so the engine never decides who may do what:

* ``HttpBackend``  - a local awdk daemon (or any off-box client) calling the platform
  API with the owner's own bearer.
* ``AsgiBackend``  - Genesis calling ITS OWN routes in-process with the caller's
  forwarded credentials (same auth deps, rate limits and RBAC as a browser call).

The memory plane (``MemoryPlane``) is what the Context pillar reads and the Learning
pillar writes. The platform plane is ``/external/memory/*`` (tenant scope comes from
the authenticated caller, never from the payload); the local plane is ``adk.memory``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

logger = logging.getLogger("adk.create_flow")


@dataclass
class Reply:
    status: int
    body: Any = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def detail(self) -> str:
        b = self.body
        if isinstance(b, dict):
            d = b.get("detail") or b.get("error") or b.get("message")
            if isinstance(d, (dict, list)):
                return json.dumps(d)[:300]
            if d:
                return str(d)[:300]
        return (str(b) if b is not None else "")[:300] or f"HTTP {self.status}"


class Backend(Protocol):
    name: str

    async def call(self, method: str, path: str, body: Any = None) -> Reply: ...


async def _httpx_call(
    client: Any, method: str, path: str, body: Any, headers: Mapping[str, str]
) -> Reply:
    try:
        resp = await client.request(method, path, json=body, headers=dict(headers))
    except Exception as exc:  # noqa: BLE001 - a dead backend is a Reply, not a crash
        return Reply(599, {"error": f"{type(exc).__name__}: {exc}"[:300]})
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    return Reply(resp.status_code, data)


class HttpBackend:
    """The platform API over HTTP with a user bearer (local daemon -> Genesis)."""

    name = "platform-api"

    def __init__(self, base_url: str, bearer: str, *, verify: Any = True, timeout: float = 60.0):
        import httpx

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), verify=verify, timeout=timeout
        )
        self._headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}

    async def call(self, method: str, path: str, body: Any = None) -> Reply:
        if not self._headers:
            return Reply(401, {"error": "no platform bearer; run `aither login` first"})
        return await _httpx_call(self._client, method, path, body, self._headers)

    async def aclose(self) -> None:
        await self._client.aclose()


class AsgiBackend:
    """Genesis calling its own routes in-process with the caller's credentials."""

    name = "genesis"

    def __init__(
        self,
        app: Any,
        headers: Mapping[str, str],
        *,
        identity: Optional[Mapping[str, str]] = None,
        timeout: float = 60.0,
    ):
        import httpx

        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://genesis.internal",
            timeout=timeout,
        )
        # The caller's own credentials are forwarded so the inner route re-authenticates
        # them (a JWT is re-validated; its claims win over any header). Client-written x-*
        # identity headers are NEVER copied: ``identity`` is built by the router from the
        # already-resolved CallerContext, so a header cannot point the inner call at another
        # tenant. Nothing here ever adds an internal key or a platform caller.
        keep = {"authorization", "cookie"}
        self._headers = {k: v for k, v in headers.items() if k.lower() in keep}
        self._headers.update({k: v for k, v in (identity or {}).items() if v})

    async def call(self, method: str, path: str, body: Any = None) -> Reply:
        return await _httpx_call(self._client, method, path, body, self._headers)

    async def aclose(self) -> None:
        await self._client.aclose()


# ── the memory plane ────────────────────────────────────────────────────────


class MemoryPlane(Protocol):
    name: str

    async def remember(self, category: str, content: str) -> bool: ...

    async def recall(self, query: str, category: str = "") -> list[str]: ...


class PlatformMemory:
    """``/external/memory/*`` - the tenant's own knowledge graph (MemoryGraph / Strata)."""

    name = "platform-memory"

    def __init__(self, backend: Backend):
        self._b = backend

    async def remember(self, category: str, content: str) -> bool:
        r = await self._b.call(
            "POST", "/external/memory/remember", {"content": content, "category": category}
        )
        return r.ok

    async def recall(self, query: str, category: str = "") -> list[str]:
        r = await self._b.call(
            "POST", "/external/memory/recall", {"query": query, "category": category or None}
        )
        if not r.ok or not isinstance(r.body, dict):
            return []
        out = []
        for m in r.body.get("memories", []) or []:
            if isinstance(m, dict):
                text = m.get("content") or m.get("snippet") or ""
                if text:
                    out.append(str(text))
        return out


class LocalMemory:
    """``adk.memory.Memory`` (SQLite, plus Spirit/fleet sync when configured)."""

    name = "local-memory"

    def __init__(self, db_path: Any, agent_name: str = "create-flow"):
        from pathlib import Path

        from adk.memory import Memory

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._m = Memory(db_path=db_path, agent_name=agent_name)
        self._n = 0

    async def remember(self, category: str, content: str) -> bool:
        import hashlib

        key = f"{category}:{hashlib.sha1(content.encode('utf-8')).hexdigest()[:16]}"
        await self._m.remember(key, content, category=category)
        return True

    async def recall(self, query: str, category: str = "") -> list[str]:
        rows = await self._m.search(query, limit=50)
        return [r.value for r in rows if not category or r.category == category]


class MemoryFanout:
    """Write to every plane that is up; read from all and de-duplicate."""

    name = "fanout"

    def __init__(self, planes: list[MemoryPlane]):
        self.planes = planes

    async def remember(self, category: str, content: str) -> list[str]:  # type: ignore[override]
        sinks: list[str] = []
        for p in list(self.planes):
            try:
                if await p.remember(category, content):
                    sinks.append(p.name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("memory plane %s write failed: %s", p.name, exc)
        return sinks

    async def recall(self, query: str, category: str = "") -> list[str]:
        seen: list[str] = []
        for p in list(self.planes):
            try:
                for text in await p.recall(query, category):
                    if text not in seen:
                        seen.append(text)
            except Exception as exc:  # noqa: BLE001
                logger.debug("memory plane %s read failed: %s", p.name, exc)
        return seen


Hook = Callable[..., Awaitable[Any]]
