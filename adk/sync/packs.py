"""`adk sync packs` -- converge this machine onto the packs your workspace holds.

WHY THIS EXISTS (2026-10-04)
---------------------------------------------------------------------------
A workspace on aitherium.com owns agent packs, skill packs, tool packs and apps,
and every surface on this machine (awdesk, awsh, awconnect) wants the same
answer to "what should be installed here, and what changed?". The pieces were
there but nothing joined them: `adk link` fetched the role-aware bundle,
awconnect listed workspaces, `adk pack sync` installed missing entitled packs
but never updated one, never removed one whose license ended, and printed prose
no other surface could read. This is the one client that does all of it:

  ({g} = {portal}/api/bridge/genesis, the portal's Genesis bridge)
  1. who + where     GET {portal}/api/me/workspaces           (user bearer)
                     GET {g}/v1/link/bundle                   (role)
  2. desired set     GET {g}/v1/marketplace/license/mine      (X-Workspace-ID)
                     -- purchased licenses plus tenant-entitled packs, the
                     SAME list `adk pack sync` installs
  3. versions/kinds  GET {g}/v1/packs/catalog
  4. local set       ~/.aitheros/packs/<id>/ (the agent discovery root), with
                     the sync ledger ``.aither-pack-sync.json`` there
  5. diff            install / update / remove / unchanged (+ unmanaged)
  6. apply           GET {g}/v1/packs/{id}/download -> sha + Ed25519
                     verify -> extract -> swap in; then hot-reload the running
                     agent server (POST {adk}/agent/packs/reload), the same
                     activation `/packs activate` performs.

Safety rules that are the point of the design:

* A pack is only REMOVED if this sync (or the license-driven `adk pack sync`,
  which writes ``.aither-entitlement.json``) installed it, for the SAME
  workspace scope. A hand-installed pack is reported as ``unmanaged`` and is
  never touched; syncing workspace B never removes workspace A's packs.
* No removal is computed unless the entitlement list answered 200 with a
  well-formed body: an outage or a 401 must never read as "you own nothing".
* A company-persona (``source: tenant``) pack is removed ONLY when license/mine
  says ``tenant_listed: true`` -- Genesis answers 200 with purchased rows alone
  when its packs catalog fails to load, and absence there is not revocation.
  An older Genesis that does not send the field never triggers a tenant removal.
* Integrity fails CLOSED: a download is installed only when it carried an
  X-Pack-SHA256 that matched or an X-Aither-Pack-Signature that verified.
  ``--allow-unverified`` is the explicit, reported escape hatch. Every result
  names its ``integrity`` (sha256, signature, sha256+signature, unverified).
* Removed and updated packs are NOT unloaded from a running agent: the server's
  reload only adds. The report lists them under ``activation.pending_restart``
  and never calls that "reloaded".
* The token is never printed, logged, or put in a URL.

Gaps stated, not papered over: there is no server endpoint that lists a
workspace's packs by kind (agent/skill/tool/app) -- kind is derived from the
license row's ``pack_type`` (agent_pack/skill_pack/tool_pack), else the catalog
row (``app_manifest_id`` -> app, tools -> tool, skills only -> skill, else agent).
Versions come from the license/mine row (Genesis copies them from
packs_catalog.yaml, the catalog downloads are served from); /v1/packs/catalog
(tool packs only) is the fallback for an older Genesis. A pack with no version
anywhere is compared by presence and reported as such.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "LEDGER_FILE",
    "PackItem",
    "SyncPlan",
    "RemoteState",
    "apply_plan",
    "compute_diff",
    "default_root",
    "fetch_remote",
    "main",
    "run",
    "scan_local",
]

DEFAULT_PORTAL = "https://api.aitherium.com"
WORKSPACES_PATH = "/api/me/workspaces"
#: Genesis routes are reached through the portal's Genesis bridge: measured
#: 2026-10-04, api.aitherium.com/v1/marketplace/license/mine is a 404 HTML page
#: while /api/bridge/genesis/v1/marketplace/license/mine answers JSON. Override
#: with AITHER_GENESIS_PREFIX="" when AITHER_PORTAL_URL points at Genesis itself.
GENESIS_PREFIX = "/api/bridge/genesis"
BUNDLE_PATH = "/v1/link/bundle"
LICENSES_PATH = "/v1/marketplace/license/mine"
CATALOG_PATH = "/v1/packs/catalog"
DOWNLOAD_PATH = "/v1/packs/{pack_id}/download"
AGENTS_PATH = "/api/workspaces/{workspace_id}/agents"

#: The sync ledger, kept inside the pack root (dot-prefixed: never a pack).
LEDGER_FILE = ".aither-pack-sync.json"
#: Written by the license-driven `adk pack sync` (shell/plugins/builtins/packs.py).
ENTITLEMENT_FILE = ".aither-entitlement.json"

_PACK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MANIFEST_NAMES = ("pack.yaml", "manifest.json", "agent.yaml", "brain_pack.yaml",
                   "toolpack.yaml", "pack.json")

#: (method, url, headers) -> (status, response headers, body bytes). Raises OSError
#: (or any Exception) when the host cannot be reached.
HttpFn = Callable[[str, str, Mapping[str, str]], Tuple[int, Mapping[str, str], bytes]]


# ── data ──────────────────────────────────────────────────────────────────────


@dataclass
class PackItem:
    id: str
    name: str = ""
    kind: str = "agent"
    local_version: Optional[str] = None
    remote_version: Optional[str] = None
    source: str = ""
    reason: str = ""


@dataclass
class SyncPlan:
    install: List[PackItem] = field(default_factory=list)
    update: List[PackItem] = field(default_factory=list)
    remove: List[PackItem] = field(default_factory=list)
    unchanged: List[PackItem] = field(default_factory=list)
    unmanaged: List[PackItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, List[Dict[str, Any]]]:
        return {k: [asdict(i) for i in getattr(self, k)]
                for k in ("install", "update", "remove", "unchanged", "unmanaged")}

    def counts(self) -> Dict[str, int]:
        return {k: len(v) for k, v in self.to_dict().items()}


@dataclass
class RemoteState:
    ok: bool
    workspace_id: str = ""
    workspace_name: str = ""
    workspace_source: str = ""
    workspaces: List[Dict[str, str]] = field(default_factory=list)
    role: Optional[str] = None
    #: pack id -> {source, tenant_id, name, kind, version}
    entitled: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    catalog_ok: bool = False
    #: license/mine's ``tenant_listed``: True only when Genesis computed the
    #: tenant rows from a catalog that loaded. None = an older Genesis.
    tenant_listed: Optional[bool] = None
    agents: List[Dict[str, str]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# ── helpers ───────────────────────────────────────────────────────────────────


def default_root() -> Path:
    """The agent discovery root that `adk pack install|sync` write to."""
    return Path.home() / ".aitheros" / "packs"


def portal_url() -> str:
    return (os.environ.get("AITHER_PORTAL_URL") or DEFAULT_PORTAL).rstrip("/")


def genesis_prefix() -> str:
    return os.environ.get("AITHER_GENESIS_PREFIX", GENESIS_PREFIX).rstrip("/")


def resolve_token() -> Tuple[str, str]:
    """(token, kind). The user bearer first (workspaces need a person), then the
    saved key `adk pack sync` uses. kind names the source; the value is never shown."""
    try:
        from adk.sync.settings import _resolve_token

        tok = _resolve_token()
        if tok:
            return tok, "user_bearer"
    except Exception:  # noqa: BLE001 -- a broken auth store means signed out
        pass
    try:
        from adk.config import load_saved_config

        cfg = load_saved_config() or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    tok = str(cfg.get("api_key") or cfg.get("access_token") or "")
    if tok:
        return tok, "saved_config"
    tok = os.environ.get("AITHER_API_KEY", "").strip()
    return (tok, "env") if tok else ("", "")


def _httpx_fetch(method: str, url: str, headers: Mapping[str, str]
                 ) -> Tuple[int, Mapping[str, str], bytes]:
    import httpx

    try:
        from adk._tls import tls_verify

        verify: Any = tls_verify()
    except Exception:  # noqa: BLE001
        verify = True
    with httpx.Client(timeout=60.0, follow_redirects=True, verify=verify) as c:
        r = c.request(method, url, headers=dict(headers))
    return r.status_code, r.headers, r.content


def _get_json(http: HttpFn, url: str, headers: Mapping[str, str]
              ) -> Tuple[int, Any, str]:
    """(status, parsed body or None, error text). Never raises."""
    try:
        status, _h, body = http("GET", url, headers)
    except Exception as exc:  # noqa: BLE001 -- the transport error IS the answer
        return 0, None, f"unreachable: {type(exc).__name__}"
    try:
        data = json.loads(body or b"null")
    except ValueError:
        return status, None, f"HTTP {status}: not JSON"
    if status >= 400:
        return status, data, f"HTTP {status}"
    return status, data, ""


def _ws_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("workspaces") or payload.get("items") or payload.get("data") or []
    else:
        rows = []
    return [w for w in rows if isinstance(w, dict) and _ws_id(w)]


def _ws_id(w: Mapping[str, Any]) -> str:
    return str(w.get("id") or w.get("workspace_id") or w.get("slug") or "")


def _ws_name(w: Mapping[str, Any]) -> str:
    return str(w.get("name") or w.get("display_name") or w.get("slug") or _ws_id(w))


def kind_of(row: Mapping[str, Any]) -> str:
    """agent | skill | tool | app, from a catalog row (no endpoint names the kind)."""
    explicit = str(row.get("kind") or row.get("type") or "").lower()
    if explicit in ("agent", "skill", "tool", "app"):
        return explicit
    if row.get("app_manifest_id"):
        return "app"
    if int(row.get("tool_count") or 0) > 0 or row.get("has_mcp_server"):
        return "tool"
    if row.get("skills"):
        return "skill"
    return "agent"


_PACK_TYPE_KIND = {"agent_pack": "agent", "skill_pack": "skill", "tool_pack": "tool",
                   "app": "app", "app_pack": "app"}


def _version_key(v: str) -> Tuple[int, int, int]:
    from adk.pack_registry import _parse_semver

    return _parse_semver(v)


def is_newer(remote: Optional[str], local: Optional[str]) -> Optional[bool]:
    """True/False when both versions are known; None when either is unknown."""
    if not remote or not local or remote == "unknown" or local == "unknown":
        return None
    try:
        return _version_key(remote) > _version_key(local)
    except ValueError:
        return remote != local


# ── remote ────────────────────────────────────────────────────────────────────


def fetch_remote(http: HttpFn, portal: str, token: str,
                 workspace: str = "", genesis: Optional[str] = None) -> RemoteState:
    """Ask the platform what this account's workspace holds. Never raises."""
    st = RemoteState(ok=False)
    g = portal + (genesis_prefix() if genesis is None else genesis)
    auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    status, data, err = _get_json(http, portal + WORKSPACES_PATH, auth)
    workspaces = _ws_list(data) if not err else []
    if err:
        st.errors.append(f"workspaces: {err}")
    st.workspaces = [{"id": _ws_id(w), "name": _ws_name(w)} for w in workspaces]

    status, bundle, err = _get_json(http, g + BUNDLE_PATH, auth)
    bundle_ws = ""
    if not err and isinstance(bundle, dict):
        st.role = bundle.get("role") if bundle.get("role") in ("owner", "user") else None
        bundle_ws = str((bundle.get("identity") or {}).get("workspace_id") or "")
    elif err:
        st.errors.append(f"link bundle: {err}")

    # Scope: explicit choice, then the bundle's verified workspace, then the only
    # one. Several and no choice = the account default scope (no header), the
    # same "never silently pick" rule awconnect's chooseWorkspace follows.
    ids = [w["id"] for w in st.workspaces]
    if workspace:
        if st.workspaces and workspace not in ids:
            st.errors.append(f"workspace {workspace!r} is not one of yours: {ids}")
            return st
        st.workspace_id, st.workspace_source = workspace, "flag"
    elif bundle_ws:
        st.workspace_id, st.workspace_source = bundle_ws, "bundle"
    elif len(ids) == 1:
        st.workspace_id, st.workspace_source = ids[0], "only"
    else:
        st.workspace_source = "account"
    for w in st.workspaces:
        if w["id"] == st.workspace_id:
            st.workspace_name = w["name"]

    scoped = dict(auth)
    if st.workspace_id:
        scoped["X-Workspace-ID"] = st.workspace_id
    status, lic, err = _get_json(http, g + LICENSES_PATH, scoped)
    rows = lic.get("licenses") if isinstance(lic, dict) else None
    if err or not isinstance(rows, list):
        st.errors.append(f"entitlements: {err or 'not a license list'}")
        return st
    tl = lic.get("tenant_listed") if isinstance(lic, dict) else None
    st.tenant_listed = tl if isinstance(tl, bool) else None
    if st.tenant_listed is not True:
        st.errors.append("entitlements: tenant packs not confirmed by the server "
                         "(company packs are kept, never removed, this run)")
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "active":
            continue
        pid = str(row.get("listing_id") or "")
        if not _PACK_ID_RE.match(pid) or pid in st.entitled:
            continue
        ptype = str(row.get("pack_type") or "")
        st.entitled[pid] = {"source": str(row.get("source") or "license"),
                            "tenant_id": str(row.get("tenant_id") or ""),
                            "name": pid, "kind": _PACK_TYPE_KIND.get(ptype, "agent"),
                            "kind_known": ptype in _PACK_TYPE_KIND,
                            "version": str(row.get("version") or "") or None}

    # The bridge refuses an anonymous catalog read (401), so it carries the bearer.
    status, cat, err = _get_json(http, g + CATALOG_PATH, auth)
    packs = cat.get("packs") if isinstance(cat, dict) else None
    if err or not isinstance(packs, list):
        st.errors.append(f"catalog: {err or 'not a pack list'} (versions unknown)")
    else:
        st.catalog_ok = True
        for row in packs:
            if isinstance(row, dict) and str(row.get("id") or "") in st.entitled:
                e = st.entitled[str(row["id"])]
                e["name"] = str(row.get("name") or e["name"])
                if not e.get("kind_known"):
                    e["kind"] = kind_of(row)
                # license/mine's version is from the catalog downloads come
                # from; this one is only a fallback for an older Genesis.
                e["version"] = e.get("version") or str(row.get("version") or "") or None

    if st.workspace_id:
        url = portal + AGENTS_PATH.format(workspace_id=st.workspace_id)
        status, ag, err = _get_json(http, url, scoped)
        if not err:
            rows = ag if isinstance(ag, list) else (
                (ag or {}).get("agents") or (ag or {}).get("items") or [])
            for a in rows if isinstance(rows, list) else []:
                if isinstance(a, dict) and (a.get("id") or a.get("name")):
                    aid = str(a.get("id") or a.get("name"))
                    st.agents.append({"id": aid,
                                      "name": str(a.get("display_name") or a.get("name") or aid)})
    st.ok = True
    return st


# ── local ─────────────────────────────────────────────────────────────────────


def load_ledger(root: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads((root / LEDGER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    packs = data.get("packs") if isinstance(data, dict) else None
    return {k: v for k, v in (packs or {}).items() if isinstance(v, dict)}


def save_ledger(root: Path, ledger: Mapping[str, Mapping[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (LEDGER_FILE + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "packs": dict(ledger)}, indent=2,
                              sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, root / LEDGER_FILE)


def _manifest_version(pack_dir: Path) -> Optional[str]:
    for name in _MANIFEST_NAMES:
        p = pack_dir / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            if name.endswith(".json"):
                v = (json.loads(text) or {}).get("version")
            else:
                m = re.search(r"^version:\s*['\"]?([^'\"\s#]+)", text, re.M)
                v = m.group(1) if m else None
        except (OSError, ValueError, AttributeError):
            continue
        if v:
            return str(v)
    return None


def scan_local(root: Path) -> Dict[str, Dict[str, Any]]:
    """id -> {version, managed, scope, source} for every pack dir under ``root``."""
    ledger = load_ledger(root)
    out: Dict[str, Dict[str, Any]] = {}
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or not any(d.iterdir()):
            continue
        rec = ledger.get(d.name)
        ent = None
        try:
            ent = json.loads((d / ENTITLEMENT_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ent = None
        if rec is not None:
            out[d.name] = {"version": rec.get("version") or _manifest_version(d),
                           "managed": True, "scope": str(rec.get("workspace") or ""),
                           "source": str(rec.get("source") or "sync")}
        elif isinstance(ent, dict):
            # Installed by the license-driven `adk pack sync` (account scope).
            out[d.name] = {"version": _manifest_version(d), "managed": True,
                           "scope": "", "source": str(ent.get("source") or "license")}
        else:
            out[d.name] = {"version": _manifest_version(d), "managed": False,
                           "scope": "", "source": "manual"}
    return out


# ── diff ──────────────────────────────────────────────────────────────────────


def compute_diff(remote: RemoteState, local: Mapping[str, Mapping[str, Any]],
                 allow_remove: bool = True) -> SyncPlan:
    """Pure: what to install, update, remove, leave alone. No I/O."""
    plan = SyncPlan()
    for pid in sorted(remote.entitled):
        e = remote.entitled[pid]
        item = PackItem(id=pid, name=e.get("name") or pid, kind=e.get("kind") or "agent",
                        remote_version=e.get("version"), source=e.get("source") or "")
        have = local.get(pid)
        if have is None:
            item.reason = "entitled, not on this machine"
            plan.install.append(item)
            continue
        item.local_version = have.get("version")
        newer = is_newer(item.remote_version, item.local_version)
        if newer:
            item.reason = f"{item.local_version} -> {item.remote_version}"
            plan.update.append(item)
        else:
            item.reason = ("up to date" if newer is False
                           else "present (version not comparable)")
            plan.unchanged.append(item)
    for pid in sorted(local):
        if pid in remote.entitled:
            continue
        have = local[pid]
        item = PackItem(id=pid, name=pid, local_version=have.get("version"),
                        source=str(have.get("source") or ""), kind="")
        if not have.get("managed"):
            item.reason = "installed by hand; sync never removes it"
            plan.unmanaged.append(item)
        elif str(have.get("scope") or "") != remote.workspace_id:
            item.reason = f"synced for workspace {have.get('scope') or 'account'!r}; kept"
            plan.unmanaged.append(item)
        elif not allow_remove:
            item.reason = "no longer entitled (kept: --no-remove)"
            plan.unchanged.append(item)
        elif item.source == "tenant" and remote.tenant_listed is not True:
            # Genesis answers 200 with purchased rows only when its catalog read
            # fails; absence from THAT list is not a revoked company pack.
            item.reason = "company pack absent but tenant list unconfirmed; kept"
            plan.unchanged.append(item)
        else:
            item.reason = "no longer entitled"
            plan.remove.append(item)
    return plan


# ── apply ─────────────────────────────────────────────────────────────────────


def _confined(root: Path, pack_id: str) -> Path:
    if not _PACK_ID_RE.match(pack_id or "") or pack_id in (".", ".."):
        raise ValueError(f"unsafe pack id {pack_id!r}")
    dest = (root / pack_id).resolve()
    base = root.resolve()
    if dest == base or base not in dest.parents:
        raise ValueError(f"{pack_id!r} escapes the pack root")
    return dest


def _install_one(http: HttpFn, base: str, token: str, workspace_id: str,
                 root: Path, item: PackItem, hooks: Tuple[Any, Any],
                 allow_unverified: bool = False,
                 ) -> Tuple[bool, str, Optional[str], str]:
    """Download -> verify -> extract -> swap. Returns (ok, detail, version, integrity).

    integrity: ``sha256``, ``signature``, ``sha256+signature`` or ``unverified``.
    An unverified download is refused unless ``allow_unverified``."""
    from adk.shell.plugins.builtins.packs import _safe_extract

    dest = _confined(root, item.id)
    headers = {"Authorization": f"Bearer {token}"}
    if workspace_id:
        headers["X-Workspace-ID"] = workspace_id
    try:
        status, rh, body = http("GET", base + DOWNLOAD_PATH.format(pack_id=item.id),
                                headers)
    except Exception as exc:  # noqa: BLE001
        return False, f"download unreachable: {type(exc).__name__}", None, "none"
    if status in (402, 403):
        return False, f"HTTP {status}: no valid license for this pack", None, "none"
    if status == 404:
        return False, "HTTP 404: no downloadable artifact", None, "none"
    if status != 200 or not body:
        return False, f"HTTP {status}: download failed", None, "none"
    lower = {str(k).lower(): str(v) for k, v in dict(rh).items()}
    want = lower.get("x-pack-sha256", "").strip().lower()
    checks: List[str] = []
    if want:
        if hashlib.sha256(body).hexdigest() != want:
            return False, "sha256 mismatch: refused", None, "failed"
        checks.append("sha256")
    sig = lower.get("x-aither-pack-signature", "").strip() or None
    try:
        from adk.pack_verifier import verify_pack_tarball
    except ImportError:
        verify_pack_tarball = None  # type: ignore[assignment]
    if verify_pack_tarball is not None:
        ok, msg = verify_pack_tarball(body, sig)
        if not ok:
            return False, f"signature check failed: {msg}", None, "failed"
        if sig:
            checks.append("signature")
    integrity = "+".join(checks) or "unverified"
    if integrity == "unverified" and not allow_unverified:
        # Genesis always sends X-Pack-SHA256; its absence means something on the
        # path stripped it (the portal bridge did, before 2026-10-04).
        return (False, "no X-Pack-SHA256 or pack signature on the download: refused "
                "(--allow-unverified to install anyway)", None, integrity)
    version = lower.get("x-pack-version") or item.remote_version
    mint, revoke = hooks
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=str(root), prefix=".sync-") as tmp:
            stage = Path(tmp) / "x"
            stage.mkdir()
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
                _safe_extract(tar, str(stage))
            kids = list(stage.iterdir())
            src = kids[0] if len(kids) == 1 and kids[0].is_dir() else stage
            if dest.exists():
                if revoke is not None:
                    revoke(item.id)
                shutil.rmtree(dest)
            shutil.move(str(src), str(dest))
    except (tarfile.TarError, ValueError, OSError) as exc:
        return False, f"extract failed: {type(exc).__name__}: {exc}", None, integrity
    if mint is not None:
        mint(item.id)
    return True, f"installed {version or 'unknown version'} ({integrity})", version, integrity


def _remove_one(root: Path, item: PackItem, hooks: Tuple[Any, Any]) -> Tuple[bool, str]:
    dest = _confined(root, item.id)
    if not dest.exists():
        return True, "already absent"
    _mint, revoke = hooks
    if revoke is not None:  # revoke while its metadata is still readable
        revoke(item.id)
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        return False, f"remove failed: {exc}"
    return True, "removed"


def _default_activate() -> Dict[str, Any]:
    """Hot-reload the running agent server, exactly as `/packs activate` does."""
    from adk.shell.plugins.builtins.packs import PacksPlugin

    p = PacksPlugin()
    return p._reload_packs_on_server(p._get_adk_server_url())


def apply_plan(plan: SyncPlan, *, http: HttpFn, portal: str, token: str,
               workspace_id: str, root: Path,
               hooks: Optional[Tuple[Any, Any]] = None,
               activate: Optional[Callable[[], Dict[str, Any]]] = None,
               entitled: Optional[Mapping[str, Mapping[str, Any]]] = None,
               allow_unverified: bool = False,
               ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Execute a plan. ``portal`` is the Genesis base downloads go to (portal +
    bridge prefix). Returns (per-pack results, activation result or None)."""
    if hooks is None:
        hooks = (None, None)
        if root.resolve() == default_root().resolve():
            # Per-install credentials live beside the default root only.
            from adk.shell.plugins.builtins.packs import _credential_hooks

            hooks = _credential_hooks()
    ledger = load_ledger(root)
    results: List[Dict[str, Any]] = []
    changed = False
    #: removed/updated packs: the running agent's reload only ADDS tools, so
    #: their old tools and persona fragments stay live until it restarts.
    pending: List[str] = []
    for action, items in (("install", plan.install), ("update", plan.update)):
        for item in items:
            try:
                ok, detail, version, integrity = _install_one(
                    http, portal, token, workspace_id, root, item, hooks,
                    allow_unverified=allow_unverified)
            except ValueError as exc:
                ok, detail, version, integrity = False, str(exc), None, "none"
            results.append({"id": item.id, "action": action, "ok": ok, "detail": detail,
                            "integrity": integrity})
            if ok:
                changed = True
                if action == "update":
                    pending.append(item.id)
                ent = (entitled or {}).get(item.id) or {}
                ledger[item.id] = {"version": version, "workspace": workspace_id,
                                   "integrity": integrity,
                                   "source": item.source or "license", "kind": item.kind,
                                   "synced_at": int(time.time())}
                try:  # keep `adk up`'s company-persona provenance working
                    from adk.shell.plugins.builtins.packs import record_entitlement

                    record_entitlement(root / item.id,
                                       {"listing_id": item.id, "source": item.source,
                                        "tenant_id": ent.get("tenant_id", "")})
                except ImportError:
                    pass
    for item in plan.remove:
        try:
            ok, detail = _remove_one(root, item, hooks)
        except ValueError as exc:
            ok, detail = False, str(exc)
        results.append({"id": item.id, "action": "remove", "ok": ok, "detail": detail})
        if ok:
            changed = True
            ledger.pop(item.id, None)
            if detail != "already absent":
                pending.append(item.id)
    if changed:
        save_ledger(root, ledger)
    activation: Optional[Dict[str, Any]] = None
    if changed and activate is not None:
        try:
            activation = dict(activate() or {})
        except Exception as exc:  # noqa: BLE001 -- packs apply on next agent start
            activation = {"status": "unavailable", "message": f"{type(exc).__name__}"}
    if activation is not None:
        activation["pending_restart"] = sorted(pending)
        if pending and activation.get("status") == "ok":
            # The reload picked up NEW packs; it cannot unload old ones.
            activation["status"] = "partial"
            activation["message"] = (
                "new packs loaded; removed/updated packs take effect on the next "
                "agent start")
    return results, activation


# ── orchestration + CLI ───────────────────────────────────────────────────────


def run(*, dry_run: bool = False, workspace: str = "", allow_remove: bool = True,
        do_activate: bool = True, http: Optional[HttpFn] = None,
        portal: Optional[str] = None, token: Optional[str] = None,
        root: Optional[Path] = None, genesis: Optional[str] = None,
        hooks: Optional[Tuple[Any, Any]] = None,
        activate: Optional[Callable[[], Dict[str, Any]]] = None,
        allow_unverified: bool = False) -> Dict[str, Any]:
    """One sync. Returns the JSON-able report (``ok`` false on any failure)."""
    http = http or _httpx_fetch
    portal = (portal or portal_url()).rstrip("/")
    root = root or default_root()
    kind = "argument"
    if token is None:
        token, kind = resolve_token()
    report: Dict[str, Any] = {"ok": False, "dry_run": dry_run, "portal": portal,
                              "root": str(root), "credential": kind or None}
    if not token:
        report["errors"] = ["not signed in: run `adk link start` (or `adk login`)"]
        return report
    g = genesis_prefix() if genesis is None else genesis.rstrip("/")
    remote = fetch_remote(http, portal, token, workspace, genesis=g)
    report.update({
        "workspace": {"id": remote.workspace_id or None, "name": remote.workspace_name or None,
                      "source": remote.workspace_source or None},
        "workspaces": remote.workspaces, "role": remote.role, "agents": remote.agents,
        "catalog": remote.catalog_ok, "tenant_listed": remote.tenant_listed,
        "errors": list(remote.errors),
    })
    if not remote.ok:
        return report
    plan = compute_diff(remote, scan_local(root), allow_remove=allow_remove)
    report["plan"] = plan.to_dict()
    report["counts"] = plan.counts()
    if dry_run:
        report["ok"] = True
        return report
    act = activate if activate is not None else (_default_activate if do_activate else None)
    results, activation = apply_plan(plan, http=http, portal=portal + g, token=token,
                                     workspace_id=remote.workspace_id, root=root,
                                     hooks=hooks, activate=act, entitled=remote.entitled,
                                     allow_unverified=allow_unverified)
    report["results"] = results
    report["activation"] = activation
    report["ok"] = all(r["ok"] for r in results)
    return report


def render(report: Mapping[str, Any]) -> str:
    lines: List[str] = []
    ws = report.get("workspace") or {}
    scope = ws.get("name") or ws.get("id") or "account default"
    mode = "dry run" if report.get("dry_run") else "apply"
    lines.append(f"pack sync ({mode}) -- workspace: {scope}")
    plan = report.get("plan") or {}
    marks = {"install": "+", "update": "^", "remove": "-", "unchanged": "=",
             "unmanaged": "?"}
    for key, mark in marks.items():
        for it in plan.get(key, []):
            kind = f"[{it['kind']}] " if it.get("kind") else ""
            lines.append(f"  {mark} {kind}{it['id']}  {it.get('reason', '')}")
    if plan:
        c = report.get("counts") or {}
        lines.append("  " + " · ".join(f"{k} {c.get(k, 0)}" for k in marks))
    for r in report.get("results") or []:
        flag = "ok" if r["ok"] else "FAILED"
        lines.append(f"  {r['action']} {r['id']}: {flag} -- {r['detail']}")
    act = report.get("activation")
    if act:
        lines.append(f"  agent reload: {act.get('status')} {act.get('message', '')}".rstrip())
        if act.get("pending_restart"):
            lines.append("  restart the agent to unload: "
                         + ", ".join(act["pending_restart"]))
    for e in report.get("errors") or []:
        lines.append(f"  ! {e}")
    return "\n".join(lines)


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dry-run", "-n", dest="dry_run", action="store_true",
                   help="Print the diff; change nothing")
    p.add_argument("--json", dest="json_output", action="store_true",
                   help="Machine-readable report (for awdesk / awconnect)")
    p.add_argument("--workspace", "-w", default="",
                   help="Workspace id to sync (default: the linked one, or your only one)")
    p.add_argument("--no-remove", dest="no_remove", action="store_true",
                   help="Never remove a pack whose entitlement ended")
    p.add_argument("--no-activate", dest="no_activate", action="store_true",
                   help="Do not hot-reload the running agent server afterwards")
    p.add_argument("--allow-unverified", dest="allow_unverified", action="store_true",
                   help="Install a download that carried no sha256 or signature")


def main(args: Any) -> int:
    report = run(dry_run=bool(getattr(args, "dry_run", False)),
                 workspace=str(getattr(args, "workspace", "") or ""),
                 allow_remove=not getattr(args, "no_remove", False),
                 do_activate=not getattr(args, "no_activate", False),
                 allow_unverified=bool(getattr(args, "allow_unverified", False)))
    if getattr(args, "json_output", False):
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    _ap = argparse.ArgumentParser(prog="adk sync packs")
    add_arguments(_ap)
    sys.exit(main(_ap.parse_args()))
