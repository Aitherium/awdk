"""Device class detection and the WSL-is-a-facet-of-its-host rule.

DESKTOP-EG38V5F (a tower PC) enrolled as a laptop because every default said
"laptop", and its WSL distro enrolled as a second device. These pin the fixes.
"""

from __future__ import annotations

import json
import struct

import pytest

from adk import device_class as dc
from adk import host_identity as hi


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(dc, "_cached", None)
    monkeypatch.delenv("AITHER_NODE_CLASS", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)


@pytest.mark.parametrize("chassis,battery,want", [
    (3, False, "desktop"),     # Desktop -- what DESKTOP-EG38V5F reports
    (7, None, "desktop"),      # Tower
    (35, None, "desktop"),     # Mini PC
    (23, None, "desktop"),     # Rack mount
    (9, True, "laptop"),       # Laptop
    (10, None, "laptop"),      # Notebook
    (31, None, "laptop"),      # Convertible
    (2, True, "laptop"),       # Unknown chassis, has a battery
    (2, False, "desktop"),     # Unknown chassis, no battery
    (None, None, ""),          # nothing readable: caller decides
])
def test_chassis_mapping(chassis, battery, want):
    assert dc.from_chassis(chassis, battery) == want


def test_products_win_over_chassis():
    assert dc.from_product("NVIDIA DGX Spark") == "spark"
    assert dc.from_product("Jupiter") == "deck"
    assert dc.from_product("OptiPlex 7090") == ""


def _smbios(*structs: bytes) -> bytes:
    header = b"\x00\x03\x00\x00" + struct.pack("<I", sum(len(s) for s in structs))
    return header + b"".join(structs)


def _type(stype: int, formatted: bytes, strings: bytes = b"") -> bytes:
    body = bytes([stype, 4 + len(formatted)]) + b"\x00\x00" + formatted
    return body + (strings + b"\x00" if strings else b"\x00") + b"\x00"


def test_smbios_parse_finds_chassis_after_other_structures():
    table = _smbios(_type(0, b"\x01\x02\x03", b"Vendor\x00Ver"),
                    _type(1, b"\x01" * 8, b"Product"),
                    _type(3, b"\x01\x83\x02\x03"),  # manufacturer str, type 0x83 (lock bit + 3)
                    _type(127, b""))
    assert dc.parse_smbios_chassis(table) == 3


def test_smbios_parse_garbage_is_none():
    assert dc.parse_smbios_chassis(b"") is None
    assert dc.parse_smbios_chassis(b"\x00\x00\x00\x00\x00\x00\x00\x00\x03\x01") is None


def test_windows_class_uses_smbios_then_battery():
    table = _smbios(_type(3, b"\x01\x09\x02\x03"))
    assert dc.windows_class(smbios=lambda: table, battery=lambda: None) == "laptop"
    assert dc.windows_class(smbios=lambda: b"", battery=lambda: False) == "desktop"


def test_linux_class_from_sysfs(tmp_path):
    dmi, power = tmp_path / "dmi", tmp_path / "power"
    dmi.mkdir(); power.mkdir()
    (dmi / "chassis_type").write_text("10\n")
    assert dc.linux_class(dmi, power) == "laptop"
    (dmi / "chassis_type").write_text("3\n")
    assert dc.linux_class(dmi, power) == "desktop"
    (dmi / "product_name").write_text("NVIDIA DGX Spark\n")
    assert dc.linux_class(dmi, power) == "spark"


def test_linux_unknown_chassis_falls_back_to_battery(tmp_path):
    dmi, power = tmp_path / "dmi", tmp_path / "power"
    dmi.mkdir(); (power / "BAT0").mkdir(parents=True)
    (dmi / "chassis_type").write_text("2\n")
    assert dc.linux_class(dmi, power) == "laptop"


def test_mac_models():
    assert dc.mac_class("MacBookPro18,3") == "laptop"
    assert dc.mac_class("Mac14,13") == "desktop"


def test_explicit_then_env_then_hardware(monkeypatch):
    monkeypatch.setattr(dc, "detect_node_class", lambda: "desktop")
    assert dc.default_node_class("spark") == "spark"
    monkeypatch.setenv("AITHER_NODE_CLASS", "deck")
    assert dc.default_node_class() == "deck"
    monkeypatch.delenv("AITHER_NODE_CLASS")
    assert dc.default_node_class() == "desktop"


def test_nothing_readable_still_laptop(monkeypatch):
    monkeypatch.setattr(dc, "detect_node_class", lambda: "")
    assert dc.default_node_class() == "laptop"


def test_stored_default_laptop_is_redetected(monkeypatch):
    monkeypatch.setattr(dc, "detect_node_class", lambda: "desktop")
    assert dc.resolve_stored("laptop") == "desktop"            # old default, no source
    assert dc.resolve_stored("laptop", "explicit") == "laptop"  # a real choice stays
    assert dc.resolve_stored("sovereign") == "sovereign"
    assert dc.resolve_stored(None) == "desktop"


def test_wsl_reads_class_from_host_file(tmp_path, monkeypatch):
    f = tmp_path / "device.json"
    f.write_text(json.dumps({"schema": 1, "host_machine_id": "ab" * 32, "node_class": "desktop"}))
    monkeypatch.setenv(hi.HOST_FILE_ENV, str(f))
    monkeypatch.setattr(dc, "is_wsl", lambda: True)
    monkeypatch.setattr(dc.os, "name", "posix")
    monkeypatch.setattr(dc.sys, "platform", "linux")
    assert dc.detect_node_class() == "desktop"


def test_host_identity_rejects_malformed(tmp_path, monkeypatch):
    f = tmp_path / "device.json"
    f.write_text(json.dumps({"host_machine_id": "not-a-hash"}))
    assert hi.read_host_identity(f) == {}
    f.write_text("{broken")
    assert hi.read_host_identity(f) == {}


def test_host_identity_roundtrip_holds_only_hashes(tmp_path, monkeypatch):
    from adk import device_identity as di
    monkeypatch.setattr(di, "os_machine_id", lambda: "4C4C4544-0042-3510-8051-B4C04F4E4B32")
    monkeypatch.setattr(dc, "detect_node_class", lambda: "desktop")
    f = tmp_path / "Aither" / "device.json"
    assert hi.write_host_identity(f) is True
    data = hi.read_host_identity(f)
    assert data["host_machine_id"] == di.MACHINE_ID_VECTORS[0]["machine_id"]
    assert data["node_class"] == "desktop"
    assert "4C4C4544" not in f.read_text()  # the raw OS id never lands on disk


def test_wsl_distro_reports_host_machine_id_and_wsl_facet(tmp_path, monkeypatch):
    from adk import device_identity as di
    host = tmp_path / "host.json"
    host.write_text(json.dumps({"schema": 1, "host_machine_id": "cd" * 32, "node_class": "desktop"}))
    monkeypatch.setenv(hi.HOST_FILE_ENV, str(host))
    monkeypatch.setenv(di.DEVICE_FILE_ENV, str(tmp_path / "dev.json"))
    monkeypatch.setenv("WSL_DISTRO_NAME", "awnix")
    monkeypatch.setattr(hi, "_is_windows", lambda: False)
    monkeypatch.setattr(di, "os_machine_id", lambda: "80c99a8ff4504a57b6b3bb94d9dbe4b3")
    assert di.machine_id() == "cd" * 32
    assert di.load_device()["machine_id_source"] == "wsl-host"
    assert di.effective_facet("daemon") == "wsl-awnix"


def test_without_host_file_wsl_keeps_its_own_id(tmp_path, monkeypatch):
    from adk import device_identity as di
    monkeypatch.setenv(hi.HOST_FILE_ENV, str(tmp_path / "absent.json"))
    monkeypatch.setenv(di.DEVICE_FILE_ENV, str(tmp_path / "dev.json"))
    monkeypatch.setenv("WSL_DISTRO_NAME", "awnix")
    monkeypatch.setattr(hi, "_is_windows", lambda: False)
    monkeypatch.setattr(di, "os_machine_id", lambda: "80c99a8ff4504a57b6b3bb94d9dbe4b3")
    assert di.machine_id() == di.machine_key("80c99a8ff4504a57b6b3bb94d9dbe4b3")
    assert di.effective_facet("daemon") == "daemon"


def test_nvidia_smi_fallback_finds_wsl_path(tmp_path):
    from adk import setup_cli
    fake = tmp_path / "nvidia-smi"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert setup_cli._nvidia_smi_fallback((str(tmp_path / "missing"), str(fake))) == str(fake)
    assert setup_cli._nvidia_smi_fallback((str(tmp_path / "missing"),)) is None
