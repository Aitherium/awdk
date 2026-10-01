---
name: pooled-inference
description: >-
  Run a model no single box can hold, across every tier you own. A 284B sparse MoE at Q4 is ~155 GB. No consumer box holds that. But a desktop with 128 GB DDR5 + a 32 GB GPU, plus a 128 GB unified-memory box, holds it three times over — if you can spread it. llama.cpp's RPC backend does exactly that, and it works.
---

# pooled-inference — run a model no single box can hold, across every tier you own

A 284B sparse MoE at Q4 is ~155 GB. No consumer box holds that. But a desktop with
128 GB DDR5 + a 32 GB GPU, plus a 128 GB unified-memory box, holds it three times over —
if you can spread it. `llama.cpp`'s RPC backend does exactly that, and it works.

What follows is the set of things that cost real hours to learn. Every claim here was
measured on live hardware, and most of them are invisible to the obvious check.

## The one that decides everything: capacity vs bandwidth

A sparse MoE **holds** 155 GB but **reads** only ~7 GB per token — 6 of 256 experts fire.
Capacity and bandwidth decouple, which is the entire reason a slow tier can hold expert
weights and a pool can beat "doesn't run".

Corollary: **the coordinator is the node with the fastest weight-read path and the most
usable memory — NOT the biggest GPU.** Measured on one fleet: the box with the 32 GB GPU
reached its model through a Windows bind mount at **64 MB/s**; the other box read its own
NVMe at **6.9 GB/s**. Same model, ~100x apart, and both are physically NVMe. Electing on
GPU size picks the wrong coordinator and you eat a 40-minute load.

## An RPC backend is a remote EXECUTOR, not remote memory

Read the source before you cost anything: `rpc_server::graph_compute`
(`ggml/src/ggml-rpc/ggml-rpc.cpp`) receives a serialized node/tensor graph and **runs it on
the remote node**. So:

- **Weights cross the wire once, at load.** Per-token traffic is one hidden-state vector,
  ~14 KB.
- **Link bandwidth is a LOAD-time cost, not a decode-time cost.** Measured 142 MB/s over
  direct LAN vs 18 MB/s through an SSH tunnel: 8x on load time, ~nothing on steady-state
  throughput.
- **Per-token link cost is round-trip LATENCY x layer boundaries**, not bytes. 20 remote
  layers at 1 ms LAN RTT is ~20 ms/token — fine. The same 20 boundaries at 100 ms WAN RTT
  is 2 s/token. That, not bandwidth, is why you never split across a WAN.

So place experts wherever there is free **fast** memory. NVMe at 6.9 GB/s is ~40x worse
than DDR5; an expert that would otherwise page from disk is better off resident in a
*remote* node's RAM, even across 1 GbE. **Capacity is the binding constraint, not the link.**

## `ggml-rpc-server` serves exactly ONE client, and llama.cpp does not retry

This pair causes nearly every mysterious failure.

The accept loop is `accept() -> rpc_serve_client() BLOCKS until that client disconnects ->
loop`, with a listen backlog of 1. And `get_socket()` calls `ggml_abort()` when HELLO goes
unanswered — killing a coordinator that may have spent 17+ minutes loading.

The error it prints is **actively misleading**:

```
ggml-rpc.cpp:345: Remote RPC server crashed or returned malformed response
```

The backend is healthy. It is *busy*. That text will send you chasing stale sessions, a
wedged container runtime, and OOM — all wrong.

**The rule: retry the LAUNCH, and restart the BACKENDS between attempts.** Relaunching the
coordinator alone meets the same lingering session every time. A backend restart returns
it to `accept()` deterministically instead of waiting an unknown TCP drain. Measured:
launches that aborted repeatedly succeeded on the *first* try after a backend restart.

**And do not preflight immediately before launching.** A handshake probe occupies the
single client slot; the coordinator behind it then starves. Preflight to DIAGNOSE, restart
to REMEDY.

## `-ot` syntax, and the naming trap that wastes a run

`-ot`/`--override-tensor` maps a tensor-name regex to a buffer type. The buffer type for a
remote backend is the **full endpoint**, not the device name:

```
Available buffer types:            Available devices:
  CPU                                CUDA0: RTX 5090
  CUDA0                              RPC0:  192.168.1.112:50053
  RPC0[192.168.1.112:50053]        <-- what -ot needs
```

Devices enumerate `RPC0`, `RPC1`, … but **buffer types are ALL `RPC0[endpoint]`**,
disambiguated only by the bracketed address. Writing the natural `RPC1[...]` fails with
"unknown buffer type".

Placement by layer range, not by expert index — expert-name regexes match nothing:

```bash
-ot 'blk\.(3[7-9]|4[0-2])\.ffn_(gate|up|down)_exps\.=RPC0[10.0.0.2:50054]'  # -> GPU tier
-ot 'blk\.(1[3-9]|2[0-6])\.ffn_(gate|up|down)_exps\.=RPC0[10.0.0.2:50055]'  # -> RAM tier
-ot '\.ffn_(gate|up|down)_exps\.=CPU'                                        # remainder
```

Read the layer count from the GGUF header (`<arch>.block_count`) rather than guessing — a
wrong count silently produces rules that match nothing, and llama.cpp does not warn.

## Traps that each cost a run

- **The target is `ggml-rpc-server`**, not `rpc-server`.
- **Use `-c`** (local tensor cache) on every backend or each coordinator restart re-ships
  tens of GB. The cache survives a container restart; it does not survive `docker rm`.
- **A CPU-only backend still needs `--gpus all`** — the binary links CUDA regardless of
  `-d CPU`, and without it `libcuda.so.1` is a broken stub ("file too short").
- **Coordinator and every backend must be built from ONE commit.** The protocol has a major
  version and a mismatch is rejected at handshake.
- **`-ngl 99` aborts** rather than placing what fits: "n_gpu_layers already set by user,
  abort". Pass a real number, or omit it and let auto-fit decide.
- **Free VRAM is not total VRAM.** A desktop session can hold ~11 GB that no teardown
  reclaims. Budget against measured free memory.
- **On unified-memory boxes, `nvidia-smi --query-gpu=memory.used` returns `[N/A]`** and CUDA
  allocations are invisible to both `ps` RSS and container memory stats. One fleet had 61 GB
  of a 121 GB box held by inference containers showing ~470 MiB each. Measure with `free`,
  and check `--query-compute-apps=pid,used_memory` for the per-process truth.
- **A publish is not a reachable port.** Prove reachability FROM THE PEER, never from
  localhost — a default-deny host firewall silently blocks the whole pool.
- **A container runtime's userland port proxy may not sustain two concurrent long-lived RPC
  sessions** even when brief probes pass. If a two-backend pool aborts while each backend
  works alone, route one around the proxy.

## Verify with something that can fail

Throughput without a coherence assertion is worthless — a config emitting fast noise scores
as the winner. Assert a deterministic prompt with a known substring, plus a repetition
floor, and measure **prefill and decode separately**: they are bound by different resources.

Two gates:

```
pooled_best >= single_node_best * 1.10   else the pooling is not earning its complexity
output must be COHERENT                  else FAIL regardless of tok/s
```

Exit non-zero on failure and **exit 2 when a config could not be measured** — a config that
did not run is never a pass.

## Security, before anyone else joins

`ggml-rpc-server` is **unauthenticated**: anyone who can reach the port executes tensor ops
on your hardware. Bind it to RFC1918/CGNAT/loopback only, never a public interface, and put
an overlay ACL in front before a stranger's node participates.

## What to expect

At 13B activated params and Q4, roughly ~7 GB read per token:

| expert tier | effective bandwidth | predicted decode |
|---|---|---|
| host DDR5 | ~60 GB/s | ~8.5 tok/s |
| unified LPDDR5X | ~180 GB/s | ~25 tok/s |
| NVMe spill | ~6 GB/s | <1 tok/s — avoid |

Measured first working pool: **2.25 tok/s decode**, bottlenecked on expert paging from NVMe
— which is the number that tells you resident memory, not more GPU, is the lever.
