"""Household LAN inventory: every device the home network has seen, passively.

``adk home devices scan`` reads the OS neighbor table (the ARP / NDP cache the
kernel already keeps), optionally sends ONE SSDP M-SEARCH so UPnP devices (TVs,
speakers, cameras, routers) answer and land in that cache, and diffs the result
against the last scan so a device that was never seen before is flagged
``new: true`` -- the seed for a "new device joined your network" alert.

What it deliberately does NOT do: active port scanning, ping sweeps, nmap. A
device that has not talked to this machine (or answered SSDP) is not listed;
that is the price of staying passive.

Not included, and why:
  * mDNS browse -- ``zeroconf`` is not an awdk dependency, so it is not added.
  * MAC vendor (OUI) lookup -- the repo bundles no OUI table, so ``vendor`` is
    ``None``. :func:`vendor_for_mac` is the single seam to fill when one lands.

Parsers are pure functions over captured command output (see the tests); only
:func:`read_neighbor_table`, :func:`ssdp_discover`, :func:`resolve_hostnames`
and :func:`scan` touch the OS or the network.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("adk.home.lan_inventory")

STATE_NAME = "lan_inventory.json"
STATE_VERSION = 1

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_TIMEOUT_S = 2.0
#: Bounds on what one scan accepts from the LAN: anyone on it can answer SSDP.
SSDP_MAX_IPS = 256
SSDP_HEADER_MAX = 256
SSDP_ST_MAX = 16
HOSTNAME_MAX = 253
#: The saved inventory never grows past this many devices (see ``_cap_inventory``).
MAX_INVENTORY_ROWS = 1024
#: An SSDP-only address (no neighbor-table row) is kept only inside these: a reply
#: claiming to come from a public address is not "a device on your home network".
LOCAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "fe80::/10", "fc00::/7"))
#: Absolute paths only: no PATH lookup for the tools this module runs.
LINUX_IP_PATHS = ("/usr/sbin/ip", "/sbin/ip")
BSD_ARP_PATHS = ("/usr/sbin/arp", "/sbin/arp")
SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    "ST: ssdp:all\r\n"
    "\r\n"
)

Neighbor = Dict[str, Optional[str]]

_IPV4 = r"(?:\d{1,3}\.){3}\d{1,3}"
_MAC_ANY = r"[0-9A-Fa-f]{1,2}(?:[:-][0-9A-Fa-f]{1,2}){5}"


# ── MAC helpers ──────────────────────────────────────────────────────────────

def normalize_mac(raw: str) -> Optional[str]:
    """``A4-2B-B0-1-2-3`` / ``a4:2b:b0:01:02:03`` -> ``a4:2b:b0:01:02:03``.

    Args:
        raw: a MAC in any of the separators/paddings the three OSes print.

    Returns:
        The lower-case, colon-separated, zero-padded MAC, or ``None`` when it is
        not a usable unicast hardware address (incomplete, all-zero, broadcast,
        or multicast -- those are not devices).
    """
    parts = re.split(r"[:-]", (raw or "").strip())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9A-Fa-f]{1,2}", p) for p in parts):
        return None
    mac = ":".join(p.lower().zfill(2) for p in parts)
    if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return None
    if int(parts[0], 16) & 0x01:  # group bit: multicast (01:00:5e.., 33:33..)
        return None
    return mac


def _usable_ip(ip: str) -> bool:
    """IPv4 unicast only: drop multicast (224/4), limited broadcast and 0.0.0.0."""
    try:
        octets = [int(o) for o in ip.split(".")]
    except ValueError:
        return False
    if len(octets) != 4 or any(o > 255 for o in octets):
        return False
    if octets[0] >= 224 or octets == [0, 0, 0, 0]:
        return False
    return True


_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_text(value: Any, limit: int = SSDP_HEADER_MAX) -> Optional[str]:
    """A string that came off the network (SSDP header, PTR name), safe to print:
    control and bidi-override characters removed, at most ``limit`` characters.

    Returns:
        The cleaned text, or ``None`` when nothing printable is left.
    """
    if value is None:
        return None
    text = _CONTROL.sub("", str(value)).strip()[:limit]
    return text or None


def is_local_ip(ip: str) -> bool:
    """True for an address on a home network: RFC 1918, link-local, IPv6 ULA."""
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in LOCAL_NETS)


def vendor_for_mac(mac: Optional[str]) -> Optional[str]:
    """Vendor from the MAC's OUI. Always ``None``: no OUI table is bundled yet.

    Args:
        mac: a normalized MAC.

    Returns:
        ``None`` (the seam a bundled prefix table plugs into).
    """
    return None


def _entry(ip: str, mac: str, hostname: Optional[str] = None) -> Optional[Neighbor]:
    norm = normalize_mac(mac)
    if norm is None or not _usable_ip(ip):
        return None
    return {"ip": ip, "mac": norm, "hostname": clean_text(hostname, HOSTNAME_MAX)}


def _dedupe(rows: Iterable[Optional[Neighbor]]) -> List[Neighbor]:
    seen: Dict[Tuple[str, str], Neighbor] = {}
    for r in rows:
        if r is not None:
            seen.setdefault((str(r["ip"]), str(r["mac"])), r)
    return list(seen.values())


# ── neighbor-table parsers (pure) ────────────────────────────────────────────

_WIN_ROW = re.compile(rf"^\s*({_IPV4})\s+({_MAC_ANY})\s+(\S+)\s*$")


def parse_windows_arp(text: str) -> List[Neighbor]:
    """Parse Windows ``arp -a`` output (any display language: rows are matched,
    headers ignored).

    Args:
        text: the command's stdout.

    Returns:
        Unicast neighbors, one per (ip, mac).
    """
    return _dedupe(_entry(m.group(1), m.group(2))
                   for m in map(_WIN_ROW.match, text.splitlines()) if m)


_IP_NEIGH_ROW = re.compile(rf"^\s*({_IPV4})\s.*?\blladdr\s+({_MAC_ANY})\b(.*)$")


def parse_linux_ip_neigh(text: str) -> List[Neighbor]:
    """Parse Linux ``ip neigh`` output; FAILED / INCOMPLETE rows (no lladdr) and
    IPv6 rows are skipped.

    Args:
        text: the command's stdout.

    Returns:
        Unicast IPv4 neighbors.
    """
    out: List[Optional[Neighbor]] = []
    for line in text.splitlines():
        m = _IP_NEIGH_ROW.match(line)
        if m and "FAILED" not in m.group(3) and "INCOMPLETE" not in m.group(3):
            out.append(_entry(m.group(1), m.group(2)))
    return _dedupe(out)


def parse_proc_net_arp(text: str) -> List[Neighbor]:
    """Parse Linux ``/proc/net/arp``; flags ``0x0`` (incomplete) rows are skipped.

    Args:
        text: the file's content.

    Returns:
        Unicast IPv4 neighbors.
    """
    out: List[Optional[Neighbor]] = []
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 4 or not re.fullmatch(_IPV4, cols[0]):
            continue
        try:
            flags = int(cols[2], 16)
        except ValueError:
            continue
        if flags & 0x2:  # ATF_COM: the entry is complete
            out.append(_entry(cols[0], cols[3]))
    return _dedupe(out)


_MAC_ROW = re.compile(rf"^\s*(\S+)\s+\(({_IPV4})\)\s+at\s+({_MAC_ANY})\b")


def parse_macos_arp(text: str) -> List[Neighbor]:
    """Parse macOS / BSD ``arp -an`` (or ``arp -a``: then the hostname is kept).

    macOS drops leading zeros (``0:11:32:a:b:c``); :func:`normalize_mac` pads.

    Args:
        text: the command's stdout.

    Returns:
        Unicast IPv4 neighbors; ``(incomplete)`` rows are skipped.
    """
    out: List[Optional[Neighbor]] = []
    for line in text.splitlines():
        m = _MAC_ROW.match(line)
        if m:
            host = m.group(1)
            out.append(_entry(m.group(2), m.group(3), None if host == "?" else host))
    return _dedupe(out)


# ── SSDP (pure parser + one M-SEARCH) ────────────────────────────────────────

def parse_ssdp_response(data: Any) -> Optional[Dict[str, str]]:
    """Parse one SSDP M-SEARCH reply (``HTTP/1.1 200 OK`` + headers).

    Args:
        data: the datagram, bytes or str.

    Returns:
        Lower-cased header name -> value, or ``None`` when it is not a 200
        response (NOTIFY chatter, garbage).
    """
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or not re.match(r"^HTTP/1\.[01]\s+200\b", lines[0].strip(), re.I):
        return None
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip().lower()[:64]
        if key and len(headers) < 32:
            headers[key] = clean_text(v) or ""
    return headers


def ssdp_discover(timeout: float = SSDP_TIMEOUT_S) -> List[Dict[str, Any]]:
    """Send one SSDP M-SEARCH (ssdp:all) and collect replies for ``timeout`` s.

    Args:
        timeout: seconds to listen (default 2).

    Returns:
        One ``{ip, server, location, st: [..]}`` per responding IP. Socket
        errors (no multicast route, firewall) give ``[]`` and a debug log.
        Bounded: replies from non-local addresses are ignored, at most
        ``SSDP_MAX_IPS`` addresses and ``SSDP_ST_MAX`` ST values each are kept,
        and the wall-clock budget is ``timeout`` whatever the LAN sends.
    """
    found: Dict[str, Dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.sendto(SSDP_MSEARCH.encode("ascii"), (SSDP_ADDR, SSDP_PORT))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(min(0.25, left))
            try:
                data, (ip, _port) = sock.recvfrom(65507)
            except socket.timeout:
                continue
            _ssdp_merge(found, ip, parse_ssdp_response(data))
    except OSError as exc:
        logger.debug("ssdp discover failed: %s", exc)
    finally:
        sock.close()
    return list(found.values())


def _ssdp_merge(found: Dict[str, Dict[str, Any]], ip: str,
                hdr: Optional[Dict[str, str]]) -> None:
    """Fold one parsed reply into ``found``, inside the bounds (pure; tested)."""
    if hdr is None or not is_local_ip(ip):
        return
    rec = found.get(ip)
    if rec is None:
        if len(found) >= SSDP_MAX_IPS:
            return
        rec = found[ip] = {"ip": ip, "server": None, "location": None, "st": []}
    rec["server"] = rec["server"] or hdr.get("server")
    rec["location"] = rec["location"] or hdr.get("location")
    st = hdr.get("st")
    if st and st not in rec["st"] and len(rec["st"]) < SSDP_ST_MAX:
        rec["st"].append(st)


# ── OS reads ─────────────────────────────────────────────────────────────────

def _run(cmd: List[str]) -> Optional[str]:
    kwargs: Dict[str, Any] = {"capture_output": True, "text": True,
                              "encoding": "utf-8", "errors": "replace", "timeout": 10}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(cmd, check=False, **kwargs)  # noqa: S603 - fixed argv
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("%s failed: %s", cmd[0], exc)
        return None
    return proc.stdout if proc.returncode == 0 else None


def windows_arp_path() -> str:
    """``%SystemRoot%\\System32\\ARP.EXE`` (never a PATH lookup)."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return str(Path(root) / "System32" / "ARP.EXE")


def _existing(paths: Iterable[str]) -> List[str]:
    """The absolute tool paths that exist, in order (tests monkeypatch this)."""
    return [p for p in paths if os.path.isfile(p)]


def read_neighbor_table(platform: Optional[str] = None) -> List[Neighbor]:
    """Read this machine's IPv4 neighbor table.

    Args:
        platform: ``sys.platform`` override (tests).

    Returns:
        Unicast neighbors.

    Raises:
        RuntimeError: no neighbor-table source works on this OS (the CLI turns
            it into exit 2 -- "could not judge", never an empty success).
    """
    plat = platform or sys.platform
    if plat == "win32":
        out = _run([windows_arp_path(), "-a"])
        if out is not None:
            return parse_windows_arp(out)
    elif plat == "darwin" or plat.startswith(("freebsd", "openbsd", "netbsd")):
        for arp in _existing(BSD_ARP_PATHS):
            out = _run([arp, "-an"])
            if out is not None:
                return parse_macos_arp(out)
    else:
        for ip_tool in _existing(LINUX_IP_PATHS):
            out = _run([ip_tool, "-4", "neigh", "show"])
            if out is not None:
                return parse_linux_ip_neigh(out)
        try:
            return parse_proc_net_arp(Path("/proc/net/arp").read_text(encoding="utf-8"))
        except OSError as exc:
            logger.debug("/proc/net/arp unreadable: %s", exc)
    raise RuntimeError(f"no neighbor table readable on {plat} "
                       "(tried arp / ip neigh / /proc/net/arp)")


def resolve_hostnames(ips: Iterable[str], timeout: float = 2.0) -> Dict[str, str]:
    """Reverse-DNS (PTR) the given IPs in parallel, bounded by ``timeout`` total.

    Args:
        ips: addresses to look up.
        timeout: overall seconds; slow lookups are abandoned, not awaited.

    Returns:
        ip -> hostname for the lookups that answered in time.
    """
    ips = list(dict.fromkeys(ips))[:SSDP_MAX_IPS]
    if not ips:
        return {}
    got: Dict[str, str] = {}
    lock = threading.Lock()
    todo = list(ips)

    def _worker() -> None:
        while True:
            with lock:
                if not todo:
                    return
                ip = todo.pop()
            try:
                name = clean_text(socket.gethostbyaddr(ip)[0], HOSTNAME_MAX)
            except (OSError, UnicodeError):
                name = None
            if name:
                with lock:
                    got[ip] = name

    # daemon threads: a PTR lookup stuck past the budget is abandoned, and it cannot
    # hold the process open at exit the way a ThreadPoolExecutor's workers do
    threads = [threading.Thread(target=_worker, daemon=True, name="adk-ptr")
               for _ in range(min(16, len(ips)))]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    with lock:
        return dict(got)


# ── inventory state + diff (pure) ────────────────────────────────────────────

def device_key(rec: Dict[str, Any]) -> str:
    """Stable identity: the MAC; an SSDP-only device without one keys on its IP."""
    return str(rec.get("mac") or f"ip:{rec.get('ip')}")


def merge_inventory(previous: Dict[str, Dict[str, Any]],
                    observed: Iterable[Dict[str, Any]],
                    now: str) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Fold one scan into the known-device map and flag what is new.

    Args:
        previous: key -> record from the last saved inventory.
        observed: this scan's ``{ip, mac, hostname?, vendor?, ssdp?}`` rows.
        now: ISO-8601 timestamp of this scan.

    Returns:
        ``(known, seen_now)``: ``known`` is the updated map (devices not seen
        this time are kept with their old ``last_seen``, ``new`` cleared);
        ``seen_now`` is this scan's devices, ``new: True`` for those whose key
        was absent from ``previous``.
    """
    known: Dict[str, Dict[str, Any]] = {k: {**v, "new": False} for k, v in previous.items()}
    seen_now: Dict[str, Dict[str, Any]] = {}
    for obs in observed:
        key = device_key(obs)
        old = previous.get(key)
        rec: Dict[str, Any] = {
            "ip": obs.get("ip"),
            "mac": obs.get("mac"),
            "hostname": obs.get("hostname") or (old or {}).get("hostname"),
            "vendor": obs.get("vendor") or (old or {}).get("vendor"),
            "first_seen": (old or {}).get("first_seen") or now,
            "last_seen": now,
            "new": old is None,
        }
        if obs.get("ssdp") or (old or {}).get("ssdp"):
            rec["ssdp"] = obs.get("ssdp") or (old or {}).get("ssdp")
        if key in seen_now:  # same device twice in one scan: keep the first, merge ssdp
            seen_now[key].setdefault("ssdp", rec.get("ssdp"))
            continue
        known[key] = rec
        seen_now[key] = rec
    _cap_inventory(known, seen_now)
    return known, list(seen_now.values())


def _cap_inventory(known: Dict[str, Dict[str, Any]], seen_now: Dict[str, Dict[str, Any]],
                   limit: Optional[int] = None) -> None:
    """Hold ``known`` at ``MAX_INVENTORY_ROWS``: evict SSDP-only rows (no MAC: the kind
    anything on the LAN can mint) before MAC rows, rows not seen this scan before rows
    that were, oldest ``last_seen`` first."""
    limit = MAX_INVENTORY_ROWS if limit is None else limit
    over = len(known) - limit
    if over <= 0:
        return
    order = sorted(known, key=lambda k: (bool(known[k].get("mac")), k in seen_now,
                                         str(known[k].get("last_seen") or "")))
    for key in order[:over]:
        known.pop(key, None)
        seen_now.pop(key, None)


def combine(neighbors: Iterable[Neighbor],
            ssdp: Iterable[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Join neighbor rows with SSDP replies by IP; SSDP-only IPs become rows too.

    Args:
        neighbors: parsed neighbor table.
        ssdp: :func:`ssdp_discover` output.

    Returns:
        Observation rows for :func:`merge_inventory`.
    """
    by_ip = {s["ip"]: s for s in ssdp}
    rows: List[Dict[str, Any]] = []
    used = set()
    for n in neighbors:
        row: Dict[str, Any] = dict(n)
        row["vendor"] = vendor_for_mac(n.get("mac"))
        s = by_ip.get(str(n["ip"]))
        if s:
            row["ssdp"] = {k: s.get(k) for k in ("server", "location", "st")}
            used.add(n["ip"])
        rows.append(row)
    for ip, s in by_ip.items():
        if ip not in used and _usable_ip(ip) and is_local_ip(ip):
            rows.append({"ip": ip, "mac": None, "hostname": None, "vendor": None,
                         "ssdp": {k: s.get(k) for k in ("server", "location", "st")}})
    return rows


def state_path(root: Optional[Path] = None) -> Path:
    """``<agent home>/lan_inventory.json`` (the dir ``adk home`` already uses)."""
    if root is None:
        from . import config as hc

        root = hc.home_dir()
    return Path(root) / STATE_NAME


def load_state(path: Path) -> Dict[str, Dict[str, Any]]:
    """Known devices from ``path``; a missing file is an empty inventory.

    Raises:
        ValueError: the file exists but is not a valid inventory (refuse to
            overwrite it silently -- every device would re-flag as new).
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not a readable inventory: {exc}") from exc
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict):
        raise ValueError(f"{path} has no 'devices' map")
    return devices


def save_state(path: Path, devices: Dict[str, Dict[str, Any]], now: str) -> None:
    """Atomically write the inventory, owner-only (a unique tmp, restricted before the
    content lands, then replace): it maps every device in the home.

    Raises:
        PermissionError: the file could not be restricted to its owner.
    """
    from .._private_file import write_private_text

    write_private_text(path, json.dumps({"version": STATE_VERSION, "updated": now,
                                         "devices": devices}, indent=2, sort_keys=True))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def scan(*, use_ssdp: bool = True, resolve: bool = False,
         path: Optional[Path] = None, now: Optional[str] = None) -> Dict[str, Any]:
    """One passive scan: SSDP (warms the ARP cache) -> neighbor table -> diff -> save.

    Args:
        use_ssdp: send one SSDP M-SEARCH first.
        resolve: reverse-DNS the IPs without a hostname.
        path: inventory file (default :func:`state_path`).
        now: timestamp override (tests).

    Returns:
        ``{"devices": [...], "new": [...], "state": str, "first_scan": bool}``.
        The first scan is the baseline: every device is recorded, none is new.

    Raises:
        RuntimeError: no neighbor table is readable.
        ValueError: the saved inventory is corrupt.
    """
    path = path or state_path()
    now = now or _now()
    previous = load_state(path)
    replies = ssdp_discover() if use_ssdp else []
    rows = combine(read_neighbor_table(), replies)
    if resolve:
        names = resolve_hostnames(str(r["ip"]) for r in rows if not r.get("hostname"))
        for r in rows:
            r["hostname"] = r.get("hostname") or names.get(str(r["ip"]))
    known, seen = merge_inventory(previous, rows, now)
    if not previous:  # the first scan is the baseline: nothing "joined", so no alert seed
        for rec in known.values():
            rec["new"] = False
    save_state(path, known, now)
    seen.sort(key=lambda r: tuple(int(o) for o in str(r["ip"]).split(".")))
    return {"devices": seen, "new": [r for r in seen if r["new"]],
            "state": str(path), "first_scan": not previous}
