---
name: dgx-spark-deepseek-v4
description: >-
  A 284B model at ~30 tok/s on one desk machine. Serve DeepSeek-V4-Flash-0731 — 284B parameters, 13B active, MoE — on a single NVIDIA DGX Spark (GB10, 121 GB unified memory, ARM64), with the vendor's own speculative-decoding drafter attached, and verify it is really working.
---

# dgx-spark-deepseek-v4 — a 284B model at ~30 tok/s on one desk machine

Serve **DeepSeek-V4-Flash-0731** — 284B parameters, 13B active, MoE — on a single
**NVIDIA DGX Spark (GB10, 121 GB unified memory, ARM64)**, with the vendor's own
speculative-decoding drafter attached, and verify it is really working.

Measured on that hardware, from the server's own `timings` block:

| | tok/s | draft acceptance |
|---|---|---|
| Structured output (lists, JSON, CSV) | 30.6 | ~85% |
| Prose | 27.7 | ~60% |
| **Median** | **~30** | — |
| Baseline, no drafter | 12.6 | — |

That is **2.1–2.6× from speculative decoding**, on a 2-bit GGUF, at ctx 32768.

This skill exists because the difference between 12 tok/s and 30 tok/s on this box is
four decisions, and three of them are invisible: get one wrong and the server starts
cleanly, reports healthy, and quietly does less than you think.

## The four decisions

### 1. The quant is chosen by arithmetic, not by a table

The model must be mmap'd and the OS needs page cache to stream it. Leave too little and
the machine does not fail — it **thrashes**, which is worse, because thrashing looks
like slowness right up until nothing responds.

```
UD-IQ2_M  (90.9 GB) + DSpark drafter (10.9 GB) = 102 GB of 121  ->  fits, ~19 GB free
IQ3_XXS   (97.0 GB) + DSpark drafter (10.9 GB) = 108 GB of 121  ->  does NOT fit
```

Measured: the 3-bit quant loaded in **6.5 minutes** with 24 GB of headroom. The same
model plus the drafter was still loading at **21 minutes** with 13 GB.

Vendor guidance says "128 GB machines will need IQ3_XXS and Q8_0". This box reports
**121 GB**. Seven gigabytes decide it. **Trust the number your machine reports, not the
number in the table** — and compute it before launch:

```bash
model_kb=$(du -Lsk "$(dirname "$MODEL_GGUF")" | awk '{print $1}')
draft_kb=$(du -Lsk "$DRAFT_GGUF" | awk '{print $1}')
total_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
echo "need $(( (model_kb + draft_kb) / 1048576 )) GB of $(( total_kb / 1048576 )) GB"
# headroom below ~16 GB -> drop the drafter rather than wedge the box
```

### 2. `--spec-type` is what actually turns speculation on

Passing `--spec-draft-model` **alone** loads cleanly, serves correctly, and drafts
**nothing**. There is no error, no warning and no unhealthy container — the model is
simply 2× slower than you believe. Speculative decoding is opt-in and this flag is the
opt-in:

```bash
--spec-type draft-dspark \
--spec-draft-model /path/to/dspark-DeepSeek-V4-Flash-0731-Q8_0.gguf \
--spec-draft-n-max 3 \
--spec-draft-ngl 99
```

### 3. Use the vendor's drafter

A third-party draft model must share the target's **exact vocabulary**, and `llama.cpp`
hard-fails at load if it does not. DSpark ships with the checkpoint, so it is
vocab-matched by construction. It costs 10.9 GB — which is precisely why decision 1
comes first.

### 4. One tenant owns the box

121 GB serves this model **or** a pair of vLLM tenants — never both; 180 GB does not fit
in 121. Put a single file on disk that says who owns the machine, and have every
watchdog read it and park itself when it loses. Without that arbiter, two stacks start
at boot, the box pages itself to death, and `sshd` starves exactly when you need it.

## Launch

```bash
llama-server \
  -m "$MODEL_GGUF" -ngl 43 \
  --spec-type draft-dspark \
  --spec-draft-model "$DRAFT_GGUF" --spec-draft-n-max 3 --spec-draft-ngl 99 \
  --host 0.0.0.0 --port 8114 \
  --ctx-size 32768 -np 1 --no-context-shift \
  -t 20 -b 1024 -ub 512 -fa on \
  --cache-type-k f16 --cache-type-v f16 \
  --slot-save-path "$KV_DIR" --cache-reuse 256
```

Notes that are not arbitrary:

* **`-np 1`.** Concurrent slots divide the context and compete for the same memory
  bandwidth on a memory-bound MoE. One slot is the fast configuration here.
* **KV stays `f16`.** This architecture is sensitive to KV quantisation; q8/q4 KV is not
  a free win on deepseek4.
* **A cold load takes tens of minutes.** Any watchdog must measure PROGRESS (is the
  process's RSS still growing?), never a flat timer. A timer kills a load that is
  working, the next attempt starts against a cold page cache, and it is slower — forever.

## Verify it — the only proof that counts

A running process is not evidence. **`draft_n > 0` in the server's own timings is:**

```bash
curl -s -m 300 http://localhost:8114/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Count from 1 to 40, one per line."}],
       "max_tokens":200,"temperature":0}' \
  | python3 -c '
import json,sys
t = json.load(sys.stdin)["timings"]
acc = t["draft_n_accepted"] / max(t["draft_n"], 1)
print(f"{t[\"predicted_per_second\"]:.2f} tok/s  draft_n={t[\"draft_n\"]}  accept={acc:.0%}")
if t["draft_n"] == 0:
    sys.exit("draft_n == 0 — speculation is OFF; check --spec-type")
'
```

Read tok/s from `timings`, never from a wall clock: the wall clock includes prompt
processing and your own network, and will quietly flatter or punish the number.

**Report acceptance alongside throughput.** It is strongly prompt-dependent — ~85% on
structured output, ~60% on prose — so a single headline figure taken from one prompt
class is not reproducible by anyone else.

## Long context

The model is capable of 1M context. Serving it is a memory question, not a flag
question: KV grows with context and comes out of the same budget as the weights and the
drafter. Published third-party numbers on this hardware show throughput falling with
context (≈29 → 25 → 19 tok/s from 16k to 256k), so measure your own curve at the context
you will actually use rather than quoting a peak.

## What good looks like

```
predicted_per_second   30.56
draft_n                168
draft_n_accepted       143      -> 85%
model_ftype            IQ2_M - 2.7 bpw
```

## The trade, stated plainly

This is a **2-bit quantisation**. Two-bit is a genuine quality reduction against the
near-lossless 4-bit tier. It is taken deliberately: the drafter's 10.9 GB has to come
from somewhere, and for a fast agentic reasoning loop the speed is worth more than the
last fraction of a point. That is a choice, and you may make it differently.

## Related

* [`omninode-node`](omninode-node.md) — pool hardware across machines when one box is
  not enough.
* Upstream recipes worth reading: Salvatore Sanfilippo's original `ds4`, the MiaAI-Lab
  start-script recipe, and `0xSero/deepseek-v4-flash-0731-spark-sparkinfer`.
