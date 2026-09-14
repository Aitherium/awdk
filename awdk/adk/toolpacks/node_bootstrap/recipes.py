"""Recipe engine for node-bootstrap pack.

Loads YAML recipe definitions from the recipes/ directory and provides
resolution logic to match recipes against detected system hardware.

Recipes are authoritative specifications for inference deployments:
  - Hardware requirements (RAM, CPU cores, GPU vendor/VRAM, unified memory)
  - Inference config (engine, models, serve arguments, deployment target)
  - Fleet integration (catalog entry, MicroScheduler env var)

Resolution algorithm: explicit recipe_id wins; else score by hardware match
  (GPU vendor + VRAM bands, unified memory, RAM, cores); ties break toward
  higher tier; nothing fits => cloud-api fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("node_bootstrap_recipes")

RECIPES_DIR = Path(__file__).parent / "recipes"


def _discover_recipe_ids() -> list[str]:
    """Every .yaml in recipes/, sorted.

    This used to be a hardcoded list, and that made adding a recipe a SILENT no-op: the
    YAML parsed, `get_recipe(<id>)` loaded it by path, and every test that touched the file
    directly passed — but `resolve_recipe` gates on membership in this list both for the
    explicit `recipe_id` branch and for the auto-resolution loop, so an unlisted recipe was
    never selectable by any caller. `bonsai-selfhost` shipped inert exactly this way.

    Deriving from disk is safe because the candidate sort is fully deterministic on
    (-score, -tier_rank, needs_delegate, rid) — iteration order does not affect the winner.
    """
    try:
        return sorted(p.stem for p in RECIPES_DIR.glob("*.yaml"))
    except OSError as e:  # unreadable dir — better to serve nothing than to crash import
        logger.error("Cannot enumerate recipes dir %s: %s", RECIPES_DIR, e)
        return []


RECIPE_IDS = _discover_recipe_ids()

TIER_RANKS = {
    "cpu": 1,
    "gpu-small": 2,
    "gpu-medium": 3,
    "gpu-large": 4,
    "gpu-stack": 5,
    "gpu-apple": 6,
    "gpu-hpc": 7,  # Highest tier for largest unified memory systems
    "cloud": 0,
}


@dataclass
class RecipeMatch:
    """Result of recipe resolution."""

    recipe: dict
    match_score: float
    rationale: str
    warnings: list[str]


def _load_recipe(recipe_id: str) -> dict:
    """Load a recipe YAML from the recipes/ data directory."""
    try:
        # Load from recipes/ subdirectory relative to this module
        recipe_path = Path(__file__).parent / "recipes" / f"{recipe_id}.yaml"

        if not recipe_path.exists():
            logger.error("Recipe file not found: %s", recipe_path)
            return {}

        with open(recipe_path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error("Failed to load recipe %s: %s", recipe_id, e)
        return {}


def _score_recipe(recipe: dict, system_info: dict) -> tuple[float, list[str]]:
    """Score a recipe against system hardware. Returns (score, warnings).

    Scoring approach: All matching recipes get the same base score. The tie-breaker
    is handled via tier rank in resolve_recipe. This ensures that recipes optimized
    for the detected hardware (higher tier) win over lower-tier recipes even if both
    technically fit.
    """
    warnings = []

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
        # Recipe requires a GPU
        if gpu_vendor != req_vendor:
            return 0.0, [f"GPU vendor mismatch: recipe needs {req_vendor}, system has {gpu_vendor}"]
    else:
        # Recipe is CPU-only but system has GPU
        if gpu_vendor != "none":
            warnings.append("System has GPU but recipe is CPU-only")

    # GPU VRAM (hard constraint for GPU recipes)
    if req_vendor != "none":
        if gpu_vram_gb < req_min_vram:
            msg = (
                f"Insufficient VRAM: recipe needs {req_min_vram}GB, "
                f"system has {gpu_vram_gb:.1f}GB"
            )
            return 0.0, [msg]
        # Optional ceiling: card-specific stacks (e.g. the 32GB dual-stack)
        # must not outrank the correct band on BIGGER cards via tier alone.
        req_max_vram = reqs.get("max_vram_gb", 0)
        if req_max_vram and gpu_vram_gb > req_max_vram:
            return 0.0, [
                f"VRAM above recipe ceiling: recipe caps at {req_max_vram}GB, "
                f"system has {gpu_vram_gb:.1f}GB"
            ]

    # Unified memory: warn if mismatch but don't fail
    if req_unified and not unified_memory:
        warnings.append("Recipe prefers unified memory, system has discrete GPU")
    elif not req_unified and unified_memory:
        warnings.append("Recipe is discrete GPU-only, system has unified memory")

    # RAM (hard constraint)
    if ram_gb < req_min_ram:
        return 0.0, [f"Insufficient RAM: recipe needs {req_min_ram}GB, system has {ram_gb:.1f}GB"]

    # CPU cores (hard constraint)
    if cpu_cores < req_min_cores:
        return 0.0, [f"Insufficient cores: recipe needs {req_min_cores}, system has {cpu_cores}"]

    # All hard constraints met: return 1.0. Tier rank handles the tie-breaking.
    return 1.0, warnings


def resolve_recipe(
    system_info: dict,
    prefer_backend: str = "auto",
    recipe_id: str = "",
) -> dict:
    """Resolve the best recipe for the given system hardware.

    Args:
        system_info: dict with keys: ram_gb, cpu_cores, gpu_vendor, gpu_vram_mb,
                     unified_memory, gpu_name
        prefer_backend: "auto" (use resolution), or specific backend name (e.g., "ollama")
        recipe_id: explicit recipe ID to use (overrides resolution)

    Returns:
        {
            "recipe": <loaded recipe dict>,
            "match_score": <float>,
            "rationale": <str>,
            "warnings": [<str>, ...],
        }

    If no recipe fits: returns cloud-api fallback.
    """
    # Explicit recipe_id wins — checked BEFORE the empty-system_info fallback,
    # otherwise an explicit request with no hardware info silently returned
    # cloud-api (bug found by the strengthened traps test).
    if recipe_id and recipe_id in RECIPE_IDS:
        recipe = _load_recipe(recipe_id)
        if recipe:
            warnings = []
            if "platform_traps" in recipe.get("inference_config", {}):
                warnings.extend(recipe["inference_config"]["platform_traps"])
            return {
                "recipe": recipe,
                "match_score": 10.0,
                "rationale": f"Explicit recipe_id: {recipe_id}",
                "warnings": warnings,
            }
        else:
            logger.warning("Failed to load explicit recipe %s, using cloud-api", recipe_id)

    # Score all recipes
    candidates = []
    for rid in RECIPE_IDS:
        recipe = _load_recipe(rid)
        if not recipe:
            continue
        # `auto_select: false` = reachable by explicit id, never chosen for you. Now that the
        # id list comes off disk, any new recipe would otherwise silently enter the ranking
        # and could displace a fleet default by tier-and-alphabet accident.
        if recipe.get("auto_select") is False:
            continue
        score, warnings = _score_recipe(recipe, system_info)
        if score > 0 or recipe.get("id") == "cloud-api":  # cloud-api always available
            candidates.append((rid, recipe, score, warnings))

    if not candidates:
        # Fallback to cloud-api
        recipe = _load_recipe("cloud-api")
        return {
            "recipe": recipe,
            "match_score": 0.0,
            "rationale": "No matching recipes; falling back to cloud API.",
            "warnings": ["No local recipe matched hardware"],
        }

    # Sort by score (descending), then by tier rank (higher tier for ties)
    # Since all matching recipes have score 1.0, tier becomes the primary sort
    def sort_key(item):
        rid, recipe, score, _warnings = item
        tier = recipe.get("tier", "cloud")
        tier_rank = TIER_RANKS.get(tier, 0)
        # Prefer SELF-CONTAINED recipes over fleet-delegate ones on ties: a
        # delegate recipe needs fleet playbooks/scripts the public pack does not
        # ship, so it must never win by alphabetical accident (cpu-1bit-llamacpp
        # was beating cpu-ollama for every big CPU box exactly this way).
        needs_delegate = 1 if recipe.get("deployment", {}).get("delegate") else 0
        # Sort: desc score, desc tier rank, self-contained first, then ID (stability)
        return (-score, -tier_rank, needs_delegate, rid)

    candidates.sort(key=sort_key)

    # prefer_backend filter: keep only recipes whose engine matches the requested
    # backend. If nothing matches (e.g. prefer "ollama" on a box that scored no
    # ollama recipe), fall back to the unfiltered ranking with a warning rather
    # than failing — the preference is a tiebreaker, not a hard gate.
    prefer_warning = []
    if prefer_backend and prefer_backend != "auto":
        preferred = [
            c for c in candidates
            if c[1].get("inference_config", {}).get("engine", "") == prefer_backend
        ]
        if preferred:
            candidates = preferred
        else:
            prefer_warning = [
                f"prefer_backend={prefer_backend!r} matched no viable recipe "
                "for this hardware; using best overall match instead"
            ]

    # Pick the best match
    best_id, best_recipe, best_score, best_warnings = candidates[0]
    best_warnings = list(best_warnings) + prefer_warning

    # Compute rationale based on system info
    gpu_vendor = system_info.get("gpu_vendor", "none")
    gpu_vram_gb = system_info.get("gpu_vram_mb", 0) / 1024
    ram_gb = system_info.get("ram_gb", 0)
    cpu_cores = system_info.get("cpu_cores", 0)

    rationale_parts = [
        f"System: {cpu_cores} cores, {ram_gb:.1f}GB RAM, {gpu_vendor}",
    ]
    if gpu_vram_gb > 0:
        rationale_parts.append(f"{gpu_vram_gb:.1f}GB VRAM")
    rationale = " — ".join(rationale_parts) + f". Best match: {best_id} (score {best_score:.1f})"

    return {
        "recipe": best_recipe,
        "match_score": best_score,
        "rationale": rationale,
        "warnings": best_warnings,
    }


def get_recipe(recipe_id: str) -> Optional[dict]:
    """Load a recipe by explicit ID. Returns None if not found."""
    if recipe_id not in RECIPE_IDS:
        return None
    return _load_recipe(recipe_id)


def list_recipes() -> list[str]:
    """Return all available recipe IDs."""
    return RECIPE_IDS
