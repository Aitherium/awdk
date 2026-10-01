---
name: graph-a-repo
allowed-tools: rag_detect_hardware, rag_resolve_embedder, rag_plan_embedder, rag_apply_embedder, rag_verify_embedder, rag_ingest, rag_verify_retrieval, codegraph_trigger_index, codegraph_search, codegraph_get_context, search_knowledge, Bash, Read
description: Turn a repository OR a knowledge base into a graph an agent can actually answer from — pick the right embedder, stand it up, ingest, and PROVE both halves work (the vectors are the right dimension AND retrieval returns the ingested content). The operational runbook for the graphrag toolpack; pairs with graph-rag-agent (the toolkit) and aither-codegraph (code structure).
argument-hint: <path-to-repo-or-docs> [--as code|docs] [--agent <name>] [--serve-embedder]

---

## What this does

Takes `$ARGUMENTS` — a repo or a folder of documents — and produces a **queryable
graph**: chunked, embedded, linked, and verified. The emphasis is on the last word.
Most "I graphed my repo" failures are silent: an embedder that answers `200` with
useless vectors, or a graph that answers `200` with zero hits. This runbook makes
you prove neither happened before you hand the graph to an agent.

Tools come from the `graphrag` toolpack (`rag_*`) and CodeGraph (`codegraph_*`).

## Step 0 — Decide: code or prose?

This is the first fork and it is not cosmetic — **code and prose are different
vector spaces.** A text embedder on source gives you keyword-ish matches; a code
embedder captures call/def structure. Never mix them in one graph.

| your material | embedder | why |
|---|---|---|
| a **codebase** (`.py`, `.ts`, `.go`, …) | `code` (CodeRankEmbed) | function/def-level similarity, call graph |
| **docs / a KB** (`.md`, `.pdf`, prose) | `text` (nomic-embed-text, 768-dim) | semantic prose retrieval |
| **both** | two graphs, one per space | querying code with a text embedder returns garbage |

`--as` overrides the guess. If the path is mostly source, default to `code`.

## Step 1 — Stand up (and PROVE) the embedder

Skip this if you're using the fleet's already-running embedder at
`https://…:8209` — just verify it. Otherwise:

```bash
python -m adk.toolpacks.graphrag detect                       # what fits
python -m adk.toolpacks.graphrag apply --embedder text --dry-run
python -m adk.toolpacks.graphrag apply --embedder text        # launches vLLM detached
```

Then the check that actually matters:

```bash
python -m adk.toolpacks.graphrag verify-embedder --embedder text --base-url https://localhost:8209
```

- `healthy` — up **and** returns a vector of the expected dimension (768 for
  nomic-embed-text). Only now proceed.
- `wrong_dimension` (exit 4) — it loaded as the wrong task/checkpoint. The vectors
  would be silently incompatible with the graph store. **Stop and fix** before
  ingesting — everything downstream would be poisoned.
- `degraded`/`unknown` — still loading or not up. Wait, re-check.

**Fleet-parity trap:** `nomic-embed-text` (768-dim) is the fleet's canonical vector
space — its memory/RAG are keyed on it. If you want your graph to interoperate with
the fleet's store, serve *that* embedder, not a different one. A different embedder
= an island.

## Step 2 — Ingest

### Prose / KB

```bash
python -m adk.toolpacks.graphrag ingest --path ./docs --agent research
# or directly: adk ingest ./docs --agent research --chunk-size 1500 --chunk-overlap 200
```

`adk ingest` is local-first and incremental — re-run it when the material changes.
Tune `--chunk-size` up for dense reference material, down for FAQ-style Q&A.

### Code / repo

For code, CodeGraph gives you *structure* (call→definition, import edges) that flat
chunking misses:

```
codegraph_trigger_index    # parse the repo into the structural graph
codegraph_search "how does auth work"   # structure-aware, not string grep
codegraph_get_context <symbol>          # callers/callees around a symbol
```

Use both when you want structure AND semantic retrieval: CodeGraph for the call
graph, `rag_ingest --as code` (CodeRankEmbed) for similarity search over code.

## Step 3 — PROVE retrieval (the step everyone skips)

An empty graph answers `200` with zero hits. A query that matches nothing looks
identical to a broken pipeline. So query for something you **know** you just
ingested, and require it back:

```bash
python -m adk.toolpacks.graphrag verify-retrieval --agent research --sentinel "<a phrase you KNOW is in the corpus>"
# with a fleet RAG endpoint (semantic hit_count instead of local store lookup):
python -m adk.toolpacks.graphrag verify-retrieval --query-url https://…/rag --agent research --sentinel "…"
```

The local check queries the ingest graph store directly (`~/.aither/graph/<agent>.db`,
the SQLite `nodes` table `adk ingest` writes) for your sentinel's distinctive terms —
an honest "is the ingested content in the graph and findable" signal. It does **not**
route through `adk chat` (that targets *mesh* agents, not a local ingest graph, and
its error text would be a false-positive "hit" — a trap this pack was written to
avoid). For semantic ranking, point `--query-url` at a real RAG endpoint.

- `healthy` — retrieval returned the ingested content. The graph works.
- `empty` (exit 2) — zero hits. The graph is empty or your query matched nothing.
  **This is NOT a working RAG.** Re-check that ingest actually ran (Step 2 output),
  that the embedder was `healthy` (Step 1), and that your sentinel really is in the
  material. Do not hand an `empty` graph to an agent.

Pick a sentinel that is specific and unambiguous — a proper noun, an error string,
a function name — not a common word that could match by chance.

## Step 4 — Give an agent the graph

Once retrieval is proven:

```bash
adk create-app "Research Assistant" --description "Answers only from the ingested graph."
adk pack customize research --system-prompt \
  "Answer only from the ingested graph; cite the source chunk. If the graph lacks the answer, say so and name what to ingest."
adk chat research "what does the deploy pipeline do?"   # one-shot query (adk chat <agent> <msg>)
```

Note the query command is **`adk chat <agent> <msg>`** — there is no `adk query`.
The steward system-prompt above matters: told to say "the graph lacks the answer,"
the agent fails honestly instead of hallucinating, and `rag_verify_retrieval` can
read that as `empty`.

## Step 5 — Keep it fresh

The graph is only as current as your last ingest. Re-run `rag_ingest` when the
source changes (it's incremental), and for code re-run `codegraph_trigger_index`.
A scheduled ingest keeps the agent's answers current without a separate pipeline.

## The failure table

| symptom | cause | fix |
|---|---|---|
| agent answers confidently but wrong | queried an `empty` graph | `verify-retrieval` first; never ship `empty` |
| retrieval returns garbage | wrong embedder for the material (text on code) | separate graphs, right embedder per space |
| vectors "don't match the fleet" | served a non-canonical text embedder | use `nomic-embed-text` (768-dim) for parity |
| `verify-embedder` says wrong_dimension | model loaded as generative, not embed | `--task embed`; check the served model id |
| `adk query` not found | that command doesn't exist | for a *mesh* agent use `adk chat <agent> <msg>`; for a *local ingest graph* use `verify-retrieval` (queries the store) |
| `verify-retrieval` says healthy but graph is empty | routed retrieval through `adk chat` (mesh) whose error text counted as a hit | fixed — it queries `~/.aither/graph/<agent>.db` directly; a false-positive here is the exact trap to avoid |
| code search misses obvious callers | used prose retrieval on code | `codegraph_*` for structure |

## The one rule

**Prove both halves before you trust the graph.** A `200` from the embedder and a
`200` from the query prove nothing on their own — only a right-dimension vector and
a retrieval that returns your sentinel prove the graph is real. Everything else is
a graph-shaped object that answers questions wrong.

Pairs with: `graph-rag-agent` (the adk toolkit view), `aither-codegraph`
(code structure), `aither-prospector` (repo landmark map before you ingest).
