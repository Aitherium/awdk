"""Reversible pack activation: everything a pack adds to an agent can be taken back.

WHY THIS EXISTS (2026-10-03)
---------------------------------------------------------------------------
``tool_pack_loader`` could load a pack onto an agent but never unload one: its
``register(registry)`` wrote tools straight into the agent's ToolRegistry,
persona fragments were copied in at construction, and nothing recorded which of
those writes came from which pack. Switching a pack off meant a process restart.
And the manifest's ``skills:`` field was parsed and read by NOTHING, so a pack
that shipped skills gave the agent none.

Packs have always been the unit of capability here; what they lacked was a way
back out. Every registration is now an *effect* owned by the pack that made it,
and deactivating the pack unwinds its effects. :func:`activate` records each effect on a :class:`PackActivation`
and :func:`deactivate` reverses them in LIFO order. That covers tools added,
tools a pack overwrote (their previous definition is restored), persona
fragments and skills. Skills follow progressive disclosure: the prompt carries
names and descriptions, and ``load_pack_skill(name)`` returns the body. That
tool is present only while at least one active pack contributes a skill.

Skill resolution (a manifest entry is a bare name or a relative path):
``<pack>/skills/<name>/SKILL.md``, ``<pack>/skills/<name>.md``, ``<pack>/<name>.md``,
then each dir in ``AITHER_SKILL_DIRS`` (os.pathsep-separated), ``~/.aither/skills``,
and the repo's ``awskills/skills`` (where in-tree packs point today). An
unresolved entry stays on the activation as ``missing`` and is never silently
dropped.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOAD_SKILL_TOOL = "load_pack_skill"


@dataclass
class PackSkill:
    name: str
    description: str
    path: Path
    pack_id: str

    def body(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")


@dataclass
class PackActivation:
    """The record of every effect one pack made on one agent."""
    pack_id: str
    tools_added: list[str] = field(default_factory=list)
    tools_replaced: dict[str, Any] = field(default_factory=dict)  # name -> prior ToolDef
    persona_added: list[str] = field(default_factory=list)
    skills_added: list[str] = field(default_factory=list)
    skills_missing: list[str] = field(default_factory=list)
    load_tool_added: bool = False

    def summary(self) -> dict:
        return {
            "pack": self.pack_id,
            "tools": sorted(self.tools_added + list(self.tools_replaced)),
            "persona_fragments": len(self.persona_added),
            "skills": list(self.skills_added),
            "skills_missing": list(self.skills_missing),
        }


def _skill_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    for d in os.environ.get("AITHER_SKILL_DIRS", "").split(os.pathsep):
        if d.strip():
            dirs.append(Path(d.strip()).expanduser())
    dirs.append(Path.home() / ".aither" / "skills")
    here = Path(__file__).resolve()
    for parent in here.parents[1:4]:
        cand = parent / "awskills" / "skills"
        if cand.is_dir():
            dirs.append(cand)
            break
    return dirs


_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _describe(path: Path, fallback: str) -> tuple[str, str]:
    """(name, description) from YAML frontmatter, else the first prose line."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fallback, ""
    name, desc = fallback, ""
    m = _FRONT.match(text)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip().lower(), v.strip().strip("\"'")
            if k == "name" and v:
                name = v
            elif k == "description" and v:
                desc = v
        text = text[m.end():]
    if not desc:
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                desc = s
                break
    return name, desc[:240]


def resolve_skill(entry: str, pack_dir: Path) -> Path | None:
    entry = entry.strip()
    if not entry:
        return None
    rel = entry.replace("\\", "/")
    if rel.startswith(("/", "~")) or ".." in rel.split("/"):
        return None  # a manifest may not reach outside its pack / the skill dirs
    stem = rel[:-3] if rel.endswith(".md") else rel
    cands = [pack_dir / rel] if rel.endswith(".md") else []
    cands += [pack_dir / "skills" / stem / "SKILL.md", pack_dir / "skills" / f"{stem}.md",
              pack_dir / f"{stem}.md"]
    for d in _skill_search_dirs():
        cands += [d / f"{stem}.md", d / stem / "SKILL.md"]
    for c in cands:
        if c.is_file():
            return c
    return None


def _registry(agent: Any):
    return (getattr(agent, "_tools", None) or getattr(agent, "tools", None)
            or getattr(agent, "tool_registry", None))


def _skills(agent: Any) -> dict[str, PackSkill]:
    if not isinstance(getattr(agent, "_pack_skills", None), dict):
        agent._pack_skills = {}
    return agent._pack_skills


def _persona(agent: Any) -> list[str]:
    if not isinstance(getattr(agent, "_pack_persona_fragments", None), list):
        agent._pack_persona_fragments = []
    return agent._pack_persona_fragments


def _activations(agent: Any) -> dict[str, PackActivation]:
    if not isinstance(getattr(agent, "_pack_activations", None), dict):
        agent._pack_activations = {}
    return agent._pack_activations


def _make_load_tool(agent: Any):
    def load_pack_skill(name: str) -> str:
        """Load the full instructions of a skill an active pack provides."""
        sk = _skills(agent).get(name)
        if sk is None:
            return f"No active pack skill named {name!r}. Active: {sorted(_skills(agent))}"
        try:
            return sk.body()
        except OSError as exc:
            return f"Skill {name!r} could not be read: {exc}"
    return load_pack_skill


def activate(agent: Any, manifest: Any, loader: Any) -> PackActivation:
    """Mount *manifest* on *agent*, recording every effect. Idempotent per pack id."""
    acts = _activations(agent)
    if manifest.id in acts:
        return acts[manifest.id]
    act = PackActivation(pack_id=manifest.id)
    reg = _registry(agent)
    before = dict(getattr(reg, "_tools", {})) if reg is not None else {}

    # Empty tool_modules is the scaffold default: the loader file-loads __init__.py.
    has_code = bool(manifest.tool_modules) or (Path(manifest.path) / "__init__.py").is_file()
    if reg is not None and has_code:
        loader.register_on_adk_agent(manifest, agent)
        after = getattr(reg, "_tools", {})
        for name, td in after.items():
            if name not in before:
                act.tools_added.append(name)
            elif before[name] is not td:
                act.tools_replaced[name] = before[name]

    persona = _persona(agent)
    for frag in manifest.persona_fragments:
        if frag and frag not in persona:
            persona.append(frag)
            act.persona_added.append(frag)

    skills = _skills(agent)
    for entry in manifest.skills:
        path = resolve_skill(entry, Path(manifest.path))
        if path is None:
            act.skills_missing.append(entry)
            continue
        name, desc = _describe(path, Path(entry).stem)
        if name in skills:  # first pack to claim a name keeps it
            logger.warning("pack %s: skill %r already provided by %s", manifest.id, name,
                           skills[name].pack_id)
            continue
        skills[name] = PackSkill(name=name, description=desc, path=path, pack_id=manifest.id)
        act.skills_added.append(name)
    if act.skills_missing:
        logger.warning("pack %s: skills not found: %s", manifest.id, act.skills_missing)

    if act.skills_added and reg is not None and reg.get(LOAD_SKILL_TOOL) is None:
        reg.register(_make_load_tool(agent), name=LOAD_SKILL_TOOL)
        act.load_tool_added = True

    acts[manifest.id] = act
    return act


def deactivate(agent: Any, pack_id: str) -> PackActivation | None:
    """Reverse every effect *pack_id*'s activation made. Returns it, or None."""
    act = _activations(agent).pop(pack_id, None)
    if act is None:
        return None
    reg = _registry(agent)
    skills = _skills(agent)
    for name in reversed(act.skills_added):
        if name in skills and skills[name].pack_id == pack_id:
            del skills[name]
    persona = _persona(agent)
    for frag in reversed(act.persona_added):
        if frag in persona:
            persona.remove(frag)
    if reg is not None:
        for name in reversed(act.tools_added):
            reg.unregister(name)
        for name, prior in act.tools_replaced.items():
            reg._tools[name] = prior
        if not skills and reg.get(LOAD_SKILL_TOOL) is not None:
            # the loader tool belongs to the SET of skill-bearing packs, not to one
            reg.unregister(LOAD_SKILL_TOOL)
            if LOAD_SKILL_TOOL in act.tools_added:
                act.tools_added.remove(LOAD_SKILL_TOOL)
    return act


def skills_prompt_block(agent: Any) -> str:
    skills = _skills(agent)
    if not skills:
        return ""
    lines = ["\n[PACK SKILLS] Call load_pack_skill(name) for the full instructions."]
    lines += [f"- {s.name}: {s.description}" for s in sorted(skills.values(), key=lambda s: s.name)]
    return "\n".join(lines)
