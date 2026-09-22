"""Every builtin slash plugin that defines a SlashCommand subclass must REGISTER.

PluginRegistry discovers builtins by `attr.name` on the CLASS. A plugin that sets
its name only through `super().__init__(name=...)` imports cleanly, raises nothing,
and is silently absent -- measured 2026-09-21: /judge and /compact were both
shipped in that shape and neither existed at the prompt. This test walks every
builtin module the way the registry does and asserts each subclass came through.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from adk.shell.plugins import PluginRegistry, SlashCommand

BUILTINS = Path(__file__).resolve().parents[1] / "adk" / "shell" / "plugins" / "builtins"


def _subclass_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
            (isinstance(b, ast.Name) and b.id == "SlashCommand")
            or (isinstance(b, ast.Attribute) and b.attr == "SlashCommand")
            for b in node.bases
        ):
            out.append(node.name)
    return out


def test_every_builtin_slashcommand_subclass_registers():
    reg = PluginRegistry([])
    reg.load_all()
    registered = {c.name for c in reg.list_commands()}
    missing = []
    for f in sorted(BUILTINS.glob("*.py")):
        if f.name.startswith("_"):
            continue
        for cls in _subclass_names(f):
            # the class must carry a non-empty name at CLASS level -- that is
            # what the registry reads before it ever instantiates the plugin
            mod = {}
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
            class_name = None
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == cls:
                    for stmt in node.body:
                        if isinstance(stmt, ast.AnnAssign) and getattr(stmt.target, "id", "") == "name":
                            class_name = ast.literal_eval(stmt.value)
                        elif isinstance(stmt, ast.Assign) and any(
                            getattr(t, "id", "") == "name" for t in stmt.targets
                        ):
                            class_name = ast.literal_eval(stmt.value)
            if not class_name or class_name not in registered:
                missing.append(f"{f.name}:{cls} (class-level name={class_name!r})")
    assert not missing, "builtin plugins that never register:\n  " + "\n  ".join(missing)


@pytest.mark.parametrize("name,alias", [("judge", "grade"), ("compact", "shrink")])
def test_door_plugins_present_with_aliases(name, alias):
    reg = PluginRegistry([])
    reg.load_all()
    cmd = reg.get(name)
    assert isinstance(cmd, SlashCommand), f"/{name} is not registered"
    assert reg.get(alias) is cmd, f"/{alias} does not alias /{name}"


def test_compact_keeps_failure_and_summary_lines(tmp_path):
    """/compact on a synthetic pytest log: the one unforgivable failure is dropping
    the error or the summary line; with no door reachable every verdict is a keep."""
    reg = PluginRegistry([])
    reg.load_all()
    cmd = reg.get("compact")
    lines = ["collected 40 items"]
    lines += [f"tests/test_x.py::test_{i} PASSED" for i in range(40)]
    lines += ["E   AssertionError: boom", "1 failed, 39 passed in 1.2s"]
    p = tmp_path / "pytest.txt"
    p.write_text("\n".join(lines), encoding="utf-8")
    out = asyncio.run(cmd.run([str(p), "--tool", "pytest"], {}))
    assert out, "no output"
    if out.startswith("[!]"):
        pytest.xfail(f"door unavailable in this environment: {out[:80]}")
    assert "AssertionError: boom" in out
    assert "1 failed, 39 passed" in out
