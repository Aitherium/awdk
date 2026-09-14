"""Open Data Systems (ODS) model resolver for local LLM inference.

Vendored from https://github.com/Osmantic/ODS (Apache License 2.0).
Provides deterministic, offline model selection based on hardware constraints.

Architecture & Integration:
- See adk/ods/adk_integration.md for system_info() flow, hardware detection, tier mapping
- See adk/ods/README.md for catalog schema, edge rules, re-vendor procedure
- See NOTICE for attribution and material changes from upstream

Usage:
    from adk.ods import OdsResolver, OdsRecommendation

    resolver = OdsResolver()  # Loads vendored catalog via importlib.resources
    recommendation = resolver.resolve(
        backend='nvidia', vram_mb=24576, ram_gb=32, profile='qwen', tier='3'
    )
    print(recommendation.selected.llm_model_name)  # e.g. 'qwen3.5-7b-instruct'

Versioning:
    ODS_VENDORED_COMMIT and ODS_LAST_VENDORED_DATE track upstream source.
    Callers can log which ODS version is in use:
        print(f"ODS catalog {ODS_VENDORED_COMMIT} vendored {ODS_LAST_VENDORED_DATE}")
"""

from __future__ import annotations

from .data import load_catalog
from .model_types import ModelRecord, OdsError, OdsRecommendation
from .resolver import OdsResolver

__all__ = [
    "OdsResolver",
    "OdsRecommendation",
    "ModelRecord",
    "OdsError",
    "load_catalog",
    "ODS_VENDORED_COMMIT",
    "ODS_VENDORED_REF",
    "ODS_VENDORED_RELEASE",
    "ODS_VENDORED_SHA256",
    "ODS_VENDORED_URL",
    "ODS_SCHEMA_VERSION",
    "ODS_LAST_VENDORED_DATE",
]

# Vendored metadata — track upstream source for traceability.
# These must be updated when re-vendoring from upstream ODS repository.
# Use: python adk/ods/tools/validate_catalog.py to verify after update.
#
# PROVENANCE HONESTY: the catalog was vendored from a GitHub *zipball* of
# Osmantic/ODS `main`, which carries no commit SHA. We therefore do NOT publish a
# commit id — an invented-but-plausible SHA is worse than none, because it reads
# as verified provenance. Integrity is instead pinned by the sha256 of each
# vendored file (verifiable with `python adk/ods/tools/validate_catalog.py`).
# If a future re-vendor uses `git clone`, set ODS_VENDORED_COMMIT to the real SHA.
ODS_VENDORED_COMMIT: str | None = None  # unknown: sourced from a zipball, not a clone
ODS_VENDORED_REF = "main"
ODS_VENDORED_RELEASE = "2.5.3"  # ods/manifest.json ods_version at time of vendoring
ODS_VENDORED_URL = "https://github.com/Osmantic/ODS"
ODS_LAST_VENDORED_DATE = "2026-07-25"  # ISO8601 date when catalog was last re-vendored

# Schema versions as declared by the vendored files themselves (not hand-set).
ODS_SCHEMA_VERSION = "model-library=2; hardware-classes=1; gpu-database=see schema_version"

# sha256 of each vendored file exactly as shipped. These are the integrity anchor:
# validate_catalog.py recomputes them, so an accidental hand-edit to vendored data
# fails loudly instead of silently drifting from upstream.
ODS_VENDORED_SHA256 = {
    "model-library.json": "3cb860d0c7cd04cd6be812dd0549ce6b22b0b6029bc992c20eae48dc73de18b0",
    "gpu-database.json": "4feaade5e104a9a00c8d42abba4aba89d4a6df1463c53e21ed59f2be7b9fa09f",
    "hardware-classes.json": "82e34a6c6bc07908695c312b108b6b0e1057f73f9738dc067f8fb8f0bbc55ea5",
    # ODS scripts/select-model.py — the reference selection algorithm itself,
    # carried as code rather than reimplemented. resolver.py wraps it.
    "_upstream_select.py": "6a0e7fa2dfc0c0188b6fc127af884f9087f0af2cd0e84edab4032dbb5f3b1020",
    # ODS scripts/classify-hardware.sh — the reference HARDWARE classifier.
    # hardware.py is a port of it (it is bash-wrapped Python, so it cannot be
    # imported the way select-model.py can), and tests/test_classify_differential.py
    # runs the vendored original as the reference for every ported decision.
    "_upstream_classify.sh": "7c807f9741c95f40005205f42fca15d2f060e3b92d430ae1824b2f9405f56769",
}

__version__ = "0.1.0"
__doc_uri__ = "https://github.com/Osmantic/ODS"
