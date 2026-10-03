"""``python -m adk.cells plan estate.yaml nodes.yaml [--current current.yaml]``
``python -m adk.cells inventory [--name N] [--label k=v ...] [--tag t ...]``
``python -m adk.cells serve --cell memory=DB --tokens T.yaml --cert C --key K``

``plan`` prints the placement and the reconciler actions as JSON. Exit 0 placed, 1 the
estate cannot be placed (every problem is listed), 2 the input could not be read.
``inventory`` prints this machine, measured, as a ``nodes:`` YAML document. Exit 0, or
2 when a probe could not measure the host.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import yaml

from .estate import EstateError, PlanError, diff, load_files, plan
from .inventory import ProbeError, local_node, node_doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m adk.cells")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="place an estate on a node inventory")
    p.add_argument("estate")
    p.add_argument("nodes")
    p.add_argument("--current", help="yaml {cell: [node, ...]} of what runs now")
    inv = sub.add_parser("inventory", help="measure this machine as an estate node")
    inv.add_argument("--name", help="node name (default: hostname)")
    inv.add_argument("--label", action="append", default=[], metavar="KEY=VALUE")
    inv.add_argument("--tag", action="append", default=[])
    srv = sub.add_parser("serve", help="serve cells on this node over TLS")
    srv.add_argument("--cell", action="append", required=True, metavar="NAME=TARGET")
    srv.add_argument("--tokens", required=True, help="yaml of sha256(token) -> caller")
    srv.add_argument("--cert", required=True)
    srv.add_argument("--key", required=True)
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8443)
    srv.add_argument("--name", help="node name for /cells/_node (default: hostname)")
    args = parser.parse_args(argv)

    if args.cmd == "inventory":
        return _inventory(args)
    if args.cmd == "serve":
        return _serve(args)

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


def _serve(args: argparse.Namespace) -> int:
    from .node import TokenFileError, host_cells, load_callers, serve

    try:
        cells = host_cells(args.cell)
        authenticate = load_callers(args.tokens)
    except (OSError, ValueError, yaml.YAMLError, TokenFileError) as exc:
        print(f"cannot start node: {exc}", file=sys.stderr)
        return 2
    try:
        serve(cells, authenticate, cert=args.cert, key=args.key, host=args.host,
              port=args.port, node_name=args.name)
    except FileNotFoundError as exc:
        print(f"cannot start node: {exc}", file=sys.stderr)
        return 2
    return 0


def _inventory(args: argparse.Namespace) -> int:
    labels = {}
    for item in args.label:
        key, sep, value = item.partition("=")
        if not sep or not key:
            print(f"--label wants KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        labels[key] = yaml.safe_load(value)
    try:
        node = local_node(args.name, labels, set(args.tag))
    except (ProbeError, OSError, subprocess.SubprocessError) as exc:
        print(f"cannot measure this host: {exc}", file=sys.stderr)
        return 2
    print(yaml.safe_dump(node_doc(node), sort_keys=False), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
