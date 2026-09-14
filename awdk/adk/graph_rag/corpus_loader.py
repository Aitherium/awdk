"""Walk a corpus directory and yield raw documents for parsing.

Pure stdlib. Classifies files by extension and skips noise directories.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from adk.graph_rag.config import IGNORE_DIRS, LANGUAGE_BY_EXT


@dataclass
class RawDoc:
    """One source file, normalized for parsing."""

    rel_path: str          # POSIX-style path relative to the corpus root
    language: str          # markdown | sql | typescript
    content: str
    size: int
    mtime: float


def load_corpus(
    root: str | Path,
    *,
    include_languages: set[str] | None = None,
    max_bytes: int = 2_000_000,
) -> dict[str, RawDoc]:
    """Return ``{rel_path: RawDoc}`` for every indexable file under ``root``.

    ``include_languages`` (e.g. ``{"markdown"}``) restricts the sweep; ``None``
    indexes all supported languages. Files larger than ``max_bytes`` are skipped.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"corpus root is not a directory: {root_path}")

    docs: dict[str, RawDoc] = {}
    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        if any(part in IGNORE_DIRS for part in path.relative_to(root_path).parts):
            continue
        language = LANGUAGE_BY_EXT.get(path.suffix.lower())
        if language is None:
            continue
        if include_languages is not None and language not in include_languages:
            continue
        try:
            stat = path.stat()
            if stat.st_size > max_bytes:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root_path).as_posix()
        docs[rel] = RawDoc(
            rel_path=rel,
            language=language,
            content=content,
            size=stat.st_size,
            mtime=stat.st_mtime,
        )
    return docs
