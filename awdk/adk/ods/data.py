"""Catalog loading and validation for ODS model database."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from .model_types import OdsError

# NO selection tunables live here. They are all upstream's, in
# `_upstream_select.py` (VRAM_FIT_TOLERANCE_GB, the score weights, and
# `usable_memory_gb`'s memory shares). This module previously re-declared them
# — leftovers from the discarded reimplementation — including a
# MEMORY_USAGE_DISCRETE_PERCENT = 0.95 that CONTRADICTED upstream's 100% and
# was read by nothing. A stale duplicate of a number that decides model fit is
# worse than no constant: the next reader treats it as the source of truth.
# If you need a tunable, read it from `_upstream_select`.


def _validate_model_schema(model: dict[str, Any]) -> None:
    """Validate a single model record against expected schema.

    Raises OdsError if schema is invalid.
    """
    # Fields present on EVERY record of the real upstream catalog (verified against
    # ODS 2.5.3: 52/52). install_recommendation/app_compatibility are on 32/52 and
    # runtime_profiles on 3/52 — requiring them rejects the genuine upstream file.
    required_fields = {
        "id", "name", "family", "gguf_file", "gguf_url", "gguf_sha256",
        "size_mb", "vram_required_gb", "context_length", "quantization",
        "specialty", "llm_model_name",
    }
    missing = required_fields - set(model.keys())
    if missing:
        raise OdsError(
            f"Model {model.get('id', 'unknown')} missing required fields: {missing}"
        )

    # Type checks
    if not isinstance(model["id"], str):
        raise OdsError(f"Model id must be str, got {type(model['id'])}")
    if not isinstance(model["family"], str):
        raise OdsError(f"Model family must be str, got {type(model['family'])}")
    if not isinstance(model["vram_required_gb"], (int, float)):
        raise OdsError(
            f"Model vram_required_gb must be numeric, got {type(model['vram_required_gb'])}"
        )
    if model["vram_required_gb"] <= 0:
        raise OdsError(
            f"Model {model['id']} vram_required_gb must be > 0, "
            f"got {model['vram_required_gb']}"
        )
    if not isinstance(model["context_length"], int):
        raise OdsError(
            f"Model context_length must be int, got {type(model['context_length'])}"
        )
    # Optional upstream field — type-check only when present.
    if "install_recommendation" in model and not isinstance(
        model["install_recommendation"], bool
    ):
        raise OdsError(
            "Model install_recommendation must be bool, got "
            f"{type(model['install_recommendation'])}"
        )
    if not isinstance(model.get("size_mb"), (int, float)):
        raise OdsError(
            f"Model {model['id']} size_mb must be numeric, got {type(model.get('size_mb'))}"
        )
    if model.get("size_mb", 0) <= 0:
        raise OdsError(
            f"Model {model['id']} size_mb must be > 0, got {model.get('size_mb')}"
        )


def _validate_gpu_database_schema(db: dict[str, Any]) -> None:
    """Validate gpu-database.json schema.

    Raises OdsError if invalid.
    """
    if "known_gpus" not in db:
        raise OdsError("gpu-database missing 'known_gpus' key")
    if "heuristic_classes" not in db:
        raise OdsError("gpu-database missing 'heuristic_classes' key")
    # Upstream shape (ODS 2.5.3): known_gpus is a LIST of {id, match, specs} entries,
    # not a vendor->device mapping.
    if not isinstance(db.get("known_gpus"), list):
        raise OdsError("gpu-database 'known_gpus' must be a list")
    if not isinstance(db.get("heuristic_classes"), list):
        raise OdsError("gpu-database 'heuristic_classes' must be list")


@lru_cache(maxsize=1)
def load_catalog(catalog_path: str | None = None) -> dict[str, Any]:
    """Load and validate ODS model catalog.

    Lazy-loads JSON files from package data on first call, then caches.
    Subsequent calls return cached result.

    Args:
        catalog_path: Explicit path to model-library.json (for testing).
                      If None, loads from package data (adk/ods:model-library.json).

    Returns:
        Catalog dict with keys: version, models, metadata.

    Raises:
        OdsError: If catalog is missing, corrupt, or schema is invalid.
    """
    if catalog_path:
        try:
            with open(catalog_path, encoding="utf-8") as f:
                catalog = json.load(f)
        except FileNotFoundError:
            raise OdsError(f"Catalog file not found: {catalog_path}")
        except json.JSONDecodeError as e:
            raise OdsError(f"Catalog JSON invalid: {e}")
    else:
        # Load from package data via importlib.resources
        try:
            pkg_files = files("adk.ods")
            model_lib_file = pkg_files / "model-library.json"
            catalog_text = model_lib_file.read_text(encoding="utf-8")
            catalog = json.loads(catalog_text)
        except (FileNotFoundError, AttributeError) as e:
            raise OdsError(f"Failed to load model-library.json from package: {e}")
        except json.JSONDecodeError as e:
            raise OdsError(f"model-library.json is invalid JSON: {e}")

    # Validate structure
    if "version" not in catalog:
        raise OdsError("Catalog missing 'version' key")
    if "models" not in catalog:
        raise OdsError("Catalog missing 'models' key")
    if not isinstance(catalog["models"], list):
        raise OdsError("Catalog 'models' must be a list")

    # Validate each model
    for model in catalog["models"]:
        _validate_model_schema(model)

    return catalog


def load_gpu_database(db_path: str | None = None) -> dict[str, Any]:
    """Load and validate ODS GPU hardware database.

    Args:
        db_path: Explicit path to gpu-database.json (for testing).
                 If None, loads from package data (adk/ods:gpu-database.json).

    Returns:
        GPU database dict with keys: known_gpus, heuristic_classes, known_gpu_bandwidth, defaults.

    Raises:
        OdsError: If database is missing, corrupt, or schema is invalid.
    """
    if db_path:
        try:
            with open(db_path, encoding="utf-8") as f:
                db = json.load(f)
        except FileNotFoundError:
            raise OdsError(f"GPU database file not found: {db_path}")
        except json.JSONDecodeError as e:
            raise OdsError(f"GPU database JSON invalid: {e}")
    else:
        try:
            pkg_files = files("adk.ods")
            gpu_db_file = pkg_files / "gpu-database.json"
            db_text = gpu_db_file.read_text(encoding="utf-8")
            db = json.loads(db_text)
        except (FileNotFoundError, AttributeError) as e:
            raise OdsError(f"Failed to load gpu-database.json from package: {e}")
        except json.JSONDecodeError as e:
            raise OdsError(f"gpu-database.json is invalid JSON: {e}")

    _validate_gpu_database_schema(db)
    return db


def load_hardware_classes(hw_path: str | None = None) -> dict[str, Any]:
    """Load the compose-overlay class table (hardware-classes.json).

    Consumed by `adk.ods.hardware.classify_host()`, which is the only caller.
    This file's unique payload is `recommended.compose_overlays` — the per-class
    tier is better sourced from gpu-database.json, which carries exact device
    knowledge. (Originally this file was intended to hold tier information,
    but gpu-database.json proved to be the authoritative source.)

    Args:
        hw_path: Explicit path to hardware-classes.json (for testing).
                 If None, loads from package data (adk/ods:hardware-classes.json).

    Returns:
        Hardware classes dict: {"version": ..., "classes": [...]}.

    Raises:
        OdsError: If file is missing, corrupt, or invalid.
    """
    if hw_path:
        try:
            with open(hw_path, encoding="utf-8") as f:
                hw = json.load(f)
        except FileNotFoundError:
            raise OdsError(f"Hardware classes file not found: {hw_path}")
        except json.JSONDecodeError as e:
            raise OdsError(f"Hardware classes JSON invalid: {e}")
    else:
        try:
            pkg_files = files("adk.ods")
            hw_file = pkg_files / "hardware-classes.json"
            hw_text = hw_file.read_text(encoding="utf-8")
            hw = json.loads(hw_text)
        except (FileNotFoundError, AttributeError) as e:
            raise OdsError(f"Failed to load hardware-classes.json from package: {e}")
        except json.JSONDecodeError as e:
            raise OdsError(f"hardware-classes.json is invalid JSON: {e}")

    if "version" not in hw:
        raise OdsError("Hardware classes missing 'version' key")
    if "classes" not in hw:
        raise OdsError("Hardware classes missing 'classes' key")

    return hw


def validate_catalog(catalog_path: str) -> tuple[bool, str]:
    """Validate model-library.json schema.

    Used in re-vendor procedure to ensure upstream catalogs are compatible
    before committing.

    Args:
        catalog_path: Path to model-library.json.

    Returns:
        (is_valid, error_message_or_empty)
    """
    try:
        with open(catalog_path, encoding="utf-8") as f:
            catalog = json.load(f)
    except FileNotFoundError:
        return False, f"Catalog file not found: {catalog_path}"
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    except OSError as e:
        return False, f"Cannot read file: {e}"

    # Validate structure
    if "version" not in catalog:
        return False, "Catalog missing 'version' key"
    if "models" not in catalog:
        return False, "Catalog missing 'models' key"
    if not isinstance(catalog.get("models"), list):
        return False, "Catalog 'models' must be a list"

    models = catalog.get("models", [])
    if not models:
        return False, "Catalog 'models' list is empty"

    # Validate each model
    for i, model in enumerate(models):
        if not isinstance(model, dict):
            return False, f"Model at index {i} is not a dict"
        # Validate via existing schema check
        try:
            _validate_model_schema(model)
        except OdsError as e:
            return False, str(e)

    return True, ""


def validate_gpu_database(db_path: str) -> tuple[bool, str]:
    """Validate gpu-database.json schema.

    Args:
        db_path: Path to gpu-database.json.

    Returns:
        (is_valid, error_message_or_empty)
    """
    try:
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
    except FileNotFoundError:
        return False, f"GPU database file not found: {db_path}"
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    except OSError as e:
        return False, f"Cannot read file: {e}"

    # Validate via existing schema check
    try:
        _validate_gpu_database_schema(db)
    except OdsError as e:
        return False, str(e)

    return True, ""
