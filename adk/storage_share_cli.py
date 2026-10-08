"""`adk storage share on|off|status` -- lend this device's disk to the family's mesh pool.

Opt-in (owner, 2026-10-07): nothing is lent until the owner runs ``adk storage share on``.
The choice is saved as the ``storage:`` section of ``~/.aither/config.yaml`` and every
join path and heartbeat reads it (:mod:`adk.storage_contribution`). Turning it off stops
the keepalive at its next round, so the pool drops the peer within minutes.

    adk storage share on --quota 50                # lend at most 50 GiB
    adk storage share on --quota 50 --path D:\\aither-pool
    adk storage share on --quota 20 --allow-battery --allow-metered
    adk storage share off
    adk storage share status [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from adk import storage_contribution as sc


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adk storage share",
                                description="Lend this device's disk to your family's mesh pool")
    sub = p.add_subparsers(dest="verb")
    on = sub.add_parser("on", help="Start lending (a quota is required)")
    on.add_argument("--quota", type=float, required=True, metavar="GB",
                    help="Most this device lends, in GiB")
    on.add_argument("--path", default="", help="Folder the pool may use (absolute)")
    on.add_argument("--allow-battery", action="store_true",
                    help="Keep lending on battery (paused by default)")
    on.add_argument("--allow-metered", action="store_true",
                    help="Keep lending on a metered network (paused by default)")
    sub.add_parser("off", help="Stop lending")
    st = sub.add_parser("status", help="What this device lends right now, and why")
    st.add_argument("--json", action="store_true")
    st.add_argument("--node-class", default=os.getenv("AITHER_NODE_CLASS", ""),
                    help="Node class for the tier preview (default: $AITHER_NODE_CLASS)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv if argv is not None else sys.argv[1:])
    if args.verb == "on":
        if args.quota <= 0:
            print("adk storage share on: --quota must be more than 0 GiB", file=sys.stderr)
            return 2
        if args.path and not Path(args.path).is_absolute():
            print("adk storage share on: --path must be absolute", file=sys.stderr)
            return 2
        saved = sc.save_settings(sc.StorageSettings(
            enabled=True, quota_gb=args.quota, path=args.path,
            pause_on_battery=not args.allow_battery, pause_on_metered=not args.allow_metered))
        print(f"Lending up to {saved.quota_gb:g} GiB to your family's mesh pool"
              + (f" from {saved.path}" if saved.path else "")
              + ". It takes effect at the next heartbeat; `adk storage share off` stops it.")
        return 0
    if args.verb == "off":
        cur = sc.load_settings()
        sc.save_settings(cur._replace(enabled=False))
        print("Storage lending is off. The pool drops this device within a few minutes.")
        return 0
    if args.verb == "status":
        view = sc.storage_capability(getattr(args, "node_class", ""))
        if getattr(args, "json", False):
            print(json.dumps(view, indent=2, sort_keys=True))
            return 0
        d = view["detail"]
        if not view["opted_in"]:
            print(f"Not lending: {view['reason']}")
        elif d["paused"]:
            print(f"Opted in, paused ({d['paused']}). Quota {d['quota_gib']:g} GiB.")
        elif view["serving"]:
            print(f"Lending {d['contributed_gib']:g} GiB (quota {d['quota_gib']:g} GiB, "
                  f"{d['used_gib']:g} GiB in use, {d['free_gib']:g} GiB free on the disk).")
        else:
            print(f"Opted in, not lending: {view['reason']}")
        return 0
    _parser().print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
