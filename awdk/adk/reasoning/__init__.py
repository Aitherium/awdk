"""Tiered reasoning for AitherOS — the enterprise differentiator.

OpenClaw and Hermes are great personal agents. AitherOS goes further:

* **Local orchestrator by default** — cheap, fast, no token cost. Most turns
  go to a mid-sized local model (Qwen, Mistral, DeepSeek-R1:8b, etc.) running
  through Genesis / vLLM / Ollama.
* **Frontier reasoning on demand** — when a task is hard or the user asks for
  it, escalate to the strongest model the operator has configured (GPT-5,
  Claude Opus, DeepSeek-R1:70b, o3, NVIDIA Nemotron Ultra, etc.).
* **Long-horizon planning via MCTS** — Monte Carlo Tree Search expands a
  plan space using the orchestrator for rollouts and the reasoning model for
  value estimation. Real planning, not just chain-of-thought.

The router and planner here are **transport-agnostic**: they speak to
:class:`adk.core.model.ModelBackend` instances, so the same code paths
work for Ollama on a laptop and a managed cluster behind ``MicroScheduler``.

Public API::

    from adk.reasoning import (
        ModelTier, TierAssignment, ReasoningConfig, ReasoningRouter,
        MCTSPlanner, PlanNode, PlanResult,
    )
"""

from __future__ import annotations

from .tiers import (
    DEFAULT_CONFIG_PATH,
    ModelTier,
    ReasoningConfig,
    ReasoningRouter,
    TierAssignment,
    TierSpec,
    classify_effort,
    load_config,
    save_config,
)
from .planner import MCTSPlanner, PlanNode, PlanResult
from .cem import CEMPlanner, CEMResult, CEMRound, DEFAULT_HINTS
from .speculative import SpeculativeBackend, SpeculativeStats

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ModelTier",
    "TierSpec",
    "TierAssignment",
    "ReasoningConfig",
    "ReasoningRouter",
    "classify_effort",
    "load_config",
    "save_config",
    "MCTSPlanner",
    "PlanNode",
    "PlanResult",
    "CEMPlanner",
    "CEMResult",
    "CEMRound",
    "DEFAULT_HINTS",
    "SpeculativeBackend",
    "SpeculativeStats",
    "UnifiedMCTS",
    "MCTSConfig",
    "MCTSResult",
    "MCTSEnvironment",
]

from .mcts import UnifiedMCTS, MCTSConfig, MCTSResult, MCTSEnvironment  # noqa: E402,F401
