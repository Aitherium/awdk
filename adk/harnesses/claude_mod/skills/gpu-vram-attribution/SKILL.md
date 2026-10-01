---
name: gpu-vram-attribution
description: >-
  When the platform cannot tell you who is using the GPU. You have one GPU and a dozen containers on it. Something is starving. You run nvidia-smi to find out who is holding the memory, and it tells you nothing useful. Now what?
---

# gpu-vram-attribution — when the platform cannot tell you who is using the GPU

You have one GPU and a dozen containers on it. Something is starving. You run `nvidia-smi`
to find out who is holding the memory, and it tells you nothing useful. Now what?

This is the technique for attributing VRAM per-consumer **when every per-process API is
blind** — which, on Windows/WSL2, is all of them.

## First: stop trying to make NVML work

Under WDDM (any consumer Windows GPU, including through WSL2), per-process VRAM is simply
not exposed:

```
nvidia-smi --query-compute-apps=pid,used_gpu_memory
pid, used_gpu_memory [MiB]
1,   [N/A]
104, [N/A]
```

`[N/A]` for **every** pid — on the Windows host *and* inside a GPU container. Windows GPU
performance counters *do* give per-process numbers, but every container collapses into a
single VM worker process:

```
vmwp             24387 MB   <- the ENTIRE container fleet, as one opaque blob
dwm               1791 MB   <- compositor
WindowsTerminal   1015 MB
```

So: **you cannot attribute container VRAM from outside the container.** Do not spend a day
on this. Two hours of probing NVML, `/proc`, and cgroups produce nothing, and the negative
result is the single most useful thing to know going in.

## The insight

The platform can't attribute VRAM, but **every framework already knows its own footprint,
and most of them already publish it.** You don't need a privileged API. You need one
adapter per framework.

The load-bearing example — **vLLM tells you exactly, for free**:

```
vllm:cache_config_info{...,gpu_memory_utilization="0.28",num_gpu_blocks="2211"} 1.0
```

vLLM *preallocates* `gpu_memory_utilization × device_total` at engine init and holds it for
the process lifetime. That is an allocation **contract**, not a sample: `0.28 × 32768 =
9175 MB`, exact, stable, and derivable by scraping a metrics endpoint with **zero changes
to vLLM**. In one real deployment this replaced a hardcoded 20,000 MB placeholder.

## The adapter ladder

Try these in order and take the first that yields evidence:

| rung | evidence | when |
|---|---|---|
| 1. self-report | the process measures itself (`torch.cuda.memory_reserved()`) and serves it on an endpoint | you control the code |
| 2. framework contract | a preallocation the framework guarantees (vLLM's utilization fraction) | the framework reserves up front |
| 3. framework metrics | a sampled figure the service publishes | there is a metrics endpoint |
| 4. identity + measured artifact | the service says *which* model is loaded; you supply that artifact's measured size | the framework publishes nothing |
| 5. declared ceiling | a configured maximum | nothing else worked |

**Rung 4 is the interesting one.** Some servers publish no size at all — llama.cpp's
`/metrics` is often disabled, and even enabled it is throughput-only; `/props` gives
`model_path` and `model_alias` but an empty `model_info`. The trick is to split the
evidence: the *service* tells you which artifact is loaded, and you supply that artifact's
**measured** size (`ls -l model.gguf`). Combined with full offload (`n_gpu_layers` high),
that is a real number.

The interlock is what makes it evidence instead of a guess: **if the loaded model does not
match the one you measured, report nothing.** Without that check you have a hardcoded
allowance that happens to be validated against nothing.

## The discipline that actually matters

Getting numbers is the easy half. These four rules are what keep the numbers honest, and
each of them exists because its absence caused a real failure.

**1. Every number carries its method and confidence.** The defect is never "the number was
missing" — it is *"a guess was indistinguishable from a measurement"*. A consumer sitting
on a placeholder must be visibly distinct from one that measured itself.

**2. A declared ceiling is NEVER counted as attributed.** Report it so the consumer is
visible; exclude it from the coverage figure. Counting configured maximums as if they were
measurements is precisely how a fleet with 63% of its GPU invisible presented as having a
complete, healthy budget.

**3. Over-attribution is a FAILURE, not a good score.** If attributed exceeds device-used,
the numbers cannot all be true. Guard for it explicitly — otherwise coverage reads 121%,
sails past your 60% floor, and reports **OK**. A broken measurement *satisfying* the check
is worse than no check. Three benign causes before you accuse anyone:
- **sampling skew** (most likely — consumer reads and device totals are different instants)
- **`memory_reserved` vs `memory_allocated`** — reserved includes the allocator's cached
  pool. That is the *right* number for an arbiter (it is genuinely unavailable to others),
  but it is far larger than the live model, so a small model legitimately reports gigabytes
- **double counting** — two services registered under one consumer id

**4. Set a coverage FLOOR, not a target.** 100% is unreachable: driver, compositor and
context overhead are real VRAM belonging to no consumer (~2.8 GB on a desktop Windows box).
The floor asserts the arbiter can see *enough to decide*. The failure mode is silent drift
downward, which is why this must be a continuously-checked threshold rather than a number
someone reads once.

## Traps worth knowing before you hit them

- **A single-shot probe measures whether DNS worked, not whether evidence exists.** If a
  failed fetch degrades to a ceiling, one transient lookup failure silently converts a
  measurement into a guess. Retry every probe.
- **One shared dependency can disable a whole adapter class.** `utilization × total` needs
  the device total; if that single lookup times out, *every* framework-contract consumer
  collapses to a ceiling and coverage drops to 0% — indistinguishable from "nothing is
  attributable". Harden it and source it from more than one place.
- **Service discovery can override a correct literal with an unresolvable one.** A name
  lookup that returns a hostname the container does not actually answer to fails exactly
  like a down service. Prefer the literal you verified.
- **Check the scheme and port against the service's own healthcheck**, not against what
  you assume. A service on `https://…:8212` probed as `http://…:8123` looks identical to
  one that publishes nothing.
- **A consumer that answers with `is_instrumented: false` measured nothing.** Treat it as
  absent evidence, not as a legitimate zero, or you launder a 0 into your coverage.

## Where this pays off

Attribution is not reporting for its own sake — an eviction plane acts on the consumer map,
so **anything invisible there is also unreclaimable**. A caller asks for room, is told
there is none, and nothing indicates that most of the GPU cannot be seen. Closing the gap
turns "the GPU is full" into "*this* service is holding 9 GB and here is the lever to
reclaim it".

Applied to one real fleet, the ladder above took a GPU from **63% unattributed to under 1%**,
with no changes to the services being measured.
