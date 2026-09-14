-- agent_core migration 0001 — memory_entries + memory_citations
-- Standalone SQLite schema; applied automatically by LocalMemoryStore.init().
-- For Postgres/MySQL hosts, translate types as needed.

CREATE TABLE IF NOT EXISTS memory_entries (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    conversation_id TEXT,
    tier TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding_blob BLOB,
    hits INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.5,
    sources_json TEXT NOT NULL DEFAULT '[]',
    credibility REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    accessed_at REAL NOT NULL,
    auto_recall_blocked INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_mem_tenant_tier
    ON memory_entries(tenant_id, tier);
CREATE INDEX IF NOT EXISTS idx_mem_conv
    ON memory_entries(conversation_id);
CREATE INDEX IF NOT EXISTS idx_mem_recall
    ON memory_entries(tenant_id, auto_recall_blocked);

CREATE TABLE IF NOT EXISTS memory_citations (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    span_start INTEGER NOT NULL DEFAULT 0,
    span_end INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memory_entries(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cit_doc ON memory_citations(document_id, status);
CREATE INDEX IF NOT EXISTS idx_cit_mem ON memory_citations(memory_id);

-- Host-app document table additive columns (apply manually if your app
-- uses SQLAlchemy; the agent_core package never owns these columns):
--
--   ALTER TABLE documents ADD COLUMN deleted_at TIMESTAMP NULL;
--   ALTER TABLE documents ADD COLUMN tombstoned BOOLEAN DEFAULT 0;
--   CREATE INDEX IF NOT EXISTS idx_doc_tombstoned ON documents(tombstoned);
