---
name: llm-serving
allowed-tools: llm_detect_hardware, llm_resolve, llm_plan_deployment, llm_apply, llm_register_backend, llm_verify, Read, Bash
description: Install vLLM and serve the AitherOS fleet models (Nemotron-Orchestrator-8B, gemma4-12b, qwen-27b, deepseek-r1-14b) with quantization and serve flags OPTIMIZED to the detected GPU — NVFP4 on Blackwell, AWQ/W8A16 on Ampere, fp8 KV where supported — then prove it with a real chat round-trip.
argument-hint: "[--model orchestrator|perception|reasoner | <recipe-id>] [--dry-run] [--install-vllm]"

---

## Context
- Tools: `llm_*` from the `llm_serving` toolpack (`adk/toolpacks/llm_serving`).
- CLI: `python -m adk.toolpacks.llm_serving {detect|resolve|plan|apply|verify}`.
- The 4 fleet models, by role:
  | role | model | recipe id |
  |---|---|---|
  | orchestrator | Nemotron-Orchestrator-8B (AWQ) | `nemotron-orchestrator-8b` |
  | perception / vision | gemma4-12b | `gemma4-12b` |
  | reasoner | qwen-27b (Qwen3.6-27B, vrfai NVFP4) | `qwen-27b-reason` |
  | reasoner (fallback) | DeepSeek-R1-14B | `deepseek-r1-14b` |
- Request: `$ARGUMENTS`

## The point: quant OPTIMIZED to the hardware

node_bootstrap gives you generic vLLM recipes. This pack does the thing the owner
asked for — it **picks the best quant the GPU can actually accelerate**, per model:

```
GPU name -> arch -> quant formats it accelerates (best first)
  Blackwell (RTX 50xx, DGX Spark GB10, B200)  nvfp4 > awq > fp8 > w8a16
  Hopper    (H100/H200)                        fp8 > awq > w8a16
  Ada       (RTX 40xx, L40)                    fp8 > awq > w8a16
  Ampere    (A100/A6000, RTX 30xx)             awq > w8a16 > bitsandbytes   (NO native fp8)
  Turing    (RTX 20xx, T4)                     awq > bitsandbytes
```

It intersects that with each model's preference and takes the best runnable one.
**It never hand-forces a quant the card can't run** — `nvfp4` needs Blackwell FP4
tensor cores (fails to load on Ampere); native `fp8` KV cache needs Ada+ (silently
falls back on Ampere). When a preference isn't runnable it drops down and WARNS.

Verified live on a 5090 (Blackwell): orchestrator→awq, qwen-27b→**nvfp4**, all with
`fp8_e4m3` KV. Same recipes on an A100 would resolve awq + auto (fp16) KV, with a
warning that fp8 KV isn't supported there.

## The loop

### 1. Detect — which models fit, at what quant?

```bash
python -m adk.toolpacks.llm_serving detect
```

Reports arch and, per model, `fits` + the chosen `quant` + `kv_cache_dtype`.

### 2. Resolve — recipe + optimized serve config

```bash
python -m adk.toolpacks.llm_serving resolve --model orchestrator
```

Returns the effective serve config (quant, kv dtype, gpu-util, max-len, enforce-eager)
and any warnings. A model that doesn't fit is reported as `fits: false` with the
reason — the pack does NOT silently swap to CPU or a smaller model.

### 3. Plan — the exact vllm command

```bash
python -m adk.toolpacks.llm_serving plan --model reasoner
```

Renders the full `vllm serve ...` argv. These carry the fleet's PROVEN args, not
guesses (see traps below).

### 4. Apply

```bash
python -m adk.toolpacks.llm_serving apply --model orchestrator --install-vllm --dry-run
python -m adk.toolpacks.llm_serving apply --model orchestrator
```

Installs vLLM (optional) and launches **detached** (`nohup ... &`) — a vLLM serve
blocks forever, so apply returns immediately with the verify command.

### 5. Verify — a REAL chat, not just /health

```bash
python -m adk.toolpacks.llm_serving verify --model orchestrator --base-url http://localhost:8120
```

`/health` alone is not proof. A vLLM up with the **wrong `--served-model-name`** is a
silent routing dead end — routing keyed on the served name never hits it. Verify:

| status | meaning | exit |
|---|---|---|
| `healthy` | served name matches AND a chat round-trip returns content | 0 |
| `wrong_model` | up and answering, but serves a DIFFERENT name than expected | 4 |
| `degraded` | up but no completion (often still loading) | 2 |
| `unknown` | `/health` didn't answer — not up, or still starting | 3 |

All four proven live: healthy + wrong_model tested against the running fleet
orchestrator on :8199 (serves `aither-orchestrator`).

## The proven serve args (why the traps matter)

Each recipe carries the fleet's real config — do not "clean these up":

- **Nemotron orchestrator** — `--enable-auto-tool-choice --tool-call-parser hermes`.
  It is tool-calling by design; drop these and downstream agent loops get zero tool
  schemas and fabricate output.
- **gemma4-12b** — `--enforce-eager` is **mandatory**. The sliding-window +
  cudagraph-capture path crashes without it. 8K context is deliberate (VRAM budget
  when co-resident with the orchestrator).
- **qwen-27b** — keep `--max-num-seqs` modest (8). This is the model most prone to
  KV-overflow crashloops when fleet-saturated; pushing concurrency or max-model-len
  past what the KV cache fits triggers the documented crash loop.

## Notes

- A weight quant (awq/nvfp4/gptq) is baked into the CHECKPOINT — you cannot serve a
  base FP16 repo with `--quantization awq`. So each recipe carries a `quant_repos`
  map (quant → the real pre-quantized repo), and the optimizer picks the best
  arch-runnable quant **that has a checkpoint**. `base` can be shrunk on-the-fly only
  via bitsandbytes/fp8. All repos verified on HF: nemotron/gemma AWQ,
  `vrfai/Qwen3.6-27B-NVFP4`, casperhansen deepseek-r1-14b AWQ. The reasoner is the
  fleet's REAL model — **Qwen3.6-27B** — and its NVFP4 checkpoint IS published, so
  Blackwell serves NVFP4; `served_name` stays `qwen36-27b-dgx` for routing parity.
- A `quant_args:` map lets a recipe PIN the vLLM `--quantization` per checkpoint,
  because the same logical quant is packaged differently: vrfai's NVFP4 is
  **compressed-tensors**; a modelopt export is **modelopt_fp4** (ModelOptNvFp4Config —
  NOT plain `modelopt`, which is FP8). Verified against vLLM 0.14.1.
- Fleet qwen ~19-22 tok/s = DFlash spec-decode via the AEON image; drafter
  `z-lab/Qwen3.6-27B-DFlash` (3.2GB) is PUBLIC on HF and already on the DGX.
  Standalone NVFP4 ~8.7 tok/s (dense-27B is FP4-compute-bound).
- Registration is fail-closed: `llm_register_backend` needs a real token + Genesis
  URL, never an anonymous POST.
