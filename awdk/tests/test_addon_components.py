"""The addon manager as the ONE component loader (2026-09-06).

Pins: source precedence + shadowing is recorded; a `capability` resolves to its host;
a `process` with a command is spawned with the manifest's port; the inventory carries
brick/surfaces; the disabled-with-pid path terminates what it spawned.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest
from adk import addon_manager as am


def _write(d: pathlib.Path, name: str, text: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


@pytest.fixture
def sources(tmp_path, monkeypatch):
    home = tmp_path / "home"
    bundled = tmp_path / "bundled"
    _write(bundled, "_schema.yaml", "x: 1\n")
    _write(bundled, "awx.yaml", "id: awx\nbrick: awx\ntype: process\ndefault_port: 9100\n"
                                "surfaces: {ui: {path: /}}\n")
    _write(bundled, "shadowed.yaml",
           "id: shadowed\ntype: external\ndefault_port: 1\nname: bundled\n")
    _write(home, "shadowed.yaml", "id: shadowed\ntype: external\ndefault_port: 2\nname: home\n")
    _write(home, "cap.yaml", "id: cap\nbrick: cap\ntype: capability\nhosted_by: awdk\n"
                             "default_port: 0\nhealth_check: {path: /health}\n")
    monkeypatch.setattr(am, "COMPONENTS_DIR", home)
    monkeypatch.setattr(am, "_find_manifest_dir", lambda: bundled)
    monkeypatch.setattr(am, "_entry_point_manifest_paths", lambda: [])
    monkeypatch.setattr(am, "_state_path", lambda: tmp_path / "state.json")
    return home, bundled


def test_precedence_and_source_recorded(sources):
    ms = {m["id"]: m for m in am.load_all_manifests()}
    assert ms["shadowed"]["name"] == "home", "home must shadow bundled"
    assert ms["shadowed"]["_source"] == "home"
    assert ms["awx"]["_source"] == "bundled"
    assert am.load_addon_manifest("cap")["hosted_by"] == "awdk"
    assert am.load_addon_manifest("nope") is None


def test_entry_point_manifests_win(sources, tmp_path):
    ep = tmp_path / "ep"
    _write(ep, "shadowed.yaml", "id: shadowed\ntype: external\ndefault_port: 3\nname: ep\n")
    am._entry_point_manifest_paths = lambda: [("entry-point:awx", ep / "shadowed.yaml")]
    ms = {m["id"]: m for m in am.load_all_manifests()}
    assert ms["shadowed"]["name"] == "ep" and ms["shadowed"]["_source"] == "entry-point:awx"


def test_capability_resolves_to_host(sources, monkeypatch):
    monkeypatch.setenv("AITHER_PORT", "9123")
    mgr = am.AddonManager()

    async def fake_health(manifest, inst):
        return inst.endpoint == "http://127.0.0.1:9123"
    monkeypatch.setattr(mgr, "_check_health", fake_health)
    inst = asyncio.run(mgr.enable("cap"))
    assert inst.addon_type == "capability"
    assert inst.endpoint == "http://127.0.0.1:9123"
    assert inst.health_ok and inst.status == "running"
    assert inst.brick == "cap" and inst.source == "home"


def test_process_spawns_command_with_manifest_port(sources, tmp_path, monkeypatch):
    bundled = sources[1]
    py = sys.executable.replace("\\", "/")
    # A real child that just exits: what we assert is the argv it was given.
    cmd = f"command: '\"{py}\" -c \"import sys; print(sys.argv)\" --port {{port}}'\n"
    _write(bundled, "awx.yaml", "id: awx\nbrick: awx\ntype: process\ndefault_port: 9100\n"
                                + cmd + "health_check: {path: /health}\n")
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "ah"))
    seen = {}
    import subprocess

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return FakeProc()
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    mgr = am.AddonManager()

    async def fake_health(manifest, inst):
        return True
    monkeypatch.setattr(mgr, "_check_health", fake_health)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(am.asyncio, "sleep", no_sleep)
    inst = asyncio.run(mgr.enable("awx", config={"port": 9155}))
    assert seen["argv"][-2:] == ["--port", "9155"], seen
    assert seen["kw"]["env"]["AITHER_PORT"] == "9155"
    if sys.platform == "win32":
        assert seen["kw"]["creationflags"] & subprocess.CREATE_NO_WINDOW, \
            "console window must not open"
    assert inst.pid == 4242 and inst.status == "running"
    assert inst.endpoint == "http://127.0.0.1:9155"


def test_inventory_carries_brick_and_surfaces(sources):
    mgr = am.AddonManager()
    inv = {c["id"]: c for c in mgr.components_inventory()}
    assert inv["awx"]["brick"] == "awx" and inv["awx"]["surfaces"] == {"ui": {"path": "/"}}
    assert inv["awx"]["status"] == "available"        # offered, not yet enabled
    assert inv["cap"]["hosted_by"] == "awdk"
    hb = {c["addon_id"]: c for c in mgr.get_inventory()}
    assert hb == {}                                     # nothing enabled yet
