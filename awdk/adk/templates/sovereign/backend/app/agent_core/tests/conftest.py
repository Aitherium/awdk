"""Pytest bootstrap: load agent_core directly from the package directory.

These tests must run regardless of whether the consumer app has
performed the Docker rename of `awkit-backend` -> `portal_kit_backend`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent  # .../agent_core
_PORTAL_DIR = _PKG_DIR.parent  # .../awkit-backend


def _load_agent_core() -> None:
    if "agent_core" in sys.modules:
        return
    # Pre-load submodules so relative imports inside __init__.py resolve.
    submods = [
        "tiers", "reinforcement", "store", "consolidation",
        "bridge", "client", "pillars", "eviction", "admin", "integration",
    ]
    pkg_spec = importlib.util.spec_from_file_location(
        "agent_core", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules["agent_core"] = pkg
    for name in submods:
        spec = importlib.util.spec_from_file_location(
            f"agent_core.{name}", _PKG_DIR / f"{name}.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"agent_core.{name}"] = mod
        spec.loader.exec_module(mod)
    pkg_spec.loader.exec_module(pkg)


_load_agent_core()
