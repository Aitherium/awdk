"""adk mobile: device discovery, device choice, and the proof directory a run leaves."""

from __future__ import annotations

import json
import subprocess

import pytest
from adk.mobile import Device, list_devices, pick_device, run_flow

ADB_OUT = (
    "List of devices attached\n"
    "emulator-5554          device product:sdk_gphone64 model:sdk_gphone64_x86_64 transport_id:1\n"
    "R58M123ABC             unauthorized usb:1-1 transport_id:2\n"
)
SIMCTL = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
    {"udid": "U1", "name": "iPhone 16", "state": "Booted", "isAvailable": True},
    {"udid": "U2", "name": "iPad", "state": "Shutdown", "isAvailable": True},
]}}


def fake_run(calls, maestro_code=0):
    def run(argv, timeout=60, **kw):
        calls.append(argv)
        if argv[:2] == ["adb", "devices"]:
            return subprocess.CompletedProcess(argv, 0, ADB_OUT, "")
        if argv[:2] == ["xcrun", "simctl"] and "list" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(SIMCTL), "")
        if "screencap" in argv:
            return subprocess.CompletedProcess(argv, 0, b"\x89PNG fake", b"")
        if argv[0] == "maestro":
            out = argv[argv.index("--output") + 1]
            open(out, "w").write("<testsuites/>")
            return subprocess.CompletedProcess(argv, maestro_code, "flow ran", "")
        raise AssertionError(f"unexpected {argv}")

    return run


def test_lists_android_and_ios_on_macos():
    devs, notes = list_devices(run=fake_run([]), which=lambda b: b, system="Darwin")
    assert [(d.platform, d.id, d.usable) for d in devs] == [
        ("android", "emulator-5554", True), ("android", "R58M123ABC", False),
        ("ios", "U1", True), ("ios", "U2", False)]
    assert devs[0].name == "sdk_gphone64_x86_64" and not notes


def test_missing_tools_are_notes_not_crashes():
    devs, notes = list_devices(run=fake_run([]), which=lambda b: None, system="Windows")
    assert devs == []
    assert any("adb not found" in n for n in notes)
    assert any("need macOS" in n for n in notes)


def test_pick_device_refuses_unready_and_unknown():
    devs = [Device("android", "a", "a", "unauthorized"), Device("android", "b", "b", "device")]
    assert pick_device(devs).id == "b"
    with pytest.raises(LookupError, match="unauthorized"):
        pick_device(devs, "a")
    with pytest.raises(LookupError, match="not found"):
        pick_device(devs, "zz")
    with pytest.raises(LookupError, match="no ready device"):
        pick_device(devs[:1])


@pytest.mark.parametrize("code,passed", [(0, True), (1, False)])
def test_run_flow_writes_the_proof(tmp_path, code, passed):
    flow = tmp_path / "login.yaml"
    flow.write_text("appId: ${APP_ID}\n---\n- launchApp\n", encoding="utf-8")
    calls = []
    dev = Device("android", "emulator-5554", "pixel", "device")
    rec = run_flow(flow, dev, app="com.acme", run=fake_run(calls, code), which=lambda b: b,
                   proof_root=tmp_path / "proof")
    assert rec["passed"] is passed and rec["exit_code"] == code
    out = tmp_path / "proof" / rec["proof_dir"].split("proof")[-1].strip("\\/")
    for name in ("before.png", "after.png", "report.xml", "maestro.log", "login.yaml", "run.json"):
        assert (out / name).exists(), name
    maestro = next(c for c in calls if c[0] == "maestro")
    assert maestro[:3] == ["maestro", "--device", "emulator-5554"] and "APP_ID=com.acme" in maestro


def test_run_flow_needs_maestro_and_a_flow(tmp_path):
    dev = Device("android", "e", "e", "device")
    with pytest.raises(FileNotFoundError, match="maestro not found"):
        run_flow(tmp_path / "x.yaml", dev, run=fake_run([]), which=lambda b: None)
    with pytest.raises(FileNotFoundError, match="flow not found"):
        run_flow(tmp_path / "x.yaml", dev, run=fake_run([]), which=lambda b: b)
