"""Prompt patterns: a catalog of reusable system prompts, one directory each.

A *pattern* is a directory holding a ``system.md`` (the system prompt) and
optionally a ``user.md`` (text prepended to the input) and a ``README.md``.
That on-disk shape is the one Fabric (https://github.com/danielmiessler/Fabric,
MIT) uses for its ``data/patterns/`` tree, chosen deliberately so a local Fabric
clone is directly importable with ``adk patterns import <clone>``. Nothing from
Fabric ships in this package; the bundled patterns under
``adk/packs/aither/patterns`` are our own.

Discovery order (first directory holding a name wins):

1. ``AITHER_PATTERN_DIRS`` — ``os.pathsep``-separated list of directories
2. ``~/.aither/patterns`` — where ``import_patterns`` writes
3. the bundled ``adk/packs/aither/patterns``

Usage::

    from adk.patterns import get_pattern, run_pattern
    p = get_pattern("summarize")
    text = await run_pattern("summarize", open("notes.md").read())

CLI: ``adk patterns list | show <name> | run <name> [--in FILE] | import <path>``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

SYSTEM_FILE = "system.md"
USER_FILE = "user.md"
README_FILE = "README.md"
PROVENANCE_FILE = "PROVENANCE.txt"
ENV_DIRS = "AITHER_PATTERN_DIRS"

_BUNDLED = Path(__file__).parent / "packs" / "aither" / "patterns"


@dataclass
class Pattern:
    """One loaded pattern.

    Attributes:
        name: Directory name, also the CLI handle.
        path: The pattern directory.
        system: Contents of ``system.md``.
        user: Contents of ``user.md`` (empty when absent). Prepended to the input.
        source: The search directory the pattern was discovered in.
    """

    name: str
    path: Path
    system: str
    user: str = ""
    source: str = ""


# ── discovery ───────────────────────────────────────────────────────────────


def user_pattern_dir() -> Path:
    """The per-user pattern directory (``~/.aither/patterns``)."""
    return Path.home() / ".aither" / "patterns"


def pattern_dirs() -> list[Path]:
    """Search directories in priority order, existing ones only.

    ``AITHER_PATTERN_DIRS`` entries first (in the order given), then the user
    directory, then the bundled patterns. Duplicates are dropped, keeping the
    first occurrence so precedence is preserved.
    """
    candidates: list[Path] = []
    for raw in os.environ.get(ENV_DIRS, "").split(os.pathsep):
        raw = raw.strip()
        if raw:
            candidates.append(Path(raw).expanduser())
    candidates.append(user_pattern_dir())
    candidates.append(_BUNDLED)

    seen: set[Path] = set()
    out: list[Path] = []
    for d in candidates:
        if not d.is_dir():
            continue
        key = d.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _load(pattern_dir: Path, source: Path) -> Pattern | None:
    system_path = pattern_dir / SYSTEM_FILE
    if not system_path.is_file():
        return None
    user_path = pattern_dir / USER_FILE
    return Pattern(
        name=pattern_dir.name,
        path=pattern_dir,
        system=system_path.read_text(encoding="utf-8"),
        user=user_path.read_text(encoding="utf-8") if user_path.is_file() else "",
        source=str(source),
    )


def list_patterns() -> list[Pattern]:
    """Every discoverable pattern, sorted by name. First search dir wins a name."""
    found: dict[str, Pattern] = {}
    for source in pattern_dirs():
        for child in sorted(source.iterdir()):
            if not child.is_dir() or child.name in found:
                continue
            p = _load(child, source)
            if p is not None:
                found[child.name] = p
    return [found[k] for k in sorted(found)]


def get_pattern(name: str) -> Pattern:
    """Load one pattern by name.

    Raises:
        KeyError: no search directory holds ``<name>/system.md``; the message
            lists the directories that were searched.
    """
    dirs = pattern_dirs()
    for source in dirs:
        p = _load(source / name, source)
        if p is not None:
            return p
    searched = ", ".join(str(d) for d in dirs) or "(no pattern directories exist)"
    raise KeyError(f"pattern {name!r} not found; searched: {searched}")


# ── running ─────────────────────────────────────────────────────────────────


def build_messages(pattern: Pattern, input_text: str) -> list[Any]:
    """The two-message conversation a pattern runs as."""
    from adk.llm import Message

    user = (pattern.user + "\n\n" if pattern.user.strip() else "") + input_text
    return [
        Message(role="system", content=pattern.system),
        Message(role="user", content=user),
    ]


async def run_pattern(
    name: str,
    input_text: str,
    *,
    model: str | None = None,
    router: Any = None,
) -> str:
    """Apply a pattern to ``input_text`` and return the model's text.

    Args:
        name: Pattern name (see :func:`list_patterns`).
        input_text: The text the pattern operates on.
        model: Optional model override passed to the router.
        router: Anything with ``async chat(messages, model=...)`` returning an
            object with ``.content``. Defaults to ``adk.llm.LLMRouter()``.

    Raises:
        KeyError: unknown pattern.
    """
    pattern = get_pattern(name)
    if router is None:
        from adk.llm import LLMRouter

        router = LLMRouter()
    response = await router.chat(build_messages(pattern, input_text), model=model)
    return getattr(response, "content", "") or ""


# ── importing ───────────────────────────────────────────────────────────────


def _resolve_import_root(src: Path) -> Path:
    # A Fabric clone keeps its catalog under data/patterns; accept the clone root.
    nested = src / "data" / "patterns"
    return nested if nested.is_dir() else src


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_patterns(
    src: Path,
    dest: Path | None = None,
    *,
    names: list[str] | None = None,
    overwrite: bool = False,
) -> list[str]:
    """Copy patterns from ``src`` into ``dest`` with a provenance record.

    Only ``system.md``, ``user.md`` and ``README.md`` are copied; anything
    else in a source pattern is ignored. Each imported pattern gets a
    ``PROVENANCE.txt`` naming the source path, the sha256 of ``system.md``
    and the import date.

    Args:
        src: A directory of ``<name>/system.md`` entries, or a Fabric clone
            root (``data/patterns`` is used automatically).
        dest: Target directory; defaults to ``~/.aither/patterns``.
        names: Restrict to these pattern names. Default: every pattern in src.
        overwrite: Replace an existing pattern in dest. Default refuses.

    Returns:
        The names imported, in order.

    Raises:
        FileNotFoundError: ``src`` is not a directory, or a requested name
            has no ``system.md`` there.
        FileExistsError: a pattern already exists in dest and ``overwrite``
            is false. Nothing is written for that name; earlier names in the
            same call stay imported.
    """
    src = Path(src).expanduser()
    if not src.is_dir():
        raise FileNotFoundError(f"pattern source is not a directory: {src}")
    root = _resolve_import_root(src)
    dest = Path(dest).expanduser() if dest is not None else user_pattern_dir()

    if names is None:
        names = sorted(
            c.name for c in root.iterdir() if c.is_dir() and (c / SYSTEM_FILE).is_file()
        )

    imported: list[str] = []
    for name in names:
        src_dir = root / name
        if not (src_dir / SYSTEM_FILE).is_file():
            raise FileNotFoundError(f"no {SYSTEM_FILE} under {src_dir}")
        dst_dir = dest / name
        if dst_dir.exists():
            if not overwrite:
                raise FileExistsError(
                    f"pattern {name!r} already exists at {dst_dir}; pass overwrite=True"
                )
            shutil.rmtree(dst_dir)
        dst_dir.mkdir(parents=True)
        for fname in (SYSTEM_FILE, USER_FILE, README_FILE):
            f = src_dir / fname
            if f.is_file():
                shutil.copyfile(f, dst_dir / fname)
        (dst_dir / PROVENANCE_FILE).write_text(
            f"source: {src_dir.resolve()}\n"
            f"system_sha256: {_sha256(src_dir / SYSTEM_FILE)}\n"
            f"imported: {date.today().isoformat()}\n",
            encoding="utf-8",
        )
        imported.append(name)
    return imported


# ── CLI ─────────────────────────────────────────────────────────────────────


def add_patterns_parser(sub: Any) -> None:
    """Register ``adk patterns`` on an ``add_subparsers`` result."""
    p = sub.add_parser("patterns", help="Prompt patterns: list, show, run, import")
    ps = p.add_subparsers(dest="patterns_command")

    ps.add_parser("list", help="List discoverable patterns and where each comes from")

    show = ps.add_parser("show", help="Print a pattern's system prompt")
    show.add_argument("name")

    run = ps.add_parser("run", help="Apply a pattern to text (stdin unless --in)")
    run.add_argument("name")
    run.add_argument("--in", dest="input_file", metavar="FILE", help="Read input from FILE")
    run.add_argument("--model", metavar="M", help="Model override for the router")

    imp = ps.add_parser("import", help="Copy patterns from a directory or a Fabric clone")
    imp.add_argument("path")
    imp.add_argument("--names", metavar="a,b", help="Only these patterns (comma-separated)")
    imp.add_argument("--dest", metavar="DIR", help="Target directory (default ~/.aither/patterns)")
    imp.add_argument("--overwrite", action="store_true", help="Replace existing patterns")


def cmd_patterns(args: argparse.Namespace) -> int:
    """Dispatch ``adk patterns <subcommand>``. Returns the process exit code."""
    sub = getattr(args, "patterns_command", None)

    if sub == "list":
        pats = list_patterns()
        if not pats:
            print("no patterns found; searched: " + ", ".join(map(str, pattern_dirs())))
            return 1
        width = max(len(p.name) for p in pats)
        for p in pats:
            print(f"{p.name:<{width}}  {p.source}")
        return 0

    if sub == "show":
        try:
            p = get_pattern(args.name)
        except KeyError as e:
            print(e.args[0], file=sys.stderr)
            return 1
        print(p.system, end="" if p.system.endswith("\n") else "\n")
        if p.user:
            print(f"\n--- {USER_FILE} ---\n{p.user}", end="")
        return 0

    if sub == "run":
        import asyncio

        if args.input_file:
            text = Path(args.input_file).read_text(encoding="utf-8")
        else:
            text = sys.stdin.read()
        if not text.strip():
            print("no input: pass --in FILE or pipe text on stdin", file=sys.stderr)
            return 2
        try:
            out = asyncio.run(run_pattern(args.name, text, model=args.model))
        except KeyError as e:
            print(e.args[0], file=sys.stderr)
            return 1
        print(out, end="" if out.endswith("\n") else "\n")
        return 0

    if sub == "import":
        names = [n.strip() for n in args.names.split(",") if n.strip()] if args.names else None
        dest = Path(args.dest) if args.dest else None
        try:
            done = import_patterns(Path(args.path), dest, names=names, overwrite=args.overwrite)
        except (FileNotFoundError, FileExistsError) as e:
            print(str(e), file=sys.stderr)
            return 1
        target = dest or user_pattern_dir()
        for n in done:
            print(f"imported {n} -> {target / n}")
        print(f"{len(done)} pattern(s) imported")
        return 0

    print("usage: adk patterns {list,show,run,import}", file=sys.stderr)
    return 2


__all__ = [
    "Pattern",
    "pattern_dirs",
    "list_patterns",
    "get_pattern",
    "build_messages",
    "run_pattern",
    "import_patterns",
    "add_patterns_parser",
    "cmd_patterns",
]
