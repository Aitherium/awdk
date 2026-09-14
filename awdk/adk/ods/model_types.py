"""Type definitions for ODS model catalog and recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class OdsError(Exception):
    """Fail-closed error for ODS operations.

    Raised when:
    - Catalog file is missing or corrupt
    - Catalog schema is invalid
    - Resolver logic fails (e.g., no candidates found)

    Never returns silent/empty; always raises to fail-closed.
    """

    pass


@dataclass(frozen=True)
class ModelRecord:
    """A single model from the ODS catalog (immutable)."""

    id: str
    """Unique model identifier (e.g., 'qwen3.5-2b-q4')."""

    name: str
    """Human-readable model name (e.g., 'Qwen 3.5 2B Q4')."""

    family: str
    """Model family: qwen, gemma4, phi, llama, hermes, etc."""

    gguf_file: str
    """GGUF filename (e.g., 'qwen3.5-2b-q4.gguf')."""

    gguf_url: str
    """Download URL for the GGUF file (e.g., HuggingFace link)."""

    gguf_sha256: str
    """SHA256 hash of the GGUF file for integrity check."""

    size_mb: float
    """Total model size in MB (on disk after download)."""

    vram_required_gb: float
    """VRAM required to load this model (in GB)."""

    context_length: int
    """Maximum context window length (tokens)."""

    quantization: str
    """Quantization format: q4, q5, q6, q8, fp8, etc."""

    specialty: str
    """Model specialty as it appears in the vendored catalog: Fast, Chat, Quality,
    Reasoning, General, Balanced, Long Context, Tool Use, Code, Enterprise.
    (Upstream's score table also weights a 'Bootstrap' specialty, but no record
    in the vendored library uses it.)"""

    llm_model_name: str
    """LLM model identifier (e.g., 'qwen3.5-2b-instruct')."""

    install_recommendation: bool
    """Whether this model is recommended for installation."""

    runtime_profiles: dict[str, Any]
    """Runtime performance profiles per use-case (e.g., qwen, gemma4, default)."""

    app_compatibility: list[str]
    """Application compatibility: General, Code, Chat, Reasoning, Embedding, etc."""

    tokens_per_sec_estimate: float = 0.0
    """Catalog's estimated decode throughput. 0.0 when unknown — upstream's
    normalize_model() drops this field, so it is only populated on records that
    came through OdsResolver (which re-joins it from the raw catalog)."""


@dataclass(frozen=True)
class OdsRecommendation:
    """Result of resolver.resolve() — never None; always OdsRecommendation or OdsError."""

    policy: str
    """Policy name (e.g., 'unified-memory-coder-next-a3b-v1', 'default-tier3-qwen')."""

    source: str
    """Source: always 'ods' (for backward compat with llmfit)."""

    confidence: float
    """Confidence 0.0–1.0 (0.95+ high, 0.80–0.95 good, <0.80 fallback)."""

    profile: str
    """Resolved profile: qwen or gemma4 (auto resolved to one of these)."""

    host_arch: str
    """Host architecture: x86_64, arm64, unknown (echoed from input)."""

    memory_capacity_gb: float
    """Usable memory capacity (GB), exactly as upstream computes it in
    `usable_memory_gb()`: 55% of RAM on unified/Apple, min(max(RAM*0.35, 3), 8)
    on CPU or when VRAM is unknown, and the FULL VRAM (100%, not 95%) on a
    discrete GPU. Do not 'correct' the discrete case to a derate — a prior
    review asserted 95% and applying it introduced a real divergence from
    upstream; the fit tolerance lives in `fits()`, not here."""

    memory_label: str
    """Human-readable memory label (e.g., 'NVIDIA A100 discrete (24GB)')."""

    selected: ModelRecord
    """The recommended model (primary pick)."""

    reason: str
    """Human-readable reason for selection."""

    alternatives: list[ModelRecord]
    """Top 3 alternative models (same profile+family constraints)."""

    def as_legacy_dict(self) -> dict[str, Any]:
        """Transform to legacy LLMFitClient.recommend_config() return shape.

        Returns dict with keys: hardware, fast, balanced, reasoning, coding, embedding.
        Used internally for backward compatibility with llmfit integration.
        """
        return {
            "selected_policy": self.policy,
            "selected_model": self.selected.id,
            "selected_name": self.selected.name,
            "confidence": self.confidence,
            "reason": self.reason,
            "memory_label": self.memory_label,
        }
