---
name: aither-retrieval
description: >-
  Aither Retrieval — one surface, every modality. The AitherOS retrieval surface answers one question and returns fused, ranked results from every modality you have — vector similarity, PostgreSQL full-text (tsvector), fuzzy (trigram), graph traversal, code-graph lookup and scoped memory recall. One base URL, one auth model, one error envelope, one cursor.
---

# Aither Retrieval — one surface, every modality

The AitherOS retrieval surface answers one question and returns fused, ranked
results from every modality you have — vector similarity, PostgreSQL full-text
(tsvector), fuzzy (trigram), graph traversal, code-graph lookup and scoped
memory recall. One base URL, one auth model, one error envelope, one cursor.

Set the base URL and a tenant token before anything else:

```bash
export RETRIEVAL_BASE_URL="https://retrieval.example.com"   # your deployment's URL
export RETRIEVAL_TOKEN="..."                                # tenant Bearer token
```

Every request sends `Authorization: Bearer $RETRIEVAL_TOKEN` and
`X-Aither-Retrieval-Version: 2026-08-31` (the date-versioned contract; an
unsupported value returns `400 version_unsupported`).

## Unified search — the entry point

`POST /v1/search` dispatches across modalities with weights:

```bash
curl -sS "$RETRIEVAL_BASE_URL/v1/search" \
  -H "Authorization: Bearer $RETRIEVAL_TOKEN" \
  -H "X-Aither-Retrieval-Version: 2026-08-31" \
  -H "Content-Type: application/json" \
  -d '{"query": "refund policy", "mode": "hybrid", "weights": {"text": 0.5, "vector": 0.4, "graph": 0.1}, "limit": 10}'
```

Response: `{"success": true, "items": [...], "cursor": "...", "has_more": bool,
"request_id": "...", "metadata": {"api_version": "2026-08-31", "modality": "hybrid"}}`
Each item carries `doc_id`, `score`, `modality`, `title`, `content`, `metadata`.

## Modality endpoints

| Need | Endpoint |
|---|---|
| Semantic similarity | `POST /v1/vector/search` — `{query, collection, limit, filters}` (embeddings generated server-side) |
| Embed one text without storing | `POST /v1/vector/embed` — returns the 768-dim vector |
| Full-text (tsvector) | `POST /v1/text/search` — `{query, limit, filters}` |
| Typo-tolerant short text | `POST /v1/text/search` with `"fuzzy": true` (trigram similarity) |
| Hybrid with weights | `POST /v1/hybrid/search` — `{query, weights: {text, vector, graph}}` |
| Graph traversal | `POST /v1/graph/search` `{query}`; `GET /v1/graph/neighbors/{node_id}`; `GET /v1/graph/path/{source_id}/{target_id}` |
| Code graph | `POST /v1/code/search` — `{query, limit}` over an indexed codebase |
| Scoped memory | `POST /v1/memory/search` — `{query, scope}` |
| Recursive, window-exceeding context | `POST /v1/recurse/answer` — `{question}` (retrieves, then recurses with the trace kept) |
| Sessionful iteration | `POST /v1/session/exec` — `{code, session_id?}`; state survives between turns; close with `POST /v1/session/close` |

## Pagination

Search responses return an opaque `cursor` plus `has_more`. Pass the cursor
back unchanged to get the next page — never hand-edit it. A tampered or
cross-tenant cursor returns `400 cursor_invalid`.

## Writes — idempotent by design

`POST /v1/ingest` writes one document across the modalities you enable, in one
transaction. Always supply an `idempotency_key` — replaying the exact same
payload returns `200` with `"idempotent_replay": true` and does NOT duplicate.
The SAME key with a DIFFERENT payload is a conflict (`409 idempotency_conflict`)
— that is the ambiguous-write guard; never retry a write whose outcome you do
not know, and never reuse a key for different content.

```bash
curl -sS "$RETRIEVAL_BASE_URL/v1/ingest" \
  -H "Authorization: Bearer $RETRIEVAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"document_id": "doc-123", "title": "Refund policy", "content": "Refunds are processed within 5 business days.",
       "collection": "support", "modalities": ["text", "vector"], "idempotency_key": "req-<uuid>", "metadata": {"status": "published"}}'
```

Collection points (rows with payloads, pgContext-style) live under
`/v1/collections/{collection}/points/` — `upsert` (same idempotency semantics),
`scroll` (cursor pagination), `delete`. Bulk cleanup: `POST /v1/documents/delete`.

## Leases before editing retrieved files

When retrieval returns repository paths and the agent will EDIT them, first
acquire a write lease so concurrent agents cannot collide:

```bash
curl -sS "$RETRIEVAL_BASE_URL/v1/leases/acquire" \
  -H "Authorization: Bearer $RETRIEVAL_TOKEN" -H "Content-Type: application/json" \
  -d '{"actor": "agent-1", "targets": ["src/service.py", "tests/test_service.py"]}'
```

A held lease returns `409 lease_conflict` — pick different files or wait.
Release with `POST /v1/leases/release`; heartbeat long sessions with
`POST /v1/leases/heartbeat` so the lease does not expire mid-edit.

## Artifacts

- `POST /v1/artifacts/ingest` — pull an artifact from the object store, chunk
  it and index the content so searches find it.
- `POST /v1/artifacts/ingest-bundle` — ingest everything in a verified bundle.
- `POST /v1/export` — run a search and publish the result set as a verified
  artifact bundle; the response carries the manifest and a content digest you
  can re-check after fetching, so a downloaded result set is provably intact.

## Errors — every failure is a code

All errors share one envelope: `{"success": false, "error": {"code", "message",
"details"}, "request_id": "...", "metadata": {"api_version"}}`. Keep the
`request_id` when reporting a problem — it is the correlation handle.

| Code family | HTTP | Meaning |
|---|---|---|
| `RET-1xxx` | 401/403 | auth: `authentication_required`, `invalid_credentials`, `tenant_scope_denied` |
| `RET-2xxx` | 400 | validation: `validation_error`, `malformed_query`, `cursor_invalid`, `version_unsupported` |
| `RET-3xxx` | 404 | `not_found` |
| `RET-4xxx` | 409 | conflicts: `idempotency_conflict`, `lease_conflict` |
| `RET-5xxx` | 502/503 | backend down: `nexus_unavailable`, `postgres_unavailable`, `graph_unavailable`, `embedding_failed`, `strata_unavailable`, `code_graph_unavailable` |
| `RET-6xxx` | 500/503 | platform: `internal_error`, `not_ready` |
| `RET-4291` | 429 | `rate_limited` (per-tenant cap) |

## Readiness and capability

- `GET /version` — the service and its API contract: `{"service":
  "aither-retrieval", "api_version": "2026-08-31", "protocol": 1}`.
- `GET /ready` — 200 when Postgres, the vector store and the graph backend all
  answer; 503 during startup or degradation.
- `POST /v1/meta` — the live capability manifest: modalities, collections,
  limits and the full error-code table.
