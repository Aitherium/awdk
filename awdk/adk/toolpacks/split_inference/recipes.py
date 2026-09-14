"""Recipe engine for the split-inference pack.

Loads YAML recipe definitions from recipes/ and resolves them against a detected
TOPOLOGY (local devices + reachable RPC backends), rather than against a single
machine's hardware the way node_bootstrap does.

Resolution: explicit recipe_id wins; else score by combined VRAM and the number of
reachable RPC backends; nothing fits => single-node fallback (an honest "this is
not a split" rather than a pretend one).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("split_inference_recipes")

RECIPE_IDS = [
    "single-node-cuda",
    "bonsai-27b-5090-dgx-rpc",
    "multi-node-rpc-generic",
    "kimi-k3-mesh-rpc",
]

# Honest fallback: zero remote backends is not a split, and we say so.
FALLBACK_RECIPE_ID = "single-node-cuda"

TIER_RANKS = {
    "single-node-gpu": 1,
    "multi-node-gpu": 2,
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


def _score_recipe(recipe: dict, topology: dict) -> tuple[float, list[str]]:
    """Score a recipe against a detected topology. Returns (score, warnings).

    Hard constraints return 0.0 with the reason so a caller can always explain why
    a recipe was rejected.
    """
    warnings: list[str] = []
    reqs = recipe.get("hardware_requirements", {})

    local_vram = topology.get("local_vram_gb", 0)
    combined_vram = topology.get("combined_vram_gb", 0)
    n_backends = len(topology.get("rpc_backends", []) or [])
    ram_gb = topology.get("ram_gb", 0)
    cpu_cores = topology.get("cpu_cores", 0)

    if ram_gb < reqs.get("min_ram_gb", 0):
        return 0.0, [
            f"Insufficient RAM: needs {reqs.get('min_ram_gb')}GB, has {ram_gb:.1f}GB"
        ]
    if cpu_cores < reqs.get("min_cpu_cores", 0):
        return 0.0, [
            f"Insufficient cores: needs {reqs.get('min_cpu_cores')}, has {cpu_cores}"
        ]
    if local_vram < reqs.get("min_local_vram_gb", 0):
        return 0.0, [
            f"Insufficient local VRAM: needs {reqs.get('min_local_vram_gb')}GB, "
            f"has {local_vram:.1f}GB"
        ]
    if combined_vram < reqs.get("min_combined_vram_gb", 0):
        return 0.0, [
            f"Insufficient combined VRAM: needs {reqs.get('min_combined_vram_gb')}GB, "
            f"has {combined_vram:.1f}GB"
        ]
    # A split recipe with no reachable backend is not viable — refusing here is what
    # keeps us from "deploying a split" that is silently local-only.
    req_backends = reqs.get("min_rpc_backends", 0)
    if n_backends < req_backends:
        return 0.0, [
            f"Needs {req_backends} reachable RPC backend(s), found {n_backends}"
        ]

    return 1.0, warnings


def resolve_recipe(
    topology: dict,
    recipe_id: str = "",
) -> dict:
    """Resolve the best split recipe for a detected topology.

    Args:
        topology: {local_vram_gb, combined_vram_gb, rpc_backends: [...],
                   ram_gb, cpu_cores}
        recipe_id: explicit recipe ID (overrides resolution)

    Returns {recipe, match_score, rationale, warnings, rejected}. Never raises.
    """
    if recipe_id and recipe_id in RECIPE_IDS:
        recipe = _load_recipe(recipe_id)
        if recipe:
            return {
                "recipe": recipe,
                "match_score": 10.0,
                "rationale": f"Explicit recipe_id: {recipe_id}",
                "warnings": list(recipe.get("serve", {}).get("platform_traps", [])),
            }
        logger.warning("Failed to load explicit recipe %s, falling back", recipe_id)

    candidates = []
    rejections: dict[str, str] = {}
    for rid in RECIPE_IDS:
        recipe = _load_recipe(rid)
        if not recipe:
            continue
        score, warnings = _score_recipe(recipe, topology)
        if score > 0 or rid == FALLBACK_RECIPE_ID:
            candidates.append((rid, recipe, score, warnings))
        else:
            rejections[rid] = warnings[0] if warnings else "did not match"

    if not candidates:
        recipe = _load_recipe(FALLBACK_RECIPE_ID)
        return {
            "recipe": recipe,
            "match_score": 0.0,
            "rationale": "No split recipe matched; falling back to single-node.",
            "warnings": ["No multi-node recipe matched this topology"],
            "rejected": rejections,
        }

    def sort_key(item):
        rid, recipe, score, _w = item
        tier_rank = TIER_RANKS.get(recipe.get("tier", "single-node-gpu"), 0)
        # Prefer a CONCRETE topology recipe over the generic one on ties so the
        # named reference deployment wins when its hosts are actually present.
        generic = 1 if rid.endswith("-generic") else 0
        return (-score, -tier_rank, generic, rid)

    candidates.sort(key=sort_key)
    best_id, best_recipe, best_score, best_warnings = candidates[0]
    best_warnings = list(best_warnings)
    best_warnings += list(best_recipe.get("serve", {}).get("platform_traps", []))

    n_backends = len(topology.get("rpc_backends", []) or [])
    rationale = (
        f"Topology: {topology.get('local_vram_gb', 0):.1f}GB local VRAM, "
        f"{n_backends} RPC backend(s), "
        f"{topology.get('combined_vram_gb', 0):.1f}GB combined. "
        f"Best match: {best_id} (score {best_score:.1f})"
    )

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
