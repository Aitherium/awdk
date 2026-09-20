"""Delta-spec parser and living-spec merge for the specflow pack.

A delta spec (``openspec/changes/<id>/specs/<capability>/spec.md``) describes
how a capability's requirements change, in three sections::

    ## ADDED Requirements
    ### Requirement: <name>
    <requirement text>
    #### Scenario: <name>
    - **WHEN** <condition>
    - **THEN** <outcome>

    ## MODIFIED Requirements   (same block shape; replaces the block by name)
    ## REMOVED Requirements    (name only is enough)

A living spec (``openspec/specs/<capability>/spec.md``) is the accumulated
truth: ``# <capability>`` / ``## Purpose`` / ``## Requirements`` followed by
``### Requirement:`` blocks. Archiving a change merges its delta into it.

This module is deliberately pure: text in, text/structure out, no filesystem,
so the parser and the merge are unit-testable without the tool registry. The
format is the OpenSpec delta shape (https://github.com/Fission-AI/OpenSpec,
MIT), re-implemented here rather than vendored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SECTIONS: tuple[str, ...] = ("ADDED", "MODIFIED", "REMOVED")

_SECTION_RE = re.compile(r"^##\s+(ADDED|MODIFIED|REMOVED)\s+Requirements\s*$", re.IGNORECASE)
_REQ_RE = re.compile(r"^###\s+Requirement:\s*(.+?)\s*$")
_SCENARIO_RE = re.compile(r"^####\s+Scenario:\s*(.+?)\s*$")
_WHEN_RE = re.compile(r"^\s*[-*]\s+\*\*WHEN\*\*")
_THEN_RE = re.compile(r"^\s*[-*]\s+\*\*THEN\*\*")
_H2_RE = re.compile(r"^##\s+\S")
_H3_RE = re.compile(r"^###\s+\S")
_REQUIREMENTS_HEADING_RE = re.compile(r"^##\s+Requirements\s*$", re.IGNORECASE)


@dataclass
class Scenario:
    """One ``#### Scenario:`` under a requirement, with its WHEN/THEN presence."""
    name: str
    line: int                 # 1-based line of the heading
    has_when: bool = False
    has_then: bool = False


@dataclass
class RequirementBlock:
    """One ``### Requirement:`` block: heading through the line before the next
    ``##``/``###`` heading. ``lines`` has trailing blank lines stripped."""
    name: str
    line: int                 # 1-based line of the heading
    start: int                # 0-based index of the heading in the source lines
    end: int                  # 0-based exclusive end (includes trailing blanks)
    lines: list[str] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"


def _collect_blocks(lines: list[str], start: int, end: int) -> list[RequirementBlock]:
    """Requirement blocks between ``start`` and ``end`` (0-based, end exclusive).
    A block ends at the next ``##`` or ``###`` heading (``####`` scenarios stay in)."""
    blocks: list[RequirementBlock] = []
    i = start
    while i < end:
        m = _REQ_RE.match(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while j < end and not (_H2_RE.match(lines[j]) or _H3_RE.match(lines[j])):
            j += 1
        body = lines[i:j]
        while body and not body[-1].strip():
            body.pop()
        block = RequirementBlock(name=m.group(1), line=i + 1, start=i, end=j, lines=body)
        current: Scenario | None = None
        for k in range(i + 1, j):
            sm = _SCENARIO_RE.match(lines[k])
            if sm:
                current = Scenario(name=sm.group(1), line=k + 1)
                block.scenarios.append(current)
                continue
            if current is None:
                continue
            if _WHEN_RE.match(lines[k]):
                current.has_when = True
            elif _THEN_RE.match(lines[k]):
                current.has_then = True
        blocks.append(block)
        i = j
    return blocks


def parse_delta(text: str) -> dict[str, list[RequirementBlock]]:
    """Parse a delta spec into ``{"ADDED": [...], "MODIFIED": [...], "REMOVED": [...]}``.

    Every key is always present. A ``### Requirement:`` outside one of the three
    section headings is ignored (it belongs to no delta); a ``##`` heading that is
    not a section heading closes the current section.
    """
    lines = text.splitlines()
    out: dict[str, list[RequirementBlock]] = {s: [] for s in SECTIONS}
    i = 0
    n = len(lines)
    while i < n:
        m = _SECTION_RE.match(lines[i])
        if not m:
            i += 1
            continue
        section = m.group(1).upper()
        j = i + 1
        while j < n and not _H2_RE.match(lines[j]):
            j += 1
        out[section].extend(_collect_blocks(lines, i + 1, j))
        i = j
    return out


def parse_living(text: str) -> list[RequirementBlock]:
    """Every ``### Requirement:`` block in a living spec, in document order."""
    lines = text.splitlines()
    return _collect_blocks(lines, 0, len(lines))


def delta_purpose(text: str) -> str:
    """The body of a delta's optional ``## Purpose`` section (comments and blank
    lines stripped), used to seed a NEW living spec. Empty when absent."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^##\s+Purpose\s*$", ln, re.IGNORECASE):
            j = i + 1
            while j < len(lines) and not _H2_RE.match(lines[j]):
                j += 1
            body = "\n".join(lines[i + 1:j])
            body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
            return "\n".join(x for x in body.splitlines() if x.strip()).strip()
    return ""


def new_living_text(capability: str, purpose: str = "") -> str:
    """The skeleton of a living spec that does not exist yet."""
    purpose = purpose.strip() or f"Requirements for the {capability} capability."
    return f"# {capability}\n\n## Purpose\n\n{purpose}\n\n## Requirements\n"


def merge_into_living(
    living_text: str, delta: dict[str, list[RequirementBlock]],
) -> tuple[str, dict[str, int], list[str]]:
    """Apply a parsed delta to a living spec.

    ADDED appends the block (error if a requirement of that name already exists);
    MODIFIED replaces the block with the same name (error if absent); REMOVED
    deletes it (error if absent). The merge is all-or-nothing: on any error the
    original text is returned unchanged with zero counts, so a caller never
    half-applies a change.

    Returns ``(new_text, counts, errors)`` with ``counts`` keyed ``added`` /
    ``modified`` / ``removed``.
    """
    counts = {"added": 0, "modified": 0, "removed": 0}
    errors: list[str] = []
    lines = living_text.splitlines()
    existing = {b.name: b for b in _collect_blocks(lines, 0, len(lines))}

    seen: dict[str, str] = {}
    for section in SECTIONS:
        for b in delta.get(section, []):
            if b.name in seen:
                errors.append(
                    f"requirement '{b.name}' appears in both {seen[b.name]} and {section}")
            seen[b.name] = section
    for b in delta.get("ADDED", []):
        if b.name in existing:
            errors.append(
                f"ADDED requirement '{b.name}' already exists in the living spec; use MODIFIED")
    for section in ("MODIFIED", "REMOVED"):
        for b in delta.get(section, []):
            if b.name not in existing:
                errors.append(f"{section} requirement '{b.name}' not found in the living spec")
    if errors:
        return living_text, counts, errors

    # Splice from the bottom up so earlier indices stay valid.
    ops: list[tuple[int, int, list[str]]] = []
    for b in delta.get("MODIFIED", []):
        old = existing[b.name]
        ops.append((old.start, old.end, b.lines + [""]))
    for b in delta.get("REMOVED", []):
        old = existing[b.name]
        ops.append((old.start, old.end, []))
    for start, end, replacement in sorted(ops, key=lambda t: t[0], reverse=True):
        lines[start:end] = replacement

    added = delta.get("ADDED", [])
    if added and not any(_REQUIREMENTS_HEADING_RE.match(ln) for ln in lines):
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", "## Requirements"])
    for b in added:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append("")
        lines.extend(b.lines)

    counts["added"] = len(added)
    counts["modified"] = len(delta.get("MODIFIED", []))
    counts["removed"] = len(delta.get("REMOVED", []))
    new_text = "\n".join(lines).rstrip("\n") + "\n"
    return new_text, counts, errors
