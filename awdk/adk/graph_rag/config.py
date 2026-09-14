"""Configuration, node/edge vocabularies, and tunable thresholds for graph-RAG.

Kept dependency-free (pure stdlib). Thresholds read from the environment so the
service-pack can be tuned without code changes, mirroring how the rest of the
runtime is configured.
"""

from __future__ import annotations

import os
from enum import Enum


class NodeType(str, Enum):
    """Node kinds in a corpus knowledge graph (tuned for MD/SQL/TS corpora)."""

    FILE = "file"
    SECTION = "section"      # a heading + its body within a markdown file
    AGENT = "agent"          # an agent defined under agents/<name>/
    STANDARD = "standard"    # a canonical standard under standards/<name>.md
    TABLE = "table"          # a SQL table
    COLUMN = "column"        # a SQL column
    FUNCTION = "function"    # a TypeScript / code function
    MODULE = "module"        # an external import target
    SYMBOL = "symbol"        # an extracted identifier (service/class/code)


class EdgeType(str, Enum):
    """Edge kinds. Relations are stored as plain strings on GraphEdge."""

    CONTAINS = "contains"                  # structural nesting (file→section, table→column)
    DEFINED_IN = "defined_in"              # node defined in a file
    REFERENCES = "references"              # md link / $ref: / ts import
    DEPENDS_ON = "depends_on"              # FK / import / hard dependency
    USES_TABLE = "uses_table"             # agent/function uses a table
    HANDOFF_TO = "handoff_to"             # agent A hands off to agent B
    IMPLEMENTS_STANDARD = "implements_standard"
    SUCCEEDS = "succeeds"                  # migration B follows migration A
    RELATED_TO = "related_to"             # embedding-similarity discovered


# File extension → logical language.
LANGUAGE_BY_EXT: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "typescript",
}

# Directories never worth indexing.
IGNORE_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
     ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache"}
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


# Seeds below this cosine similarity are dropped — this is what keeps a
# leave-policy query from surfacing codebase nodes (and vice-versa).
RELEVANCE_FLOOR: float = _env_float("AITHER_GRAPH_RAG_FLOOR", 0.22)

DEFAULT_K_SEEDS: int = _env_int("AITHER_GRAPH_RAG_K_SEEDS", 5)
DEFAULT_K_HOPS: int = _env_int("AITHER_GRAPH_RAG_K_HOPS", 1)
DEFAULT_LIMIT: int = _env_int("AITHER_GRAPH_RAG_LIMIT", 14)

# Default namespace when none is supplied.
DEFAULT_NAMESPACE: str = "default"

# Conflict detector: cosine ≥ this between two versions of the SAME node means
# "an edit" (UPDATE); below it (but same id) means a possible CONTRADICTION.
CONFLICT_SIMILARITY: float = _env_float("AITHER_GRAPH_RAG_CONFLICT_SIM", 0.85)
