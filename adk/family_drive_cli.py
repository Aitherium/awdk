"""`adk storage drive ...` -- the family drive on this computer.

Files go into the family's mesh storage pool sealed with the family key, which is made
and kept on family devices only (:mod:`adk.family_vault`). Genesis and the phones that
keep copies see ciphertext; file names live inside a sealed index.

    adk storage drive key enroll CODE  make this computer a family device (Family app code)
    adk storage drive key init        make the family key here (first family computer)
    adk storage drive key register    publish this computer's public keys to the family
    adk storage drive key approve P F guardian: approve computer P after checking fingerprint F
    adk storage drive key share       wrap the key to every APPROVED family computer
    adk storage drive key accept      pick up the key another family computer wrapped for this one
    adk storage drive key status
    adk storage drive key remove X    take a lost / sold computer off the key, then rotate
    adk storage drive key rotate      new key version for the devices still here; re-seal files
    adk storage drive key reencrypt   finish sealing the drive under the current version
    adk storage drive backup now|status|restore|drill   (also `adk family-drive backup ...`)

A computer's keys are bound to the family device it enrolled as (its credential, never a
payload field). Who holds the family key is exactly the member list of the household's
append-only, signed membership chain (``adk.family_vault``): ``key init`` on a computer
that holds the key writes record 0; ``key approve`` on a member adds a computer (after
the guardian checks its fingerprint); removing a device -- the Family app, or ``key
remove`` -- only REQUESTS removal, and ``key rotate`` on a member signs the record that
drops it and carries a new key. Every computer verifies the chain itself and refuses a
record that does not extend the head it verified.

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


DEVICE_TOKEN_HEADER = "x-aither-device-token"

#: Shown wherever the household asks for a guardian step-up.
STEP_UP_HINT = (
    "This needs a guardian's fresh sign-in with a passkey or authenticator app enrolled at "
    "least a day ago. Keep that factor on a DIFFERENT device from the computers it "
    "protects (e.g. your phone): if your only factor is on a lost or stolen laptop, nobody "
    "can step up to stop a removal, and it takes effect after 24 hours.")


def _device_token() -> str:
    """This computer's family device credential (``key enroll`` saves it)."""
    tok = (os.environ.get("AITHER_FAMILY_DEVICE_TOKEN") or "").strip()
    if tok:
        return tok
    path = fv.vault_dir() / "device_token"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def request(method: str, path: str, *, json_body: Any = None, content: Optional[bytes] = None,
            params: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
    """One call to the household API. Returns (status, json-or-bytes). Raises ApiError >= 400.

    The family device credential rides along when this computer has one: the key routes
    that change who holds the family key refuse a call without a live one."""
    import httpx

    headers = {"Authorization": f"Bearer {_token()}"} if _token() else {}
    device = _device_token()
    if device:
        headers[DEVICE_TOKEN_HEADER] = device
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

def _chain() -> Dict[str, Any]:
    _s, body = request("GET", "/family/storage/keys/chain")
    return body if isinstance(body, dict) else {"records": []}


def _devices() -> List[Dict[str, Any]]:
    _s, body = request("GET", "/family/storage/keys/devices")
    return list((body or {}).get("devices") or [])


def rotation_state() -> Dict[str, Any]:
    """The household's chain head, removals no record answered, members missing the key."""
    empty: Dict[str, Any] = {"rotation_pending": False, "pending": [], "head": None}
    try:
        _s, body = request("GET", "/family/storage/keys/rotation")
    except ApiError as exc:
        if exc.status == 404:  # a household from before the chain existed
            return empty
        raise
    return body if isinstance(body, dict) else empty


class EpochChange(fv.VaultError):
    """The household restarted its membership chain (a guardian recovery)."""


def _sync_chain(accept_new_epoch: bool = False
                ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """(the chain head as verified HERE, the household's chain answer).

    The first contact verifies record 0 (and opens every member's possession proof when
    this computer holds the family key); every later record must extend the head this
    computer kept, by the chain rule. A household that serves a different record where
    this computer already verified one (a fork, a rewrite) is refused, never resolved."""
    served = _chain()
    recs = sorted(served.get("records") or [], key=lambda r: int(r["seq"]))
    head = fv.load_head(FAMILY)
    if not recs:
        if head is not None:
            raise fv.VaultError("the household no longer shows the membership chain this "
                                "computer verified; refusing to continue")
        return None, served

    def raw(r: Dict[str, Any]) -> Tuple[bytes, bytes]:
        return base64.b64decode(r["record_b64"]), base64.b64decode(r["signature_b64"])

    start = 0
    epoch = int(json.loads(raw(recs[0])[0]).get("epoch") or 0)
    # the devices a recovery may not restart the chain from: what the guardians named,
    # and every removal still pending in the head THIS computer verified (signed records)
    excluded = set(served.get("recovery_excluded") or [])
    if head is not None and epoch != int(head.get("epoch") or 0):
        excluded |= set(head.get("pending") or [])
        if epoch < int(head.get("epoch") or 0) or not accept_new_epoch:
            raise EpochChange(
                f"EPOCH CHANGE: the household restarted its family key chain (epoch "
                f"{head.get('epoch') or 0} -> {epoch}), which only a guardian recovery does. "
                "If a guardian told you about it, run `adk storage drive key accept "
                "--accept-new-epoch`; otherwise tell a guardian now.")
        head = None  # verify the new epoch's genesis against every key version held here
    if head is None:
        rec, sig = raw(recs[0])
        head = fv.verify_genesis(rec, sig, tenant=str(served.get("tenant") or ""),
                                 workspace=str(served.get("workspace") or ""),
                                 key=fv.load_family_key(FAMILY), keys=fv.keyring(FAMILY),
                                 excluded=excluded if epoch > 0 else ())
        start = 1
    else:
        seq = int(head["seq"])
        if len(recs) <= seq or int(recs[seq]["seq"]) != seq or \
                fv.record_hash(raw(recs[seq])[0]) != head["hash"]:
            raise fv.VaultError("FORK: the household's membership chain does not contain "
                                "the record this computer verified at seq "
                                f"{seq}; refusing it (ask a guardian to investigate)")
        start = seq + 1
    watching = fv.load_head(FAMILY) is not None and start > 0
    seen = _seen()
    for r in recs[start:]:  # first sight of each record, kept across syncs
        seen.setdefault(fv.record_hash(raw(r)[0]), fv._now())
    _save_seen(seen)
    if not watching:
        fv.save_head(FAMILY, head)
    for r in recs[start:]:
        rec, sig = raw(r)
        head = fv.verify_extends(head, rec, sig,
                                 seen_at=seen[fv.record_hash(rec)] if watching else None)
        fv.save_head(FAMILY, head)  # progress up to the last record that verified
    fv.save_head(FAMILY, head)
    return head, served


def _seen_path() -> Any:
    return fv.vault_dir() / f"{FAMILY}.seen.json"


def _seen() -> Dict[str, int]:
    """{record sha256: when this computer first saw it} (the clock removals are judged by)."""
    path = _seen_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _save_seen(seen: Dict[str, int]) -> None:
    fv._write_private(_seen_path(), json.dumps(seen, sort_keys=True).encode())


def _need_member(head: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if head is None or fv.signing_public_hex() not in head["members"]:
        raise fv.VaultError("this computer is not a member of the household's family key "
                            "(a member adds it with `adk storage drive key approve`)")
    return head


def _possession_b64(tenant: str, workspace: str) -> str:
    key = fv.load_family_key(FAMILY)
    if key is None or not tenant:
        return ""
    return base64.b64encode(fv.possession_proof(key, tenant, workspace,
                                                fv.signing_public_hex(),
                                                fv.public_key_hex())).decode()


def key_init(_args: argparse.Namespace) -> int:
    """Make the family key here (or use the one held) and start the membership chain.

    With no chain yet, this computer writes record 0: itself, plus every registered,
    bound computer whose possession proof OPENS under the key held here (a household from
    before the chain). A computer without the family key can never write it."""
    key = fv.load_family_key(FAMILY)
    if key is None:
        key = fv.new_family_key()
        fv.save_family_key(FAMILY, key)
        print(f"Made the family key here (id {fv.key_id(key).hex()}). It never leaves family "
              "devices: share it with `adk storage drive key share`.")
    else:
        print(f"This computer already holds the family key (id {fv.key_id(key).hex()}).")
    rc = key_register(_args)
    served = _chain()
    if rc != 0 or served.get("records"):
        return rc
    tenant, workspace = str(served.get("tenant") or ""), str(served.get("workspace") or "")
    me_s, me_x = fv.signing_public_hex(), fv.public_key_hex()
    members = {me_s: me_x}
    proofs = {me_s: fv.possession_proof(key, tenant, workspace, me_s, me_x).hex()}
    for d in _devices():
        s = d.get("sign_pubkey") or ""
        blob = base64.b64decode(d.get("possession_b64") or "")
        if s and s != me_s and d.get("bound") and fv.check_possession(
                key, blob, tenant, workspace, s, d["pubkey"]):
            members[s], proofs[s] = d["pubkey"], blob.hex()
    record = fv.chain_record(tenant=tenant, workspace=workspace, seq=0, prev_hash="",
                             members=members, signer=me_s, key_id_hex=fv.key_id(key).hex(),
                             proofs=proofs)
    _append(record)
    fv.save_head(FAMILY, fv.verify_genesis(record, fv.sign_record(record), tenant=tenant,
                                           workspace=workspace, key=key))
    print(f"Started the household's membership chain with {len(members)} member(s).")
    return 0


def _pending(head: Dict[str, Any]) -> List[str]:
    """Members whose removal the household says was requested (signed into the next
    record, where every family computer checks them)."""
    return sorted(p["sign_pubkey"] for p in rotation_state().get("pending") or []
                  if p.get("sign_pubkey") in head["members"])


def _added_by(sign: str) -> Dict[str, str]:
    """{signing key: x25519} of current members that ``sign`` added (verified chain)."""
    recs = sorted(_chain().get("records") or [], key=lambda r: int(r["seq"]))
    prev: Dict[str, str] = {}
    added: Dict[str, str] = {}
    for r in recs:
        b = fv.parse_record(base64.b64decode(r["record_b64"]),
                            base64.b64decode(r["signature_b64"]))
        if b["seq"] > 0 and b["signer"] == sign:
            added.update({s: x for s, x in b["members"].items() if s not in prev})
        prev = b["members"]
    return {s: x for s, x in added.items() if s in prev}


def _added_by_all(sign: str) -> Dict[str, str]:
    """Every current member ``sign`` added, and everyone THEY added, and so on."""
    out: Dict[str, str] = {}
    todo = [sign]
    while todo:
        for s, x in _added_by(todo.pop()).items():
            if s not in out and s != sign:
                out[s] = x
                todo.append(s)
    return out


def _next(head: Dict[str, Any], members: Dict[str, str]) -> bytes:
    """The next plain membership record (no key change), carrying the pending removals."""
    return fv.chain_record(tenant=head["tenant"], workspace=head["workspace"],
                           seq=int(head["seq"]) + 1, prev_hash=head["hash"], members=members,
                           signer=fv.signing_public_hex(), pending_removals=_pending(head),
                           ts=max(fv._now(), int(head.get("ts") or 0)),
                           epoch=int(head.get("epoch") or 0))


def _append(record: bytes) -> None:
    request("POST", "/family/storage/keys/chain", json_body={
        "record_b64": base64.b64encode(record).decode(),
        "signature_b64": base64.b64encode(fv.sign_record(record)).decode()})


def key_enroll(args: argparse.Namespace) -> int:
    """Redeem a family device enroll code here; keeps the device credential (owner-only)."""
    label = getattr(args, "label", "") or platform.node()
    _s, body = request("POST", "/me/device/claim", json_body={"code": args.code, "model": label})
    token = str((body or {}).get("device_token") or "")
    if not token:
        raise fv.VaultError("the household did not return a device credential")
    fv._write_private(fv.vault_dir() / "device_token", token.encode())
    print(f"This computer is family device {body.get('device_id')}. Next: "
          "`adk storage drive key register` (or `key init` on the first family computer).")
    return 0


def _my_fingerprint() -> str:
    return fv.fingerprint(bytes.fromhex(fv.public_key_hex()),
                          bytes.fromhex(fv.signing_public_hex()))


def key_register(args: argparse.Namespace) -> int:
    label = getattr(args, "label", "") or platform.node()
    if not _device_token():
        raise fv.VaultError("this computer is not an enrolled family device: run "
                            "`adk storage drive key enroll <code>` with a code from the "
                            "Family app first")
    served = _chain()
    body = {"pubkey": fv.public_key_hex(), "sign_pubkey": fv.signing_public_hex(),
            "label": label}
    proof = _possession_b64(str(served.get("tenant") or ""), str(served.get("workspace") or ""))
    if proof:
        body["possession_b64"] = proof
    _s, out = request("POST", "/family/storage/keys/devices", json_body=body)
    print(f"Registered this computer ({label}) for the family key.")
    if not (out or {}).get("approved"):
        print(f"A member computer adds it after the guardian checks this fingerprint: "
              f"{_my_fingerprint()}\n  (there: `adk storage drive key approve "
              f"{fv.public_key_hex()} {_my_fingerprint()}`)")
    return 0


def _leave_wrap(pubkey_hex: str, kid: str, blob: bytes) -> None:
    request("POST", "/family/storage/keys/wraps", json_body={
        "to_pubkey": pubkey_hex, "key_id": kid, "blob_b64": base64.b64encode(blob).decode()})


def key_approve(args: argparse.Namespace) -> int:
    """On a MEMBER computer: add another computer to the chain after checking its
    fingerprint (the guardian reads it off the new computer), then share the key."""
    head = _need_member(_sync_chain()[0])
    want = args.pubkey.lower()
    hits = [d for d in _devices() if d["pubkey"] == want]
    if len(hits) != 1 or not hits[0].get("sign_pubkey"):
        raise fv.VaultError("no registered computer has that public key")
    d = hits[0]
    fp = fv.fingerprint(bytes.fromhex(d["pubkey"]), bytes.fromhex(d["sign_pubkey"]))
    if args.fingerprint.replace("-", "").lower() != fp.replace("-", ""):
        raise fv.VaultError("the fingerprint does not match that computer's keys; do not "
                            "add it")
    if d["sign_pubkey"] in head["members"]:
        print("That computer is already a member.")
        return 0
    record = _next(head, {**head["members"], d["sign_pubkey"]: d["pubkey"]})
    _append(record)
    fv.save_head(FAMILY, fv.verify_extends(head, record, fv.sign_record(record)))
    print("Added it to the household's family key members.")
    return key_share(args)


def key_rotate(args: argparse.Namespace) -> int:
    """A new key version in a chain record that drops every device whose removal was
    requested; wrapped to exactly the remaining members (wraps first, record last; the
    key is made current here only once the household appended the record)."""
    family_key()
    head = _need_member(_sync_chain()[0])
    drop = _pending(head)
    if set(drop) != set(head.get("pending") or []):
        # first a record that ANNOUNCES the removals to every device and guardian; the
        # drop is valid only REMOVAL_DELAY after it (a guardian can cancel meanwhile)
        record = _next(head, dict(head["members"]))
        _append(record)
        head = fv.verify_extends(head, record, fv.sign_record(record))
        fv.save_head(FAMILY, head)
        print(f"Announced the removal of {len(drop)} device(s); it can take effect in "
              f"{fv.REMOVAL_DELAY // 3600} h (a guardian can cancel until then). Run "
              "`adk storage drive key rotate` again then.")
        if drop:
            return 3
    wait = {x: t for x, t in fv.removal_ready(head).items() if x in drop and t > 0}
    if wait:
        print(f"The removal can take effect in {max(wait.values()) // 3600 + 1} h at the "
              "latest; run `adk storage drive key rotate` again then.")
        return 3
    rot = fv.rotate_family_key(FAMILY, head, remove=drop, pending=drop, install=False)
    for x, blob in rot.wraps.items():
        _leave_wrap(x, rot.key_id, blob)
    request("POST", "/family/storage/keys/chain", json_body={
        "record_b64": base64.b64encode(rot.record).decode(),
        "signature_b64": base64.b64encode(rot.signature).decode()})
    fv.install_rotated_key(FAMILY, rot.key)
    fv.save_head(FAMILY, fv.verify_extends(head, rot.record, rot.signature))
    print(f"Rotated the family key {rot.previous_key_id} -> {rot.key_id}; "
          f"{len(rot.wraps)} member(s) hold it (each runs `adk storage drive key accept`).")
    if getattr(args, "no_reencrypt", False):
        print("Files stay sealed under the old version, and the household keeps saying so, "
              "until `adk storage drive key reencrypt`.")
        return 0
    return key_reencrypt(args)


def key_reencrypt(args: argparse.Namespace) -> int:
    """Seal every chunk under the current version, verify, then retire the old versions."""
    from adk import family_drive_files as files

    key = family_key()
    head = rotation_state().get("head")
    if head and head["key_id"] != fv.key_id(key).hex():
        raise fv.VaultError("this computer is not on the newest family key: run "
                            "`adk storage drive key accept` first")
    out = files.reencrypt_drive(key, wait_s=float(getattr(args, "wait", 600) or 600))
    print(f"Sealed {out['chunks']} chunk(s) of {out['files']} file(s) under the current key.")
    if out["remaining"]:
        print(f"{out['remaining']} chunk(s) are still under an old key (files kept changing); "
              "run `adk storage drive key reencrypt` again.", file=sys.stderr)
        return 2
    if head:
        request("POST", "/family/storage/keys/rotation/reencrypted",
                json_body={"key_id": head["key_id"]})
    fv.forget_retired_keys(FAMILY)
    return 0


def key_remove(args: argparse.Namespace) -> int:
    """Request a computer's removal (by public key or label), then rotate it out."""
    want = (args.device or "").strip().lower()
    hits = [d for d in _devices()
            if d["pubkey"] == want or (d.get("label") or "").lower() == want]
    if len(hits) != 1:
        raise fv.VaultError(f"{len(hits)} registered computers match {args.device!r}; "
                            "use its public key (`key status` on that computer prints it)")
    if hits[0]["pubkey"] == fv.public_key_hex():
        raise fv.VaultError("this is the computer you are on; remove it from another one")
    _sync_chain()
    added = _added_by_all(hits[0].get("sign_pubkey") or "")
    if added:
        names = {d.get("sign_pubkey"): d.get("label") or d["pubkey"][:12] for d in _devices()}
        print("It added (directly or through them) these members: "
              + ", ".join(names.get(s, s[:12]) for s in added)
              + ("" if getattr(args, "with_added", False)
                 else "  (they stay; pass --with-added to remove them in the same rotation)"))
    request("DELETE", f"/family/storage/keys/devices/{hits[0]['pubkey']}")
    print(f"Asked to remove {hits[0].get('label') or hits[0]['pubkey'][:12]}.")
    if getattr(args, "with_added", False):
        for s, x in added.items():
            if x != fv.public_key_hex():
                request("DELETE", f"/family/storage/keys/devices/{x}")
    return key_rotate(args)


def key_approve_remove(args: argparse.Namespace) -> int:
    """The SECOND guardian approves a removal (households with two or more guardians)."""
    want = args.device.lower()
    head = fv.load_head(FAMILY) or {}
    sign = next((s for s, x in (head.get("members") or {}).items() if want in (s, x)), want)
    request("POST", "/family/storage/keys/removals/approve", json_body={"sign_pubkey": sign})
    print("Approved the removal as the second guardian; its 24 h delay starts now.")
    return 0


def key_cancel_remove(args: argparse.Namespace) -> int:
    """A guardian cancels a pending removal (needs a fresh passkey / authenticator sign-in;
    the household refuses a plain session and the device being removed)."""
    want = args.device.lower()
    head = fv.load_head(FAMILY) or {}
    sign = next((s for s, x in (head.get("members") or {}).items() if want in (s, x)), want)
    request("POST", "/family/storage/keys/removals/cancel", json_body={"sign_pubkey": sign})
    print("Cancelled the removal request (a receipt records it).")
    return 0


def key_recover(args: argparse.Namespace) -> int:
    """Guardian recovery of the chain: ``request`` / ``cancel`` (fresh step-up; every
    guardian is told), then after 72 h ``complete`` on a computer holding ANY family key
    version of this household: it writes record 0 of the next epoch with itself."""
    if args.step == "request":
        _s, out = request("POST", "/family/storage/keys/recovery")
        print("Recovery requested; every guardian was told. Complete it in 72 hours with "
              "`adk storage drive key recover complete`.")
        return 0
    if args.step == "cancel":
        request("POST", "/family/storage/keys/recovery/cancel")
        print("Recovery cancelled.")
        return 0
    if args.step == "approve":
        request("POST", "/family/storage/keys/recovery/approve")
        print("Approved the recovery as the second guardian.")
        return 0
    served = _chain()
    recs = served.get("records") or []
    epoch = int(json.loads(base64.b64decode(recs[0]["record_b64"])).get("epoch") or 0) + 1 \
        if recs else 0
    tenant, workspace = str(served.get("tenant") or ""), str(served.get("workspace") or "")
    ring = fv.keyring(FAMILY)
    if not ring:
        raise fv.VaultError("this computer holds no family key version: it cannot recover")
    key = fv.load_family_key(FAMILY) or next(iter(ring.values()))
    me_s, me_x = fv.signing_public_hex(), fv.public_key_hex()
    record = fv.chain_record(tenant=tenant, workspace=workspace, seq=0, prev_hash="",
                             members={me_s: me_x}, signer=me_s,
                             key_id_hex=fv.key_id(key).hex(), epoch=epoch,
                             proofs={me_s: fv.possession_proof(key, tenant, workspace, me_s,
                                                               me_x).hex()})
    _append(record)
    fv.save_head(FAMILY, fv.verify_genesis(record, fv.sign_record(record), tenant=tenant,
                                           workspace=workspace, key=key))
    print(f"Recovered: the family key chain restarts at epoch {epoch} with this computer. "
          "Add the others again with `key approve`, then `key rotate`.")
    return 0


def key_share(args: argparse.Namespace) -> int:
    key = family_key()
    if any(p.get("sign_pubkey") for p in rotation_state().get("pending") or []):
        print("A family device's removal is pending: rotating (or announcing it) instead "
              "of sharing the old key.")
        return key_rotate(args)
    head = _need_member(_sync_chain()[0])
    kid = fv.key_id(key).hex()
    if head.get("key_id") and head["key_id"] != kid:
        raise fv.VaultError("this computer is not on the newest family key: run "
                            "`adk storage drive key accept` first")
    have = {d["pubkey"]: d.get("has_key", []) for d in _devices()}
    me = fv.public_key_hex()
    shared = 0
    for s, x in head["members"].items():  # the chain's members, never the household's list
        if x == me or kid in have.get(x, []):
            continue
        _leave_wrap(x, kid, fv.wrap_key(key, bytes.fromhex(x)))
        shared += 1
        print(f"  wrapped for member {x[:12]}")
    print(f"Shared the family key with {shared} computer(s); "
          "each runs `adk storage drive key accept`.")
    return 0


def _held_wraps(wraps: List[Dict[str, Any]]) -> Dict[str, bytes]:
    """Every wrapped key that opens here and matches its id; a bad one is skipped."""
    held: Dict[str, bytes] = {}
    for w in wraps:
        try:
            k = fv.unwrap_key(base64.b64decode(w["blob_b64"]))
        except (fv.VaultError, ValueError, KeyError, TypeError):
            continue
        if fv.key_id(k).hex() == w.get("key_id"):
            held[w["key_id"]] = k
    return held


def key_accept(_args: argparse.Namespace) -> int:
    pub = fv.public_key_hex()
    _s, body = request("GET", "/family/storage/keys/wraps", params={"pubkey": pub})
    held = _held_wraps(body.get("wraps", []))
    head, _served = _sync_chain(getattr(_args, "accept_new_epoch", False))
    if head is None:
        raise fv.VaultError("the household has no membership chain yet: a computer that "
                            "holds the family key runs `adk storage drive key init`")
    _need_member(head)
    want = str(head.get("key_id") or "")
    current = fv.load_family_key(FAMILY)
    if current is not None and fv.key_id(current).hex() == want:
        print(f"This computer already holds the current family key (id {want}).")
    else:
        key = held.get(want)
        if key is None:
            print("The current family key has not been wrapped for this computer yet. Ask a "
                  "member computer to run `adk storage drive key share`.", file=sys.stderr)
            return 2
        if current is None:
            fv.save_family_key(FAMILY, key)
        else:
            fv.install_rotated_key(FAMILY, key)
        print(f"This computer now holds the family key (id {want}).")
    if (rotation_state().get("head") or {}).get("reencrypted"):
        fv.forget_retired_keys(FAMILY)  # every chunk is under this version already
    return 0


def key_status(args: argparse.Namespace) -> int:
    key = fv.load_family_key(FAMILY)
    head = fv.load_head(FAMILY)
    out: Dict[str, Any] = {"key_id": fv.key_id(key).hex() if key else "",
                           "device_pubkey": fv.public_key_hex(),
                           "sign_pubkey": fv.signing_public_hex(),
                           "fingerprint": _my_fingerprint(),
                           "enrolled": bool(_device_token()),
                           "chain_seq": head["seq"] if head else None,
                           "member": bool(head and fv.signing_public_hex() in head["members"]),
                           "retired_key_ids": sorted(fv.retired_keys(FAMILY))}
    try:
        state = rotation_state()
        out["rotation_pending"] = bool(state.get("rotation_pending"))
        out["reencrypt_pending"] = bool(state.get("reencrypt_pending"))
    except ApiError:
        out["rotation_pending"] = out["reencrypt_pending"] = None  # unknown: no answer
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
    else:
        print(f"family key: {out['key_id'] or 'none on this computer'}")
        print(f"this computer's public key: {out['device_pubkey']}")
        print(f"fingerprint (checked before a member adds it): {out['fingerprint']}")
        print(f"member: {'yes' if out['member'] else 'no'} (chain seq {out['chain_seq']})")
        if out["retired_key_ids"]:
            print("old versions kept for re-sealing: " + ", ".join(out["retired_key_ids"]))
        if out["rotation_pending"]:
            print("A family device was removed: run `adk storage drive key rotate` here.")
        if out["reencrypt_pending"]:
            print("Some files may still be under an old key: `adk storage drive key reencrypt`.")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adk family-drive",
                                description="Your family's drive on the family's own devices")
    sub = p.add_subparsers(dest="verb")
    key = sub.add_parser("key", help="The family key (made and kept on family devices)")
    ks = key.add_subparsers(dest="key_verb")
    init = ks.add_parser("init", help="Make the family key here (first family computer)")
    init.add_argument("--label", default="")
    enr = ks.add_parser("enroll", help="Make this computer a family device (enroll code)")
    enr.add_argument("code")
    enr.add_argument("--label", default="")
    reg = ks.add_parser("register", help="Publish this computer's public keys to the family")
    reg.add_argument("--label", default="")
    apr = ks.add_parser("approve", help="On a member computer: add another by fingerprint")
    apr.add_argument("pubkey")
    apr.add_argument("fingerprint")
    ks.add_parser("share", help="Wrap the key to every approved family computer")
    rot = ks.add_parser("rotate", help="New key version for the approved computers left")
    rot.add_argument("--no-reencrypt", action="store_true",
                     help="Only rotate; seal the files again later with `key reencrypt`")
    rot.add_argument("--wait", type=float, default=600)
    rem = ks.add_parser("remove", help="Take a lost / sold computer off the key, then rotate")
    rem.add_argument("device", help="Its public key or label")
    rem.add_argument("--with-added", action="store_true",
                     help="Also remove the members it added (listed first)")
    rem.add_argument("--no-reencrypt", action="store_true")
    can = ks.add_parser("cancel-remove", help="Guardian (step-up): cancel a pending removal")
    can.add_argument("device", help="Its public key (x25519 or signing)")
    rec = ks.add_parser("recover", help="Guardian recovery of the family key chain")
    rec.add_argument("step", choices=["request", "approve", "cancel", "complete"])
    apr2 = ks.add_parser("approve-remove",
                         help="Second guardian (step-up): approve a pending removal")
    apr2.add_argument("device", help="Its public key (x25519 or signing)")
    rem.add_argument("--wait", type=float, default=600)
    ree = ks.add_parser("reencrypt", help="Seal the whole drive under the current key version")
    ree.add_argument("--wait", type=float, default=600)
    acc = ks.add_parser("accept", help="Pick up the key wrapped for this computer")
    acc.add_argument("--accept-new-epoch", action="store_true",
                     help="Follow a guardian recovery (a new epoch of the chain)")
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
    try:
        from adk.family_drive_backup import add_parsers as add_backup
    except ImportError:
        return
    add_backup(sub)


_KEY = {"init": key_init, "register": key_register, "share": key_share,
        "accept": key_accept, "status": key_status, "rotate": key_rotate,
        "remove": key_remove, "reencrypt": key_reencrypt, "enroll": key_enroll,
        "cancel-remove": key_cancel_remove, "recover": key_recover,
        "approve-remove": key_approve_remove,
        "approve": key_approve}


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
        if "step_up" in exc.detail:
            print(STEP_UP_HINT, file=sys.stderr)
        return 2
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
