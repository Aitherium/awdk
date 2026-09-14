"""`adk storage ...` -- pass-through to the awstorage CLI.

awstorage is its own brick (stdlib-only; scan, inventory, diff, propose, apply
with a reversible quarantine). adk does not re-implement any of it: this module
forwards the remainder of the command line to `awstorage.cli:main` so an agent
running on any node can inventory the disk it sits on with the same tool a
human uses. If the package is not installed, say so and how to get it -- never
degrade into a half-implementation that looks like the real one.
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    try:
        from awstorage.cli import main as _awstorage_main
    except ImportError:
        print(
            "adk storage: the `awstorage` package is not installed.\n"
            "  pip install 'awdk[storage]'   or   pip install awstorage\n"
            "Then: adk storage scan <root> --catalog inventory.db",
            file=sys.stderr,
        )
        return 2
    if not argv:
        argv = ["--help"]
    return int(_awstorage_main(argv))
