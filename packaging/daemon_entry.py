"""Narrow PyInstaller entry point — the two daemons the launcher starts, and nothing else.

WHY THIS EXISTS, measured 2026-09-05.

``build_executable.py`` freezes ``adk/cli.py``, the whole CLI. Run end to end
that produces **727,327,049 bytes (694 MiB)** in ~17 minutes, having processed
164+ PyInstaller hooks: torch, torchvision, torchaudio, transformers, scipy,
sklearn, pandas, matplotlib, numpy, cv2, PIL, boto3, grpc. It works — the
binary runs and ``harness --help`` lists ``serve`` — it is simply enormous, and
the AitherOS launcher bundle needs exactly TWO things from it.

The cause is one import. ``cli.py`` imports ``adk.images`` for the image
commands; the daemons do not. Measured through the import system rather than
guessed:

    from adk.harnesses.daemon import create_app  ->  33 adk modules,  0 ML
    from adk.server import main                  ->  21 adk modules,  0 ML
    both together                                ->  35 adk modules,  0 ML

where "0 ML" means no module matching torch/transformers/scipy/sklearn/
pandas/numpy/cv2/PIL/boto3/grpc appears in ``sys.modules``. So a freeze rooted
here carries the servers without the data-science distribution behind them.

🚨 THE ARGV CONTRACT IS NOT DECORATIVE. ``surfaceCommand()`` in
``.DEPLOYMENT/standalone/aitheros/src/surfaces.ts`` spawns this binary as:

    <bin> harness serve --host 127.0.0.1 --port <N>     (awsh)
    <bin> up                                            (adk)

A first draft of this file answered ``daemon`` and defaulted everything else to
the server, with the port hardcoded to 8062. It would have frozen cleanly,
installed cleanly, and then ignored the port the launcher picked — which is the
one thing the launcher exists to control, because awdesk's default port turned
out to sit inside a Windows reserved range. An entry point whose verbs do not
match its caller's is a binary that runs and does the wrong thing.

Both verbs below therefore mirror ``adk/cli.py``'s spelling exactly, and the
port comes from the flag, then ``AITHER_PORT``, then the module default — never
a literal here.

🪤 THE LAZY IMPORTS BELOW DO NOT STOP PYINSTALLER, and that is worth knowing
before someone "tidies" them into module scope. This module imports only
argparse/os/sys at module level, deliberately — and PyInstaller's Analysis
still imports the traced targets to walk their dependencies, so adk's
import-time side effects run AT BUILD TIME. Measured on the first narrow build:
16 service-construction lines in the build log ("Prometheus metrics endpoint
installed", "Tenant middleware installed", "A2A Protocol enabled").

That is noisy but SAFE, and the distinction was checked rather than assumed:
zero uvicorn/listening lines in the whole log, and the only relevant ports open
on the host (8111, 8199) are owned by ``wslrelay``, i.e. the fleet's own
relay — not the build. So the analysis CONSTRUCTS app objects and never SERVES.

Keep the imports lazy anyway: they are what makes the module importable, and
therefore testable, without standing up half the platform. The freeze cost is
PyInstaller's to pay; a developer running ``python -c "import daemon_entry"``
should not pay it.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Callable


def _port(explicit: int) -> int:
    """Flag, then AITHER_PORT, then 0 meaning 'let the module decide'.

    Never a literal in this file: two copies of a default is one drifting from
    the other, and the launcher is the thing that legitimately chooses.
    """
    if explicit:
        return explicit
    env = os.environ.get("AITHER_PORT", "").strip()
    return int(env) if env.isdigit() else 0


def _host(explicit: str) -> str:
    """Loopback unless told otherwise. awdk's own defaults are 0.0.0.0
    (config.py:275, daemon.py:72) and daemon.py:15 records that default as how
    an unauthenticated port reaches a tunnel."""
    return explicit or os.environ.get("AITHER_HOST", "").strip() or "127.0.0.1"


def _cmd_harness_serve(args: argparse.Namespace) -> int:
    """`harness serve` — the AitherShell daemon the launcher starts for awsh."""
    from adk.harnesses.daemon import resolve_token, serve

    # resolve_token is the module's OWN resolution (explicit -> file -> minted).
    # Passing token="" instead would hand the daemon an empty credential, which
    # it accepts and then rejects every caller against — a daemon that starts
    # and refuses everyone.
    return serve(host=_host(args.host), port=_port(args.port),
                 token=resolve_token(args.token or ""))


def _cmd_up(args: argparse.Namespace) -> int:
    """`up` — the agent server. adk/server.py owns its own arg handling, so the
    argv is handed back to it untouched rather than re-parsed here."""
    from adk.server import main as server_main

    sys.argv = [sys.argv[0]] + list(args.rest or [])
    result = server_main()
    return int(result) if isinstance(result, int) else 0


def build_parser(extra: Callable[[Any], None] | None = None) -> argparse.ArgumentParser:
    """The daemons' argv contract, optionally EXTENDED by the caller.

    `extra` is handed the subparsers action so a downstream entry point can add
    a verb without this file importing it. That direction is not stylistic: this
    tree is public-mirrored and `tools/check_public_paths.py awdk` forbids
    private product paths here, so the only way a product-side daemon can share
    this parser is for the product to reach IN. The first caller adds a
    `saga serve` verb and reuses `_host`/`_port`, so its host/port precedence
    cannot drift from the two verbs the launcher already spawns.
    """
    ap = argparse.ArgumentParser(
        # `awdaemons`, matching what build_executable.py actually names this
        # freeze (its `--narrow` output name) and the aw-family rule it cites
        # (.claude/rules/aw-family.md) — every name in the family reads as
        # "Aither World <thing>". This said `aither` while the binary on disk
        # was `awdaemons`, so `awdaemons --help` announced a different program
        # than the one you ran. Usage text is the only identity a frozen binary
        # can show, and the wide build genuinely IS `aither` — two programs
        # claiming one name is how you debug the wrong one.
        prog="awdaemons",
        description="AitherOS daemons (narrow build: harness serve, up).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    harness = sub.add_parser("harness", help="AitherShell harness daemon")
    hsub = harness.add_subparsers(dest="harness_cmd", required=True)
    hserve = hsub.add_parser("serve", help="run the harness daemon")
    hserve.add_argument("--host", default="")
    hserve.add_argument("--port", type=int, default=0)
    hserve.add_argument("--token", default="")
    hserve.set_defaults(func=_cmd_harness_serve)

    up = sub.add_parser("up", help="run the agent server")
    up.add_argument("rest", nargs=argparse.REMAINDER)
    up.set_defaults(func=_cmd_up)

    if extra is not None:
        extra(sub)
    return ap


def main(argv: list[str] | None = None,
         extra: Callable[[Any], None] | None = None) -> int:
    args = build_parser(extra).parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
