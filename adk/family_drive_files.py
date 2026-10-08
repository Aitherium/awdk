"""`adk storage drive put|get|ls|rm` -- files on the family drive (B8).

A file is split into chunks of up to 32 MiB, each sealed with the family key
(:mod:`adk.family_vault`) and stored as one object on the family's devices. The drive's
list of files (names, sizes, chunk ids) is itself one sealed object, the *index*; the
household holds only which object that is and moves it with compare-and-set, so two
family computers adding files at once both land (the slower one re-reads and retries).

Reading a chunk the household no longer stages asks a device that keeps it to send it
back; ``get`` waits for that (``--wait`` seconds, default 600).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from adk import family_drive_cli as cli
from adk import family_vault as fv

CHUNK = 32 * 1024 * 1024
INDEX_SCHEMA = "aither-family-drive-index/1"
_RETRIES = 5

# indirection so tests can swap the clock
_sleep: Callable[[float], None] = time.sleep


def _empty() -> Dict[str, Any]:
    return {"schema": INDEX_SCHEMA, "files": {}}


def fetch_object(object_id: str, *, wait_s: float = 600, poll_s: float = 15) -> bytes:
    """The bytes of one object, waiting while a family device sends them back."""
    deadline = time.monotonic() + wait_s
    while True:
        status, body = cli.request("GET", f"/family/storage/objects/{object_id}")
        if status == 200 and isinstance(body, (bytes, bytearray)):
            return bytes(body)
        if time.monotonic() >= deadline:
            raise fv.VaultError("a family device has not sent this back yet; try again later "
                                "(phones send only on Wi-Fi while charging)")
        _sleep(poll_s)


def put_object(sealed: bytes, *, replicas: int, tier: str = "warm") -> str:
    _s, body = cli.request("POST", "/family/storage/objects", content=sealed,
                           params={"replicas": str(replicas), "tier": tier})
    return str(body["object_id"])


def read_index(key: bytes, *, wait_s: float = 600) -> tuple:
    _s, ptr = cli.request("GET", "/family/storage/drive/index")
    oid = str(ptr.get("object_id") or "")
    if not oid:
        return "", _empty()
    index = json.loads(fv.open_sealed(key, fetch_object(oid, wait_s=wait_s)))
    if index.get("schema") != INDEX_SCHEMA:
        raise fv.VaultError("the drive index is not one this adk understands")
    return oid, index


def update_index(key: bytes, change: Callable[[Dict[str, Any]], None], *,
                 replicas: int = 2, wait_s: float = 600) -> Dict[str, Any]:
    """Read, change, seal, store and point at the new index; retry when another computer won."""
    for _attempt in range(_RETRIES):
        prev, index = read_index(key, wait_s=wait_s)
        change(index)
        sealed = fv.seal(key, json.dumps(index, sort_keys=True).encode())
        new_id = put_object(sealed, replicas=replicas)
        try:
            cli.request("POST", "/family/storage/drive/index",
                        json_body={"object_id": new_id, "previous": prev})
        except cli.ApiError as exc:
            if exc.status == 409 and "index_moved" in exc.detail:
                cli.request("DELETE", f"/family/storage/objects/{new_id}")
                continue
            raise
        if prev:
            try:
                cli.request("DELETE", f"/family/storage/objects/{prev}")
            except cli.ApiError:
                pass  # an old index left behind is harmless; repair will not chase it
        return index
    raise fv.VaultError("the drive kept changing under us; try again")


def _clean_name(name: str) -> str:
    n = name.replace("\\", "/").strip("/")
    if not n or n.startswith("..") or "/../" in f"/{n}/" or len(n) > 512:
        raise fv.VaultError(f"not a drive path: {name!r}")
    return n


def cmd_put(args: argparse.Namespace) -> int:
    key = cli.family_key()
    src = Path(args.file)
    if not src.is_file():
        raise fv.VaultError(f"no such file: {src}")
    name = _clean_name(args.name or src.name)
    chunks: List[str] = []
    size = 0
    with src.open("rb") as f:
        while True:
            part = f.read(CHUNK)
            if not part and chunks:
                break
            size += len(part)
            chunks.append(put_object(fv.seal(key, part), replicas=args.replicas))
            if not part:
                break
    entry = {"size": size, "mtime": int(src.stat().st_mtime), "chunks": chunks,
             "added": int(time.time()), "replicas": args.replicas}

    def change(index: Dict[str, Any]) -> None:
        index["files"][name] = entry

    update_index(key, change, replicas=args.replicas)
    print(f"Stored {name} ({size} bytes, {len(chunks)} chunk(s), {args.replicas} copies wanted).")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    key = cli.family_key()
    name = _clean_name(args.name)
    _oid, index = read_index(key, wait_s=args.wait)
    entry = index["files"].get(name)
    if entry is None:
        raise fv.VaultError(f"{name} is not on the family drive")
    out = Path(args.out or Path(name).name)
    tmp = out.with_suffix(out.suffix + ".part")
    with tmp.open("wb") as f:
        for oid in entry["chunks"]:
            f.write(fv.open_sealed(key, fetch_object(oid, wait_s=args.wait)))
    tmp.replace(out)
    print(f"Wrote {out} ({entry['size']} bytes).")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    key = cli.family_key()
    _oid, index = read_index(key, wait_s=args.wait)
    _s, objs = cli.request("GET", "/family/storage/objects")
    stored = {o["object_id"]: int(o.get("stored") or 0) for o in objs.get("objects", [])}
    rows = []
    for name, e in sorted(index["files"].items()):
        copies = min((stored.get(c, 0) for c in e["chunks"]), default=0)
        rows.append({"name": name, "size": e["size"], "copies": copies,
                     "wanted": e.get("replicas", 2), "added": e.get("added")})
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
    else:
        if not rows:
            print("The family drive is empty.")
        for r in rows:
            print(f"{r['size']:>12}  {r['copies']}/{r['wanted']} copies  {r['name']}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    key = cli.family_key()
    name = _clean_name(args.name)
    gone: Dict[str, Any] = {}

    def change(index: Dict[str, Any]) -> None:
        gone.clear()
        entry = index["files"].pop(name, None)
        if entry is not None:
            gone.update(entry)

    update_index(key, change)
    if not gone:
        raise fv.VaultError(f"{name} is not on the family drive")
    for oid in gone["chunks"]:
        try:
            cli.request("DELETE", f"/family/storage/objects/{oid}")
        except cli.ApiError:
            pass
    print(f"Removed {name} from the family drive.")
    return 0


def add_parsers(sub: Any) -> None:
    put = sub.add_parser("put", help="Seal a file and store it on the family's devices")
    put.add_argument("file")
    put.add_argument("--name", default="", help="Path on the drive (default: the file name)")
    put.add_argument("--replicas", type=int, default=2, choices=[1, 2, 3])
    put.set_defaults(handler=cmd_put)
    get = sub.add_parser("get", help="Fetch a file back and open it here")
    get.add_argument("name")
    get.add_argument("--out", default="")
    get.add_argument("--wait", type=float, default=600)
    get.set_defaults(handler=cmd_get)
    ls = sub.add_parser("ls", help="What is on the family drive (names are opened here)")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--wait", type=float, default=600)
    ls.set_defaults(handler=cmd_ls)
    rm = sub.add_parser("rm", help="Remove a file from the family drive")
    rm.add_argument("name")
    rm.set_defaults(handler=cmd_rm)
