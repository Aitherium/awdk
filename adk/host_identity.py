"""One computer, one device: the Windows host and its WSL distros.

A Windows PC running awdk on Windows AND a WSL distro (awnix) used to show up as
two devices: Windows answers its MachineGuid, the distro its own /etc/machine-id,
so the one-device merge (device_identity.machine_id) never saw them as one.

The Windows side writes a host identity file that every distro can read through
/mnt/c, holding only one-way hashes and the detected class:

    %ProgramData%\\Aither\\device.json
    {"host_machine_id": <machine_key(MachineGuid)>, "node_class": "desktop",
     "hostname": "DESKTOP-...", "written_at": <unix>, "schema": 1}

A distro that finds it reports the HOST's machine id with facet ``wsl:<distro>``,
so Identity files both under the one device. Nothing secret is in the file.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Optional

HOST_FILE_ENV = "AITHER_HOST_IDENTITY_FILE"
SCHEMA = 1


def _is_windows() -> bool:
    return os.name == "nt"


def host_file_windows() -> Path:
    base = os.environ.get("ProgramData") or r"C:\ProgramData"
    return Path(base) / "Aither" / "device.json"


def host_file_from_wsl() -> Path:
    return Path("/mnt/c/ProgramData/Aither/device.json")


def host_file() -> Path:
    env = (os.environ.get(HOST_FILE_ENV) or "").strip()
    if env:
        return Path(env)
    return host_file_windows() if _is_windows() else host_file_from_wsl()


def read_host_identity(path: Optional[Path] = None) -> dict:
    """The host identity, {} when absent, unreadable or malformed."""
    try:
        data = json.loads((path or host_file()).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    mid = str(data.get("host_machine_id") or "")
    if len(mid) != 64 or any(c not in "0123456789abcdef" for c in mid):
        return {}
    return data


def write_host_identity(path: Optional[Path] = None) -> bool:
    """Windows only: record this host's machine key and class. Never raises."""
    if not _is_windows() and path is None:
        return False
    try:
        from adk.device_class import default_node_class  # noqa: PLC0415
        from adk.device_identity import machine_key, os_machine_id  # noqa: PLC0415
        raw = os_machine_id()
        if not raw:
            return False
        data = {"schema": SCHEMA, "host_machine_id": machine_key(raw),
                "node_class": default_node_class(), "hostname": socket.gethostname(),
                "written_at": int(time.time())}
        target = path or host_file()
        current = read_host_identity(target)
        if current and all(current.get(k) == data[k] for k in ("host_machine_id", "node_class", "hostname")):
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except Exception:  # noqa: BLE001
        return False


def wsl_host_machine_key() -> str:
    """Inside WSL: the Windows host's machine key, "" when not under WSL or unknown."""
    try:
        from adk.device_class import is_wsl  # noqa: PLC0415
        if _is_windows() or not is_wsl():
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return str(read_host_identity().get("host_machine_id") or "")
