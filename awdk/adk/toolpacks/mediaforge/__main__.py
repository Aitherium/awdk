"""CLI for the media-forge tool pack.

  python -m adk.toolpacks.mediaforge --self-test   # prove the pack can still fail
  python -m adk.toolpacks.mediaforge --list        # what this pack binds, and where

`--list` exists because the defect this pack was created to fix — 24 tools
declared, 0 bound — is invisible from the manifest alone. Printing the bound
names next to the engine URL makes "declared" and "bound" two different,
checkable things rather than one assumed one.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(prog="adk.toolpacks.mediaforge")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the pack's registration can still fail")
    ap.add_argument("--list", action="store_true",
                    help="list the tools this pack binds and the engine URL")
    args = ap.parse_args()

    if not (args.self_test or args.list):
        ap.print_help()
        return 2

    from . import _collect, self_test

    if args.self_test:
        return self_test()

    fns = _collect()
    # Read the engine URL from a client module rather than re-deriving it: this
    # is the value the tools will ACTUALLY use, resolved at their import time.
    # Re-computing it here from the env would print what the URL *should* be,
    # which is precisely the difference that hid the broken surface.
    base = "unknown"
    try:
        from . import mcp_character_forge as _cf
        base = getattr(_cf, "_BASE", "unknown")
    except Exception as exc:                          # noqa: BLE001
        base = f"unresolved ({type(exc).__name__})"

    print(f"engine: {base}")
    print(f"bound:  {len(fns)} tools")
    for fn in fns:
        print(f"  {getattr(fn, '__name__', '?')}")
    if not fns:
        print("NOTHING BOUND — the vendored client modules are missing or "
              "failed to import; this pack would advertise and deliver nothing.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
