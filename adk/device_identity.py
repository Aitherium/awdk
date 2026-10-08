"""This device's identity: its signing key, and the sync it unlocks.

``adk enroll`` gives every device its own Ed25519 key (awseal). The public half
goes into the enrollment record; the user's other devices trust what it signs
for as long as the device stays enrolled, so removing the device is the
revocation. Today that covers settings sync (awsettings); the same key is meant
to sign the device's mesh peer key next.

Everything here is best effort: a device without awseal still enrolls, it just
cannot sign, and says so.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

SETTINGS_PATH = "/api/settings/preferences"
SEAL_KEYS_PATH = "/v1/nodes/seal-keys"


def seal_public_key(create: bool = True) -> str:
    """This device's public key (hex), generating the key once. "" without awseal."""
    try:
        from awseal import keys
    except Exception:  # noqa: BLE001 -- optional dependency (`pip install awdk[seal]`)
        return ""
    try:
        from pathlib import Path
        env = (os.getenv(keys.KEY_PATH_ENV) or "").strip()
        target = Path(env) if env else Path(keys.DEFAULT_KEY_PATH)
        if create and not target.exists():
            keys.generate(target)
        return keys.public_key_hex(path=target)
    except Exception:  # noqa: BLE001 -- an unreadable key must not block enrollment
        return ""


def token_command() -> str:
    """The credential helper awsettings runs to get the current login's bearer."""
    return f'"{sys.executable}" -m adk.sync.token'


def configure_settings_sync(portal_url: str, identity_url: str,
                            install_hooks: bool = True) -> tuple[int, str]:
    """Point awsettings at this user's store and device list, and install the
    Claude Code hooks at user level (every project, every awsh session).

    Returns (exit code, one line to print). Never raises.
    """
    if (os.getenv("AITHER_SETTINGS_SYNC") or "").strip().lower() in ("0", "false", "off", "no"):
        return 0, "Settings sync: off (AITHER_SETTINGS_SYNC=0)"
    try:
        from awsettings.cli import main as awsettings_main
    except Exception as exc:  # noqa: BLE001
        return 2, f"Settings sync: not configured (awsettings unavailable: {exc})"
    argv = [
        "--user", "--quiet", "enroll",
        "--url", portal_url.rstrip("/") + SETTINGS_PATH,
        "--keys-url", identity_url.rstrip("/") + SEAL_KEYS_PATH,
        "--token-command", token_command(),
    ]
    if not install_hooks:
        argv.append("--no-hooks")
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            rc = awsettings_main(argv)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001
        return 2, f"Settings sync: not configured ({exc})"
    if rc == 0:
        return 0, "Settings sync: on (signed; Claude Code hooks installed at user level)"
    return rc, f"Settings sync: not configured (awsettings enroll exited {rc})"


def registration_fields(facet: str = "daemon") -> dict:
    """Extra fields for the enrollment payload: the seal key (absent without awseal)
    and this machine's id + the facet registering, so Identity answers with the ONE
    node id this computer already has instead of minting another."""
    pub: Optional[str] = seal_public_key()
    out = {"seal_pubkey": pub} if pub else {}
    try:
        mid = machine_id()
    except Exception:  # noqa: BLE001 -- no machine id: registers as before (by node_id)
        mid = ""
    if mid:
        out.update({"machine_id": mid, "facet": effective_facet(facet)})
    return out


# ── One device, many facets (B1/B3) ─────────────────────────────────────────
#
# A computer runs several Aither programs (the adk daemon, the node_beat autostart,
# Desk, awnode). Each used to mint its own node id, so one machine showed up as two
# or three devices. They now share ~/.aither/device.json:
#
#   {"machine_id": <hash of the OS machine id>, "node_id": <the id Identity answered>,
#    "facets": {"daemon": {...}, "desk": {...}}, "leader": {facet, pid, ts, interval}}
#
# machine_id is a one-way hash of the OS's own machine id (Windows MachineGuid,
# /etc/machine-id, macOS IOPlatformUUID), so it survives a reinstall of every Aither
# program and the raw id never leaves the box. Identity stores it per tenant
# (tenant_machine_id), so the same computer in two workspaces is not linkable.
# Desk computes the same value in electron/device-identity.cjs; both are pinned to
# MACHINE_ID_VECTORS below.

import hashlib
import hmac
import json
import time
from pathlib import Path

MACHINE_ID_DOMAIN = "aither-machine-id/1"
DEVICE_FILE_ENV = "AITHER_DEVICE_FILE"

#: Shared test vector (raw OS id, tenant) -> (machine_id, tenant_machine_id). The JS
#: implementation asserts the same three values.
MACHINE_ID_VECTORS = [
    {"raw": "4C4C4544-0042-3510-8051-B4C04F4E4B32", "tenant": "tnt_acme",
     "machine_id": "b29b563a42ede6b8309258b8e2d048f4d51649d3f4e62c45b75decd7e2cfa02b",
     "tenant_machine_id": "d7e41ebcc79d72015735b98b7084bab5a45ee4228c7721d5117075b55426028b"},
]

#: Who beats for the device when several facets run (B3). Higher wins at once;
#: equal priority waits for the holder to miss LEASE_MISSED intervals.
FACET_PRIORITY = {"awnode": 3, "daemon": 2, "node_beat": 2, "desk": 1}
LEASE_MISSED = 3


def device_file() -> Path:
    env = (os.getenv(DEVICE_FILE_ENV) or "").strip()
    return Path(env) if env else Path.home() / ".aither" / "device.json"


def load_device(path: Optional[Path] = None) -> dict:
    try:
        data = json.loads((path or device_file()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 -- absent or unreadable is "nothing recorded"
        return {}


def save_device(data: dict, path: Optional[Path] = None) -> bool:
    """Atomic replace. False when it could not be written (never raises)."""
    target = path or device_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except Exception:  # noqa: BLE001
        return False


def os_machine_id() -> str:
    """The OS's own machine id, "" when this platform does not offer one."""
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
        if sys.platform == "darwin":
            import re
            import subprocess
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            return m.group(1).strip() if m else ""
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                v = Path(p).read_text(encoding="utf-8").strip()
                if v:
                    return v
            except OSError:
                continue
    except Exception:  # noqa: BLE001 -- no OS id: the caller falls back to a stored one
        return ""
    return ""


def machine_key(raw: str) -> str:
    """One-way, tenant-free hash of a raw OS machine id (what the device sends)."""
    return hashlib.sha256(f"{MACHINE_ID_DOMAIN}\n{raw.strip().lower()}".encode()).hexdigest()


def tenant_machine_id(tenant_id: str, key: str) -> str:
    """The per-tenant id Identity stores: HMAC-SHA256(tenant, machine_key)."""
    return hmac.new(tenant_id.encode(), key.encode(), hashlib.sha256).hexdigest()


def machine_id(tenant_id: str = "", path: Optional[Path] = None) -> str:
    """This computer's machine id (tenant-scoped when ``tenant_id`` is given).

    Prefers the OS id, so a reinstall gets the same value; without one, a random id
    is kept in device.json. The value is recorded in device.json either way.
    """
    data = load_device(path)
    raw = os_machine_id()
    host_key = _wsl_host_key()
    if host_key:
        # A WSL distro is a facet of the Windows PC it runs on, not a second device.
        key, source = host_key, "wsl-host"
    elif raw:
        key = machine_key(raw)
        source = "os"
    elif data.get("machine_id"):
        key, source = str(data["machine_id"]), str(data.get("machine_id_source") or "stored")
    else:
        import uuid
        key, source = machine_key(uuid.uuid4().hex), "random"
    if data.get("machine_id") != key:
        data.update({"machine_id": key, "machine_id_source": source})
        save_device(data, path)
    return tenant_machine_id(tenant_id, key) if tenant_id else key


def _wsl_host_key() -> str:
    try:
        from adk.host_identity import wsl_host_machine_key
        return wsl_host_machine_key()
    except Exception:  # noqa: BLE001 -- no host file: this distro registers on its own id
        return ""


def effective_facet(facet: str) -> str:
    """The facet name Identity sees. Inside WSL every program is ``wsl-<distro>``,
    so the distro reads as one facet of the Windows PC beside its ``daemon``."""
    try:
        from adk.device_class import is_wsl, wsl_distro
        from adk.host_identity import _is_windows
        if not _is_windows() and is_wsl() and _wsl_host_key():
            import re
            name = re.sub(r"[^a-z0-9_-]", "-", wsl_distro().lower())[:27] or "wsl"
            return f"wsl-{name}"
    except Exception:  # noqa: BLE001
        pass
    return facet


def record_facet(facet: str, node_id: str, *, capabilities: Optional[list] = None,
                 path: Optional[Path] = None) -> None:
    """Note in device.json that ``facet`` runs here under ``node_id`` (Identity's answer)."""
    data = load_device(path)
    facets = data.get("facets") if isinstance(data.get("facets"), dict) else {}
    facets[facet] = {"node_id": node_id, "pid": os.getpid(), "at": int(time.time()),
                     "capabilities": list(capabilities or [])}
    data["facets"] = facets
    if node_id:
        data["node_id"] = node_id
    save_device(data, path)


def claim_lease(facet: str, interval: float, *, pid: Optional[int] = None,
                now: Optional[float] = None, path: Optional[Path] = None) -> bool:
    """True when this process should send the device's heartbeat now (and renews it).

    The holder keeps the lease while it renews; a higher-priority facet takes it at
    once; anyone else takes it after the holder misses LEASE_MISSED intervals. A
    device.json that cannot be written answers True: two beats beat none.
    """
    pid = os.getpid() if pid is None else pid
    now = time.time() if now is None else now
    data = load_device(path)
    lease = data.get("leader") if isinstance(data.get("leader"), dict) else {}
    mine = lease.get("pid") == pid and lease.get("facet") == facet
    if lease and not mine:
        held_for = float(lease.get("interval") or interval)
        stale = now - float(lease.get("ts") or 0) > held_for * LEASE_MISSED
        outranks = FACET_PRIORITY.get(facet, 0) > FACET_PRIORITY.get(str(lease.get("facet")), 0)
        if not (stale or outranks):
            return False
    data["leader"] = {"facet": facet, "pid": pid, "ts": now, "interval": interval}
    save_device(data, path)
    return True


def release_lease(facet: str, *, pid: Optional[int] = None,
                  path: Optional[Path] = None) -> None:
    """Drop the lease if this process holds it, so the next facet beats at once."""
    pid = os.getpid() if pid is None else pid
    data = load_device(path)
    lease = data.get("leader") if isinstance(data.get("leader"), dict) else {}
    if lease.get("pid") == pid and lease.get("facet") == facet:
        data.pop("leader", None)
        save_device(data, path)
