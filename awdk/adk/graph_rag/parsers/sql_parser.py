"""SQL parser: tables, columns, foreign keys, and RLS — regex-based, no SQL engine.

Tuned for Postgres migration files (the corpus is ~35% SQL migrations).
"""

from __future__ import annotations

import re

from adk.graph_rag.config import NodeType
from adk.graph_rag.parsers import ParsedEdge, ParsedNode, ParseResult, file_key

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\"\w.]+)\s*\(",
    re.IGNORECASE,
)
_POLICY_ON = re.compile(r"CREATE\s+POLICY\b.*?\bON\s+([\"\w.]+)", re.IGNORECASE | re.DOTALL)
_ENABLE_RLS = re.compile(
    r"ALTER\s+TABLE\s+([\"\w.]+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY", re.IGNORECASE
)
_REFERENCES = re.compile(r"REFERENCES\s+([\"\w.]+)\s*\(", re.IGNORECASE)
_CONSTRAINT_LEAD = re.compile(
    r"^\s*(PRIMARY|FOREIGN|CONSTRAINT|UNIQUE|CHECK|EXCLUDE|LIKE)\b", re.IGNORECASE
)


def _clean_name(raw: str) -> str:
    """Strip quotes and schema qualifier → bare table/column name."""
    name = raw.strip().strip('"')
    if "." in name:
        name = name.split(".")[-1].strip('"')
    return name


def _balanced_body(text: str, open_idx: int) -> tuple[str, int]:
    """Return the substring inside the parens that open at ``open_idx`` and the
    index just past the closing paren."""
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i + 1
    return text[open_idx + 1:], len(text)


def _split_top_level(body: str) -> list[str]:
    """Split a column-definition body on top-level commas."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in body:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        parts.append("".join(cur))
    return parts


def parse_sql(rel_path: str, content: str) -> ParseResult:
    res = ParseResult()
    fkey = file_key(rel_path)
    res.nodes.append(ParsedNode(key=fkey, type=NodeType.FILE, content=rel_path,
                                title=rel_path, metadata={"language": "sql"}))
    tables_seen: dict[str, ParsedNode] = {}

    def ensure_table(name: str, *, rls: bool = False) -> str:
        tkey = f"table:{name}"
        node = tables_seen.get(name)
        if node is None:
            node = ParsedNode(key=tkey, type=NodeType.TABLE, content=f"table {name}",
                              title=name, metadata={"rls": rls})
            tables_seen[name] = node
            res.nodes.append(node)
            res.edges.append(ParsedEdge(src_key=fkey, dst_key=tkey, relation="contains",
                                        weight=1.0))
        elif rls:
            node.metadata["rls"] = True  # RLS enabled after the CREATE TABLE
        return tkey

    for m in _CREATE_TABLE.finditer(content):
        name = _clean_name(m.group(1))
        tkey = ensure_table(name)
        body, _ = _balanced_body(content, m.end() - 1)
        for part in _split_top_level(body):
            stripped = part.strip()
            if not stripped:
                continue
            ref = _REFERENCES.search(stripped)
            if ref:
                other = _clean_name(ref.group(1))
                if other != name:
                    res.edges.append(ParsedEdge(src_key=tkey, dst_key=f"table:{other}",
                                                relation="depends_on", weight=0.9))
            if _CONSTRAINT_LEAD.match(stripped):
                continue
            col_token = stripped.split()[0]
            col = _clean_name(col_token)
            if not col or not re.match(r"^\w+$", col):
                continue
            ckey = f"column:{name}.{col}"
            res.nodes.append(ParsedNode(key=ckey, type=NodeType.COLUMN,
                                        content=f"{name}.{col} {stripped[:120]}",
                                        title=f"{name}.{col}",
                                        metadata={"table": name}))
            res.edges.append(ParsedEdge(src_key=tkey, dst_key=ckey, relation="contains",
                                        weight=1.0))

    # RLS markers may target tables defined in other migration files.
    for m in _ENABLE_RLS.finditer(content):
        ensure_table(_clean_name(m.group(1)), rls=True)
    for m in _POLICY_ON.finditer(content):
        ensure_table(_clean_name(m.group(1)), rls=True)

    return res
