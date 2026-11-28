"""Local knowledge base indexer and pure-Python BM25 RAG / ``file_search`` tool.

Zero external vector database required. Rejects path traversal and symlinks,
preserves short non-empty documents (``< 50`` chars), and optionally converts
PDF/DOCX/PPTX/XLSX via ``adk.docconvert`` when installed.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from adk.bots.manifest import KnowledgeConfig

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset(
    "a an and the of in on for to with by from into at or is are be this that "
    "my our your it its as use using how what do does".split()
)
_TEXT_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".json", ".csv", ".yaml", ".yml"})
_SKIP_NAMES = frozenset({".env", ".DS_Store", ".gitkeep"})

K1 = 1.2
B = 0.75


def _stem(tok: str) -> str:
    for suf in ("ing", "ies", "es", "ed", "s"):
        if len(tok) > len(suf) + 2 and tok.endswith(suf):
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


def tokenize(text: str) -> list[str]:
    """Tokenize and lightly stem *text* for BM25 indexing and search."""
    raw = _TOKEN_RE.findall((text or "").lower())
    filtered = [_stem(t) for t in raw if t not in _STOP_WORDS]
    if filtered:
        return filtered
    return [_stem(t) for t in raw]


@dataclass
class KnowledgeChunk:
    chunk_id: str
    source: str
    text: str
    offset: int
    tokens: list[str] = field(default_factory=list)


@dataclass
class KnowledgeHit:
    source: str
    text: str
    score: float
    offset: int = 0
    chunk_id: str = ""


def _resolve_safe_knowledge_path(bot_dir: Path, rel_path: str) -> Path:
    raw_input = rel_path or ""
    if "\x00" in raw_input:
        raise ValueError(f"Null byte not allowed in knowledge path: {raw_input!r}")
    raw = raw_input.strip()
    if not raw:
        raise ValueError("Empty knowledge path")
    p = Path(raw)
    if p.is_absolute() or raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError(f"Absolute knowledge path not allowed: {raw!r}")
    if ".." in p.parts:
        raise ValueError(f"Path traversal not allowed in knowledge path: {raw!r}")

    root_resolved = bot_dir.resolve()
    cur = bot_dir
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"Symlink not allowed in knowledge path: {raw!r}")
    resolved = cur.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"Knowledge path escapes bot directory: {raw!r}")
    return cur


def _read_document_text(path: Path) -> str | None:
    if path.name in _SKIP_NAMES or path.is_symlink():
        return None
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if suffix == ".json":
            try:
                parsed = json.loads(raw)
                return json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception:
                return raw
        return raw

    # Optional document conversion (PDF/DOCX/PPTX/XLSX) if docconvert/pypdf/docling installed.
    try:
        from adk.docconvert import DOCUMENT_EXTENSIONS, convert_document

        if suffix.lstrip(".") in DOCUMENT_EXTENSIONS:
            return convert_document(path)
    except ImportError:
        return None
    return None


def _chunk_text(text: str, chunk_size: int) -> list[tuple[int, str]]:
    """Split *text* into chunks of up to *chunk_size* chars, keeping short docs intact."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [(0, cleaned)]

    overlap = min(120, max(20, chunk_size // 6))
    paragraphs = re.split(r"\n{2,}", cleaned)
    chunks: list[tuple[int, str]] = []
    buf = ""
    buf_offset = 0
    cursor = 0

    for para in paragraphs:
        p = para.strip()
        if not p:
            continue
        if len(p) > chunk_size:
            if buf:
                chunks.append((buf_offset, buf.strip()))
                buf = ""
            start = 0
            step = max(1, chunk_size - overlap)
            while start < len(p):
                piece = p[start : start + chunk_size].strip()
                if piece:
                    chunks.append((cursor + start, piece))
                if start + chunk_size >= len(p):
                    break
                start += step
        elif not buf:
            buf = p
            buf_offset = cursor
        elif len(buf) + 2 + len(p) <= chunk_size:
            buf = f"{buf}\n\n{p}"
        else:
            chunks.append((buf_offset, buf.strip()))
            buf = p
            buf_offset = cursor
        cursor += len(para) + 2

    if buf.strip():
        chunks.append((buf_offset, buf.strip()))
    return chunks


class KnowledgeIndex:
    """Pure-Python BM25 + query-coverage local knowledge retriever."""

    def __init__(self, chunks: list[KnowledgeChunk] | None = None) -> None:
        self.chunks: list[KnowledgeChunk] = list(chunks or [])
        self._avg_len: float = (
            sum(len(c.tokens) for c in self.chunks) / len(self.chunks)
            if self.chunks
            else 1.0
        ) or 1.0

    @classmethod
    def from_manifest(
        cls,
        bot_dir: str | Path,
        knowledge_cfg: KnowledgeConfig | dict[str, Any] | None,
    ) -> KnowledgeIndex:
        root = Path(bot_dir)
        if knowledge_cfg is None:
            return cls([])
        if isinstance(knowledge_cfg, dict):
            cfg = KnowledgeConfig.model_validate(knowledge_cfg)
        else:
            cfg = knowledge_cfg

        chunks: list[KnowledgeChunk] = []
        seen_files: set[Path] = set()

        for rel in cfg.paths:
            target = _resolve_safe_knowledge_path(root, rel)
            if not target.exists():
                continue
            files: list[Path]
            if target.is_file():
                files = [target]
            else:
                files = []
                for walk_root, walk_dirs, walk_files in os.walk(target):
                    for dname in sorted(walk_dirs):
                        dp = Path(walk_root) / dname
                        if dp.is_symlink():
                            raise ValueError(
                                f"Symlink not allowed in knowledge: {dp.relative_to(root)}"
                            )
                    for fname in sorted(walk_files):
                        fp = Path(walk_root) / fname
                        if fp.is_symlink():
                            raise ValueError(
                                f"Symlink not allowed in knowledge: {fp.relative_to(root)}"
                            )
                        files.append(fp)
            for fp in files:
                # Reject any symlinked parent dir inside target
                rel_fp = fp.relative_to(root)
                cur = root
                for part in rel_fp.parts:
                    cur = cur / part
                    if cur.is_symlink():
                        raise ValueError(f"Symlink not allowed in knowledge: {rel_fp}")
                resolved = fp.resolve()
                if not resolved.is_relative_to(root.resolve()):
                    raise ValueError(f"Knowledge file escapes bot_dir: {rel_fp}")
                if resolved in seen_files:
                    continue
                seen_files.add(resolved)

                content = _read_document_text(fp)
                if not content or not content.strip():
                    continue
                source_name = rel_fp.as_posix()
                for idx, (offset, piece) in enumerate(_chunk_text(content, cfg.chunk_size)):
                    toks = tokenize(f"{source_name} {piece}")
                    if not toks:
                        continue
                    chunks.append(
                        KnowledgeChunk(
                            chunk_id=f"{source_name}#{idx}",
                            source=source_name,
                            text=piece,
                            offset=offset,
                            tokens=toks,
                        )
                    )
        return cls(chunks)

    def search(self, query: str, top_k: int = 5) -> list[KnowledgeHit]:
        """Rank chunks by BM25 + IDF-weighted query coverage."""
        q_tokens = list(dict.fromkeys(tokenize(query)))
        if not q_tokens or not self.chunks or top_k <= 0:
            return []

        n = len(self.chunks)
        df = {t: sum(1 for c in self.chunks if t in c.tokens) for t in q_tokens}
        idf = {t: math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_tokens}
        total_idf = sum(idf.values()) or 1.0

        scored: list[KnowledgeHit] = []
        for chunk in self.chunks:
            dl = len(chunk.tokens) or 1
            bm25_score = 0.0
            covered_idf = 0.0
            for t in q_tokens:
                tf = chunk.tokens.count(t)
                if not tf:
                    continue
                covered_idf += idf[t]
                bm25_score += (
                    idf[t]
                    * tf
                    * (K1 + 1.0)
                    / (tf + K1 * (1.0 - B + B * dl / self._avg_len))
                )
            if bm25_score <= 0.0:
                continue
            coverage = covered_idf / total_idf
            combined = bm25_score * (0.5 + 0.5 * coverage)
            scored.append(
                KnowledgeHit(
                    source=chunk.source,
                    text=chunk.text,
                    score=round(combined, 6),
                    offset=chunk.offset,
                    chunk_id=chunk.chunk_id,
                )
            )

        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]

    @staticmethod
    def format_context(hits: list[KnowledgeHit]) -> str:
        """Render *hits* as a grounded prompt context block."""
        if not hits:
            return ""
        lines = ["[KNOWLEDGE CONTEXT]"]
        for h in hits:
            lines.append(f"[{h.source}] {h.text}")
        return "\n".join(lines)

    def as_file_search_tool(self, default_top_k: int = 5) -> Callable[[str, int], dict[str, Any]]:
        """Return a callable ``file_search`` tool suitable for ``ToolRegistry``."""

        def file_search(query: str, top_k: int = default_top_k) -> dict[str, Any]:
            """Search the bot's local knowledge documents for relevant snippets."""
            hits = self.search(query, top_k=max(1, min(int(top_k), 20)))
            return {
                "query": query,
                "hits": [
                    {
                        "source": h.source,
                        "score": h.score,
                        "text": h.text,
                    }
                    for h in hits
                ],
            }

        return file_search
