"""``python -m adk.cells plan estate.yaml nodes.yaml [--current current.yaml]``

Prints the placement and the reconciler actions as JSON. Exit 0 placed, 1 the estate
cannot be placed (every problem is listed), 2 the input could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys

import yaml

from .estate import EstateError, PlanError, diff, load_files, plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m adk.cells")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="place an estate on a node inventory")
    p.add_argument("estate")
    p.add_argument("nodes")
    p.add_argument("--current", help="yaml {cell: [node, ...]} of what runs now")
    args = parser.parse_args(argv)

    try:
        estate = load_files(args.estate, args.nodes)
        current = {}
        if args.current:
            with open(args.current, encoding="utf-8") as fh:
                current = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError, EstateError) as exc:
        print(f"cannot read input: {exc}", file=sys.stderr)
        return 2
    try:
        placement = plan(estate)
    except PlanError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"placement": placement, "actions": diff(current, placement)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
