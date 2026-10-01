---
name: split-inference
allowed-tools: split_detect_topology, split_resolve_recipe, split_plan_deployment, split_apply, split_verify, Read, Bash
description: Shard ONE model across MULTIPLE machines' GPUs with llama.cpp RPC — build with -DGGML_RPC=ON, start rpc-server on the backend hosts, launch the main server with --rpc, and PROVE the split is real instead of a silent local-only fallback. The reference topology is Bonsai-27B across the local 5090 and the DGX Spark over the ~1ms LAN.
argument-hint: "[--recipe bonsai-27b-5090-dgx-rpc|multi-node-rpc-generic|single-node-cuda] [--stage build|main] [--dry-run]"

---

## Context
- Tools: `split_*` from the `split_inference` toolpack (`adk/toolpacks/split_inference`).
- CLI: `python -m adk.toolpacks.split_inference {topology|resolve|plan|apply|verify}`.
- Reference topology: main node = local RTX 5090 (`aither-llamacpp-bonsai`, llama.cpp
  source at `/work`, model `/work/bonsai.gguf`); RPC backend = `spark.local` (DGX Spark).
- Request: `$ARGUMENTS`

## THE CORE LAW

**A server that answers is not proof of a split.**

There are two ways to end up with a perfectly healthy endpoint that ran entirely on
one GPU:

1. The binary was built **without `-DGGML_RPC=ON`** — it has no `--rpc` flag at all.
2. The `--rpc` target was **unreachable** and the run continued local-only.

When the model fits the local card — Bonsai-27B is ~3.6GB against 31GB of 5090 — this
is **completely invisible**. Nothing errors. Tokens come out. It is simply not a split.

`split_verify` exists for exactly this. It asserts an **RPC device is actually
attached** before it will call anything a split:

| status | meaning | exit |
|---|---|---|
| `healthy` | RPC device(s) attached AND inference round-trips | 0 |
| `local_only` | works, but **NO RPC device** — the silent fallback | 4 |
| `degraded` | inference itself failed | 2 |
| `unknown` | could not determine — never reported as local_only | 3 |

Never report `local_only` as a working split.

## The loop

### 1. Topology — what do we actually have?

```bash
python -m adk.toolpacks.split_inference topology
```

Returns local devices, probed RPC backends, `combined_vram_gb`, and crucially
`rpc_capable_build`. Note that combined VRAM counts an RPC device only once it is
**attached** (visible in `--list-devices`) — a merely-reachable TCP port contributes
nothing until the main binary sees it as a device.

The ground-truth signal is `llama-server --list-devices`:

```
Available devices:
  CUDA0: NVIDIA GeForce RTX 5090 (32606 MiB, 30927 MiB free)     <- before
  RPC0[spark.local:50052]: RPC (... MiB free)                     <- after
```

### 2. Resolve

```bash
python -m adk.toolpacks.split_inference resolve
```

With zero reachable backends this correctly picks `single-node-cuda` and tells you
why the split recipes were rejected. That is the resolver working, not failing —
**a split across zero remote nodes is not a split.**

### 3. Build with RPC (compile-time only)

```bash
python -m adk.toolpacks.split_inference apply --recipe-id bonsai-27b-5090-dgx-rpc --stage build --dry-run
```

```
cmake -B build-rpc -DGGML_CUDA=ON -DGGML_RPC=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build-rpc --config Release -j
```

Builds into `build-rpc/` **beside** the existing `build/` so the working non-RPC
binary is never clobbered. Produces `rpc-server` and an `--rpc`-capable
`llama-server`. Budget ~25min for a CUDA build.

### 4. Start the backend(s) — on the backend host

`split_apply` deliberately does **not** SSH into peers. It reports the exact command
to run on each backend host:

```bash
/work/build-rpc/bin/rpc-server --host <PRIVATE_ADDR> --port 50052
```

### 5. Start main + verify

```bash
python -m adk.toolpacks.split_inference apply --recipe-id bonsai-27b-5090-dgx-rpc --stage main
python -m adk.toolpacks.split_inference verify --recipe-id bonsai-27b-5090-dgx-rpc --base-url http://localhost:8080
```

Verification is not optional here. It is the only thing separating a split from a
story about a split.

## Security — rpc-server has NO authentication

`rpc-server` deserializes and executes tensor operations from **any client that can
reach it**. Upstream documents it as unsafe on untrusted networks.

- Bind the **private LAN / AitherNet overlay** interface only.
- Never a public bind, never through a tunnel, never a published Docker port on a
  host with a public NIC.
- `split_plan_deployment` **refuses** to plan a public bind — that refusal is a
  feature; do not work around it.

## Traps

| trap | symptom | fix |
|---|---|---|
| built without `-DGGML_RPC=ON` | `--rpc` is an unrecognised argument | rebuild with the flag |
| backend unreachable | healthy server, **no RPC device** | check rpc-server is up + port open |
| version mismatch | connect-time failure | both sides must be the SAME llama.cpp/ggml build |
| even spread, unequal GPUs | throughput pinned to the smallest card | weight with `--tensor-split` |
| WAN backend | token latency collapses | RPC adds a round trip per op — LAN (~1ms) only |

## Why this matters

This is the **distributed pipeline-parallel inference** primitive: one model served
across pooled GPUs from more than one machine. It is what lets mesh nodes contribute
VRAM to a single large model rather than each running a small one separately.

Which is exactly why the honesty of `split_verify` matters more than the deployment
itself — a mesh built on unverified splits is a mesh that silently isn't one.
