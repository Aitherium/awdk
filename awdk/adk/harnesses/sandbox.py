"""Dev sandboxes — provision a container, then open a real shell inside it.

This is a CLIENT of machinery that already exists on this platform, not a
reimplementation of it:

- ``AitherGenesis/routers/dev_workspace.py`` (``/v1/dev-workspace/*``) provisions
  a scoped workspace and returns a descriptor.
- ``AitherTunnel._spawn_workspace_container`` runs the actual container from
  ``aitheros-dev-workspace:latest`` with ``--cap-drop=ALL
  --security-opt=no-new-privileges``.
- ``Dockerfile.DevWorkspace`` bakes Python 3.12, Node 22, pwsh, code-server, gh
  and Claude Code into it.

What was missing was the last hop: nothing let a shell say "give me a sandbox
and put me inside it". :func:`provision` plus the ``sandbox`` PTY harness closes
that loop — provision returns a container name, and a PTY session on that
container is a real Linux TTY with the full toolchain.

Auth note: provisioning derives the developer identity from the AUTHENTICATED
caller on the Genesis side and refuses payload-supplied identities for
non-admins. This client therefore sends the bearer and nothing else — it never
claims an identity, because a caller-supplied identity is precisely the
authorization defect pattern #2 in the security rules.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

GENESIS_URL = os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001")
PROVISION_PATH = "/v1/dev-workspace"

#: Default repos a sandbox may materialize. The server holds the real
#: whitelist; this is only what we ASK for.
DEFAULT_REPOS = ["awkit", "adk"]


class SandboxError(RuntimeError):
    """A sandbox operation failed. Always surfaced with the server's own words."""


def _bearer() -> str:
    return (
        os.environ.get("AITHER_IDENTITY_BEARER", "")
        or os.environ.get("AITHER_SESSION_BEARER", "")
        or os.environ.get("AITHER_GATEWAY_KEY", "")
    ).strip()


def _client() -> Any:
    try:
        import httpx
    except ImportError as exc:
        raise SandboxError("httpx is required for sandbox operations") from exc
    headers = {}
    token = _bearer()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Internal TLS is trusted via the platform CA; verification stays ON.
    return httpx.Client(base_url=GENESIS_URL, headers=headers, timeout=120.0)


def _unwrap(response: Any, action: str) -> dict[str, Any]:
    if response.status_code >= 400:
        # Carry the server's message through verbatim. A generic
        # "provisioning failed" hides a 403 that means "you are not an admin"
        # and sends the operator debugging the container runtime instead.
        raise SandboxError(
            f"{action} failed: HTTP {response.status_code} {response.text[:400]}"
        )
    payload = response.json()
    return payload if isinstance(payload, dict) else {"result": payload}


def provision(
    workspace_slug: str,
    repos: Optional[list[str]] = None,
    *,
    genesis_url: str = "",
) -> dict[str, Any]:
    """Provision a dev workspace and return its descriptor."""
    global GENESIS_URL
    if genesis_url:
        GENESIS_URL = genesis_url.rstrip("/")
    body = {
        "workspace_slug": workspace_slug,
        "allowed_repos": repos or list(DEFAULT_REPOS),
    }
    with _client() as client:
        return _unwrap(client.post(f"{PROVISION_PATH}/provision", json=body), "provision")


def mine() -> list[dict[str, Any]]:
    """Every sandbox belonging to the authenticated caller."""
    with _client() as client:
        payload = _unwrap(client.get(f"{PROVISION_PATH}/mine"), "list sandboxes")
    workspaces = payload.get("workspaces")
    return workspaces if isinstance(workspaces, list) else []


def teardown(workspace_id: str) -> dict[str, Any]:
    with _client() as client:
        return _unwrap(
            client.post(f"{PROVISION_PATH}/{workspace_id}/teardown", json={}), "teardown"
        )


#: AitherTunnel's dev-workspace container prefix (AitherTunnel.py:7285).
DEV_WORKSPACE_PREFIX = "aitheros-devws-"


def _email_slug(email: str) -> str:
    """The slug half of AitherTunnel's container name (AitherTunnel.py:7586)."""
    local = email.split("@")[0].lower()
    return re.sub(r"[^a-z0-9-]", "-", local).strip("-")


def container_name(descriptor: dict[str, Any]) -> tuple[str, str]:
    """Resolve the container to exec into. Returns ``(container, reason)``.

    ``container`` is "" when it cannot be determined, and ``reason`` says why.
    This function deliberately does NOT guess.

    Why that matters here: AitherTunnel names the container
    ``{DEV_WORKSPACE_PREFIX}{email-slug}-{session_id[:8]}``
    (``_dev_container_name``, AitherTunnel.py:7586), but Genesis's
    ``WorkspaceDescriptor`` (dev_workspace_provisioner.py:334) carries
    ``workspace_id``/``dev_identity``/``browser_ssh_target`` and **no container
    field and no session id at all**. An earlier version of this function
    invented ``aitheros-devws-{workspace_id}``; that name never exists, so
    ``docker exec`` would have failed with "no such container" and read as a
    broken terminal rather than as an unresolvable provisioning result.

    The descriptor's ``browser_ssh_target`` is the intended connect route when
    no container name is available.
    """
    for key in ("container_name", "container", "workspace_container"):
        value = descriptor.get(key)
        if isinstance(value, str) and value:
            return (value, "")

    email = descriptor.get("dev_identity")
    session_id = descriptor.get("session_id") or descriptor.get("tunnel_session_id")
    if isinstance(email, str) and email and isinstance(session_id, str) and session_id:
        return (f"{DEV_WORKSPACE_PREFIX}{_email_slug(email)}-{session_id[:8]}", "")

    return (
        "",
        "the workspace descriptor carries no container name and no session id, so the "
        "container cannot be derived. Connect via browser_ssh_target "
        f"({descriptor.get('browser_ssh_target') or 'not set'}), or pass an explicit "
        "--target to a sandbox session.",
    )
