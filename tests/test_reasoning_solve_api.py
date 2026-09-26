"""adk.reasoning.solve: the frozen public surface, hermetic imports, numpy-free import."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import adk.reasoning.solve as solve_pkg

SOLVE = Path(__file__).resolve().parents[1] / "adk" / "reasoning" / "solve"

PUBLIC = {
    "solve",
    "SolveRun",
    "SolveLoop",
    "SolveResult",
    "LoopConfig",
    "Budget",
    "Environment",
    "Obs",
    "Action",
    "Strategy",
    "Hypothesis",
    "Memory",
}


def test_public_api_is_the_twelve_design_names():
    assert set(solve_pkg.__all__) == PUBLIC and len(solve_pkg.__all__) == 12
    for name in PUBLIC:
        assert getattr(solve_pkg, name) is not None, name


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield 0, a.name, node
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or "", node


def _absolute(path: Path, level: int, module: str) -> str:
    if level == 0:
        return module
    pkg = list(path.relative_to(SOLVE.parents[2]).with_suffix("").parts[:-1])
    base = pkg[: len(pkg) - (level - 1)]
    return ".".join(base + ([module] if module else []))


def test_solve_imports_nothing_from_arc_evalharness_or_the_explorer():
    files = sorted(SOLVE.rglob("*.py"))
    assert len(files) >= 15, files
    bad = []
    for f in files:
        for level, module, _node in _imports(f):
            full = _absolute(f, level, module)
            parts = full.split(".")
            if (
                full.startswith("adk.evalharness")
                or parts[0] in ("agent", "arc_agi", "arcengine")
                or any(p == "arc" or p.startswith("arc_") for p in parts)
                or full.startswith("agent.explore")
            ):
                bad.append("%s: %s" % (f.relative_to(SOLVE), full))
            if full.startswith("adk.") and not (
                full.startswith("adk.reasoning.solve")
                or full
                in (
                    "adk.core.model",
                    "adk.core.agent",
                    "adk.reasoning_session",
                    "adk.reasoning.mcts",
                )
            ):
                bad.append("%s: %s (outside the allowed adk seams)" % (f.relative_to(SOLVE), full))
    assert bad == [], bad


def test_numpy_is_imported_only_inside_the_vendored_core_and_the_toy_envs():
    allowed = {"_vendor", "envs"}
    bad = []
    for f in sorted(SOLVE.rglob("*.py")):
        rel = f.relative_to(SOLVE)
        if rel.parts[0] in allowed:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in tree.body:  # top level only: a lazy import inside a function is fine
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            if any(n.split(".")[0] == "numpy" for n in names):
                bad.append(str(rel))
    assert bad == [], bad


def test_import_and_public_names_work_without_numpy():
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'numpy':\n"
        "            raise ImportError('numpy blocked for this test')\n"
        "sys.meta_path.insert(0, Block())\n"
        "import adk.reasoning\n"
        "import adk.reasoning.solve as s\n"
        "for n in s.__all__:\n"
        "    getattr(s, n)\n"
        "s.LoopConfig(budget=s.Budget(max_llm_calls=3))\n"
        "assert 'numpy' not in sys.modules\n"
        "print('ok')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(SOLVE.parents[2]),
    )
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stderr[-2000:]


def test_exit_codes():
    from adk.reasoning.solve import SolveResult

    def res(reason, won=False, calls=1, turns=1):
        return SolveResult(reason, won, 0, [], 0, turns, calls, {}, 0.0)

    assert res("won", won=True).exit_code == 0
    assert res("llm_error", calls=0).exit_code == 2
    assert res("llm_error", calls=2).exit_code == 1
    assert res("error", turns=0).exit_code == 2
    assert res("budget:actions").exit_code == 1
    assert res("cancelled").exit_code == 1
