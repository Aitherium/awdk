"""Recipe engine for the llm-serving pack.

Loads per-MODEL YAML recipes and resolves them against detected hardware. Unlike
node_bootstrap (which picks ONE recipe for a box), this pack is model-first: you
name the model role you want, and the recipe + the hardware-adaptive quant decide
HOW to serve it here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("llm_serving_recipes")

RECIPE_IDS = [
    "nemotron-orchestrator-8b",
    "gemma4-12b",
    "qwen-27b-reason",
    "deepseek-r1-14b",
]

# role -> default recipe, so a caller can ask by role without knowing the model id.
ROLE_DEFAULTS = {
    "orchestrator": "nemotron-orchestrator-8b",
    "perception": "gemma4-12b",
    "vision": "gemma4-12b",
    "reasoner": "qwen-27b-reason",
    "reasoning": "qwen-27b-reason",
}


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


def resolve_by_role_or_id(role_or_id: str) -> Optional[str]:
    """Map a role name OR a recipe id to a recipe id. None if neither matches."""
    if role_or_id in RECIPE_IDS:
        return role_or_id
    return ROLE_DEFAULTS.get(role_or_id)


def fits_hardware(recipe: dict, system_info: dict) -> tuple[bool, list[str]]:
    """Hard-constraint check: does this model fit the box AT ALL? (score-free.)

    Returns (fits, reasons). A model that does not fit local VRAM in ANY quant is
    rejected — this pack does not silently swap to CPU or a different model.
    """
    reqs = recipe.get("hardware_requirements", {})
    reasons = []
    gpu_vram_gb = system_info.get("gpu_vram_mb", 0) / 1024
    ram_gb = system_info.get("ram_gb", 0)
    vendor = system_info.get("gpu_vendor", "none")

    if reqs.get("gpu_vendor") == "nvidia" and vendor != "nvidia":
        reasons.append(f"needs an NVIDIA GPU, found {vendor}")
    if gpu_vram_gb < reqs.get("min_vram_gb", 0):
        reasons.append(
            f"needs {reqs.get('min_vram_gb')}GB VRAM, has {gpu_vram_gb:.1f}GB"
        )
    if ram_gb < reqs.get("min_ram_gb", 0):
        reasons.append(f"needs {reqs.get('min_ram_gb')}GB RAM, has {ram_gb:.1f}GB")
    return (not reasons), reasons


def get_recipe(recipe_id: str) -> Optional[dict]:
    """Load a recipe by id. Returns None if unknown or unloadable."""
    if recipe_id not in RECIPE_IDS:
        return None
    return _load_recipe(recipe_id) or None


def list_recipes() -> list[str]:
    """Return all available recipe ids."""
    return RECIPE_IDS
