"""Markdown parser: frontmatter + heading tree + links + agent/standard awareness.

The corpus is majority Markdown, so this is the workhorse. It avoids a hard YAML
dependency with a minimal frontmatter reader (falls back to PyYAML if present).
"""

from __future__ import annotations

import posixpath
import re

from adk.graph_rag.config import NodeType
from adk.graph_rag.parsers import ParsedEdge, ParsedNode, ParseResult, file_key, slugify
from adk.graph_rag.parsers.symbol_extractor import symbol_fragments

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_MD_LINK = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
_REF_DIRECTIVE = re.compile(r"\$ref:\s*([^\s)`'\"]+)")
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SECTION_MAX = 1400


def _agent_name(rel_path: str) -> str | None:
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[0] == "agents":
        return parts[1]
    return None


def _standard_name(rel_path: str) -> str | None:
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == "standards" and rel_path.endswith(".md"):
        return parts[-1][:-3]
    return None


def _normalize_target(rel_path: str, target: str) -> str | None:
    """Resolve a relative link/ref target to a corpus-relative posix path.

    Returns None for external (http/mailto) or absolute targets.
    """
    target = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not target or "://" in target or target.startswith(("mailto:", "#", "/")):
        return None
    base = posixpath.dirname(rel_path)
    resolved = posixpath.normpath(posixpath.join(base, target))
    if resolved.startswith(".."):
        return None
    return resolved


def _frontmatter_refs(block: str) -> list[str]:
    """Pull ref-like targets out of a frontmatter block (best-effort, no YAML dep)."""
    refs: list[str] = []
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(block) or {}
        if isinstance(data, dict):
            for key in ("$ref", "ref", "see_also", "refs", "related", "extends"):
                val = data.get(key)
                if isinstance(val, str):
                    refs.append(val)
                elif isinstance(val, list):
                    refs.extend(str(v) for v in val)
        return refs
    except Exception:  # noqa: BLE001 — yaml absent or malformed; fall back to regex
        for m in re.finditer(r"(?:\$?ref|see_also|extends):\s*(.+)", block):
            chunk = m.group(1).strip().strip("[]")
            refs.extend(p.strip().strip("'\"") for p in chunk.split(",") if p.strip())
        return refs


def parse_markdown(rel_path: str, content: str) -> ParseResult:
    res = ParseResult()
    fkey = file_key(rel_path)
    res.nodes.append(ParsedNode(key=fkey, type=NodeType.FILE, content=rel_path,
                                title=rel_path, metadata={"language": "markdown"}))

    agent = _agent_name(rel_path)
    standard = _standard_name(rel_path)
    owner_key = fkey  # the node links hang off of
    if agent:
        akey = f"agent:{agent}"
        res.nodes.append(ParsedNode(key=akey, type=NodeType.AGENT, content=f"Agent {agent}",
                                    title=agent, metadata={"name": agent}))
        res.edges.append(ParsedEdge(src_key=akey, dst_key=fkey, relation="contains", weight=1.0))
        owner_key = akey
    if standard:
        skey = f"standard:{standard}"
        res.nodes.append(ParsedNode(key=skey, type=NodeType.STANDARD,
                                    content=f"Standard {standard}", title=standard,
                                    metadata={"name": standard}))
        res.edges.append(ParsedEdge(src_key=skey, dst_key=fkey, relation="contains", weight=1.0))

    body = content
    fm = _FRONTMATTER.match(content)
    if fm:
        for ref in _frontmatter_refs(fm.group(1)):
            tgt = _normalize_target(rel_path, ref)
            if tgt:
                res.edges.append(ParsedEdge(src_key=owner_key, dst_key=file_key(tgt),
                                            relation="references", weight=0.8))
        body = content[fm.end():]

    # Heading tree → section nodes (with nesting via a level stack).
    lines = body.splitlines()
    stack: list[tuple[int, str]] = []  # (heading_level, section_key)
    cur_key: str | None = None
    cur_level = 0
    buf: list[str] = []

    def flush(section_key: str | None, level: int, text_lines: list[str]) -> None:
        if section_key is None:
            return
        text = "\n".join(text_lines).strip()
        # find the parent (nearest shallower heading on the stack)
        parent = owner_key
        for lvl, k in reversed(stack):
            if lvl < level:
                parent = k
                break
        heading = section_key.split("#", 1)[-1]
        res.nodes.append(ParsedNode(
            key=section_key, type=NodeType.SECTION,
            content=(heading.replace("-", " ") + "\n" + text)[:_SECTION_MAX],
            title=heading, metadata={"file": rel_path, "level": level},
        ))
        res.edges.append(ParsedEdge(src_key=parent, dst_key=section_key,
                                    relation="contains", weight=1.0))
        # link references + symbols from this section's text
        for m in _MD_LINK.finditer(text):
            tgt = _normalize_target(rel_path, m.group(1))
            if tgt:
                res.edges.append(ParsedEdge(src_key=section_key, dst_key=file_key(tgt),
                                            relation="references", weight=0.7))
        for m in _REF_DIRECTIVE.finditer(text):
            tgt = _normalize_target(rel_path, m.group(1))
            if tgt:
                res.edges.append(ParsedEdge(src_key=section_key, dst_key=file_key(tgt),
                                            relation="references", weight=0.8))
        snodes, sedges = symbol_fragments(section_key, text)
        res.nodes.extend(snodes)
        res.edges.extend(sedges)

    for line in lines:
        m = _HEADING.match(line)
        if m:
            flush(cur_key, cur_level, buf)
            buf = []
            level = len(m.group(1))
            heading = m.group(2).strip() or "section"
            cur_key = f"section:{rel_path}#{slugify(heading)}"
            cur_level = level
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, cur_key))
        else:
            buf.append(line)
    flush(cur_key, cur_level, buf)

    # Whole-file links (catch links outside any heading / preamble) + handoff_to.
    for m in _MD_LINK.finditer(body):
        tgt = _normalize_target(rel_path, m.group(1))
        if not tgt:
            continue
        res.edges.append(ParsedEdge(src_key=owner_key, dst_key=file_key(tgt),
                                    relation="references", weight=0.6))
        other = _agent_name(tgt)
        if agent and other and other != agent and rel_path.endswith("handoff.md"):
            res.edges.append(ParsedEdge(src_key=f"agent:{agent}", dst_key=f"agent:{other}",
                                        relation="handoff_to", weight=0.7))
        std = _standard_name(tgt)
        if agent and std:
            res.edges.append(ParsedEdge(src_key=f"agent:{agent}", dst_key=f"standard:{std}",
                                        relation="implements_standard", weight=0.8))

    return res
