---
name: aither-prospector
description: >-
  A semantic file-explorer that tells agents WHERE to look. Before an agent greps a 4,000-directory monorepo, it should know the three dirs worth grepping. Prospector (Phase 1, the 'landmark map') clusters a codebase into semantic regions — auth, api, data, ui, service — and answers *'where is rate limiting enforced?'* with the directories to search first. It's the cheap scout that makes [CodeGraph](aither-codegraph.md)/grep pay off instead of scanning everything.
---

# aither-prospector — a semantic file-explorer that tells agents WHERE to look

Before an agent greps a 4,000-directory monorepo, it should know the three dirs worth
grepping. **Prospector** (Phase 1, the "landmark map") clusters a codebase into semantic
regions — auth, api, data, ui, service — and answers *"where is rate limiting enforced?"*
with the directories to search **first**. It's the cheap scout that makes
[CodeGraph](aither-codegraph.md)/grep pay off instead of scanning everything.

> This is the capability that feeds CodeGraph: localize → then search inside the hits.

## Give an agent the tools (self-service)

The `prospector` pack is **free** (`tier: free`, no entitlement). Apply it and your agent gains
four `map_*` tools:

```
apply_pack_self("prospector")     # MCP tool — self-service, fail-closed, idempotent
```

- **`map_build(root)`** — scan a codebase and persist its landmark map. Dependency-free (no
  LLM), fast; run once per repo, re-run after big structural changes.
- **`map_localize(question, root, k=5)`** — the explorer: top-k landmarks (dir + purpose +
  representative files) to search first.
- **`map_subsystems(root)`** — the project at a glance: purpose → directories.
- **`map_status(root)`** — is a map built, and how big?

## Use it

```jsonc
map_build(root="/path/to/repo")
// → {ok:true, n_landmarks: 28, by_purpose:{code:11, tooling:8, test:3, auth:2, ...}}

map_localize(question="where are the compression tools?", root="/path/to/repo", k=3)
// → {ok:true, landmarks:[{name:"headroom", purpose:"tooling", rel:"...", files:[...]}], ...}

map_subsystems(root="/path/to/repo")
// → {ok:true, subsystems:{auth:[...], api:[...], data:[...], ...}}
```

If `map_localize` says *"no landmark map"*, run `map_build(root)` first. Every tool is
guarded — a missing map yields a readable hint, never an error.

## How it works

`map_build` walks the tree, keeps only directories with real code/text, tags each with a
purpose (keyword heuristic: `auth`, `api`, `data`, `ui`, `service`, `test`, `config`,
`tooling`, …), and persists an **HMAC-verified snapshot** under `Library/Data/prospector/`.
`map_localize` ranks landmarks against your question with intent-aware scoring (code queries
boost code regions; conversational ones don't). The consumer + ranking are the proven
`lib/cognitive/landmark_map` engine — this pack adds the missing builder and the agent-callable
surface.

**Richer maps (optional upgrade):** the built-in builder is a fast, dependency-free heuristic.
For LLM-described purposes and cross-region edges, point Prospector at a map produced by the
gemma **cartographer** (the external arc-agi-3 `agents/project_map.py` builder) via
`AITHER_PROJECT_MAP_DIR` — same schema, richer semantics. The heuristic builder is enough to
be useful out of the box.

## Localize → search → trace

Prospector and [CodeGraph](aither-codegraph.md) are a pipeline:

1. **Prospector** — `map_localize("where is auth?")` → 3 dirs.
2. **CodeGraph** — `code_search`/`codegraph_search` inside those dirs → exact symbols + call graph.
3. **Act** — read/trace/edit with `code_context` / `codegraph_impact`.

On a large repo that's the difference between one focused pass and scanning the whole tree.

## Part of one substrate

Prospector is the *where*, [CodeGraph](aither-codegraph.md) is the *what*, and
[graph-rag-agent](graph-rag-agent.md) is knowledge over your docs — three layers of the same
[awdk](awdk.md) agent. MIT-licensed, like everything in `awskills`.
