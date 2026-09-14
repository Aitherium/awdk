"""Pytest fixtures for ODS resolver tests.

Provides mock catalogs, hardware profiles, and catalog loaders for unit/integration testing.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def mock_catalog() -> dict[str, Any]:
    """Basic ODS model catalog for testing.

    Includes:
    - qwen3.5-2b-q4 (bootstrap, 1.5GB VRAM)
    - qwen3-coder-next-32b-q4 (code specialty, 20GB VRAM)
    - qwen3.6-35b-a3b-ud-q4 (unified-memory variant, 25GB VRAM)
    - gemma4-9b-q4 (gemma4 family, 7GB VRAM)
    - qwen3.6-32b-q4 (general tier 2, 18GB VRAM)
    """
    return {
        "version": "1.0",
        "models": [
            {
                "id": "qwen3.5-2b-q4",
                "name": "Qwen 3.5 2B Q4",
                "family": "qwen",
                "gguf_file": "qwen3.5-2b-q4.gguf",
                "gguf_url": "https://huggingface.co/Qwen/qwen3.5-2b-q4/resolve/main/model.gguf",
                "gguf_sha256": "abc123",
                "size_mb": 1500,
                "vram_required_gb": 1.5,
                "context_length": 8192,
                "quantization": "q4",
                "specialty": "Bootstrap",
                "llm_model_name": "qwen3.5-2b-instruct",
                "install_recommendation": True,
                "runtime_profiles": {"qwen": {"tps": 50}},
                "app_compatibility": ["General", "Chat"],
            },
            {
                "id": "qwen3-coder-next-32b-q4",
                "name": "Qwen 3 Coder Next 32B Q4",
                "family": "qwen",
                "gguf_file": "qwen3-coder-next-32b-q4.gguf",
                "gguf_url": "https://huggingface.co/Qwen/qwen3-coder-32b-q4/resolve/main/model.gguf",
                "gguf_sha256": "def456",
                "size_mb": 18000,
                "vram_required_gb": 20.0,
                "context_length": 32768,
                "quantization": "q4",
                "specialty": "Code",
                "llm_model_name": "qwen3-32b-coder",
                "install_recommendation": True,
                "runtime_profiles": {"qwen": {"tps": 25}},
                "app_compatibility": ["Code", "General"],
            },
            {
                "id": "qwen3.6-35b-a3b-ud-q4",
                "name": "Qwen 3.6 35B A3B Unified Q4",
                "family": "qwen",
                "gguf_file": "qwen3.6-35b-a3b-ud-q4.gguf",
                "gguf_url": "https://huggingface.co/Qwen/qwen3.6-35b-a3b-ud-q4/resolve/main/model.gguf",
                "gguf_sha256": "ghi789",
                "size_mb": 20000,
                "vram_required_gb": 25.0,
                "context_length": 32768,
                "quantization": "q4",
                "specialty": "Quality",
                "llm_model_name": "qwen3.6-35b-instruct",
                "install_recommendation": True,
                "runtime_profiles": {"qwen": {"tps": 20}},
                "app_compatibility": ["General", "Code", "Chat"],
            },
            {
                "id": "gemma4-9b-q4",
                "name": "Gemma 4 9B Q4",
                "family": "gemma4",
                "gguf_file": "gemma4-9b-q4.gguf",
                "gguf_url": "https://huggingface.co/google/gemma4-9b-q4/resolve/main/model.gguf",
                "gguf_sha256": "jkl012",
                "size_mb": 6500,
                "vram_required_gb": 7.0,
                "context_length": 32768,
                "quantization": "q4",
                "specialty": "Balanced",
                "llm_model_name": "gemma-4-9b-it",
                "install_recommendation": True,
                "runtime_profiles": {"gemma4": {"tps": 45}},
                "app_compatibility": ["General", "Chat"],
            },
            {
                "id": "qwen3.6-32b-q4",
                "name": "Qwen 3.6 32B Q4",
                "family": "qwen",
                "gguf_file": "qwen3.6-32b-q4.gguf",
                "gguf_url": "https://huggingface.co/Qwen/qwen3.6-32b-q4/resolve/main/model.gguf",
                "gguf_sha256": "mno345",
                "size_mb": 18500,
                "vram_required_gb": 18.0,
                "context_length": 32768,
                "quantization": "q4",
                "specialty": "General",
                "llm_model_name": "qwen3.6-32b-instruct",
                "install_recommendation": True,
                "runtime_profiles": {"qwen": {"tps": 28}},
                "app_compatibility": ["General", "Chat", "Reasoning"],
            },
        ],
        "metadata": {
            "upstream_commit": "abc123def456",
            "upstream_date": "2026-07-25",
        },
    }


@pytest.fixture
def mock_gpu_database() -> dict[str, Any]:
    """Mock GPU hardware database for testing."""
    return {
        "known_gpus": {
            "nvidia_a100": {
                "id": "nvidia_a100",
                "specs": {
                    "label": "NVIDIA A100",
                    "memory_type": "discrete",
                    "vram_mb": 40960,
                },
                "recommended": {"tier": "4"},
            },
            "apple_m3": {
                "id": "apple_m3",
                "specs": {
                    "label": "Apple M3 Pro",
                    "memory_type": "unified",
                    "vram_mb": 0,
                },
                "recommended": {"tier": "2"},
            },
        },
        "heuristic_classes": [
            {
                "match": {"vendor": "nvidia", "memory_type": "discrete", "min_vram_mb": 40000},
                "recommended": {"tier": "4"},
            },
            {
                "match": {"vendor": "nvidia", "memory_type": "discrete", "min_vram_mb": 20000},
                "recommended": {"tier": "3"},
            },
            {
                "match": {"vendor": "apple", "memory_type": "unified", "min_vram_mb": 0},
                "recommended": {"tier": "2"},
            },
        ],
        "known_gpu_bandwidth": {
            "nvidia_a100": 1935,  # GB/s
            "apple_m3": 100,  # GB/s
        },
        "defaults": {"VRAM_FIT_TOLERANCE_GB": 0.25},
    }


@pytest.fixture
def catalog_file(mock_catalog: dict[str, Any]) -> Path:
    """Write mock catalog to a temp file and return path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(mock_catalog, f)
        temp_path = Path(f.name)
    yield temp_path
    # Cleanup
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def gpu_database_file(mock_gpu_database: dict[str, Any]) -> Path:
    """Write mock GPU database to a temp file and return path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(mock_gpu_database, f)
        temp_path = Path(f.name)
    yield temp_path
    # Cleanup
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def invalid_catalog_file() -> Path:
    """Write invalid JSON to a temp file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        f.write("{invalid json content")
        temp_path = Path(f.name)
    yield temp_path
    # Cleanup
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def empty_models_catalog_file() -> Path:
    """Catalog with empty models array."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump({"version": "1.0", "models": []}, f)
        temp_path = Path(f.name)
    yield temp_path
    # Cleanup
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def malformed_model_catalog_file() -> Path:
    """Catalog with incomplete model record."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(
            {
                "version": "1.0",
                "models": [
                    {
                        "id": "incomplete-model",
                        # Missing required fields
                    }
                ],
            },
            f,
        )
        temp_path = Path(f.name)
    yield temp_path
    # Cleanup
    temp_path.unlink(missing_ok=True)
