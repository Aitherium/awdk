"""Tool-pack loader — discover, license-gate, and activate installed tool packs
by name onto an ADK agent.

This is the backbone of "activate an installed pack": ``register_tool_packs``
(adk/builtin_tools.py) and ``AitherAgent._collect_pack_persona_fragments``
(adk/agent.py) both import ``get_tool_pack_loader`` from here. Until now this
module did not exist, so both silently no-op'd (the ImportError branch) and NO
pack could ever be activated by name — only by direct ``from ... import tools;
register()``. This module makes activation-by-name real.

A "tool pack" is a directory containing a ``.toolpack.yaml`` manifest and a
Python package exposing ``register(registry) -> int`` (see
``lib/agents/packs/aitherbrowser`` / ``.../structuredml``). Discovery scans, in
order: the AITHER_TOOLPACK_DIRS env (os.pathsep-separated), any importable
``lib.agents.packs`` package, the marketplace install dir ``~/.aitheros/packs``,
and any explicit ``extra_dirs``.

License gating is intentionally LENIENT: a pack loads unless it declares a hard
gate (``require_all_entitlements: true`` with an unmet entitlement, or a
``min_tier`` above the active tier). Per-tool entitlement gating lives inside
each pack's ``register()`` (e.g. AitherBrowser registers free ``web_*`` tools
unconditionally and ``pilot_*`` only when entitled) — so blocking a whole pack
at the loader would wrongly suppress its free tier.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TIER_ORDER = ["community", "free", "builder", "professional", "enterprise", "internal"]


@dataclass
class ToolPackManifest:
    """Parsed ``.toolpack.yaml`` + where it lives + how to import it."""
    id: str
    path: Path
    name: str = ""
    version: str = ""
    description: str = ""
    category: str = "tool_packs"
    tier: str = "free"
    tool_modules: list[str] = field(default_factory=list)
    entitlements: list[str] = field(default_factory=list)
    require_all_entitlements: bool = False
    min_tier: str = ""
    persona_fragments: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    icon: str = ""
    tags: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    deprecated: bool = False
    redirect_to: str = ""
    pricing: dict = field(default_factory=dict)
    # Optional pack-provided console UI: {"assets_dir": "ui", "tabs": [{"id",
    # "title", "entry", "icon"}]}. Assets are served by the agent server and
    # mounted in the admin console as a sandboxed iframe (never same-origin).
    ui: dict = field(default_factory=dict)
    # Optional MCP server configuration for the pack (when the pack itself
    # exposes tools via an MCP server, not just Python register()).
    mcp_server: dict = field(default_factory=dict)

    def tool_matches(self, tool_name: str) -> bool:
        """True if *tool_name* falls under this pack's declared ``mcp_tools``
        patterns (trailing-``*`` glob or exact name). This is the authoritative
        ownership check for the console's pack-scoped tool invocation."""
        for pat in self.mcp_tools:
            if pat.endswith("*"):
                if pat[:-1] and tool_name.startswith(pat[:-1]):
                    return True
            elif tool_name == pat:
                return True
        return False

    @property
    def ui_tabs(self) -> list[dict]:
        """Normalized pack UI tab declarations (empty when the pack has no UI)."""
        tabs = self.ui.get("tabs") if isinstance(self.ui, dict) else None
        out = []
        for t in tabs or []:
            if not isinstance(t, dict):
                continue
            entry = str(t.get("entry") or "index.html")
            out.append({
                "id": str(t.get("id") or "main"),
                "title": str(t.get("title") or self.name or self.id),
                "entry": entry,
                "icon": str(t.get("icon") or self.icon or ""),
            })
        return out

    @property
    def ui_assets_dir(self) -> Path | None:
        """Absolute, resolved assets dir for the pack UI, or None if undeclared."""
        if not isinstance(self.ui, dict) or not self.ui:
            return None
        rel = str(self.ui.get("assets_dir") or "ui").strip().replace("\\", "/")
        # A manifest must not point the asset server outside its own pack dir.
        if rel.startswith(("/", "~")) or ".." in rel.split("/"):
            return None
        try:
            base = (self.path / rel).resolve()
            if base.is_relative_to(self.path.resolve()):
                return base
        except (OSError, ValueError):
            pass
        return None

    @property
    def package_candidates(self) -> list[str]:
        """Dotted paths that MIGHT expose ``register()``, most-specific first.
        Handles both pack conventions: ``tool_modules`` pointing at a submodule
        (register lives in the parent package's __init__, e.g. aitherbrowser) OR
        at the package itself (register in that package, e.g. deep_research)."""
        out: list[str] = []
        for head in self.tool_modules:
            if head and head not in out:
                out.append(head)                      # the module as given
            parent = head.rsplit(".", 1)[0] if "." in head else head
            if parent and parent not in out:
                out.append(parent)                    # its parent package
        return out


def _default_dirs(extra_dirs: list[Path] | None) -> list[Path]:
    dirs: list[Path] = []
    # 1. explicit env override(s)
    env = os.environ.get("AITHER_TOOLPACK_DIRS", "")
    for d in env.split(os.pathsep):
        if d.strip():
            dirs.append(Path(d.strip()))
    # 2. the importable lib.agents.packs package (the AitherOS in-repo packs).
    # Present only inside the monorepo; when it is, its live source wins over the
    # bundled snapshot below (discover() is first-dir-wins).
    try:
        spec = importlib.util.find_spec("lib.agents.packs")
        for loc in (spec.submodule_search_locations or []) if spec else []:
            dirs.append(Path(loc))
    except Exception:
        pass
    # 3. packs bundled inside the wheel (adk/toolpacks/) — these ship with the
    # SDK so a bare `pip install awdk` has arc-brainpack, deep_research and
    # cloudflare available with no monorepo checkout or marketplace download.
    dirs.append(Path(__file__).resolve().parent / "toolpacks")
    # 4. marketplace-installed packs
    dirs.append(Path.home() / ".aitheros" / "packs")
    # 4. caller-supplied
    for d in extra_dirs or []:
        dirs.append(Path(d))
    # dedup, preserve order, keep only existing dirs
    seen, out = set(), []
    for d in dirs:
        try:
            rd = d.resolve()
        except Exception:
            continue
        if rd in seen or not rd.is_dir():
            continue
        seen.add(rd)
        out.append(rd)
    return out


class ToolPackLoader:
    def __init__(self, extra_dirs: list[Path] | None = None, enforce_entitlements: bool = True):
        """Initialize the loader with optional entitlement enforcement.

        Args:
            extra_dirs: Extra directories to search for packs.
            enforce_entitlements: If True (default), fail-closed during discovery:
                skip packs whose entitlements are not met. If False (sovereign mode),
                load packs even if their entitlements are unmet, deferring the gate to
                per-tool checks inside each pack's register(). Default True enforces
                fail-closed behavior for hosted/multi-tenant deployments.
        """
        self.dirs = _default_dirs(extra_dirs)
        self._manifests: dict[str, ToolPackManifest] = {}
        self._discovered = False
        self.enforce_entitlements = enforce_entitlements

    # -- discovery -------------------------------------------------------------
    def discover(self) -> dict[str, ToolPackManifest]:
        """Scan the search dirs for ``<child>/.toolpack.yaml``. Idempotent.

        Applies fail-closed entitlement gating if enforce_entitlements is True:
        packs whose required entitlements are not met raise RuntimeError.
        """
        if self._discovered:
            return self._manifests
        for base in self.dirs:
            try:
                children = list(base.iterdir())
            except Exception:
                continue
            for child in children:
                mf_path = child / ".toolpack.yaml"
                if not (child.is_dir() and mf_path.is_file()):
                    continue
                m = self._parse(mf_path, child)
                if not m:
                    continue
                if m.id in self._manifests:  # first dir wins
                    continue

                # Fail-closed entitlement gate during discovery
                if self.enforce_entitlements:
                    allowed, reason = self.check_license(m)
                    if not allowed:
                        raise RuntimeError(
                            f"pack {m.id!r} failed entitlement gate: {reason}"
                        )

                self._manifests[m.id] = m
        self._discovered = True
        logger.info("tool_pack_loader: discovered %d packs across %d dirs: %s",
                    len(self._manifests), len(self.dirs), sorted(self._manifests))
        return self._manifests

    @staticmethod
    def _parse(mf_path: Path, pack_dir: Path) -> ToolPackManifest | None:
        try:
            import yaml
            data = yaml.safe_load(mf_path.read_text("utf-8")) or {}
        except Exception as exc:
            logger.warning("tool_pack_loader: bad manifest %s: %s", mf_path, exc)
            return None
        pid = str(data.get("id") or pack_dir.name).strip()
        if not pid:
            return None

        def _list(key):
            v = data.get(key) or []
            return [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]

        ui_block = data.get("ui")
        pricing = data.get("pricing")
        mcp_server_block = data.get("mcp_server")
        # Manifests use either legacy singular `entitlement:` or list `entitlements:`.
        entitlements = _list("entitlements")
        legacy = str(data.get("entitlement") or "").strip()
        if legacy and legacy not in entitlements:
            entitlements.append(legacy)
        return ToolPackManifest(
            id=pid, path=pack_dir, name=str(data.get("name") or pid),
            version=str(data.get("version") or ""), description=str(data.get("description") or ""),
            category=str(data.get("category") or "tool_packs"),
            tier=str(data.get("tier") or "free").lower(),
            tool_modules=_list("tool_modules"), entitlements=entitlements,
            require_all_entitlements=bool(data.get("require_all_entitlements", False)),
            min_tier=str(data.get("min_tier") or "").lower(),
            persona_fragments=_list("persona_fragments"), mcp_tools=_list("mcp_tools"),
            icon=str(data.get("icon") or ""), tags=_list("tags"), skills=_list("skills"),
            deprecated=bool(data.get("deprecated", False)),
            redirect_to=str(data.get("redirect_to") or ""),
            pricing=pricing if isinstance(pricing, dict) else {},
            ui=ui_block if isinstance(ui_block, dict) else {},
            mcp_server=mcp_server_block if isinstance(mcp_server_block, dict) else {})

    def load_packs(self, pack_ids: list[str] | None = None) -> list[ToolPackManifest]:
        self.discover()
        if pack_ids is None:
            return list(self._manifests.values())
        out = []
        for pid in pack_ids:
            m = self._manifests.get(pid)
            if m is None:
                logger.warning("tool_pack_loader: pack %r not found (have: %s)",
                               pid, sorted(self._manifests))
            else:
                out.append(m)
        return out

    # -- license gate (fail-closed at discovery when enforce_entitlements=True;
    # per-tool gating lives in each pack) ------
    def check_license(self, manifest: ToolPackManifest) -> tuple[bool, str]:
        # hard tier gate
        if manifest.min_tier and manifest.min_tier in _TIER_ORDER:
            tier = self._active_tier()
            if _TIER_ORDER.index(tier) < _TIER_ORDER.index(manifest.min_tier):
                return False, f"requires tier '{manifest.min_tier}', active '{tier}'"
        # hard "must have ALL listed entitlements" gate (opt-in)
        if manifest.require_all_entitlements and manifest.entitlements:
            missing = [e for e in manifest.entitlements if not self._entitled(e)]
            if missing:
                return False, f"missing entitlements: {', '.join(missing)}"
        # default: allow — the pack's register() self-gates its premium tools
        return True, ""

    @staticmethod
    def _entitled(capability: str) -> bool:
        try:
            from adk.licensing import get_license_manager
            return bool(getattr(get_license_manager().license.entitlements, capability, False))
        except Exception:
            return False

    @staticmethod
    def _active_tier() -> str:
        try:
            from adk.licensing import get_license_manager
            return str(getattr(get_license_manager().license, "tier", "community")).lower()
        except Exception:
            return "community"

    # -- activation ------------------------------------------------------------
    def register_on_adk_agent(self, manifest: ToolPackManifest, agent: Any) -> int:
        """Import the pack's package and call its ``register(registry) -> int``.
        Falls back to loading the pack's ``__init__.py`` by file path when its
        package is not on sys.path (marketplace-installed packs)."""
        registry = (getattr(agent, "_tools", None) or getattr(agent, "tools", None)
                    or getattr(agent, "tool_registry", None))
        if registry is None:
            logger.warning("tool_pack_loader: agent has no tool registry; cannot register %s",
                           manifest.id)
            return 0
        mod = self._import_pack(manifest)
        if mod is None:
            return 0
        reg = getattr(mod, "register", None)
        if not callable(reg):
            logger.warning("tool_pack_loader: pack %s has no register()", manifest.id)
            return 0
        try:
            n = reg(registry)
            return int(n or 0)
        except Exception as exc:
            logger.warning("tool_pack_loader: %s.register() failed: %s", manifest.id, exc)
            return 0

    @staticmethod
    def _import_pack(manifest: ToolPackManifest):
        # 1. normal import — try each candidate, return the first with register()
        first = None
        for cand in manifest.package_candidates:
            try:
                mod = importlib.import_module(cand)
            except Exception as exc:
                logger.debug("tool_pack_loader: import %s failed (%s)", cand, exc)
                continue
            if first is None:
                first = mod
            if callable(getattr(mod, "register", None)):
                return mod
        if first is not None:
            return first  # importable but no register() — caller reports it
        # 2. load __init__.py by file path (marketplace packs not on sys.path)
        init = manifest.path / "__init__.py"
        if not init.is_file():
            logger.warning("tool_pack_loader: %s has no importable package or __init__.py",
                           manifest.id)
            return None
        try:
            # Create spec as a PACKAGE (with submodule_search_locations) so that
            # relative imports within the pack (__init__.py → "from . import tools")
            # can resolve. Register in sys.modules BEFORE exec_module so Python
            # can find sibling modules during import.
            mod_name = f"_toolpack_{manifest.id}"
            spec = importlib.util.spec_from_file_location(
                mod_name, init,
                submodule_search_locations=[str(manifest.path)],
            )
            module = importlib.util.module_from_spec(spec)
            # Register in sys.modules BEFORE exec so relative imports can find siblings
            import sys
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            return module
        except Exception as exc:
            logger.warning("tool_pack_loader: file-load of %s failed: %s", manifest.id, exc)
            # Clean up sys.modules on failure: remove main module and all submodules
            try:
                import sys
                mod_name = f"_toolpack_{manifest.id}"
                # Remove the main module
                sys.modules.pop(mod_name, None)
                # Remove all submodules (e.g., _toolpack_myapp.tools, .config)
                to_remove = [key for key in sys.modules
                             if key.startswith(f"{mod_name}.")]
                for key in to_remove:
                    sys.modules.pop(key, None)
            except Exception:
                pass
            return None


# module-level cache so repeated get_tool_pack_loader() calls don't rescan disk.
_LOADERS: dict[tuple, ToolPackLoader] = {}


def get_tool_pack_loader(extra_dirs: list[Path] | None = None,
                         enforce_entitlements: bool = True) -> ToolPackLoader:
    """Return a discovered ToolPackLoader (cached by configuration).

    Args:
        extra_dirs: Extra directories to search for packs.
        enforce_entitlements: If True (default), fail-closed during discovery:
            skip packs whose entitlements are not met. If False (sovereign mode),
            load all discovered packs regardless of entitlements.

    Returns:
        A cached, discovered ToolPackLoader instance. The cache key includes both
        extra_dirs and enforce_entitlements to ensure configurations don't mix.
    """
    key = (tuple(str(d) for d in (extra_dirs or [])), bool(enforce_entitlements))
    loader = _LOADERS.get(key)
    if loader is None:
        loader = ToolPackLoader(extra_dirs=extra_dirs,
                               enforce_entitlements=enforce_entitlements)
        loader.discover()
        _LOADERS[key] = loader
    return loader
