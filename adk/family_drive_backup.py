"""`adk family-drive backup ...` (also `adk storage drive backup ...`): back up and restore.

The household node snapshots the drive and copies its SEALED objects to the backup target
it was given (``AITHER_FAMILY_BACKUP_DIR`` on that node: a second disk, or a second family
node's share). Nothing is decrypted for a backup; the family key never leaves family
computers.

    backup now        start a backup pass and wait for it (--no-wait: just start it)
    backup status     last good backup, overdue or not, retained snapshots, signing key
    backup restore    put a snapshot back INTO the drive (asks first; --yes skips)
    backup drill      restore the newest snapshot to a scratch dir and verify every hash

Write down the ``signing key`` and fingerprint ``backup status`` shows (the guardians also
get the fingerprint once, when backups first succeed). Restoring on a NEW household node
checks the backup against that key -- never take it from a file on the backup disk.

Exit codes: 0 ok, 1 the backup is wrong (a mismatch, a failed backup, a drive with no file
list), 2 could not run, 3 not finished yet (``backup now``: ``waiting`` on a device to send
bytes back, ``partial`` past one pass's budget, ``busy`` with another pass, or still
running when ``--wait`` ran out) -- run it again later.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict

from adk import family_drive_cli as cli


def _when(ts: Any) -> str:
    if not ts:
        return "never"
    hours = (time.time() - float(ts)) / 3600
    return f"{hours:.0f} h ago" if hours >= 1 else "under an hour ago"


def _print(args: argparse.Namespace, body: Dict[str, Any]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(body, indent=2))


#: ``backup now`` status -> exit code. Unknown, ``failed`` and ``no_index`` are 1.
_NOW_EXIT = {"ok": 0, "empty": 0, "waiting": 3, "partial": 3, "busy": 3, "lost_claim": 3,
             "running": 3, "started": 3}


def cmd_now(args: argparse.Namespace) -> int:
    try:
        _s, started = cli.request("POST", "/family/storage/backup/now")
    except cli.ApiError as exc:
        if exc.status == 409 and "backup_running" in exc.detail:
            print("A backup, restore or drill is already running; try again shortly.",
                  file=sys.stderr)
            return 3
        raise
    body: Dict[str, Any] = dict(started or {})
    deadline = time.monotonic() + max(0.0, float(getattr(args, "wait", 0) or 0))
    while getattr(args, "wait", 0):
        _s, st = cli.request("GET", "/family/storage/backup")
        if not st.get("running"):
            body = {"status": st.get("last_status") or "started", "error": st.get("last_error"),
                    "last_ok": st.get("last_ok"), "snapshot_id": st.get("last_ok_snapshot")}
            break
        if time.monotonic() >= deadline:
            body = {"status": "running"}
            break
        time.sleep(2.0)
    _print(args, body)
    if not getattr(args, "json", False):
        print(f"backup {body.get('status')}"
              + (f" ({body['error']})" if body.get("error") else "")
              + (f": snapshot {str(body.get('snapshot_id'))[:12]}"
                 if body.get("status") == "ok" else ""))
    return _NOW_EXIT.get(str(body.get("status")), 1)


def cmd_status(args: argparse.Namespace) -> int:
    _s, body = cli.request("GET", "/family/storage/backup")
    _print(args, body)
    if not getattr(args, "json", False):
        if not body.get("configured"):
            print("No backup target is set up on the family's node "
                  "(AITHER_FAMILY_BACKUP_DIR).")
        print(f"last good backup: {_when(body.get('last_ok'))}"
              + ("  OVERDUE" if body.get("overdue") else ""))
        if body.get("last_status") and body.get("last_status") != "ok":
            print(f"last run: {body['last_status']} {body.get('last_error') or ''}".rstrip())
        for s in body.get("snapshots", []):
            print(f"  {s['snapshot_id'][:12]}  {s['objects']} objects  {s['bytes']} bytes  "
                  f"{_when(s['created'])}")
        print(f"complete on the target: {body.get('complete_on_target', 0)} "
              f"(keeping {body.get('keep')})")
        if body.get("unverified_on_target"):
            print(f"IGNORED on the target (not signed by this household): "
                  f"{body['unverified_on_target']}")
        if body.get("signing_key"):
            print(f"signing key: {body['signing_key']}")
            print(f"fingerprint: {body.get('signing_fingerprint')}  (write these down: a "
                  "restore on a new node checks the backup against them)")
    return 0 if body.get("configured") and not body.get("overdue") else 1


def cmd_restore(args: argparse.Namespace) -> int:
    if not args.yes:
        sys.stderr.write("This puts the snapshot back into the family drive and points the "
                         "drive at its file list. Files added since then stay stored but drop "
                         "out of the list. Type 'restore' to go on: ")
        sys.stderr.flush()
        if sys.stdin.readline().strip() != "restore":
            print("Nothing restored.", file=sys.stderr)
            return 2
    _s, body = cli.request("POST", "/family/storage/backup/restore",
                           json_body={"snapshot_id": args.snapshot, "confirm": True})
    _print(args, body)
    if not getattr(args, "json", False):
        print(f"restored snapshot {str(body.get('snapshot_id'))[:12]}: "
              f"{body.get('put_back', 0)} put back, {body.get('kept', 0)} were still there, "
              f"{len(body.get('failed') or [])} failed; index moved: {body.get('index_moved')}")
    return 1 if body.get("failed") else 0


def cmd_drill(args: argparse.Namespace) -> int:
    _s, body = cli.request("POST", "/family/storage/backup/drill")
    _print(args, body)
    if not getattr(args, "json", False):
        print(f"drill exit {body.get('exit')}: {body.get('reason')}")
    try:
        return int(body.get("exit", 2))
    except (TypeError, ValueError):
        return 2


def add_parsers(sub: Any) -> None:
    bk = sub.add_parser("backup", help="Back up and restore the family drive")
    bs = bk.add_subparsers(dest="backup_verb")
    for name, fn, text in (("now", cmd_now, "Start a backup pass and wait for it"),
                           ("status", cmd_status,
                            "Last good backup, snapshots, and the signing key to write down"),
                           ("drill", cmd_drill, "Restore to a scratch dir and verify hashes")):
        p = bs.add_parser(name, help=text)
        p.add_argument("--json", action="store_true")
        if name == "now":
            p.add_argument("--wait", type=float, default=600,
                           help="Seconds to wait for the pass (0: just start it)")
        p.set_defaults(handler=fn)
    r = bs.add_parser("restore", help="Put a snapshot back into the family drive")
    r.add_argument("--snapshot", default="", help="Snapshot id (default: the newest complete)")
    r.add_argument("--yes", action="store_true", help="Do not ask first")
    r.add_argument("--json", action="store_true")
    r.set_defaults(handler=cmd_restore)
