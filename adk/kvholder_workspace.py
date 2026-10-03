"""The workspace relay: phones lend memory from anywhere, signed in as workspace devices.

``adk kvholder phone`` is a relay a person sets up by hand: a USB cable, a LAN address or a
tunnel URL, and a token copied into a link. This module is the durable form of the same
relay:

- **Reach.** The relay's holder port is published at a fixed public host
  (``kv.aitherium.com``, a Cloudflare tunnel route to this machine's mesh address). The public
  listener serves only the holder page, ``holder.js`` and the ``/holder`` WebSocket; the status
  JSON, the swarm view and token minting stay on loopback. The relay's master token is
  refused on the public listener.
- **Identity.** A holder signs in as a DEVICE of the owner's workspace, not with a copied
  token. Every enrolled device has an Ed25519 key whose public half is in its enrollment
  record (``adk enroll``, or the pairing code flow the Android app uses). The holder signs
  ``aither-kvholder-hello/1`` over this relay's host, its device id, a timestamp and a
  nonce; the relay checks the signature against the owner's enrolled keys
  (``GET {identity}/v1/nodes/seal-keys``, owner-scoped).
- **Owner policy.** A signed-in device lends only after the owner allows it on this machine
  (``adk kvholder workspace allow <device>``). Nothing is allowed by default, so a child's
  phone never lends unless the owner says so. ``deny`` takes effect within one sweep, and
  removing the device from the workspace (``adk devices rm``) revokes it at the next key
  refresh.

Nothing here prints or stores a token in a status file. Stdlib + ``cryptography`` (an awdk
dependency).
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

HELLO_DOMAIN = "aither-kvholder-hello/1"
DEFAULT_PUBLIC_HOST = "kv.aitherium.com"
CLOCK_SKEW_S = 120.0
KEYS_REFRESH_S = 60.0
KEYS_STALE_S = 600.0  # an identity outage longer than this stops NEW sign-ins (fail closed)
SWEEP_S = 5.0
_DEVICE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")


# ---------------------------------------------------------------- the signed hello


def hello_message(relay_id: str, device_id: str, ts: int, nonce: str) -> bytes:
    """The exact bytes a device signs. Binding the relay host stops a hello for one relay
    being replayed at another; the nonce and timestamp stop a replay at this one."""
    return "\n".join([HELLO_DOMAIN, relay_id.lower(), device_id, str(int(ts)), nonce]).encode()


def sign_hello(private_key: Any, relay_id: str, device_id: str) -> dict:
    """The hello fields a holder adds to sign in as ``device_id`` (an Ed25519 private key)."""
    ts, nonce = int(time.time()), secrets.token_urlsafe(12)
    sig = private_key.sign(hello_message(relay_id, device_id, ts, nonce))
    return {
        "auth": "device",
        "device_id": device_id,
        "relay": relay_id.lower(),
        "ts": ts,
        "nonce": nonce,
        "sig": base64.b64encode(sig).decode(),
    }


def device_hello(relay_url: str, device_id: str = "", key_path: str = "") -> Callable[[], dict]:
    """For a Python holder: sign each hello with this machine's enrolled device key (awseal).

    The device id defaults to this machine's enrolled node id."""
    from awseal import keys  # optional dependency: pip install awdk[seal]

    env = (os.getenv(keys.KEY_PATH_ENV) or "").strip()
    priv = keys.load_private_key(Path(key_path or env or keys.DEFAULT_KEY_PATH))
    if not device_id:
        from adk import devices

        device_id = devices._local_node_id()
    host = urllib.parse.urlsplit(relay_url).hostname or ""
    return lambda: sign_hello(priv, host, device_id)


def verify_sig(pub_hex: str, msg: bytes, sig_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        key.verify(base64.b64decode(sig_b64, validate=True), msg)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---------------------------------------------------------------- workspace device keys


class SealKeys:
    """The owner's enrolled devices' public keys, refreshed from identity.

    ``fetch`` returns ``(status, {device_id: pubkey_hex})``. An outage keeps the last good
    set (attached holders are not dropped because identity blinked) but only for
    ``KEYS_STALE_S``; after that no NEW device signs in until identity answers again. A 200
    is authoritative: a device missing from it is revoked.
    """

    def __init__(self, fetch: Callable[[], tuple[int, dict[str, str]]] | None = None):
        self._fetch = fetch or fetch_seal_keys
        self.keys: dict[str, str] = {}
        self.at = 0.0  # last authoritative answer
        self.error = ""
        self.lock = threading.Lock()

    def refresh(self) -> bool:
        try:
            status, keys = self._fetch()
        except Exception as e:  # noqa: BLE001 - any transport failure is an outage, not a list
            status, keys = 0, {}
            self.error = f"identity unreachable: {type(e).__name__}"
        if status == 200:
            with self.lock:
                self.keys, self.at, self.error = dict(keys), time.time(), ""
            return True
        if status:
            self.error = f"identity answered {status}"
        return False

    def get(self, device_id: str) -> str:
        with self.lock:
            if time.time() - self.at > KEYS_STALE_S:
                return ""
            return self.keys.get(device_id, "")

    def authoritative(self) -> dict[str, str] | None:
        """The last 200's keys while fresh, else None (do not revoke on an outage)."""
        with self.lock:
            return dict(self.keys) if time.time() - self.at <= KEYS_STALE_S else None


def fetch_seal_keys(ids: Callable[[], list[str]] | None = None) -> tuple[int, dict[str, str]]:
    """The owner's device keys. ``GET /v1/nodes/seal-keys`` (one directory SEARCH) first.

    When that fails and ``ids`` names the devices the owner allowed here, each is read
    with ``GET /v1/nodes/{id}`` (a directory read by name, which keeps answering while
    searches starve; measured 2026-10-03: 503 after 25 s vs 200 in 1.1 s). That answer is
    authoritative only for those ids: a 404 means removed. Any other status is an outage.
    """
    from adk import devices

    status, _text, parsed, _url = devices.request_nodes("GET", "/seal-keys", timeout=30.0)
    keys: dict[str, str] = {}
    if status == 200 and isinstance(parsed, dict):
        for row in parsed.get("keys") or []:
            did, pub = str(row.get("device_id") or ""), str(row.get("seal_pubkey") or "")
            if did and re.fullmatch(r"[0-9a-f]{64}", pub):
                keys[did] = pub
        return status, keys
    wanted = [d for d in (ids() if ids else []) if _DEVICE_ID.match(d)]
    if not wanted:
        return status, keys
    for did in wanted:
        st, _t, node, _u = devices.request_nodes("GET", "/" + urllib.parse.quote(did), timeout=30.0)
        if st == 404:
            continue
        if st != 200 or not isinstance(node, dict) or node.get("node_id") != did:
            return (st if st and st != 200 else 502), {}  # an outage, never an empty list
        pub = str(node.get("seal_pubkey") or "")
        if re.fullmatch(r"[0-9a-f]{64}", pub):
            keys[did] = pub
    return 200, keys


def _file_keys(path: str) -> Callable[[], tuple[int, dict[str, str]]]:
    """A seal-keys answer read from a file, re-read on every refresh (edit it to revoke)."""

    def fetch() -> tuple[int, dict[str, str]]:
        rows = json.loads(Path(path).read_text(encoding="utf-8")).get("keys") or []
        return 200, {
            str(r["device_id"]): str(r["seal_pubkey"])
            for r in rows
            if re.fullmatch(r"[0-9a-f]{64}", str(r.get("seal_pubkey", "")))
        }

    return fetch


# ---------------------------------------------------------------- owner policy


def grants_path() -> Path:
    return Path(
        os.environ.get("AITHER_KVHOLDER_GRANTS")
        or Path.home() / ".aither" / "kvholder" / "grants.json"
    )


def _write_private(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, p)


class Grants:
    """Which workspace devices may lend to this relay. Absent = not allowed."""

    def __init__(self, path: Path | None = None):
        self.path = path or grants_path()
        self._mtime = -1.0
        self.devices: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            mt = 0.0
        if mt == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if mt else {}
        except (OSError, ValueError):
            data = {}
        with self.lock:
            self.devices = {
                k: v for k, v in (data.get("devices") or {}).items() if isinstance(v, dict)
            }
            self._mtime = mt

    def save(self) -> None:
        with self.lock:
            _write_private(self.path, {"devices": self.devices})
        self._mtime = -1.0

    def allowed(self, device_id: str) -> bool:
        with self.lock:
            return bool(self.devices.get(device_id, {}).get("lend"))

    def allowed_ids(self) -> list[str]:
        self.reload()
        with self.lock:
            return sorted(k for k, v in self.devices.items() if v.get("lend"))

    def max_bytes(self, device_id: str) -> int | None:
        with self.lock:
            mb = self.devices.get(device_id, {}).get("max_mb")
        return int(mb) << 20 if isinstance(mb, int) and mb > 0 else None

    def set(self, device_id: str, lend: bool, **fields: Any) -> None:
        self.reload()
        with self.lock:
            row = self.devices.setdefault(device_id, {})
            row.update({k: v for k, v in fields.items() if v is not None})
            row["lend"] = bool(lend)
            row["changed"] = time.time()
        self.save()

    def note_refused(self, device_id: str, name: str) -> None:
        """A signed-in device the owner has not allowed: listed so the owner can decide."""
        with self.lock:
            row = self.devices.get(device_id)
            if row is not None and "asked" in row and time.time() - row["asked"] < 60:
                return
        self.reload()
        with self.lock:
            row = self.devices.setdefault(device_id, {"lend": False})
            row["asked"], row["name"] = time.time(), row.get("name") or name[:80]
        self.save()


# ---------------------------------------------------------------- the gate the relay calls


FAMILY_DEVICES_URL = "https://api.aitherium.com/api/tutor/family/devices"


class Household:
    """The owner's household device registry (family devices: profile, kv_lend, revoked).

    ``kv_lend`` is the owner's switch, off by default for every device; a child's phone lends
    only when the owner turned it on there. Same outage rule as SealKeys: the last 200 holds
    for ``KEYS_STALE_S``, then nothing in it is trusted.
    """

    def __init__(self, fetch: Callable[[], tuple[int, dict[str, dict]]] | None = None):
        self._fetch = fetch or fetch_family_devices
        self.rows: dict[str, dict] = {}
        self.at = 0.0
        self.error = ""
        self.lock = threading.Lock()

    def refresh(self) -> bool:
        try:
            status, rows = self._fetch()
        except Exception as e:  # noqa: BLE001 - any transport failure is an outage
            status, rows = 0, {}
            self.error = f"household registry unreachable: {type(e).__name__}"
        if status == 200:
            with self.lock:
                self.rows, self.at, self.error = dict(rows), time.time(), ""
            return True
        if status:
            self.error = f"household registry answered {status}"
        return False

    def fresh(self) -> dict[str, dict] | None:
        with self.lock:
            return dict(self.rows) if time.time() - self.at <= KEYS_STALE_S else None


def fetch_family_devices() -> tuple[int, dict[str, dict]]:
    """``GET /api/tutor/family/devices`` as the signed-in owner (family registry, Genesis)."""
    import urllib.error
    import urllib.request

    from adk import devices

    bearer = devices.resolve_bearer()
    if not bearer:
        return 401, {}
    url = os.environ.get("AITHER_FAMILY_DEVICES_URL") or FAMILY_DEVICES_URL
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (adk kvholder)",  # Cloudflare 403s a Python UA
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            doc = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    rows = {
        str(d["device_id"]): d
        for d in (doc.get("devices") or [])
        if isinstance(d, dict) and d.get("device_id")
    }
    return 200, rows


class DeviceGate:
    """``Relay.device_gate``: admits a signed hello from an allowed workspace device."""

    def __init__(
        self,
        relay_ids: set[str],
        keys: SealKeys,
        grants: Grants,
        household: Household | None = None,
    ):
        self.relay_ids = {r.lower() for r in relay_ids if r}
        self.keys = keys
        self.grants = grants
        self.household = household
        self._nonces: dict[str, float] = {}
        self.lock = threading.Lock()

    def admit(self, hello: dict) -> tuple[str | None, str]:
        """``(device_id, "")`` or ``(None, reason)``. The reason never echoes input."""
        did = str(hello.get("device_id", ""))
        relay_id = str(hello.get("relay", "")).lower()
        nonce, sig = str(hello.get("nonce", "")), str(hello.get("sig", ""))
        ts = hello.get("ts")
        if not _DEVICE_ID.match(did) or not nonce or len(nonce) > 64 or not sig:
            return None, "malformed device hello"
        if relay_id not in self.relay_ids:
            return None, "hello signed for another relay"
        if not isinstance(ts, int) or abs(time.time() - ts) > CLOCK_SKEW_S:
            return None, "device clock is off (or the hello is old)"
        pub = self.keys.get(did)
        if not pub:
            return None, "not a device of this workspace (enroll or pair it first)"
        if not verify_sig(pub, hello_message(relay_id, did, ts, nonce), sig):
            return None, "bad device signature"
        now = time.time()
        with self.lock:
            for n, exp in list(self._nonces.items()):
                if exp < now:
                    self._nonces.pop(n, None)
            if nonce in self._nonces:
                return None, "replayed hello"
            self._nonces[nonce] = now + 2 * CLOCK_SKEW_S
        ok, why = self.may_lend(did)
        if not ok:
            if why == NOT_ALLOWED:
                self.grants.note_refused(did, str(hello.get("device", "")))
            return None, why
        return did, ""

    def may_lend(self, did: str) -> tuple[bool, str]:
        """The owner's decision for ``did``, from every place the owner can make it.

        A household device (a row in the family registry) lends only while that row says
        ``kv_lend`` and is not revoked: a child's phone included, whatever this host's local
        list says. A local ``deny`` always wins. Any other device needs a local ``allow``.
        """
        self.grants.reload()
        with self.grants.lock:
            local = dict(self.grants.devices.get(did) or {})
        if local and not local.get("lend") and "changed" in local:
            return False, "denied by the workspace owner"
        rows = self.household.fresh() if self.household is not None else None
        if self.household is not None and rows is None:
            # fail closed: while the registry cannot answer, no device can be shown NOT to be a
            # child with lending off, so nothing new is admitted (sweep keeps who is attached)
            return False, "household registry unreachable: lending refused until it answers"
        row = rows.get(did) if rows is not None else None
        if row is not None:
            if row.get("revoked"):
                return False, "this device was removed from the household"
            if not row.get("kv_lend"):
                kind = "child " if row.get("profile_kind") == "child" else ""
                return False, f"the owner has not turned on lending for this {kind}device"
            return True, ""
        if did.startswith("fdev_"):  # a household id the registry does not list
            return False, "not in the household registry"
        if not self.grants.allowed(did):
            return False, NOT_ALLOWED
        return True, ""


NOT_ALLOWED = "the workspace owner has not allowed this device to lend"


def device_of(session: str) -> str:
    return session[4:] if session.startswith("dev:") else ""


def sweep(relay: Any, gate: DeviceGate) -> list[str]:
    """Detach device holders the owner denied or the workspace no longer lists."""
    gate.grants.reload()
    known = gate.keys.authoritative()
    dropped = []
    for h in list(relay.holders):
        did = device_of(getattr(h, "session", ""))
        if not did:
            continue
        gone = known is not None and did not in known
        ok, why = gate.may_lend(did)
        if not ok and why.startswith("household registry unreachable"):
            continue  # an outage revokes nothing; new sign-ins wait for the registry
        if gone or not ok:
            relay.revoke(h.session)
            dropped.append(did)
            why = "removed from the workspace" if gone else why
            print(f"kvholder: revoked {did} ({why})", flush=True)
    return dropped


# ---------------------------------------------------------------- status for awsh / awdesk


def workspace_status_path() -> Path:
    return Path(
        os.environ.get("AITHER_KVHOLDER_WORKSPACE_STATUS")
        or Path.home() / ".aither" / "kvholder" / "workspace.json"
    )


def snapshot(relay: Any, gate: DeviceGate, public_url: str) -> dict:
    """What awsh/awdesk read: the swarm by device, the owner's grants, pending devices.

    Never a token: holders appear by device id and display name only."""
    st = relay.status()
    by_dev = {}
    for h, row in zip(list(relay.holders), st.get("holders", [])):
        did = device_of(getattr(h, "session", ""))
        row = dict(row)
        row["device_id"] = did or None
        by_dev[did or f"anon-{len(by_dev)}"] = row
    gate.grants.reload()
    with gate.grants.lock:
        grants = {
            k: {"lend": bool(v.get("lend")), "name": v.get("name", ""), "max_mb": v.get("max_mb")}
            for k, v in gate.grants.devices.items()
        }
    known = gate.keys.authoritative()
    return {
        "relay": public_url,
        "pid": os.getpid(),
        "updated": time.time(),
        "identity": {"ok": known is not None, "error": gate.keys.error or None},
        "household": None
        if gate.household is None
        else {"ok": gate.household.fresh() is not None, "error": gate.household.error or None},
        "enrolled": sorted(known) if known is not None else None,
        "grants": grants,
        "pending": sorted(k for k, v in grants.items() if not v["lend"]),
        "swarm": {
            "holders": list(by_dev.values()),
            "attached": st.get("attached"),
            "configured": st.get("configured"),
            "lent_bytes": st.get("lent_bytes"),
            "used_bytes": st.get("used_bytes"),
            "attn_calls": st.get("attn_calls"),
            "broken": st.get("broken"),
        },
    }


def read_status() -> dict | None:
    try:
        st = json.loads(workspace_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    st["stale"] = time.time() - float(st.get("updated", 0)) > 3 * SWEEP_S
    return st


# ---------------------------------------------------------------- pairing (Android app)


def overlay_ip() -> str:
    """This machine's AitherNet / Tailscale address (100.64.0.0/10), or ""."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    ips = [str(info[4][0]) for info in infos]
    try:  # the source address toward the overlay's own DNS (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("100.100.100.100", 53))
            ips.append(s.getsockname()[0])
    except OSError as e:  # no route to the overlay: the hostname's addresses decide
        print(f"kvholder: no overlay route ({e})", file=sys.stderr)
    for ip in ips:
        a, b = (int(x) for x in ip.split(".")[:2])
        if a == 100 and 64 <= b <= 127:
            return ip
    return ""


def pair_link(relay_url: str, identity: str, code: str, device_id: str, mb: int) -> str:
    q = urllib.parse.urlencode(
        {"r": relay_url, "i": identity, "c": code, "d": device_id, "mb": str(mb)}
    )
    return f"aitherkv://pair?{q}"


def mint_pair_code() -> tuple[int, str, str]:
    """``(status, code, body)`` from identity's pairing/init, as the signed-in owner."""
    from adk import devices

    status, text, parsed, _url = devices.request_nodes("POST", "/pairing/init", timeout=30.0)
    code = str((parsed or {}).get("code", "")) if isinstance(parsed, dict) else ""
    return status, code, text


# ---------------------------------------------------------------- CLI


def register(s) -> None:
    """``adk kvholder workspace ...`` (called from adk.kvholder_cli)."""
    w = s.add_parser(
        "workspace",
        help="The always-on relay: phones lend from anywhere, signed in as workspace devices",
    )
    ws = w.add_subparsers(dest="workspace_action")
    sv = ws.add_parser("serve", help="Run the relay: loopback + the public listener (tunnel)")
    sv.add_argument("--port", type=int, default=50062, help="engine-side PATN port (loopback)")
    sv.add_argument("--web-port", type=int, default=50063, help="holder page + WebSocket port")
    sv.add_argument(
        "--public-bind",
        default="",
        help="address the tunnel reaches (default: this machine's 100.64/10 mesh address)",
    )
    sv.add_argument("--public-host", default=DEFAULT_PUBLIC_HOST, help="the relay's public host")
    sv.add_argument(
        "--keys-file",
        default="",
        help="offline / sovereign: trust the device keys in this JSON file "
        '({"keys": [{"device_id", "seal_pubkey"}]}, the seal-keys shape) instead of identity',
    )
    pr = ws.add_parser("pair", help="Pair a phone: mint a code, allow it, hand it the app link")
    pr.add_argument("--name", required=True, help="device id to give the phone, e.g. fold")
    pr.add_argument("--max-mb", type=int, default=2048, help="memory it lends (owner's cap)")
    pr.add_argument("--serial", default="", help="adb serial: open the link on that phone")
    pr.add_argument("--public-host", default=DEFAULT_PUBLIC_HOST)
    pr.add_argument(
        "--offline",
        action="store_true",
        help="sovereign relay (--keys-file): no identity code; add the key the app shows "
        "to the keys file",
    )
    for verb, h in (("allow", "Let a workspace device lend"), ("deny", "Stop a device lending")):
        p = ws.add_parser(verb, help=h)
        p.add_argument("device_id")
        if verb == "allow":
            p.add_argument("--max-mb", type=int, default=0, help="cap what it may lend")
    stp = ws.add_parser("status", help="The workspace swarm: devices, grants, pending")
    stp.add_argument("--json", action="store_true")
    ws.add_parser("install", help="Start the relay at logon (Windows task / systemd user unit)")


def run(args) -> int:
    act = getattr(args, "workspace_action", None)
    if act == "serve":
        return run_serve(args)
    if act == "pair":
        return run_pair(args)
    if act in ("allow", "deny"):
        did = args.device_id
        if not _DEVICE_ID.match(did):
            print("kvholder: not a device id", file=sys.stderr)
            return 2
        mb = getattr(args, "max_mb", 0) or None
        Grants().set(did, act == "allow", max_mb=mb)
        print(
            f"kvholder: {did} {'may lend' if act == 'allow' else 'may NOT lend'}"
            + (f" (up to {mb} MB)" if mb else "")
        )
        return 0
    if act == "status":
        st = read_status()
        if st is None:
            print(
                "kvholder: the workspace relay is not running (adk kvholder workspace serve)",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps(st, indent=1))
            return 0
        sw = st["swarm"]
        print(f"relay    : {st['relay']}{'  (STALE: not updating)' if st['stale'] else ''}")
        print(f"identity : {'ok' if st['identity']['ok'] else st['identity']['error']}")
        print(
            f"swarm    : {len(sw['holders'])} holder(s), lent {(sw['lent_bytes'] or 0) >> 20} MB,"
            f" used {(sw['used_bytes'] or 0) >> 20} MB, {sw['attn_calls']} attention calls"
        )
        for h in sw["holders"]:
            print(
                f"  {h.get('device_id') or '-':<20} {h['device'][:40]:<40} held={h['held']}"
                f" last={h['last_ms']}ms seen={h['seen_s']}s"
            )
        for did, g in sorted(st["grants"].items()):
            print(
                f"  grant {did:<20} {'lend' if g['lend'] else 'PENDING/denied'}"
                + (f" max {g['max_mb']} MB" if g.get("max_mb") else "")
            )
        return 0
    if act == "install":
        return run_install()
    print("usage: adk kvholder workspace {serve,pair,allow,deny,status,install}", file=sys.stderr)
    return 2


def run_serve(args) -> int:
    from adk import kvholder_net as net

    pub_ip = args.public_bind or overlay_ip()
    if not pub_ip:
        print(
            "kvholder: no mesh address (100.64/10) to publish on; pass --public-bind",
            file=sys.stderr,
        )
        return 2
    master = secrets.token_urlsafe(18)  # loopback tools only (/join, /swarm); never public
    try:
        relay, servers = net.start_relay(
            master,
            ("127.0.0.1", args.port),
            ("127.0.0.1", args.web_port),
            public_addr=(pub_ip, args.web_port),
        )
    except OSError as e:
        print(f"kvholder: cannot listen ({e}); is another relay running?", file=sys.stderr)
        return 1
    grants = Grants()
    if args.keys_file:
        keys = SealKeys(fetch=_file_keys(args.keys_file))
    else:
        keys = SealKeys(fetch=lambda: fetch_seal_keys(lambda: grants.allowed_ids()))
    keys.refresh()
    household = Household()
    household.refresh()
    from adk.kvholder_mesh import relay_ids

    # the public host, and every address a tailnet/LAN holder may dial the listener by directly
    gate = DeviceGate(relay_ids() | {args.public_host, pub_ip}, keys, grants, household)
    relay.device_gate = gate
    public_url = f"wss://{args.public_host}/holder"
    net.write_state(
        {
            "engine_port": args.port,
            "web_port": args.web_port,
            "token": master,
            "public": f"https://{args.public_host}",
            "lan": "",
            "pid": os.getpid(),
        }
    )
    print(f"kvholder: engine port 127.0.0.1:{args.port} (point LLAMA_KV_REMOTE here)")
    print(f"kvholder: holders dial {public_url} (public listener {pub_ip}:{args.web_port})")
    print(
        f"kvholder: identity {'ok' if keys.at else keys.error}; "
        f"{sum(1 for g in grants.devices.values() if g.get('lend'))} device(s) allowed"
    )
    last_keys = time.time()
    try:
        while True:
            time.sleep(SWEEP_S)
            if time.time() - last_keys >= KEYS_REFRESH_S:
                keys.refresh()
                household.refresh()
                last_keys = time.time()
            sweep(relay, gate)
            try:
                _write_private(workspace_status_path(), snapshot(relay, gate, public_url))
            except OSError as e:
                print(f"kvholder: status not written: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        print("kvholder: workspace relay stopped")
    finally:
        net.stop_relay(relay, servers)
        net.state_path().unlink(missing_ok=True)
    return 0


def run_pair(args) -> int:
    did = args.name if args.name.startswith("kvh-") else f"kvh-{args.name}"
    if not _DEVICE_ID.match(did):
        print("kvholder: --name: letters, digits, . _ - only", file=sys.stderr)
        return 2
    from adk import devices

    relay_url = f"wss://{args.public_host}/holder"
    if args.offline:
        link = pair_link(relay_url, "", "", did, args.max_mb)
    else:
        try:
            status, code, body = mint_pair_code()
        except devices.DevicesError as e:
            print(f"kvholder: {e}", file=sys.stderr)
            return 1
        if status != 200 or not code:
            print(
                f"kvholder: identity refused the pairing code ({status}): {body[:300]}",
                file=sys.stderr,
            )
            return 1
        link = pair_link(relay_url, devices.enroll_base(), code, did, args.max_mb)
    Grants().set(did, True, max_mb=args.max_mb, name=args.name)
    print(
        f"kvholder: {did} may lend up to {args.max_mb} MB."
        + ("" if args.offline else " The code works once, for 5 minutes.")
    )
    if args.serial:
        from adk import kvholder_net as net

        adb = net.adb_path()
        if not adb:
            print("kvholder: adb not found; open the link on the phone yourself", file=sys.stderr)
        else:
            r = subprocess.run(
                [
                    adb,
                    "-s",
                    args.serial,
                    "shell",
                    "am",
                    "start",
                    "-a",
                    "android.intent.action.VIEW",
                    "-d",
                    f"'{link}'",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            print(
                "kvholder: opened the pairing link in the app"
                if r.returncode == 0
                else f"kvholder: adb: {r.stderr.strip()[:200]}"
            )
            return 0 if r.returncode == 0 else 1
    from adk import kvholder_net as net

    net._show_url("open on the phone (camera)", link)
    return 0


def run_install() -> int:
    exe = Path(sys.executable)
    if os.name == "nt":
        pyw = exe.with_name("pythonw.exe")
        cmd = f'"{pyw if pyw.exists() else exe}" -m adk kvholder workspace serve'
        r = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/TN",
                "AitherKVRelay",
                "/TR",
                cmd,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode == 0:
            print(r.stdout.strip())
            subprocess.run(["schtasks", "/Run", "/TN", "AitherKVRelay"], capture_output=True)
            return 0
        # an ONLOGON task needs an elevated shell; the per-user Run key does not
        print(f"kvholder: logon task refused ({r.stderr.strip()}); using the per-user Run key")
        r = subprocess.run(
            [
                "reg",
                "add",
                r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                "/v",
                "AitherKVRelay",
                "/t",
                "REG_SZ",
                "/d",
                cmd,
                "/f",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode != 0:
            print(f"kvholder: {r.stderr.strip()}", file=sys.stderr)
            return r.returncode
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        argv = [str(pyw if pyw.exists() else exe), "-m", "adk", "kvholder", "workspace", "serve"]
        subprocess.Popen(argv, creationflags=flags, close_fds=True)
        print("kvholder: starts at logon (HKCU Run: AitherKVRelay); started now")
        return 0
    unit = (
        "[Unit]\nDescription=Aither KV holder workspace relay\nAfter=network-online.target\n\n"
        f"[Service]\nExecStart={exe} -m adk kvholder workspace serve\nRestart=always\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    p = Path.home() / ".config" / "systemd" / "user" / "aither-kvrelay.service"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(unit, encoding="utf-8")
    r = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "aither-kvrelay.service"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(f"kvholder: wrote {p}; systemctl: {r.returncode}")
    return r.returncode
