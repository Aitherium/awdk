"""What kind of device this is: phone, laptop, desktop, deck or spark.

Every place that enrolls or beats used to default to "laptop", so a tower PC (and a
WSL distro on one) showed up as a laptop. ``default_node_class()`` is now the one
default: an explicit ``--node-class`` still wins, then ``AITHER_NODE_CLASS``, then
what the hardware says, and "laptop" only when nothing can be read.

The hardware answer comes from the SMBIOS chassis type (Windows: the firmware
table; Linux: /sys/class/dmi/id/chassis_type), a battery check, and a few product
names (DGX Spark, Steam Deck). A WSL distro reads the Windows host's answer from
the host identity file (host_identity.py) or asks the host over interop.

Every probe is best effort and never raises.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

NODE_CLASS_ENV = "AITHER_NODE_CLASS"
FALLBACK = "laptop"

#: SMBIOS 3.x chassis types (DSP0134 table 17) that are carried around.
PORTABLE_CHASSIS = frozenset({8, 9, 10, 11, 14, 30, 31, 32})
#: Chassis types that sit on or under a desk, in a rack, or in a closet.
STATIONARY_CHASSIS = frozenset({3, 4, 5, 6, 7, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23,
                                24, 25, 26, 27, 28, 29, 33, 34, 35, 36})

_DMI = Path("/sys/class/dmi/id")
_POWER = Path("/sys/class/power_supply")


def from_chassis(chassis: Optional[int], has_battery: Optional[bool]) -> str:
    """Map an SMBIOS chassis type plus battery presence to a node class.

    "Other"/"Unknown"/unreadable chassis falls back to the battery; nothing known
    at all is ``""`` (the caller decides).
    """
    if chassis in PORTABLE_CHASSIS:
        return "laptop"
    if chassis in STATIONARY_CHASSIS:
        return "desktop"
    if has_battery is True:
        return "laptop"
    if has_battery is False:
        return "desktop"
    return ""


def from_product(product: str) -> str:
    """Devices recognised by name before the chassis is consulted ("" = none)."""
    p = (product or "").strip().lower()
    if "dgx spark" in p or p in ("spark", "nvidia dgx spark"):
        return "spark"
    if p in ("jupiter", "galileo") or "steam deck" in p:
        return "deck"
    return ""


# ── Linux / WSL ──────────────────────────────────────────────────────────────

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def linux_battery(power: Path = _POWER) -> Optional[bool]:
    try:
        entries = list(power.iterdir())
    except OSError:
        return None
    for e in entries:
        if e.name.upper().startswith("BAT") or _read(e / "type").lower() == "battery":
            return True
    return False


def linux_class(dmi: Path = _DMI, power: Path = _POWER) -> str:
    named = from_product(_read(dmi / "product_name")) or from_product(_read(dmi / "board_name"))
    if named:
        return named
    raw = _read(dmi / "chassis_type")
    chassis = int(raw) if raw.isdigit() else None
    return from_chassis(chassis, linux_battery(power))


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in _read(Path("/proc/version")).lower()


def wsl_distro() -> str:
    return (os.environ.get("WSL_DISTRO_NAME") or "").strip() or "wsl"


# ── Windows ──────────────────────────────────────────────────────────────────

def parse_smbios_chassis(table: bytes) -> Optional[int]:
    """The chassis type from a raw SMBIOS table (RSMB blob or bare structures).

    Walks the structures: each is ``type, length, handle(2)`` + formatted area +
    a string set ended by two NULs. Type 3 byte 5 is the chassis type; the top
    bit is the lock flag.
    """
    data = table[8:] if table[:1] in (b"\x00", b"\x01") and len(table) > 8 else table
    i = 0
    while i + 4 <= len(data):
        stype, length = data[i], data[i + 1]
        if length < 4:
            return None
        if stype == 3 and length > 5:
            return data[i + 5] & 0x7F
        if stype == 127:
            return None
        j = i + length
        while j + 1 < len(data) and not (data[j] == 0 and data[j + 1] == 0):
            j += 1
        i = j + 2
    return None


def _windows_smbios() -> bytes:
    import ctypes  # noqa: PLC0415
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    sig = int.from_bytes(b"RSMB", "big")
    size = k32.GetSystemFirmwareTable(sig, 0, None, 0)
    if not size:
        return b""
    buf = ctypes.create_string_buffer(size)
    if not k32.GetSystemFirmwareTable(sig, 0, buf, size):
        return b""
    return buf.raw


def _windows_battery() -> Optional[bool]:
    import ctypes  # noqa: PLC0415

    class _SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    s = _SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):  # type: ignore[attr-defined]
        return None
    if s.BatteryFlag == 255:  # unknown
        return None
    return not bool(s.BatteryFlag & 128)  # 128 = no system battery


def windows_class(smbios: Callable[[], bytes] = None, battery: Callable[[], Optional[bool]] = None) -> str:
    try:
        chassis = parse_smbios_chassis((smbios or _windows_smbios)())
    except Exception:  # noqa: BLE001
        chassis = None
    try:
        bat = (battery or _windows_battery)()
    except Exception:  # noqa: BLE001
        bat = None
    return from_chassis(chassis, bat)


def _ask_windows_host(timeout: float = 8.0) -> str:
    """From inside WSL: ask the Windows host over interop (powershell.exe)."""
    ps = "powershell.exe"
    for cand in ("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",):
        if Path(cand).exists():
            ps = cand
    cmd = ("$c=(Get-CimInstance Win32_SystemEnclosure).ChassisTypes|Select -First 1;"
           "$b=@(Get-CimInstance Win32_Battery).Count;Write-Output \"$c $b\"")
    try:
        out = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", cmd],
                             capture_output=True, text=True, timeout=timeout).stdout.split()
    except Exception:  # noqa: BLE001 -- interop off, or no powershell
        return ""
    if len(out) < 2 or not out[0].isdigit():
        return ""
    return from_chassis(int(out[0]), int(out[1]) > 0 if out[1].isdigit() else None)


# ── macOS ────────────────────────────────────────────────────────────────────

def mac_class(model: Optional[str] = None) -> str:
    if model is None:
        try:
            model = subprocess.run(["sysctl", "-n", "hw.model"], capture_output=True,
                                   text=True, timeout=5).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
    m = (model or "").lower()
    if not m:
        return ""
    return "laptop" if m.startswith("macbook") else "desktop"


# ── The one default ──────────────────────────────────────────────────────────

def detect_node_class() -> str:
    """What the hardware says ("" when nothing could be read)."""
    try:
        if os.name == "nt":
            return windows_class()
        if sys.platform == "darwin":
            return mac_class()
        if is_wsl():
            from adk.host_identity import read_host_identity  # noqa: PLC0415
            host = read_host_identity()
            if host.get("node_class"):
                return str(host["node_class"])
            return _ask_windows_host() or linux_class()
        if platform.system() == "Linux":
            return linux_class()
    except Exception:  # noqa: BLE001 -- detection must never break enrollment
        return ""
    return ""


_cached: Optional[str] = None


def default_node_class(explicit: Optional[str] = None) -> str:
    """The class to use: explicit > $AITHER_NODE_CLASS > hardware > "laptop"."""
    global _cached
    if explicit:
        return str(explicit)
    env = (os.environ.get(NODE_CLASS_ENV) or "").strip()
    if env:
        return env
    if _cached is None:
        _cached = detect_node_class() or FALLBACK
    return _cached


def resolve_stored(stored: Optional[str], source: Optional[str] = None) -> str:
    """A class read back from a saved record.

    Records written before detection existed carry "laptop" with no source; that
    value was a default, not a choice, so it is re-detected. Anything else (or a
    record that says it was explicit) is kept.
    """
    if stored and (stored != FALLBACK or source in ("explicit", "detected", "env")):
        return str(stored)
    return default_node_class()
