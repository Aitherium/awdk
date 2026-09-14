"""Recipe engine for the graphrag pack — embedder recipes + role resolution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("graphrag_recipes")

RECIPE_IDS = ["nomic-text-embed", "coderank-embed"]

ROLE_DEFAULTS = {
    "text": "nomic-text-embed",
    "docs": "nomic-text-embed",
    "prose": "nomic-text-embed",
    "code": "coderank-embed",
}


def _load_recipe(recipe_id: str) -> dict:
    try:
        p = Path(__file__).parent / "recipes" / f"{recipe_id}.yaml"
        if not p.exists():
            logger.error("Recipe not found: %s", p)
            return {}
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to load recipe %s: %s", recipe_id, e)
        return {}


def resolve_by_role_or_id(role_or_id: str) -> Optional[str]:
    if role_or_id in RECIPE_IDS:
        return role_or_id
    return ROLE_DEFAULTS.get(role_or_id)


def get_recipe(recipe_id: str) -> Optional[dict]:
    if recipe_id not in RECIPE_IDS:
        return None
    return _load_recipe(recipe_id) or None


def list_recipes() -> list[str]:
    return RECIPE_IDS
