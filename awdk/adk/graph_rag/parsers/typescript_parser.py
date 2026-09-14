"""TypeScript parser: exported functions + relative imports — regex, no AST.

Tuned for Supabase Edge Functions (the corpus is ~5% TS). Deliberately shallow:
function nodes for navigation, relative imports as ``references`` edges.
"""

from __future__ import annotations

import posixpath
import re

from adk.graph_rag.config import NodeType
from adk.graph_rag.parsers import ParsedEdge, ParsedNode, ParseResult, file_key
from adk.graph_rag.parsers.symbol_extractor import symbol_fragments

_FUNC = re.compile(
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
)
_CONST_FN = re.compile(
    r"(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(",
)
_IMPORT = re.compile(r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""")


def _resolve_rel(rel_path: str, target: str) -> str | None:
    if not target.startswith("."):
        return None  # npm / URL / bare specifier — skip
    base = posixpath.dirname(rel_path)
    resolved = posixpath.normpath(posixpath.join(base, target))
    if resolved.startswith(".."):
        return None
    if not resolved.endswith((".ts", ".tsx")):
        resolved += ".ts"
    return resolved


def parse_typescript(rel_path: str, content: str) -> ParseResult:
    res = ParseResult()
    fkey = file_key(rel_path)
    res.nodes.append(ParsedNode(key=fkey, type=NodeType.FILE, content=rel_path,
                                title=rel_path, metadata={"language": "typescript"}))

    seen_fn: set[str] = set()
    for pattern in (_FUNC, _CONST_FN):
        for m in pattern.finditer(content):
            name = m.group(1)
            if name in seen_fn:
                continue
            seen_fn.add(name)
            fnkey = f"function:{rel_path}:{name}"
            res.nodes.append(ParsedNode(key=fnkey, type=NodeType.FUNCTION,
                                        content=f"function {name} in {rel_path}",
                                        title=name, metadata={"file": rel_path}))
            res.edges.append(ParsedEdge(src_key=fkey, dst_key=fnkey, relation="contains",
                                        weight=1.0))

    for m in _IMPORT.finditer(content):
        tgt = _resolve_rel(rel_path, m.group(1))
        if tgt:
            res.edges.append(ParsedEdge(src_key=fkey, dst_key=file_key(tgt),
                                        relation="depends_on", weight=0.8))

    snodes, sedges = symbol_fragments(fkey, content, max_symbols=6)
    res.nodes.extend(snodes)
    res.edges.extend(sedges)
    return res
