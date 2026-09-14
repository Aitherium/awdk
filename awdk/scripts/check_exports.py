#!/usr/bin/env python3
"""Verify adk package exports resolve and version is synced.

Catches:
  1. __all__ entries that reference non-existent modules (ghost exports)
  2. __version__ mismatch between __init__.py and pyproject.toml
  3. Top-level .py modules with zero imports from anywhere (dead code)

Run: python scripts/check_exports.py
Exit code 1 on any failure.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADK = ROOT / "adk"
PYPROJECT = ROOT / "pyproject.toml"

errors: list[str] = []


# ── 1. Verify every __all__ entry resolves via __getattr__ ────────────────

def check_ghost_exports():
    init = ADK / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))

    # Collect __all__ names
    all_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                all_names.append(elt.value)

    # Collect lazy import targets from __getattr__
    lazy_names: set[str] = set()
    eager_names: set[str] = set()

    for node in ast.walk(tree):
        # Eager imports at module level (from adk.X import Y)
        if isinstance(node, ast.ImportFrom) and node.col_offset == 0:
            for alias in node.names:
                eager_names.add(alias.asname or alias.name)

        # __getattr__ function: collect the names each branch can serve. Both
        # dispatch forms are idiomatic and must be understood:
        #   if name == "X":                 -> Constant comparator
        #   if name in ("X", "Y", "Z"):     -> Tuple/List/Set comparator
        # Only handling the first form false-flags every grouped branch as a
        # ghost export, which is what used to fail this gate on a green package.
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            for child in ast.walk(node):
                if isinstance(child, ast.Compare):
                    for comp in child.comparators:
                        if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                            lazy_names.add(comp.value)
                        elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                            for elt in comp.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    lazy_names.add(elt.value)

    resolvable = eager_names | lazy_names
    for name in all_names:
        if name not in resolvable:
            errors.append(f"ghost export: '{name}' is in __all__ but has no import or __getattr__ entry")


# ── 2. Verify __version__ matches pyproject.toml ─────────────────────────

def check_version_sync():
    # __init__.py resolves __version__ dynamically from installed metadata and
    # keeps a string-literal FALLBACK for source checkouts (inside a try/except,
    # so it is indented — a line.startswith() parse sees nothing). Match the
    # first literal assignment anywhere; that fallback must stay release-synced.
    init = ADK / "__init__.py"
    m_init = re.search(
        r'^\s*__version__\s*=\s*["\']([0-9][^"\']*)["\']',
        init.read_text(encoding="utf-8"),
        re.M,
    )
    init_version = m_init.group(1) if m_init else None

    m = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(encoding="utf-8"), re.M)
    pyproject_version = m.group(1) if m else None

    if init_version != pyproject_version:
        errors.append(
            f"version mismatch: __init__.py={init_version} vs pyproject.toml={pyproject_version}"
        )


# ── 3. Detect orphan modules (zero imports from any other file) ──────────

def check_orphan_modules():
    # Get all top-level .py files in adk/ (excluding __init__.py)
    module_files = {
        f.stem: f for f in ADK.glob("*.py")
        if f.name != "__init__.py"
    }
    # Also orphan-check cohesive subpackages whose every module should be
    # reachable. adk/sync/ is a curated domain package — before the
    # 2.24.0 move, a dead root-level *_sync.py was caught here, but a dead
    # adk/sync/<x>.py would have been invisible (this scan was top-level only).
    # Keyed "sync/<name>" so the error prints the real path.
    for f in (ADK / "sync").glob("*.py"):
        if f.name != "__init__.py":
            module_files[f"sync/{f.stem}"] = f

    # Scan all .py files in the package for imports
    all_py = list(ADK.rglob("*.py"))
    # Also check tests, examples, packaging, root scripts, packages, and root .py files
    for subdir in ["tests", "examples", "packaging", "scripts", "packages"]:
        p = ROOT / subdir
        if p.exists():
            all_py.extend(p.rglob("*.py"))
    all_py.extend(ROOT.glob("*.py"))

    imported: set[str] = set()
    for py_file in all_py:
        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # from adk.foo import ... or from .foo import ...
                parts = node.module.split(".")
                if len(parts) >= 2 and parts[0] == "adk":
                    imported.add(parts[1])
                    # subpackage submodule reachability:
                    #   from adk.sync.secrets import X  -> "sync/secrets"
                    #   from adk.sync import secrets, X -> "sync/secrets", "sync/X"
                    if parts[1] == "sync":
                        if len(parts) >= 3:
                            imported.add(f"sync/{parts[2]}")
                        else:
                            for alias in node.names:
                                imported.add(f"sync/{alias.name}")
                elif parts == ["adk"]:
                    # from adk import foo — the imported name is the module
                    for alias in node.names:
                        imported.add(alias.name)
                elif node.level and len(parts) >= 1:
                    imported.add(parts[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if len(parts) >= 2 and parts[0] == "adk":
                        imported.add(parts[1])
                        if parts[1] == "sync" and len(parts) >= 3:
                            imported.add(f"sync/{parts[2]}")  # import adk.sync.secrets

    # Also count __init__.py lazy imports as "used"
    init_source = (ADK / "__init__.py").read_text(encoding="utf-8")
    init_tree = ast.parse(init_source)
    for node in ast.walk(init_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if len(parts) >= 2 and parts[0] == "adk":
                imported.add(parts[1])

    # Entry points from pyproject.toml count as used
    for m in re.finditer(r'=\s*"(adk\.\w+):', PYPROJECT.read_text(encoding="utf-8")):
        parts = m.group(1).split(".")
        if len(parts) >= 2:
            imported.add(parts[1])

    # Modules that legitimately have zero static importers in the PUBLIC payload.
    # Each entry needs a reason; an unexplained orphan is still a failure.
    exempt = {
        "__main__",  # executed via `python -m adk`, never imported
        "ports",  # imported by the internal aither_platform toolkit (outside this repo)
        # Standalone hardware diagnostic: run via `python -m adk.bandwidth_benchmark`,
        # prints JSON consumed by node-registration payloads. Nothing imports it.
        "bandwidth_benchmark",
    }
    for name, path in sorted(module_files.items()):
        if name not in imported and name not in exempt:
            errors.append(f"orphan module: adk/{name}.py is never imported")


# ── Run ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    check_ghost_exports()
    check_version_sync()
    check_orphan_modules()

    if errors:
        print(f"FAIL: {len(errors)} issue(s) found:\n")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("OK: all exports resolve, version synced, no orphan modules")
        sys.exit(0)
