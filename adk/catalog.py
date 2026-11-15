"""The unified catalog, for people and for agents (``adk catalog ...``).

One reader over the SAME files the platform's ``lib/catalog`` layer reads, so the
adk CLI, the discovery index and the web door describe one inventory. awdk is a
standalone published package and cannot import AitherOS, so this module re-reads
the source files itself. That is deliberate and guarded: ``dev/tests/
test_catalog_parity.py`` compares the ids this reader derives from the shared
config files against ``lib.catalog.load_unified_catalog()`` and fails on drift.

Sources (every one degrades to empty; a missing tree is never fatal):

    config/packs_catalog.yaml    -> agent_pack | skill_pack | tool_pack | bundle
    config/app_catalog.yaml      -> application
    config/mcp-registry.json     -> mcp_pack
    config/extension_catalog.yaml-> extension | plugin
    config/identities/*.yaml     -> agent
    Library/packs/*/pack/*/      -> brain_pack
    Library/packs/*/company.yaml -> company
    awpack/packs/*/pack.yaml     -> awpack

``adk catalog find | add | remove`` (what ``aither find | add | remove`` runs) read
those PLUS the sources in :mod:`adk.catalog_person` -- aw* bricks and stacks, integrations,
shop products, tool packs, skills, the installer's own parts -- as one flat row.

Every entry carries the person-facing projection the web and MCP doors use:
``what_it_does`` (<=140 chars, no jargon), ``audience``, ``install_targets``,
``includes``, ``trust``, ``requires``, ``status`` and ``blockers``. ``status`` is
``available`` | ``preview`` | ``blocked`` | ``internal``; a ``preview``/``blocked``
entry names WHY in ``blockers`` instead of failing at install time.

Two curated starter bundles live in ``adk/data/catalog_bundles.yaml`` as the
offline snapshot (the same pattern as ``adk/data/packs_catalog.json``). An entry
with the same id in the on-disk ``config/packs_catalog.yaml`` WINS, so the
platform file stays the single source once it carries them.

Install is honest about provenance:
  * first-party + free  -> installed by DIGEST (sha256 of the pack tree), no
    network, no signature needed; the digest is reported so a caller can pin it.
  * anything paid       -> a signature is required; absent/invalid is exit 1.

Exit codes: 0 ok, 1 refused / install failed, 2 could not judge (no catalog tree,
bad arguments).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

__all__ = [
    "PERSON_FIELDS",
    "catalog_stats",
    "cli_main",
    "get_entry",
    "install_entry",
    "load_catalog",
    "search",
    "starter_bundles",
]

PERSON_FIELDS = ("what_it_does", "audience", "install_targets", "includes",
                 "trust", "requires", "status", "blockers")

AUDIENCES = ("everyone", "patty-everyday", "jason-business", "developer", "owner")
INSTALL_TARGETS = ("my_claude", "workspace", "awdk_local")

#: Aither plan tier -> the discovery scope ladder (scope.TIER_ORDER). Unknown
#: plans map to the widest gate so a card is not silently granted.
_SCOPE_TIER = {
    "free": "free",
    "starter": "builder",
    "developer": "builder",
    "pro": "professional",
    "team": "professional",
    "business": "enterprise",
    "enterprise": "enterprise",
}

#: packs_catalog `type` -> canonical kind.
_PACK_TYPE_TO_KIND = {
    "agent_pack": "agent_pack",
    "skill_pack": "skill_pack",
    "tool_pack": "tool_pack",
    "service_pack": "application",
    "bundle": "bundle",
}


# ─────────────────────────────────────────────────────────────────────────────
# Location
# ─────────────────────────────────────────────────────────────────────────────

def _repo_root() -> Optional[Path]:
    """The checkout that contains this awdk tree, when awdk was not pip-installed."""
    p = Path(__file__).resolve().parents[2]
    if (p / "AitherOS").is_dir() or (p / "awpack").is_dir():
        return p
    return None


def _candidates() -> List[Path]:
    out: List[Path] = []
    env = os.environ.get("AITHER_CATALOG_ROOT", "").strip()
    if env:
        out.append(Path(env).expanduser())
    repo = _repo_root()
    if repo is not None:
        out += [repo / "AitherOS", repo]
    out += [Path.cwd() / "AitherOS", Path.cwd(), Path("/app/AitherOS")]
    seen: set = set()
    unique: List[Path] = []
    for root in out:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def find(relpath: str) -> Optional[Path]:
    """First existing path for ``relpath`` across the candidate roots."""
    for root in _candidates():
        p = root / relpath
        if p.exists():
            return p
    return None


def _pack_roots(override: Optional[Iterable[Path]] = None) -> List[Path]:
    """Pack registry roots (env, user, monorepo, container) + the checkout tree.

    ``override`` is used by callers that must read ONE registry -- the parity
    guard pins the repo tree so a locally installed pack under ``~/.aither``
    cannot make the two readers disagree on a developer's machine.
    """
    if override is not None:
        return [Path(r) for r in override if Path(r).is_dir()]
    roots: List[Path] = []
    try:
        from adk.pack_install import pack_roots
    except Exception:  # noqa: BLE001 - the reader must not need the installer
        pack_roots = None  # type: ignore[assignment]
    if pack_roots is not None:
        roots += list(pack_roots())
    repo = _repo_root()
    if repo is not None:
        roots.append(repo / "AitherOS" / "Library" / "packs")
    roots += [c / "Library" / "packs" for c in _candidates()]
    seen: set = set()
    unique: List[Path] = []
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return [r for r in unique if r.is_dir()]


# ─────────────────────────────────────────────────────────────────────────────
# Small readers
# ─────────────────────────────────────────────────────────────────────────────

def _load_yaml(path: Optional[Path]) -> Any:
    if path is None or not path.is_file():
        return {}
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is an adk dependency
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    except Exception:  # noqa: BLE001 - one bad file must not sink the catalog
        return {}


def _load_json(path: Optional[Path]) -> Any:
    if path is None or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def _clamp(text: Any, limit: int = 140) -> str:
    """Collapse whitespace and guarantee the result is <= ``limit`` characters."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    room = max(1, limit - 3)
    cut = s[:room].rsplit(" ", 1)[0] or s[:room]
    return (cut + "...")[:limit]


def _first_sentence(text: Any, limit: int = 140) -> str:
    s = " ".join(str(text or "").split())
    for sep in (". ", "; ", " -- ", " — "):
        if sep in s:
            s = s.split(sep, 1)[0].rstrip(".") + "."
            break
    return _clamp(s, limit)


def _pricing(tier: str, amount: Any = None) -> Dict[str, Any]:
    t = (tier or "free").strip().lower()
    if t in ("", "free"):
        return {"tier": "free", "amount_cents": None}
    cents = None
    try:
        if amount is not None:
            cents = int(round(float(amount) * 100))
    except (TypeError, ValueError):
        cents = None
    if cents is None and tier and tier != "free":
        cents = None
    return {"tier": "subscription", "amount_cents": cents, "currency": "USD",
            "billing_period": "monthly"}


def _trust(source: str, provenance: Any = None, signed: bool = False,
           license_: str = "") -> Dict[str, Any]:
    return {"source": source,
            "provenance": provenance if isinstance(provenance, dict) else {},
            "signed": bool(signed),
            "license": str(license_ or "")}


def _requires(tier: str = "", runtime: Any = "", credentials: Any = None,
              capabilities: Any = None) -> Dict[str, Any]:
    min_node = ""
    rt = str(runtime or "").strip()
    if rt.lower().startswith("awdk"):
        min_node = rt.split(">=", 1)[-1].strip() if ">=" in rt else rt
    creds: List[str] = []
    if isinstance(credentials, dict):
        creds = sorted(str(k) for k in credentials)
    elif isinstance(credentials, (list, tuple)):
        creds = [str(c) for c in credentials]
    return {"capabilities": [str(c) for c in (capabilities or []) if c],
            "min_plan": "" if (tier or "free").lower() == "free" else tier,
            "min_node_version": min_node,
            "credentials": creds}


def _entry(**fields: Any) -> Dict[str, Any]:
    """Build an entry with every person field present (defaults, never absent)."""
    out: Dict[str, Any] = {
        "id": "",
        "kind": "",
        "name": "",
        "summary": "",
        "category": "general",
        "source": "first_party",
        "version": "1.0.0",
        "tags": [],
        "pricing": {"tier": "free", "amount_cents": None},
        "install": {"method": "manual"},
        "icon": None,
        "what_it_does": "",
        "audience": ["everyone"],
        "install_targets": ["workspace"],
        "includes": [],
        "trust": _trust("first_party"),
        "requires": _requires(),
        "status": "available",
        "blockers": [],
    }
    out.update(fields)
    if not out.get("what_it_does"):
        out["what_it_does"] = _first_sentence(out.get("summary"))
    if not out["what_it_does"]:
        # A person-facing sentence is REQUIRED, so an entry with no description
        # gets an honest generic one rather than an empty field (an empty
        # `what_it_does` renders as a blank card in every door).
        human = out["kind"].replace("_", " ")
        out["what_it_does"] = _clamp(
            f"{out['name']}: a {human} you can add to your workspace.")
    if out.get("icon") is None:
        out.pop("icon", None)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Normalizers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_pack(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict) or not entry.get("id"):
        return None
    raw_type = str(entry.get("type") or "agent_pack")
    kind = _PACK_TYPE_TO_KIND.get(raw_type, "agent_pack")
    tier = str(entry.get("tier") or "free")
    pricing = _pricing(tier, entry.get("price_monthly_usd"))
    if pricing.get("amount_cents") is None and entry.get("subscription_cents"):
        try:
            pricing["amount_cents"] = int(entry["subscription_cents"])
        except (TypeError, ValueError):
            pricing["amount_cents"] = None  # unparseable price stays unpriced

    includes: List[Dict[str, str]] = []
    for member in entry.get("includes") or []:
        if isinstance(member, str) and member:
            includes.append({"kind": "", "id": member, "name": member})
    agents = [str(a.get("identity", a)) if isinstance(a, dict) else str(a)
              for a in entry.get("agents") or [] if a]
    skills = [str(s) for s in entry.get("skills") or [] if s]

    status = str(entry.get("status") or "available").lower()
    if status not in ("available", "preview", "blocked", "internal"):
        status = "available"
    blockers: List[str] = []
    # A skill_pack that declares skills must resolve them; a pack that names
    # files which are not there is sold hollow, so it becomes visibly blocked.
    for sf in entry.get("skill_files") or []:
        if not find(str(sf)) and not (_repo_root() and (_repo_root() / str(sf)).exists()):
            status = "blocked"
            blockers.append(f"declared skill file not found: {sf}")

    # An explicit `audience:` in the source file wins (that is how a curated
    # bundle reaches Patty's filter); only an unspecified pack is inferred.
    declared = entry.get("audience")
    if entry.get("allowed_tenants") or entry.get("unlisted") is True:
        # One customer's pack (tenant-restricted or unlisted in packs_catalog.yaml):
        # never "for everyone". Genesis withholds the listing from other callers
        # (lib.catalog.schema.item_visible); this local reader, which only ever
        # runs beside the monorepo, labels it the same way.
        audience = ["owner"]
    elif isinstance(declared, list) and declared:
        audience = [str(a) for a in declared]
    elif kind == "bundle":
        audience = ["everyone"]
    elif str(entry.get("category") or "") in ("dev", "infrastructure", "creative"):
        audience = ["developer"]
    else:
        audience = ["everyone"]

    return _entry(
        id=str(entry["id"]),
        kind=kind,
        name=str(entry.get("name") or entry["id"]),
        summary=str(entry.get("description") or ""),
        category=str(entry.get("category") or "general"),
        source="first_party",
        version=str(entry.get("version") or "1.0.0"),
        tags=[str(t) for t in entry.get("tags") or []],
        pricing=pricing,
        install={"method": "adk", "command": f"adk catalog install {entry['id']}"},
        what_it_does=_first_sentence(entry.get("description")),
        audience=audience,
        install_targets=["workspace", "my_claude"] if kind in ("agent_pack", "bundle")
                        else ["workspace"],
        includes=includes,
        trust=_trust("first_party", entry.get("branding"), signed=False),
        requires=_requires(tier, capabilities=entry.get("requires_capabilities")),
        status=status,
        blockers=blockers,
        agents=agents or None,
        skills=skills or None,
        tools=[str(m) for m in entry.get("mcp_modules") or []] or None,
    )


def _normalize_app(app_id: str, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    src = entry.get("source") or {}
    url = None
    if isinstance(src, dict) and src.get("repo"):
        url = f"https://github.com/{src['repo']}"
        if src.get("path"):
            url += f"/tree/{src.get('branch', 'main')}/{src['path']}"
    runtime = entry.get("runtime") or {}
    return _entry(
        id=f"app:{app_id}",
        kind="application",
        name=str(entry.get("display_name") or entry.get("name") or app_id),
        summary=str(entry.get("description") or ""),
        category="examples",
        source="community",
        version=str(entry.get("version") or "1.0.0"),
        tags=[str(t) for t in entry.get("tags") or []],
        pricing={"tier": "free", "amount_cents": None},
        install={"method": "git", "url": url} if url else {"method": "git"},
        what_it_does=_first_sentence(entry.get("description")),
        audience=["developer"],
        install_targets=["awdk_local"],
        trust=_trust("community", {"repo": src.get("repo") if isinstance(src, dict) else ""}),
        requires=_requires("", (runtime or {}).get("type")),
    )


def _normalize_mcp(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict) or not entry.get("id"):
        return None
    transport = entry.get("transport") or {}
    cats = [str(c) for c in entry.get("categories") or [] if c]
    url = transport.get("url") if isinstance(transport, dict) else None
    return _entry(
        id=f"mcp:{entry['id']}",
        kind="mcp_pack",
        name=str(entry.get("name") or entry["id"]),
        summary=str(entry.get("description") or ""),
        category=cats[0] if cats else "tools",
        source="first_party",
        version=str(entry.get("version") or "1.0.0"),
        tags=cats,
        pricing={"tier": "free", "amount_cents": None},
        install={"method": "mcp", "url": url} if url else {"method": "mcp"},
        what_it_does=_first_sentence(entry.get("description")),
        audience=["developer"],
        install_targets=["my_claude", "workspace"],
        trust=_trust("first_party"),
        requires=_requires("", credentials=[k for k in (entry.get("auth") or {})]),
        mcp_server_url=url,
    )


def _normalize_extension(entry: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    ident = entry.get("id") or entry.get("name")
    if not ident:
        return None
    return _entry(
        # The platform catalog keys extensions/plugins by their bare id (no
        # kind prefix); match it so the two readers describe the same rows.
        id=str(ident),
        kind=kind,
        name=str(entry.get("name") or ident),
        summary=str(entry.get("description") or ""),
        category=str(entry.get("category") or "general"),
        source="first_party",
        version=str(entry.get("version") or "1.0.0"),
        tags=[str(t) for t in entry.get("tags") or []],
        pricing={"tier": "free", "amount_cents": None},
        install={"method": str(entry.get("install_method") or "manual")},
        what_it_does=_first_sentence(entry.get("description")),
        audience=["developer"],
        install_targets=["my_claude", "workspace"],
        trust=_trust("first_party"),
        requires=_requires(""),
    )


def _normalize_brain_pack(manifest: Dict[str, Any], path: Path,
                          company: str) -> Optional[Dict[str, Any]]:
    if not isinstance(manifest, dict):
        return None
    name = str(manifest.get("name") or manifest.get("identity") or path.parent.name)
    desc = str(manifest.get("description") or manifest.get("summary") or "")
    status = str(manifest.get("status") or "available").lower()
    if status not in ("available", "preview", "blocked", "internal"):
        status = "available"
    return _entry(
        id=f"brain:{company}/{path.parent.name}",
        kind="brain_pack",
        name=name,
        summary=desc,
        category=str(manifest.get("category") or "agents"),
        source="first_party",
        version=str(manifest.get("version") or "1.0.0"),
        tags=[str(t) for t in manifest.get("tags") or []],
        pricing={"tier": "free", "amount_cents": None},
        install={"method": "adk", "command": f"adk catalog install {path.parent.name}"},
        what_it_does=_first_sentence(desc),
        audience=["everyone"],
        install_targets=["workspace"],
        includes=[{"kind": "skill", "id": str(s), "name": str(s)}
                  for s in manifest.get("skills") or []],
        trust=_trust("first_party", {"company": company, "path": str(path)}),
        requires=_requires("", capabilities=manifest.get("capabilities")),
        status=status,
        blockers=[str(b) for b in manifest.get("blockers") or []],
    )


def _normalize_company(manifest: Dict[str, Any], path: Path) -> Optional[Dict[str, Any]]:
    if not isinstance(manifest, dict):
        return None
    slug = str(manifest.get("slug") or path.parent.name)
    name = str(manifest.get("name") or slug)
    brand = manifest.get("brand") or {}
    desc = str(brand.get("tagline") if isinstance(brand, dict) else "") or f"{name} company brain"
    pack_dir = path.parent / "pack"
    roster = sorted(p.name for p in pack_dir.iterdir()
                    if p.is_dir() and (p / "brain_pack.yaml").is_file()) \
        if pack_dir.is_dir() else []
    return _entry(
        id=f"company:{slug}",
        kind="company",
        name=name,
        summary=desc,
        category="company",
        source="first_party",
        version=str(manifest.get("version") or "1.0.0"),
        tags=["company", slug],
        pricing={"tier": "free", "amount_cents": None},
        install={"method": "adk", "command": f"adk catalog install company:{slug}"},
        what_it_does=_first_sentence(desc),
        audience=["owner"],
        install_targets=["workspace"],
        includes=[{"kind": "brain_pack", "id": f"brain:{slug}/{a}", "name": a}
                  for a in roster],
        trust=_trust("first_party", {"path": str(path)}),
        requires=_requires(""),
    )


def _normalize_awpack(manifest: Dict[str, Any], path: Path) -> Optional[Dict[str, Any]]:
    if not isinstance(manifest, dict):
        return None
    pack_id = str(manifest.get("id") or path.parent.name)
    status = str(manifest.get("status") or "available").lower()
    blockers: List[str] = []
    reason = str(manifest.get("reason") or "").strip()
    if status == "preview" and reason:
        blockers.append(reason)
    elif status not in ("available", "preview", "blocked", "internal"):
        status = "available"
    desc = str(manifest.get("summary") or manifest.get("description") or "")
    lic = str(manifest.get("licence") or manifest.get("license") or "")
    requires = manifest.get("needs") or []
    return _entry(
        id=f"awpack:{pack_id}",
        kind="awpack",
        name=str(manifest.get("name") or pack_id),
        summary=desc,
        category="awpack",
        source="first_party",
        version=str(manifest.get("version") or "1.0.0"),
        tags=[str(t) for t in manifest.get("tags") or []],
        pricing={"tier": "free", "amount_cents": None},
        install={"method": "awpack", "command": f"awpack install {pack_id}"},
        what_it_does=_first_sentence(desc),
        audience=["developer"],
        install_targets=["awdk_local"],
        includes=[{"kind": "awpack", "id": str(n), "name": str(n)} for n in requires],
        trust=_trust("first_party", {"path": str(path)}, license_=lic),
        requires=_requires("", manifest.get("runtime"),
                           capabilities=manifest.get("tools")),
        status=status,
        blockers=blockers,
    )


def _normalize_agent_identity(manifest: Dict[str, Any], path: Path) -> Optional[Dict[str, Any]]:
    if not isinstance(manifest, dict):
        return None
    ident = str(manifest.get("id") or manifest.get("name") or path.stem)
    desc = str(manifest.get("description") or manifest.get("role") or "")
    return _entry(
        id=f"agent:{ident}",
        kind="agent",
        name=str(manifest.get("display_name") or ident),
        summary=desc,
        category=str(manifest.get("category") or "agents"),
        source="first_party",
        version=str(manifest.get("version") or "1.0.0"),
        tags=[str(t) for t in manifest.get("tags") or []],
        pricing={"tier": "free", "amount_cents": None},
        install={"method": "adk", "command": f"adk catalog install agent:{ident}"},
        what_it_does=_first_sentence(desc),
        audience=["everyone"],
        install_targets=["workspace", "my_claude"],
        includes=[{"kind": "skill", "id": str(s), "name": str(s)}
                  for s in manifest.get("skills") or []],
        trust=_trust("first_party", {"path": str(path)}),
        requires=_requires("", capabilities=manifest.get("capabilities")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bundles (curated starter sets)
# ─────────────────────────────────────────────────────────────────────────────

def starter_bundles() -> List[Dict[str, Any]]:
    """Curated bundles: the on-disk catalog when it carries them, else the snapshot.

    ``adk/data/catalog_bundles.yaml`` is the offline snapshot (same pattern as
    ``adk/data/packs_catalog.json``). A bundle id that also appears in
    ``config/packs_catalog.yaml`` is taken from the file and the snapshot copy for
    that id is dropped, so the platform file stays authoritative.
    """
    from_file: Dict[str, Dict[str, Any]] = {}
    doc = _load_yaml(find("config/packs_catalog.yaml"))
    for entry in (doc.get("packs") if isinstance(doc, dict) else []) or []:
        if isinstance(entry, dict) and str(entry.get("type")) == "bundle" and entry.get("id"):
            item = _normalize_pack(entry)
            if item:
                from_file[item["id"]] = item

    out: Dict[str, Dict[str, Any]] = {}
    snap = _load_yaml(Path(__file__).resolve().parent / "data" / "catalog_bundles.yaml")
    for entry in (snap.get("bundles") if isinstance(snap, dict) else []) or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if entry["id"] in from_file:
            continue
        normalized = _normalize_pack(dict(entry, type="bundle"))
        if normalized:
            out[normalized["id"]] = normalized
    out.update(from_file)
    return list(out.values())


# ─────────────────────────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────────────────────────

def _walk_brain_packs(roots: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    """Both shapes a company ships: a root persona (``<company>/brain_pack.yaml``)
    and a roster under ``<company>/pack/<name>/``. The root one was invisible to
    a ``*/pack/*/`` glob, which is how 18 packs went missing from this reader."""
    for root in roots:
        files = sorted(root.glob("*/brain_pack.yaml")) + sorted(
            root.glob("*/pack/*/brain_pack.yaml"))
        seen: Dict[str, str] = {}
        for manifest_file in files:
            rel = manifest_file.relative_to(root)
            company = rel.parts[0] if len(rel.parts) > 1 else manifest_file.parent.name
            item = _normalize_brain_pack(_load_yaml(manifest_file), manifest_file, company)
            if not item:
                continue
            if item["id"] in seen:
                # A company ships both a root persona and pack/<same-name>; the
                # platform qualifies the second by its path so BOTH survive
                # (`brain:dgg/dgg` and `brain:dgg/dgg#dgg/pack/dgg`).
                item["id"] = f"{item['id']}#{rel.parent.as_posix()}"
            seen[item["id"]] = str(manifest_file)
            yield item


def _walk_companies(roots: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    for root in roots:
        for company_file in sorted(root.glob("*/company.yaml")):
            item = _normalize_company(_load_yaml(company_file), company_file)
            if item:
                yield item


def _walk_awpacks(roots: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    """awpack manifests: ``*/pack.yaml`` shelves and ``*.awpack.yaml`` imports."""
    seen: set = set()
    for root in roots:
        for manifest_file in sorted(root.glob("*/pack.yaml")):
            doc = _load_yaml(manifest_file)
            if not isinstance(doc, dict) or "awpack" not in str(
                    manifest_file.parent.parent.name).lower():
                # A `pack.yaml` that is a brain pack manifest, not an awpack.
                if not (isinstance(doc, dict) and doc.get("install") and doc.get("runtime")):
                    continue
            item = _normalize_awpack(doc, manifest_file)
            if item and item["id"] not in seen:
                seen.add(item["id"])
                yield item
        for manifest_file in sorted(root.glob("**/*.awpack.yaml")):
            item = _normalize_awpack(_load_yaml(manifest_file), manifest_file)
            if item and item["id"] not in seen:
                seen.add(item["id"])
                yield item


def _normalize_acp(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One agentclientprotocol registry agent -- third-party, installable."""
    if not isinstance(entry, dict) or not entry.get("id"):
        return None
    dist = entry.get("distribution") or {}
    method, command = "unsupported", ""
    if isinstance(dist, dict):
        npx = dist.get("npx") or {}
        uvx = dist.get("uvx") or {}
        if isinstance(npx, dict) and npx.get("package"):
            args = " ".join(str(a) for a in npx.get("args") or [])
            method = "npx"
            command = f"npx -y {npx['package']}{(' ' + args) if args else ''}".strip()
        elif isinstance(uvx, dict) and uvx.get("package"):
            args = " ".join(str(a) for a in uvx.get("args") or [])
            method = "uvx"
            command = f"uvx {uvx['package']}{(' ' + args) if args else ''}".strip()
        elif dist.get("binary"):
            method = "binary"
    desc = str(entry.get("description") or "")
    return _entry(
        id=f"acp:{entry['id']}",
        kind="agent_pack",
        name=str(entry.get("name") or entry["id"]),
        summary=desc,
        category="acp-agents",
        source="community",
        version=str(entry.get("version") or "0.0.0"),
        tags=["acp", "external-agent"],
        pricing={"tier": "free", "amount_cents": None},
        install={"method": method, "command": command} if command else {"method": method},
        what_it_does=_first_sentence(desc),
        audience=["developer"],
        install_targets=["awdk_local"],
        trust=_trust("community", {"registry": "agentclientprotocol"}),
        requires=_requires(""),
    )


def _acp_items() -> List[Dict[str, Any]]:
    doc = _load_json(find("config/acp-registry.json"))
    agents = doc.get("agents") if isinstance(doc, dict) else None
    if not isinstance(agents, list):
        return []
    out = []
    for entry in agents:
        # The registry lists our own adk; packs_catalog already carries it, and a
        # second row would show the same tool twice under two names.
        if isinstance(entry, dict) and str(entry.get("id")) == "awdk":
            continue
        item = _normalize_acp(entry)
        if item:
            out.append(item)
    return out


def load_catalog(root: Optional[Path] = None,
                 pack_roots: Optional[Iterable[Path]] = None) -> List[Dict[str, Any]]:
    """Every catalog entry, deduped by id; a missing source contributes nothing.

    ``pack_roots`` restricts the brain/company walk to the given registries;
    the default is every root the platform and this machine know about.
    """
    items: List[Dict[str, Any]] = []

    doc = _load_yaml(find("config/packs_catalog.yaml"))
    for entry in (doc.get("packs") if isinstance(doc, dict) else []) or []:
        item = _normalize_pack(entry)
        if item:
            items.append(item)

    doc = _load_yaml(find("config/app_catalog.yaml"))
    for app_id, entry in ((doc.get("apps") or {}) if isinstance(doc, dict) else {}).items():
        item = _normalize_app(str(app_id), entry)
        if item:
            items.append(item)

    doc = _load_json(find("config/mcp-registry.json"))
    for entry in (doc.get("servers") if isinstance(doc, dict) else []) or []:
        item = _normalize_mcp(entry)
        if item:
            items.append(item)

    items.extend(_acp_items())

    doc = _load_yaml(find("config/extension_catalog.yaml"))
    if isinstance(doc, dict):
        for entry in doc.get("extensions") or []:
            item = _normalize_extension(entry, "extension")
            if item:
                items.append(item)
        for entry in doc.get("plugins") or []:
            item = _normalize_extension(entry, "plugin")
            if item:
                items.append(item)

    ident_dir = find("config/identities")
    if ident_dir is not None and ident_dir.is_dir():
        for ident_file in sorted(ident_dir.glob("*.yaml")):
            item = _normalize_agent_identity(_load_yaml(ident_file), ident_file)
            if item:
                items.append(item)

    brain_roots = _pack_roots(pack_roots)
    items.extend(_walk_brain_packs(brain_roots))
    items.extend(_walk_companies(brain_roots))

    awpack_roots: List[Path] = []
    repo = _repo_root()
    if repo is not None:
        awpack_roots.append(repo / "awpack" / "packs")
    for cand in _candidates():
        awpack_roots.append(cand / "awpack" / "packs")
    items.extend(_walk_awpacks([r for r in awpack_roots if r.is_dir()]))

    items.extend(starter_bundles())

    by_id: Dict[str, Dict[str, Any]] = {}
    for item in items:
        by_id.setdefault(item["id"], item)
    return sorted(by_id.values(), key=lambda i: (i["kind"], i["name"].lower()))


def get_entry(item_id: str, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    wanted = str(item_id or "").strip()
    if not wanted:
        return None
    items = load_catalog(root)
    for item in items:
        if item["id"] == wanted:
            return item
    # A bare id (no namespace) resolves when exactly one entry matches on name/id
    # suffix, so `adk catalog get awdesk` works without knowing the prefix.
    matches = [i for i in items if i["id"].split(":", 1)[-1] == wanted]
    return matches[0] if len(matches) == 1 else None


def search(query: str = "", kind: Optional[str] = None,
           for_who: Optional[str] = None, root: Optional[Path] = None,
           limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    items = load_catalog(root)
    q = str(query or "").strip().lower()
    # A person types a sentence, not a keyword ("film budgeting"), so a multi-word
    # query matches on ANY term and ranks by how many it matched; a single term
    # still behaves as a plain substring test.
    terms = [t for t in q.split() if len(t) > 2] or ([q] if q else [])
    scored = []
    for item in items:
        if kind and item["kind"] != kind:
            continue
        if for_who and for_who not in item.get("audience", []):
            continue
        score = 0
        if terms:
            hay = " ".join([item["name"], item["summary"], item.get("what_it_does", ""),
                            " ".join(item.get("tags") or []),
                            item["id"]]).lower()
            score = sum(1 for t in terms if t in hay)
            if score == 0:
                continue
        scored.append((score, item))
    # Stable sort keeps the catalog's kind ordering within a score band.
    scored.sort(key=lambda pair: -pair[0])
    sliced = scored if limit is None else scored[:max(1, int(limit))]
    return [item for _, item in sliced]


_PACK_KINDS = ("agent_pack", "brain_pack", "awpack", "bundle")
_TOOL_KINDS = ("tool_pack", "mcp_pack")


def _installed_here() -> Dict[str, Any]:
    """What is INSTALLED on this machine: packs the loader can load, and what
    ``adk pack install`` / ``adk skills install`` recorded."""
    out: Dict[str, Any] = {"packs": 0, "pack_installs": 0, "skills": 0, "error": ""}
    try:
        from adk.tool_pack_loader import ToolPackLoader
        out["packs"] = len(ToolPackLoader(enforce_entitlements=False).discover())
    except Exception as exc:  # noqa: BLE001 - reported, the other counts still answer
        out["error"] = f"{type(exc).__name__}: {exc}"
    try:
        from adk.addon_lock import installs
        out["pack_installs"] = sum(len(installs(k)) for k in ("library", "awpack", "awnode-ext"))
        out["skills"] = len(installs("skill"))
    except Exception as exc:  # noqa: BLE001
        out["error"] = out["error"] or f"{type(exc).__name__}: {exc}"
    return out


def _gateway_loaded(fetch: Any = None) -> Dict[str, Any]:
    """What the local MCP gateway has LOADED right now (its own GET /health/packs).

    ``packs`` counts packs serving at least one tool; ``registered_no_tools`` the
    ones that registered and serve none (deprecated shims, packs this gateway is
    not entitled to, packs whose tools another module serves).
    """
    try:
        from adk.discovery import sources
        doc = (fetch or sources._get_json)(
            f"{sources.GATEWAY_URL}/health/packs", bearer=sources._bearer())
        packs = (doc or {}).get("packs")
        if not isinstance(packs, dict):
            raise ValueError("the gateway answered without a packs map")
        serving = {k: int(v) for k, v in packs.items() if int(v) > 0}
        return {"url": sources.GATEWAY_URL, "packs": len(serving),
                "tools": sum(serving.values()),
                "registered_no_tools": sorted(k for k, v in packs.items() if int(v) == 0),
                "error": ""}
    except Exception as exc:  # noqa: BLE001 - a gateway that is down is a fact to print
        return {"url": "", "packs": None, "tools": None, "registered_no_tools": [],
                "error": f"{type(exc).__name__}: {exc}"}


def catalog_stats(root: Optional[Path] = None, *, refresh: bool = True,
                  gateway_fetch: Any = None, index_state: Any = None) -> Dict[str, Any]:
    """Three counts that are different things, each named, from one catalogue.

    * ``catalogue``  -- what is AVAILABLE: the entries of the unified catalogue
      (:func:`load_catalog`), the same source ``adk catalog list`` prints.
    * ``installed``  -- what is installed on THIS machine.
    * ``gateway``    -- what the local MCP gateway has LOADED right now.
    * ``index``      -- the discovery index an agent searches here, with its age;
      a missing or stale index is rebuilt first (``refresh=False`` skips that).

    Measured 2026-11-12 this command printed "tools 0, packs 0": it counted the
    newest file in the discovery directory, which was one directory's part
    (skills only) and 15 days old, and called that the catalogue. The top-level
    ``*_total`` keys are kept and now always come from the catalogue.
    """
    import time
    now = time.time()
    items = load_catalog(root)
    file_at = 0.0
    cfg = find("config/packs_catalog.yaml")
    if cfg is not None:
        try:
            file_at = cfg.stat().st_mtime
        except OSError:
            file_at = 0.0
    counts = {"tools": sum(1 for i in items if i["kind"] in _TOOL_KINDS),
              "skills": sum(1 for i in items if i["kind"] == "skill_pack"),
              "packs": sum(1 for i in items if i["kind"] in _PACK_KINDS),
              "agents": sum(1 for i in items if i["kind"] == "agent")}

    if index_state is None:
        try:
            from adk.discovery import index as _index
            index_state = _index.ensure_fresh(refresh=refresh)
        except Exception as exc:  # noqa: BLE001 - the catalogue counts still answer
            index_state = {"merged": {"cards": []}, "age_s": None, "refreshed": False,
                           "was": "missing", "error": f"{type(exc).__name__}: {exc}"}
    by_domain: Dict[str, int] = {}
    for card in (index_state.get("merged") or {}).get("cards") or []:
        if isinstance(card, dict):
            dom = str(card.get("domain") or "")
            by_domain[dom] = by_domain.get(dom, 0) + 1
    try:
        from adk.discovery import index as _index
        index_line = _index.describe_age(index_state)
    except Exception:  # noqa: BLE001
        index_line = "discovery index: unavailable"

    return {
        "catalogue": {"entries": len(items), **counts,
                      "source": (f"catalogue files under {cfg.parent.parent}"
                                 if cfg is not None else "bundled with the kit"),
                      "measured_at": file_at},
        "installed": _installed_here(),
        "gateway": _gateway_loaded(gateway_fetch),
        "index": {"cards": sum(by_domain.values()), "by_domain": by_domain,
                  "age_s": index_state.get("age_s"), "was": index_state.get("was"),
                  "refreshed": bool(index_state.get("refreshed")),
                  "error": index_state.get("error") or "", "line": index_line},
        "tools_total": counts["tools"], "skills_total": counts["skills"],
        "packs_total": counts["packs"], "agents_total": counts["agents"],
        "measured_at": file_at,
        "stale_seconds": max(0, int(now - file_at)) if file_at else -1,
        "source": "catalog",
    }


def format_stats(stats: Dict[str, Any]) -> List[str]:
    """The lines ``adk catalog stats`` prints: one per count, each saying what it counts."""
    cat, inst, gw, idx = (stats["catalogue"], stats["installed"], stats["gateway"],
                          stats["index"])
    lines = [
        f"available in the catalogue: {cat['entries']} entries "
        f"(packs {cat['packs']}, tool packs {cat['tools']}, skill packs {cat['skills']}, "
        f"agents {cat['agents']})",
        f"  source: {cat['source']}",
        f"installed on this machine:  {inst['packs']} packs the loader can load, "
        f"{inst['pack_installs']} installed with `adk pack install`, "
        f"{inst['skills']} skills installed"
        + (f"  ({inst['error']})" if inst.get("error") else ""),
    ]
    if gw.get("error"):
        lines.append(f"loaded in the gateway:      unknown ({gw['error'][:100]})")
    else:
        lines.append(f"loaded in the gateway:      {gw['packs']} packs serving "
                     f"{gw['tools']} tools at {gw['url']}")
        if gw.get("registered_no_tools"):
            lines.append(f"  registered but serving no tools ({len(gw['registered_no_tools'])}): "
                         + ", ".join(gw["registered_no_tools"]))
    doms = ", ".join(f"{k} {v}" for k, v in sorted((idx.get("by_domain") or {}).items()))
    lines.append(f"searchable by an agent here: {idx['cards']} cards" + (f" ({doms})" if doms else ""))
    lines.append(f"  {idx['line']}")
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# Install
# ─────────────────────────────────────────────────────────────────────────────

def tree_digest(path: Path) -> str:
    """sha256 over the pack tree's relative paths and bytes (order-independent)."""
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = f.relative_to(path).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            continue
        h.update(b"\0")
    return h.hexdigest()


def _install_source_dir(item: Dict[str, Any]) -> Optional[Path]:
    """The directory a catalog entry installs FROM, when it is a local pack."""
    ident = item["id"]
    kind = item["kind"]
    if kind == "brain_pack":
        company, _, name = ident.split(":", 1)[-1].partition("/")
        for root in _pack_roots():
            cand = root / company / "pack" / name
            if cand.is_dir():
                return cand
        return None
    if kind == "awpack":
        pack_id = ident.split(":", 1)[-1]
        repo = _repo_root()
        for root in ([repo / "awpack" / "packs"] if repo else []) + \
                [c / "awpack" / "packs" for c in _candidates()]:
            cand = root / pack_id
            if cand.is_dir():
                return cand
        return None
    if kind in ("agent", "agent_pack"):
        name = ident.split(":", 1)[-1]
        for root in _pack_roots():
            for cand in sorted(root.glob(f"*/pack/{name}")):
                if cand.is_dir():
                    return cand
        return None
    return None


def _is_paid(item: Dict[str, Any]) -> bool:
    """A non-free tier is paid even when no price is stated: a stale/absent
    amount must not let a paid pack install as if it were free (fail closed)."""
    pricing = item.get("pricing") or {}
    if (item.get("trust") or {}).get("source") != "first_party":
        return True
    return pricing.get("tier") not in (None, "free")


def install_entry(item_id: str, target: str = "awdk_local", *, dry_run: bool = False,
                  signature: str = "", agents_root: Optional[Path] = None,
                  root: Optional[Path] = None) -> Dict[str, Any]:
    """Install (or plan) one catalog entry into ``target``.

    Returns ``{ok, id, target, exit_code, plan, digest, verified_by, detail}``.
    Never raises, never reaches the network. A paid entry without a valid signature
    is refused (``ok`` False, ``exit_code`` 1); a free first-party entry is
    accepted on its DIGEST, so a caller can pin exactly what was installed.
    """
    item = get_entry(item_id, root)
    if item is None:
        return {"ok": False, "id": item_id, "target": target, "exit_code": 1,
                "plan": [], "digest": "", "verified_by": "",
                "detail": f"no catalog entry {item_id!r}"}
    if target not in INSTALL_TARGETS:
        return {"ok": False, "id": item["id"], "target": target, "exit_code": 2,
                "plan": [], "digest": "", "verified_by": "",
                "detail": f"unknown target {target!r}; use one of {INSTALL_TARGETS}"}
    if item.get("status") in ("blocked", "internal"):
        return {"ok": False, "id": item["id"], "target": target, "exit_code": 1,
                "plan": [], "digest": "", "verified_by": "",
                "detail": "; ".join(item.get("blockers") or ["not installable"])}

    src = _install_source_dir(item)
    digest = tree_digest(src) if src is not None and src.is_dir() else ""
    verified_by = ""
    plan: List[str] = []

    if target == "awdk_local":
        plan.append(str((item.get("install") or {}).get("command") or
                        f"adk catalog install {item['id']}"))
        return {"ok": True, "id": item["id"], "target": target, "exit_code": 0,
                "plan": plan, "digest": digest, "verified_by": "local",
                "detail": "already local; use the printed command to attach it"}

    paid = _is_paid(item)
    if paid:
        if not signature:
            return {"ok": False, "id": item["id"], "target": target, "exit_code": 1,
                    "plan": [], "digest": digest, "verified_by": "",
                    "detail": "pack is paid; a signature is required (--signature HEX)"}
        try:
            from adk.pack_verifier import verify_bytes
            digest_bytes = (digest or "").encode("utf-8")
            if not verify_bytes(digest_bytes, signature):
                return {"ok": False, "id": item["id"], "target": target,
                        "exit_code": 1, "plan": [], "digest": digest,
                        "verified_by": "", "detail": "signature did not verify"}
        except Exception as exc:  # noqa: BLE001 - cannot verify = fail closed
            return {"ok": False, "id": item["id"], "target": target, "exit_code": 1,
                    "plan": [], "digest": digest, "verified_by": "",
                    "detail": f"cannot verify signature: {type(exc).__name__}"}
        verified_by = "signature"
    else:
        if not digest:
            # An entry with no local pack tree (a hosted app, an MCP server) has
            # nothing to copy. A DRY RUN can still answer honestly ("run the
            # printed command"); a real install must refuse rather than report
            # success with nothing installed.
            plan.append(str((item.get("install") or {}).get("command")
                            or f"adk catalog get {item['id']}"))
            if dry_run:
                return {"ok": True, "id": item["id"], "target": target,
                        "exit_code": 0, "plan": plan, "digest": "",
                        "verified_by": "command",
                        "detail": "no local pack tree; planned the printed command"}
            return {"ok": False, "id": item["id"], "target": target, "exit_code": 1,
                    "plan": plan, "digest": "", "verified_by": "",
                    "detail": "no local pack directory to install from"}
        verified_by = "digest"

    plan.append(f"install {item['id']} into {target} from {src}")
    plan.append(f"verify by {verified_by}: {digest[:16]}...")
    if dry_run:
        return {"ok": True, "id": item["id"], "target": target, "exit_code": 0,
                "plan": plan, "digest": digest, "verified_by": verified_by,
                "detail": "dry run; nothing written"}

    if src is None or not src.is_dir():
        return {"ok": False, "id": item["id"], "target": target, "exit_code": 1,
                "plan": plan, "digest": digest, "verified_by": verified_by,
                "detail": "no local pack directory to install from"}
    try:
        from adk.pack_install import install_pack_dir
        result = install_pack_dir(src, agents_root=agents_root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "id": item["id"], "target": target, "exit_code": 1,
                "plan": plan, "digest": digest, "verified_by": verified_by,
                "detail": f"{type(exc).__name__}: {exc}"}
    plan.append(f"installed to {result.get('installed_to', '')}")
    return {"ok": True, "id": item["id"], "target": target, "exit_code": 0,
            "plan": plan, "digest": digest, "verified_by": verified_by,
            "detail": f"installed {result.get('agent')}"}


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="adk catalog",
                                 description="Browse and install catalog entries.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="verb")

    # `--json` is accepted both before the verb (`adk catalog --json list`) and
    # after it (`adk catalog list --json`); the sibling verbs all teach the
    # second form, so a reader who types it must not get an argparse usage error.
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so the subparser's default does not overwrite a `--json` given
    # before the verb.
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)

    ls = sub.add_parser("list", parents=[common], help="list catalog entries")
    ls.add_argument("query", nargs="?", default="")
    ls.add_argument("--kind", default=None)
    ls.add_argument("--for", dest="for_who", default=None,
                    choices=list(AUDIENCES))
    ls.add_argument("--limit", type=int, default=None,
                    help="max entries (default: all)")
    g = sub.add_parser("get", parents=[common], help="show one entry")
    g.add_argument("id")
    st = sub.add_parser("stats", parents=[common],
                        help="catalogue, installed and gateway counts, each labelled")
    st.add_argument("--no-refresh", action="store_true",
                    help="do not rebuild a missing or stale discovery index first")
    fnd = sub.add_parser("find", parents=[common],
                         help="find something to add: apps, packs, skills, bricks, "
                              "integrations, stacks, products")
    fnd.add_argument("words", nargs="*")
    fnd.add_argument("--kind", default=None)
    fnd.add_argument("--all", action="store_true", help="show every match, not five")
    for verb, text in (("add", "add one entry to this computer (reversible)"),
                       ("remove", "undo an add")):
        p = sub.add_parser(verb, parents=[common], help=text)
        p.add_argument("id")
        p.add_argument("--dry-run", action="store_true",
                       help="print the steps; install or remove nothing")
    ins = sub.add_parser("install", parents=[common], help="install one entry")
    ins.add_argument("id")
    ins.add_argument("--target", default="awdk_local", choices=list(INSTALL_TARGETS))
    ins.add_argument("--dry-run", action="store_true")
    ins.add_argument("--signature", default="")
    ins.add_argument("--agents-root", default=None)
    return ap


def _print_entry(item: Dict[str, Any]) -> None:
    print(f"  {item['id']}  [{item['kind']}]  {item['status']}")
    print(f"    {item['name']}")
    print(f"    {item.get('what_it_does') or item.get('summary')}")
    audience = ", ".join(item.get("audience") or [])
    targets = ", ".join(item.get("install_targets") or [])
    print(f"    for: {audience}   install to: {targets}")
    pricing = item.get("pricing") or {}
    price = "free" if pricing.get("tier") in (None, "free") else \
        f"{pricing.get('tier')} {pricing.get('amount_cents') or 0}c"
    print(f"    price: {price}   trust: {(item.get('trust') or {}).get('source')}")
    if item.get("includes"):
        print(f"    includes {len(item['includes'])}: " +
              ", ".join(str(i.get("id")) for i in item["includes"][:8]))
    for blocker in item.get("blockers") or []:
        print(f"    blocker: {_clamp(blocker, 100)}")


def cli_main(argv: Optional[List[str]] = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code in (0, None) else 2

    # A cp1252 console must not turn a catalog string into a traceback; a stream
    # that is not a TextIOWrapper (a test's capture) keeps its own handling.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            continue  # a capture object keeps its own stream handling

    if args.verb in (None, "list"):
        items = search(getattr(args, "query", "") or "",
                       kind=getattr(args, "kind", None),
                       for_who=getattr(args, "for_who", None),
                       limit=getattr(args, "limit", None))
        if args.json:
            print(json.dumps({"ok": True, "count": len(items), "items": items},
                             ensure_ascii=True, indent=2))
            return 0
        print(f"{len(items)} catalog entries")
        for item in items:
            _print_entry(item)
        return 0

    if args.verb == "get":
        item = get_entry(args.id)
        if item is None:
            print(f"no catalog entry {args.id!r}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(item, ensure_ascii=True, indent=2))
        else:
            _print_entry(item)
        return 0

    if args.verb == "stats":
        stats = catalog_stats(refresh=not getattr(args, "no_refresh", False))
        if args.json:
            print(json.dumps(stats, ensure_ascii=True, indent=2))
        else:
            for line in format_stats(stats):
                print(line)
        return 0

    if args.verb in ("find", "add", "remove"):
        from adk import catalog_person as person
        if args.verb == "find":
            res = person.find_entries(" ".join(args.words), kind=args.kind,
                                      limit=100000 if args.all else person.LIST_CAP)
            print(json.dumps(res, ensure_ascii=True, indent=2) if args.json
                  else person.render_find(res))
            return 0
        act = person.add_entry if args.verb == "add" else person.remove_entry
        res = act(args.id, dry_run=args.dry_run)
        print(json.dumps(res, ensure_ascii=True, indent=2) if args.json
              else person.render_result(res, args.verb))
        return int(res.get("exit_code", 1))

    if args.verb == "install":
        result = install_entry(args.id, args.target, dry_run=args.dry_run,
                               signature=args.signature,
                               agents_root=Path(args.agents_root).expanduser()
                               if args.agents_root else None)
        if args.json:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            for line in result.get("plan") or []:
                print(f"  {line}")
            print(f"  {result.get('detail')}")
        return int(result.get("exit_code", 1))

    _parser().print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(cli_main())
