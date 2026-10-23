"""Skills and slash commands the harness daemon can hand a session, read from disk.

WHY THIS EXISTS (2026-09-21)
---------------------------------------------------------------------------
The owner's skills (``.claude/skills/<name>/SKILL.md``) and slash commands
(``.claude/commands/<name>.md``) were readable by exactly one program: Claude
Code. Every other front door -- awsh, the desk Console, an ``adk shell new
--harness aither`` session -- had no way to name one. The daemon runs on the
HOST, so it can read those files directly, and a session already carries the
field the text belongs in (``SessionConfig.system_prompt_append``, which the
relay forwards as Genesis ``system_additions``). This module is the reader.

It is stdlib-only and knows nothing about the fleet: ``.claude/`` is a public
Claude Code convention, not an internal identifier, and a stranger's box with
its own skills dir gets the same behaviour.

Discovery order is deliberate: the working directory's ``.claude`` wins over
``~/.claude`` on a name collision, because a project-local skill is the more
specific claim. Collisions are reported, never silently shadowed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

#: What a rendered skill substitutes for the command's argument slot. This is
#: the Claude Code convention (``$ARGUMENTS``), kept verbatim so a command
#: file needs no edits to work here.
ARGUMENTS_TOKEN = "$ARGUMENTS"

#: A skill's ``context:`` frontmatter: ``inline`` (its text joins the session)
#: or ``fork`` (it is meant to run as a separate sub-agent with its own context).
SKILL_CONTEXTS = ("inline", "fork")


@dataclass(frozen=True)
class LocalSkill:
    """One skill or command as found on disk."""

    name: str
    kind: str  # "skill" | "command"
    path: Path
    description: str
    root: Path
    context: str = "inline"  # one of SKILL_CONTEXTS
    when_to_use: str = ""
    argument_hint: str = ""

    def describe(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "path": str(self.path),
            "context": self.context,
            "when_to_use": self.when_to_use,
            "argument_hint": self.argument_hint,
        }


def default_roots(cwd: str | os.PathLike[str] | None = None) -> list[Path]:
    """``<cwd>/.claude`` then ``~/.claude`` -- the first root wins a name."""
    roots: list[Path] = []
    if cwd:
        roots.append(Path(cwd).resolve() / ".claude")
    home = Path(os.environ.get("AITHER_HOME_OVERRIDE", "") or Path.home())
    roots.append(home / ".claude")
    return roots


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter fields, body) for a ``---``-fenced markdown file.

    A file with no fence returns ``({}, text)``. Only ``key: value`` lines are
    read; nested YAML is left alone (a description is all this needs).
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head = text[3:end]
    body = text[end + 4:].lstrip("\r\n")
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if ":" not in line or line.startswith((" ", "\t", "#")):
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("'\"")
    return meta, body


def _context_of(meta: dict[str, str]) -> str:
    """``fork`` only when the frontmatter says so; anything else is ``inline``."""
    return "fork" if meta.get("context", "").strip().lower() == "fork" else "inline"


def _meta_first(meta: dict[str, str], *keys: str) -> str:
    for key in keys:
        if meta.get(key):
            return meta[key]
    return ""


def _describe_from(text: str) -> str:
    meta, body = split_frontmatter(text)
    if meta.get("description"):
        return meta["description"]
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:200]
    return ""


def discover(
    roots: Iterable[Path] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, LocalSkill]:
    """Every skill and command under the roots, keyed by name.

    ``skills/<name>/SKILL.md`` -> kind ``skill``; ``commands/<name>.md`` -> kind
    ``command``. The first root that names a skill keeps it; a later collision
    is dropped here and reported by :func:`collisions`.
    """
    found: dict[str, LocalSkill] = {}
    for root in list(roots) if roots is not None else default_roots(cwd):
        root = Path(root)
        for skill_md in sorted((root / "skills").glob("*/SKILL.md")):
            name = skill_md.parent.name
            if name in found:
                continue
            found[name] = _load(name, "skill", skill_md, root)
        for cmd_md in sorted((root / "commands").glob("*.md")):
            name = cmd_md.stem
            if name in found:
                continue
            found[name] = _load(name, "command", cmd_md, root)
    return found


def collisions(
    roots: Iterable[Path] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, list[Path]]:
    """Names defined in more than one root, each with every path that claims it."""
    seen: dict[str, list[Path]] = {}
    for root in list(roots) if roots is not None else default_roots(cwd):
        root = Path(root)
        for skill_md in (root / "skills").glob("*/SKILL.md"):
            seen.setdefault(skill_md.parent.name, []).append(skill_md)
        for cmd_md in (root / "commands").glob("*.md"):
            seen.setdefault(cmd_md.stem, []).append(cmd_md)
    return {name: paths for name, paths in seen.items() if len(paths) > 1}


def _safe_describe(path: Path) -> str:
    try:
        return _describe_from(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _load(name: str, kind: str, path: Path, root: Path) -> LocalSkill:
    """One skill with the frontmatter fields every front door can act on."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    meta, _ = split_frontmatter(text)
    return LocalSkill(
        name=name, kind=kind, path=path, description=_describe_from(text), root=root,
        context=_context_of(meta),
        when_to_use=_meta_first(meta, "when_to_use", "when-to-use"),
        argument_hint=_meta_first(meta, "argument-hint", "argument_hint"),
    )


def render(skill: LocalSkill, arguments: str = "") -> str:
    """The text a session is handed: frontmatter stripped, arguments substituted.

    The frontmatter (``allowed-tools``, ``model``, …) means something only to
    Claude Code and is dropped; the body is the procedure and travels intact.
    A one-line header names the source so the model (and a transcript reader)
    can tell where the instructions came from. A ``context: fork`` skill was
    written to run as its own sub-agent; when it is handed to a session
    anyway, a note says so, so the model delegates it instead of silently
    running a fork-only procedure inside the conversation.
    """
    text = skill.path.read_text(encoding="utf-8", errors="replace")
    _, body = split_frontmatter(text)
    body = body.replace(ARGUMENTS_TOKEN, arguments or "")
    label = "Skill" if skill.kind == "skill" else "Command"
    note = ""
    if skill.context == "fork":
        note = (
            "> This skill declares `context: fork`: run it as a separate sub-agent "
            "or delegated task with its own context, then use its result.\n\n"
        )
    return f"# {label}: {skill.name}\n\n{note}{body.strip()}\n"


def resolve(
    name: str,
    arguments: str = "",
    roots: Iterable[Path] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> str:
    """Rendered text for ``name``; raises ``KeyError`` naming the known count."""
    found = discover(roots=roots, cwd=cwd)
    skill = found.get(name)
    if skill is None:
        raise KeyError(
            f"unknown skill {name!r}; {len(found)} known -- list them with "
            "`adk shell skills`"
        )
    return render(skill, arguments)


__all__ = [
    "ARGUMENTS_TOKEN",
    "LocalSkill",
    "SKILL_CONTEXTS",
    "collisions",
    "default_roots",
    "discover",
    "render",
    "resolve",
    "split_frontmatter",
]
