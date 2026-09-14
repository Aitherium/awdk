#!/usr/bin/env python3
"""Generate docs/CLI-REFERENCE.md from the REAL parser.

WHY THIS IS GENERATED AND NOT WRITTEN
=====================================
Measured 2026-08-22: **57 of 95** top-level `adk` subcommands were named in no
public document — not the README, not QUICKSTART_SELF_HOSTED.md, not `llms.txt`
(the file agents read to learn this package), not anything under docs/. Sixty
percent of the CLI could not be found by anyone who had not read cli.py.

That is not a small thing. `adk gobbonet` was one of them, and while it sat
undiscoverable, people in the project chat asked for a cross-platform
one-command launcher and were pointed at a third-party fork that needs
compiling and a hand-edited config.toml. Nobody withheld the command. It could
not be found.

The obvious fix — write the missing 57 entries — is the wrong one, and would be
worse than the gap. A hand-written list of 95 commands is stale the day someone
adds the 96th, and then the docs assert a CLI that does not exist, which is the
failure this repo keeps paying for (a doc citing a path that moved, a checker
naming a file that was renamed). It would also satisfy the EXT004 gate while
making the surface *look* documented forever.

So this reads `build_command_manifest()` — the SAME introspection AitherShell
already uses to register slash commands, verified to return exactly the 95
commands the parser registers, with no drift in either direction. The reference
and the shell's command list therefore come from one source and cannot disagree.

    python scripts/gen_cli_reference.py            # write the file
    python scripts/gen_cli_reference.py --check    # CI: regenerate and diff

`--check` is what makes this durable. Drift is not representable: you regenerate
or the gate is red.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
OUT = PKG / "docs" / "CLI-REFERENCE.md"

HEADER = """<!-- GENERATED FILE — DO NOT EDIT BY HAND.

Produced by `scripts/gen_cli_reference.py` from the live argparse tree, which is
the same introspection AitherShell uses for slash commands. Edit the parser in
`adk/cli.py` and regenerate:

    python scripts/gen_cli_reference.py

A hand edit here is reverted by the next run, and CI diffs this file against a
fresh generation.
-->

# `adk` command reference

Every top-level command, generated from the parser itself — so this cannot
describe a command that does not exist, and cannot omit one that does.

Run `adk <command> --help` for the authoritative, always-current detail.

"""


def _fmt_default(value: object) -> str:
    if value is None or value == "":
        return ""
    if value is True:
        return "`true`"
    if value is False:
        return "`false`"
    return f"`{value}`"


def _arg_rows(args: list) -> list[str]:
    rows: list[str] = []
    for a in args or []:
        if not isinstance(a, dict):
            continue
        flags = a.get("flags") or []
        # A positional has no flags; show its name so the row is not blank.
        label = ", ".join(f"`{f}`" for f in flags) if flags \
            else f"`<{a.get('name', '?')}>`"
        typ = a.get("type") or ""
        req = "yes" if a.get("required") else ""
        default = _fmt_default(a.get("default"))
        help_text = (a.get("help") or "").strip().replace("|", r"\|")
        rows.append(f"| {label} | {typ} | {req} | {default} | {help_text} |")
    return rows


def render(manifest: list) -> str:
    out = [HEADER]

    # Alphabetical, deliberately. Thematic grouping needs a hand-kept mapping,
    # which is a second thing to rot — the exact failure this file exists to
    # avoid. The README keeps its curated, opinionated tour; this is the index.
    cmds = sorted(manifest, key=lambda c: str(c.get("name", "")))

    out.append(f"**{len(cmds)} commands.**\n")
    out.append("| command | what it does |")
    out.append("|---|---|")
    for c in cmds:
        name = c.get("name", "")
        summary = (c.get("help") or "").strip().replace("|", r"\|")
        out.append(f"| [`adk {name}`](#adk-{name}) | {summary} |")
    out.append("")
    out.append("---")
    out.append("")

    for c in cmds:
        name = c.get("name", "")
        out.append(f"## `adk {name}`")
        out.append("")
        help_text = (c.get("help") or "").strip()
        if help_text:
            out.append(help_text)
            out.append("")

        subs = c.get("subcommands") or []
        if subs:
            out.append("**Subcommands**")
            out.append("")
            for s in subs:
                if isinstance(s, dict):
                    sname = s.get("name", "")
                    shelp = (s.get("help") or "").strip().replace("|", r"\|")
                    out.append(f"- `adk {name} {sname}`"
                               + (f" — {shelp}" if shelp else ""))
                else:
                    out.append(f"- `adk {name} {s}`")
            out.append("")

        rows = _arg_rows(c.get("args") or [])
        if rows:
            out.append("| option | type | required | default | description |")
            out.append("|---|---|---|---|---|")
            out.extend(rows)
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def build() -> str:
    # Silence the package's own start-up logging so --check produces a clean
    # diff rather than a diff plus a wall of INFO lines.
    import logging
    logging.disable(logging.CRITICAL)
    try:
        from adk.cli import build_command_manifest
    except ImportError as exc:  # pragma: no cover - environment problem
        print(f"cannot import adk.cli: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    manifest = build_command_manifest()
    if not manifest:
        # An empty manifest would render a reference documenting nothing and
        # pass every check. Never write that.
        print("build_command_manifest() returned nothing — refusing to write "
              "a reference that documents no commands", file=sys.stderr)
        raise SystemExit(2)
    return render(manifest)


def self_test() -> int:
    """Prove the gate can still fail, and that it is not rendering nothing."""
    ok = True

    def chk(label: str, cond: bool) -> None:
        nonlocal ok
        if not cond:
            ok = False
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")

    import logging
    logging.disable(logging.CRITICAL)
    from adk.cli import build_command_manifest

    manifest = build_command_manifest()
    # NOT merely non-empty. A manifest that regressed to a handful of
    # commands would render a tiny reference, and regenerating would bake the
    # loss in -- `--check` would then agree with the smaller file. The floor
    # is what stops this tool certifying its own degraded output.
    chk(f'the parser yields a real CLI ({len(manifest)} commands)',
        len(manifest) > 20)
    chk('every command carries a name',
        all(str(c.get('name') or '').strip() for c in manifest))

    rendered = render(manifest)
    chk('the reference names every command',
        all(f"## `adk {c['name']}`" in rendered for c in manifest))
    chk('gobbonet is present (the command this file was written for)',
        '## `adk gobbonet`' in rendered)
    chk('the generated header warns against hand edits',
        'DO NOT EDIT BY HAND' in rendered)

    # THE ARM THAT MATTERS: a changed parser must produce a different file,
    # or --check can never go red and this is decoration.
    mutated = [dict(c) for c in manifest]
    mutated.append({'name': 'a-command-that-does-not-exist',
                    'help': 'planted by the self-test', 'args': []})
    chk('a new command changes the output (so --check can detect drift)',
        render(mutated) != rendered)

    # And a REMOVED command must change it too -- the shrink direction, which
    # is how documentation quietly loses commands.
    shrunk = [c for c in manifest if c.get('name') != 'gobbonet']
    chk('a removed command changes the output too',
        render(shrunk) != rendered)

    print()
    print('self-test: PASS' if ok else 'self-test: FAIL')
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed file differs from a fresh run")
    ap.add_argument("--self-test", action="store_true",
                    help="prove the check can still fail")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    fresh = build()

    if args.check:
        if not OUT.is_file():
            print(f"{OUT} does not exist — run: python scripts/gen_cli_reference.py",
                  file=sys.stderr)
            return 1
        current = OUT.read_text(encoding="utf-8")
        if current == fresh:
            n = fresh.count("\n## `adk ")
            print(f"CLI reference is current ({n} commands)")
            return 0
        diff = list(difflib.unified_diff(
            current.splitlines(), fresh.splitlines(),
            fromfile="committed", tofile="freshly generated", lineterm=""))
        print("CLI reference is STALE against the parser. A command was added, "
              "removed or re-described and the docs were not regenerated — which "
              "is how 57 of 95 commands became undiscoverable in the first place.",
              file=sys.stderr)
        for line in diff[:40]:
            print(line, file=sys.stderr)
        if len(diff) > 40:
            print(f"... {len(diff) - 40} more lines", file=sys.stderr)
        print("\nFix: python scripts/gen_cli_reference.py", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(fresh, encoding="utf-8")
    n = fresh.count("\n## `adk ")
    print(f"wrote {OUT.relative_to(PKG)} ({n} commands)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
