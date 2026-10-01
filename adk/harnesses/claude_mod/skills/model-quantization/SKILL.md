---
name: model-quantization
allowed-tools: Bash, Read
description: Quantize an LLM to 4-bit with AutoRound — RTN runs free on local CPU+GPU; keeps lm_head/projectors in bf16 and dodges the new-architecture crashes
argument-hint: <model-id-or-path> [-o OUTDIR] [--iters N] [--keep-fp REGEX ...] [--dry-run]

---

## Context
- GPU present: !`nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "no NVIDIA GPU detected (RTN still works, slower on CPU)"`
- auto-round installed: !`python -c "import auto_round, sys; print(auto_round.__version__)" 2>/dev/null || echo "NOT installed — pip install auto-round"`
- Arguments: $ARGUMENTS

## Your Role
You shrink a large language model to 4-bit so it fits where bf16 won't — on a
smaller GPU, or beside another model on the same card. You drive one tool from
this repo, `tools/quantize_model.py`, which bakes in the gotchas that otherwise
yield a broken or un-loadable artifact.

The default path (**RTN**, `--iters 0`) is weight-only round-to-nearest: no
calibration data, no forward pass, ~<2 GB peak VRAM, ~1 minute. It runs on a
laptop's **CPU + GPU together** (host-RAM offload), costs nothing, and is the
right choice for most models.

## Your Task
1. **Parse `$ARGUMENTS`** for the source model (HF repo id or local path) and an
   output dir. If no model is given, ask which model to quantize.
2. **Preview first.** Run a dry-run so the user sees the plan before any heavy
   load:
   ```bash
   python tools/quantize_model.py <model> --dry-run
   ```
   The plan shows the scheme (4-bit / group_size 128), the mode (RTN vs
   calibrated), the export format, and which modules stay in bf16.
3. **Run RTN by default** (free, local). Pick an output dir if the user didn't:
   ```bash
   python tools/quantize_model.py <model> -o <outdir>
   ```
   - The tool keeps `lm_head`, embeddings, and multimodal projectors/encoders in
     bf16 automatically. For an unusual VLM, add `--keep-fp '<regex>'` (repeatable)
     for any extra module that must stay full-precision.
   - It exports `compressed-tensors` (`llm_compressor`) by default — the safe
     format when modules are left un-quantized.
4. **Only use calibrated AWQ (`--iters > 0`) when the user explicitly wants
   higher quality and has a real GPU.** The tool will *refuse* it on models with
   per-layer-type head dims (alternating sliding/global attention) and tell you
   to fall back to RTN — that's expected; calibrated mode crashes on those, RTN
   doesn't.
5. **Report** the output path and the serve command the tool prints
   (`vllm serve <outdir> --quantization awq_marlin`).

## Key facts to apply (don't re-derive)
- **AutoRound, not llm-compressor** — llm-compressor pins an old `transformers`
  and silently downgrades it until a new-arch model won't load.
- **Quantize only the language decoder** — `lm_head` + multimodal projectors must
  stay bf16 or vLLM's loaders reject them. The tool's defaults already do this.
- **RTN is the safe path for new architectures.** If `auto-round` isn't
  installed, tell the user `pip install auto-round` (and `torch transformers`).
- If there's **no GPU**, RTN still works on CPU — just slower. Calibrated mode
  effectively needs a GPU.

## Always finish with
- One line: model → output dir, scheme, mode (RTN/calibrated), and the serve
  command. If anything was kept in bf16 beyond the defaults, say which.
- If you hit the het-head refusal, state plainly that RTN was used instead and
  why — it is the correct, working choice, not a downgrade.
