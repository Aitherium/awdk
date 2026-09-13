"""Recipe engine for the image-bootstrap pack.

Loads YAML recipe definitions from the recipes/ directory and resolves them
against detected system hardware — the image-generation twin of the
node_bootstrap recipe engine.

Recipes are authoritative specifications for image-gen deployments:
  - Hardware requirements (RAM, CPU cores, GPU vendor, VRAM band, unified memory)
  - Image-gen config (engine, model profile, models, serve args, deployment target)
  - Fleet integration (catalog entry, service URL env var)

Resolution algorithm: explicit recipe_id wins; else score by hardware match
  (GPU vendor + VRAM band, RAM, cores); ties break toward higher tier, then
  toward self-contained recipes over fleet-delegate ones; nothing fits =>
  cloud-burst fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("image_bootstrap_recipes")

RECIPE_IDS = [
    "cpu-sdxl-turbo",
    "cuda-comfyui-6gb",
    "cuda-comfyui-12gb",
    "cuda-comfyui-24gb",
    "cuda-sana-sprint",
    "cuda-bonsai-image",
    "metal-comfyui",
    "cloud-burst-vast",
    "webgpu-browser",
    "mesh-spark",
]

# The always-available fallback: it needs no local hardware at all.
FALLBACK_RECIPE_ID = "cloud-burst-vast"

TIER_RANKS = {
    "cloud": 0,
    "browser": 0.5,
    "cpu": 1,
    "gpu-small": 2,
    "gpu-medium": 3,
    "gpu-large": 4,
    "gpu-apple": 5,
}

# VRAM bands drive which model profile is viable. Reported by detection so an
# agent can reason about capability without re-deriving the thresholds.
VRAM_BANDS = [
    (24, "large"),
    (12, "medium"),
    (6, "small"),
    (0, "none"),
]


@dataclass
class RecipeMatch:
    """Result of recipe resolution."""

    recipe: dict
    match_score: float
    rationale: str
    warnings: list[str]


def vram_band(gpu_vram_gb: float) -> str:
    """Classify VRAM into a capability band: large | medium | small | none."""
    for threshold, name in VRAM_BANDS:
        if gpu_vram_gb >= threshold and threshold > 0:
            return name
    return "none"


def engine_family(engine: str) -> str:
    """Normalise an engine id to its family ('comfyui-native' -> 'comfyui')."""
    return (engine or "").split("-", 1)[0]


def _load_recipe(recipe_id: str) -> dict:
    """Load a recipe YAML from the recipes/ data directory."""
    try:
        recipe_path = Path(__file__).parent / "recipes" / f"{recipe_id}.yaml"

        if not recipe_path.exists():
            logger.error("Recipe file not found: %s", recipe_path)
            return {}

        with open(recipe_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001 — a bad recipe must never crash resolution
        logger.error("Failed to load recipe %s: %s", recipe_id, e)
        return {}


def _score_recipe(recipe: dict, system_info: dict) -> tuple[float, list[str]]:
    """Score a recipe against system hardware. Returns (score, warnings).

    All viable recipes score 1.0; tier rank is the tie-breaker (see resolve_recipe).
    Any unmet hard constraint returns 0.0 with the reason, so a caller can always
    explain WHY a recipe was rejected.
    """
    warnings: list[str] = []

    reqs = recipe.get("hardware_requirements", {})
    gpu_vendor = system_info.get("gpu_vendor", "none")
    gpu_vram_gb = system_info.get("gpu_vram_mb", 0) / 1024
    ram_gb = system_info.get("ram_gb", 0)
    cpu_cores = system_info.get("cpu_cores", 0)
    unified_memory = system_info.get("unified_memory", False)

    req_vendor = reqs.get("gpu_vendor", "none")
    req_min_vram = reqs.get("min_vram_gb", 0)
    req_unified = reqs.get("unified_memory", False)
    req_min_ram = reqs.get("min_ram_gb", 0)
    req_min_cores = reqs.get("min_cpu_cores", 0)

    # GPU vendor match (hard constraint)
    if req_vendor != "none":
        if gpu_vendor != req_vendor:
            return 0.0, [
                f"GPU vendor mismatch: recipe needs {req_vendor}, system has {gpu_vendor}"
            ]
    elif gpu_vendor != "none":
        warnings.append(
            "System has a GPU but this recipe does not use it — "
            "a GPU recipe will generate far faster"
        )

    # VRAM band (hard constraint for GPU recipes)
    if req_vendor != "none":
        # Apple unified memory reports no discrete VRAM; RAM is the real budget.
        if not (req_unified and unified_memory) and gpu_vram_gb < req_min_vram:
            return 0.0, [
                f"Insufficient VRAM: recipe needs {req_min_vram}GB, "
                f"system has {gpu_vram_gb:.1f}GB"
            ]
        # Ceiling: keeps a low-VRAM recipe from winning on a bigger card, so a
        # 24GB box gets the 24GB band rather than the 6GB one by tier accident.
        req_max_vram = reqs.get("max_vram_gb", 0)
        if req_max_vram and gpu_vram_gb > req_max_vram:
            return 0.0, [
                f"VRAM above recipe ceiling: recipe caps at {req_max_vram}GB, "
                f"system has {gpu_vram_gb:.1f}GB"
            ]

    # Unified memory: advisory, not fatal
    if req_unified and not unified_memory:
        warnings.append("Recipe prefers unified memory, system has a discrete GPU")
    elif not req_unified and unified_memory and req_vendor != "none":
        warnings.append("Recipe is discrete-GPU-only, system has unified memory")

    # RAM (hard constraint)
    if ram_gb < req_min_ram:
        return 0.0, [
            f"Insufficient RAM: recipe needs {req_min_ram}GB, system has {ram_gb:.1f}GB"
        ]

    # CPU cores (hard constraint)
    if cpu_cores < req_min_cores:
        return 0.0, [
            f"Insufficient cores: recipe needs {req_min_cores}, system has {cpu_cores}"
        ]

    return 1.0, warnings


def resolve_recipe(
    system_info: dict,
    prefer_engine: str = "auto",
    recipe_id: str = "",
) -> dict:
    """Resolve the best image-gen recipe for the given system hardware.

    Args:
        system_info: dict with keys ram_gb, cpu_cores, gpu_vendor, gpu_vram_mb,
                     unified_memory, gpu_name
        prefer_engine: "auto", or an engine family ("comfyui", "sana"). This is a
                       TIEBREAKER, not a hard gate — if it matches nothing viable
                       the best overall match is returned with a warning.
        recipe_id: explicit recipe ID (overrides resolution)

    Returns {recipe, match_score, rationale, warnings}. Never raises.
    If nothing fits locally: the cloud-burst fallback.
    """
    # Explicit recipe_id wins — checked BEFORE any hardware fallback, so an
    # explicit request with empty system_info does not silently become cloud-burst.
    if recipe_id and recipe_id in RECIPE_IDS:
        recipe = _load_recipe(recipe_id)
        if recipe:
            warnings = list(recipe.get("imagegen_config", {}).get("platform_traps", []))
            return {
                "recipe": recipe,
                "match_score": 10.0,
                "rationale": f"Explicit recipe_id: {recipe_id}",
                "warnings": warnings,
            }
        logger.warning("Failed to load explicit recipe %s, falling back", recipe_id)

    # Score every recipe; the cloud fallback is always a candidate.
    candidates = []
    rejections: dict[str, str] = {}
    for rid in RECIPE_IDS:
        recipe = _load_recipe(rid)
        if not recipe:
            continue
        score, warnings = _score_recipe(recipe, system_info)
        if score > 0 or rid == FALLBACK_RECIPE_ID:
            candidates.append((rid, recipe, score, warnings))
        else:
            rejections[rid] = warnings[0] if warnings else "did not match"

    if not candidates:
        recipe = _load_recipe(FALLBACK_RECIPE_ID)
        return {
            "recipe": recipe,
            "match_score": 0.0,
            "rationale": "No local recipe matched this hardware; falling back to cloud burst.",
            "warnings": ["No local recipe matched hardware"],
            "rejected": rejections,
        }

    def sort_key(item):
        rid, recipe, score, _warnings = item
        tier_rank = TIER_RANKS.get(recipe.get("tier", "cloud"), 0)
        # Prefer SELF-CONTAINED recipes over fleet-delegate ones on ties: a
        # delegate recipe needs fleet tooling the public pack does not ship, so
        # it must never win by alphabetical accident.
        needs_delegate = 1 if recipe.get("deployment", {}).get("delegate") else 0
        return (-score, -tier_rank, needs_delegate, rid)

    candidates.sort(key=sort_key)

    # prefer_engine filter — a tiebreaker, never a hard gate.
    prefer_warning: list[str] = []
    if prefer_engine and prefer_engine != "auto":
        want = engine_family(prefer_engine)
        preferred = [
            c for c in candidates
            if engine_family(c[1].get("imagegen_config", {}).get("engine", "")) == want
        ]
        if preferred:
            candidates = preferred
        else:
            prefer_warning = [
                f"prefer_engine={prefer_engine!r} matched no viable recipe for this "
                "hardware; using best overall match instead"
            ]

    best_id, best_recipe, best_score, best_warnings = candidates[0]
    best_warnings = list(best_warnings) + prefer_warning
    best_warnings += list(best_recipe.get("imagegen_config", {}).get("platform_traps", []))

    gpu_vendor = system_info.get("gpu_vendor", "none")
    gpu_vram_gb = system_info.get("gpu_vram_mb", 0) / 1024
    ram_gb = system_info.get("ram_gb", 0)
    cpu_cores = system_info.get("cpu_cores", 0)

    rationale_parts = [f"System: {cpu_cores} cores, {ram_gb:.1f}GB RAM, {gpu_vendor}"]
    if gpu_vram_gb > 0:
        rationale_parts.append(f"{gpu_vram_gb:.1f}GB VRAM (band: {vram_band(gpu_vram_gb)})")
    rationale = " — ".join(rationale_parts) + f". Best match: {best_id} (score {best_score:.1f})"

    return {
        "recipe": best_recipe,
        "match_score": best_score,
        "rationale": rationale,
        "warnings": best_warnings,
        "rejected": rejections,
    }


def get_recipe(recipe_id: str) -> Optional[dict]:
    """Load a recipe by explicit ID. Returns None if unknown or unloadable."""
    if recipe_id not in RECIPE_IDS:
        return None
    return _load_recipe(recipe_id) or None


def list_recipes() -> list[str]:
    """Return all available recipe IDs."""
    return RECIPE_IDS
