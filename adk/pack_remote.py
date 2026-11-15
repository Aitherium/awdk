"""Where the kit gets the pack catalogue and pack downloads from -- one answer.

Genesis publishes no host port, so ``http://localhost:8001`` answers nothing on an
owner machine or anywhere else, and the MCP gateways (``mcp.aitherium.com``,
``gateway.aitherium.com``, the local ``:8182``) serve the catalogue only as MCP
tools: every HTTP catalogue route there is a 404. Measured 2026-11-12, the routes
answer in exactly one place: the portal's Genesis bridge
(``https://api.aitherium.com/api/bridge/genesis``), with the caller's bearer.

An explicitly configured base (``AITHER_GENESIS_URL`` and friends) is still tried
first, but it is not trusted to be right: on the measured machine a user-level
``AITHER_API_URL`` named the MCP gateway, and every pack command then failed
against a host that has none of the routes. Each call therefore walks the
candidates in order, uses the first that serves the route, and reports what it
tried, so a wrong override degrades to the bridge instead of to "not found".
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from adk.control_plane import GENESIS_BRIDGE_PREFIX, control_plane_url, genesis_override

#: Catalogue routes, in the order they are merged (first id wins).
CATALOG_ROUTES = ("/api/v1/catalog/packs", "/v1/packs/catalog")

Fetch = Callable[..., Any]


def bases(explicit: str = "") -> List[str]:
    """Candidate Genesis bases for pack routes, best first, without duplicates.

    ``explicit`` is a base a caller already resolved (kept for the callers that
    pass one); a ``localhost:8001`` value is dropped because nothing listens there.
    """
    out: List[str] = []
    for base in (explicit, genesis_override(), control_plane_url() + GENESIS_BRIDGE_PREFIX):
        base = (base or "").rstrip("/")
        if not base or base in out or _is_dead_default(base):
            continue
        out.append(base)
    return out


def _is_dead_default(base: str) -> bool:
    host = base.split("://", 1)[-1].split("/", 1)[0].lower()
    return host in ("localhost:8001", "127.0.0.1:8001")


def auth_headers() -> Dict[str, str]:
    """The caller's saved bearer (``adk login`` / ``aither login``), or {}.

    The bridge answers 401 without one; the catalogue is per-caller (a plan
    holder sees their plan's packs), so the token is part of the request.
    """
    token = os.environ.get("AITHER_API_KEY", "")
    tenant = os.environ.get("AITHER_TENANT_ID", "")
    if not token:
        try:
            from adk.cli import load_saved_config
            cfg = load_saved_config() or {}
            token = str(cfg.get("api_key") or cfg.get("access_token") or "")
            tenant = tenant or str(cfg.get("tenant_id") or "")
        except Exception:  # noqa: BLE001 - no saved config is an anonymous caller
            token = ""
    if not token:
        try:
            from adk.shell.auth import AuthStore
            token = str((AuthStore.get_active_profile() or {}).get("access_token") or "")
        except Exception:  # noqa: BLE001 - the shell profile is optional
            token = ""
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if tenant:
        headers["X-Tenant-ID"] = tenant
    return headers


def _http_get(base: str, path: str, headers: Dict[str, str], timeout: float) -> Any:
    import httpx
    with httpx.Client(base_url=base, timeout=timeout, headers=headers) as c:
        return c.get(path)


def _http_post(base: str, path: str, headers: Dict[str, str], timeout: float) -> Any:
    import httpx
    with httpx.Client(base_url=base, timeout=timeout, headers=headers) as c:
        return c.post(path)


def _why(resp: Any) -> str:
    code = getattr(resp, "status_code", 0)
    if code == 401:
        return "HTTP 401 (not signed in: run `adk login`)"
    return f"HTTP {code}"


def fetch_catalog(explicit: str = "", *, get: Optional[Fetch] = None,
                  timeout: float = 15.0) -> Tuple[List[dict], str, List[str]]:
    """``(packs, source, tried)`` from the first base that serves the catalogue.

    ``packs`` merges both catalogue routes of that base (agent packs, then the
    Library tool packs), first id wins. ``source`` is the base that answered, or
    "" when none did. ``tried`` has one ``"<url>: <outcome>"`` line per attempt,
    so a caller can say exactly why it fell back to the bundled catalogue.
    """
    get = get or _http_get
    headers = auth_headers()
    tried: List[str] = []
    for base in bases(explicit):
        merged: Dict[str, dict] = {}
        answered = False
        for route in CATALOG_ROUTES:
            url = base + route
            try:
                resp = get(base, route, headers, timeout)
            except Exception as exc:  # noqa: BLE001 - recorded, next candidate
                tried.append(f"{url}: {type(exc).__name__}")
                continue
            if getattr(resp, "status_code", 0) != 200:
                tried.append(f"{url}: {_why(resp)}")
                continue
            try:
                rows = (resp.json() or {}).get("packs") or []
            except Exception:  # noqa: BLE001 - an HTML error page is not a catalogue
                tried.append(f"{url}: not JSON")
                continue
            answered = True
            tried.append(f"{url}: {len(rows)} packs")
            for row in rows:
                pid = str((row or {}).get("id") or "")
                if pid and pid not in merged:
                    merged[pid] = row
        if answered:
            return list(merged.values()), base, tried
    return [], "", tried


def download(pack_id: str, explicit: str = "", *, get: Optional[Fetch] = None,
             post: Optional[Fetch] = None,
             timeout: float = 60.0) -> Tuple[Optional[Any], str, List[str]]:
    """``(response, base, tried)`` for a pack tarball.

    The first base whose answer is about the PACK wins: 200 (the tarball), 402 or
    403 (a license is needed -- trying another host would not change that). A
    404, a 401 or a transport error moves on to the next candidate; when every
    candidate fails the last response is returned so the caller can report it.
    """
    get = get or _http_get
    post = post or _http_post
    headers = auth_headers()
    tried: List[str] = []
    last: Optional[Any] = None
    last_base = ""
    for base in bases(explicit):
        resp = None
        # GET is the form every shipped kit calls; POST is the route an older
        # Genesis serves instead (it gained the GET alias on 2026-07-30).
        for method, route in ((get, f"/v1/packs/{pack_id}/download"),
                              (post, f"/v1/packs/download/{pack_id}")):
            url = base + route
            try:
                resp = method(base, route, headers, timeout)
            except Exception as exc:  # noqa: BLE001 - recorded, next candidate
                tried.append(f"{url}: {type(exc).__name__}: {exc}")
                resp = None
                break
            tried.append(f"{url}: {_why(resp)}")
            if resp.status_code not in (404, 405):
                break
        if resp is None:
            continue
        last, last_base = resp, base
        if resp.status_code in (200, 402, 403):
            return resp, base, tried
    return last, last_base, tried


def hoist_single_root(target: Any) -> bool:
    """Flatten ``<target>/<id>-<version>/...`` into ``<target>/...``.

    Genesis prefixes every archive member with ``<pack>-<version>/``; the kit
    extracts into ``~/.aitheros/packs/<pack>/`` and its loader looks for
    ``<pack>/.toolpack.yaml`` exactly one level down, so an installed tool pack
    was on disk and invisible. True when the tree was flattened.
    """
    import shutil
    from pathlib import Path

    root = Path(target)
    try:
        children = list(root.iterdir())
    except OSError:
        return False
    if len(children) != 1 or not children[0].is_dir() or children[0].is_symlink():
        return False
    inner = children[0]
    for child in list(inner.iterdir()):
        shutil.move(str(child), str(root / child.name))
    inner.rmdir()
    return True


__all__ = ["CATALOG_ROUTES", "auth_headers", "bases", "download", "fetch_catalog",
           "hoist_single_root"]
