"""The catalogue a person browses and adds from: ``aither find | add | remove``.

This is NOT a second catalogue. :mod:`adk.catalog` stays the one inventory; this module
adds the sources it never read and one flat row every surface can render:

    config/ecosystem.yaml               -> brick | stack   (the ``install:`` line is the add)
    config/integrations_catalogue.yaml  -> integration     (pwsh, PowerToys, Sysinternals ...)
    config/shop_catalog_seed.yaml       -> product         (``storefront.core``: what is on sale)
      + config/product_billing.yaml        (the SKU's ``setup:`` lines are the add)
    Library/packs/*/.toolpack.yaml      -> pack            (99 tool packs no door listed)
    awskills/skills/*.md                -> skill
    the installer's own components      -> app | brick     (desktop app, extension, mesh ...)

The row (``PERSON_ROW``): id, kind, title, what, requires, size, price, source, add,
remove, status, gap, verified, see -- plus ``add_steps`` / ``remove_steps``, the argv
lists ``add`` and ``remove`` actually run. ``installed`` is measured on THIS machine at
read time and is never stored.

``status`` is honest:
    available  -- ``add_steps`` exist and run without a purchase
    paid       -- on sale in the shop; ``add`` prints the product page, then the setup
    listed     -- a real thing with no add flow yet; ``gap`` says why

A pip-installed awdk has no AitherOS tree, so the same rows ship as
``adk/data/catalogue.json`` (written only by the monorepo's ``gen_catalogue`` tool,
whose ``--check`` fails when the snapshot is stale). The snapshot holds the public rows
only; tool packs and workspace agents are read live from a signed-in workspace.
The same public rows are served at https://aitherium.com/catalogue.json.

Every add is recorded in the add-on lock (:mod:`adk.addon_lock`, kind ``catalogue``)
with the steps that ran and the steps that undo them, so ``remove`` works even when the
catalogue row has changed since.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("adk.catalog_person")

__all__ = [
    "KINDS", "PERSON_ROW", "SNAPSHOT", "CORE_IDS", "INSTALLER_COMPONENTS",
    "build_rows", "load_rows", "resolve", "find_entries", "add_entry", "remove_entry",
    "is_installed", "parse_install_line", "render_find", "render_result",
]

#: The kinds a person sees. adk.catalog's finer kinds fold into these.
KINDS = ("app", "pack", "skill", "plugin", "brick", "integration", "stack", "product")
PERSON_ROW = ("id", "kind", "title", "what", "requires", "size", "price", "source",
              "add", "remove", "status", "gap", "verified", "see")
SNAPSHOT = Path(__file__).resolve().parent / "data" / "catalogue.json"
SCHEMA = "aither.catalogue/v1"
LIST_CAP = 5  # ux-shape law 4: five rows, then "+N more" with the real total

#: What a bare-bones install holds. ``remove`` refuses these: they are the console.
CORE_IDS = ("awdk", "awsh")

_FOLD = {
    "application": "app", "agent": "pack", "agent_pack": "pack", "skill_pack": "pack",
    "tool_pack": "pack", "brain_pack": "pack", "awpack": "pack", "bundle": "pack",
    "company": "pack", "mcp_pack": "pack", "extension": "plugin", "plugin": "plugin",
}

#: The pieces the one-line installer used to install for everyone. Each is now an
#: add-on; ``component`` is the name install.ps1 -Add / install.sh --add takes.
#: ``brick`` joins the row to its ecosystem.yaml entry (title/what come from there).
INSTALLER_COMPONENTS: Tuple[Dict[str, Any], ...] = (
    {"id": "awdesk", "brick": "awdesk", "kind": "app", "component": "awdesk",
     "title": "Aither Desktop",
     "what": "The desktop app: a tray icon, decision cards and the Living Desktop overlay.",
     "see": "open Aither Desktop from the Start menu or your applications folder",
     "remove_note": "uninstall Aither Desktop from your system's Apps list"},
    {"id": "awconnect", "brick": "AitherConnect", "kind": "app", "component": "awconnect",
     "title": "Awconnect browser extension",
     "what": "The Chrome and Edge extension: page context and the overlay in your browser.",
     "see": "the Awconnect icon in your browser toolbar",
     "remove_note": "remove Awconnect from your browser's extensions page"},
    {"id": "mesh", "kind": "brick", "component": "mesh",
     "title": "Household devices and mesh",
     "what": "Adds this computer to your household device list and joins your private mesh.",
     "requires": ["signed in", "Tailscale (installed for you)"],
     "see": "adk mesh status",
     "remove_note": "leave with: tailscale logout"},
    {"id": "agent", "kind": "brick", "component": "agent",
     "title": "Always-on agent",
     "what": "Starts your agent now and at every sign-in, reachable from your workspace.",
     "requires": ["signed in"],
     "see": "adk status",
     "remove_steps": [["adk", "down"]]},
    {"id": "harness", "kind": "brick", "component": "harness",
     "title": "Harness daemon",
     "what": "Runs local agent sessions in the background on 127.0.0.1:8362.",
     "see": "curl http://127.0.0.1:8362/health",
     "remove_note": "stop it by ending the 'adk harness serve' process"},
    {"id": "coding-tools", "kind": "integration", "component": "coding-tools",
     "title": "Connect Claude Code, Codex and opencode",
     "what": "Connects the coding tools already on this computer to your workspace's MCP.",
     "requires": ["signed in"],
     "see": "claude mcp list",
     "remove_note": "remove the 'aitherium' server in each tool: claude mcp remove aitherium"},
    {"id": "everything", "kind": "stack", "component": "everything",
     "title": "Everything the installer used to add",
     "what": "Every part the one-line installer added for everyone before November 2026.",
     "requires": ["signed in"],
     "see": "aither find",
     "remove_note": "remove the parts one at a time: aither remove awdesk, aither remove agent"},
    {"id": "settings", "brick": "awsettings", "kind": "brick", "component": "settings",
     "title": "Synced settings",
     "what": "Pulls the settings you saved on your other computers onto this one.",
     "requires": ["signed in"],
     "see": "awsettings status",
     "remove_note": "nothing to remove: it only copied your own settings"},
)

_PIP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\],-]*$")
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_GIT_URL = re.compile(r"^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/?$")
_WINGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*$")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


# ── locating the sources ─────────────────────────────────────────────────────────────

def _find(rel: str) -> Optional[Path]:
    from adk import catalog as _catalog
    return _catalog.find(rel)


def _repo_root() -> Optional[Path]:
    from adk import catalog as _catalog
    return _catalog._repo_root()


def _yaml(path: Optional[Path]) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a broken source contributes nothing, never a crash
        return None


def _plain(text: Any, limit: int = 140) -> str:
    """One line in plain words: whitespace folded, cut at a word boundary."""
    s = " ".join(str(text or "").replace("—", "-").replace("–", "-").split())
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "..."


def _first_sentence(text: Any, limit: int = 140) -> str:
    s = " ".join(str(text or "").split())
    for sep in (". ", "! ", "? "):
        if sep in s:
            s = s.split(sep, 1)[0] + sep.strip()
            break
    return _plain(s, limit)


# ── install lines -> steps ───────────────────────────────────────────────────────────

def parse_install_line(line: Any) -> Tuple[List[List[str]], List[List[str]], str]:
    """An ``install:`` line -> (add_steps, remove_steps, why-not).

    Only shapes with a known undo are accepted; anything else returns no steps and the
    reason, so the row is ``listed`` instead of running a guess.
    """
    s = " ".join(str(line or "").split())
    if not s:
        return [], [], "no install line recorded"
    m = re.match(r"^(?:python -m )?pip install (?:-U |--upgrade )?(\S+)$", s)
    if m and _PIP_NAME.match(m.group(1)):
        pkg = m.group(1)
        return [["pip", "install", pkg]], [["pip", "uninstall", "-y", pkg.split("[", 1)[0]]], ""
    m = re.match(r"^npm (?:i|install) -g (\S+)$", s)
    if m and _NPM_NAME.match(m.group(1)):
        return [["npm", "install", "-g", m.group(1)]], [["npm", "uninstall", "-g", m.group(1)]], ""
    m = re.match(r"^git clone (\S+)$", s)
    if m and _GIT_URL.match(m.group(1)):
        url = m.group(1).rstrip("/")
        name = _SAFE_ID.sub("-", url.rsplit("/", 1)[-1])
        return [["git", "clone", "--depth", "1", url, "{src}/" + name]], \
               [["rmtree", "{src}/" + name]], ""
    m = re.match(r"^winget install (?:-e )?(?:--id )?(\S+)(?: -e)?$", s)
    if m and _WINGET_ID.match(m.group(1)):
        return [["winget", "install", "-e", "--id", m.group(1)]], \
               [["winget", "uninstall", "-e", "--id", m.group(1)]], ""
    if re.match(r"^https://\S+$", s):
        return [["open", s]], [], ""
    return [], [], _plain(s, 90)


def _row(**fields: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": "", "kind": "", "title": "", "what": "", "requires": [], "size": "",
        "price": "free", "source": "", "add": "", "remove": "", "status": "available",
        "gap": "", "verified": "", "see": "", "add_steps": [], "remove_steps": [],
        "aliases": [], "detect": [], "public": False,
    }
    row.update({k: v for k, v in fields.items() if v is not None})
    row["what"] = _plain(row["what"])
    if isinstance(row.get("by_os"), dict):
        # Steps that differ per operating system are stored for all three and chosen
        # here, on the machine that reads the row.
        mine = row["by_os"].get(_os_key()) or {}
        row["add_steps"] = [list(x) for x in (mine.get("add") or [])]
        row["remove_steps"] = [list(x) for x in (mine.get("remove") or [])]
        row["status"] = "available" if row["add_steps"] else "listed"
        row["gap"] = "" if row["add_steps"] else (
            row.get("missing") or f"no one-step add for {_os_key()} yet")
    if row["status"] != "listed" and not row["add_steps"]:
        row["status"] = "listed"
        row["gap"] = row["gap"] or "no add flow built yet"
    if row["status"] == "listed":
        row["add"] = ""
        row["remove"] = ""
    else:
        row["add"] = f"aither add {row['id']}"
        row["remove"] = f"aither remove {row['id']}" if (
            row["remove_steps"] or row.get("remove_note")) else ""
    return row


# ── sources ──────────────────────────────────────────────────────────────────────────

def _ecosystem() -> Dict[str, Any]:
    doc = _yaml(_find("config/ecosystem.yaml"))
    return doc if isinstance(doc, dict) else {}


def _brick_rows(eco: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for b in eco.get("bricks") or []:
        if not isinstance(b, dict) or not b.get("id"):
            continue
        bid = str(b["id"])
        state = str(b.get("status") or "")
        add, undo, why = parse_install_line(b.get("install"))
        gap = ""
        status = "available"
        if state == "planned":
            status, gap = "listed", "planned, not built"
        elif state in ("unpublished", "merged"):
            status, gap = "listed", f"{state}: not published on its own yet"
        elif not add:
            status, gap = "listed", f"no one-step add yet ({why})"
        size = ""  # not measured: a wheel size belongs to the publish lane, not a guess
        kind = "integration" if str(b.get("kind")) == "corpus" and bid[:2] != "aw" else "brick"
        see = ""
        if add and add[0][0] == "pip":
            see = f"{bid} --help"
        elif add and add[0][0] == "git":
            see = "the folder printed above"
        rows.append(_row(
            id=bid, kind=kind, title=bid, what=b.get("tagline") or b.get("adopt") or "",
            requires=[], size=size,
            source=(add[0][-2] if add and add[0][0] == "git" else
                    f"https://github.com/Aitherium/{bid}") if state == "public" else "",
            status=status, gap=gap,
            add_steps=add if status == "available" else [],
            remove_steps=undo if status == "available" else [],
            see=see, public=(state == "public"),
            verified="install line checked against the public index in CI"
            if status == "available" else "",
            first_step=_plain(b.get("adopt") or "", 120)))
    return rows


def _stack_rows(eco: Dict[str, Any], bricks: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for s in eco.get("stacks") or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        members = [str(x) for x in (s.get("bricks") or [])]
        add: List[List[str]] = []
        undo: List[List[str]] = []
        missing: List[str] = []
        for m in members:
            b = bricks.get(m.lower())
            if b is None or b["status"] != "available":
                missing.append(m)
                continue
            if b["id"].lower() in CORE_IDS:
                continue  # the start console is already here, and a stack never removes it
            for step in b["add_steps"]:
                if step[0] != "open" and step not in add:
                    add.append(step)
            for step in b["remove_steps"]:
                if step not in undo:
                    undo.append(step)
        gap = ""
        if missing:
            gap = "adds the parts that are ready; not addable yet: " + ", ".join(missing[:6]) + (
                f" +{len(missing) - 6} more" if len(missing) > 6 else "")
        rows.append(_row(
            id=str(s["id"]), kind="stack", title=_plain(s.get("name") or s["id"], 80),
            what=(str(s.get("name")) if len(str(s.get("name") or "")) >= 24 else
                  f"{s.get('name') or s['id']}: {len(members)} parts that work together "
                  f"({', '.join(members[:4])}{' ...' if len(members) > 4 else ''})"),
            requires=members, source="config/ecosystem.yaml#stacks",
            status="available" if add else "listed",
            gap=gap if add else (gap or "none of its parts has a one-step add yet"),
            add_steps=add, remove_steps=undo,
            see="aither find " + str(s["id"]), public=True))
    return rows


def _integration_rows(bricks: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    doc = _yaml(_find("config/integrations_catalogue.yaml"))
    rows: List[Dict[str, Any]] = []
    for it in (doc.get("integrations") if isinstance(doc, dict) else []) or []:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        iid = str(it["id"])
        brick = bricks.get(str(it.get("brick") or "").lower())
        if brick is not None:
            # The integration IS a brick: one row, the brick's. This row only lends it
            # the words a person types ("omnishell", "tunnel") and what proves it is here.
            if iid.lower() != brick["id"].lower() and iid not in brick["aliases"]:
                brick["aliases"].append(iid)
            for exe in it.get("detect") or []:
                if exe not in brick["detect"]:
                    brick["detect"].append(str(exe))
            continue
        declared = it.get("add_by_os") if isinstance(it.get("add_by_os"), dict) else {}
        plats = [str(p) for p in (it.get("platforms") or [])]
        by_os: Dict[str, Dict[str, Any]] = {}
        for name in ("windows", "macos", "linux"):
            line = declared.get(name) or (
                it.get("add") if (not plats or name in plats) else "")
            add, undo, _why = parse_install_line(line) if line else ([], [], "")
            if add:
                by_os[name] = {"add": add, "remove": undo}
        detect = [str(e) for e in (it.get("detect") or [])]
        rows.append(_row(
            id=iid, kind="integration", title=str(it.get("name") or iid),
            what=it.get("what") or "", requires=plats, source="", by_os=by_os,
            missing=str(it.get("missing") or "listed, add flow not built"),
            verified=str(it.get("verified_on") or ""), detect=detect,
            see=(detect[0] + " is on your PATH") if detect else "", public=True))
    return rows


def _os_key() -> str:
    forced = os.environ.get("AITHER_CATALOGUE_OS", "").strip().lower()
    if forced in ("windows", "macos", "linux"):
        return forced
    if sys.platform.startswith("win"):
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def _money(cents: Any, ptype: str, period: Any) -> str:
    try:
        c = int(cents or 0)
    except (TypeError, ValueError):
        return ""
    if c <= 0:
        return "free"
    amount = f"${c / 100:.2f}"
    if ptype == "subscription":
        return f"{amount}/{period or 'month'}"
    return f"{amount} once"


def _product_rows() -> List[Dict[str, Any]]:
    seed = _yaml(_find("config/shop_catalog_seed.yaml"))
    if not isinstance(seed, dict):
        return []
    core = [str(x) for x in ((seed.get("storefront") or {}).get("core") or [])]
    by_id = {str(p.get("id")): p for p in (seed.get("products") or []) if isinstance(p, dict)}
    billing = _yaml(_find("config/product_billing.yaml"))
    skus: Dict[str, Any] = {}
    if isinstance(billing, dict):
        skus = (((billing.get("products") or {}).get("shop") or {}).get("skus") or {})
    rows: List[Dict[str, Any]] = []
    for pid in core:
        p = by_id.get(pid)
        if not isinstance(p, dict) or str(p.get("status")) != "active":
            continue
        slug = str(p.get("slug") or pid)
        url = f"https://shop.aitherium.com/shop/{slug}"
        price = _money(p.get("price_cents"), str(p.get("type") or ""), p.get("billing_period"))
        sku = skus.get(pid) if isinstance(skus.get(pid), dict) else {}
        setup = [str(x) for x in (sku.get("setup") or []) if str(x).strip()]
        # Run only what needs no answer from the person: the lines before the first
        # note, minus the self-upgrade (adk is what is running).
        auto: List[List[str]] = []
        later: List[str] = []
        noted = False
        for line in setup:
            if line.startswith("#"):
                noted = True
            if noted or line.startswith("pip install") or not line.startswith("adk "):
                later.append(line.lstrip("# ").strip() if line.startswith("#") else line)
                continue
            auto.append(line.split())
        paid = price not in ("free", "")
        add: List[List[str]] = []
        if paid:
            add.append(["open", url])
        add.extend(auto)
        if not paid and not auto:
            add = [["open", url]]
        rows.append(_row(
            id=slug, kind="product", title=str(p.get("name") or pid),
            what=_first_sentence(p.get("description") or ""),
            requires=["signed in with the email you paid with"] if paid else [],
            price=price or "see the product page", source=url,
            status="paid" if paid else "available", add_steps=add, remove_steps=[],
            remove_note=("your purchase stays on your account; manage it at "
                         "https://shop.aitherium.com/manage") if paid else "",
            see=("adk home status" if auto and auto[-1][:2] == ["adk", "home"] else url),
            next_steps=later[:6],
            packs=[str(x) for x in (sku.get("license_packs") or [])], public=True,
            aliases=[_SAFE_ID.sub("-", w.lower()) for w in str(p.get("name") or "").split()
                     if len(w) > 4 and w.lower() not in ("aither", "aitherium")][:2]))
    # A word two products share ("credits", "world") is not a name for either.
    counts: Dict[str, int] = {}
    for r in rows:
        for a in r["aliases"]:
            counts[a] = counts.get(a, 0) + 1
    for r in rows:
        r["aliases"] = [a for a in r["aliases"] if counts[a] == 1]
    return rows


def _toolpack_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    root = _find("Library/packs")
    if root is None or not root.is_dir():
        return rows
    for manifest in sorted(root.glob("*/.toolpack.yaml")):
        doc = _yaml(manifest)
        if not isinstance(doc, dict):
            continue
        pid = str(doc.get("id") or manifest.parent.name)
        tools = doc.get("mcp_tools") or doc.get("tools") or []
        rows.append(_row(
            id=pid, kind="pack", title=str(doc.get("name") or pid),
            what=_first_sentence(doc.get("description") or ""),
            requires=["signed in", "a workspace"],
            size=f"{len(tools)} tools" if isinstance(tools, list) and tools else "",
            source=f"Library/packs/{manifest.parent.name}",
            add_steps=[["adk", "pack", "enable", pid]],
            remove_steps=[["adk", "pack", "disable", pid]],
            see=f"adk pack info {pid}"))
    return rows


def _skill_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    shelf: Optional[Path] = None
    repo = _repo_root()
    if repo is not None and (repo / "awskills" / "skills").is_dir():
        shelf = repo / "awskills" / "skills"
    if shelf is None:
        return rows
    front = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
    for md in sorted(shelf.glob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        desc = ""
        m = front.match(text)
        if m:
            try:
                import yaml
                desc = str((yaml.safe_load(m.group(1)) or {}).get("description") or "")
            except Exception:  # noqa: BLE001 - bad frontmatter still lists by name
                desc = ""
        rows.append(_row(
            id=f"skill:{md.stem}", kind="skill", title=md.stem,
            what=_first_sentence(desc), size="1 file",
            source=f"https://github.com/Aitherium/awskills/blob/main/skills/{md.name}",
            add_steps=[["adk", "skills", "install", md.stem]],
            remove_steps=[["adk", "skills", "remove", md.stem]],
            see=f"~/.claude/skills/{md.stem}/SKILL.md", public=True))
    return rows


def _component_rows(bricks: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for comp in INSTALLER_COMPONENTS:
        rows.append(_row(
            id=comp["id"], kind=comp["kind"], title=comp["title"], what=comp["what"],
            requires=list(comp.get("requires") or []),
            source="https://aitherium.com/install",
            add_steps=[["installer", comp["component"]]],
            remove_steps=[list(s) for s in (comp.get("remove_steps") or [])],
            remove_note=comp.get("remove_note") or "", see=comp.get("see") or "",
            public=True, aliases=[comp["brick"]] if comp.get("brick") and
            comp["brick"].lower() != comp["id"] else []))
    return rows


def _folded_rows() -> List[Dict[str, Any]]:
    """adk.catalog's own entries, as person rows (their add stays adk.catalog's)."""
    from adk import catalog as _catalog
    rows: List[Dict[str, Any]] = []
    for item in _catalog.load_catalog():
        kind = _FOLD.get(item["kind"])
        if kind is None:
            continue
        pricing = item.get("pricing") or {}
        price = "free"
        if pricing.get("tier") not in (None, "free"):
            plan = (item.get("requires") or {}).get("min_plan") or pricing.get("tier")
            price = f"needs the {plan} plan"
        status = "available" if item.get("status") == "available" else "listed"
        gap = "; ".join(str(b) for b in (item.get("blockers") or [])) or (
            "" if status == "available" else f"{item.get('status')} in the catalog")
        rows.append(_row(
            id=item["id"], kind=kind, title=item["name"],
            what=item.get("what_it_does") or item.get("summary") or "",
            price=price, source=str((item.get("install") or {}).get("url") or
                                    (item.get("trust") or {}).get("source") or ""),
            status=status, gap=_plain(gap, 120),
            add_steps=[["adk", "catalog", "install", item["id"], "--target", "workspace"]]
            if status == "available" else [],
            remove_steps=[], see=f"adk catalog get {item['id']}"))
    return rows


def build_rows(include_folded: bool = True) -> List[Dict[str, Any]]:
    """Every row, from the sources on disk. Empty when there is no AitherOS tree."""
    eco = _ecosystem()
    if not eco:
        return []
    bricks = _brick_rows(eco)
    by_lower = {b["id"].lower(): b for b in bricks}
    rows: List[Dict[str, Any]] = []
    components = _component_rows(by_lower)
    taken = {c["id"].lower() for c in components}
    for c in components:
        for alias in c["aliases"]:
            taken.add(alias.lower())
    integrations = _integration_rows(by_lower)
    rows.extend(components)
    rows.extend(b for b in bricks if b["id"].lower() not in taken)
    rows.extend(_stack_rows(eco, by_lower))
    rows.extend(integrations)
    rows.extend(_product_rows())
    rows.extend(_toolpack_rows())
    rows.extend(_skill_rows())
    if include_folded:
        rows.extend(_folded_rows())
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r["id"] and r["id"].lower() not in seen:
            seen[r["id"].lower()] = r
    return sorted(seen.values(), key=lambda r: (KINDS.index(r["kind"]), r["id"].lower()))


def load_rows() -> List[Dict[str, Any]]:
    """The rows for this machine: the live sources when a tree is here, else the snapshot."""
    rows = build_rows()
    if rows:
        return rows
    try:
        doc = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    rows = [_row(**r) for r in (doc.get("entries") or []) if isinstance(r, dict) and r.get("id")]
    have = {r["id"].lower() for r in rows}
    rows.extend(r for r in _folded_rows() if r["id"].lower() not in have)
    return rows


# ── installed? ───────────────────────────────────────────────────────────────────────

def is_installed(row: Dict[str, Any]) -> bool:
    """Measured here, now: a lock row, a named executable, or the pip distribution."""
    try:
        from adk.addon_lock import lookup
        if lookup(row["id"], "catalogue") is not None:
            return True
    except Exception as exc:  # noqa: BLE001 - an unreadable lock is "not recorded"
        logger.debug("add-on lock unreadable: %s", exc)
    for exe in row.get("detect") or []:
        if shutil.which(str(exe)):
            return True
    for step in row.get("add_steps") or []:
        if step and step[0] == "pip" and len(step) >= 3:
            try:
                from importlib import metadata
                metadata.version(step[2].split("[", 1)[0])
                return True
            except Exception:  # noqa: BLE001 - PackageNotFoundError and friends
                return False
        if step and step[0] == "npm" and len(step) >= 4:
            return bool(shutil.which(step[3].rsplit("/", 1)[-1]))
    return False


# ── find ─────────────────────────────────────────────────────────────────────────────

def _hay(row: Dict[str, Any]) -> str:
    return " ".join([row["id"], row["title"], row["what"], row["kind"],
                     " ".join(row.get("aliases") or [])]).lower()


def resolve(ref: str, rows: Optional[List[Dict[str, Any]]] = None
            ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """One row for what a person typed, or (None, the candidates)."""
    rows = load_rows() if rows is None else rows
    want = str(ref or "").strip().lower()
    if not want:
        return None, []
    for r in rows:
        if r["id"].lower() == want:
            return r, []
    for probe in ("aw" + want,):
        for r in rows:
            if r["id"].lower() == probe:
                return r, []
    hits = [r for r in rows if want in [a.lower() for a in (r.get("aliases") or [])]]
    if len(hits) == 1:
        return hits[0], []
    if not hits:
        hits = [r for r in rows if r["id"].lower().split(":", 1)[-1] == want]
        if len(hits) == 1:
            return hits[0], []
    if not hits:
        hits = [r for r in rows
                if want in re.split(r"[^a-z0-9]+", r["title"].lower())]
        if len(hits) == 1:
            return hits[0], []
    if not hits:
        hits = [r for r in rows if want in _hay(r)]
    return None, hits


def find_entries(words: str = "", kind: Optional[str] = None, limit: int = LIST_CAP,
                 rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Ranked rows for what a person typed: ``{items, more, total, next}``.

    ``items`` is capped (five by default) and ``total`` is the real count, so a surface
    can say "+N more" without ever dropping a row silently. ``next`` is the one thing
    to do now, computed from the result.
    """
    rows = load_rows() if rows is None else rows
    q = " ".join(str(words or "").lower().split())
    terms = [t for t in q.split() if len(t) > 1]
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for r in rows:
        if kind and r["kind"] != kind:
            continue
        score = 0.0
        if terms:
            hay = _hay(r)
            ident = r["id"].lower()
            names = [ident, ident.split(":", 1)[-1], "aw" + q] + \
                [a.lower() for a in (r.get("aliases") or [])]
            for t in terms:
                if t in hay:
                    score += 1
                if t in ident or t in r["title"].lower():
                    score += 2
            if q in names or ("aw" + q) == ident:
                score += 10
            if q == ident:
                score += 2
            if score == 0:
                continue
        else:
            # Nothing typed: lead with what a bare install left out, then the shop.
            first = (r.get("add_steps") or [[""]])[0][0]
            score = 3.0 if first == "installer" else {"product": 2.0, "brick": 1.0}.get(
                r["kind"], 0.0)
        if r["status"] != "listed":
            score += 0.5  # something you can add outranks something you can only read about
        scored.append((score, r))
    scored.sort(key=lambda p: (-p[0], KINDS.index(p[1]["kind"]), p[1]["id"].lower()))
    total = len(scored)
    cap = max(1, int(limit or LIST_CAP))
    items = []
    for _score, r in scored[:cap]:
        view = {k: r.get(k, "") for k in PERSON_ROW}
        view["installed"] = is_installed(r)
        items.append(view)
    if not items:
        nxt = "Nothing matched. Try fewer words, or list a kind: aither find --kind brick"
    else:
        top = items[0]
        if top["installed"]:
            nxt = f"{top['id']} is already here. See it: {top['see'] or 'aither find ' + top['id']}"
        elif top["status"] == "listed":
            nxt = f"{top['id']} cannot be added yet ({top['gap']})."
        else:
            nxt = f"Add the first one: aither add {top['id']}"
    return {"ok": True, "query": q, "items": items, "more": max(0, total - len(items)),
            "total": total, "next": nxt}


# ── add / remove ─────────────────────────────────────────────────────────────────────

Runner = Callable[[List[str]], Tuple[int, str]]


def _src_dir() -> Path:
    return Path(os.environ.get("AITHER_CATALOGUE_SRC", "") or
                (Path.home() / ".aither" / "catalogue" / "src"))


def _argv(step: List[str]) -> Optional[List[str]]:
    """The real command for one step, or None when it is not something to execute."""
    head, rest = step[0], [s.replace("{src}", str(_src_dir())) for s in step[1:]]
    if head == "pip":
        return [sys.executable, "-m", "pip", *rest]
    if head == "adk":
        return [sys.executable, "-m", "adk", *rest]
    if head == "installer":
        base = os.environ.get("AITHER_INSTALL_BASE", "https://aitherium.com").rstrip("/")
        if _os_key() == "windows":
            shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                    f"& ([scriptblock]::Create((irm {base}/install.ps1))) -Add {rest[0]}"]
        return ["sh", "-c", f"curl -fsSL {base}/install.sh | sh -s -- --add {rest[0]}"]
    if head in ("npm", "git", "winget"):
        exe = shutil.which(head + ".cmd") or shutil.which(head) if head == "npm" \
            else shutil.which(head)
        return [exe or head, *rest]
    return None


def _show(step: List[str]) -> str:
    if step[0] == "open":
        return step[1]
    if step[0] == "installer":
        return f"the Aitherium installer, only the '{step[1]}' part"
    if step[0] == "rmtree":
        return "delete " + step[1].replace("{src}", str(_src_dir()))
    return " ".join(s.replace("{src}", str(_src_dir())) for s in step)


def _run(argv: List[str]) -> Tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800,
                              encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        return 127, f"{argv[0]} is not installed on this computer"
    except subprocess.TimeoutExpired:
        return 124, "timed out after 30 minutes"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return proc.returncode, (tail[-1] if tail else "")


def _result(ok: bool, row: Optional[Dict[str, Any]], ref: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": ok, "id": (row or {}).get("id", ref),
                           "title": (row or {}).get("title", ""), "did": [], "detail": "",
                           "see": "", "undo": "", "next": "", "exit_code": 0 if ok else 1}
    out.update(extra)
    return out


def add_entry(ref: str, *, dry_run: bool = False, runner: Optional[Runner] = None,
              rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Add one catalogue entry. Says what it installed, how to see it, how to undo it.

    Never raises. ``dry_run`` plans without running or recording. A step that fails stops
    the add, and the steps that already ran are undone, so a failed add leaves nothing.
    """
    row, candidates = resolve(ref, rows)
    if row is None:
        names = [c["id"] for c in candidates[:LIST_CAP]]
        more = max(0, len(candidates) - len(names))
        if names:
            detail = (f"'{ref}' matches {len(candidates)} entries - pick one: "
                      + ", ".join(names) + (f" +{more} more" if more else ""))
            return _result(False, None, ref, detail=detail, exit_code=2,
                           next=f"aither add {names[0]}")
        return _result(False, None, ref, detail=f"nothing in the catalogue is called '{ref}'",
                       exit_code=1, next=f"aither find {ref}")
    if row["status"] == "listed":
        return _result(False, row, ref, exit_code=1,
                       detail=f"{row['id']} is listed but cannot be added yet - {row['gap']}",
                       next=f"aither find {row['kind']}")
    steps = [list(s) for s in row.get("add_steps") or []]
    plan = [_show(s) for s in steps]
    if dry_run:
        return _result(True, row, ref, did=plan, see=row.get("see", ""),
                       undo=row.get("remove") or row.get("remove_note", ""),
                       detail="dry run - nothing was installed",
                       next=f"aither add {row['id']}")
    run = runner or _run
    done: List[List[str]] = []
    did: List[str] = []
    opened = ""
    for step in steps:
        if step[0] == "open":
            opened = step[1]
            continue
        argv = _argv(step)
        if argv is None:
            continue
        rc, tail = run(argv)
        if rc != 0:
            for prior in reversed(_undo_for(row, done)):
                undo_argv = _argv(prior)
                if undo_argv is not None:
                    run(undo_argv)
                elif prior[0] == "rmtree":
                    _rmtree(prior[1])
            return _result(False, row, ref, did=did, exit_code=1,
                           detail=f"1 step failed - {_show(step)}: {tail or 'exit ' + str(rc)}"
                           + (" - what had been added was removed again" if done else ""),
                           next=f"fix that, then: aither add {row['id']}")
        done.append(step)
        did.append(_show(step))
    if row["status"] == "paid" and opened and did:
        nxt = (f"The free part is set up. The purchase ({row['price']}) unlocks the rest: "
               f"{opened} - then sign in with the email you paid with")
    elif row["status"] == "paid" and opened:
        nxt = (f"Buy it ({row['price']}): {opened} - your receipt carries the download "
               "and the setup steps")
    elif opened and not did:
        nxt = f"Open: {opened}"
    else:
        later = row.get("next_steps") or []
        nxt = later[0] if later else (row.get("see") or f"aither find {row['id']}")
    if did or not opened:
        _record(row, done)
    return _result(True, row, ref, did=did, see=row.get("see", ""),
                   undo=row.get("remove") or row.get("remove_note", ""),
                   detail=(f"{row['title']} is added" if did else
                           f"{row['title']} is not something to install"),
                   next=nxt, steps_after=list(row.get("next_steps") or []))


def _undo_for(row: Dict[str, Any], done: List[List[str]]) -> List[List[str]]:
    """The undo steps that correspond to the add steps that actually ran."""
    undo = [list(s) for s in row.get("remove_steps") or []]
    all_add = [s for s in row.get("add_steps") or [] if s[0] != "open"]
    if len(done) >= len(all_add):
        return undo
    out: List[List[str]] = []
    for step in done:
        u: List[List[str]] = []
        if step[0] in ("pip", "npm", "winget"):
            _a, u, _w = parse_install_line(" ".join(step))
        if step[0] == "git" and len(step) >= 2:
            u = [["rmtree", step[-1]]]
        out.extend(u)
    return out


def _rmtree(target: str) -> None:
    """Delete a folder THIS module cloned: only ever inside the catalogue src dir."""
    root = _src_dir().resolve()
    path = Path(target.replace("{src}", str(_src_dir()))).resolve()
    if root == path or root not in path.parents:
        return
    shutil.rmtree(path, ignore_errors=True)


def _record(row: Dict[str, Any], done: List[List[str]]) -> None:
    try:
        from adk.addon_lock import record_install
        body = json.dumps({"add": done, "remove": row.get("remove_steps") or []},
                          sort_keys=True)
        record_install("catalogue", row["id"], version="", source=str(row.get("source") or ""),
                       sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                       trust="official", hash_of="manifest", dest=str(row.get("see") or ""),
                       extra={"remove_steps": row.get("remove_steps") or [],
                              "remove_note": row.get("remove_note") or "",
                              "title": row.get("title") or ""})
    except Exception as exc:  # noqa: BLE001 - the add happened; say the record did not
        logger.warning("added %s but could not record it in the add-on lock: %s",
                       row.get("id"), exc)


def remove_entry(ref: str, *, dry_run: bool = False, runner: Optional[Runner] = None,
                 rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Undo an add: run the recorded undo steps and drop the lock row."""
    row, candidates = resolve(ref, rows)
    recorded: Optional[Dict[str, Any]] = None
    try:
        from adk.addon_lock import forget_install, lookup
        recorded = lookup(row["id"] if row else ref, "catalogue")
    except Exception:  # noqa: BLE001
        forget_install = None  # type: ignore[assignment]
    if row is None and recorded is None:
        names = ", ".join(c["id"] for c in candidates[:LIST_CAP])
        return _result(False, None, ref, exit_code=2 if names else 1,
                       detail=(f"'{ref}' matches more than one entry - pick one: {names}"
                               if names else f"nothing called '{ref}' is in the catalogue"),
                       next=f"aither find {ref}")
    ident = row["id"] if row else str(ref)
    if ident.lower() in CORE_IDS:
        return _result(False, row, ref, exit_code=1,
                       detail=f"{ident} is the start console itself and stays",
                       next="to remove everything: pip uninstall awdk ; "
                            "npm uninstall -g @aitherium/awsh")
    extra = (recorded or {}).get("extra") or {}
    steps = [list(s) for s in (extra.get("remove_steps") or (row or {}).get("remove_steps") or [])]
    note = str(extra.get("remove_note") or (row or {}).get("remove_note") or "")
    if not steps and not note:
        return _result(False, row, ref, exit_code=1,
                       detail=f"{ident} has no remove flow recorded",
                       next=f"aither find {ident}")
    plan = [_show(s) for s in steps]
    if dry_run:
        return _result(True, row, ref, did=plan, detail="dry run - nothing was removed",
                       next=note or f"aither remove {ident}")
    run = runner or _run
    did: List[str] = []
    for step in steps:
        if step[0] == "rmtree":
            _rmtree(step[1])
            did.append(_show(step))
            continue
        argv = _argv(step)
        if argv is None:
            continue
        rc, tail = run(argv)
        if rc != 0:
            return _result(False, row, ref, did=did, exit_code=1,
                           detail=f"1 step failed - {_show(step)}: {tail or 'exit ' + str(rc)}",
                           next=f"fix that, then: aither remove {ident}")
        did.append(_show(step))
    if recorded is not None and forget_install is not None:
        try:
            forget_install("catalogue", ident)
        except Exception as exc:  # noqa: BLE001 - removed; only the record is stale
            logger.warning("removed %s but its add-on lock row stayed: %s", ident, exc)
    return _result(True, row, ref, did=did,
                   detail=f"{(row or {}).get('title') or ident} is removed" if did else
                   f"{ident}: nothing on this computer to remove",
                   next=note or f"add it back any time: aither add {ident}")


# ── rendering (ux-shape: next action first, five rows, real total) ───────────────────

def render_find(res: Dict[str, Any]) -> str:
    lines = [f"Next: {res['next']}"]
    shown = len(res["items"])
    lines.append(f"Catalogue: {res['total']} match{'' if res['total'] == 1 else 'es'}"
                 + (f" for '{res['query']}'" if res.get("query") else "")
                 + (f", showing {shown}" if res["more"] else ""))
    for i, it in enumerate(res["items"], 1):
        state = "installed" if it["installed"] else (
            "not addable yet" if it["status"] == "listed" else it["price"])
        lines.append(f"  {i}. {it['id']}  [{it['kind']}]  {state}")
        lines.append(f"     {it['what'] or it['title']}")
        if it["status"] == "listed" and it["gap"]:
            lines.append(f"     why: {it['gap']}")
    if res["more"]:
        lines.append(f"  +{res['more']} more - see them: aither find "
                     f"{res.get('query') or ''} --all".replace("  --all", " --all"))
    return "\n".join(lines)


def render_result(res: Dict[str, Any], verb: str = "add") -> str:
    if not res["ok"]:
        return f"Not {verb}ed: {res['detail']}\nNext: {res['next']}"
    lines = [f"{res['detail'][0].upper()}{res['detail'][1:]}."]
    for i, d in enumerate(res.get("did") or [], 1):
        lines.append(f"  {i}. {d}")
    if verb == "add":
        if res.get("see"):
            lines.append(f"See it: {res['see']}")
        if res.get("undo"):
            lines.append(f"Undo: {res['undo']}")
    lines.append(f"Next: {res['next']}")
    return "\n".join(lines)
