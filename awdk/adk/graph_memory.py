"""Local knowledge graph — entity extraction, embeddings, hybrid search.

Zero external dependencies beyond httpx (already required). Uses SQLite for
storage, Ollama nomic-embed-text for embeddings (falls back to feature hashing).

This is a lightweight port of AitherOS MemoryGraph. Key differences:
  - SQLite instead of pickle (single file, no HMAC needed)
  - Feature hashing fallback instead of sentence-transformers
  - Simpler entity extraction (regex, no spacy)
  - Same hybrid search pipeline (keyword + semantic)

Usage:
    from adk.graph_memory import GraphMemory

    graph = GraphMemory(agent_name="atlas")
    await graph.remember("AitherOS", "uses", "SQLite")
    await graph.remember("AitherOS", "has", "196 microservices")

    results = await graph.search("What database does AitherOS use?")
    # [GraphNode(label="AitherOS", ...), GraphNode(label="SQLite", ...)]

    # Auto-ingest from conversations
    await graph.ingest_conversation("session1", [
        {"role": "user", "content": "How does AitherOS handle memory?"},
        {"role": "assistant", "content": "AitherOS uses MemoryGraph with embeddings."},
    ])

    # Multi-hop traversal
    related = await graph.get_related("AitherOS", depth=2)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("adk.graph_memory")


# ─────────────────────────────────────────────────────────────────────────────
# Self-maintaining unified memory — feature flag (additive, off by default)
# ─────────────────────────────────────────────────────────────────────────────

def unified_memory_mode() -> str:
    """Return the AITHER_UNIFIED_MEMORY mode: ``'off'`` | ``'shadow'`` | ``'on'``.

    ``off`` (default) → GraphMemory behaves byte-identically to before (no
    governance, no activation recall used). ``shadow`` → governance ledgers run
    but recall behaviour is unchanged. ``on`` → activation recall + governance.
    """
    v = os.getenv("AITHER_UNIFIED_MEMORY", "off").strip().lower()
    if v in ("on", "1", "true", "yes", "enabled"):
        return "on"
    if v == "shadow":
        return "shadow"
    return "off"


def unified_memory_enabled() -> bool:
    """True when the unified-memory governance machinery should run."""
    return unified_memory_mode() in ("on", "shadow")


# Map graph-edge relation strings onto the unified EdgeType vocabulary used by
# the activation scorer (unknown relations fall back to 'related').
_REL_TO_UNIFIED_EDGE: dict[str, str] = {
    "related": "related", "derived_from": "derived_from",
    "tag_sibling": "tag_sibling", "same_session": "same_session",
    "temporal": "temporal", "part_of": "part_of", "elaborates": "elaborates",
    "reinforced_by": "reinforced_by", "same_agent": "same_agent",
    "supersedes": "supersedes", "superseded_by": "superseded_by",
    "contains": "part_of", "mentions": "related", "is_a": "related",
    "uses": "related", "depends_on": "related", "connects_to": "related",
}

_EMBED_DIM = 384  # Match nomic-embed-text small dimension
_OLLAMA_URL = "http://localhost:11434"
_EMBED_MODEL = "nomic-embed-text"

# Stopwords for keyword extraction
_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could of in to for with on at "
    "by from as into through during before after above below between "
    "and or but not no nor so yet both either neither each every all any "
    "few more most other some such this that these those it its he she "
    "they them their we our you your i me my what which who whom how "
    "when where why if then than too very just about also back only "
    "even still already again further once here there up down out off".split()
)


class EdgeType(str, Enum):
    RELATED = "related"           # Embedding similarity > threshold
    DERIVED_FROM = "derived_from" # B created because of A
    TAG_SIBLING = "tag_sibling"   # Share 2+ tags
    SAME_SESSION = "same_session" # Same conversation session
    TEMPORAL = "temporal"         # Created within time window
    MENTIONS = "mentions"         # Entity mentions entity
    IS_A = "is_a"
    USES = "uses"
    DEPENDS_ON = "depends_on"
    CONTAINS = "contains"
    CONNECTS_TO = "connects_to"


@dataclass
class GraphNode:
    """A node in the knowledge graph."""
    id: str
    label: str
    node_type: str = "entity"   # entity, concept, session, fact, relation
    content: str = ""
    tags: list[str] = field(default_factory=list)
    source_agent: str = ""
    source_session: str = ""
    importance: float = 0.5
    created_at: float = 0.0
    updated_at: float = 0.0
    access_count: int = 0
    metadata: dict = field(default_factory=dict)
    # Typed-activation fields (unified memory). Additive; default to the
    # neutral FACT/persistent classification so old data + flag-off behave the
    # same. Populated from the v3 schema columns.
    role: str = "fact"
    confidence: float = 0.7
    tier: str = "persistent"


@dataclass
class GraphEdge:
    """An edge connecting two nodes."""
    source_id: str
    target_id: str
    relation: str
    weight: float = 1.0
    created_at: float = 0.0
    metadata: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Entity extraction patterns
# ─────────────────────────────────────────────────────────────────────────────

_ENTITY_PATTERNS = [
    # Service/class names (CamelCase with known suffixes)
    (re.compile(r'\b([A-Z][a-zA-Z]+(?:Service|Manager|Client|Engine|Controller|Provider|Graph|Store|Bridge|Guard|Pipeline))\b'), "service"),
    # Capitalized multi-word phrases (2-4 words)
    (re.compile(r'\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){1,3})\b'), "entity"),
    # Single capitalized words (3+ chars, not at sentence start — rough heuristic)
    (re.compile(r'(?<=[.!?]\s)\b([A-Z][a-z]{2,})\b|(?<=\s)\b([A-Z][a-z]{2,})\b'), "entity"),
    # File paths
    (re.compile(r'\b([a-zA-Z0-9_/\\]+\.[a-z]{1,5})\b'), "file"),
    # Code identifiers (snake_case with 2+ segments)
    (re.compile(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+){2,})\b'), "code"),
]

_RELATION_PATTERNS = [
    (re.compile(r'(\w+)\s+(?:is|are)\s+(?:a|an|the)\s+(\w+)', re.I), EdgeType.IS_A),
    (re.compile(r'(\w+)\s+(?:uses?|using|utilizes?)\s+(\w+)', re.I), EdgeType.USES),
    (re.compile(r'(\w+)\s+(?:depends?\s+on|requires?)\s+(\w+)', re.I), EdgeType.DEPENDS_ON),
    (re.compile(r'(\w+)\s+(?:contains?|includes?|has)\s+(\w+)', re.I), EdgeType.CONTAINS),
    (re.compile(r'(\w+)\s+(?:connects?\s+to|communicates?\s+with)\s+(\w+)', re.I), EdgeType.CONNECTS_TO),
]


def extract_entities(text: str) -> list[tuple[str, str]]:
    """Extract (entity_label, entity_type) from text."""
    entities = []
    seen = set()
    for pattern, etype in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            label = match.group(1) or match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
            if label and label.lower() not in _STOPWORDS and len(label) >= 3:
                key = label.lower()
                if key not in seen:
                    seen.add(key)
                    entities.append((label, etype))
    return entities[:30]  # Cap


def extract_relations(text: str) -> list[tuple[str, str, str]]:
    """Extract (subject, relation, object) triples from text."""
    relations = []
    for pattern, rel_type in _RELATION_PATTERNS:
        for match in pattern.finditer(text):
            subj, obj = match.group(1), match.group(2)
            if subj.lower() not in _STOPWORDS and obj.lower() not in _STOPWORDS:
                relations.append((subj, rel_type.value, obj))
    return relations[:20]


def extract_keywords(text: str) -> list[str]:
    """Extract keywords from text (stopword-filtered, lowercased)."""
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
    return [w for w in words if w not in _STOPWORDS][:50]


# ─────────────────────────────────────────────────────────────────────────────
# Embedding helpers
# ─────────────────────────────────────────────────────────────────────────────

def _embed_to_blob(embedding: list[float]) -> bytes:
    """Pack float list into compact binary for SQLite BLOB storage."""
    return struct.pack(f'{len(embedding)}f', *embedding)


def _blob_to_embed(blob: bytes) -> list[float]:
    """Unpack BLOB back to float list. Returns [] on a corrupt/truncated blob
    (length not a multiple of 4) instead of raising struct.error into search()."""
    if not blob or len(blob) % 4 != 0:
        if blob:
            logger.warning("corrupt embedding blob (%d bytes, not /4) — skipping", len(blob))
        return []
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity. No numpy needed."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _fallback_embed(text: str, dim: int = _EMBED_DIM) -> list[float]:
    """Feature-hashing embedding. Works offline, no model needed."""
    vector = [0.0] * dim
    words = text.lower().split()
    for word in words:
        idx = hash(word) % dim
        vector[idx] += 1.0
        # Bigrams for basic context
        if len(word) > 3:
            idx2 = hash(word[:3]) % dim
            vector[idx2] += 0.5
    # L2 normalize
    magnitude = sum(x * x for x in vector) ** 0.5
    if magnitude > 0:
        vector = [x / magnitude for x in vector]
    return vector


# ─────────────────────────────────────────────────────────────────────────────
# Query classification (simplified from AitherOS 6-category system)
# ─────────────────────────────────────────────────────────────────────────────

_RE_IDENTITY = re.compile(r'(?:what|who)\s+(?:is|am|are)\s+(?:my|your|the)\s', re.I)
_RE_PROCEDURAL = re.compile(r'(?:how\s+(?:do|to|can)|steps?\s+to|procedure)', re.I)
_RE_SPECIFIC = re.compile(r'"[^"]+"|\b[A-Z][a-zA-Z]+(?:Service|Graph|Engine)\b', re.I)
_RE_CONCEPTUAL = re.compile(r'(?:related\s+to|connections?\s+(?:to|between)|associated)', re.I)
_RE_EXPLORATORY = re.compile(r'(?:what\s+do\s+(?:I|you|we)\s+know|tell\s+me\s+about)', re.I)


def _classify_query(query: str) -> tuple[float, float]:
    """Returns (keyword_weight, semantic_weight)."""
    if _RE_IDENTITY.search(query):
        return (0.9, 0.1)
    if _RE_PROCEDURAL.search(query):
        return (0.6, 0.4)
    if _RE_SPECIFIC.search(query):
        return (0.8, 0.2)
    if _RE_CONCEPTUAL.search(query):
        return (0.2, 0.8)
    if _RE_EXPLORATORY.search(query):
        return (0.3, 0.7)
    return (0.4, 0.6)  # balanced default


# ─────────────────────────────────────────────────────────────────────────────
# GraphMemory
# ─────────────────────────────────────────────────────────────────────────────

_SIMILARITY_THRESHOLD = 0.65
_TEMPORAL_WINDOW_SECS = 300
_MAX_RELATED_PER_NODE = 5
_MAX_CANDIDATES_FOR_SIM = 50


class GraphMemory:
    """Local knowledge graph with embedding-based search.

    SQLite-backed, Ollama-optional, zero external dependencies.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        agent_name: str = "default",
        embed_model: str = _EMBED_MODEL,
        ollama_url: str = "",
        embedder: Optional[Any] = None,
        tenant_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        fleet_url: Optional[str] = None,
        auto_sync: Optional[bool] = None,
    ):
        # Optional injected async embedder `async (text) -> vector`. When set it
        # takes priority over the built-in Ollama path — this is how an app/agent
        # routes graph embeddings through ITS model (e.g. the appliance's vLLM
        # nomic-embed), instead of assuming a local Ollama. Falls back to
        # Ollama/feature-hash only if the injected embedder is absent or fails.
        #
        # When NO embedder is injected, default to the canonical adk.embeddings
        # provider (self-resolving: local vLLM → Ollama → gateway → auto-deploy →
        # CPU/hash), so every agent shares ONE portable 768-d embedding space.
        # Opt out with AITHER_GRAPH_EMBEDDER=legacy to keep the raw Ollama→hash path.
        self._embedder = embedder
        if self._embedder is None and os.getenv(
            "AITHER_GRAPH_EMBEDDER", "adk"
        ).strip().lower() != "legacy":
            try:
                from adk.embeddings import get_default_embedder
                self._embedder = get_default_embedder()
            except Exception as exc:  # noqa: BLE001 — degrade to legacy Ollama/hash
                logger.debug("adk.embeddings default embedder unavailable: %s", exc)
                self._embedder = None
        # Dimension guard — the index is pinned to the dim of the FIRST embedding
        # written; a later embedder producing a different dim (e.g. 768-d vLLM vs
        # 384-d hash) is refused rather than silently mixed (poisons cosine sim).
        self._embed_dim: int | None = None
        self._dim_warned = False
        if db_path is None:
            data_dir = Path(
                os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
            ) / "graph"
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / f"{agent_name}.db"

        self._db_path = str(db_path)
        self._agent = agent_name
        self._embed_model = embed_model
        _raw = ollama_url or os.getenv("OLLAMA_HOST", _OLLAMA_URL)
        if "0.0.0.0" in _raw:
            _raw = _raw.replace("0.0.0.0", "localhost")
        if not _raw.startswith("http"):
            _raw = "http://" + _raw
        self._ollama_url = _raw.rstrip("/")
        self._ollama_available: bool | None = None  # Lazy detect

        # ── Fleet / per-tenant dataplane sync ───────────────────────────────
        # Replicate the local graph to a per-tenant dataplane (Qdrant/Nexus) so
        # a sovereign agent's memory is DURABLE + REHYDRATABLE. Local SQLite is
        # the SOURCE OF TRUTH; sync is best-effort catch-up (push unsynced nodes,
        # pull to rehydrate a fresh instance). Same env contract as adk.memory's
        # KV fleet-sync, but a SEPARATE collection so graph nodes and KV entries
        # never mix. Enable by setting AITHER_FLEET_MEMORY_URL (auto) or
        # AITHER_FLEET_SYNC=true.
        # Explicit args override env — REQUIRED for a multi-tenant host process
        # (e.g. a hosted tenant app) where AITHER_TENANT_ID can't be a process-wide env;
        # (e.g. a hosted workspace) where AITHER_TENANT_ID can't be a process-wide env;
        # the caller passes the per-request tenant. Sovereign single-tenant
        # runtimes just set the env and pass nothing.
        self._tenant_id = (
            tenant_id if tenant_id is not None
            else os.environ.get("AITHER_TENANT_ID", "")
        )
        self._workspace_id = (
            workspace_id if workspace_id is not None
            else os.environ.get("AITHER_WORKSPACE_ID", "")
        )
        self._fleet_url = (
            fleet_url if fleet_url is not None
            else os.environ.get("AITHER_FLEET_MEMORY_URL", "")
        )
        # Sync is ON BY DEFAULT — a sovereign agent's memory is meant to be
        # durable in its dataplane, so this "just works" like the canonical
        # embeddings provider. `auto` (default) = enabled; ONLY
        # AITHER_FLEET_SYNC=false|0|off|no disables. When no explicit target is
        # set we INFER one (local Nexus :8122, or the gateway in cloud mode).
        # Push is always best-effort — if the target is unreachable it fails
        # silently and the local SQLite graph remains fully functional.
        _fleet_flag = os.environ.get("AITHER_FLEET_SYNC", "auto").strip().lower()
        self._fleet_enabled = _fleet_flag not in ("false", "0", "off", "no")
        if not self._fleet_url and self._fleet_enabled:
            cloud_mode = os.environ.get("AITHER_CLOUD_MODE", "")
            if cloud_mode in ("cloud_first", "cloud_only"):
                gw = os.environ.get("AITHER_GATEWAY_URL", "https://gateway.aitherium.com")
                self._fleet_url = f"{gw}/v1/memory"
            else:
                self._fleet_url = "http://localhost:8122"
        self._fleet_collection = os.environ.get(
            "AITHER_FLEET_GRAPH_COLLECTION", "graph_memory"
        )
        self._fleet_auth_token = os.environ.get("AITHER_API_KEY", "")
        # Qdrant backend (RELIABLE two-way): when AITHER_FLEET_QDRANT_URL is set it
        # takes over from the Nexus /ingest path. Nexus can't reliably enumerate
        # (its /search doesn't return ingested docs and /export needs lancedb),
        # so cross-agent rehydration was impossible; Qdrant's `scroll` enumerates a
        # tenant's points exactly. Points are upserted with the node's OWN
        # embedding under a deterministic per-(tenant,node) id (idempotent);
        # fleet_pull scrolls the tenant's points back. Verified live.
        self._qdrant_url = os.environ.get(
            "AITHER_FLEET_QDRANT_URL", "").strip().rstrip("/")
        self._fleet_backend = "qdrant" if self._qdrant_url else "nexus"
        # Auto-replicate new nodes to the dataplane after every ingest (on by
        # default). Runs as a tracked BACKGROUND task so ingest never blocks;
        # call drain_sync() to await it (tests / graceful shutdown).
        self._auto_sync = (
            auto_sync if auto_sync is not None
            else os.environ.get("AITHER_GRAPH_AUTOSYNC", "true").strip().lower()
            not in ("false", "0", "off", "no")
        )
        self._sync_tasks: set = set()

        self._init_db()

        # Self-maintaining unified memory (flag-gated, additive). When
        # AITHER_UNIFIED_MEMORY is 'on'/'shadow' wire a governance ledger so
        # (re-)ingests are classified update-vs-contradiction and audited. Off by
        # default → ``self._governed`` stays None and every write path is a no-op.
        self._governed = None
        self._unified_mode = unified_memory_mode()
        if self._unified_mode in ("on", "shadow"):
            try:
                from adk.graph_rag.governance import (
                    ConflictDetector,
                    GovernanceArtifacts,
                    GovernedIngest,
                    MutationLedger,
                    StableNodeID,
                    TombstoneStore,
                )
                arts = GovernanceArtifacts.beside(self._db_path)
                self._governed = GovernedIngest(
                    ledger=MutationLedger(arts.ledger_path, persist=True),
                    detector=ConflictDetector(),
                    tombstones=TombstoneStore(arts.tombstone_path, persist=True),
                    stable_ids=StableNodeID(arts.stable_id_path, persist=True),
                    source=f"graph:{agent_name}",
                    enabled=True,
                )
            except Exception as exc:  # non-fatal — degrade to ungoverned
                logger.debug("unified-memory governance init failed: %s", exc)
                self._governed = None

    _SCHEMA_VERSION = 4

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    node_type TEXT DEFAULT 'entity',
                    content TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    source_agent TEXT DEFAULT '',
                    source_session TEXT DEFAULT '',
                    importance REAL DEFAULT 0.5,
                    embedding BLOB,
                    created_at REAL,
                    updated_at REAL,
                    access_count INTEGER DEFAULT 0,
                    metadata TEXT DEFAULT '{}',
                    role TEXT DEFAULT 'fact',
                    confidence REAL DEFAULT 0.7,
                    tier TEXT DEFAULT 'persistent',
                    synced INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS edges (
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    weight REAL DEFAULT 1.0,
                    created_at REAL,
                    metadata TEXT DEFAULT '{}',
                    PRIMARY KEY (source_id, target_id, relation),
                    FOREIGN KEY (source_id) REFERENCES nodes(id),
                    FOREIGN KEY (target_id) REFERENCES nodes(id)
                );
                CREATE TABLE IF NOT EXISTS keywords (
                    keyword TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    PRIMARY KEY (keyword, node_id),
                    FOREIGN KEY (node_id) REFERENCES nodes(id)
                );
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
                CREATE INDEX IF NOT EXISTS idx_nodes_agent ON nodes(source_agent);
                CREATE INDEX IF NOT EXISTS idx_nodes_session ON nodes(source_session);
                CREATE INDEX IF NOT EXISTS idx_nodes_created ON nodes(created_at);
                CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
                CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
                CREATE INDEX IF NOT EXISTS idx_keywords ON keywords(keyword);
            """)
            # Migrate BEFORE any index that references a migration-added column.
            # `synced` is added by the v4 migration; on a pre-v4 DB the
            # `CREATE TABLE IF NOT EXISTS` above is a no-op (table already exists
            # without `synced`), so creating idx_nodes_synced here would fail with
            # "no such column: synced" and silently disable GraphMemory entirely.
            self._migrate(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_synced ON nodes(synced)")
            conn.commit()

    def _migrate(self, conn: sqlite3.Connection):
        """Run schema migrations for the graph database."""
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        current = row[0] if row else 0
        if current >= self._SCHEMA_VERSION:
            return
        if current < 1:
            pass  # v1: initial schema
        if current < 2:
            # v2: add edge weight decay timestamp
            try:
                conn.execute("ALTER TABLE edges ADD COLUMN last_accessed REAL DEFAULT 0.0")
            except sqlite3.OperationalError:
                pass
        if current < 3:
            # v3: typed-activation columns (role/confidence/tier) for unified
            # memory. Migration-safe: ALTER guarded so pre-existing DBs still open.
            for ddl in (
                "ALTER TABLE nodes ADD COLUMN role TEXT DEFAULT 'fact'",
                "ALTER TABLE nodes ADD COLUMN confidence REAL DEFAULT 0.7",
                "ALTER TABLE nodes ADD COLUMN tier TEXT DEFAULT 'persistent'",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass
        if current < 4:
            # v4: per-tenant dataplane sync — track which nodes have been
            # replicated to the fleet/dataplane. Migration-safe ALTER.
            try:
                conn.execute("ALTER TABLE nodes ADD COLUMN synced INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (self._SCHEMA_VERSION,))
        conn.commit()
        if current > 0:
            logger.info("Graph DB migrated %d → %d", current, self._SCHEMA_VERSION)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ─── Fleet / per-tenant dataplane sync ──────────────────────────────
    # Replicate the local graph to a per-tenant dataplane (Qdrant/Nexus) so a
    # sovereign agent's memory is durable + rehydratable across restarts and
    # hosting boundaries (hosted-dev ↔ appliance). Local SQLite is the source
    # of truth; every call here is best-effort and MUST NOT raise into ingest.

    def _fleet_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._fleet_auth_token:
            headers["Authorization"] = f"Bearer {self._fleet_auth_token}"
        if self._tenant_id:
            headers["X-Tenant-ID"] = self._tenant_id
        return headers

    @staticmethod
    def _fleet_verify():
        """TLS verify for fleet calls. The fleet dataplane (Nexus) serves
        internal-TLS with a private CA — ``verify=True`` (system trust) would
        REJECT it, so honor a CA-bundle PATH when the runtime provides one
        (``AITHER_CA_BUNDLE`` / ``AITHER_TLS_CA``). Falls back to a bool
        (``AITHER_TLS_VERIFY``, default True). Never returns False silently
        unless explicitly disabled for a plain-HTTP dev Nexus."""
        ca = os.getenv("AITHER_CA_BUNDLE") or os.getenv("AITHER_TLS_CA", "")
        if ca and os.path.exists(ca):
            return ca
        return os.getenv("AITHER_TLS_VERIFY", "true").lower() != "false"

    async def _fleet_push_node(
        self, node_id: str, label: str, content: str, node_type: str,
        tags: list, role, confidence, tier, importance,
    ) -> bool:
        """Push one graph node to the per-tenant dataplane (best-effort).

        Mirrors adk.memory's KV ``/ingest`` contract. The node's content+label
        is re-embedded by the dataplane in the SAME canonical 768-d space, so
        vectors stay portable. Returns True on 200/201.
        """
        if not self._fleet_enabled or not self._fleet_url:
            return False
        try:
            import httpx

            payload = {
                "content": content or label,
                "title": label,
                # NB: source_type/content_type are VALIDATED enums on AitherNexus —
                # "graph"/"graph_node" are rejected (500). Use the accepted values;
                # the graph provenance is carried in metadata.synced_from instead.
                "source_type": "manual",
                "content_type": "text",
                "collection": self._fleet_collection,
                "metadata": {
                    "node_id": node_id,
                    "node_type": node_type,
                    "tags": tags,
                    "role": role,
                    "confidence": confidence,
                    "tier": tier,
                    "importance": importance,
                    "source_agent": self._agent,
                    "tenant_id": self._tenant_id,
                    "workspace_id": self._workspace_id,
                    "synced_from": "adk_graph_memory",
                },
            }
            ingest_url = self._fleet_url.rstrip("/") + "/ingest"
            async with httpx.AsyncClient(
                timeout=8.0,
                verify=self._fleet_verify(),
            ) as client:
                resp = await client.post(
                    ingest_url, json=payload, headers=self._fleet_headers(),
                )
                return resp.status_code in (200, 201)
        except Exception as exc:  # noqa: BLE001 — non-fatal, local is source of truth
            logger.debug("graph fleet push failed (non-fatal): %s", exc)
            return False

    async def fleet_sync_pending(self) -> int:
        """Count local nodes not yet replicated to the dataplane."""
        with self._connect() as conn:
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE synced = 0"
                ).fetchone()
                return int(row[0]) if row else 0
            except sqlite3.OperationalError:
                return 0

    async def fleet_push_all_nodes(self, limit: int = 200) -> dict:
        """Push unsynced local nodes to the per-tenant dataplane (catch-up).

        Call after an ingest (fire-and-forget) to replicate new memory. Local
        SQLite stays the source of truth; a node is marked ``synced`` only on a
        confirmed push. Returns ``{pushed, failed, pending, enabled}``.
        """
        if not self._fleet_enabled or not (self._fleet_url or self._qdrant_url):
            return {"pushed": 0, "failed": 0, "pending": 0, "enabled": False}
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT id, label, content, node_type, tags, role, confidence, "
                    "tier, importance, embedding, metadata FROM nodes WHERE synced = 0 "
                    "ORDER BY updated_at DESC LIMIT ?", (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                return {"pushed": 0, "failed": 0, "pending": 0, "enabled": True}
        pushed = failed = 0
        if self._fleet_backend == "qdrant":
            # Batch upsert to Qdrant (reliable enumerate on pull). Mark synced
            # only for the ids the batch confirmed.
            ok_ids = await self._qdrant_push_nodes(rows)
            with self._connect() as c2:
                for nid in ok_ids:
                    c2.execute("UPDATE nodes SET synced = 1 WHERE id = ?", (nid,))
                c2.commit()
            pushed = len(ok_ids)
            failed = len(rows) - pushed
            # Replicate the edge topology alongside the nodes (best-effort). Edges
            # are cheap + idempotent, so we push the full set each sync rather than
            # tracking per-edge synced state.
            if ok_ids:
                await self._qdrant_push_edges()
        else:
            for nid, label, content, ntype, tags_json, role, conf, tier, imp, _emb, _md in rows:
                try:
                    tags = json.loads(tags_json) if tags_json else []
                except Exception:  # noqa: BLE001
                    tags = []
                ok = await self._fleet_push_node(
                    nid, label, content, ntype, tags, role, conf, tier, imp,
                )
                if ok:
                    with self._connect() as c2:
                        c2.execute(
                            "UPDATE nodes SET synced = 1 WHERE id = ?", (nid,))
                        c2.commit()
                    pushed += 1
                else:
                    failed += 1
        if pushed:
            await self._notify_synced(pushed)
        return {
            "pushed": pushed, "failed": failed,
            "pending": await self.fleet_sync_pending(), "enabled": True,
        }

    async def _notify_synced(self, count: int) -> None:
        """Broadcast that this agent just replicated ``count`` new nodes to the
        tenant dataplane — so OTHER agents in the same tenant/swarm can PULL the
        fresh data (push-based awareness instead of polling) and deconflict work
        in flight. Best-effort; no-op unless ``AITHER_FLEET_EVENTS_URL`` is set
        (a Flux/Chronicle events ingress reachable from the agent, e.g. via the
        gateway). This is the two-way seam: push replicates, this event tells the
        swarm to reconcile."""
        url = os.getenv("AITHER_FLEET_EVENTS_URL", "").strip()
        if not url or count <= 0:
            return
        try:
            import httpx
            async with httpx.AsyncClient(
                timeout=5.0, verify=self._fleet_verify()
            ) as c:
                await c.post(
                    url.rstrip("/") + "/events",
                    json={
                        "event_type": "graph.synced",
                        "source": f"adk_graph_memory:{self._agent}",
                        "data": {
                            "tenant_id": self._tenant_id,
                            "workspace_id": self._workspace_id,
                            "agent": self._agent,
                            "collection": self._fleet_collection,
                            "count": count,
                        },
                    },
                    headers=self._fleet_headers(),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph.synced notify failed (non-fatal): %s", exc)

    # ── Qdrant backend (reliable two-way: upsert + scroll) ───────────────
    def _qdrant_point_id(self, node_id: str) -> str:
        """Deterministic per-(tenant,node) Qdrant point id — a re-push of the
        same node overwrites in place (idempotent), never duplicates."""
        import uuid
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self._tenant_id}:{node_id}"))

    def _qdrant_headers(self) -> dict:
        """Auth header for a Qdrant that requires it. A PUBLICLY-exposed dataplane
        (e.g. behind the gateway/cloudflared so remote swarm agents can reach it)
        MUST be api-key protected — set ``AITHER_FLEET_QDRANT_API_KEY``. Empty for
        a network-internal Qdrant. Qdrant reads the ``api-key`` header."""
        key = os.environ.get("AITHER_FLEET_QDRANT_API_KEY", "").strip()
        hdr = {"api-key": key} if key else {}
        # Routed through mesh-core's /proxy/qdrant (e.g. a tailnet-joined CI
        # runner with no public Qdrant route): the proxy authorizes callers by
        # their registered mesh node identity, not a Qdrant api-key.
        node_id = os.environ.get("AITHER_MESH_NODE_ID", "").strip()
        if node_id:
            hdr["X-Aither-Node-ID"] = node_id
        return hdr

    @staticmethod
    def _qdrant_verify():
        """TLS ``verify=`` for Qdrant calls. Uses the SDK's canonical policy —
        trusts the AitherNet internal CA bundle when installed (so an internal-TLS
        or cloudflared-exposed Qdrant verifies), else system trust. A plain-HTTP
        local Qdrant is unaffected (verify is ignored on http://)."""
        try:
            from adk._tls import tls_verify
            return tls_verify()
        except Exception:  # noqa: BLE001 — never break a sync call on TLS policy
            return True

    def _edges_collection(self) -> str:
        """Companion collection holding EDGES (scroll-enumerable). Edges carry no
        semantic vector, so they live apart from nodes — mixing dummy vectors into
        the node collection would poison cosine search."""
        return f"{self._fleet_collection}_edges"

    async def _qdrant_push_nodes(self, rows) -> list:
        """Upsert node rows (incl. their embeddings) as Qdrant points. Returns
        the node_ids confirmed written. Best-effort; never raises."""
        points, ok_ids, dim = [], [], None
        for nid, label, content, ntype, tags_json, role, conf, tier, imp, emb, md_json in rows:
            vec = _blob_to_embed(emb) if emb else []
            if not vec:
                continue  # a node with no embedding can't be a Qdrant point
            dim = dim or len(vec)
            if len(vec) != dim:
                continue  # never mix dims in one collection
            try:
                tags = json.loads(tags_json) if tags_json else []
            except Exception:  # noqa: BLE001
                tags = []
            try:
                meta = json.loads(md_json) if md_json else {}
            except Exception:  # noqa: BLE001
                meta = {}
            points.append({
                "id": self._qdrant_point_id(nid), "vector": vec,
                "payload": {
                    "node_id": nid, "tenant_id": self._tenant_id,
                    "workspace_id": self._workspace_id, "label": label,
                    "content": content, "node_type": ntype, "tags": tags,
                    "role": role, "confidence": conf, "tier": tier,
                    "importance": imp, "source_agent": self._agent,
                    # Carry the node's metadata so cross-agent pull restores the
                    # full record (goal_id/task_index/reinforcement/… ) — without
                    # it the sync silently dropped everything in metadata.
                    "metadata": meta,
                    "synced_from": "adk_graph_memory",
                },
            })
            ok_ids.append(nid)
        if not points:
            return []
        try:
            import httpx
            col = self._fleet_collection
            hdr = self._qdrant_headers()
            async with httpx.AsyncClient(timeout=20.0, verify=self._qdrant_verify()) as c:
                r = await c.get(f"{self._qdrant_url}/collections/{col}", headers=hdr)
                if r.status_code != 200:
                    await c.put(
                        f"{self._qdrant_url}/collections/{col}",
                        json={"vectors": {"size": dim, "distance": "Cosine"}},
                        headers=hdr)
                r = await c.put(
                    f"{self._qdrant_url}/collections/{col}/points?wait=true",
                    json={"points": points}, headers=hdr)
                return ok_ids if r.status_code in (200, 201) else []
        except Exception as exc:  # noqa: BLE001
            logger.debug("qdrant push failed (non-fatal): %s", exc)
            return []

    async def _qdrant_pull(self, limit: int = 1000) -> int:
        """Rehydrate by SCROLLING the tenant's points from Qdrant — the reliable
        enumerate Nexus lacks. Re-adds each node locally (marked synced)."""
        try:
            import httpx
            col = self._fleet_collection
            async with httpx.AsyncClient(timeout=15.0, verify=self._qdrant_verify()) as c:
                r = await c.post(
                    f"{self._qdrant_url}/collections/{col}/points/scroll",
                    json={"limit": limit, "with_payload": True,
                          "filter": {"must": [{"key": "tenant_id",
                                     "match": {"value": self._tenant_id}}]}},
                    headers=self._qdrant_headers())
                if r.status_code != 200:
                    return 0
                pts = r.json().get("result", {}).get("points", []) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("qdrant pull failed (non-fatal): %s", exc)
            return 0
        restored = 0
        for p in pts:
            pl = p.get("payload", {}) or {}
            if pl.get("synced_from") != "adk_graph_memory":
                continue
            label = pl.get("label") or ""
            if not label:
                continue
            try:
                node = await self.add_node(
                    label=label, node_type=pl.get("node_type", "entity"),
                    content=pl.get("content") or "", tags=pl.get("tags") or [],
                    importance=float(pl.get("importance", 0.5) or 0.5),
                    role=pl.get("role"), confidence=pl.get("confidence"),
                    tier=pl.get("tier"), metadata=pl.get("metadata") or {})
                with self._connect() as c2:
                    c2.execute(
                        "UPDATE nodes SET synced = 1 WHERE id = ?", (node.id,))
                    c2.commit()
                restored += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("qdrant rehydrate node failed: %s", exc)
        # Rehydrate the EXPLICIT edge topology too (nodes-only left agents with
        # only text-re-derived edges — the original graph structure was lost).
        try:
            edges = await self._qdrant_pull_edges()
            if edges:
                logger.debug("qdrant rehydrated %d edges", edges)
        except Exception as exc:  # noqa: BLE001
            logger.debug("qdrant edge pull failed (non-fatal): %s", exc)
        return restored

    async def _qdrant_push_edges(self) -> int:
        """Replicate local EDGES to the companion Qdrant collection so a pulling
        agent restores the exact graph topology, not just re-derived text edges.
        Edges have no semantic vector → a dummy size-1 Dot-space point, scrolled
        (never searched). Deterministic id per (tenant, src, rel, tgt) = idempotent.
        Best-effort; never raises."""
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT source_id, target_id, relation, weight FROM edges"
                ).fetchall()
            except sqlite3.OperationalError:
                return 0
        if not rows:
            return 0
        points = []
        for sid, tid, rel, w in rows:
            pid = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{self._tenant_id}:edge:{sid}:{rel}:{tid}"))
            points.append({
                "id": pid, "vector": [0.0],
                "payload": {
                    "tenant_id": self._tenant_id,
                    "workspace_id": self._workspace_id,
                    "source_id": sid, "target_id": tid, "relation": rel,
                    "weight": float(w or 1.0),
                    "synced_from": "adk_graph_memory",
                },
            })
        col = self._edges_collection()
        try:
            import httpx
            hdr = self._qdrant_headers()
            async with httpx.AsyncClient(timeout=20.0, verify=self._qdrant_verify()) as c:
                r = await c.get(f"{self._qdrant_url}/collections/{col}", headers=hdr)
                if r.status_code != 200:
                    # Dot space tolerates the zero vector (Cosine would reject a
                    # zero-norm point); we only scroll, so distance is irrelevant.
                    await c.put(
                        f"{self._qdrant_url}/collections/{col}",
                        json={"vectors": {"size": 1, "distance": "Dot"}},
                        headers=hdr)
                r = await c.put(
                    f"{self._qdrant_url}/collections/{col}/points?wait=true",
                    json={"points": points}, headers=hdr)
                return len(points) if r.status_code in (200, 201) else 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("qdrant edge push failed (non-fatal): %s", exc)
            return 0

    async def _qdrant_pull_edges(self, limit: int = 5000) -> int:
        """Scroll the tenant's edges back and restore each locally — but ONLY when
        BOTH endpoints already exist locally. Node ids are deterministic
        md5(label:type) so they're portable across agents; an edge whose endpoints
        weren't pulled is skipped (never fabricate a dangling edge)."""
        col = self._edges_collection()
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15.0, verify=self._qdrant_verify()) as c:
                r = await c.post(
                    f"{self._qdrant_url}/collections/{col}/points/scroll",
                    json={"limit": limit, "with_payload": True,
                          "filter": {"must": [{"key": "tenant_id",
                                     "match": {"value": self._tenant_id}}]}},
                    headers=self._qdrant_headers())
                if r.status_code != 200:
                    return 0
                pts = r.json().get("result", {}).get("points", []) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("qdrant edge scroll failed (non-fatal): %s", exc)
            return 0
        restored = 0
        for p in pts:
            pl = p.get("payload", {}) or {}
            if pl.get("synced_from") != "adk_graph_memory":
                continue
            sid, tid, rel = pl.get("source_id"), pl.get("target_id"), pl.get("relation")
            if not (sid and tid and rel):
                continue
            if not (await self.get_node(sid) and await self.get_node(tid)):
                continue  # endpoint missing locally — don't fabricate a dangling edge
            try:
                await self.add_edge(
                    sid, tid, rel, weight=float(pl.get("weight", 1.0) or 1.0))
                restored += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("edge rehydrate failed: %s", exc)
        return restored

    async def fleet_pull(self, query: str = "*", limit: int = 500) -> int:
        """Rehydrate the local graph from the per-tenant dataplane.

        TIERING:
        - LOCAL (Qdrant, ``AITHER_FLEET_QDRANT_URL`` set): Reliable two-way sync via
          scroll enumeration. Full rehydration on fresh instance. Recommended for
          working-set + cross-agent sync. Requires ``adk stack qdrant`` (or external
          Qdrant with API key).
        - CLOUD (Nexus, fallback): Best-effort semantic search only. Degrades to
          append-only RAG (can't enumerate full collection). Use for durable cloud
          storage under cloud_sync SKU; not suitable for cross-agent rehydration.
          For cold restore, carry the SQLite ``.db`` via ``adk.sync``.

        Returns nodes pulled.
        """
        if self._fleet_backend == "qdrant" and self._qdrant_url:
            return await self._qdrant_pull(limit=max(limit, 1000))
        if not self._fleet_enabled or not self._fleet_url:
            return 0
        try:
            import httpx

            search_url = self._fleet_url.rstrip("/") + "/search"
            payload: dict = {
                "query": query, "limit": limit,
                "collection": self._fleet_collection,
            }
            if self._tenant_id:
                payload["tenant_id"] = self._tenant_id
            async with httpx.AsyncClient(
                timeout=15.0,
                verify=self._fleet_verify(),
            ) as client:
                resp = await client.post(
                    search_url, json=payload, headers=self._fleet_headers(),
                )
                if resp.status_code != 200:
                    return 0
                data = resp.json()
                hits = data.get("results", data.get("hits", [])) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph fleet pull failed (non-fatal): %s", exc)
            return 0
        restored = 0
        for h in hits:
            meta = h.get("metadata", h.get("payload", {})) or {}
            if meta.get("synced_from") != "adk_graph_memory":
                continue
            label = h.get("title") or meta.get("label") or ""
            content = h.get("content") or h.get("text") or ""
            if not label:
                continue
            try:
                node = await self.add_node(
                    label=label,
                    node_type=meta.get("node_type", "entity"),
                    content=content,
                    tags=meta.get("tags") or [],
                    importance=float(meta.get("importance", 0.5) or 0.5),
                    role=meta.get("role"),
                    confidence=meta.get("confidence"),
                    tier=meta.get("tier"),
                )
                # Already in the dataplane → mark synced (don't echo it back).
                with self._connect() as c2:
                    c2.execute(
                        "UPDATE nodes SET synced = 1 WHERE id = ?", (node.id,),
                    )
                    c2.commit()
                restored += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("rehydrate node failed (non-fatal): %s", exc)
        return restored

    def _maybe_autosync(self) -> None:
        """Fire a best-effort background replication of unsynced nodes.

        Called automatically after ingest (on by default) so apps NEVER have to
        remember to sync. Runs as a tracked background task — never blocks
        ingest. Use :meth:`drain_sync` to await completion (tests / shutdown).
        No running loop (a sync caller) → skipped; they can call
        :meth:`fleet_push_all_nodes` explicitly.
        """
        if not (self._auto_sync and self._fleet_enabled and self._fleet_url):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.fleet_push_all_nodes())
        self._sync_tasks.add(task)
        task.add_done_callback(self._sync_tasks.discard)

    async def drain_sync(self) -> None:
        """Await any in-flight background sync tasks (tests / graceful shutdown)."""
        if self._sync_tasks:
            await asyncio.gather(*list(self._sync_tasks), return_exceptions=True)

    # ─── Core CRUD ──────────────────────────────────────────────────────

    async def add_node(
        self,
        label: str,
        node_type: str = "entity",
        content: str = "",
        tags: list[str] | None = None,
        source_session: str = "",
        importance: float = 0.5,
        metadata: dict | None = None,
        role: str | None = None,
        confidence: float | None = None,
        tier: str | None = None,
    ) -> GraphNode:
        """Add a node to the graph. Auto-detects edges.

        ``role``/``confidence``/``tier`` are the typed-activation classification
        (unified memory). They are persisted to the v3 columns regardless of the
        feature flag (additive, never read by the legacy :meth:`search`); when
        unset, ``role`` is inferred from content and ``tier`` from the role.
        """
        node_id = hashlib.md5(f"{label}:{node_type}".encode()).hexdigest()[:12]
        now = time.time()
        tags = tags or []
        embedding = await self._embed(f"{label} {content}")

        # Resolve typed-activation classification (cheap, deterministic).
        if role is None:
            try:
                from adk.typed_memory import infer_role
                role = infer_role(content or label, metadata or {})
            except Exception:
                role = "fact"
        if tier is None:
            try:
                from adk.typed_memory import default_tier_for
                tier = default_tier_for(role)
            except Exception:
                tier = "persistent"
        conf = 0.7 if confidence is None else float(confidence)

        with self._connect() as conn:
            # Upsert node
            existing = conn.execute(
                "SELECT id, access_count, content, metadata FROM nodes WHERE id = ?",
                (node_id,)
            ).fetchone()

            if existing:
                # Re-storing the same content must REINFORCE, not reset: the
                # metadata column carries the activation mirror accrued by
                # recall (_reinforce_nodes: reinforcement_count/last_reinforced
                # — sweep()'s archive guard and the scorer's freshness math).
                # A wholesale replace made restating a preference WIPE its
                # protection; instead merge — count carries forward (+1 for
                # the restatement itself) and the reinforcement clock resets
                # to now.
                merged_md = dict(metadata or {})
                try:
                    old_md = json.loads(existing[3] or "{}")
                except (ValueError, TypeError):
                    old_md = {}
                old_rc = int(old_md.get("reinforcement_count", 0) or 0)
                if old_rc or "last_reinforced" in old_md:
                    new_rc = int(merged_md.get("reinforcement_count", 0) or 0)
                    merged_md["reinforcement_count"] = max(old_rc, new_rc) + 1
                    merged_md["last_reinforced"] = now
                metadata = merged_md
                conn.execute(
                    "UPDATE nodes SET content = ?, tags = ?, updated_at = ?, "
                    "access_count = ?, embedding = ?, importance = ?, metadata = ?, "
                    "role = ?, confidence = ?, tier = ?, synced = 0 WHERE id = ?",
                    (content, json.dumps(tags), now, (existing[1] or 0) + 1,
                     _embed_to_blob(embedding) if embedding else None,
                     importance, json.dumps(metadata or {}), role, conf, tier, node_id),
                )
            else:
                conn.execute(
                    "INSERT INTO nodes (id, label, node_type, content, tags, source_agent, "
                    "source_session, importance, embedding, created_at, updated_at, metadata, "
                    "role, confidence, tier) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (node_id, label, node_type, content, json.dumps(tags),
                     self._agent, source_session, importance,
                     _embed_to_blob(embedding) if embedding else None,
                     now, now, json.dumps(metadata or {}), role, conf, tier),
                )

            # Update keyword index
            conn.execute("DELETE FROM keywords WHERE node_id = ?", (node_id,))
            keywords = extract_keywords(f"{label} {content}")
            for kw in keywords:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO keywords (keyword, node_id) VALUES (?, ?)",
                        (kw, node_id),
                    )
                except sqlite3.IntegrityError:
                    pass

            # Governance: classify STORE vs SUPERSEDE/contradiction + ledger.
            # Flag-gated (``self._governed`` is None when AITHER_UNIFIED_MEMORY is
            # off) so this path is a strict no-op in the default configuration.
            if self._governed is not None:
                try:
                    nkey = f"{label}:{node_type}"
                    snap = {"id": node_id, "content": content, "label": label}
                    prev_snap = (
                        {"id": node_id, "content": existing[2] or ""}
                        if existing else None
                    )
                    self._governed.observe(
                        nkey, node_id, content, snap,
                        prev_snapshot=prev_snap, embedding=embedding,
                    )
                except Exception as exc:
                    logger.debug("governance observe failed (non-fatal): %s", exc)

        # Auto-detect edges (non-blocking)
        try:
            await self._auto_detect_edges(node_id, label, tags, embedding, source_session)
        except Exception as exc:
            logger.debug("Auto-edge detection failed (non-fatal): %s", exc)

        node = GraphNode(
            id=node_id, label=label, node_type=node_type, content=content,
            tags=tags, source_agent=self._agent, source_session=source_session,
            importance=importance, created_at=now, updated_at=now,
            metadata=metadata or {}, role=role, confidence=conf, tier=tier,
        )
        return node

    async def get_node(self, node_id: str) -> GraphNode | None:
        """Get a node by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, label, node_type, content, tags, source_agent, "
                "source_session, importance, created_at, updated_at, access_count, metadata, "
                "role, confidence, tier "
                "FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
        if not row:
            return None
        return GraphNode(
            id=row[0], label=row[1], node_type=row[2], content=row[3],
            tags=json.loads(row[4] or "[]"), source_agent=row[5] or "",
            source_session=row[6] or "", importance=row[7] or 0.5,
            created_at=row[8] or 0, updated_at=row[9] or 0,
            access_count=row[10] or 0, metadata=json.loads(row[11] or "{}"),
            role=row[12] or "fact",
            confidence=row[13] if row[13] is not None else 0.7,
            tier=row[14] or "persistent",
        )

    async def remove_node(self, node_id: str):
        """Remove a node and all its edges."""
        with self._connect() as conn:
            conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (node_id, node_id))
            conn.execute("DELETE FROM keywords WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> GraphEdge:
        """Add an edge between two nodes."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO edges (source_id, target_id, relation, weight, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, target_id, relation, weight, now, json.dumps(metadata or {})),
            )
        return GraphEdge(
            source_id=source_id, target_id=target_id, relation=relation,
            weight=weight, created_at=now, metadata=metadata or {},
        )

    async def get_neighbors(
        self,
        node_id: str,
        relation: str | None = None,
        direction: str = "both",
    ) -> list[GraphNode]:
        """Get neighboring nodes via edges."""
        node_ids = set()
        with self._connect() as conn:
            if direction in ("out", "both"):
                q = "SELECT target_id FROM edges WHERE source_id = ?"
                params: list = [node_id]
                if relation:
                    q += " AND relation = ?"
                    params.append(relation)
                for row in conn.execute(q, params).fetchall():
                    node_ids.add(row[0])
            if direction in ("in", "both"):
                q = "SELECT source_id FROM edges WHERE target_id = ?"
                params = [node_id]
                if relation:
                    q += " AND relation = ?"
                    params.append(relation)
                for row in conn.execute(q, params).fetchall():
                    node_ids.add(row[0])

        nodes = []
        for nid in node_ids:
            node = await self.get_node(nid)
            if node:
                nodes.append(node)
        return nodes

    # ─── Convenience API ────────────────────────────────────────────────

    async def remember(
        self,
        subject: str,
        relation: str,
        object_: str,
        metadata: dict | None = None,
    ):
        """Store a knowledge triple (subject, relation, object)."""
        subj_node = await self.add_node(
            label=subject, node_type="entity",
            content=f"{subject} {relation} {object_}",
            metadata=metadata,
        )
        obj_node = await self.add_node(
            label=object_, node_type="entity",
            content=f"{object_} (related to {subject})",
        )
        await self.add_edge(subj_node.id, obj_node.id, relation, weight=0.8)

    async def recall(self, subject: str, relation: str | None = None) -> list[dict]:
        """Query triples by subject. Returns [{relation, object, weight}]."""
        node_id = hashlib.md5(f"{subject}:entity".encode()).hexdigest()[:12]
        results = []
        with self._connect() as conn:
            q = "SELECT e.relation, n.label, e.weight FROM edges e JOIN nodes n ON e.target_id = n.id WHERE e.source_id = ?"
            params: list = [node_id]
            if relation:
                q += " AND e.relation = ?"
                params.append(relation)
            for row in conn.execute(q, params).fetchall():
                results.append({"relation": row[0], "object": row[1], "weight": row[2]})
        return results

    # ─── Unified typed-activation memory (flag-gated additions) ──────────

    async def store(self, record: Any) -> GraphNode:
        """Persist a unified :class:`~adk.unified_contract.MemoryRecord` (or any
        object/dict carrying ``content``/``role``/``tier``/``confidence``) as a
        graph node, honouring the typed-activation columns and the governance
        ledger (when ``AITHER_UNIFIED_MEMORY`` is on/shadow).

        This is additive: it is never invoked on the legacy path, so the default
        (flag-off) behaviour of GraphMemory is unchanged.
        """
        if isinstance(record, dict):
            def g(k, d=None):
                return record.get(k, d)
        else:
            def g(k, d=None):
                return getattr(record, k, d)
        content = str(g("content", "") or "")
        role = g("role", None)
        role = getattr(role, "value", role)
        tier = g("tier", None)
        tier = getattr(tier, "value", tier)
        conf = g("confidence", None)
        label = str(g("label", "") or "") or (content[:60].strip() if content else "memory")
        node_type = str(g("node_type", "fact") or "fact")
        tags = list(g("tags", []) or [])
        md = dict(g("metadata", {}) or {})
        if g("superseded_by", None):
            md["superseded_by"] = g("superseded_by")
        if g("stale", None):
            md["stale"] = bool(g("stale"))
        # Round-trip the activation fields through metadata so recall can
        # reconstruct freshness/authority (mirrors to_memory_entry_kwargs).
        for fld in ("last_reinforced", "reinforcement_count", "temporal_consistency"):
            val = g(fld, None)
            if val is not None:
                md[fld] = val
        return await self.add_node(
            label=label, node_type=node_type, content=content, tags=tags,
            role=role, confidence=conf, tier=tier, metadata=md,
            source_session=str(g("scope_namespace", "") or ""),
        )

    async def _base_scores(
        self, query: str, fetch: int, node_type: str | None = None,
    ) -> dict[str, float]:
        """Hybrid keyword+semantic relevance ``{node_id: score}`` — the ranking
        core of :meth:`search` without the fetch/access-count side effects, used
        as the base relevance for activation re-ranking."""
        kw_weight, sem_weight = _classify_query(query)
        keywords = extract_keywords(query)
        query_embedding = await self._embed(query)
        scores: dict[str, float] = {}
        with self._connect() as conn:
            if keywords:
                placeholders = ",".join("?" * len(keywords))
                rows = conn.execute(
                    f"SELECT node_id, COUNT(*) as hits FROM keywords "
                    f"WHERE keyword IN ({placeholders}) GROUP BY node_id "
                    f"ORDER BY hits DESC LIMIT ?",
                    (*keywords, fetch),
                ).fetchall()
                max_hits = max((r[1] for r in rows), default=1)
                for nid, hits in rows:
                    scores[nid] = (hits / max_hits) * kw_weight
            if query_embedding:
                type_filter = ""
                params: list = []
                if node_type:
                    type_filter = "WHERE node_type = ?"
                    params.append(node_type)
                rows = conn.execute(
                    f"SELECT id, embedding FROM nodes {type_filter} "
                    f"ORDER BY created_at DESC LIMIT ?",
                    (*params, _MAX_CANDIDATES_FOR_SIM * 5),
                ).fetchall()
                for nid, emb_blob in rows:
                    if emb_blob:
                        emb = _blob_to_embed(emb_blob)
                        sim = cosine_similarity(query_embedding, emb)
                        if sim > 0.1:
                            scores[nid] = scores.get(nid, 0) + sim * sem_weight
        return scores

    def _edges_of(self, node_id: str) -> list:
        """Outgoing edges as unified ``MemoryEdgeRecord`` objects (sync provider
        consumed by the activation scorer)."""
        from adk.unified_contract import MemoryEdgeRecord
        out: list = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT target_id, relation, weight FROM edges WHERE source_id = ?",
                (node_id,),
            ).fetchall()
        for tgt, rel, w in rows:
            et = _REL_TO_UNIFIED_EDGE.get(str(rel), "related")
            try:
                out.append(MemoryEdgeRecord(
                    source_id=node_id, target_id=tgt, edge_type=et,
                    weight=float(w or 1.0), weight_decay_rate=0.0,
                ))
            except Exception:
                continue
        return out

    def _node_to_record(self, node: GraphNode, relevance: float = 0.0,
                        now: float | None = None) -> Any:
        """Map a :class:`GraphNode` onto a unified :class:`MemoryRecord`."""
        from adk.unified_contract import MemoryRecord
        now = now if now is not None else time.time()
        md = node.metadata if isinstance(node.metadata, dict) else {}
        role = node.role or md.get("role") or "fact"
        tier = node.tier or "persistent"
        last_reinf = float(md.get("last_reinforced", node.updated_at or node.created_at or now))
        try:
            return MemoryRecord(
                id=node.id,
                content=node.content or node.label,
                role=role, tier=tier,
                confidence=float(node.confidence if node.confidence is not None else 0.7),
                reinforcement_count=int(md.get("reinforcement_count", 0) or 0),
                temporal_consistency=float(md.get("temporal_consistency", 1.0) or 1.0),
                created_at=float(node.created_at or now),
                last_accessed=float(node.updated_at or node.created_at or now),
                last_reinforced=last_reinf,
                stale=bool(md.get("stale", False)),
                superseded_by=md.get("superseded_by"),
                relevance=float(relevance),
            )
        except Exception:
            return MemoryRecord(
                id=node.id, content=node.content or node.label, relevance=float(relevance),
            )

    def _freshness_of(self, node_id: str, now: float | None = None) -> float:
        """Synchronous freshness lookup for the cascade planner (default 1.0)."""
        from adk.unified_contract import MemoryRecord
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT tier, updated_at, created_at, metadata FROM nodes WHERE id = ?",
                    (node_id,),
                ).fetchone()
            if not row:
                return 1.0
            last = float(row[1] or row[2] or 0.0)
            md = json.loads(row[3] or "{}")
            last = float(md.get("last_reinforced", last))
            return MemoryRecord(
                content="", tier=row[0] or "persistent", last_reinforced=last,
            ).freshness(now)
        except Exception:
            return 1.0

    async def recall_with_activation(
        self,
        query: str,
        limit: int = 10,
        node_type: str | None = None,
        include_stale: bool = False,
        now: float | None = None,
        reinforce: bool = False,
    ) -> list[GraphNode]:
        """Authority + spreading-activation recall.

        Returns the same ``list[GraphNode]`` shape as :meth:`search`, re-ranked by
        ``relevance × role-authority × confidence × freshness × supersession ×
        tier-weight × (1 + activation)``. A correction outranks a stale fact; an
        ephemeral memory decays out. Each returned node carries its authority
        labels in ``metadata['_authority_labels']``. Falls back to :meth:`search`
        if the unified scorer is unavailable.

        ``reinforce=True`` (opt-in; default False = zero writes, existing callers
        byte-identical) makes recall REINFORCING: every RETURNED node gets
        ``reinforcement_count += 1`` and ``last_reinforced = now`` persisted in
        its metadata mirror — the exact fields the scorer's freshness /
        reinforcement-bonus math reads — so recalled memories decay slower and
        unrecalled ones age out (see :meth:`sweep`).
        """
        if not query.strip():
            return []
        try:
            from adk.graph_rag.activation_scoring import get_scorer
            from adk.unified_contract import Role
        except Exception:
            # Degraded recall must NOT drop the reinforcement contract too —
            # the write path (_reinforce_nodes) has no scorer dependency, and
            # silently skipping it here let actively-recalled nodes decay to
            # the sweep exactly when scoring was already degraded.
            nodes = await self.search(query, limit=limit, node_type=node_type)
            if reinforce and nodes:
                self._reinforce_nodes([n.id for n in nodes], now=now)
            return nodes

        now = now if now is not None else time.time()
        scorer = get_scorer()
        base = await self._base_scores(query, max(limit * 3, 20), node_type)
        if not base:
            return []

        recs: dict[str, Any] = {}
        roles: dict[str, Any] = {}
        for nid, rel in base.items():
            node = await self.get_node(nid)
            if not node:
                continue
            rec = self._node_to_record(node, relevance=rel, now=now)
            recs[nid] = rec
            try:
                roles[nid] = rec.role  # already a Role enum
            except Exception:
                roles[nid] = None
        if not recs:
            return []

        # Flat-relevance detection: hash/keyword backends emit near-uniform noise;
        # collapse to 1.0 so authority alone orders the result (mirrors the store).
        rels = [r.relevance for r in recs.values()]
        if not rels or (max(rels) - min(rels) < 1e-3) or (max(rels) < 0.05):
            for r in recs.values():
                r.relevance = 1.0

        seeds = sorted(recs, key=lambda n: -recs[n].relevance)[: max(5, limit)]
        try:
            activation = scorer.spread_activation(
                seeds, self._edges_of, role_of=lambda nid: roles.get(nid), now=now,
            )
        except Exception:
            activation = {}

        # Pull in newly-activated neighbours not already in the candidate set.
        for nid in list(activation):
            if nid in recs or len(recs) >= 50:
                continue
            node = await self.get_node(nid)
            if node:
                recs[nid] = self._node_to_record(node, relevance=0.0, now=now)

        scored: list[tuple[float, str, Any]] = []
        for nid, rec in recs.items():
            if not include_stale:
                if rec.superseded_by or (rec.stale and rec.freshness(now) < 0.3):
                    continue
            bd = scorer.score(rec, activation=activation.get(nid, 0.0), now=now)
            scored.append((bd.combined, nid, rec))
        scored.sort(key=lambda x: -x[0])

        selected = scored[:limit]
        if reinforce and selected:
            # Reinforce-on-recall BEFORE fetching the output nodes so the
            # returned metadata already carries the bumped values.
            self._reinforce_nodes([nid for _c, nid, _r in selected], now=now)

        out: list[GraphNode] = []
        for _combined, nid, rec in selected:
            node = await self.get_node(nid)
            if node:
                node.metadata = dict(node.metadata or {})
                node.metadata["_authority_labels"] = scorer.labels(rec, now)
                out.append(node)
        return out

    def _reinforce_nodes(self, node_ids: list[str], now: float | None = None) -> None:
        """Bump ``reinforcement_count``/``last_reinforced`` in each node's
        metadata mirror — the fields :meth:`_node_to_record` feeds the activation
        scorer, making the MemoryRecord reinforcement contract LIVE. One UPDATE
        per node inside a single WAL transaction. Only runs when a caller opts
        in (``recall_with_activation(..., reinforce=True)``)."""
        now = now if now is not None else time.time()
        try:
            with self._connect() as conn:
                for nid in node_ids:
                    row = conn.execute(
                        "SELECT metadata FROM nodes WHERE id = ?", (nid,)
                    ).fetchone()
                    if not row:
                        continue
                    try:
                        md = json.loads(row[0] or "{}")
                    except ValueError:
                        md = {}
                    md["reinforcement_count"] = int(md.get("reinforcement_count", 0) or 0) + 1
                    md["last_reinforced"] = now
                    conn.execute(
                        "UPDATE nodes SET metadata = ? WHERE id = ?",
                        (json.dumps(md), nid),
                    )
        except Exception as exc:  # non-fatal — recall must never fail on reinforce
            logger.debug("reinforce-on-recall failed (non-fatal): %s", exc)

    def _apply_cascade(self, plan: dict, trigger_id: str = "") -> None:
        """Apply a supersession cascade plan as neighbour metadata hints
        (``cascade_decay`` / ``temporal_consistency_hint`` / ``stale``)."""
        for nid, info in plan.items():
            try:
                with self._connect() as conn:
                    row = conn.execute(
                        "SELECT metadata FROM nodes WHERE id = ?", (nid,)
                    ).fetchone()
                    if not row:
                        continue
                    md = json.loads(row[0] or "{}")
                    decay = float(info.get("decay", 0.0))
                    # APPLY the decay to the field recall scoring actually consumes: temporal_consistency feeds
                    # MemoryScorer.effective_confidence (= confidence * temporal_consistency), so lowering it makes
                    # the cascade BITE a neighbour's recall score. (Previously only the write-only *_hint was set,
                    # leaving the neighbour's ranking unchanged — the advertised cascade was inert.)
                    prev_tc = float(md.get("temporal_consistency", 1.0) or 1.0)
                    md["temporal_consistency"] = round(max(0.0, min(1.0, prev_tc * (1.0 - decay))), 4)
                    md["cascade_decay"] = round(decay, 4)
                    md["cascade_from"] = trigger_id
                    md["temporal_consistency_hint"] = round(1.0 - decay, 4)
                    if info.get("stale", 0.0) >= 1.0:
                        md["stale"] = True
                    conn.execute(
                        "UPDATE nodes SET metadata = ? WHERE id = ?",
                        (json.dumps(md), nid),
                    )
            except Exception:
                continue
        if self._governed is not None and plan:
            try:
                from adk.graph_rag.governance import MutationType
                self._governed.ledger.record(
                    MutationType.SUPERSEDE, node_id=trigger_id,
                    reason="cascade", source=f"graph:{self._agent}",
                    related_ids=list(plan.keys()),
                )
            except Exception:
                pass

    async def supersede(
        self,
        old_id: str,
        new_record: Any,
        *,
        reason: str = "",
        cascade: bool = True,
        now: float | None = None,
    ) -> GraphNode:
        """Replace ``old_id`` with ``new_record`` (a MemoryRecord / dict / text).

        Marks the old node superseded + stale (× supersede-factor on recall),
        links new→old with a ``supersedes`` edge, ripples a bounded freshness /
        temporal-consistency decay to related neighbours, ledgers the mutation,
        and returns the new :class:`GraphNode`.
        """
        now = now if now is not None else time.time()
        before = await self.get_node(old_id)
        if isinstance(new_record, str):
            from adk.unified_contract import MemoryRecord
            new_record = MemoryRecord(content=new_record, role="correction")
        new_node = await self.store(new_record)

        if before is not None:
            md = dict(before.metadata or {})
            md["superseded_by"] = new_node.id
            md["stale"] = True
            md["supersede_reason"] = str(reason)[:500]
            try:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE nodes SET metadata = ?, confidence = ? WHERE id = ?",
                        (json.dumps(md), 0.3, old_id),
                    )
            except Exception as exc:
                logger.debug("supersede mark-old failed: %s", exc)
            try:
                await self.add_edge(new_node.id, old_id, "supersedes", weight=1.0)
            except Exception:
                pass
            if self._governed is not None:
                try:
                    from adk.graph_rag.governance import MutationType
                    self._governed.ledger.record(
                        MutationType.SUPERSEDE, node_id=old_id,
                        before={"id": old_id, "content": before.content},
                        after={"id": new_node.id, "content": new_node.content},
                        source=f"graph:{self._agent}",
                        reason=str(reason) or "superseded by correction",
                    )
                except Exception:
                    pass

        if cascade:
            try:
                from adk.graph_rag.activation_scoring import get_scorer
                plan = get_scorer().cascade_plan(
                    [old_id], self._edges_of, self._freshness_of, now=now,
                )
                if plan:
                    self._apply_cascade(plan, trigger_id=new_node.id)
            except Exception as exc:
                logger.debug("supersede cascade failed (non-fatal): %s", exc)

        return new_node

    # ─── Memory maintenance (opt-in primitives; nothing calls them
    #     automatically, so default behaviour is byte-identical) ──────────

    def _governance_stores(self):
        """``(ledger, tombstones)`` for maintenance ops — the governed pair when
        ``AITHER_UNIFIED_MEMORY`` is on/shadow, else lazily-opened persistent
        stores beside the db, so :meth:`sweep` archives stay reversible even
        with the flag off. Returns ``(None, None)`` if governance is unavailable
        (in which case sweep refuses to delete — never an irreversible drop)."""
        if self._governed is not None:
            return self._governed.ledger, self._governed.tombstones
        try:
            from adk.graph_rag.governance import (
                GovernanceArtifacts,
                MutationLedger,
                TombstoneStore,
            )
            arts = GovernanceArtifacts.beside(self._db_path)
            return (
                MutationLedger(arts.ledger_path, persist=True),
                TombstoneStore(arts.tombstone_path, persist=True),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("governance stores unavailable: %s", exc)
            return None, None

    def sweep(
        self,
        now: float | None = None,
        archive_below: float = 0.05,
        max_nodes: int | None = None,
        dry_run: bool = False,
        preserve_roles: Any = None,
    ) -> dict:
        """Enforce read-time decay at rest: archive (tombstone + FORGET-ledger +
        remove) every node that is past its tier TTL, scored below
        ``archive_below`` (tier freshness ``e^(-λ·Δt)`` × reinforcement bonus —
        the EXISTING MemoryRecord math), and weakly reinforced
        (``reinforcement_count <= 1``). PERMANENT-tier nodes are immune, and so
        is any node whose ``role`` is in ``preserve_roles`` (a caller-owned
        exemption — e.g. a durable preference store passes
        ``{"preference","identity","correction"}`` so prefs lose rank but never
        silently die). When ``max_nodes`` is set and live nodes exceed it, the
        lowest-scored overflow is additionally archived (never PERMANENT/preserved).

        Archives are REVERSIBLE: each node is snapshot into the governance
        ``TombstoneStore`` first (``TombstoneStore.recover(tombstone_id)``
        returns the snapshot; ids in the returned ``tombstones`` map) — a node
        is never deleted without a tombstone. The snapshot includes the node's
        edge rows (``snap["edges"]``) so a recover can re-add its graph
        connectivity; embeddings are re-derived on re-store. ``dry_run=True``
        returns the would-archive list without acting.

        Returns ``{examined, archived, kept, skipped_permanent,
        skipped_preserved, would_archive, tombstones}``.
        """
        from adk.unified_contract import TIER_TTL_SECONDS, MemoryRecord, Tier

        now = now if now is not None else time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, label, node_type, content, tags, tier, role, "
                "confidence, importance, created_at, updated_at, metadata, "
                "source_agent, source_session FROM nodes"
            ).fetchall()

        examined = len(rows)
        skipped_permanent = 0
        skipped_preserved = 0
        preserved = {str(x).strip().lower() for x in (preserve_roles or ())}
        candidates: list[tuple[str, dict]] = []   # to archive
        live: list[tuple[float, str, dict]] = []  # (score, id, snapshot) survivors

        for r in rows:
            nid = r[0]
            tier = (r[5] or "persistent").strip().lower()
            try:
                md = json.loads(r[11] or "{}")
            except ValueError:
                md = {}
            snapshot = {
                "id": nid, "label": r[1], "node_type": r[2], "content": r[3],
                "tags": r[4], "tier": tier, "role": r[6], "confidence": r[7],
                "importance": r[8], "created_at": r[9], "updated_at": r[10],
                "metadata": md, "source_agent": r[12], "source_session": r[13],
            }
            if tier == Tier.PERMANENT.value:
                skipped_permanent += 1
                continue
            if preserved and (r[6] or "").strip().lower() in preserved:
                skipped_preserved += 1
                continue  # caller-exempt role — never archived, never overflow-pressured
            last = float(md.get("last_reinforced", r[10] or r[9] or now) or now)
            reinf = int(md.get("reinforcement_count", 0) or 0)
            try:
                rec = MemoryRecord(
                    content="", tier=tier, last_reinforced=last,
                    reinforcement_count=reinf,
                )
            except ValueError:  # unknown tier string → neutral persistent
                rec = MemoryRecord(
                    content="", last_reinforced=last, reinforcement_count=reinf,
                )
            # The EXISTING tier math: freshness = e^(-λ·Δt), reinforcement-adjusted.
            score = rec.freshness(now) * (1.0 + rec.reinforcement_bonus())
            ttl = TIER_TTL_SECONDS.get(rec.tier)
            past_ttl = ttl is not None and (now - last) > ttl
            if past_ttl and score < archive_below and reinf <= 1:
                candidates.append((nid, snapshot))
            else:
                live.append((score, nid, snapshot))

        # Overflow pressure: archive the lowest-scored survivors beyond max_nodes.
        if max_nodes is not None and len(live) > max_nodes:
            live.sort(key=lambda t: t[0])
            candidates.extend(
                (nid, snap) for _s, nid, snap in live[: len(live) - max_nodes]
            )

        stats: dict[str, Any] = {
            "examined": examined,
            "archived": 0,
            "kept": examined - skipped_permanent - len(candidates),
            "skipped_permanent": skipped_permanent,
            "skipped_preserved": skipped_preserved,
            "would_archive": [nid for nid, _ in candidates],
            "tombstones": {},
        }
        if dry_run:
            return stats

        ledger, tombs = self._governance_stores()
        for nid, snap in candidates:
            if tombs is None:
                continue  # never delete without a tombstone (reversibility)
            # Snapshot the node's edges too — the DELETEs below drop them, and
            # a recover() without them restores content but silently loses the
            # graph connectivity spreading activation depends on.
            try:
                with self._connect() as conn:
                    erows = conn.execute(
                        "SELECT source_id, target_id, relation, weight FROM edges "
                        "WHERE source_id = ? OR target_id = ?", (nid, nid),
                    ).fetchall()
                if erows:
                    snap["edges"] = [
                        {"source_id": e[0], "target_id": e[1],
                         "relation": e[2], "weight": e[3]} for e in erows]
            except Exception as exc:  # noqa: BLE001 — edge capture is best-effort
                logger.debug("sweep edge snapshot failed for %s: %s", nid, exc)
            try:
                tomb_id = tombs.entomb(snap, reason="sweep: decayed past tier TTL")
            except Exception as exc:  # noqa: BLE001 — keep the node instead
                logger.debug("sweep entomb failed for %s — kept: %s", nid, exc)
                continue
            if ledger is not None:
                try:
                    from adk.graph_rag.governance import MutationType
                    ledger.record(
                        MutationType.FORGET, node_id=nid, before=snap,
                        source=f"graph:{self._agent}", reason="sweep",
                        related_ids=[tomb_id],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("sweep ledger failed for %s: %s", nid, exc)
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
                    (nid, nid),
                )
                conn.execute("DELETE FROM keywords WHERE node_id = ?", (nid,))
                conn.execute("DELETE FROM nodes WHERE id = ?", (nid,))
            stats["archived"] += 1
            stats["tombstones"][nid] = tomb_id
        stats["kept"] = examined - skipped_permanent - stats["archived"]
        return stats

    def promote(
        self, node_id: str, tier: str | None = None, role: str | None = None,
    ) -> bool:
        """Re-tier / re-role a node IN PLACE — updates the ``tier``/``role``
        columns plus the metadata mirror without delete+re-store, so edges,
        embedding, keywords and ledger history survive. Returns True when the
        node was updated; False for a missing node, no-op args, or an invalid
        tier/role. Ledgers an UPDATE entry when governance is on."""
        if tier is None and role is None:
            return False
        try:
            from adk.unified_contract import Role as _Role, Tier as _Tier
            if tier is not None:
                tier = _Tier(str(tier).strip().lower()).value
            if role is not None:
                role = _Role(str(role).strip().lower()).value
        except ValueError:
            logger.debug("promote(%s): invalid tier/role (%r/%r)", node_id, tier, role)
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tier, role, metadata FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if not row:
                return False
            try:
                md = json.loads(row[2] or "{}")
            except ValueError:
                md = {}
            before = {"id": node_id, "tier": row[0], "role": row[1]}
            new_tier = tier if tier is not None else (row[0] or "persistent")
            new_role = role if role is not None else (row[1] or "fact")
            md["tier"] = new_tier
            md["role"] = new_role
            conn.execute(
                "UPDATE nodes SET tier = ?, role = ?, metadata = ? WHERE id = ?",
                (new_tier, new_role, json.dumps(md), node_id),
            )
        if self._governed is not None:
            try:
                from adk.graph_rag.governance import MutationType
                self._governed.ledger.record(
                    MutationType.UPDATE, node_id=node_id, before=before,
                    after={"id": node_id, "tier": new_tier, "role": new_role},
                    source=f"graph:{self._agent}", reason="promote",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("promote ledger failed (non-fatal): %s", exc)
        return True

    # ─── Hybrid Search ──────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        limit: int = 10,
        node_type: str | None = None,
    ) -> list[GraphNode]:
        """Hybrid search: keyword + semantic, weighted by query type."""
        if not query.strip():
            return []

        kw_weight, sem_weight = _classify_query(query)
        keywords = extract_keywords(query)
        query_embedding = await self._embed(query)

        scores: dict[str, float] = {}

        with self._connect() as conn:
            # Stage 1: Keyword search via inverted index
            if keywords:
                placeholders = ",".join("?" * len(keywords))
                rows = conn.execute(
                    f"SELECT node_id, COUNT(*) as hits FROM keywords "
                    f"WHERE keyword IN ({placeholders}) GROUP BY node_id "
                    f"ORDER BY hits DESC LIMIT ?",
                    (*keywords, limit * 3),
                ).fetchall()
                max_hits = max((r[1] for r in rows), default=1)
                for nid, hits in rows:
                    scores[nid] = (hits / max_hits) * kw_weight

            # Stage 2: Semantic search via embedding similarity
            if query_embedding:
                type_filter = ""
                params: list = []
                if node_type:
                    type_filter = "WHERE node_type = ?"
                    params.append(node_type)
                rows = conn.execute(
                    f"SELECT id, embedding FROM nodes {type_filter} "
                    f"ORDER BY created_at DESC LIMIT ?",
                    (*params, _MAX_CANDIDATES_FOR_SIM * 5),
                ).fetchall()
                for nid, emb_blob in rows:
                    if emb_blob:
                        emb = _blob_to_embed(emb_blob)
                        sim = cosine_similarity(query_embedding, emb)
                        if sim > 0.1:
                            scores[nid] = scores.get(nid, 0) + sim * sem_weight

        # Stage 3: Rank and fetch
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]

        nodes = []
        for nid, score in ranked:
            node = await self.get_node(nid)
            if node:
                # Increment access count
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE nodes SET access_count = access_count + 1 WHERE id = ?",
                        (nid,),
                    )
                nodes.append(node)
        return nodes

    async def query(self, question: str, limit: int = 5) -> list[GraphNode]:
        """Natural language query — alias for search()."""
        return await self.search(question, limit=limit)

    # ─── Conversation Ingestion ─────────────────────────────────────────

    async def ingest_conversation(
        self,
        session_id: str,
        messages: list[dict],
        extract_triples: bool = True,
    ) -> int:
        """Auto-ingest entities and relationships from conversation messages.

        Returns number of nodes created/updated.
        """
        count = 0
        all_text = " ".join(m.get("content", "") for m in messages)
        # Strip <think>/<thinking> reasoning blocks before storing
        all_text = re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', all_text, flags=re.IGNORECASE)
        all_text = re.sub(r'<think(?:ing)?>[^<]*$', '', all_text, flags=re.IGNORECASE)
        all_text = all_text.strip()

        # Extract and store entities
        entities = extract_entities(all_text)
        for label, etype in entities:
            try:
                await self.add_node(
                    label=label, node_type=etype,
                    content=all_text[:500],
                    source_session=session_id,
                    tags=[etype, "auto_extracted"],
                )
                count += 1
            except Exception:
                pass

        # Extract and store relations as edges
        if extract_triples:
            relations = extract_relations(all_text)
            for subj, rel, obj in relations:
                try:
                    await self.remember(subj, rel, obj)
                    count += 2  # subject + object nodes
                except Exception:
                    pass

        # Store conversation as a session node
        try:
            user_msgs = [m["content"] for m in messages if m.get("role") == "user"]
            summary = "; ".join(user_msgs)[:300]
            await self.add_node(
                label=f"session:{session_id[:8]}",
                node_type="session",
                content=summary,
                source_session=session_id,
                tags=["conversation", "auto_ingested"],
            )
            count += 1
        except Exception:
            pass

        if count:
            logger.debug("Ingested %d nodes from session %s", count, session_id[:8])
        # Replicate the new nodes to the per-tenant dataplane (background,
        # best-effort, on by default). Both ingest_text and this path inherit it.
        self._maybe_autosync()
        return count

    async def ingest_text(self, text: str, source: str = "unknown") -> int:
        """Ingest entities from arbitrary text."""
        return await self.ingest_conversation(
            session_id=f"text:{source}",
            messages=[{"role": "user", "content": text}],
        )

    # ─── Graph Traversal ────────────────────────────────────────────────

    async def get_related(self, entity: str, depth: int = 2) -> dict:
        """BFS expansion from entity. Returns subgraph as adjacency dict."""
        node_id = hashlib.md5(f"{entity}:entity".encode()).hexdigest()[:12]
        visited = set()
        subgraph: dict[str, list[dict]] = {}
        queue = [(node_id, 0)]

        while queue:
            current_id, current_depth = queue.pop(0)
            if current_id in visited or current_depth > depth:
                continue
            visited.add(current_id)

            node = await self.get_node(current_id)
            if not node:
                continue

            neighbors = await self.get_neighbors(current_id)
            subgraph[node.label] = []
            for n in neighbors:
                # Get edge details
                with self._connect() as conn:
                    edge = conn.execute(
                        "SELECT relation, weight FROM edges "
                        "WHERE (source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?)",
                        (current_id, n.id, n.id, current_id),
                    ).fetchone()
                rel = edge[0] if edge else "related"
                weight = edge[1] if edge else 0.5
                subgraph[node.label].append({
                    "entity": n.label, "relation": rel, "weight": weight,
                })
                if n.id not in visited and current_depth + 1 <= depth:
                    queue.append((n.id, current_depth + 1))

        return subgraph

    # ─── Stats ──────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        """Get graph statistics."""
        with self._connect() as conn:
            node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            keyword_count = conn.execute("SELECT COUNT(DISTINCT keyword) FROM keywords").fetchone()[0]
            types = conn.execute(
                "SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type"
            ).fetchall()
            relations = conn.execute(
                "SELECT relation, COUNT(*) FROM edges GROUP BY relation"
            ).fetchall()
            embedded = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL"
            ).fetchone()[0]
        return {
            "nodes": node_count,
            "edges": edge_count,
            "keywords": keyword_count,
            "embedded": embedded,
            "node_types": dict(types),
            "edge_relations": dict(relations),
            "agent": self._agent,
            "db_path": self._db_path,
            "ollama_available": self._ollama_available,
        }

    # ─── Embeddings ─────────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        """Get an embedding. Priority: injected embedder (the app's model — e.g.
        the appliance/fleet vLLM nomic-embed) → Ollama → feature-hash.

        DIMENSION SAFETY: when an embedder was INJECTED, a failure returns [] (skip
        this node's embedding) rather than falling back to a DIFFERENT-dimension
        model. Mixing dims in one index (e.g. 768-d vLLM with 384-d hash) silently
        poisons cosine similarity — never do it. The legacy Ollama→hash path is
        unchanged only when no embedder is injected."""
        if self._embedder is not None:
            try:
                vec = await self._embedder(text)
                return self._guard_dim(list(vec) if vec else [])
            except Exception as exc:
                logger.debug("injected embedder failed (skip embed, not mixing dims): %s", exc)
                return []
        if self._ollama_available is False:
            return self._guard_dim(_fallback_embed(text))

        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self._ollama_url}/api/embeddings",
                    json={"model": self._embed_model, "prompt": text},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    embedding = data.get("embedding", [])
                    if embedding:
                        self._ollama_available = True
                        return self._guard_dim(embedding)
        except Exception:
            pass

        if self._ollama_available is None:
            self._ollama_available = False
            logger.info("Ollama embeddings unavailable — using feature hashing fallback")
        return self._guard_dim(_fallback_embed(text))

    def _guard_dim(self, vec: list[float]) -> list[float]:
        """Enforce a single embedding dimension per index. The first non-empty
        vector pins the dim (persisted in ``meta``); any later vector of a
        different dim is refused (``[]``) rather than mixed — mixing 768-d and
        384-d vectors in one store silently corrupts cosine similarity."""
        if not vec:
            return []
        d = len(vec)
        if self._embed_dim is None:
            self._embed_dim = self._pin_dim(d)
        if self._embed_dim is not None and d != self._embed_dim:
            if not self._dim_warned:
                logger.warning(
                    "graph %s: embedding dim %d != index dim %d — skipping embedding "
                    "(dimension mismatch; re-index to switch models)",
                    self._agent, d, self._embed_dim,
                )
                self._dim_warned = True
            return []
        return vec

    def _pin_dim(self, d: int) -> int:
        """Return the index's committed embedding dim, pinning it to ``d`` on first
        write. Atomic and race-safe across concurrent processes on the same db:
        ``BEGIN IMMEDIATE`` serialises writers, ``INSERT OR IGNORE`` lets the first
        winner set the value, and the read-back inside the same transaction returns
        the AUTHORITATIVE committed value (never a dim we failed to persist). Only a
        genuine storage error degrades to ``d`` in-memory for this process alone."""
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('embed_dim', ?)",
                    (str(d),),
                )
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'embed_dim'"
                ).fetchone()
                conn.commit()
                if row and row[0]:
                    return int(row[0])
                return d
        except Exception as exc:  # noqa: BLE001
            logger.debug("embed_dim pin failed (using %d in-memory): %s", d, exc)
            return d

    # ─── Auto-Edge Detection ────────────────────────────────────────────

    async def _auto_detect_edges(
        self,
        node_id: str,
        label: str,
        tags: list[str],
        embedding: list[float] | None,
        source_session: str,
    ):
        """Detect and create edges for a new/updated node."""
        with self._connect() as conn:
            # 1. TAG_SIBLING: shared tags (via keyword index on tag values)
            if tags:
                for tag in tags:
                    tag_lower = tag.lower()
                    rows = conn.execute(
                        "SELECT id, tags FROM nodes WHERE id != ? AND tags LIKE ?",
                        (node_id, f'%"{tag_lower}"%'),
                    ).fetchall()
                    for other_id, other_tags_json in rows:
                        other_tags = json.loads(other_tags_json or "[]")
                        shared = set(t.lower() for t in tags) & set(t.lower() for t in other_tags)
                        if len(shared) >= 2:
                            weight = min(1.0, len(shared) / 5.0)
                            try:
                                conn.execute(
                                    "INSERT OR IGNORE INTO edges (source_id, target_id, relation, weight, created_at) "
                                    "VALUES (?, ?, ?, ?, ?)",
                                    (node_id, other_id, EdgeType.TAG_SIBLING.value, weight, time.time()),
                                )
                            except sqlite3.IntegrityError:
                                pass

            # 2. SAME_SESSION: nodes from same conversation
            if source_session:
                rows = conn.execute(
                    "SELECT id FROM nodes WHERE id != ? AND source_session = ? LIMIT 10",
                    (node_id, source_session),
                ).fetchall()
                for (other_id,) in rows:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO edges (source_id, target_id, relation, weight, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (node_id, other_id, EdgeType.SAME_SESSION.value, 0.5, time.time()),
                        )
                    except sqlite3.IntegrityError:
                        pass

            # 3. RELATED: embedding similarity
            if embedding:
                rows = conn.execute(
                    "SELECT id, embedding FROM nodes WHERE id != ? AND embedding IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    (node_id, _MAX_CANDIDATES_FOR_SIM),
                ).fetchall()
                related_count = 0
                for other_id, emb_blob in rows:
                    if related_count >= _MAX_RELATED_PER_NODE:
                        break
                    other_emb = _blob_to_embed(emb_blob)
                    sim = cosine_similarity(embedding, other_emb)
                    if sim >= _SIMILARITY_THRESHOLD:
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO edges (source_id, target_id, relation, weight, created_at) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (node_id, other_id, EdgeType.RELATED.value, round(sim, 3), time.time()),
                            )
                            related_count += 1
                        except sqlite3.IntegrityError:
                            pass


# ─────────────────────────────────────────────────────────────────────────────
# Module singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: GraphMemory | None = None


def get_graph_memory(agent_name: str = "default") -> GraphMemory:
    """Get or create the module-level GraphMemory singleton."""
    global _instance
    if _instance is None:
        _instance = GraphMemory(agent_name=agent_name)
    return _instance
