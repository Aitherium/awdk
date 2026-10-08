"""`adk storage ...` -- pass-through to the awstorage CLI.

awstorage is its own brick (stdlib-only; scan, inventory, diff, propose, apply
with a reversible quarantine, and -- from 0.3.0 -- the per-file index behind
`adk storage files ...`). adk does not re-implement any of it: this module
forwards the remainder of the command line to `awstorage.cli:main` so an agent
running on any node can inventory the disk it sits on with the same tool a
human uses. If the package is not installed -- or is too old for the verb asked
for -- say so and how to fix it; never degrade into a half-implementation that
looks like the real one.
"""

from __future__ import annotations

import sys

#: Verbs that arrived after awstorage 0.1.0, and the version that ships them.
_NEEDS = {"sweep": "0.2.0", "audit": "0.2.0", "harvest": "0.2.0",
          "files": "0.3.0", "whoami": "0.3.0", "manage": "0.3.0",
          "relocate": "0.5.0"}


def _version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def main(argv: list[str]) -> int:
    if argv[:1] == ["share"]:
        # Lending disk to the family's mesh pool is adk's own (adk.storage_contribution),
        # not the inventory brick's: handled here, before the pass-through.
        from adk.storage_share_cli import main as _share_main
        return int(_share_main(argv[1:]))
    try:
        from awstorage.cli import main as _awstorage_main
    except ImportError:
        # Asking for help is not an error: say how to get the brick, exit 0.
        asked_help = not argv or argv[0] in ("-h", "--help")
        print(
            "adk storage: the `awstorage` package is not installed.\n"
            "  pip install 'awdk[storage]'   or   pip install awstorage\n"
            "Then: adk storage scan <root> --catalog inventory.db",
            file=sys.stdout if asked_help else sys.stderr,
        )
        return 0 if asked_help else 2
    if not argv:
        argv = ["--help"]
    need = _NEEDS.get(argv[0])
    if need:
        have = str(getattr(sys.modules.get("awstorage"), "__version__", "0"))
        if _version_tuple(have) < _version_tuple(need):
            print(
                f"adk storage {argv[0]}: needs awstorage>={need}; this node has {have}.\n"
                f"  pip install -U 'awstorage>={need}'   (or: pip install -U 'awdk[storage]')",
                file=sys.stderr,
            )
            return 2
    return int(_awstorage_main(argv))
