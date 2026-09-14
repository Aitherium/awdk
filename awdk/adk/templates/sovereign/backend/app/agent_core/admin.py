"""FastAPI admin router for agent_core memory operations.

Mount this on any host app to expose:
    GET  /api/memory/stats              -- per-tier counts, bridge health
    GET  /api/memory/recent             -- recent memories for the tenant
    POST /api/memory/recall             -- {query} -> top-k results
    POST /api/memory/forget             -- {document_id} block recall + stale citations

The host app must provide:
    1. A way to resolve the active UnifiedMemoryClient (singleton).
    2. A way to resolve the current tenant_id (from auth or header).

Usage in main.py:
    from app.agent_core.admin import build_memory_router
    from app.agent_memory import get_memory_client, resolve_tenant_id
    app.include_router(build_memory_router(get_memory_client, resolve_tenant_id))
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .client import UnifiedMemoryClient


class RecallRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(10, ge=1, le=50)
    min_confidence: float = Field(0.30, ge=0.0, le=1.0)


class ForgetRequest(BaseModel):
    document_id: str = Field(..., min_length=1, max_length=200)


ClientGetter = Callable[[], UnifiedMemoryClient | Awaitable[UnifiedMemoryClient]]
TenantResolver = Callable[[Request], str | Awaitable[str]]


async def _resolve_client(getter: ClientGetter) -> UnifiedMemoryClient:
    res = getter()
    if hasattr(res, "__await__"):
        res = await res  # type: ignore[func-returns-value]
    if not isinstance(res, UnifiedMemoryClient):
        raise HTTPException(500, "agent_core: memory client not available")
    return res


async def _resolve_tenant(req: Request, resolver: TenantResolver) -> str:
    res = resolver(req)
    if hasattr(res, "__await__"):
        res = await res  # type: ignore[func-returns-value]
    tenant = str(res or "").strip()
    if not tenant:
        raise HTTPException(401, "agent_core: tenant context required")
    return tenant


def build_memory_router(
    get_client: ClientGetter,
    resolve_tenant: TenantResolver,
    *,
    prefix: str = "/api/memory",
    tags: list[str] | None = None,
) -> APIRouter:
    """Construct a FastAPI router wired to the host app's singletons."""
    router = APIRouter(prefix=prefix, tags=tags or ["memory"])

    @router.get("/stats")
    async def stats(
        request: Request,
        scoped: bool = True,
    ) -> dict[str, Any]:
        client = await _resolve_client(get_client)
        tenant = await _resolve_tenant(request, resolve_tenant) if scoped else None
        return await client.stats(tenant_id=tenant)

    @router.get("/recent")
    async def recent(
        request: Request,
        limit: int = 20,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        client = await _resolve_client(get_client)
        tenant = await _resolve_tenant(request, resolve_tenant)
        rows = await client.recent(
            tenant, limit=max(1, min(limit, 100)),
            conversation_id=conversation_id,
        )
        return {
            "tenant_id": tenant,
            "count": len(rows),
            "memories": [
                {
                    "id": r.memory_id,
                    "tier": r.tier.value,
                    "content": r.content,
                    "hits": r.hits,
                    "confidence": round(r.confidence, 3),
                    "sources": sorted(r.sources),
                    "auto_recall_blocked": r.auto_recall_blocked,
                    "created_at": r.created_at,
                    "accessed_at": r.accessed_at,
                }
                for r in rows
            ],
        }

    @router.post("/recall")
    async def recall(
        request: Request,
        body: RecallRequest = Body(...),
    ) -> dict[str, Any]:
        client = await _resolve_client(get_client)
        tenant = await _resolve_tenant(request, resolve_tenant)
        results = await client.recall(
            tenant, body.query,
            limit=body.limit, min_confidence=body.min_confidence,
        )
        return {
            "query": body.query,
            "count": len(results),
            "results": [r.as_dict() for r in results],
        }

    @router.post("/forget")
    async def forget(
        request: Request,
        body: ForgetRequest = Body(...),
    ) -> dict[str, Any]:
        client = await _resolve_client(get_client)
        tenant = await _resolve_tenant(request, resolve_tenant)
        blocked = await client.block_recall_for_document(tenant, body.document_id)
        return {
            "document_id": body.document_id,
            "memories_blocked": blocked,
        }

    return router
