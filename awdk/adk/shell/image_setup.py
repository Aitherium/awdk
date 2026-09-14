"""Image-studio setup for the first-run wizard.

Wraps the `image_bootstrap` toolpack in a friendly, plain-language flow so a
non-technical user can stand up ComfyUI + Stable Diffusion on their own
hardware and have it visible to the platform (AitherCanvas / Genesis).

The toolpack owns the mechanics — hardware detection, recipe resolution by
VRAM band, docker-compose / native rendering, backend registration, and the
positive-assertion verify. This module owns the ORDER, the idempotency, and
the plain words.

Flow:
    detect -> resolve -> plan -> (apply if not already healthy) -> register
             -> verify

Nothing here raises; every step returns a dict, matching the toolpack doctrine.
A missing Genesis/token only skips registration with a note — it never fails the
whole setup, because the backend still runs locally on :8188.

Drive it from the GUI wizard (`adk.shell.gui_wizard`) or the CLI:
    python -m adk.shell.image_setup --dry-run
    python -m adk.shell.image_setup --recipe-id cuda-comfyui-12gb --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# ── Import the engine lazily so importing this module never fails on a
#    machine missing httpx/hardware-probe deps.  The GUI wizard imports this
#    module at startup; the engine only matters once the user reaches the
#    image-studio step. ────────────────────────────────────────────────────


def _engine():
    from adk.toolpacks.image_bootstrap import tools as _tools
    return _tools


# ── Engine options (ComfyUI / Sana / Bonsai) surfaced to the user ────────


def _engine_options_from_detect(detect: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Translate the hardware detection into the image engines this computer can run.

    Each option is {id, name, plain, recipe_id, requires_download_gb}. The
    in-browser Bonsai option is always present — it needs nothing installed.
    """
    sysinfo = detect.get("system_info", {})
    gpu_vendor = sysinfo.get("gpu_vendor", "none")
    vram_mb = int(sysinfo.get("gpu_vram_mb") or 0)
    vram_gb = vram_mb / 1024
    capability = detect.get("capability", {})

    options: List[Dict[str, Any]] = []

    # ComfyUI — the full studio (control, LoRA, ControlNet). A recipe exists for
    # every band; pick the recipe matching this hardware (resolve already does).
    if capability.get("sdxl_1024") or capability.get("sdxl_lowvram") or capability.get("cpu_only"):
        options.append({
            "id": "comfyui",
            "name": "Full studio",
            "plain": "Stable Diffusion with full control (styles, fine-tuning, inpainting).",
            "recipe_id": detect.get("recommended_recipe", ""),
            "requires_download_gb": 14.0 if vram_gb >= 12 else 7.0,
        })

    # Sana — fast one-step generation on NVIDIA 8GB+.
    if gpu_vendor == "nvidia" and vram_gb >= 8:
        options.append({
            "id": "sana",
            "name": "Fast images (Sana)",
            "plain": "~1 second per image on your graphics card. Less control, much faster.",
            "recipe_id": "cuda-sana-sprint",
            "requires_download_gb": 1.5,
        })

    # Bonsai Image — near-Flux quality on NVIDIA 8GB+.
    if gpu_vendor == "nvidia" and vram_gb >= 8:
        options.append({
            "id": "bonsai",
            "name": "Bonsai Image",
            "plain": "High-quality images with a small, fast model (~4GB).",
            "recipe_id": "cuda-bonsai-image",
            "requires_download_gb": 4.3,
        })

    # Bonsai in the browser — nothing to install, works on any modern computer.
    options.append({
        "id": "bonsai-browser",
        "name": "In your web browser",
        "plain": "Make images right on aitherium.com — no download, works on most computers.",
        "recipe_id": "",
        "requires_download_gb": 0.0,
    })

    return options


def image_studio_status() -> Dict[str, Any]:
    """Detect hardware and report whether a local image studio is possible.

    Returns {capable, vram_band, gpu_name, recommended_recipe, capability,
             engine_options, plain_english, errors}.
    """
    tools = _engine()
    detect = tools.imagegen_detect_hardware()
    if "error" in detect:
        return {
            "capable": False,
            "plain_english": "We could not look at your computer's graphics card.",
            "engine_options": [],
            "errors": [detect.get("error", "detection failed")],
            "raw": detect,
        }

    sysinfo = detect.get("system_info", {})
    band = detect.get("vram_band", "none")
    capability = detect.get("capability", {})
    recipe = detect.get("recommended_recipe", "")
    gpu_name = sysinfo.get("gpu_name", "")
    gpu_vendor = sysinfo.get("gpu_vendor", "none")

    # Plain-language capability verdict.
    if gpu_vendor == "apple":
        capable = True
        plain = "Your Mac has unified memory — you can run an image studio (Apple Silicon)."
    elif capability.get("sdxl_1024"):
        capable = True
        plain = "Your computer has a strong graphics card — it can run a full image studio."
    elif capability.get("sdxl_lowvram"):
        capable = True
        plain = "Your computer has a modest graphics card — it can run image generation with low-memory settings."
    elif capability.get("cpu_only"):
        capable = True
        plain = "No usable graphics card, but your computer can still make images slowly (CPU mode)."
    else:
        capable = False
        plain = "We could not tell if your computer can run image generation locally."

    return {
        "capable": capable,
        "vram_band": band,
        "gpu_vendor": gpu_vendor,
        "gpu_name": gpu_name,
        "recommended_recipe": recipe,
        "capability": capability,
        "engine_options": _engine_options_from_detect(detect),
        "plain_english": plain,
        "errors": [],
        "raw": detect,
    }


def image_studio_plan(recipe_id: str = "", prefer_engine: str = "auto") -> Dict[str, Any]:
    """Resolve a recipe for this hardware and render its plan (no side effects).

    ``prefer_engine`` is a tiebreaker — "comfyui" | "sana" | "bonsai" | "auto" —
    exactly like the toolpack's own resolver.
    """
    tools = _engine()
    resolved = tools.imagegen_resolve_recipe(prefer_engine=prefer_engine, recipe_id=recipe_id)
    if "error" in resolved:
        return {"error": resolved["error"], "fix": resolved.get("fix", "")}
    recipe = resolved.get("recipe", {})
    rid = recipe.get("id", recipe_id)
    plan = tools.imagegen_plan_deployment(rid)
    if "error" in plan:
        return {"error": plan["error"], "fix": plan.get("fix", "")}
    plan["recipe_name"] = recipe.get("name", rid)
    plan["rationale"] = resolved.get("rationale", "")
    plan["warnings"] = resolved.get("warnings", [])
    return plan


def image_studio_run(
    *,
    recipe_id: str = "",
    prefer_engine: str = "auto",
    dry_run: bool = False,
    host_port: int = 0,
    genesis_url: str = "",
    token: str = "",
    auto_apply: bool = False,
) -> Dict[str, Any]:
    """Run the full image-studio flow.

    Idempotent: if the backend at the plan's port already verifies healthy,
    apply is skipped.  Registration is best-effort and never blocks success.

    Returns {status: "healthy"|"skipped"|"planned"|"deferred"|"failed",
             recipe_id, mode, port, backend_url, applied, registered, verify,
             plain_english, errors, notes}.
    """
    tools = _engine()
    errors: list = []
    notes: list = []

    plan = image_studio_plan(recipe_id=recipe_id, prefer_engine=prefer_engine)
    if "error" in plan:
        return {
            "status": "failed",
            "errors": [plan["error"]],
            "plain_english": "We could not plan your image studio setup.",
        }

    rid = plan["recipe_id"]
    port = plan.get("port", 8188)
    backend_url = f"http://localhost:{port}"
    mode = plan.get("mode", "none")

    # ── Verify-first: is a healthy backend already there? ──────────────────
    verify = tools.imagegen_verify(backend_url, plan.get("backend_type", "comfyui"))
    if verify.get("status") == "healthy":
        return {
            "status": "healthy",
            "recipe_id": rid,
            "mode": mode,
            "port": port,
            "backend_url": backend_url,
            "applied": False,
            "registered": False,
            "verify": verify,
            "plain_english": "Your image studio is already running and ready.",
            "errors": [],
            "notes": notes,
        }

    if dry_run:
        apply = tools.imagegen_apply(rid, dry_run=True)
        return {
            "status": "planned",
            "recipe_id": rid,
            "mode": mode,
            "port": port,
            "backend_url": backend_url,
            "applied": False,
            "registered": False,
            "verify": verify,
            "plain_english": f"We are ready to set up: {plan.get('recipe_name', rid)}.",
            "errors": [],
            "notes": notes,
            "plan": plan,
            "commands": apply.get("commands", []),
        }

    if not auto_apply:
        # GUI/CLI caller will surface the plan and let the human choose.
        return {
            "status": "deferred",
            "recipe_id": rid,
            "mode": mode,
            "port": port,
            "backend_url": backend_url,
            "applied": False,
            "registered": False,
            "verify": verify,
            "plain_english": (
                f"Your computer can run: {plan.get('recipe_name', rid)} "
                f"(~{plan.get('download_size_mb', 0) / 1024:.0f} GB download)."
            ),
            "errors": [],
            "notes": notes,
            "plan": plan,
        }

    # ── Apply ──────────────────────────────────────────────────────────────
    apply = tools.imagegen_apply(rid, host_port=host_port)
    if "error" in apply:
        return {
            "status": "failed",
            "recipe_id": rid,
            "mode": mode,
            "port": port,
            "backend_url": backend_url,
            "applied": False,
            "errors": [apply["error"]],
            "fix": apply.get("fix", ""),
            "plain_english": "The image studio could not start. Check Docker is installed and running.",
            "notes": notes,
        }
    if apply.get("mode") == "native":
        notes.append(
            "This computer needs the manual steps (macOS Metal / CPU). They are "
            "listed in the plan; once ComfyUI is running, come back and we will register it."
        )

    # ── Register with the platform (best-effort) ───────────────────────────
    registered = False
    reg_note = ""
    if not genesis_url:
        genesis_url = os.environ.get("AITHER_GENESIS_URL", "")
    if not token:
        token = os.environ.get("AITHER_AUTH_TOKEN", "")
    if genesis_url and token:
        reg = tools.imagegen_register_backend(
            genesis_url=genesis_url,
            base_url=backend_url,
            backend_type=plan.get("backend_type", "comfyui"),
            models=plan.get("model_profile", "").split(",") if plan.get("model_profile") else [],
        )
        if reg.get("registered"):
            registered = True
            notes.append("Registered with the platform — your image studio is now available in AitherCanvas.")
        else:
            reg_note = reg.get("error", "registration failed")
    else:
        reg_note = "No platform connection yet — registration skipped (the studio still runs locally)."

    # ── Positive-assertion verify ──────────────────────────────────────────
    final = tools.imagegen_verify(backend_url, plan.get("backend_type", "comfyui"))
    status = "healthy" if final.get("status") == "healthy" else "degraded"
    if final.get("status") == "degraded":
        notes.append(
            "The studio is up but has not finished loading models yet — model "
            "downloads are large and can take a while. Come back and re-check."
        )

    return {
        "status": status,
        "recipe_id": rid,
        "mode": mode,
        "port": port,
        "backend_url": backend_url,
        "applied": True,
        "registered": registered,
        "registration_note": reg_note or None,
        "verify": final,
        "plain_english": (
            "Your image studio is ready! You can make images with Stable Diffusion "
            "right on your own computer."
            if status == "healthy"
            else "Your image studio is starting up. Models may still be downloading."
        ),
        "errors": errors,
        "notes": notes,
    }


# ── CLI entry (also the testable module entry) ────────────────────────────


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m adk.shell.image_setup")
    parser.add_argument("--dry-run", action="store_true", help="plan without applying")
    parser.add_argument("--apply", action="store_true", help="apply (start) the studio")
    parser.add_argument("--recipe-id", default="", help="explicit recipe id")
    parser.add_argument("--genesis-url", default="", help="platform URL for registration")
    parser.add_argument("--token", default="", help="platform auth token for registration")
    args = parser.parse_args(argv)

    if args.apply:
        result = image_studio_run(
            recipe_id=args.recipe_id,
            dry_run=False,
            auto_apply=True,
            genesis_url=args.genesis_url,
            token=args.token,
        )
    else:
        result = image_studio_run(recipe_id=args.recipe_id, dry_run=args.dry_run)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in ("healthy", "planned", "skipped", "deferred") else 1


if __name__ == "__main__":
    sys.exit(_main())
