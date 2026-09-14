# adk.graph_rag — Graph-based RAG for awdk

A portable, dependency-light service-pack that turns a **corpus** (Markdown, SQL,
TypeScript) into an **embedded knowledge graph**, retrieves a **bounded subgraph**
(facts + relations) instead of flat top-k, and governs (re-)ingestion with a
**"memory you can put on trial"** layer (audit ledger + conflict-on-write +
reversible forget).

Pure stdlib + the existing adk graph/vector primitives. No monorepo dependency —
it's meant to ship inside an agent product.

---

## Why

Most agent "memory" is a flat vector store: it returns the *k* most similar chunks,
silently overwrites old facts, and can't explain what it knows or when it changed.
This module fixes both halves:

- **Retrieval quality** — a graph subgraph carries *relationships* (a section →
  the standard it implements → the table it writes), so the model gets connected
  context, not a bag of chunks. Fewer tokens, more signal.
- **Trust over time** — every ingestion is audited; a changed fact is *classified*
  (refinement vs. contradiction) and recorded; a removed fact is *tombstoned*
  (recoverable), not destroyed. You can answer "why did this change?".

---

## Architecture

```
INGEST (one-time / nightly)                 QUERY (per turn)
───────────────────────────                 ───────────────────────────
corpus dir (*.md *.sql *.ts)                 "how does approval work?"
  │ corpus_loader.load_corpus                  │ open_retriever(path, embedder)
  ▼                                            ▼
parsers/  (md / sql / ts / symbol)           CorpusGraphRetriever.subgraph_search
  │  → ParseResult(nodes, edges)               │ embed query → vector seed search
  ▼                                            │ drop seeds below RELEVANCE_FLOOR
graph_builder.build_graph                      │ filter by namespace (optional)
  │  merge by natural key                      │ BFS expand along edges (k_hops)
  │  StableNodeID.id_for (namespaced)          ▼
  │  embed nodes (injected embedder)         Subgraph(nodes, edges)
  │  GovernedIngest.observe  ── TRIAL          │ .to_context(max_chars)
  │     ├─ MutationLedger (store/supersede)    ▼
  │     ├─ ConflictDetector (verdict)        "# Facts … # Relations …"  → prompt
  │     └─ TombstoneStore (forget→recover)
  ▼
graph_store.save → <name>.graph.json
                   <name>.graph.ledger.jsonl
                   <name>.graph.tombstones.jsonl
                   <name>.graph.stable_ids.json
```

---

## File map

| File | Responsibility |
|---|---|
| `config.py` | `NodeType` / `EdgeType` vocab; tunable thresholds (`RELEVANCE_FLOOR`, `DEFAULT_K_*`, `CONFLICT_SIMILARITY`) read from env. |
| `corpus_loader.py` | Walk a dir → `{rel_path: RawDoc}`; skips noise dirs, classifies by extension. |
| `parsers/__init__.py` | `ParsedNode` / `ParsedEdge` / `ParseResult`, the `parse(doc)` dispatcher, `slugify`, `file_key`. |
| `parsers/markdown_parser.py` | Frontmatter, heading tree → `section` nodes, `[md](links)` + `$ref:` → `references`, agent/standard nodes, `handoff_to` / `implements_standard`. |
| `parsers/sql_parser.py` | `CREATE TABLE` → `table`/`column`, `REFERENCES` → `depends_on`, RLS flag. |
| `parsers/typescript_parser.py` | `export function` → `function` nodes, relative `import` → `depends_on`. |
| `parsers/symbol_extractor.py` | Reuses `adk.graph_memory` entity extraction → `symbol` nodes + `references`. |
| `graph_builder.py` | Parse → merge → embed → resolve/dedupe edges → importance → governance. |
| `graph_store.py` | In-memory graph + adjacency + JSON persistence (reuses `GraphNode`/`GraphEdge`). |
| `bounded_retriever.py` | `CorpusGraphRetriever` (implements `adk.graph_retriever.GraphRetriever`). |
| `governance.py` | `MutationLedger`, `ConflictDetector`, `TombstoneStore`, `StableNodeID`, `GovernedIngest`, `GovernanceArtifacts`. |
| `__init__.py` | High-level `ingest_corpus()` / `open_retriever()` + exports. |

---

## Quick start

```python
from adk.graph_rag import ingest_corpus, open_retriever

# 1. Ingest (incremental + audited). `embedder` is an adk Embedder OR an
#    async callable text -> list[float]. Use the SAME embedder for query.
stats = await ingest_corpus(
    corpus_root="/path/to/repo",
    output_path="index/repo.graph.json",
    embedder=my_embed,           # async def my_embed(text) -> list[float]
    namespace="codebase",
    govern=True,
)
print(stats["nodes"], stats["edges"], stats["conflicts"])

# 2. Query
retriever = open_retriever("index/repo.graph.json", embedder=my_embed)
sg = await retriever.subgraph_search(
    "how does the approval workflow work?",
    namespace="codebase", k_seeds=5, k_hops=2, limit=14,
)
print(sg.to_context(max_chars=6000))   # "# Facts … # Relations …"
```

> **Embedding-space rule:** the query embedder must match the one used at ingest
> (same model/dim). Mixing them yields garbage similarity.

---

## Graph schema

**Node types:** `file`, `section`, `agent`, `standard`, `table`, `column`,
`function`, `module`, `symbol`.

**Edge relations:** `contains` (structural nesting), `defined_in`, `references`
(md link / `$ref:` / import), `depends_on` (FK / import), `uses_table`,
`handoff_to`, `implements_standard`, `succeeds` (migration order), `related_to`.

Nodes are addressed by a **natural key** (e.g. `section:agents/priya/rule.md#leave-rules`,
`table:global_outbox`, `agent:arjun`). The natural key is what you see in a
`Subgraph` and what you pass to `why_did_this_change`. Internally each key maps to a
content-independent **stable id** (`n_<hash>`) so a node keeps its identity — and its
ledger history and edges — across content edits.

---

## The retriever

`CorpusGraphRetriever.subgraph_search(query, *, k_seeds, k_hops, limit, namespace)`:

1. Embed the query, k-NN over node embeddings.
2. **Relevance floor** — drop seeds below `RELEVANCE_FLOOR` (default 0.22). This is
   what keeps an off-topic namespace out of an answer.
3. **Namespace filter** — restrict seeds to one namespace if given.
4. **BFS expansion** — follow edges (both directions) up to `k_hops`, capped at `limit`.
5. Return a `Subgraph` whose node/edge ids are the readable natural keys.

`Subgraph.to_context(max_chars)` renders a deterministic `# Facts` + `# Relations`
block (existing adk behaviour), truncated to budget.

---

## Governance ("memory you can put on trial")

Enabled per ingest with `govern=True`. Artifacts live beside the graph file
(`<stem>.graph.ledger.jsonl`, `.tombstones.jsonl`, `.stable_ids.json`).

- **`MutationLedger`** — append-only JSONL; one entry per `STORE` / `SUPERSEDE` /
  `FORGET` with before/after, reason, source, timestamp. `by_node(id)` returns a
  node's full history (its "trial record"). Durable: fsync per line.
- **`ConflictDetector`** — when a node's content changes on re-ingest, returns a
  `ConflictEvent` classified **update** (refinement, high similarity, no polarity/
  number flip) or **contradiction** (similarity below `CONFLICT_SIMILARITY`, or a
  negation added/removed, or a numeric value changed).
- **`TombstoneStore`** — a node removed from the corpus is snapshotted and
  `FORGET`-logged; `recover(id)` restores it within retention.
- **`StableNodeID`** — persisted natural-key → id registry; also tracks content
  hashes so the builder knows new vs. changed vs. unchanged (unchanged → no ledger
  noise).

`GovernedIngest` is **namespace-scoped** (`key_prefix`): ingesting one namespace
never tombstones another's nodes, even though they share the registry.

---

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `AITHER_GRAPH_RAG_FLOOR` | `0.22` | Seed relevance floor. |
| `AITHER_GRAPH_RAG_K_SEEDS` | `5` | Default seed count. |
| `AITHER_GRAPH_RAG_K_HOPS` | `1` | Default expansion depth. |
| `AITHER_GRAPH_RAG_LIMIT` | `14` | Default max nodes per subgraph. |
| `AITHER_GRAPH_RAG_CONFLICT_SIM` | `0.85` | Similarity boundary for update vs. contradiction. |

---

## Tests

`awdk/tests/test_graph_rag.py` — parsers, builder, retriever (floor / BFS /
determinism), governance (ledger / conflict / tombstone / stable-id), end-to-end
governed re-ingest, and namespace isolation. Run:

```
cd awdk && python -m pytest tests/test_graph_rag.py -q
```

---

## Performance notes

- JSON persistence stores embeddings as float arrays — at 1536-dim this is ~25 KB
  per node (a 4 k-node corpus ≈ 100 MB). Fine to build/load; **gitignore the index**.
  For large corpora, implement the `VectorStore` protocol against FAISS/SQLite and a
  binary embedding store (the interfaces already support it).
- Ingestion is O(files); retrieval seed search is O(nodes·dim) via
  `InMemoryVectorStore` — fine for ≤ a few thousand nodes, swap an ANN index above that.
```
