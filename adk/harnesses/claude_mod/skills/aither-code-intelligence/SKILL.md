---
name: aither-code-intelligence
description: >-
  Stand up agent code-search, and prove it actually works. Three layers make an agent stop reading files it doesn't need: [prospector](aither-prospector.md) (*where* to look), [codegraph](aither-codegraph.md) (*which symbols*, and who calls them), [headroom](aither-headroom.md) (*shrink what's left*). Each has its own skill. This one is about running them for real — because the hard part isn't setup, it's noticing when they've quietly stopped working.
---

# aither-code-intelligence — stand up agent code-search, and prove it actually works

Three layers make an agent stop reading files it doesn't need:
[**prospector**](aither-prospector.md) (*where* to look), [**codegraph**](aither-codegraph.md)
(*which symbols*, and who calls them), [**headroom**](aither-headroom.md) (*shrink what's left*).
Each has its own skill. **This one is about running them for real** — because the hard part
isn't setup, it's noticing when they've quietly stopped working.

> Every failure documented below was found on a live fleet, and **every single one reported
> healthy while it was broken.** Not one raised an error. That is the whole reason this
> skill exists.

## The 60-second health check

Run these four. Any one of them failing means your agents are silently searching a lie.

```bash
# 1) Is the index non-empty AND does it have vectors?
curl -sk https://localhost:8153/codegraph/stats
# → {"available":true,"chunks":59128,"embedding_coverage":0.99,...}
#   ⚠️ embedding_coverage: 0.0 with chunks > 0 = semantic search is DEAD.
#      Retrieval silently degrades to keyword-only. Nothing errors.

# 2) Do indexed paths still EXIST in the running container?
curl -sk https://localhost:8153/codegraph/search?q=YourCoreClass | head
# ⚠️ If source_path shows a layout you no longer deploy (e.g. /app/old-root/services/...
#    when the code is at /app/services/...), every lookup misses. Index looks huge, hit rate ~0.

# 3) Does a symbol you KNOW exists come back?
curl -sk -X POST https://localhost:8153/codegraph/context-for-task \
  -H 'Content-Type: application/json' -d '{"task":"How does <YourClass> do <X>","max_results":3}'
# ⚠️ A positive assertion. "Returns nothing" passes trivially when the feature is inert.

# 4) Does it SURVIVE a restart?
docker restart <codegraph-container>
docker logs <codegraph-container> | grep "Loaded .* chunks from index"
# ⚠️ If the count reverts to an older number, your rebuild was never persisted.
```

Check 4 is the one everybody skips, and it's the one that bites. A reindex can report
`indexed: true, chunks: 96587` and be **gone on the next recreate**.

## Failure modes, with the symptom you'll actually see

Every row is a real bug, its real symptom, and the check that catches it.

| What you see | What's actually wrong |
|---|---|
| Index has 100k+ chunks, hit rate near zero | Chunks hold **paths that no longer exist**. Container layout changed under a stale index. |
| Reindex says `persisted: false` | The API refuses to persist a **scoped** root. Passing an explicit path can silently mean "don't save". |
| Index reverts to an old snapshot every recreate | The rebuild wrote to a path the service never reads (see *mount traps*). |
| `embedding_coverage: 0.0` right after a successful reindex | Embeddings are keyed by **chunk id**. A reindex mints new ids → **every vector orphaned**. |
| Embed job dies ~6 min in, "spontaneously restarts" | **OOM.** `OOMKilled=false` lies — Docker flags only the cgroup's *main* process; a Python `MemoryError` exits **0** and the restart policy resurrects the service. |
| Embed never finishes no matter how many times you retry | Save happens only at the **end**. Each crash loses 100%. Without checkpointing it can never converge. |
| Every result appears twice | Image COPYs `lib/` to two paths for import compatibility → each file indexed twice → **top-10 is really top-5**. |
| Indexer container restarts forever, never completes | Its own work blocks the event loop → healthcheck times out → **your self-healer kills it**. The work that makes it look unhealthy *is its job*. |
| `git blame`/history enrichment returns nothing | `.git/config` references a `blame.ignoreRevsFile` that doesn't exist → every blame aborts **exit 128**. |

### The two that will cost you a day

**Mount traps.** A hard-coded `Path(__file__).parent.parent.parent / "Library"` resolves
differently once the container layout changes. `os.makedirs()` then *silently creates the
wrong directory* in the container's ephemeral layer, so writes vanish on recreate and reads
return empty. Never hard-code a data path — derive it, and assert the directory is on a
mount:

```bash
docker inspect <container> --format '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}'
docker exec <container> python -c "import os;print(os.path.isdir('<your/data/path>'))"
```

**Self-healers vs. slow jobs.** Any container whose *normal* work blocks its health endpoint
for minutes — indexers, embedders, migrations — must carry a tolerant healthcheck **or** opt
out of auto-recovery. Otherwise self-healing turns a slow job into an infinite restart loop
that can never finish:

```yaml
healthcheck: { interval: 60s, timeout: 30s, start_period: 120s, retries: 20 }
labels:
  aitheros.selfheal: "false"   # opt out; coverage is opt-OUT
```

## Retrieval quality: what actually moved the number

Measured on one repo, 10 ground-truth queries. Useful as *shape*, not as a benchmark to quote.

| change | F1 |
|---|---:|
| stale-path index | 0.083 |
| reindexed on current paths | 0.293 |
| + Reciprocal Rank Fusion | **0.470** |

**The RRF finding generalizes.** Hybrid search fails when the two signals live on different
scales. Keyword scored as reciprocal rank (`1.0, 0.5, 0.33` — steep) mixed with semantic
scored as `sim / max_sim` (`1.0, 0.98, 0.96` — flat, because code-embedding cosines cluster
tightly) makes the semantic term a **flat membership bonus** rather than a ranking signal:
everything in the candidate pool gets ~the same score, and semantically-adjacent chunks
bulldoze exact matches out of the top-N. Put both on `w / (k + rank)` (k≈60). If your
"architectural" queries score exactly zero, this is why — they route to pure-semantic, which
is pure-flat, i.e. an arbitrary ordering of near-ties.

Two corollaries worth internalizing:

- **Enabling embeddings can make retrieval worse.** Ours dropped F1 from 0.293 to 0.229
  before the fusion was fixed. Measure; don't assume.
- **Re-tune weights after fixing fusion.** Any weights grid-searched against a broken scale
  are compensating for a bug that no longer exists.

## Wiring it up

```bash
apply_pack_self("prospector")   # free — map_build / map_localize
apply_pack_self("headroom")     # free — headroom_compress / headroom_stats
# codegraph: adk auto-indexes a Python project; fleet path is the managed service.
```

Order matters, and it's the same move at three altitudes — *answer a narrower question
first*. Which directories, before which files. Which symbols, before which bytes. What's
redundant, before what's sent.

## Know the ceiling

- **CodeGraph is Python-only by architecture** — it uses `ast.parse`, which cannot read
  TS/TSX/Go/Rust. Widening the extension list yields parse failures, not coverage. Real
  multi-language needs tree-sitter, or a separate multi-language indexer for breadth.
- **Compression is lossy above ~50%.** High ratios drop unique lines. Never point it at code
  without a needle test — see [`aither-headroom`](aither-headroom.md).
- **Small ground-truth sets lie.** Per-category F1 from 1–3 queries each is noise wearing a
  number's clothes.

## The actual lesson

Nine of the failures above were *silent*. `available: true`, healthy containers, green
checks, plausible chunk counts — and retrieval quietly returning nothing useful. Fail-closed
paths that always return empty pass every "returns nothing" assertion trivially.

**So assert something positive.** A known symbol resolves. Coverage is non-zero. The count
survives a restart. If your monitoring can't tell "working" from "inert", it will report
inert as healthy indefinitely — ours did, for months.

MIT-licensed, like everything in `awskills`.
