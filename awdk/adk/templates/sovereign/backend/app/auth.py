"""User identity propagation — Portal headers + API key fallback."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .config import settings


@dataclass
class UserContext:
    user_id: str
    tenant_id: str
    role: str
    display_name: str


def get_user_context(request: Request) -> UserContext:
    """Extract user identity from session, Portal headers, or defaults."""
    session_user = getattr(getattr(request, "state", None), "user", None)
    if session_user:
        return UserContext(
            user_id=getattr(session_user, "user_id", "") or "anonymous",
            tenant_id=getattr(session_user, "tenant_id", "") or settings.default_tenant_id,
            role=getattr(session_user, "role", "") or "member",
            display_name=getattr(session_user, "display_name", "") or "anonymous",
        )

    user_id = request.headers.get("X-User-Id", "anonymous")
    tenant_id = request.headers.get("X-Tenant-Id") or settings.default_tenant_id
    role = request.headers.get("X-Workspace-Role", "member")
    display_name = request.headers.get("X-Display-Name", user_id)

    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        display_name=display_name,
    )
