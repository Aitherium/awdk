---
name: graph-rag-agent
description: >-
  Build a knowledge graph and an agent that owns it. Point [awdk](awdk.md) at a folder of documents (or a codebase) and it builds a knowledge graph — chunked, embedded, and linked — then give a dedicated agent that graph as its memory so it answers from *your* material instead of guessing. This is graph RAG without standing up a vector database yourself: ingestion, storage, retrieval, and the agent are one toolkit.
---

# graph-rag-agent — build a knowledge graph and an agent that owns it

Point [awdk](awdk.md) at a folder of documents (or a codebase) and it builds a
**knowledge graph** — chunked, embedded, and linked — then give a dedicated agent that graph as its
memory so it answers from *your* material instead of guessing. This is graph RAG without standing up
a vector database yourself: ingestion, storage, retrieval, and the agent are one toolkit.

## Build the knowledge graph

With awdk installed (`pip install awdk`):

```bash
# Ingest a folder into a named agent's knowledge graph (chunk + embed + link).
adk ingest ./docs --agent research

# Tune chunking for dense material; ingest a whole codebase for code-aware RAG.
adk ingest ./docs --agent research --chunk-size 1500 --chunk-overlap 200
adk ingest ./src  --agent research      # code is graphed too (call/def relationships)
```

`adk ingest` is **local-first** — the graph stays on your machine. Re-run it any time to add or
refresh material; it's incremental.

## Create the agent that manages it

Scaffold an agent and give it a purpose, then run it — it automatically gets the knowledge tools
(`recall` / `remember` graph-memory, `search_knowledge`, `list_knowledge_bases`, and code-graph
lookups) bound to the graph you built:

```bash
adk create-app "Research Assistant" --description "Answers from our ingested docs and code."
adk run --agents research         # run the agent with the graph as its memory
adk chat research                 # ask it questions — it retrieves from the graph, then answers
```

To shape how it uses the graph, set its system prompt (survives updates, written to an overlay):

```bash
adk pack customize research --system-prompt \
  "You are our knowledge steward. Answer only from the ingested graph; cite the source chunk. If the graph lacks the answer, say so and suggest what to ingest."
```

## Keep it fresh

The agent *manages* the graph, not just reads it: it can `remember` new facts as it learns them, and
you re-`ingest` when the source material changes. A simple loop — ingest on a schedule, let the agent
answer and record — keeps the knowledge current without a separate pipeline.

```bash
adk ingest ./docs --agent research     # re-run after docs change (incremental)
adk chat research                       # "what changed since last week?" — it recalls
```

## What you get

- **Graph, not just chunks** — retrieval follows links (doc→section, call→definition), so answers
  pull in related context a flat vector search would miss.
- **Local by default** — your material never leaves the box unless you choose to sync it.
- **One agent, one graph** — `--agent <name>` scopes each graph, so you can run separate assistants
  over separate corpora on the same machine.

## Part of one substrate

The agent runs on [awdk](awdk.md), the graph lives on an [awnode](aithernode.md) you
own, point it at a local model (see [bonsai-27b](bonsai-27b.md) for a CPU-friendly one), and share
the assistant across a fleet over [AitherMesh](aithermesh.md).

MIT-licensed, like everything in `awskills`.
