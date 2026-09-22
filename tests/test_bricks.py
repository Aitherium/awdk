"""adk bricks -- the truth rules, each pinned by a test that fails if it lies."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from adk import bricks

REGISTRY = Path(__file__).resolve().parents[2] / "AitherOS" / "config" / "ecosystem.yaml"


def _registry_family() -> set[tuple[str, str, str]]:
    import yaml

    doc = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    out: set[tuple[str, str, str]] = set()

    def walk(o):
        if isinstance(o, dict):
            if "id" in o and isinstance(o.get("install"), str):
                m = re.match(r"\s*pip install\s+([A-Za-z0-9_.\-\[\]]+)", o["install"])
                if m:
                    dist = m.group(1)
                    src = "git" if dist == "git" else "pypi"
                    out.add((o["id"], o["id"] if src == "git" else dist, src))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)
    return out


@pytest.mark.skipif(not REGISTRY.exists(), reason="monorepo registry not present (installed wheel)")
def test_family_matches_the_registry():
    """The frozen list travels with the wheel; this is what keeps it honest."""
    assert set(bricks.FAMILY) == _registry_family()


def test_is_newer_orders_versions_and_never_guesses():
    assert bricks.is_newer("1.11.1", "1.11.0")
    assert bricks.is_newer("0.10.0", "0.9.9")
    assert not bricks.is_newer("0.2.0", "0.2.0")
    assert not bricks.is_newer(None, "1.0.0"), "an unknown latest is never 'newer'"
    assert not bricks.is_newer("1.0.0", None)


def test_an_unreachable_index_is_an_error_not_up_to_date(monkeypatch):
    monkeypatch.setattr(bricks, "INDEX_URL", "http://127.0.0.1:9/pypi")
    version, err = bricks.latest("awgit", timeout=1.0)
    assert version is None
    assert err and "unreachable" in err


def _fake_installed(version="0.1.0", editable=False):
    return lambda _d: {"version": version, "editable": editable, "location": "file:///src",
                       "import_name": "awtunnel"}


def test_an_editable_install_is_refused_not_clobbered(monkeypatch):
    monkeypatch.setattr(bricks, "installed", _fake_installed(editable=True))
    calls = []
    monkeypatch.setattr(bricks, "_pip", lambda args, timeout=900: calls.append(args) or (0, ""))
    res = bricks.upgrade("awtunnel")
    assert res["ok"] is False and "editable" in res["error"]
    assert calls == [], "pip must never run against a source checkout"


def test_a_git_installed_brick_is_refused(monkeypatch):
    monkeypatch.setattr(bricks, "installed", _fake_installed())
    res = bricks.upgrade("awmine")
    assert res["ok"] is False and "git" in res["error"]


def test_a_failed_test_rolls_the_upgrade_back(monkeypatch, tmp_path):
    monkeypatch.setenv("ADK_BRICKS_HOME", str(tmp_path))
    monkeypatch.setattr(bricks, "installed", _fake_installed("0.1.0"))
    monkeypatch.setattr(bricks, "latest", lambda _d, timeout=8.0: ("0.2.1", None))
    monkeypatch.setattr(bricks, "test", lambda _n: {"ok": False, "import": "FAILED"})
    calls = []
    monkeypatch.setattr(bricks, "_pip", lambda args, timeout=900: calls.append(args) or (0, ""))
    res = bricks.upgrade("awtunnel")
    assert res["ok"] is False and res["rolled_back"] is True
    assert calls == [["install", "awtunnel==0.2.1"], ["install", "awtunnel==0.1.0"]]
    assert bricks.history("awtunnel")[-1]["rolled_back"] is True


def test_rollback_without_a_recorded_upgrade_refuses_to_guess(monkeypatch, tmp_path):
    monkeypatch.setenv("ADK_BRICKS_HOME", str(tmp_path))
    monkeypatch.setattr(bricks, "installed", _fake_installed("0.2.1"))
    calls = []
    monkeypatch.setattr(bricks, "_pip", lambda args, timeout=900: calls.append(args) or (0, ""))
    res = bricks.rollback("awtunnel")
    assert res["ok"] is False and "no recorded upgrade" in res["error"]
    assert calls == []


def test_rollback_returns_to_the_recorded_version(monkeypatch, tmp_path):
    monkeypatch.setenv("ADK_BRICKS_HOME", str(tmp_path))
    bricks.record({"op": "upgrade", "dist": "awtunnel", "from": "0.1.0", "to": "0.2.1", "ok": True})
    monkeypatch.setattr(bricks, "installed", _fake_installed("0.2.1"))
    monkeypatch.setattr(bricks, "test", lambda _n: {"ok": True, "import": "ok"})
    calls = []
    monkeypatch.setattr(bricks, "_pip", lambda args, timeout=900: calls.append(args) or (0, ""))
    res = bricks.rollback("awtunnel")
    assert res["ok"] is True and res["to"] == "0.1.0"
    assert calls == [["install", "awtunnel==0.1.0"]]


@pytest.mark.parametrize("argv", [["--json", "outdated"], ["outdated", "--json"]])
def test_json_is_accepted_before_or_after_the_verb(monkeypatch, capsys, argv):
    """bricks.main accepts --json before or after the verb. Through the `adk` CLI
    only the AFTER form works (`adk bricks list --json`): adk's own parser
    rejects a flag that precedes the REMAINDER, before bricks ever runs."""
    monkeypatch.setattr(bricks, "status", lambda check_latest=True: [])
    assert bricks.main(argv) == 0
    assert capsys.readouterr().out.strip() == "[]"
