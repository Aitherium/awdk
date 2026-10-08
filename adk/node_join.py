"""Join this machine to the owner's devices by approval -- `adk pair --join`.

The candidate half of device join (Identity ``/v1/nodes/join``, device discovery
phase 1): discover -> prove -> approve -> join. No code is typed on THIS machine:

* signed in here (``adk login`` / ``$AITHER_NODE_TOKEN``): the request is made for that
  account (``/join/requests``); the owner's phone shows it on the lock screen and in
  This device > Devices waiting to join;
* not signed in: an OPEN request (``/join/open``); this machine prints a short code, a
  link (and a QR when the ``qrcode`` package is installed) and the six-digit number; a
  signed-in member types or scans them and approves; the machine joins THEIR mesh.

Either way this machine shows the six-digit number only after recomputing it from its
own key (a key swapped on the way gives a different number, so nothing is shown), polls
with its claim secret and an Ed25519 signature by that key, and enrols with the
single-use code it collects through the ordinary pairing confirm
(``node_pairing.pair_with_code``), which Identity pins to that same key.

The key is this device's awseal signing key (``device_identity.seal_public_key``):
the same key the registration carries as ``seal_pubkey``. Without awseal there is no
key to prove, so the command says so and stops.
"""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import time
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("adk.node_join")

SIGN_PREFIX = "aither-join-v1"
JOIN_PATH = "/v1/nodes/join"
DEFAULT_PORTAL = "https://aitherium.com"
JOIN_CLASSES = ("phone", "laptop", "desktop", "deck")
POLL_S = 3.0


def compute_sas(rid: str, pubkey: str, nonce: str) -> str:
    """Identity's comparison number, recomputed here from this machine's own key."""
    raw = hashlib.sha256(f"{SIGN_PREFIX}|sas|{rid}|{pubkey}|{nonce}".encode()).digest()
    return str(int.from_bytes(raw[:8], "big") % 1_000_000).zfill(6)


def spaced(sas: str) -> str:
    return f"{sas[:3]} {sas[3:]}" if len(sas) == 6 and sas.isdigit() else sas


def dashed(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def approve_link(portal: str, code: str, sas: str) -> str:
    """What the QR carries: the portal's This device app, code and number pre-filled."""
    from urllib.parse import urlencode
    q = urlencode({"app": "this-device", "join": code, "sas": sas})
    return f"{portal.rstrip('/')}/?{q}"


def _signer() -> Optional[Callable[[bytes], bytes]]:
    try:
        from pathlib import Path

        from awseal import keys
        env = (os.getenv(keys.KEY_PATH_ENV) or "").strip()
        priv = keys.load_private_key(Path(env) if env else None)
        return priv.sign
    except Exception as exc:  # noqa: BLE001 -- no key = cannot prove, caller stops
        log.debug("no awseal signing key: %s", exc)
        return None


def _print_qr(text: str, out: Callable[[str], None]) -> None:
    try:
        import io

        import qrcode  # type: ignore
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        out(buf.getvalue())
    except Exception as exc:  # noqa: BLE001 -- the code and link still work without it
        log.debug("no terminal QR (%s)", type(exc).__name__)


async def request_join(identity: str, *, node_class: str, bearer: str = "",
                       portal: str = DEFAULT_PORTAL, label: str = "",
                       pubkey: str = "", sign: Optional[Callable[[bytes], bytes]] = None,
                       out: Callable[[str], None] = print, timeout_s: float = 330.0,
                       client: Any = None) -> Dict[str, Any]:
    """Ask to join and wait for the owner's answer. Never raises.

    Returns ``{"approved": True, "code": ...}`` (the single-use pairing code, for
    ``pair_with_code``) or ``{"approved": False, "error": ...}``.
    """
    if node_class not in JOIN_CLASSES:
        return {"approved": False, "error": f"a {node_class} cannot join by approval"}
    if not pubkey:
        from adk.device_identity import seal_public_key
        pubkey = seal_public_key()
    sign = sign or _signer()
    if not pubkey or sign is None:
        return {"approved": False,
                "error": "this machine has no device key (pip install 'awdk[seal]')"}
    base = identity.rstrip("/") + JOIN_PATH
    body = {"device_class": node_class, "pubkey": pubkey,
            "label": (label or socket.gethostname())[:40]}
    own = client is None
    if own:
        import httpx
        client = httpx.AsyncClient(timeout=30.0)
    try:
        if bearer:
            r = await client.post(f"{base}/requests", json=body,
                                  headers={"Authorization": f"Bearer {bearer}"})
        else:
            r = await client.post(f"{base}/open", json=body)
        if r.status_code != 200:
            return {"approved": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        j = r.json()
        rid, nonce = str(j.get("rid") or ""), str(j.get("nonce") or "")
        sas, secret = str(j.get("sas") or ""), str(j.get("claim_secret") or "")
        if not rid or not secret:
            return {"approved": False, "error": "Identity sent no request id"}
        # the number must come from THIS machine's key, or it is not shown at all
        if compute_sas(rid, pubkey, nonce) != sas:
            return {"approved": False,
                    "error": "the numbers did not check out; nothing was added"}
        if bearer:
            who = "a grown-up's" if j.get("approver") == "guardian" else "your"
            out(f"  Approve on {who} phone (or This device > Devices waiting to join).")
        else:
            code = str(j.get("join_code") or "")
            link = approve_link(portal, code, sas)
            out("  On a device signed in to your account, open This device and enter:")
            out(f"    code    {dashed(code)}")
            out(f"    or scan {link}")
            _print_qr(link, out)
        out(f"  Approve only if it shows this number:  {spaced(sas)}")
        signature = sign(f"{SIGN_PREFIX}|{rid}|{nonce}".encode()).hex()
        until = time.monotonic() + min(timeout_s, float(j.get("expires_in") or 300) + 30)
        while time.monotonic() < until:
            c = await client.post(f"{base}/requests/{rid}/claim",
                                  json={"claim_secret": secret, "signature": signature})
            state = ""
            if c.status_code == 200:
                cj = c.json()
                state = str(cj.get("state") or "")
                if state == "approved" and cj.get("code"):
                    return {"approved": True, "code": str(cj["code"]),
                            "node_class": str(cj.get("node_class") or node_class),
                            "role": str(cj.get("role") or "personal")}
            if c.status_code == 404:
                return {"approved": False, "error": "the request expired; run it again"}
            if c.status_code == 403:
                return {"approved": False, "error": "Identity refused this machine's key"}
            if state in ("denied", "claimed"):
                return {"approved": False, "error": f"not approved ({state}); nothing was added"}
            await _sleep(POLL_S)
        return {"approved": False, "error": "nobody approved in time; run it again"}
    except Exception as exc:  # noqa: BLE001 -- CLI surface; the message is the product
        log.warning("join failed: %s", exc)
        return {"approved": False, "error": str(exc)}
    finally:
        if own:
            await client.aclose()


async def _sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(s)


def approved_code(args: Any, identity: str) -> str:
    """`adk pair --join`: ask, wait for the approval, return the single-use code that
    cmd_pair then presents exactly as a typed one ("" = not approved; said why)."""
    import asyncio

    from adk.devices import resolve_bearer

    node_class = getattr(args, "node_class", None) or "laptop"
    portal = os.environ.get("AITHER_PORTAL_URL", "") or DEFAULT_PORTAL
    bearer = resolve_bearer()
    print(f"  Asking to join {'your' if bearer else 'a'} device mesh via {identity} …")
    got = asyncio.run(request_join(identity, node_class=node_class, bearer=bearer,
                                   portal=portal))
    if not got.get("approved"):
        print(f"  ✗ {got.get('error', 'not approved')}")
        return ""
    print(f"  ✓ Approved ({got.get('role')}); enrolling …")
    return str(got["code"])
