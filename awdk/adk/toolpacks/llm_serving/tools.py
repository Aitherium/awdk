"""LLM serving pack — llm_* agent tools.

Install vLLM and serve the AitherOS fleet models (Nemotron-Orchestrator-8B,
gemma4-12b, qwen-27b, deepseek-r1-14b) with quantization + serve flags OPTIMIZED
to the detected GPU.

Design rules (same doctrine as node_bootstrap / image_bootstrap / split_inference):
  * Fail soft — every tool returns a dict, never raises.
  * Pure tools (detect, resolve, plan) have no side effects.
  * llm_apply is dry_run-able; nothing executes under dry_run.
  * llm_verify makes the POSITIVE assertion: a /v1/chat/completions round-trip that
    returns non-empty content AND whose served model name matches. A vLLM that is
    "up" but serving the wrong model name is a silent routing dead end.
  * Registration is fail-closed (token required).
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

import httpx

from adk.toolpacks.llm_serving.quant import optimize
from adk.toolpacks.llm_serving.recipes import (
    RECIPE_IDS,
    fits_hardware,
    get_recipe,
    list_recipes,
    resolve_by_role_or_id,
)

logger = logging.getLogger("llm_serving_pack")

_TIMEOUT_DEFAULT = 60.0


# ── helpers ─────────────────────────────────────────────────────────────


def _system_dict() -> dict:
    from adk.hardware_probe import detect_system

    s = detect_system()
    return {
        "ram_gb": s.ram_gb,
        "cpu_cores": s.cpu_cores,
        "gpu_vendor": s.gpu_vendor,
        "gpu_name": s.gpu_name,
        "gpu_vram_mb": s.gpu_vram_mb,
    }


def _run(cmd: list, timeout: int = 120) -> tuple:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, ((p.stdout or "") + "\n" + (p.stderr or "")).strip()[-3000:]
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


# ── 1. DETECTION ────────────────────────────────────────────────────────


def llm_detect_hardware() -> dict:
    """Detect the GPU and report which fleet models fit + their optimal quant.

    Pure local operation. Returns {system_info, arch, models: {id: {fits, quant,...}}}.
    """
    try:
        sysinfo = _system_dict()
        from adk.toolpacks.llm_serving.quant import classify_arch

        arch = classify_arch(sysinfo["gpu_name"], sysinfo["gpu_vendor"])
        models = {}
        for rid in RECIPE_IDS:
            recipe = get_recipe(rid) or {}
            fits, reasons = fits_hardware(recipe, sysinfo)
            opt = optimize(
                recipe.get("model", {}).get("quant_repos", {}),
                sysinfo["gpu_name"], sysinfo["gpu_vendor"],
            )
            models[rid] = {
                "fits": fits,
                "reasons": reasons,
                "quant": opt["quant"] if fits else None,
                "kv_cache_dtype": opt["kv_cache_dtype"] if fits else None,
            }
        return {"system_info": sysinfo, "arch": arch, "models": models}
    except Exception as e:  # noqa: BLE001
        logger.exception("llm hardware detection failed")
        return {"error": f"hardware detection failed: {e}", "fix": "check GPU drivers"}


# ── 2. RESOLVE (recipe + optimized quant) ───────────────────────────────


def llm_resolve(model: str) -> dict:
    """Resolve a model role/id to its recipe + hardware-optimized quant (pure).

    `model` is a role ("orchestrator", "perception", "reasoner") or a recipe id.
    Returns {recipe_id, model, fits, optimization, serve_effective, warnings}.
    """
    try:
        recipe_id = resolve_by_role_or_id(model)
        if not recipe_id:
            return {
                "error": f"unknown model or role: {model}",
                "available_ids": list_recipes(),
                "available_roles": ["orchestrator", "perception", "reasoner"],
            }
        recipe = get_recipe(recipe_id)
        if not recipe:
            return {"error": f"failed to load recipe: {recipe_id}"}

        sysinfo = _system_dict()
        fits, reasons = fits_hardware(recipe, sysinfo)
        opt = optimize(
            recipe.get("model", {}).get("quant_repos", {}),
            sysinfo["gpu_name"], sysinfo["gpu_vendor"],
        )
        serve = recipe.get("serve", {})
        # enforce_eager can come from either the recipe (gemma4) or the optimizer.
        enforce_eager = bool(serve.get("enforce_eager", False) or opt.get("enforce_eager"))

        # Per-checkpoint vLLM --quantization override. The same logical quant can be
        # packaged differently (e.g. NVFP4 as compressed-tensors in vrfai's checkpoint
        # vs modelopt_fp4 from a modelopt export) — the recipe's quant_args wins over
        # the optimizer's generic default so the RIGHT loader is used for THIS repo.
        quant_args = recipe.get("model", {}).get("quant_args", {}) or {}
        quantization = quant_args.get(opt["quant"], opt["quantization_arg"])

        result = {
            "recipe_id": recipe_id,
            "model": recipe.get("model", {}),
            "fits": fits,
            "fit_reasons": reasons,
            "optimization": opt,
            "serve_effective": {
                "port": serve.get("port", 8000),
                "quantization": quantization,
                "kv_cache_dtype": opt["kv_cache_dtype"],
                "gpu_memory_utilization": serve.get("gpu_memory_utilization", 0.9),
                "max_model_len": serve.get("max_model_len", 8192),
                "max_num_seqs": serve.get("max_num_seqs", 8),
                "swap_space_gb": serve.get("swap_space_gb", 0),
                "enforce_eager": enforce_eager,
            },
            "warnings": list(opt.get("warnings", [])) + list(serve.get("platform_traps", [])),
        }
        if not fits:
            result["warnings"].insert(
                0, f"{recipe_id} does NOT fit this hardware: {'; '.join(reasons)}"
            )
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("llm resolve failed")
        return {"error": f"resolve failed: {e}"}


# ── 3. PLAN ─────────────────────────────────────────────────────────────


def _vllm_command(recipe: dict, opt: dict, serve_eff: dict) -> list:
    """Build the `vllm serve` argv from recipe + optimized quant.

    The repo pulled is the CHOSEN quant's checkpoint (opt.repo_quant) — a weight
    quant is baked into the checkpoint, so we must serve the matching one, never a
    base repo with an awq/nvfp4 flag (that fails at load).
    """
    model = recipe.get("model", {})
    repos = model.get("quant_repos", {})
    repo = repos.get(opt.get("repo_quant", ""), "") or next(iter(repos.values()), "")
    args = [
        "vllm", "serve", repo,
        "--host", "0.0.0.0",
        "--port", str(serve_eff["port"]),
        "--served-model-name", model.get("served_name", ""),
        "--gpu-memory-utilization", str(serve_eff["gpu_memory_utilization"]),
        "--max-model-len", str(serve_eff["max_model_len"]),
        "--max-num-seqs", str(serve_eff["max_num_seqs"]),
        "--dtype", "auto",
    ]
    if serve_eff.get("quantization"):
        args += ["--quantization", serve_eff["quantization"]]
    if serve_eff.get("kv_cache_dtype") and serve_eff["kv_cache_dtype"] != "auto":
        args += ["--kv-cache-dtype", serve_eff["kv_cache_dtype"]]
    if serve_eff.get("swap_space_gb"):
        args += ["--swap-space", str(serve_eff["swap_space_gb"])]
    if serve_eff.get("enforce_eager"):
        args += ["--enforce-eager"]
    for extra in recipe.get("serve", {}).get("extra_args", []) or []:
        args += extra.split()
    return args


def llm_plan_deployment(model: str) -> dict:
    """Render the vLLM serve plan for a model (pure, no side effects)."""
    resolved = llm_resolve(model)
    if "error" in resolved:
        return resolved
    if not resolved.get("fits"):
        return {
            "error": f"{resolved['recipe_id']} does not fit this hardware",
            "reasons": resolved.get("fit_reasons", []),
            "fix": "pick a smaller model role (e.g. deepseek-r1-14b instead of "
                   "qwen-27b-reason) or add VRAM",
        }
    try:
        recipe = get_recipe(resolved["recipe_id"])
        cmd = _vllm_command(recipe, resolved["optimization"], resolved["serve_effective"])
        return {
            "recipe_id": resolved["recipe_id"],
            "served_name": recipe.get("model", {}).get("served_name", ""),
            "quant": resolved["optimization"]["quant"],
            "arch": resolved["optimization"]["arch"],
            "vllm_command": cmd,
            "vllm_command_str": " ".join(cmd),
            "port": resolved["serve_effective"]["port"],
            "steps": [
                "Ensure vLLM is installed (pip install vllm)",
                f"Pull weights: {recipe.get('model', {}).get('quant_repos', {}).get(resolved['optimization'].get('repo_quant', ''), '?')}",
                f"Serve with quant={resolved['optimization']['quant']} "
                f"kv={resolved['serve_effective']['kv_cache_dtype']}",
                f"Verify a chat round-trip on :{resolved['serve_effective']['port']}",
            ],
            "warnings": resolved.get("warnings", []),
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("llm plan failed")
        return {"error": f"plan failed: {e}"}


# ── 4. APPLY ────────────────────────────────────────────────────────────


def llm_apply(model: str, dry_run: bool = False, install_vllm: bool = False) -> dict:
    """Install vLLM (optional) and start the model server.

    dry_run=True shows the commands without executing. Because a vLLM serve is a
    long-lived foreground process, apply launches it DETACHED (nohup) and returns
    immediately with the verify command — it never blocks on model load.
    """
    plan = llm_plan_deployment(model)
    if "error" in plan:
        return plan

    install_cmd = ["pip", "install", "-U", "vllm"]
    cmd_str = plan["vllm_command_str"]

    if dry_run:
        cmds = []
        if install_vllm:
            cmds.append(" ".join(install_cmd))
        cmds.append(f"nohup {cmd_str} > vllm-{plan['recipe_id']}.log 2>&1 &")
        return {
            "planned": True, "dry_run": True, "recipe_id": plan["recipe_id"],
            "commands": cmds, "port": plan["port"], "warnings": plan.get("warnings", []),
        }

    try:
        if install_vllm:
            rc, out = _run(install_cmd, timeout=1800)
            if rc != 0:
                return {"error": f"vllm install failed (rc={rc})", "output": out[-800:]}

        # Detached launch — vLLM serve blocks forever otherwise.
        log_path = f"vllm-{plan['recipe_id']}.log"
        launch = f"nohup {cmd_str} > {log_path} 2>&1 &"
        rc, out = _run(["sh", "-lc", launch], timeout=30)
        if rc != 0:
            return {"error": f"failed to launch vllm (rc={rc})", "output": out}
        return {
            "applied": True, "recipe_id": plan["recipe_id"], "port": plan["port"],
            "log": log_path,
            "next": f"llm_verify(model='{model}', "
                    f"base_url='http://localhost:{plan['port']}') once weights load",
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("llm apply failed")
        return {"error": f"apply failed: {e}"}


# ── 5. REGISTER ─────────────────────────────────────────────────────────


def llm_register_backend(
    model: str,
    base_url: str,
    genesis_url: str = "",
    token: str = "",
) -> dict:
    """Register a served model with Genesis (fail-closed on missing URL/token)."""
    if not token:
        token = os.environ.get("AITHER_AUTH_TOKEN", "")
    if not token:
        return {"error": "auth token required",
                "fix": "pass token= or set AITHER_AUTH_TOKEN"}
    if not genesis_url:
        genesis_url = os.environ.get("AITHER_GENESIS_URL", "")
    if not genesis_url:
        return {"error": "genesis URL required",
                "fix": "provide genesis_url or set AITHER_GENESIS_URL"}
    if not base_url:
        return {"error": "base_url required (the vLLM endpoint)"}

    recipe_id = resolve_by_role_or_id(model)
    recipe = get_recipe(recipe_id) if recipe_id else {}
    served = (recipe or {}).get("model", {}).get("served_name", "")
    try:
        r = httpx.post(
            f"{genesis_url.rstrip('/')}/deploy/cloud-model/register-backend",
            json={"base_url": base_url, "backend_type": "vllm",
                  "models": [served], "category": (recipe or {}).get(
                      "fleet_wiring", {}).get("catalog_entry", {}).get("category", "")},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        if r.status_code == 200:
            return {"registered": True, "served_name": served, "base_url": base_url}
        return {"error": f"genesis HTTP {r.status_code}", "detail": r.text[:200]}
    except httpx.HTTPError as e:
        return {"error": f"registration failed: {e}"}


# ── 6. VERIFY (positive assertion) ──────────────────────────────────────


def llm_verify(
    base_url: str,
    model: str = "",
    timeout_s: float = _TIMEOUT_DEFAULT,
) -> dict:
    """Verify a vLLM backend is up AND serving the right model — with a real chat.

    Health (`/health`) alone is not proof: a vLLM can be up serving a DIFFERENT
    model name than routing expects, which is a silent dead end. This checks
    /v1/models for the expected served name, then does a real /v1/chat/completions
    round-trip and requires non-empty content.

    status: healthy | wrong_model | degraded | unknown
    """
    if not base_url:
        return {"error": "base_url required"}
    base = base_url.rstrip("/")
    timeout = float(timeout_s)

    expected = ""
    if model:
        rid = resolve_by_role_or_id(model)
        recipe = get_recipe(rid) if rid else None
        expected = (recipe or {}).get("model", {}).get("served_name", "")

    try:
        # 1. /health
        health_ok = False
        try:
            health_ok = httpx.get(f"{base}/health", timeout=timeout).status_code == 200
        except httpx.HTTPError as e:
            return {"status": "unknown", "health": False, "detail": str(e),
                    "fix": "vLLM did not answer /health — still loading, or not up"}

        # 2. /v1/models — is the expected served name present?
        served_names = []
        try:
            r = httpx.get(f"{base}/v1/models", timeout=timeout)
            if r.status_code == 200:
                served_names = [m.get("id") for m in (r.json().get("data") or [])]
        except (httpx.HTTPError, ValueError):
            pass

        name_ok = (not expected) or (expected in served_names)

        # 3. real chat round-trip — probe with a model the endpoint ACTUALLY serves,
        # so a served-name MISMATCH surfaces as 'wrong_model', not a masked
        # 'degraded'. Using the expected (possibly-absent) name here would make the
        # completion fail for the wrong reason and hide the real signal.
        content, infer_detail = "", ""
        probe_model = (served_names[0] if served_names else "") or expected
        try:
            r = httpx.post(
                f"{base}/v1/chat/completions",
                json={"model": probe_model,
                      "messages": [{"role": "user", "content": "Reply with: ready"}],
                      "max_tokens": 8, "temperature": 0},
                timeout=timeout,
            )
            if r.status_code == 200:
                content = (((r.json().get("choices") or [{}])[0]
                            .get("message") or {}).get("content") or "").strip()
            else:
                infer_detail = f"HTTP {r.status_code}: {r.text[:120]}"
        except (httpx.HTTPError, ValueError) as e:
            infer_detail = f"{type(e).__name__}: {e}"

        infer_ok = bool(content)
        if not health_ok or not infer_ok:
            status = "degraded"
        elif not name_ok:
            status = "wrong_model"
        else:
            status = "healthy"

        result = {
            "status": status,
            "health": health_ok,
            "served_names": served_names,
            "expected_served_name": expected,
            "served_name_ok": name_ok,
            "inference_ok": infer_ok,
            "sample_reply": content[:80],
        }
        if infer_detail:
            result["detail"] = infer_detail
        if status == "wrong_model":
            result["fix"] = (
                f"vLLM is up but serves {served_names}, not the expected "
                f"'{expected}' — routing keyed on the served name will never hit "
                "this backend. Fix --served-model-name to match."
            )
        elif status == "degraded":
            result["fix"] = ("up but no completion — check the vLLM log; the model "
                             "may still be loading (big models take minutes)")
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("llm verify failed")
        return {"status": "unknown", "error": f"verify failed: {e}"}
