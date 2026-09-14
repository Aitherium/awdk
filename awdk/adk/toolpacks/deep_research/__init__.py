"""Deep Research tool pack — wraps the deep-research engine that ships as an ADK
agent template (adk/templates/deep-research/engine) and exposes its full
workflow as dr_* agent tools, activatable by name via the tool-pack loader.

We do NOT duplicate the engine. The engine dir is hyphenated ("deep-research")
so it isn't importable by dotted path; we load it as a synthetic package
("_dr_engine") with submodule_search_locations so its internal relative imports
(`from . import aithersearch`, `from .ledger import ...`) resolve. Tools are
built session-scoped (build_research_tools(session)) exactly as the engine's
serve.py does, then registered with a dr_ prefix so nothing collides with
AitherBrowser's web_* tools.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("deep_research")

PACK_ID = "deep_research"
_ENGINE_PKG = "_dr_engine"
_session = None


def _engine_dir() -> Path:
    """Locate adk/templates/deep-research/engine relative to the installed adk."""
    override = os.environ.get("AITHER_DEEP_RESEARCH_ENGINE", "").strip()
    if override and Path(override).is_dir():
        return Path(override)
    import adk
    return Path(adk.__file__).parent / "templates" / "deep-research" / "engine"


def _load_engine():
    """Import the engine as a synthetic package so relative imports resolve.
    Returns the engine.tools module (build_research_tools, ResearchSession)."""
    if f"{_ENGINE_PKG}.tools" in sys.modules:
        return sys.modules[f"{_ENGINE_PKG}.tools"]
    eng = _engine_dir()
    if not (eng / "tools.py").is_file():
        raise ImportError(f"deep-research engine not found at {eng}")
    if _ENGINE_PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _ENGINE_PKG, eng / "__init__.py",
            submodule_search_locations=[str(eng)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[_ENGINE_PKG] = pkg
        spec.loader.exec_module(pkg)  # type: ignore[union-attr]
    return importlib.import_module(f"{_ENGINE_PKG}.tools")


def _build_session(tools_mod):
    """One process-global ResearchSession: its own GraphMemory (findings graph),
    a savings ledger, and an artifacts dir for generated reports."""
    global _session
    if _session is not None:
        return _session
    art = Path(os.environ.get("AITHER_DEEP_RESEARCH_DIR",
                              str(Path.home() / ".aitheros" / "deep_research")))
    art.mkdir(parents=True, exist_ok=True)
    # ledger (token-savings accounting) — engine module
    ledger_mod = importlib.import_module(f"{_ENGINE_PKG}.ledger")
    ledger = ledger_mod.SavingsLedger()
    # findings knowledge graph
    try:
        from adk.graph_memory import GraphMemory
        graph = GraphMemory(db_path=str(art / "findings.db"), agent_name="researcher")
    except Exception as exc:
        logger.warning("deep_research: GraphMemory unavailable (%s); findings graph disabled", exc)
        graph = None
    _session = tools_mod.ResearchSession(
        graph=graph, ledger=ledger, artifacts_dir=art, session_id="pack")
    return _session


def register(registry) -> int:
    """Build the session-scoped research tools and register them dr_-prefixed.
    Free-tier: all tools register unconditionally. Fail-closed on engine errors."""
    try:
        tools_mod = _load_engine()
        session = _build_session(tools_mod)
        fns = tools_mod.build_research_tools(session)
    except Exception as exc:
        logger.warning("deep_research pack unavailable (%s) — 0 tools registered", exc)
        return 0
    n = 0
    for fn in fns:
        try:
            if not getattr(fn, "__name__", "").startswith("dr_"):
                fn.__name__ = "dr_" + fn.__name__   # namespace; avoids web_* collision
            registry.register(fn)
            n += 1
        except Exception as exc:
            logger.debug("deep_research: skip tool %s: %s", getattr(fn, "__name__", "?"), exc)
    logger.info("DeepResearch registered %d dr_* tools", n)
    return n
