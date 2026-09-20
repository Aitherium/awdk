"""Vectorless tree index over a Markdown document: heading tree, per-node LLM summaries,
retrieval by descending the tree on summary relevance (no embeddings, no vector store).
Idea adapted from PageIndex (VectifyAI/PageIndex, MIT, https://github.com/VectifyAI/PageIndex);
no code copied.

Entry points::

    root = build_tree(markdown)                       # heading tree, thinned
    root = await summarize_tree(root, markdown, router=LLMRouter())
    hits = await tree_search(root, markdown, "how is X configured?", router=router)
    root = await index_document("doc.md", router=router)   # sidecar-cached

``router=None`` is supported everywhere: summaries fall back to text excerpts and search
falls back to lexical overlap, so the index answers offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.graph_rag.page_index")

INDEX_VERSION = 1
SIDECAR_SUFFIX = ".pageindex.json"
ROOT_TITLE = "document"
OWN_SUFFIX = "-own"  # option id for "the parent's own text" in a search listing

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_ID_RE = re.compile(r"\b\d{4}(?:-own)?\b")
_JSON_LIST_RE = re.compile(r"\[[^\[\]]*\]", re.S)

_SUMMARY_SYSTEM = (
    "You summarize sections of a document for a table of contents. "
    "Reply with one or two plain sentences describing what the section covers. "
    "No preamble, no markdown."
)
_SEARCH_SYSTEM = (
    "You route a question to the sections of a document most likely to answer it. "
    "You are given candidate sections as `id: title -- summary` lines. "
    "Reply with ONLY a JSON array of section ids, most relevant first."
)


@dataclass
class Node:
    """One heading section. ``start_line``/``end_line`` are 1-based, inclusive, and the span
    covers the node's own text AND every descendant's."""

    node_id: str
    title: str
    level: int
    start_line: int
    end_line: int
    summary: str = ""
    children: list["Node"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "level": self.level,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "summary": self.summary,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Node":
        return cls(
            node_id=str(data["node_id"]),
            title=str(data.get("title", "")),
            level=int(data.get("level", 0)),
            start_line=int(data.get("start_line", 1)),
            end_line=int(data.get("end_line", 0)),
            summary=str(data.get("summary", "")),
            children=[cls.from_dict(c) for c in data.get("children", [])],
        )

    def walk(self):
        """Pre-order traversal (self first)."""
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, node_id: str) -> "Node | None":
        for node in self.walk():
            if node.node_id == node_id:
                return node
        return None


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _split_lines(markdown: str) -> list[str]:
    return markdown.splitlines()


def _scan_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return ``(line_no, level, title)`` for every ATX heading outside fenced code."""
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for idx, line in enumerate(lines, start=1):
        m_fence = _FENCE_RE.match(line)
        if m_fence:
            marker = m_fence.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            continue
        if fence is not None:
            continue
        m = _HEADING_RE.match(line)
        if m:
            found.append((idx, len(m.group(1)), m.group(2).strip()))
    return found


def _own_ranges(node: Node) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of the node's span NOT covered by a child.

    Usually one range (heading to first child); after thinning a merged section can sit
    between or after kept children, so the own text may be several gaps.
    """
    ranges: list[tuple[int, int]] = []
    cursor = node.start_line
    for child in node.children:
        if child.start_line > cursor:
            ranges.append((cursor, child.start_line - 1))
        cursor = child.end_line + 1
    if cursor <= node.end_line:
        ranges.append((cursor, node.end_line))
    return ranges


def node_text(node: Node, lines: list[str]) -> str:
    """Full text of the node's span (own text plus every descendant's)."""
    return "\n".join(lines[node.start_line - 1:node.end_line])


def node_own_text(node: Node, lines: list[str]) -> str:
    """The node's heading plus every line under it that no child covers (thinned sections
    included), joined in document order."""
    return "\n".join("\n".join(lines[a - 1:b]) for a, b in _own_ranges(node))


def _own_body(node: Node, lines: list[str]) -> str:
    """Own text without the heading line itself."""
    parts = node_own_text(node, lines).split("\n", 1)
    return parts[1] if len(parts) > 1 else ""


def _thin(node: Node, lines: list[str], min_node_chars: int) -> None:
    """Merge tiny childless sections into their parent, bottom-up.

    A child whose own text (heading excluded) is shorter than ``min_node_chars`` and that
    has no children of its own is dropped from ``children``; the parent's span already
    covers its lines, so nothing is lost -- the section simply stops being a node.
    """
    for child in node.children:
        _thin(child, lines, min_node_chars)
    kept: list[Node] = []
    for child in node.children:
        if not child.children and len(_own_body(child, lines).strip()) < min_node_chars:
            continue
        kept.append(child)
    node.children = kept


def _assign_ids(root: Node) -> None:
    for n, node in enumerate(root.walk()):
        node.node_id = f"{n:04d}"


def build_tree(markdown: str, *, min_node_chars: int = 200) -> Node:
    """Parse ATX headings into a nested tree, thin tiny leaves, assign document-order ids.

    A heading nests under the nearest preceding heading with a smaller level, so a ``###``
    directly after a ``#`` nests under the ``#``. Headings inside fenced code blocks are
    ignored. The root node (``0000``, title "document") spans the whole text.
    """
    lines = _split_lines(markdown)
    total = len(lines)
    root = Node(node_id="0000", title=ROOT_TITLE, level=0, start_line=1, end_line=total)

    stack: list[Node] = [root]
    for line_no, level, title in _scan_headings(lines):
        node = Node(node_id="", title=title, level=level, start_line=line_no, end_line=total)
        while stack[-1].level >= level:
            closed = stack.pop()
            closed.end_line = line_no - 1
        stack[-1].children.append(node)
        stack.append(node)
    # Everything still open runs to the end of the document (already end_line=total).

    _thin(root, lines, min_node_chars)
    _assign_ids(root)
    return root


# ---------------------------------------------------------------------------
# Summarizing
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[... truncated ...]"


def _excerpt(text: str, limit: int = 240) -> str:
    """Offline stand-in for a summary: the first non-heading, non-blank content."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return _truncate(stripped, limit)
    return ""


async def _ask(router: Any, system: str, user: str, model: str | None) -> str:
    """One chat turn through an ``LLMRouter``-shaped object; returns the content string."""
    from adk.llm.base import Message  # lazy: adk.llm pulls provider modules

    resp = await router.chat(
        [Message(role="system", content=system), Message(role="user", content=user)],
        model=model,
    )
    content = getattr(resp, "content", resp)
    return content if isinstance(content, str) else str(content)


async def summarize_tree(
    root: Node,
    markdown: str,
    *,
    router: Any = None,
    model: str | None = None,
    max_chars_per_node: int = 6000,
    concurrency: int = 4,
) -> Node:
    """Fill ``summary`` on every node, children before parents, ``concurrency`` calls at once.

    A leaf is summarized from its own text (truncated to ``max_chars_per_node``); a parent
    from the head of its own text plus its children's summaries. With ``router=None`` the
    summary is a text excerpt so the tree is still navigable offline. Mutates and returns
    ``root``.
    """
    lines = _split_lines(markdown)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def summarize(node: Node) -> None:
        await asyncio.gather(*(summarize(c) for c in node.children))
        own = node_own_text(node, lines)
        if node.children:
            head = _truncate(own, max_chars_per_node // 3)
            parts = [f"- {c.title}: {c.summary}" for c in node.children if c.summary]
            source = f"{head}\n\nSubsections:\n" + "\n".join(parts)
        else:
            source = _truncate(own, max_chars_per_node)
        if router is None:
            node.summary = _excerpt(source) or node.title
            return
        user = f"Section title: {node.title}\n\n{source}"
        async with sem:
            try:
                node.summary = (await _ask(router, _SUMMARY_SYSTEM, user, model)).strip()
            except Exception as exc:  # a dead backend must not lose the whole index
                logger.warning("summarize %s failed: %s", node.node_id, exc)
                node.summary = _excerpt(source) or node.title

    await summarize(root)
    return root


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def sha256_text(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def sidecar_path(doc_path: str | Path) -> Path:
    """``doc.md`` -> ``doc.pageindex.json`` beside it."""
    return Path(doc_path).with_suffix(SIDECAR_SUFFIX)


def save_index(root: Node, path: str | Path, *, source: str = "", sha256: str = "") -> Path:
    """Write the tree as JSON: ``{version, source, sha256, tree}``."""
    out = Path(path)
    payload = {
        "version": INDEX_VERSION,
        "source": source,
        "sha256": sha256,
        "tree": root.to_dict(),
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _read_sidecar(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_index(path: str | Path) -> Node:
    """Read a sidecar written by :func:`save_index`."""
    data = _read_sidecar(path)
    if int(data.get("version", 0)) != INDEX_VERSION:
        raise ValueError(f"unsupported page index version {data.get('version')!r}")
    return Node.from_dict(data["tree"])


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------

def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def lexical_score(query: str, text: str) -> float:
    """Share of query terms present in ``text`` plus a small frequency bonus (0..1)."""
    terms = set(_tokens(query))
    if not terms:
        return 0.0
    words = _tokens(text)
    if not words:
        return 0.0
    counts: dict[str, int] = {}
    for w in words:
        if w in terms:
            counts[w] = counts.get(w, 0) + 1
    if not counts:
        return 0.0
    coverage = len(counts) / len(terms)
    density = sum(counts.values()) / len(words)
    return min(1.0, 0.9 * coverage + 0.1 * min(1.0, density * 20))


def _parse_ids(raw: str, valid: set[str]) -> list[str]:
    """Pull an ordered list of known ids out of a model reply; empty when nothing parses."""
    picked: list[str] = []
    m = _JSON_LIST_RE.search(raw or "")
    if m:
        try:
            items = json.loads(m.group(0))
        except (ValueError, TypeError):
            items = []
        for item in items if isinstance(items, list) else []:
            sid = str(item.get("id", "") if isinstance(item, dict) else item).strip()
            if sid in valid and sid not in picked:
                picked.append(sid)
    if not picked:
        for sid in _ID_RE.findall(raw or ""):
            if sid in valid and sid not in picked:
                picked.append(sid)
    return picked


@dataclass
class _Candidate:
    """A node on the search frontier. ``terminal`` means "this node's OWN text is the answer"
    (a leaf, or a parent whose text directly under the heading was picked), so it is not
    expanded further."""

    node: Node
    score: float
    why: str
    terminal: bool


def _options(parent: Node, lines: list[str]) -> list[tuple[Node, bool]]:
    """What can be chosen under ``parent``: its own text (if any) and each child."""
    opts: list[tuple[Node, bool]] = []
    if _own_body(parent, lines).strip():
        opts.append((parent, True))
    opts.extend((c, False) for c in parent.children)
    return opts


def _option_id(node: Node, terminal: bool) -> str:
    return f"{node.node_id}{OWN_SUFFIX}" if terminal else node.node_id


def _option_label(node: Node, terminal: bool, lines: list[str]) -> str:
    if terminal:
        return f"{node.title} (text directly under this heading) -- " + _excerpt(
            _own_body(node, lines)
        )
    return f"{node.title} -- {node.summary or _excerpt(_own_body(node, lines))}"


def _lexical_pick(
    query: str, parent: Node, lines: list[str], beam: int,
) -> list[_Candidate]:
    scored: list[_Candidate] = []
    for node, terminal in _options(parent, lines):
        text = node_own_text(node, lines) if terminal else f"{node.title}\n{node_text(node, lines)}"
        scored.append(_Candidate(node, lexical_score(query, text), "lexical overlap", terminal))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:beam]


async def _router_pick(
    router: Any, query: str, parent: Node, lines: list[str], beam: int, model: str | None,
) -> list[_Candidate] | None:
    options = _options(parent, lines)
    by_id = {_option_id(n, t): (n, t) for n, t in options}
    listing = "\n".join(
        f"{_option_id(n, t)}: {_option_label(n, t, lines)}" for n, t in options
    )
    user = (
        f"Question: {query}\n\nSections under \"{parent.title}\":\n{listing}\n\n"
        f"Return a JSON array of at most {beam} section ids, most relevant first."
    )
    try:
        raw = await _ask(router, _SEARCH_SYSTEM, user, model)
    except Exception as exc:
        logger.warning("tree_search pick under %s failed: %s", parent.node_id, exc)
        return None
    ids = _parse_ids(raw, set(by_id))
    if not ids:
        return None
    picks: list[_Candidate] = []
    for rank, sid in enumerate(ids[:beam]):
        node, terminal = by_id[sid]
        picks.append(_Candidate(
            node, round(1.0 / (rank + 1), 4), f"router pick #{rank + 1} under {parent.title}",
            terminal,
        ))
    return picks


def _path_titles(root: Node, target: Node) -> list[str]:
    def walk(node: Node, trail: list[str]) -> list[str] | None:
        trail = trail + [node.title]
        if node is target:
            return trail
        for child in node.children:
            found = walk(child, trail)
            if found:
                return found
        return None

    return walk(root, []) or [target.title]


async def tree_search(
    root: Node,
    markdown: str,
    query: str,
    *,
    router: Any = None,
    model: str | None = None,
    top_k: int = 3,
    beam: int = 3,
) -> list[dict[str, Any]]:
    """Descend from the root keeping ``beam`` sections per level; return ``top_k`` sections.

    At each expanded node the choices are its children AND its own text (what sits directly
    under the heading), so an answer written above the first subsection is reachable. With a
    router, each level asks the model to rank the choices by title+summary; when the reply
    does not parse (or ``router=None``) the level is ranked by lexical overlap instead.
    Each hit: ``{node_id, title, path, start_line, end_line, text, score, why}`` where
    ``text`` is the section's own text (subsections excluded).
    """
    lines = _split_lines(markdown)
    beam = max(1, beam)
    top_k = max(1, top_k)
    frontier = [_Candidate(root, 1.0, "root", terminal=not root.children)]
    hits: list[_Candidate] = []

    while frontier:
        next_frontier: list[_Candidate] = []
        for cand in frontier:
            if cand.terminal or not cand.node.children:
                hits.append(cand)
                continue
            picks = None
            if router is not None:
                picks = await _router_pick(router, query, cand.node, lines, beam, model)
            if picks is None:
                picks = _lexical_pick(query, cand.node, lines, beam)
            next_frontier.extend(picks)
        next_frontier.sort(key=lambda c: c.score, reverse=True)
        frontier = next_frontier[:max(beam, top_k)]

    hits.sort(key=lambda c: c.score, reverse=True)
    return [
        {
            "node_id": c.node.node_id,
            "title": c.node.title,
            "path": _path_titles(root, c.node),
            "start_line": c.node.start_line,
            "end_line": c.node.end_line,
            "text": node_own_text(c.node, lines),
            "score": round(c.score, 4),
            "why": c.why,
        }
        for c in hits[:top_k]
    ]


# ---------------------------------------------------------------------------
# Document-level entry point + tool
# ---------------------------------------------------------------------------

def _looks_like_path(value: str) -> bool:
    if "\n" in value or len(value) > 1024:
        return False
    try:
        return Path(value).is_file()
    except (OSError, ValueError):
        return False


def _read_markdown(path_or_markdown: str | Path) -> tuple[str, Path | None]:
    if isinstance(path_or_markdown, Path):
        return path_or_markdown.read_text(encoding="utf-8"), path_or_markdown
    if _looks_like_path(path_or_markdown):
        p = Path(path_or_markdown)
        return p.read_text(encoding="utf-8"), p
    return path_or_markdown, None


async def index_document(
    path_or_markdown: str | Path,
    *,
    router: Any = None,
    model: str | None = None,
    force: bool = False,
    min_node_chars: int = 200,
) -> Node:
    """Return the tree for a document, reusing ``<doc>.pageindex.json`` when its sha256 matches.

    Given a path, a fresh build is summarized (only when ``router`` is given) and saved beside
    the source; given a markdown string nothing is persisted. ``force=True`` ignores the sidecar.
    """
    markdown, path = _read_markdown(path_or_markdown)
    digest = sha256_text(markdown)

    if path is not None and not force:
        side = sidecar_path(path)
        if side.is_file():
            try:
                data = _read_sidecar(side)
                if data.get("sha256") == digest and int(data.get("version", 0)) == INDEX_VERSION:
                    return Node.from_dict(data["tree"])
            except (ValueError, KeyError, OSError) as exc:
                logger.warning("ignoring unreadable page index %s: %s", side, exc)

    root = build_tree(markdown, min_node_chars=min_node_chars)
    if router is not None:
        await summarize_tree(root, markdown, router=router, model=model)
    if path is not None:
        save_index(root, sidecar_path(path), source=str(path), sha256=digest)
    return root


def _lazy_router() -> Any:
    try:
        from adk.llm import LLMRouter

        return LLMRouter()
    except Exception as exc:  # no backend configured -> lexical only
        logger.info("doc_tree_search: no LLM router (%s); lexical fallback", exc)
        return None


async def doc_tree_search(path: str, query: str, top_k: int = 3) -> str:
    """Search a long Markdown document by descending its heading tree (no vector store).

    Args:
        path: Path to a .md file. The tree index is cached beside it as <doc>.pageindex.json.
        query: The question to route to the most relevant sections.
        top_k: How many sections to return.
    """
    try:
        markdown, doc = _read_markdown(path)
    except (OSError, UnicodeDecodeError) as exc:
        return json.dumps({"error": f"cannot read {path}: {exc}"})
    if doc is None:
        return json.dumps({"error": f"not a file: {path}"})
    router = _lazy_router()
    root = await index_document(doc, router=router)
    hits = await tree_search(root, markdown, query, router=router, top_k=top_k)
    return json.dumps(
        {
            "path": str(doc),
            "query": query,
            "mode": "router" if router is not None else "lexical",
            "hits": hits,
        },
        ensure_ascii=False,
    )


__all__ = [
    "Node",
    "build_tree",
    "node_text",
    "node_own_text",
    "summarize_tree",
    "save_index",
    "load_index",
    "sidecar_path",
    "tree_search",
    "lexical_score",
    "index_document",
    "doc_tree_search",
]
