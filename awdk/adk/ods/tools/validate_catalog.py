"""Schema validation tool for ODS catalogs.

Invoked during re-vendor procedure to ensure upstream ODS catalogs are compatible
before committing to the repository. Checks model-library.json and gpu-database.json
schema compliance.

Exit codes:
    0: All validations passed
    1: Schema mismatch or invalid data
    2: File not found
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Tuple


def verify_vendored_hashes() -> Tuple[bool, str]:
    """Recompute sha256 of each vendored file and compare to the recorded anchor.

    This is what makes the vendored data tamper-evident: an accidental hand-edit
    to a file that is supposed to be byte-identical to upstream fails loudly here
    instead of silently drifting. Vendoring hashes and never checking them is the
    silent-no-op pattern this function exists to prevent.

    Returns:
        (all_match, report_or_error)
    """
    from adk.ods import ODS_VENDORED_SHA256

    ods_dir = Path(__file__).resolve().parent.parent
    lines: list[str] = []
    ok = True
    for filename, expected in sorted(ODS_VENDORED_SHA256.items()):
        target = ods_dir / filename
        if not target.exists():
            lines.append(f"  MISSING  {filename}")
            ok = False
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual == expected:
            lines.append(f"  OK       {filename}  {actual[:16]}...")
        else:
            lines.append(
                f"  MISMATCH {filename}\n"
                f"    expected {expected}\n"
                f"    actual   {actual}"
            )
            ok = False
    return ok, "\n".join(lines)


def validate_model_schema(model: dict[str, Any]) -> Tuple[bool, str]:
    """Validate a single model record against expected schema.

    Args:
        model: Model dict from catalog.

    Returns:
        (is_valid, error_message_or_empty)
    """
    # Fields present on EVERY record of the real upstream catalog (verified against
    # ODS 2.5.3: 52/52 models). Do not add fields here without checking coverage —
    # install_recommendation/app_compatibility appear on only 32/52 records and
    # runtime_profiles on 3/52, so requiring them rejects the genuine upstream file.
    required_fields = {
        "id",
        "name",
        "family",
        "gguf_file",
        "gguf_url",
        "gguf_sha256",
        "size_mb",
        "vram_required_gb",
        "context_length",
        "quantization",
        "specialty",
        "description",
        "tokens_per_sec_estimate",
        "llm_model_name",
        "llama_server_image",
    }

    missing = required_fields - set(model.keys())
    if missing:
        return False, f"Model {model.get('id', 'unknown')} missing fields: {missing}"

    # Type checks
    if not isinstance(model.get("id"), str):
        return False, f"Model id must be str, got {type(model.get('id'))}"
    if not isinstance(model.get("family"), str):
        return False, f"Model family must be str, got {type(model.get('family'))}"
    if not isinstance(model.get("vram_required_gb"), (int, float)):
        return (
            False,
            f"Model vram_required_gb must be numeric, "
            f"got {type(model.get('vram_required_gb'))}",
        )
    if not isinstance(model.get("context_length"), int):
        return (
            False,
            f"Model context_length must be int, got {type(model.get('context_length'))}",
        )
    # Optional in the real catalog — type-check only when actually present.
    if "install_recommendation" in model and not isinstance(
        model["install_recommendation"], bool
    ):
        return (
            False,
            f"Model install_recommendation must be bool, "
            f"got {type(model.get('install_recommendation'))}",
        )
    # Upstream shape: app_compatibility is a MAPPING of surface -> {status, reason}
    # (e.g. {"openai_chat": {"status": "verified", "reason": "..."}}), not a list.
    if "app_compatibility" in model and not isinstance(model["app_compatibility"], dict):
        return (
            False,
            f"Model app_compatibility must be a dict of surface->{{status,reason}}, "
            f"got {type(model.get('app_compatibility'))}",
        )

    # Installability constraint: if install_recommendation=true, gguf_url must be present
    if model.get("install_recommendation") is True:
        gguf_url = model.get("gguf_url", "").strip()
        if not gguf_url:
            return (
                False,
                f"Model {model.get('id')} has install_recommendation=true "
                f"but missing/empty gguf_url",
            )

    # Bounds checks
    vram_gb = model.get("vram_required_gb", 0)
    if vram_gb < 0 or vram_gb > 1000:
        return (
            False,
            f"Model {model.get('id')} vram_required_gb out of bounds: {vram_gb}",
        )
    context = model.get("context_length", 0)
    # Upstream catalog carries context windows up to 262144 (qwen3-4b-instruct-2507).
    if context < 0 or context > 1_048_576:
        return (
            False,
            f"Model {model.get('id')} context_length out of bounds: {context}",
        )
    size_mb = model.get("size_mb", 0)
    if size_mb < 0 or size_mb > 200000:
        return (
            False,
            f"Model {model.get('id')} size_mb out of bounds: {size_mb}",
        )

    return True, ""


def validate_catalog(catalog_path: str) -> Tuple[bool, str]:
    """Validate model-library.json schema.

    Args:
        catalog_path: Path to model-library.json.

    Returns:
        (is_valid, error_message_or_empty)
    """
    path = Path(catalog_path)
    if not path.exists():
        return False, f"Catalog file not found: {catalog_path}"

    try:
        with open(path, encoding="utf-8") as f:
            catalog = json.load(f)
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
        is_valid, error = validate_model_schema(model)
        if not is_valid:
            return False, error

    return True, ""


def validate_gpu_database(db_path: str) -> Tuple[bool, str]:
    """Validate gpu-database.json schema.

    Args:
        db_path: Path to gpu-database.json.

    Returns:
        (is_valid, error_message_or_empty)
    """
    path = Path(db_path)
    if not path.exists():
        return False, f"GPU database file not found: {db_path}"

    try:
        with open(path, encoding="utf-8") as f:
            db = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    except OSError as e:
        return False, f"Cannot read file: {e}"

    # Validate structure
    if "known_gpus" not in db:
        return False, "GPU database missing 'known_gpus' key"
    if "heuristic_classes" not in db:
        return False, "GPU database missing 'heuristic_classes' key"
    # Upstream shape (ODS 2.5.3): known_gpus is a LIST of entries, each
    # {id, match{device_ids, name_patterns}, specs{...}} — not a vendor->device dict.
    if not isinstance(db.get("known_gpus"), list):
        return False, "GPU database 'known_gpus' must be a list"
    if not isinstance(db.get("heuristic_classes"), list):
        return False, "GPU database 'heuristic_classes' must be list"

    for i, entry in enumerate(db.get("known_gpus", [])):
        if not isinstance(entry, dict):
            return False, f"known_gpus[{i}] is not a dict"
        if "id" not in entry:
            return False, f"known_gpus[{i}] missing 'id' key"
        specs = entry.get("specs")
        if not isinstance(specs, dict):
            return False, f"known_gpus[{entry.get('id')}] 'specs' must be a dict"
        for field in ("vendor", "memory_type", "memory_mb"):
            if field not in specs:
                return (
                    False,
                    f"known_gpus[{entry.get('id')}] specs missing '{field}'",
                )

    # Upstream shape: each heuristic class is {id, match{vendor,...}, recommended{backend,tier}}.
    # vendor/tier live nested under match/recommended, not at the top level.
    for i, heuristic in enumerate(db.get("heuristic_classes", [])):
        if not isinstance(heuristic, dict):
            return False, f"Heuristic class at index {i} is not a dict"
        if "id" not in heuristic:
            return False, f"Heuristic class at index {i} missing 'id' key"
        match = heuristic.get("match")
        if not isinstance(match, dict) or "vendor" not in match:
            return (
                False,
                f"Heuristic class {heuristic.get('id', i)} missing 'match.vendor'",
            )
        recommended = heuristic.get("recommended")
        if not isinstance(recommended, dict) or "tier" not in recommended:
            return (
                False,
                f"Heuristic class {heuristic.get('id', i)} missing 'recommended.tier'",
            )

    return True, ""


def main() -> int:
    """Main entry point.

    Returns:
        Exit code: 0 on success, 1 on validation failure, 2 on file not found.
    """
    parser = argparse.ArgumentParser(
        description="Validate ODS catalog and GPU database schema.",
    )
    parser.add_argument(
        "file",
        type=str,
        nargs="?",
        help="Path to catalog or GPU database JSON file to validate",
    )
    parser.add_argument(
        "--verify-vendored",
        action="store_true",
        help="Recompute sha256 of every vendored file and compare to the recorded anchor",
    )
    parser.add_argument(
        "--type",
        choices=["auto", "catalog", "gpu-database"],
        default="auto",
        help="Type of file (auto-detect from filename if auto)",
    )
    args = parser.parse_args()

    if args.verify_vendored:
        ok, report = verify_vendored_hashes()
        print("Vendored-file integrity (sha256 vs adk.ods.ODS_VENDORED_SHA256):")
        print(report)
        if not ok:
            print(
                "[FAIL] vendored data differs from the recorded upstream snapshot",
                file=sys.stderr,
            )
            return 1
        print("[OK] all vendored files match their recorded hashes")
        if args.file is None:
            return 0

    if args.file is None:
        parser.error("a file argument is required unless --verify-vendored is used")

    file_type = args.type
    if file_type == "auto":
        if "gpu-database" in args.file:
            file_type = "gpu-database"
        elif "model-library" in args.file or "catalog" in args.file:
            file_type = "catalog"
        else:
            print(f"Cannot auto-detect file type: {args.file}", file=sys.stderr)
            return 1

    if file_type == "catalog":
        is_valid, error = validate_catalog(args.file)
    else:
        is_valid, error = validate_gpu_database(args.file)

    if is_valid:
        print(f"[OK] {args.file} schema is valid")
        return 0
    else:
        print(f"[FAIL] {args.file} schema validation failed:", file=sys.stderr)
        print(f"  {error}", file=sys.stderr)
        if "not found" in error.lower():
            return 2
        return 1


if __name__ == "__main__":
    sys.exit(main())
