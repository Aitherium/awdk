---
name: aither-codegraph
description: >-
  A call-graph-aware code index your agents can query. Grep finds strings; CodeGraph finds *structure*. It parses a codebase into chunks (functions, classes, routes) with a real call graph — who calls what, who's affected by a change — and lets an agent ask 'where is auth enforced?' and get the symbol, its signature, its callers, and its callees. It's the difference between an agent that greps and one that understands the code.
---

# aither-codegraph — a call-graph-aware code index your agents can query

Grep finds strings; **CodeGraph** finds *structure*. It parses a codebase into chunks
(functions, classes, routes) with a real **call graph** — who calls what, who's affected
by a change — and lets an agent ask "where is auth enforced?" and get the symbol, its
signature, its callers, and its callees. It's the difference between an agent that greps
and one that understands the code.

Three ways to run it, cheapest first: the **`awgraph` package** (pip, no
infrastructure), **adk-native** (your agent runtime), and the **fleet service**
(the managed graph with MCP tools).

## Zero infrastructure — `pip install awgraph`

The engine is published as [`awgraph`](https://pypi.org/project/awgraph/), Apache 2.0.
Nothing to deploy:

```bash
pip install awgraph

awgraph index .                          # one-time per repo, cached OUTSIDE it
awgraph query "retry with exponential backoff"
awgraph callers send_request             # who calls this
awgraph calls send_request               # what does this call
awgraph stats                            # incl. embedding coverage
```

`query` prints `path:line  [type] name` plus the signature, so a hit pastes straight
into an editor. `--json` on any read command wires it into a tool loop. Exit codes
distinguish **0** success, **1** nothing matched, **2** could not run — so a script can
tell "no results" from "there is no index yet", which are different problems.

### Give it to your coding agent (MCP)

```bash
pip install "awgraph[mcp]"
```

then one line in the MCP config of Claude Code, Cursor, Windsurf or Zed:

```json
{"mcpServers": {"awgraph": {"command": "awgraph", "args": ["mcp"]}}}
```

Your agent gains `code_index`, `code_search`, `code_callers`, `code_calls` and
`code_stats`. It searches by meaning and gets back symbols with file, line, signature,
calls and callers — instead of pasting file text into its own context, which is the
cost this removes.

Index once (`code_index`); indexing is never implicit, because a silent multi-minute
first call reads as a hung tool and usually gets killed. A search against an unindexed
repo tells the agent to index rather than returning empty — "nothing matched" and
"nothing is indexed" have different fixes, and an agent that cannot tell them apart
concludes your repository does not contain the thing and stops.

### What it costs, measured

Over 33 real commits, with the result budget `k` swept for **both** retrievers:

| k | awgraph recall | tokens | grep recall | tokens | cheaper |
|---|---|---|---|---|---|
| 10 | 0.803 | 1,311 | 0.924 | 351,427 | 268x |
| 25 | 0.939 | 3,132 | 0.985 | 504,640 | 161x |
| 400 | **1.000** | 45,269 | 1.000 | 735,727 | **16x** |

awgraph reaches the same ceiling as exhaustive grep for **16x less context**. Read it
honestly: grep is the better *finder* at any matched `k` — it hits 1.000 at k=50 while
awgraph is at 0.939. The argument is that grep needs 668k tokens per task at full
recall, which does not fit in most context windows at all.

Embeddings are optional and worth it at small `k`: ablated on the same tasks,
0.682 -> 0.803 at k=10 and 0.818 -> 0.939 at k=25, narrowing to +0.030 by k=50. Without
a backend, search silently degrades to keyword-only and still returns confident
results — which is why `awgraph stats` always prints coverage.

## adk-native — index your own codebase in one command

`adk` auto-indexes a Python project when you run it there and attaches two tools to your agent:

```bash
pip install awdk
cd /path/to/your/python/project
adk run            # detects Python → "Indexing N files... M chunks in Xs" → tools attached
adk chat           # now ask: "where is rate limiting enforced?"
```

Your agent gains:
- **`code_search(query, max_results=10)`** — natural-language/keyword search → chunks with
  `name`, `type`, `file`, `line`, `signature`, `calls`, `called_by`.
- **`code_context(chunk_id)`** — full context for a chunk: signature, docstring, callers,
  callees, and the source body.

Programmatically (build it into your own agent):

```python
from adk.faculties.code_graph import CodeGraph
cg = CodeGraph()
stats = await cg.index_codebase("/path/to/repo")   # {'total_chunks': ..., 'total_files': ...}
agent.set_code_graph(cg)                            # registers code_search + code_context
```

Re-run `index_codebase()` (or `adk run`) after significant changes — the index is a snapshot,
not live.

## Fleet service — the managed CodeGraph (`aitheros-cognition-advanced:8153`)

In an AitherOS fleet, CodeGraph is a router on the `aitheros-cognition-advanced` compound
service (port **8153**, path `/codegraph`), persisted under `Library/Data/codegraph` (an
`index.json` + embeddings). It's in the default `core` profile.

**Configure / mount a repo to index** (compose):
```yaml
# docker-compose: give the service your repo read-only
volumes:
  - /path/to/your/repo:/host/external/your-repo:ro
# env
AITHER_CODEGRAPH_URL: https://aitheros-cognition-advanced:8153   # override discovery
AITHER_CODEGRAPH_RECONCILE: "0"                                  # off = no disk-storm reindex
```

**HTTP API** (behind the internal CA):
```bash
POST /codegraph/index         {"root_path":"/host/external/your-repo","force":false}   # (re)index
GET  /codegraph/search?q=...                                                           # search
GET  /codegraph/context/{chunk_id}                                                     # callers/callees
GET  /codegraph/chunk/{chunk_id}/code                                                  # exact source
GET  /codegraph/stats                                                                  # index size
GET  /codegraph/impact/{symbol}                                                        # blast radius
GET  /codegraph/impact-commit/{sha}                                                    # commit blast radius
```

**MCP tools** (exposed on the awnode gateway — agent-callable):
`codegraph_search`, `codegraph_context`, `codegraph_get_code`, `codegraph_trigger_index`,
`codegraph_explore`, `codegraph_impact`, `codegraph_affected_tests`, `codegraph_impact_commit`,
`codegraph_routes`, `codegraph_get_stats`.

**Keep it fresh:** there is no auto-reindex by default — call `codegraph_trigger_index`
(or `POST /codegraph/index`) after changes, or wire it to a routine/CI step.

## Pair it with Prospector (find the dirs first)

CodeGraph is precise but indexes everything. On a big repo, narrow the search *first* with the
[`aither-prospector`](aither-prospector.md) file-explorer — `map_localize("where is auth?")`
returns the handful of dirs to look in, then let CodeGraph resolve the exact symbols and call
graph inside them. Localize → search → trace.

## Part of one substrate

CodeGraph is the code-structure layer under [awdk](awdk.md); pair it with
[graph-rag-agent](graph-rag-agent.md) (knowledge over your *docs*) and
[aither-prospector](aither-prospector.md) (semantic *file-explorer*) for an agent that knows
both your code and your knowledge. MIT-licensed, like everything in `awskills`.
