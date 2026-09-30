"""Connector env resolution for adk agents.

The agent half of the Perplexity-Connectors-style surface (owner ask
2026-08-27): a tenant admin connects GitHub / Google Drive ONCE via OAuth
(the dance is AitherIdentity's; the store and resolution plane are
Genesis /connectors/*). A spawned agent gets ``CONNECTOR_*`` env vars
resolved from the CALLER's tenant — no token in the prompt, no token in tool
args, nothing the agent has to ask for.

    CONNECTOR_GITHUB_TOKEN        (github)
    CONNECTOR_GOOGLE_DRIVE_TOKEN  (google_drive)

Resolution is FAIL-SOFT on outage (connectors are additive — an unreachable
genesis must not stop an agent from spawning; the agent just lacks the vars)
but LOUD on a 403 (the caller is genuinely not entitled — that is a config
error, not a blip). The sync form is the one harness spawns use; it is
cached by the caller (60s) so per-turn spawns do not re-resolve.

The bearer is the session's own credential (same source as adk/sync/secrets
— server-side scoping from the bearer, never an X-Tenant-ID).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from adk._tls import tls_verify

log = logging.getLogger("adk.connectors")

_ENV_PREFIX = "CONNECTOR_"

#: Connectors holding the owner's personal data (mail, calendar, to-do) --
#: mirrors ``PERSONAL_CONNECTORS`` in AitherOS lib/connectors/catalog.py. A
#: resolve-all (``connectors`` is None: the harness ``_child_env()`` path, whose
#: children hold shell and file tools and no approval gate) never carries their
#: tokens. Genesis already leaves them out; this strips them again so an older
#: Genesis cannot put mailbox access into every child env. Only a caller that
#: NAMES them (the gated Hearth tools) receives them.
PERSONAL_CONNECTORS = ("google_calendar", "gmail", "microsoft_graph")
_PERSONAL_ENV = frozenset(f"{_ENV_PREFIX}{c.upper()}_TOKEN" for c in PERSONAL_CONNECTORS)


def _genesis_base() -> str:
    return (
        os.environ.get("AITHER_GENESIS_URL")
        or os.environ.get("GENESIS_URL")
        or "https://aitheros-genesis:8001"
    ).rstrip("/")


def _bearer() -> str:
    for name in ("AITHER_API_KEY", "AITHER_IDENTITY_BEARER", "AITHER_SESSION_BEARER"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _clean_env(payload: Dict, named: bool = False) -> Dict[str, str]:
    """Keep only CONNECTOR_* values from a resolve response (never trust the
    server to hand back anything else — this env reaches every child).

    ``named`` is False for a resolve-all: personal-data tokens are dropped.
    """
    if not isinstance(payload, dict):
        payload = {}
    return {
        k: str(v)
        for k, v in payload.items()
        if k.startswith(_ENV_PREFIX) and v and (named or k not in _PERSONAL_ENV)
    }


async def resolve_connectors(
    connectors: Optional[List[str]] = None,
    genesis_base: Optional[str] = None,
    bearer: Optional[str] = None,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """Resolve connector tokens, saying WHY when none came back.

    Returns ``{"env": {CONNECTOR_*: token}, "error": reason}`` where ``error``
    is "" on a clean answer (``env`` may still be empty: nothing connected),
    else ``unreachable`` / ``denied`` / ``http_<code>`` / ``bad_response``. A
    caller that must tell a person what to do (the Hearth tools) needs that
    difference; :func:`resolve_connector_env` keeps the fail-soft ``{}``.
    ``connectors`` narrows the resolve to the named providers (fewer tokens
    minted, fewer audit rows); None resolves every connected one EXCEPT the
    :data:`PERSONAL_CONNECTORS`, which come back only when named.
    """
    import httpx

    base = (genesis_base or _genesis_base()).rstrip("/")
    headers = {"Content-Type": "application/json"}
    token = bearer if bearer is not None else _bearer()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload: Dict[str, Any] = {} if connectors is None else {"connectors": list(connectors)}
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=tls_verify()) as client:
            resp = await client.post(
                f"{base}/connectors/resolve", headers=headers, json=payload
            )
    except (httpx.HTTPError, OSError) as exc:
        log.warning(f"connector env resolution skipped (unreachable): {exc}")
        return {"env": {}, "error": "unreachable"}
    if resp.status_code in (401, 403):
        log.warning(
            "connector env resolution DENIED (%s) — this caller is not entitled "
            "to connectors; an admin must fix the tenant context", resp.status_code,
        )
        return {"env": {}, "error": "denied"}
    if resp.status_code != 200:
        log.warning(f"connector env resolution skipped (HTTP {resp.status_code})")
        return {"env": {}, "error": f"http_{resp.status_code}"}
    try:
        named = connectors is not None
        return {"env": _clean_env(resp.json().get("env"), named=named), "error": ""}
    except (ValueError, AttributeError) as exc:
        log.warning(f"connector env resolution returned an unreadable body: {exc}")
        return {"env": {}, "error": "bad_response"}


async def resolve_connector_env(
    genesis_base: Optional[str] = None,
    bearer: Optional[str] = None,
    timeout: float = 8.0,
    connectors: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Resolve the caller's connector env vars from Genesis /connectors/resolve.

    Returns {} when nothing is connected, genesis is unreachable, or the
    caller is denied — the agent still spawns; the vars are simply absent.
    """
    result = await resolve_connectors(
        connectors, genesis_base=genesis_base, bearer=bearer, timeout=timeout,
    )
    return result["env"]


def resolve_connector_env_sync(timeout: float = 3.0) -> Dict[str, str]:
    """Sync form for harness `_child_env()` (session-cached by the caller).

    A resolve-all: never returns a :data:`PERSONAL_CONNECTORS` token.
    """
    import httpx

    base = _genesis_base()
    headers = {"Content-Type": "application/json"}
    token = _bearer()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=timeout, verify=tls_verify()) as client:
            resp = client.post(f"{base}/connectors/resolve", headers=headers, json={})
        if resp.status_code == 403:
            log.warning("connector env resolution DENIED (403) for this caller")
            return {}
        if resp.status_code != 200:
            return {}
        return _clean_env(resp.json().get("env"))
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        log.warning(f"connector env sync resolution skipped: {exc}")
        return {}


def setup_gh_auth(token: str = "") -> dict:
    """Sandbox pairing: materialize the GitHub connector into gh's store.

    Perplexity's headline Connectors pairing, for the AitherOS sandbox: "use
    git and gh with your credentials already in place". The dev-workspace
    image bakes ``gh``; this consumes CONNECTOR_GITHUB_TOKEN (the env var a
    harness session injected) and hands it to gh's own credential store —
    after which ``gh api`` / ``git`` (via ``gh auth setup-git``) work with
    the CONNECTED ACCOUNT's permissions, with no token in any command line,
    prompt, or tool argument. Idempotent.
    """
    token = token or os.environ.get("CONNECTOR_GITHUB_TOKEN", "")
    if not token:
        return {"success": False, "reason": "no CONNECTOR_GITHUB_TOKEN in env"}

    login = subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=token + "\n", text=True, capture_output=True,
        encoding="utf-8", errors="replace",
    )
    if login.returncode != 0:
        return {
            "success": False, "reason": "gh auth login failed",
            "error": (login.stderr or login.stdout)[:300],
        }
    setup_git = subprocess.run(["gh", "auth", "setup-git"], capture_output=True)
    return {
        "success": True,
        "git_rewrite": setup_git.returncode == 0,
        "account": _gh_whoami(),
    }


def _gh_whoami() -> str:
    try:
        who = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        return who.stdout.strip() if who.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def setup_gh_auth_main() -> int:
    result = setup_gh_auth()
    if not result.get("success"):
        sys.stderr.write(f"[connectors] {result.get('reason')}\n")
        return 1
    print(f"[connectors] gh authenticated as {result.get('account', '?')} "
          f"(git rewrite: {result.get('git_rewrite')})")
    return 0
