"""Model tiers, per-tier configuration, and the reasoning router.

Three tiers map to AitherOS's standard effort budget (see
``AitherOS/config/agent_kernel.yaml``)::

    Effort 1-2   →  ModelTier.FAST           small local model (~1-3B)
    Effort 3-6   →  ModelTier.ORCHESTRATOR   mid local model  (~7-14B) — default
    Effort 7-10  →  ModelTier.REASONING      frontier reasoning model

A user can pin a request to a specific tier at the API or CLI level
(``aither reason ...`` forces REASONING; ``aither chat ...`` uses default
routing). Operators can rebind tiers to any backend via :func:`save_config`
or the ``aither model tier`` CLI.

Configuration is stored at ``~/.aither/reasoning.json``::

    {
      "version": 1,
      "default_tier": "orchestrator",
      "tiers": {
        "fast":         {"backend": "ollama",   "model": "qwen2.5:1.5b"},
        "orchestrator": {"backend": "ollama",   "model": "qwen2.5:14b"},
        "reasoning":    {"backend": "anthropic","model": "claude-opus-4-1"}
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from adk.core.model import ModelBackend

log = logging.getLogger(__name__)

CONFIG_VERSION = 1


def _compute_default_config_path() -> Path:
    """Resolve the default reasoning-config path, honoring ``AITHER_HOME``.

    Exposed as a helper so tests can verify the env-var contract without
    reloading the module (which would create zombie ``ModelTier`` enums and
    break ``is`` comparisons elsewhere).
    """
    return Path(
        os.environ.get("AITHER_HOME", str(Path.home()))
    ).expanduser() / ".aither" / "reasoning.json"


DEFAULT_CONFIG_PATH = _compute_default_config_path()


# ---------------------------------------------------------------------------
# Tier enum + specs
# ---------------------------------------------------------------------------


class ModelTier(str, Enum):
    """Three reasoning tiers + perception, ordered cheapest → strongest.

    PERCEPTION is a modality tier for vision/multimodal requests, independent
    of effort-based routing.
    """

    FAST = "fast"
    ORCHESTRATOR = "orchestrator"
    REASONING = "reasoning"
    PERCEPTION = "perception"

    @classmethod
    def from_str(cls, value: str | "ModelTier") -> "ModelTier":
        if isinstance(value, cls):
            return value
        # Accept enum-likes from reloaded modules (different class identity
        # but same value), as well as any object exposing ``.value``.
        raw = getattr(value, "value", value)
        key = str(raw).strip().lower()
        for tier in cls:
            if tier.value == key:
                return tier
        raise ValueError(f"unknown model tier: {value!r}")

    @property
    def rank(self) -> int:
        """Ordering: FAST=0, ORCHESTRATOR=1, REASONING=2, PERCEPTION=3 (modality)."""
        return {"fast": 0, "orchestrator": 1, "reasoning": 2, "perception": 3}[self.value]


def _setup_help(provider: str) -> str:
    """Guided-onboarding error text for a missing provider key (owner directive:
    errors must guide the user step-by-step, not dead-end)."""
    try:
        from adk.llm.onboarding import missing_key_message
        return missing_key_message(provider)
    except Exception:
        return f"{provider} backend requires {provider.upper()}_API_KEY"


@dataclass(slots=True)
class TierSpec:
    """How a single tier is wired."""

    backend: str           # "ollama" | "vllm" | "openai" | "anthropic" | "deepseek" | "moonshot" | "genesis" | "auto"
    model: str             # provider model id, e.g. "qwen2.5:14b", "claude-opus-4-1"
    max_tokens: int | None = None
    temperature: float = 0.7
    base_url: str | None = None     # optional override (e.g., custom vLLM endpoint)
    notes: str = ""        # operator-facing comment

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != ""}


# Sensible defaults if no config file is present.
_DEFAULT_TIERS: dict[ModelTier, TierSpec] = {
    ModelTier.FAST: TierSpec(
        backend="auto", model="qwen2.5:1.5b",
        max_tokens=512, temperature=0.4,
        notes="Snap responses, classifications, lightweight tool calls.",
    ),
    ModelTier.ORCHESTRATOR: TierSpec(
        backend="auto", model="qwen2.5:14b",
        max_tokens=4096, temperature=0.7,
        notes="Default workhorse. Most agent turns route here.",
    ),
    ModelTier.REASONING: TierSpec(
        backend="auto", model="deepseek-r1:70b",
        max_tokens=8192, temperature=0.4,
        notes="Long-horizon reasoning, MCTS evaluation, hard analysis.",
    ),
    ModelTier.PERCEPTION: TierSpec(
        backend="auto", model="qwen-vl:7b",
        max_tokens=2048, temperature=0.7,
        notes="Vision/multimodal requests. Image analysis, OCR, visual reasoning.",
    ),
}


# ---------------------------------------------------------------------------
# Persisted config
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReasoningConfig:
    """File-backed reasoning configuration."""

    default_tier: ModelTier = ModelTier.ORCHESTRATOR
    tiers: dict[ModelTier, TierSpec] = field(
        default_factory=lambda: {t: TierSpec(**asdict(s)) for t, s in _DEFAULT_TIERS.items()}
    )
    version: int = CONFIG_VERSION

    def get(self, tier: ModelTier | str) -> TierSpec:
        return self.tiers[ModelTier.from_str(tier)]

    def set(self, tier: ModelTier | str, spec: TierSpec) -> None:
        self.tiers[ModelTier.from_str(tier)] = spec

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "default_tier": self.default_tier.value,
            "tiers": {t.value: s.to_json() for t, s in self.tiers.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ReasoningConfig":
        version = int(data.get("version", CONFIG_VERSION))
        if version > CONFIG_VERSION:
            log.warning(
                "reasoning.json version=%s is newer than supported (%s)",
                version, CONFIG_VERSION,
            )
        default_tier = ModelTier.from_str(data.get("default_tier", "orchestrator"))
        tiers: dict[ModelTier, TierSpec] = {}
        for key, spec_data in (data.get("tiers") or {}).items():
            try:
                tier = ModelTier.from_str(key)
            except ValueError:
                log.warning("ignoring unknown tier %r in reasoning.json", key)
                continue
            tiers[tier] = TierSpec(
                backend=str(spec_data.get("backend", "auto")),
                model=str(spec_data.get("model", "")),
                max_tokens=spec_data.get("max_tokens"),
                temperature=float(spec_data.get("temperature", 0.7)),
                base_url=spec_data.get("base_url"),
                notes=str(spec_data.get("notes", "")),
            )
        # Fill in any tier that wasn't present in the file.
        for tier, default in _DEFAULT_TIERS.items():
            tiers.setdefault(tier, TierSpec(**asdict(default)))
        return cls(default_tier=default_tier, tiers=tiers, version=CONFIG_VERSION)


def load_config(path: Path | None = None) -> ReasoningConfig:
    """Load the reasoning config, falling back to defaults if absent."""
    p = path or DEFAULT_CONFIG_PATH
    if not p.is_file():
        return ReasoningConfig()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s (%s) — using defaults", p, exc)
        return ReasoningConfig()
    return ReasoningConfig.from_json(data)


def save_config(config: ReasoningConfig, path: Path | None = None) -> Path:
    """Persist the reasoning config, creating parent dirs as needed."""
    p = path or DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config.to_json(), indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


# ---------------------------------------------------------------------------
# Effort → tier classification
# ---------------------------------------------------------------------------


def classify_effort(effort: int | float) -> ModelTier:
    """Map an effort score (1-10) to the right tier.

    Mirrors ``AitherOS/lib/core/EffortScaler`` so an agent dispatched at
    effort 8 ends up at the same tier whether it ran inside Genesis or
    inside an ``aither_adk`` binary on a laptop.
    """
    try:
        e = int(effort)
    except (TypeError, ValueError):
        e = 4
    if e <= 2:
        return ModelTier.FAST
    if e <= 6:
        return ModelTier.ORCHESTRATOR
    return ModelTier.REASONING


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TierAssignment:
    """Resolved tier for a single request: which tier and which backend."""

    tier: ModelTier
    spec: TierSpec
    backend: ModelBackend


class ReasoningRouter:
    """Picks the right tier + backend per request.

    The router is intentionally tiny so it stays cheap to construct per turn
    while caching backends so we don't recreate HTTP clients.
    """

    def __init__(self, config: ReasoningConfig | None = None):
        self.config = config or load_config()
        self._backend_cache: dict[tuple[str, str, str | None], ModelBackend] = {}

    # -- core resolution ------------------------------------------------------

    def resolve(
        self,
        *,
        tier: ModelTier | str | None = None,
        effort: int | float | None = None,
    ) -> TierAssignment:
        """Resolve the (tier, spec, backend) for this call.

        Precedence: explicit ``tier`` arg → ``effort`` classification →
        configured default tier.
        """
        if tier is not None:
            chosen = ModelTier.from_str(tier)
        elif effort is not None:
            chosen = classify_effort(effort)
        else:
            chosen = self.config.default_tier

        spec = self.config.get(chosen)
        backend = self._materialize(spec)
        return TierAssignment(tier=chosen, spec=spec, backend=backend)

    # -- backend instantiation -----------------------------------------------

    def _materialize(self, spec: TierSpec) -> ModelBackend:
        key = (spec.backend, spec.model, spec.base_url)
        cached = self._backend_cache.get(key)
        if cached is not None:
            return cached

        backend = self._build_backend(spec)
        self._backend_cache[key] = backend
        return backend

    def _build_backend(self, spec: TierSpec) -> ModelBackend:
        """Construct a backend for a tier spec.

        Honors ``spec.backend == "auto"`` by delegating to
        :func:`adk.core.model.auto_backend`. Otherwise builds the
        requested kind directly from the same factory pool.
        """
        # Local import to keep ``adk.reasoning`` import-cheap.
        from adk.core.model import auto_backend
        from adk.core.backends.anthropic import AnthropicBackend
        from adk.core.backends.ollama import OllamaBackend
        from adk.core.backends.openai_compat import (
            DeepSeekBackend,
            OpenAIBackend,
            VLLMBackend,
        )

        kind = (spec.backend or "auto").lower().strip()
        model = spec.model or "auto"

        if kind == "auto":
            return auto_backend(model=model or None)
        if kind == "vllm":
            base_url = spec.base_url or os.environ.get("AITHER_VLLM_URL") or ""
            if not base_url:
                raise RuntimeError("vllm backend requires base_url or AITHER_VLLM_URL")
            return VLLMBackend(base_url=base_url, model=model)
        if kind == "openai":
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("openai backend requires OPENAI_API_KEY")
            return OpenAIBackend(api_key=key, model=model, base_url=spec.base_url or None)
        if kind == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError(_setup_help("anthropic"))
            return AnthropicBackend(api_key=key, model=model)
        if kind == "deepseek":
            key = os.environ.get("DEEPSEEK_API_KEY")
            if not key:
                raise RuntimeError(_setup_help("deepseek"))
            return DeepSeekBackend(api_key=key, model=model)
        if kind in ("moonshot", "kimi"):
            key = os.environ.get("MOONSHOT_API_KEY")
            if not key:
                raise RuntimeError(_setup_help("moonshot"))
            from adk.core.backends.openai_compat import MoonshotBackend
            return MoonshotBackend(api_key=key, model=model)
        if kind == "ollama":
            host = spec.base_url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
            return OllamaBackend(base_url=host, model=model)
        if kind == "genesis":
            # When AitherOS Genesis is reachable, route through it for VRAM
            # coordination via MicroScheduler. We model it as an OpenAI-compat
            # endpoint hosted at AITHER_URL.
            base_url = spec.base_url or os.environ.get("AITHER_URL") or ""
            if not base_url:
                raise RuntimeError("genesis backend requires base_url or AITHER_URL")
            return VLLMBackend(base_url=base_url.rstrip("/") + "/v1", model=model)

        raise RuntimeError(f"unknown backend kind: {kind!r}")

    # -- introspection --------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Human-friendly summary for ``aither model show``."""
        return {
            "default_tier": self.config.default_tier.value,
            "tiers": {
                t.value: {
                    "backend": s.backend,
                    "model": s.model,
                    "temperature": s.temperature,
                    "max_tokens": s.max_tokens,
                    "base_url": s.base_url,
                    "notes": s.notes,
                }
                for t, s in self.config.tiers.items()
            },
        }
