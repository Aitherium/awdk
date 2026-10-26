"""The local research delegate ships in awdk and starts with -S (stdlib only)."""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

AWDK = Path(__file__).resolve().parent.parent
MOD = AWDK / "adk" / "awdelegate.py"
SHIM = AWDK.parent / "tools" / "claude-backend" / "awdelegate.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(path: Path, *args: str):
    return subprocess.run(
        [sys.executable, "-S", str(path), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def test_module_and_shim_exist():
    assert MOD.is_file(), MOD
    assert SHIM.is_file(), SHIM


def test_module_runs_without_site_packages():
    p = _run(MOD, "--help")
    assert p.returncode == 0, p.stderr[-800:]
    assert "local research delegate" in p.stdout


def test_shim_runs_the_way_existing_registrations_invoke_it():
    p = _run(SHIM, "--help")
    assert p.returncode == 0, p.stderr[-800:]
    assert "local research delegate" in p.stdout


def test_module_imports_only_the_standard_library():
    tree = ast.parse(MOD.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    assert roots <= allowed, sorted(roots - allowed)


def test_default_roots_are_per_machine(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mod = _load("awdelegate_roots")
    assert mod.DEFAULTS["roots"][0] == str(tmp_path)
    assert str(Path.home() / ".claude" / "projects") in mod.DEFAULTS["roots"]
    assert "AitherOS-Fresh" not in json.dumps(mod.DEFAULTS["roots"])


def test_awm_scope_configuration_wins(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    mod = _load("awdelegate_scope_cfg")
    monkeypatch.setenv("AWM_SCOPE", "acme:dana:widgets")
    assert mod._awm_scope({}) == "acme:dana:widgets"
    monkeypatch.delenv("AWM_SCOPE")
    assert mod._awm_scope({"awm_scope": "globex:sam:gadgets"}) == "globex:sam:gadgets"


def test_awm_scope_is_derived_with_awm_casing(monkeypatch, tmp_path):
    if shutil.which("git") is None:
        import pytest

        pytest.skip("git is not installed")
    repo = tmp_path / "Widgets"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/Acme/Widgets.git"], check=True,
                   capture_output=True, text=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AWM_USER", "Dana")
    monkeypatch.delenv("AWM_SCOPE", raising=False)
    mod = _load("awdelegate_scope_git")
    assert mod._awm_scope({}) == "acme:Dana:Widgets"
