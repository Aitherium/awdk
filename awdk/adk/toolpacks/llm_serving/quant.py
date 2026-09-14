"""Hardware-aware quantization optimizer for vLLM serving.

This is the piece node_bootstrap does NOT have: given a GPU and a model, pick the
BEST quantization the hardware can actually accelerate, plus the vLLM flags that go
with it (kv-cache dtype, dtype, whether enforce-eager is needed).

The mapping is derived from the AitherOS fleet's own proven serving configs:
  * Nemotron-Orchestrator-8B  -> AWQ-4bit + fp8_e4m3 KV on Blackwell/Ada/Hopper
  * gemma4-12b                -> AWQ-4bit (Blackwell) / W8A16 (Ampere) + fp8 KV
  * qwen-27b (reasoner)       -> NVFP4 on Blackwell (DGX), else AWQ-4bit
Never guess a quant the card cannot run: e.g. NVFP4 needs Blackwell FP4 tensor
cores; native fp8 KV needs Ada+ ; on Ampere those silently fall back or fail.
"""

from __future__ import annotations

import re

# GPU architecture -> the quant formats it can ACCELERATE, best first.
# sm capability in comments for reference.
ARCH_QUANTS = {
    "blackwell": ["nvfp4", "awq", "fp8", "w8a16", "bitsandbytes"],   # sm_100/sm_120
    "hopper":    ["fp8", "awq", "w8a16", "bitsandbytes"],            # sm_90
    "ada":       ["fp8", "awq", "w8a16", "bitsandbytes"],            # sm_89
    "ampere":    ["awq", "w8a16", "bitsandbytes"],                   # sm_80/sm_86 (no native fp8)
    "turing":    ["awq", "bitsandbytes"],                            # sm_75
    "unknown":   ["awq", "bitsandbytes"],
    "cpu":       ["gguf"],
}

# Native fp8 KV cache needs Ada or newer. On Ampere/Turing it is unsupported and
# must fall back to auto (fp16) — forcing it is a silent perf/accuracy trap.
_FP8_KV_ARCHS = {"blackwell", "hopper", "ada"}


def classify_arch(gpu_name: str, gpu_vendor: str = "nvidia") -> str:
    """Map a GPU name to a coarse architecture family.

    Name-based because the hardware probe does not expose compute capability.
    Unknown NVIDIA cards default to 'ampere' (the safe modern baseline: AWQ + W8A16,
    no native fp8) rather than assuming Blackwell features that would fail.
    """
    if gpu_vendor != "nvidia":
        return "cpu" if gpu_vendor in ("none", "", "cpu") else "unknown"
    n = (gpu_name or "").lower()

    # Blackwell: RTX 50xx, B100/B200, GB10 (DGX Spark)
    if re.search(r"\brtx\s?50\d0\b", n) or "b200" in n or "b100" in n or \
       "gb10" in n or "dgx spark" in n or "blackwell" in n:
        return "blackwell"
    # Hopper: H100/H200/GH200
    if "h100" in n or "h200" in n or "gh200" in n or "hopper" in n:
        return "hopper"
    # Ada: RTX 40xx, L40/L4/L40S
    if re.search(r"\brtx\s?40\d0\b", n) or "l40" in n or re.search(r"\bl4\b", n) or "ada" in n:
        return "ada"
    # Ampere: A100/A6000/A5000/A40, RTX 30xx
    if re.search(r"\brtx\s?30\d0\b", n) or re.search(r"\ba(100|40|6000|5000|4000)\b", n) or \
       "ampere" in n:
        return "ampere"
    # Turing: RTX 20xx, T4
    if re.search(r"\brtx\s?20\d0\b", n) or re.search(r"\bt4\b", n) or "turing" in n:
        return "turing"
    return "ampere"  # safe modern default — never assume Blackwell


# Quants that can be applied ON-THE-FLY to a `base` (unquantized) checkpoint.
# awq/gptq/nvfp4 CANNOT — they require a pre-quantized checkpoint. This is the
# distinction that makes `--quantization awq` on a base FP16 repo fail at load.
_ONTHEFLY_FROM_BASE = {"bitsandbytes", "fp8"}


def optimize(available_quants, gpu_name: str, gpu_vendor: str = "nvidia") -> dict:
    """Pick the best quant for (available checkpoints, hardware) + the vLLM flags.

    Args:
        available_quants: the quants for which the model ACTUALLY HAS a checkpoint,
                          best-first — i.e. the keys of the recipe's quant_repos
                          (e.g. ["awq", "fp8", "base"]). A weight quant like awq/
                          nvfp4 is a property of the CHECKPOINT, not a flag you can
                          apply to a base model — so we only choose among quants a
                          real checkpoint exists for. `base` (unquantized) may also
                          be shrunk on-the-fly to bitsandbytes/fp8 if the hardware
                          prefers it.
        gpu_name / gpu_vendor: from hardware detection.

    Returns {arch, quant, kv_cache_dtype, dtype, enforce_eager, quantization_arg,
             rationale, warnings}. Never raises.
    """
    arch = classify_arch(gpu_name, gpu_vendor)
    hw_quants = ARCH_QUANTS.get(arch, ARCH_QUANTS["unknown"])
    warnings: list[str] = []

    avail = list(available_quants.keys()) if isinstance(available_quants, dict) \
        else list(available_quants or [])

    # Candidate quants = those with a checkpoint, PLUS on-the-fly quants reachable
    # from a base checkpoint if one exists.
    candidates = set(avail)
    if "base" in avail:
        candidates |= {q for q in _ONTHEFLY_FROM_BASE if q in hw_quants}

    # Best hardware-accelerated quant we can actually MATERIALISE (has a checkpoint
    # or is on-the-fly-from-base). Walk the arch's best-first order.
    chosen = ""
    for q in hw_quants:
        if q in candidates:
            chosen = q
            break
    if not chosen:
        # Nothing the hardware accelerates is available — take the first available
        # checkpoint quant as-is (it will still load, just unaccelerated) and warn.
        chosen = avail[0] if avail else "base"
        warnings.append(
            f"no hardware-accelerated quant is available for this model on {arch} "
            f"(available: {avail}); using {chosen} unoptimized"
        )

    # vLLM --quantization value (verified against vLLM 0.14.1's registered methods).
    # NVFP4 MUST be `modelopt_fp4` (ModelOptNvFp4Config) — plain `modelopt` is
    # ModelOptFp8Config and would load the wrong (FP8) path on a 4-bit checkpoint.
    # `base` emits NO flag (unquantized checkpoint served at its native dtype).
    quant_arg = {
        "nvfp4": "modelopt_fp4",   # ModelOptNvFp4Config — NOT "modelopt" (that's FP8)
        "awq": "awq",
        "fp8": "fp8",
        "w8a16": "compressed-tensors",
        "bitsandbytes": "bitsandbytes",
        "gguf": "gguf",
        "base": "",
    }.get(chosen, "")

    # Which checkpoint should be pulled: the chosen quant's own repo if it has one,
    # else `base` (for an on-the-fly quant like bitsandbytes/fp8 applied to base).
    repo_quant = chosen if chosen in avail else ("base" if "base" in avail else chosen)
    onthefly = chosen not in avail and repo_quant == "base"

    kv = "fp8_e4m3" if arch in _FP8_KV_ARCHS else "auto"
    if arch not in _FP8_KV_ARCHS:
        warnings.append(
            f"fp8 KV cache is not supported on {arch}; using auto (fp16) KV — "
            "expect a larger KV footprint and shorter max context"
        )

    # gemma-class models need enforce-eager on several vLLM builds (sliding-window
    # + cudagraph capture bug); the fleet sets it for gemma4. Signalled by the caller
    # via a 'gemma' pref marker is overkill — leave enforce_eager off by default and
    # let recipes override.
    return {
        "arch": arch,
        "quant": chosen,
        "quantization_arg": quant_arg,
        "repo_quant": repo_quant,      # which quant_repos key to pull
        "onthefly": onthefly,          # chosen quant applied to a base checkpoint
        "kv_cache_dtype": kv,
        "dtype": "auto",
        "enforce_eager": False,
        "hardware_quants": hw_quants,
        "rationale": f"{gpu_name or 'no GPU'} -> {arch}; best available quant "
                     f"({avail}) runnable here = {chosen}"
                     + (f" (on-the-fly from base)" if onthefly else ""),
        "warnings": warnings,
    }
