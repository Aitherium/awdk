"""``python -m adk.cells plan estate.yaml nodes.yaml [--current current.yaml]``
``python -m adk.cells inventory [--name N] [--label k=v ...] [--tag t ...]``
``python -m adk.cells serve --cell memory=DB --tokens T.yaml --cert C --key K
[--images images.yaml [--podman-via wsl:<distro>]]``
``python -m adk.cells mcp (--remote memory=https://... --ca CA | --local memory=DB
--workspace T:U:P)`` - an MCP stdio server of generated cell tools
``python -m adk.cells control estate.yaml --node NAME=https://... --ca CA
[--token-env VAR] [--dry-run] [--ticks N] [--interval S]``

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
    srv.add_argument("--images", help="yaml {cells: {name: {image: ref@sha256:..., args: []}}}"
                     " - lets the control plane start/stop these cells here")
    srv.add_argument("--podman-via", default="local",
                     help="'local' or 'wsl:<distro>' (podman inside a WSL distro)")
    mcp = sub.add_parser("mcp", help="serve cell tools to an MCP client over stdio")
    mcp.add_argument("--remote", action="append", default=[], metavar="CELL=URL")
    mcp.add_argument("--local", action="append", default=[], metavar="CELL=TARGET")
    mcp.add_argument("--ca", help="CA bundle for --remote nodes")
    mcp.add_argument("--token-env", default="AITHER_CELLS_TOKEN",
                     help="env var holding your token for --remote nodes")
    mcp.add_argument("--workspace", help="tenant:user:project for --local cells")
    ctl = sub.add_parser("control", help="reconcile an estate across nodes")
    ctl.add_argument("estate")
    ctl.add_argument("--node", action="append", required=True, metavar="NAME=URL")
    ctl.add_argument("--ca", help="CA bundle the nodes' certificates chain to")
    ctl.add_argument("--token-env", default="AITHER_CELLS_OPERATOR_TOKEN",
                     help="env var holding the operator token (never a CLI argument)")
    ctl.add_argument("--dry-run", action="store_true")
    ctl.add_argument("--ticks", type=int, default=1, help="0 = run forever")
    ctl.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)

    if args.cmd == "inventory":
        return _inventory(args)
    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "control":
        return _control(args)
    if args.cmd == "mcp":
        return _mcp(args)

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
    runtime = None
    if args.images:
        try:
            runtime = _runtime(args.images, args.podman_via)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(f"cannot start node: {exc}", file=sys.stderr)
            return 2
    try:
        serve(cells, authenticate, cert=args.cert, key=args.key, host=args.host,
              port=args.port, node_name=args.name, runtime=runtime)
    except FileNotFoundError as exc:
        print(f"cannot start node: {exc}", file=sys.stderr)
        return 2
    return 0


def _runtime(images_path: str, via: str):
    from .reconcile import PodmanRuntime

    with open(images_path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    images, cell_args = {}, {}
    for name, raw in (doc.get("cells") or {}).items():
        raw = raw if isinstance(raw, dict) else {"image": raw}
        images[name] = raw.get("image", "")
        if raw.get("args"):
            cell_args[name] = [str(a) for a in raw["args"]]
    if via == "local":
        prefix: list[str] = []
    elif via.startswith("wsl:") and via[4:]:
        prefix = ["wsl", "-d", via[4:], "-u", "root", "--", "nsenter", "-t", "1", "-m", "--"]
    else:
        raise ValueError(f"--podman-via must be 'local' or 'wsl:<distro>', got {via!r}")

    def run(cmd: list[str]) -> str:
        proc = subprocess.run([*prefix, *cmd], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} exited {proc.returncode}: "
                               f"{proc.stderr.strip()[:200]}")
        return proc.stdout

    return PodmanRuntime(images, run, args=cell_args)


CONTRACTS = {"memory": ("adk.cells.memory", "MemoryCell")}


def _mcp(args: argparse.Namespace) -> int:
    import asyncio
    import importlib
    import os

    from .caller import Caller
    from .contract import Scope
    from .mcp_server import caller_from_whoami, serve_stdio
    from .node import host_cells
    from .registry import Cells
    from .transport import HttpsTransport

    if bool(args.remote) == bool(args.local):
        print("give --remote or --local cells (one mode)", file=sys.stderr)
        return 2
    if args.local:
        if not args.workspace:
            print("--local needs --workspace tenant:user:project", file=sys.stderr)
            return 2
        try:
            cells = host_cells(args.local)
        except (OSError, ValueError) as exc:
            print(f"cannot host cells: {exc}", file=sys.stderr)
            return 2
        caller = Caller(subject="local", scopes=frozenset({Scope.workspace}),
                        workspace=args.workspace)
    else:
        token = os.environ.get(args.token_env, "")
        if not token:
            print(f"no token: set {args.token_env}", file=sys.stderr)
            return 2
        cells, whoami_url = Cells(), None
        for item in args.remote:
            name, sep, url = item.partition("=")
            if name not in CONTRACTS or not sep:
                print(f"unknown remote cell {item!r} (known: {', '.join(CONTRACTS)})",
                      file=sys.stderr)
                return 2
            module, cls = CONTRACTS[name]
            contract_cls = getattr(importlib.import_module(module), cls)
            cells.remote(contract_cls, HttpsTransport(url, token=token, ca=args.ca))
            whoami_url = whoami_url or url
        try:
            import ssl

            import httpx

            verify = ssl.create_default_context(cafile=args.ca) if args.ca else True
            doc = httpx.get(whoami_url.rstrip("/") + "/cells/_whoami", verify=verify,
                            headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            print(f"cannot reach {whoami_url}: {exc}", file=sys.stderr)
            return 2
        caller = caller_from_whoami(doc)
    asyncio.run(serve_stdio(cells, caller))
    return 0


def _control(args: argparse.Namespace) -> int:
    import os

    from .control import run
    from .remote import RemoteNode

    token = os.environ.get(args.token_env, "")
    if not token:
        print(f"no operator token: set {args.token_env}", file=sys.stderr)
        return 2
    nodes = []
    for item in args.node:
        name, sep, url = item.partition("=")
        if not sep or not name or not url:
            print(f"--node wants NAME=URL, got {item!r}", file=sys.stderr)
            return 2
        try:
            nodes.append(RemoteNode(name, url, token, ca=args.ca))
        except (ValueError, OSError) as exc:
            print(f"bad node {name}: {exc}", file=sys.stderr)
            return 2

    def load_estate() -> dict:
        with open(args.estate, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    try:
        load_estate()
    except (OSError, yaml.YAMLError) as exc:
        print(f"cannot read estate: {exc}", file=sys.stderr)
        return 2

    outcome = {"rc": 0}

    def on_tick(result) -> None:
        if args.dry_run:
            result.dry_run = True
        print(json.dumps(result.as_dict(), indent=2), flush=True)
        bad = result.problems or (result.report and not result.report.ok)
        outcome["rc"] = 1 if bad else (2 if len(result.unreachable) == len(nodes) else 0)

    if args.dry_run:
        from .control import tick

        on_tick(tick(load_estate(), nodes, dry_run=True))
    else:
        run(load_estate, nodes, interval=args.interval, on_tick=on_tick,
            ticks=None if args.ticks == 0 else args.ticks)
    return outcome["rc"]


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
