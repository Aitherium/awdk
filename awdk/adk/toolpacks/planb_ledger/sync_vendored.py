"""Plan B Ledger — copy the PURE modules to every consumer that needs them.

There is exactly one source of truth for the ledger merge rules
(``engine.py``) and the paper-sheet layout (``sheet_render.py``). Faces that
cannot import this package get a byte-identical vendored copy, produced HERE
and asserted by ``test_planb_vendored_in_sync``.

Why a generator plus an assertion, rather than trusting a one-time copy: an
artifact with no build step and no drift check is how a mirror silently rots
(the Awconnect bonsai-worker case). Copies made by hand diverge; copies
made by a script that nothing re-runs diverge more quietly.

Run: python adk/toolpacks/planb_ledger/sync_vendored.py [--check] [--target DIR[:prefix]]
``--check`` exits 1 on drift and writes nothing — that is the CI shape.

``--target DIR[:prefix]`` adds a consumer OUTSIDE the monorepo (e.g. a
tenant backend repo) without hardcoding its machine-local path into
``TARGETS`` — a monorepo TARGETS entry pointing at another repo's checkout
would print "SKIP (absent)" on this repo's Linux CI, which is the
vacuous-pass trap this script exists to prevent. A ``--target`` dir that is
absent is a FAILURE (exit 1), never a skip: the calling repo's CI created
the dir by checking itself out, so an absent dir there means the caller is
checking the wrong path.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parent
REPO = PACK.parents[3]

PURE = ["engine.py", "sheet_render.py"]

# consumer dir -> filename prefix (backend keeps a flat module namespace)
TARGETS = {
    REPO / "AitherOS" / "apps" / "packages" / "awkit-backend": "planb_",
}

CLI_PREFIX = "planb_"


def _cli_targets(raw: list[str]) -> dict[Path, str]:
    """DIR[:prefix] specs -> {Path: prefix}. Missing prefix defaults to planb_.

    The colon is a prefix separator ONLY when the tail is a bare prefix name.
    A Windows drive path (``C:\\Users\\...``) rpartitions on the drive colon,
    which would turn the dir into ``C`` — measured live on 2026-08-25, and
    exactly the MSYS-style mangling this script's targets are meant to serve.
    """
    out: dict[Path, str] = {}
    for spec in raw:
        dir_part, prefix = spec, CLI_PREFIX
        if ":" in spec:
            head, _, tail = spec.rpartition(":")
            if tail and "/" not in tail and "\\" not in tail:
                dir_part, prefix = head, tail
        out[Path(dir_part).resolve()] = prefix
    return out

HEADER = (
    "# GENERATED — do not edit. Source of truth:\n"
    "#   awdk/adk/toolpacks/planb_ledger/{name}\n"
    "# Regenerate: python adk/toolpacks/planb_ledger/sync_vendored.py\n"
    "# Drift is asserted by test_planb_vendored_in_sync.\n"
)


def rendered(name: str) -> str:
    body = (PACK / name).read_text(encoding="utf-8")
    # The vendored engine is a FLAT module (`planb_engine`). Consumers import it
    # either as a package member or flat, exactly like the rest of the backend,
    # so emit the same dual-import the house uses.
    body = body.replace(
        "from . import engine as ledger",
        "try:  # package import (deployed) / flat import (standalone app image)\n"
        "    from portal_kit_backend import planb_engine as ledger\n"
        "except ImportError:  # pragma: no cover\n"
        "    import planb_engine as ledger",
    )
    return HEADER.format(name=name) + body


def sync(check: bool = False, cli_targets: dict[Path, str] | None = None) -> int:
    drift = 0
    cli_targets = cli_targets or {}
    targets = {**TARGETS, **cli_targets}
    for target_dir, prefix in targets.items():
        if not target_dir.is_dir():
            # A monorepo TARGETS entry can legitimately be absent on a given
            # checkout (the awkit/portal-kit rename straddles two spellings);
            # a --target from another repo's CI cannot — that repo checked
            # itself out, so an absent dir is a wrong path, not a skip.
            if target_dir in cli_targets:
                print(f"ABSENT (cli target — FAIL): {target_dir}")
                return 1
            print(f"SKIP (absent): {target_dir}")
            continue
        for name in PURE:
            dest = target_dir / f"{prefix}{name}"
            want = rendered(name)
            have = dest.read_text(encoding="utf-8") if dest.exists() else None
            if have == want:
                continue
            drift += 1
            if check:
                print(f"DRIFT: {dest} differs from canonical {name}")
            else:
                dest.write_text(want, encoding="utf-8")
                print(f"wrote {dest}")
    if check and drift:
        print(f"\n{drift} vendored file(s) out of sync — run sync_vendored.py")
        return 1
    if not check:
        print(f"in sync ({len(PURE)} pure module(s) x {len(targets)} consumer(s))")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 on drift, write nothing")
    parser.add_argument(
        "--target",
        action="append",
        metavar="DIR[:prefix]",
        help="additional consumer outside the monorepo (repeatable); absent dir = failure",
    )
    args = parser.parse_args()
    sys.exit(sync(check=args.check, cli_targets=_cli_targets(args.target or [])))
