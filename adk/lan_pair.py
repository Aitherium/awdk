"""LAN pairing mode -- ``adk pair-mode``: let a member on the same Wi-Fi find this machine.

Phase 2 of device join (docs: ``AitherOS/docs/devices/LAN_PAIR_DISCOVERY.md``). Phase 1
(branch ``feat/device-join-same-account-qr``) is the join request itself: an unpaired
device asks Identity for a short-lived request (``rid``, a 6-digit comparison number and
an 8-character join code) bound to its awseal key; a signed-in member types the code and
checks the number; the device collects a pairing code with a signature by that key and
finishes through ``/v1/nodes/pairing/confirm`` exactly like ``adk pair``. This module
only adds the "nearby" part:

* While the person has opened pairing mode (at most :data:`MAX_WINDOW_S`, never always on)
  this machine advertises ``_aither-pair._tcp`` with TXT ``{v, rid, class}``. Members keep
  advertising ``_aither._tcp``; nothing here touches that.
* The number, the join code, the claim secret and the pairing code are NEVER advertised,
  logged or put on an argv: :func:`build_txt` refuses a TXT carrying a secret-shaped key,
  and the number and the join code are printed only on this machine's own terminal.
* A browser (Desk, the Android app) treats every advert as UNTRUSTED: :func:`parse_txt`
  believes nothing beyond the three keys and drops any advert that carries a secret-shaped
  key; :class:`CandidateBook` rate-limits, caps per source, expires and drops a rid seen
  from two hosts. Joining still needs the SAS and a server-side approval.

The Phase-1 HTTP calls sit behind :class:`JoinRequests` (the seam). Until Phase 1 is
merged Identity answers 404 and pairing mode stops with a clear message -- it never
advertises a rid Identity did not issue.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Protocol

log = logging.getLogger("adk.lan_pair")

# -- the advert contract (keep in step with awdesk electron/lan-pair.cjs and the
#    Android NearbyBook.java; each has the same tests) ---------------------------------

#: Unpaired devices in pairing mode. Members advertise ``_aither._tcp``.
SERVICE_TYPE = "_aither-pair._tcp"
MEMBER_SERVICE_TYPE = "_aither._tcp"
ADVERT_VERSION = "1"
#: What an advert may say it is. Display only: Identity normalizes the class at confirm.
#: Identity's JOINABLE_CLASSES (services/security/identity_device_join.py): rented GPU and
#: burst classes (spark, sovereign) never join by discovery.
DEVICE_CLASSES = frozenset({"phone", "watch", "laptop", "desktop", "deck"})
#: A request id Identity issued (identity_device_join._RID_RE: secrets.token_hex(16)).
RID_RE = re.compile(r"^[0-9a-f]{32}$")
#: Keys that must never appear in an advert. A browser drops the WHOLE advert.
FORBIDDEN_KEYS = frozenset({
    "sas", "code", "pin", "otp", "token", "poll", "poll_token", "secret", "key",
    "password", "pass", "pw", "auth", "bearer", "nonce", "claim", "claim_secret",
    "join", "join_code", "sig", "signature",
})
#: Pairing mode is time-boxed: never longer than this, whatever Identity says.
MAX_WINDOW_S = 300
MAX_TXT_KEYS = 8
MAX_TXT_VALUE = 64
MAX_TXT_BYTES = 400
MAX_LABEL = 40
SAS_RE = re.compile(r"^[0-9]{6}$")


def _s(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return "" if v is None else str(v)


def build_txt(rid: str, device_class: str) -> Dict[str, str]:
    """The TXT this device advertises. Raises ``ValueError`` on anything off-contract."""
    rid = _s(rid).strip()
    cls = _s(device_class).strip().lower()
    if not RID_RE.match(rid):
        raise ValueError("rid is not an Identity request id")
    if cls not in DEVICE_CLASSES:
        raise ValueError(f"unknown device class {cls!r}")
    txt = {"v": ADVERT_VERSION, "rid": rid, "class": cls}
    _check_no_secrets(txt)
    return txt


def _check_no_secrets(txt: Mapping[str, Any]) -> None:
    bad = sorted(k for k in txt if _s(k).strip().lower() in FORBIDDEN_KEYS)
    if bad:
        raise ValueError(f"advert must not carry {bad}")


def parse_txt(txt: Mapping[Any, Any]) -> Optional[Dict[str, str]]:
    """An advert's TXT -> ``{"v", "rid", "class"}`` or ``None`` (dropped).

    Untrusted input: keys are case-folded, bytes decoded leniently, and the advert is
    dropped when it is oversized, has a duplicate key after folding, carries a
    secret-shaped key, or any of the three values is off-contract. Other keys are
    ignored, never surfaced.
    """
    if not isinstance(txt, Mapping) or len(txt) > MAX_TXT_KEYS:
        return None
    seen: Dict[str, str] = {}
    total = 0
    for k, v in txt.items():
        key = _s(k).strip().lower()
        val = _s(v).strip()
        total += len(key) + len(val) + 2
        if not key or key in seen or len(val) > MAX_TXT_VALUE or total > MAX_TXT_BYTES:
            return None
        if key in FORBIDDEN_KEYS:
            return None
        seen[key] = val
    if seen.get("v") != ADVERT_VERSION:
        return None
    rid, cls = seen.get("rid", ""), seen.get("class", "").lower()
    if not RID_RE.match(rid) or cls not in DEVICE_CLASSES:
        return None
    return {"v": ADVERT_VERSION, "rid": rid, "class": cls}


def clean_label(name: Any) -> str:
    """An advert's instance name for display: printable, one line, short. Unverified."""
    out = []
    for ch in _s(name):
        cat = unicodedata.category(ch)
        if cat[0] == "C" or cat in ("Zl", "Zp"):
            continue  # control, format (bidi overrides), surrogates, line separators
        out.append(ch)
    text = " ".join("".join(out).split())
    return text[:MAX_LABEL]


# -- the browser side: a flood-proof book of candidates --------------------------------


@dataclass
class Candidate:
    rid: str
    device_class: str
    label: str
    source: str
    first_seen: float
    last_seen: float

    def public(self) -> Dict[str, Any]:
        return {"rid": self.rid, "class": self.device_class, "label": self.label,
                "verified": False}


@dataclass
class CandidateBook:
    """What a member's device shows under "Nearby devices".

    Every outcome of :meth:`offer` is a word, so a caller can count drops without
    logging advert content: ``added`` ``refreshed`` ``rejected`` ``flood`` ``full``
    ``source-cap`` ``conflict`` ``banned``.
    """

    clock: Callable[[], float] = time.monotonic
    ttl_s: float = MAX_WINDOW_S
    max_candidates: int = 16
    max_per_source: int = 2
    burst: float = 40.0
    refill_per_s: float = 4.0
    _cands: Dict[str, Candidate] = field(default_factory=dict)
    _banned: Dict[str, float] = field(default_factory=dict)
    _tokens: float = -1.0
    _last_refill: float = 0.0
    dropped: int = 0

    def _take_token(self, now: float) -> bool:
        if self._tokens < 0:
            self._tokens, self._last_refill = self.burst, now
        self._tokens = min(self.burst,
                           self._tokens + (now - self._last_refill) * self.refill_per_s)
        self._last_refill = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def _expire(self, now: float) -> None:
        for rid in [r for r, c in self._cands.items() if now - c.first_seen > self.ttl_s]:
            # a rid lives one window: re-announcing it after that never brings it back
            del self._cands[rid]
            self._banned[rid] = now + self.ttl_s
        for rid in [r for r, t in self._banned.items() if now > t]:
            del self._banned[rid]

    def offer(self, txt: Mapping[Any, Any], *, label: Any = "", source: Any = "") -> str:
        now = self.clock()
        if not self._take_token(now):
            self.dropped += 1
            return "flood"
        self._expire(now)
        adv = parse_txt(txt)
        if adv is None:
            self.dropped += 1
            return "rejected"
        rid, src = adv["rid"], _s(source).strip()[:64]
        if rid in self._banned:
            return "banned"
        have = self._cands.get(rid)
        if have is not None:
            if have.source != src or have.device_class != adv["class"]:
                # one rid, two hosts (or a class flip): someone is replaying it. Drop both.
                del self._cands[rid]
                self._banned[rid] = now + self.ttl_s
                return "conflict"
            have.last_seen = now  # first_seen is kept: a refresh never extends the window
            return "refreshed"
        if sum(1 for c in self._cands.values() if c.source == src) >= self.max_per_source:
            self.dropped += 1
            return "source-cap"
        if len(self._cands) >= self.max_candidates:
            self.dropped += 1
            return "full"
        self._cands[rid] = Candidate(rid, adv["class"], clean_label(label), src, now, now)
        return "added"

    def remove(self, rid: str) -> None:
        self._cands.pop(rid, None)

    def list(self) -> List[Dict[str, Any]]:
        self._expire(self.clock())
        return [c.public() for c in sorted(self._cands.values(), key=lambda c: c.first_seen)]


# -- the Phase-1 seam -----------------------------------------------------------------
#
# Phase 1 (feat/device-join-same-account-qr, services/security/identity_device_join.py):
#   POST /v1/nodes/join/open  {device_class, pubkey, label}
#       -> {rid, nonce, sas, claim_secret, join_code, expires_in, state}
#   POST /v1/nodes/join/requests/{rid}/claim  {claim_secret, signature}
#       -> {state} | {state: "approved", code, node_class, role}
#   sas = sha256("aither-join-v1|sas|{rid}|{pubkey}|{nonce}")[:8] mod 10**6, recomputed
#   here: a server or proxy that swapped the key yields a different number, and this
#   machine refuses to show it. The signature is Ed25519 over "aither-join-v1|{rid}|{nonce}"
#   by the awseal device key, the same key `adk pair` presents as ``seal_pubkey`` at
#   /v1/nodes/pairing/confirm (the code Identity mints is pinned to it).
# Paths and the number's formula live HERE only.

JOIN_OPEN_PATH = "/v1/nodes/join/open"
JOIN_CLAIM_PATH = "/v1/nodes/join/requests/{rid}/claim"
SIGN_PREFIX = "aither-join-v1"
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_PUBKEY = re.compile(r"^[0-9a-f]{64}$")
_JOIN_CODE = re.compile(r"^[A-Z0-9]{8}$")
_PAIR_CODE = re.compile(r"^[A-Z0-9-]{6,16}$")


class SeamUnavailableError(RuntimeError):
    """Identity has no join-request API yet (Phase 1 not deployed)."""


class KeyUnavailableError(RuntimeError):
    """No awseal device key: Identity pins the join to a key this machine must hold."""


def compute_sas(rid: str, pubkey: str, nonce: str) -> str:
    """Identity's comparison number, recomputed from this machine's own key."""
    raw = hashlib.sha256(f"{SIGN_PREFIX}|sas|{rid}|{pubkey}|{nonce}".encode()).digest()
    return str(int.from_bytes(raw[:8], "big") % 1_000_000).zfill(6)


class JoinKey(Protocol):
    pubkey: str

    def sign(self, message: bytes) -> bytes: ...


@dataclass
class SealKey:
    """The awseal device key (``~/.aither/awseal/signing.key`` or ``$AWSEAL_KEY_PATH``)."""

    pubkey: str
    private: Any = field(repr=False)

    def sign(self, message: bytes) -> bytes:
        return self.private.sign(message)


def load_join_key() -> SealKey:
    """This machine's awseal key, created once. Raises :class:`KeyUnavailableError`.

    It must be the key ``adk pair`` sends as ``seal_pubkey`` (adk.device_identity), or
    the confirm is refused; both resolve the path the same way, and this checks it.
    """
    try:
        from awseal import keys
    except ImportError as exc:
        raise KeyUnavailableError(
            f"awseal is not installed ({exc}): pip install 'awdk[seal]'") from exc
    from adk.device_identity import seal_public_key

    pub = seal_public_key(create=True)
    env = (os.getenv(keys.KEY_PATH_ENV) or "").strip()
    path = Path(env) if env else Path(keys.DEFAULT_KEY_PATH)
    try:
        priv = keys.load_private_key(path)
        own = keys.public_key_hex(private_key=priv)
    except Exception as exc:  # noqa: BLE001 -- an unreadable key: no join, said plainly
        raise KeyUnavailableError(
            f"the device key could not be read: {type(exc).__name__}") from exc
    if not pub or not _PUBKEY.match(own) or own != pub:
        raise KeyUnavailableError("the device key is not the one pairing presents")
    return SealKey(own, priv)


@dataclass
class JoinRequest:
    rid: str
    nonce: str
    sas: str = field(repr=False)
    claim_secret: str = field(repr=False)
    join_code: str = field(repr=False)
    expires_in: int = 0


class JoinRequests(Protocol):
    def create(self, device_class: str, label: str) -> JoinRequest: ...

    def poll(self, req: JoinRequest) -> Dict[str, Any]:
        """``{"state": "pending"|"approved"|"denied"|"expired", "code"?: str}``."""
        ...


def validate_join_request(data: Mapping[str, Any], pubkey: str) -> JoinRequest:
    """Identity's answer to the open request, checked before anything is advertised.

    The number is RECOMPUTED from this machine's key: a mismatch means the key the server
    holds is not ours (a swapped key), and nothing is shown or advertised.
    """
    rid = _s(data.get("rid")).strip()
    nonce = _s(data.get("nonce")).strip()
    sas = _s(data.get("sas")).strip()
    secret = _s(data.get("claim_secret")).strip()
    code = _s(data.get("join_code")).replace("-", "").strip().upper()
    try:
        exp = int(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        exp = 0
    if (not RID_RE.match(rid) or not _HEX32.match(nonce) or not SAS_RE.match(sas)
            or not 20 <= len(secret) <= 128 or not _JOIN_CODE.match(code) or exp <= 0):
        raise ValueError("Identity's join request is malformed")
    if not hmac.compare_digest(sas, compute_sas(rid, pubkey, nonce)):
        raise ValueError("the number does not match this device's key -- not advertising")
    return JoinRequest(rid, nonce, sas, secret, code, min(exp, MAX_WINDOW_S))


class IdentityJoinRequests:
    """HTTP implementation of :class:`JoinRequests` against Identity (no account yet)."""

    def __init__(self, base: str, key: JoinKey, *, timeout: float = 15.0) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout

    def create(self, device_class: str, label: str) -> JoinRequest:
        import httpx

        try:
            r = httpx.post(f"{self.base}{JOIN_OPEN_PATH}", timeout=self.timeout,
                           json={"device_class": device_class, "pubkey": self.key.pubkey,
                                 "label": clean_label(label)})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Identity is unreachable ({type(exc).__name__})") from exc
        if r.status_code in (404, 405):
            raise SeamUnavailableError("this Identity has no device join requests yet")
        if r.status_code == 429:
            raise RuntimeError("Identity is limiting join requests -- wait a few minutes")
        if r.status_code != 200:
            raise RuntimeError(f"Identity refused the join request (HTTP {r.status_code})")
        try:
            data = r.json()
        except ValueError as exc:
            raise ValueError("Identity's join request is malformed") from exc
        if not isinstance(data, dict):
            raise ValueError("Identity's join request is malformed")
        return validate_join_request(data, self.key.pubkey)

    def poll(self, req: JoinRequest) -> Dict[str, Any]:
        import httpx

        sig = self.key.sign(f"{SIGN_PREFIX}|{req.rid}|{req.nonce}".encode()).hex()
        try:
            r = httpx.post(f"{self.base}{JOIN_CLAIM_PATH.format(rid=req.rid)}",
                           timeout=self.timeout,
                           json={"claim_secret": req.claim_secret, "signature": sig})
        except httpx.HTTPError:
            return {"state": "pending"}  # a Wi-Fi blip: the window still closes on time
        return claim_state(r.status_code, r)


def claim_state(status: int, resp: Any) -> Dict[str, Any]:
    """Identity's claim answer -> ``{"state", "code"?}``; anything odd fails closed."""
    if status in (403, 404):
        return {"state": "expired"}  # gone, or the key / secret refused: stop advertising
    if status != 200:
        return {"state": "pending"}  # 429 / 5xx: try again until the window closes
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    state = _s(data.get("state")).lower()
    if state == "claimed":
        state = "expired"  # already collected: never a second code
    if state not in ("pending", "approved", "denied", "expired"):
        state = "pending"
    out: Dict[str, Any] = {"state": state}
    code = _s(data.get("code")).strip().upper()
    if state == "approved" and _PAIR_CODE.match(code):
        out["code"] = code
    return out


# -- advertisers ----------------------------------------------------------------------

#: The image ships the same XML (AitherDesktop/atomic/config/avahi/aitheros-pair.service.in)
#: at this path; never in /etc/avahi/services, so it is never advertised by itself.
SERVICE_TEMPLATE_PATH = Path("/usr/share/aitheros/avahi/aitheros-pair.service.in")
SERVICE_TEMPLATE = """<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!--
  AitherOS pairing mode: rendered by `adk pair-mode` for at most 5 minutes, then deleted.
  NEVER install this template in /etc/avahi/services: an unpaired device advertises only
  while its owner has opened "add device". The TXT never carries the SAS or any secret.
-->
<service-group>
  <name replace-wildcards="yes">Aither device on %h</name>
  <service>
    <type>_aither-pair._tcp</type>
    <port>9</port>
    <txt-record>v=@V@</txt-record>
    <txt-record>rid=@RID@</txt-record>
    <txt-record>class=@CLASS@</txt-record>
  </service>
</service-group>
"""


def render_service(txt: Mapping[str, str], template: Optional[str] = None) -> str:
    """The avahi service file for this advert (TXT re-validated first)."""
    adv = parse_txt(txt)
    if adv is None:
        raise ValueError("refusing to render an off-contract advert")
    tpl = template if template is not None else SERVICE_TEMPLATE
    return (tpl.replace("@V@", adv["v"]).replace("@RID@", adv["rid"])
            .replace("@CLASS@", adv["class"]))


class Advertiser(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


def _die_with_parent() -> None:
    """In the avahi child before exec: SIGTERM it when the parent dies (PR_SET_PDEATHSIG)."""
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)
    except Exception as exc:  # noqa: BLE001 -- best effort; SIGTERM/SIGHUP are handled in-process
        log.debug("PR_SET_PDEATHSIG unavailable (%s); relying on in-process signal handlers", exc)


@contextlib.contextmanager
def _signals_end_pair_mode() -> Iterator[None]:
    """SIGTERM / SIGHUP end pairing mode through ``finally`` (Python's default for SIGTERM
    exits without it, leaving the advert up). Main thread only; handlers are restored."""
    sigs = [s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None))
            if s is not None]
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _end(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + int(signum))

    old = {}
    for s in sigs:
        try:
            old[s] = signal.signal(s, _end)
        except (OSError, ValueError) as exc:
            log.debug('ignoring an unreadable entry (%s)', exc)
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


class AvahiPublish:
    """``avahi-publish-service`` as a child: no root, and the advert dies with it."""

    def __init__(self, txt: Mapping[str, str], name: str = "", port: int = 9) -> None:
        if parse_txt(txt) is None:
            raise ValueError("refusing to advertise an off-contract advert")
        self.txt = dict(txt)
        self.name = clean_label(name) or "Aither device"
        self.port = port
        self.proc: Optional[subprocess.Popen] = None

    def argv(self) -> List[str]:
        return ["avahi-publish-service", self.name, SERVICE_TYPE, str(self.port),
                *[f"{k}={v}" for k, v in self.txt.items()]]

    def start(self) -> None:
        # a SIGKILLed parent runs no ``finally``: the kernel ends the child with it (Linux)
        self.proc = subprocess.Popen(self.argv(), stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     preexec_fn=_die_with_parent if sys.platform.startswith(
                                         "linux") else None)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class AvahiServiceFile:
    """Root fallback: write the service file, delete it on stop (and on any exit path)."""

    def __init__(self, txt: Mapping[str, str],
                 services_dir: Path = Path("/etc/avahi/services")) -> None:
        tpl = None
        if SERVICE_TEMPLATE_PATH.is_file():
            tpl = SERVICE_TEMPLATE_PATH.read_text(encoding="utf-8")
        self.body = render_service(txt, tpl)
        self.path = services_dir / "aitheros-pair.service"

    def start(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)

    def stop(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError as exc:
            log.debug('nothing to remove (%s)', exc)


def default_advertiser(txt: Mapping[str, str], name: str) -> Advertiser:
    if shutil.which("avahi-publish-service"):
        return AvahiPublish(txt, name)
    svc = Path("/etc/avahi/services")
    if svc.is_dir() and os.access(svc, os.W_OK):
        return AvahiServiceFile(txt, svc)
    raise RuntimeError("no mDNS advertiser here (install avahi-utils) -- use the QR instead")


# -- pairing mode ---------------------------------------------------------------------


def run_pair_mode(seam: JoinRequests, *, device_class: str, label: str, window_s: int,
                  advertiser_factory: Callable[[Mapping[str, str], str], Advertiser],
                  complete: Callable[[str], Dict[str, Any]],
                  say: Callable[[str], None] = print,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep,
                  poll_every: float = 2.0) -> int:
    """Advertise until approved, denied or the window closes. Returns an exit code.

    ``complete(code)`` finishes the join (``adk pair``'s confirm); the code is never
    printed. The advert is stopped before ``complete`` runs and on every exit path.
    """
    if device_class not in DEVICE_CLASSES:
        say(f"  x unknown device class {device_class!r}")
        return 2
    try:
        req = seam.create(device_class, label)
    except SeamUnavailableError as e:
        say(f"  x {e}: pair with a QR or a code instead (adk pair)")
        return 3
    except KeyUnavailableError as e:
        say(f"  x {e}")
        return 4
    except (ValueError, RuntimeError) as e:
        say(f"  x {e}")
        return 1
    window = max(1, min(int(window_s), MAX_WINDOW_S, req.expires_in))
    adv = advertiser_factory(build_txt(req.rid, device_class), label)
    deadline = clock() + window
    with _signals_end_pair_mode():
        return _advertise(adv, req, seam, window, deadline, complete, say, clock, sleep,
                          poll_every)


def _advertise(adv: Advertiser, req: JoinRequest, seam: JoinRequests, window: int,
               deadline: float, complete: Callable[[str], Dict[str, Any]],
               say: Callable[[str], None], clock: Callable[[], float],
               sleep: Callable[[float], None], poll_every: float) -> int:
    adv.start()
    try:
        say(f"  Pairing mode on for {window // 60}m{window % 60:02d}s. On a device that is "
            "already yours, open Add device -> Nearby and pick this one.")
        say(f"  On that device, type the code {req.join_code[:4]}-{req.join_code[4:]} "
            f"and check the number {req.sas[:3]} {req.sas[3:]}")
        while clock() < deadline:
            sleep(poll_every)
            st = seam.poll(req)
            if st.get("state") == "approved" and st.get("code"):
                adv.stop()
                res = complete(st["code"])
                if res.get("paired"):
                    say(f"  ok: joined as {res.get('node_id', '?')}")
                    return 0
                say(f"  x approved, but the join did not finish: {res.get('error', '?')}")
                return 1
            if st.get("state") in ("denied", "expired"):
                say(f"  x request {st['state']}")
                return 1
        say("  x pairing mode timed out")
        return 1
    finally:
        adv.stop()


def add_parser(sub: Any) -> None:
    p = sub.add_parser(
        "pair-mode",
        help="Let a device that is already yours find this one on the Wi-Fi (5 minutes)")
    p.add_argument("--class", dest="device_class", default="laptop",
                   choices=sorted(DEVICE_CLASSES))
    p.add_argument("--minutes", type=int, default=5, choices=range(1, 6),
                   help="How long to advertise (1-5, default 5)")
    p.add_argument("--name", default="", help="Name shown to the approver (unverified)")
    p.add_argument("--identity", default="", help="Identity base (default: as adk pair)")


def cmd_pair_mode(args: Any) -> int:
    import asyncio
    import socket

    from adk.node_pairing import IDENTITY_CONFIRM_PATH, pair_with_code

    if getattr(args, "identity", ""):
        base = args.identity.rstrip("/")
    else:
        from adk.devices import enroll_base

        base = enroll_base()
    cls = args.device_class
    label = args.name or socket.gethostname()
    try:
        key = load_join_key()
    except KeyUnavailableError as e:
        print(f"  x {e}")
        return 4

    def complete(code: str) -> Dict[str, Any]:
        # pair_with_code presents this machine's seal_pubkey (the same awseal key): the
        # code Identity minted is pinned to it.
        return asyncio.run(pair_with_code(code, base, node_class=cls,
                                          confirm_path=IDENTITY_CONFIRM_PATH))

    try:
        return run_pair_mode(IdentityJoinRequests(base, key), device_class=cls, label=label,
                             window_s=args.minutes * 60,
                             advertiser_factory=default_advertiser, complete=complete)
    except RuntimeError as e:
        print(f"  x {e}")
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    """``awnode pair-mode ...`` enters here (awnode routes before its own parser)."""
    import argparse

    ap = argparse.ArgumentParser(prog="pair-mode")
    sub = ap.add_subparsers(dest="command")
    add_parser(sub)
    args = ap.parse_args(["pair-mode", *(sys.argv[1:] if argv is None else argv)])
    return cmd_pair_mode(args)
