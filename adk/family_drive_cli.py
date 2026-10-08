"""`adk storage drive ...` -- the family drive on this computer.

Files go into the family's mesh storage pool sealed with the family key, which is made
and kept on family devices only (:mod:`adk.family_vault`). Genesis and the phones that
keep copies see ciphertext; file names live inside a sealed index.

    adk storage drive key init        make the family key here (first family computer)
    adk storage drive key register    publish this computer's public key to the family
    adk storage drive key share       wrap the key to every registered family computer
    adk storage drive key accept      pick up the key another family computer wrapped for this one
    adk storage drive key status

Configuration (environment): ``AITHER_FAMILY_API`` (default
``$AITHER_PORTAL_URL/api/tutor``, portal default ``https://api.aitherium.com``) and the
signed-in token from ``adk login``.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import sys
from typing import Any, Dict, List, Optional, Tuple

from adk import family_vault as fv

FAMILY = "family"  # one family per account on this computer; key_id tells keys apart


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status} {detail}")
        self.status = status
        self.detail = detail


def api_base() -> str:
    explicit = (os.environ.get("AITHER_FAMILY_API") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    portal = (os.environ.get("AITHER_PORTAL_URL") or "https://api.aitherium.com").strip()
    return portal.rstrip("/") + "/api/tutor"


def _token() -> str:
    tok = (os.environ.get("AITHER_API_KEY") or "").strip()
    if tok:
        return tok
    try:
        from adk.config import load_saved_config
        cfg = load_saved_config() or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    return str(cfg.get("access_token") or cfg.get("api_key") or "")


def request(method: str, path: str, *, json_body: Any = None, content: Optional[bytes] = None,
            params: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    """One call to the household API. Returns (status, json-or-bytes). Raises ApiError >= 400."""
    import httpx

    headers = {"Authorization": f"Bearer {_token()}"} if _token() else {}
    if content is not None:
        headers["Content-Type"] = "application/octet-stream"
    with httpx.Client(timeout=60.0) as c:
        r = c.request(method, api_base() + path, json=json_body, content=content,
                      params=params, headers=headers)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise ApiError(r.status_code, str(detail)[:200])
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        return r.status_code, r.json()
    return r.status_code, r.content


def family_key() -> bytes:
    key = fv.load_family_key(FAMILY)
    if key is None:
        raise fv.VaultError("this computer has no family key: run `adk storage drive key init` "
                            "(first family computer) or `adk storage drive key accept`")
    return key


# -- key commands --------------------------------------------------------------------

def key_init(_args: argparse.Namespace) -> int:
    key = fv.load_family_key(FAMILY)
    if key is None:
        key = fv.new_family_key()
        fv.save_family_key(FAMILY, key)
        print(f"Made the family key here (id {fv.key_id(key).hex()}). It never leaves family "
              "devices: share it with `adk storage drive key share`.")
    else:
        print(f"This computer already holds the family key (id {fv.key_id(key).hex()}).")
    return key_register(_args)


def key_register(args: argparse.Namespace) -> int:
    label = getattr(args, "label", "") or platform.node()
    pub = fv.public_key_hex()
    request("POST", "/family/storage/keys/devices", json_body={"pubkey": pub, "label": label})
    print(f"Registered this computer ({label}) for the family key.")
    return 0


def key_share(_args: argparse.Namespace) -> int:
    key = family_key()
    kid = fv.key_id(key).hex()
    _s, body = request("GET", "/family/storage/keys/devices")
    shared = 0
    for d in body.get("devices", []):
        if kid in d.get("has_key", []):
            continue
        blob = fv.wrap_key(key, bytes.fromhex(d["pubkey"]))
        request("POST", "/family/storage/keys/wraps", json_body={
            "to_pubkey": d["pubkey"], "key_id": kid,
            "blob_b64": base64.b64encode(blob).decode()})
        shared += 1
        print(f"  wrapped for {d.get('label') or d['pubkey'][:12]}")
    print(f"Shared the family key with {shared} computer(s); "
          "each runs `adk storage drive key accept`.")
    return 0


def key_accept(_args: argparse.Namespace) -> int:
    pub = fv.public_key_hex()
    _s, body = request("GET", "/family/storage/keys/wraps", params={"pubkey": pub})
    wraps: List[Dict[str, Any]] = body.get("wraps", [])
    if not wraps:
        print("No family key is waiting for this computer yet. Ask a family computer that "
              "holds it to run `adk storage drive key share`.", file=sys.stderr)
        return 2
    newest = wraps[-1]
    key = fv.unwrap_key(base64.b64decode(newest["blob_b64"]))
    if fv.key_id(key).hex() != newest["key_id"]:
        raise fv.VaultError("the wrapped key does not match its id")
    fv.save_family_key(FAMILY, key)
    print(f"This computer now holds the family key (id {newest['key_id']}).")
    return 0


def key_status(args: argparse.Namespace) -> int:
    key = fv.load_family_key(FAMILY)
    out = {"key_id": fv.key_id(key).hex() if key else "", "device_pubkey": fv.public_key_hex()}
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
    else:
        print(f"family key: {out['key_id'] or 'none on this computer'}")
        print(f"this computer's public key: {out['device_pubkey']}")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adk storage drive",
                                description="Your family's drive on the family's own devices")
    sub = p.add_subparsers(dest="verb")
    key = sub.add_parser("key", help="The family key (made and kept on family devices)")
    ks = key.add_subparsers(dest="key_verb")
    init = ks.add_parser("init", help="Make the family key here (first family computer)")
    init.add_argument("--label", default="")
    reg = ks.add_parser("register", help="Publish this computer's public key to the family")
    reg.add_argument("--label", default="")
    ks.add_parser("share", help="Wrap the key to every registered family computer")
    ks.add_parser("accept", help="Pick up the key wrapped for this computer")
    st = ks.add_parser("status", help="Which family key this computer holds")
    st.add_argument("--json", action="store_true")
    _extend(sub)
    return p


def _extend(sub: Any) -> None:
    """Hook for the file verbs (put / get / ls / rm)."""
    try:
        from adk.family_drive_files import add_parsers
    except ImportError:
        return
    add_parsers(sub)


_KEY = {"init": key_init, "register": key_register, "share": key_share,
        "accept": key_accept, "status": key_status}


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        if args.verb == "key" and args.key_verb in _KEY:
            return _KEY[args.key_verb](args)
        handler = getattr(args, "handler", None)
        if handler is not None:
            return int(handler(args))
    except fv.VaultError as exc:
        print(f"adk storage drive: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"adk storage drive: the household answered {exc.status} ({exc.detail})",
              file=sys.stderr)
        return 2
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
