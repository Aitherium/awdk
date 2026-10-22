"""Discovery sources. Each returns ``(cards, receipt)`` and never raises.

A receipt is ``{"ok": bool, "count": int, "error": str, "ms": int, "sig": str}``.
``ok`` False with an ``error`` is how a dead source shows up: the index records it and
``adk discover check`` (CD004) reports it, so a source that silently returns nothing is
distinguishable from one that genuinely has nothing.

``sig`` is a cheap fingerprint (file count and newest mtime of what the source read),
recorded so a stale index can be explained. File sources cost milliseconds and are
re-read on every build; network and probe sources are reused until their
``NETWORK_TTL_S`` runs out, unless the previous run failed.

Sources are split into two PARTS:

* ``user`` -- the same for every working directory: ``~/.claude`` skills and agents,
  Claude Code plugins, aw* CLIs, user packs, the pack catalog, gateway tools and agents.
* ``cwd`` -- depends on the directory: ``<cwd>/.claude`` skills/commands/agents, a
  checkout's ``awskills/skills`` and ``awpack/packs``.
"""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from adk.discovery import clis, scope

GATEWAY_URL = os.environ.get("AITHER_GATEWAY_URL", "http://127.0.0.1:8182").rstrip("/")
NET_TIMEOUT = 8.0
ONE_LINE = 200

Card = dict
Receipt = dict
SourceFn = Callable[..., "tuple[list, dict]"]


def _home() -> Path:
    return Path(os.environ.get("AITHER_HOME_OVERRIDE", "") or Path.home())


def _receipt(ok: bool, count: int, t0: float, error: str = "", sig: str = "",
             **extra: Any) -> Receipt:
    rec = {"ok": ok, "count": count, "error": error, "ms": int((time.monotonic() - t0) * 1000),
           "sig": sig}
    rec.update(extra)
    return rec


def _sig(paths: list) -> str:
    newest = 0.0
    n = 0
    for p in paths:
        try:
            st = Path(p).stat()
        except OSError:
            continue
        n += 1
        newest = max(newest, st.st_mtime)
    return f"{n}:{newest:.0f}"


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())[:ONE_LINE]


def _card(domain: str, name: str, one_line: str, source: str, hosting: str,
          how: str, **extra: Any) -> Card:
    card = {"domain": domain, "name": name, "one_line": _one_line(one_line),
            "source": source, "hosting": hosting, "how_to_invoke": how,
            "tags": [], "license": "", "license_flag": ""}
    card.update(extra)
    return card


def _read_frontmatter(path: Path) -> "tuple[dict, str]":
    from adk.harnesses.skills_local import split_frontmatter
    try:
        return split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}, ""


def _describe(meta: dict, body: str) -> str:
    if meta.get("description"):
        return meta["description"]
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def _yaml(path: Path) -> Any:
    try:
        import yaml  # awdk depends on PyYAML
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyYAML missing") from exc
    return yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))


# ── skills ──────────────────────────────────────────────────────────────────

def _skill_cards(root: Path, source: str, prefix: str = "") -> list:
    cards: list = []
    for md in sorted((root / "skills").glob("*/SKILL.md")):
        meta, body = _read_frontmatter(md)
        name = prefix + (meta.get("name") or md.parent.name)
        cards.append(_card("skills", name, _describe(meta, body), source, "local",
                           f"Skill tool: {name}", path=str(md)))
    for md in sorted((root / "commands").glob("*.md")):
        meta, body = _read_frontmatter(md)
        name = prefix + md.stem
        cards.append(_card("skills", name, _describe(meta, body), source, "local",
                           f"/{name}", kind="command", path=str(md)))
    return cards


def _skill_files(root: Path) -> list:
    return list((root / "skills").glob("*/SKILL.md")) + list((root / "commands").glob("*.md"))


def cwd_skills(cwd: Path) -> "tuple[list, dict]":
    t0 = time.monotonic()
    root = cwd / ".claude"
    files = _skill_files(root)
    try:
        cards = _skill_cards(root, "project-skill")
    except Exception as exc:  # noqa: BLE001
        return [], _receipt(False, 0, t0, f"{type(exc).__name__}: {exc}")
    return cards, _receipt(True, len(cards), t0, sig=_sig(files))


def user_skills(_cwd: Path = Path(".")) -> "tuple[list, dict]":
    t0 = time.monotonic()
    root = _home() / ".claude"
    files = _skill_files(root)
    try:
        cards = _skill_cards(root, "user-skill")
    except Exception as exc:  # noqa: BLE001
        return [], _receipt(False, 0, t0, f"{type(exc).__name__}: {exc}")
    return cards, _receipt(True, len(cards), t0, sig=_sig(files))


def _plugin_roots(cwd: Path) -> list:
    """(plugin name, install path) for every plugin installed for this user or cwd.

    ``installed_plugins.json`` names the ONE version in use; globbing the cache would
    also return every superseded version still on disk.
    """
    base = _home() / ".claude" / "plugins"
    out: list = []
    try:
        doc = json.loads((base / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = None
    plugins = (doc or {}).get("plugins") if isinstance(doc, dict) else None
    if isinstance(plugins, dict):
        here = str(cwd.resolve()).lower()
        for key, installs in plugins.items():
            for inst in installs if isinstance(installs, list) else []:
                if not isinstance(inst, dict) or not inst.get("installPath"):
                    continue
                proj = str(inst.get("projectPath") or "")
                if inst.get("scope") == "project" and proj.lower() != here:
                    continue
                out.append((key.split("@", 1)[0], Path(inst["installPath"])))
        return out
    # No registry: newest version directory of each cached plugin.
    for plugin_dir in sorted((base / "cache").glob("*/*")):
        versions = sorted((v for v in plugin_dir.iterdir() if v.is_dir()),
                          key=lambda v: v.stat().st_mtime)
        if versions:
            out.append((plugin_dir.name, versions[-1]))
    return out


def plugin_skills(cwd: Path) -> "tuple[list, dict]":
    t0 = time.monotonic()
    try:
        roots = _plugin_roots(cwd)
        cards: list = []
        files: list = [_home() / ".claude" / "plugins" / "installed_plugins.json"]
        for name, root in roots:
            files += _skill_files(root)
            cards += _skill_cards(root, "plugin-skill", prefix=f"{name}:")
    except Exception as exc:  # noqa: BLE001
        return [], _receipt(False, 0, t0, f"{type(exc).__name__}: {exc}")
    return cards, _receipt(True, len(cards), t0, sig=_sig(files), plugins=len(roots))


def awskills(cwd: Path) -> "tuple[list, dict]":
    """A checkout's ``awskills/skills/*.md`` -- the skill sources the plugin is built from."""
    t0 = time.monotonic()
    d = cwd / "awskills" / "skills"
    if not d.is_dir():
        return [], _receipt(True, 0, t0, sig="absent")
    files = sorted(d.glob("*.md"))
    cards: list = []
    for md in files:
        if md.name.lower() == "readme.md":
            continue
        meta, body = _read_frontmatter(md)
        name = meta.get("name") or md.stem
        cards.append(_card("skills", name, _describe(meta, body), "awskills", "local",
                           f"Skill tool: awsh:{name} (awskills source)", path=str(md)))
    return cards, _receipt(True, len(cards), t0, sig=_sig(files))


# ── agents ──────────────────────────────────────────────────────────────────

def _agent_cards(d: Path, source: str) -> "tuple[list, list]":
    files = sorted(p for p in d.glob("*.md") if p.name.lower() != "readme.md")
    cards = []
    for md in files:
        meta, body = _read_frontmatter(md)
        name = meta.get("name") or md.stem
        cards.append(_card("agents", name, _describe(meta, body), source, "local",
                           f"Task(subagent_type={name!r})", path=str(md)))
    return cards, files


def cwd_agents(cwd: Path) -> "tuple[list, dict]":
    t0 = time.monotonic()
    cards, files = _agent_cards(cwd / ".claude" / "agents", "project-agent")
    return cards, _receipt(True, len(cards), t0, sig=_sig(files))


def user_agents(_cwd: Path = Path(".")) -> "tuple[list, dict]":
    t0 = time.monotonic()
    cards, files = _agent_cards(_home() / ".claude" / "agents", "user-agent")
    return cards, _receipt(True, len(cards), t0, sig=_sig(files))


# ── aw* CLIs ────────────────────────────────────────────────────────────────

def cli_tools(_cwd: Path = Path("."), probe: Optional[clis.ProbeFn] = None) -> "tuple[list, dict]":
    """One tools card per WORKING aw* CLI; the broken ones are in the receipt."""
    t0 = time.monotonic()
    results = clis.probe_all(probe or clis.static_probe)
    cards = []
    broken = {}
    for name, (status, detail) in results.items():
        if status != clis.OK:
            broken[name] = detail
            continue
        cards.append(_card("tools", name, clis.TOOL_PURPOSE.get(name, name), "cli", "local",
                           clis.TOOL_VERBS[name], tags=["cli"]))
    return cards, _receipt(True, len(cards), t0, sig="probe", broken=broken)


# ── packs ───────────────────────────────────────────────────────────────────

def awpack_shelf(cwd: Path) -> "tuple[list, dict]":
    t0 = time.monotonic()
    d = cwd / "awpack" / "packs"
    if not d.is_dir():
        return [], _receipt(True, 0, t0, sig="absent")
    files = sorted(d.glob("*/pack.yaml"))
    cards: list = []
    errors = []
    for py in files:
        try:
            doc = _yaml(py) or {}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{py.parent.name}: {type(exc).__name__}")
            continue
        name = str(doc.get("name") or doc.get("id") or py.parent.name)
        lic = str(doc.get("license") or "")
        cards.append(_card("packs", name, str(doc.get("description") or doc.get("summary") or ""),
                           "awpack", "local", f"awpack install {name}",
                           tags=[str(t) for t in doc.get("tags") or []][:8],
                           license=lic, license_flag=scope.license_flag(lic),
                           min_tier=str(doc.get("min_tier") or "")))
    return cards, _receipt(not errors or bool(cards), len(cards), t0, "; ".join(errors),
                           sig=_sig(files))


def toolpacks(_cwd: Path = Path(".")) -> "tuple[list, dict]":
    """awdk toolpacks; a pack that fails its entitlement check is marked not entitled."""
    t0 = time.monotonic()
    try:
        from adk.tool_pack_loader import ToolPackLoader
        loader = ToolPackLoader(enforce_entitlements=False)
        manifests = loader.discover()
        files = [m.path for m in manifests.values()]
        cards = []
        for m in manifests.values():
            if m.deprecated:
                continue
            try:
                entitled, _why = loader.check_license(m)
            except Exception:  # noqa: BLE001 - cannot judge = not entitled (fail closed)
                entitled = False
            cards.append(_card("packs", m.id, m.description or m.name, "toolpack", "local",
                               f"adk pack enable {m.id}", tags=list(m.tags)[:8],
                               entitled=bool(entitled), min_tier=m.min_tier or "",
                               mcp_tools=list(m.mcp_tools)[:12]))
    except Exception as exc:  # noqa: BLE001
        return [], _receipt(False, 0, t0, f"{type(exc).__name__}: {exc}")
    return cards, _receipt(True, len(cards), t0, sig=_sig(files))


def brain_packs(_cwd: Path = Path(".")) -> "tuple[list, dict]":
    t0 = time.monotonic()
    try:
        from adk.pack_discovery import list_available_packs
        packs = list_available_packs()
    except Exception as exc:  # noqa: BLE001
        return [], _receipt(False, 0, t0, f"{type(exc).__name__}: {exc}")
    cards = [_card("packs", str(p.get("name")), str(p.get("description") or p.get("source") or
                                                   "brain pack"),
                   "brain-pack", "local", f"AGENT_BRAIN_PACK={p.get('dir')} adk-workspace")
             for p in packs if p.get("name")]
    return cards, _receipt(True, len(cards), t0, sig=f"{len(cards)}")


def pack_catalog(_cwd: Path = Path("."), fetch: Optional[Callable] = None) -> "tuple[list, dict]":
    """The pack catalog: a hosted catalog (``AITHER_CATALOG_URL``) when one is
    configured and answers, else the catalog snapshot bundled in the wheel. Packs are
    installed from a hosted marketplace, so every catalog card is ``hosting: hosted``."""
    t0 = time.monotonic()
    packs: list = []
    origin = "bundled"
    url = os.environ.get("AITHER_CATALOG_URL", "").rstrip("/")
    if url:
        try:
            doc = (fetch or _get_json)(f"{url}/api/v1/catalog/packs", bearer=_bearer())
            packs = list((doc or {}).get("packs") or [])
            origin = url
        except Exception:  # noqa: BLE001 - fall through to the bundled snapshot
            packs = []
    if not packs:
        try:
            bundled = Path(__file__).resolve().parents[1] / "data" / "packs_catalog.json"
            packs = json.loads(bundled.read_text(encoding="utf-8")).get("packs", [])
        except (OSError, ValueError) as exc:
            return [], _receipt(False, 0, t0, f"no catalog: {exc}")
    cards = []
    for p in packs:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        lic = str(p.get("license") or "")
        cards.append(_card("packs", str(p["id"]),
                           f"{p.get('name') or ''}: {p.get('description') or ''}", "catalog",
                           "hosted", f"adk pack install {p['id']}",
                           tags=[str(t) for t in p.get("tags") or []][:8],
                           min_tier=str(p.get("tier") or ""), license=lic,
                           license_flag=scope.license_flag(lic)))
    return cards, _receipt(True, len(cards), t0, sig=f"{origin}:{len(cards)}", origin=origin)


# ── gateway ─────────────────────────────────────────────────────────────────

def _bearer() -> str:
    env = os.environ.get("AITHER_SESSION_BEARER", "").strip()
    if env:
        return env
    try:
        return (_home() / ".aither" / "session-bearer").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def fingerprint_of(bearer: str) -> str:
    """12 hex of sha256(bearer); "" for no bearer. Never the bearer itself."""
    b = (bearer or "").strip()
    return hashlib.sha256(b.encode("utf-8")).hexdigest()[:12] if b else ""


def bearer_fingerprint() -> str:
    """This process's caller key, computed locally (no network): which bearer the
    session would present. The index's user part and every gateway card are keyed
    by it, so a session with another bearer never reads this one's cards."""
    return fingerprint_of(_bearer())


def _get_json(url: str, bearer: str = "", timeout: float = NET_TIMEOUT) -> Any:
    """GET a JSON body. TLS is VERIFIED (default context); an http:// loopback URL has
    no TLS to verify."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    ctx = ssl.create_default_context() if url.startswith("https://") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


def gateway_caller(fetch: Optional[Callable] = None) -> "tuple[scope.Caller, str]":
    """(caller, error). The caller carries the bearer fingerprint it was resolved with;
    an unrecognised tier is ranked lowest and named in the error."""
    bearer = _bearer()
    fp = fingerprint_of(bearer)
    if not bearer:
        return scope.Caller(), "no session bearer"
    try:
        doc = (fetch or _get_json)(f"{GATEWAY_URL}/v1/auth/me", bearer=bearer)
    except Exception as exc:  # noqa: BLE001
        return scope.Caller(fingerprint=fp), f"{type(exc).__name__}: {exc}"
    caller = scope.caller_from_me(doc, fingerprint=fp)
    return caller, caller.tier_error


def gateway_tools(_cwd: Path = Path("."), fetch: Optional[Callable] = None) -> "tuple[list, dict]":
    """The gateway's entitled tool list (``/tools/manifest`` filters by the bearer)."""
    t0 = time.monotonic()
    if not _bearer():
        return [], _receipt(False, 0, t0, "no session bearer (~/.aither/session-bearer)")
    try:
        doc = (fetch or _get_json)(f"{GATEWAY_URL}/tools/manifest", bearer=_bearer())
    except Exception as exc:  # noqa: BLE001
        return [], _receipt(False, 0, t0, f"{GATEWAY_URL}/tools/manifest: "
                            f"{type(exc).__name__}: {exc}")
    tools = (doc or {}).get("tools") if isinstance(doc, dict) else None
    if not isinstance(tools, list):
        return [], _receipt(False, 0, t0, "manifest has no tools list")
    cards = []
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        name = str(t["name"])
        cards.append(_card("tools", name, str(t.get("description") or ""), "gateway",
                           scope.tool_hosting(name), f"MCP tool mcp__aitheros__{name}",
                           tags=[str(t.get("module") or "").replace("mcp_", "")],
                           tier=str(t.get("tier_label") or "")))
    return cards, _receipt(True, len(cards), t0, sig=f"{len(cards)}")


def gateway_agents(_cwd: Path = Path("."), fetch: Optional[Callable] = None) -> "tuple[list, dict]":
    t0 = time.monotonic()
    if not _bearer():
        return [], _receipt(False, 0, t0, "no session bearer")
    try:
        doc = (fetch or _get_json)(f"{GATEWAY_URL}/v1/agents", bearer=_bearer())
    except Exception as exc:  # noqa: BLE001
        return [], _receipt(False, 0, t0, f"{type(exc).__name__}: {exc}")
    agents = (doc or {}).get("agents") if isinstance(doc, dict) else None
    cards = []
    for a in agents if isinstance(agents, list) else []:
        if not isinstance(a, dict) or not (a.get("key") or a.get("name")):
            continue
        key = str(a.get("key") or a.get("name")).lower()
        skills = [str(s) for s in a.get("skills") or []][:8]
        cards.append(_card("agents", key, f"{a.get('role') or ''}: {a.get('description') or ''}",
                           "gateway-agent", "fleet",
                           f"MCP ask_agent / delegate_to_agent agent={key}", tags=skills))
    return cards, _receipt(True, len(cards), t0, sig=f"{len(cards)}")


def gateway_search(task: str, k: int = 8, timeout: float = 3.0,
                   fetch: Optional[Callable] = None) -> "tuple[list, str]":
    """Live ToolGraph search. (ordered tool names, error). Bounded by ``timeout``."""
    from urllib.parse import quote
    if not _bearer():
        return [], "no session bearer"
    url = f"{GATEWAY_URL}/v1/tools/search?query={quote(task)}&top_k={max(1, min(k, 50))}"
    try:
        if fetch is not None:
            doc = fetch(url, bearer=_bearer())
        else:
            doc = _get_json(url, bearer=_bearer(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"
    rows = (doc or {}).get("results") if isinstance(doc, dict) else None
    names = [str(r.get("name")) for r in rows or [] if isinstance(r, dict) and r.get("name")]
    return names, ""


#: part -> [(source id, function)]. Network sources carry a TTL; file sources a sig.
USER_SOURCES: list = [
    ("user_skills", user_skills), ("plugin_skills", plugin_skills),
    ("user_agents", user_agents), ("cli_tools", cli_tools), ("toolpacks", toolpacks),
    ("brain_packs", brain_packs), ("pack_catalog", pack_catalog),
    ("gateway_tools", gateway_tools), ("gateway_agents", gateway_agents),
]
CWD_SOURCES: list = [
    ("cwd_skills", cwd_skills), ("awskills", awskills), ("cwd_agents", cwd_agents),
    ("awpack_shelf", awpack_shelf),
]
#: Sources that cannot be fingerprinted by mtime: re-run when older than this.
NETWORK_TTL_S = {"gateway_tools": 3600.0, "gateway_agents": 3600.0, "pack_catalog": 6 * 3600.0,
                 "cli_tools": 3600.0, "brain_packs": 3600.0, "toolpacks": 3600.0}
#: Sources whose death makes the index unfit (CD004 exits 1).
CRITICAL = {"tools": ("gateway_tools",), "skills": ("cwd_skills", "user_skills", "plugin_skills")}

#: Sources whose answer depends on the bearer: each card is stamped ``fetched_for``
#: and a cached receipt is reused only for the same bearer fingerprint.
CALLER_SCOPED = frozenset({"gateway_tools", "gateway_agents", "pack_catalog"})

__all__ = ["CALLER_SCOPED", "CRITICAL", "CWD_SOURCES", "GATEWAY_URL", "NETWORK_TTL_S",
           "USER_SOURCES", "bearer_fingerprint", "fingerprint_of", "gateway_caller",
           "gateway_search"]
