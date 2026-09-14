"""Generic Monte Carlo Tree Search library.

A domain-agnostic port of AitherOS' ``UnifiedMCTS`` with all
AitherOS-specific couplings replaced by optional, default-off seams on
:class:`MCTSConfig` (``transition_model`` / ``policy_model`` / ``value_model``
/ ``observer`` / ``artifact_dir`` / ``dedup_embedder``). With every seam left
``None`` the engine is behaviour-identical to the base algorithm.

Public API::

    from adk.reasoning.mcts import (
        UnifiedMCTS, MCTSConfig, MCTSResult, MCTSTrace, MCTSNode, search,
        MCTSEnvironment, TransitionModel, PolicyModel, ValueModel,
        ObservedTransitionModel,
    )
"""

from __future__ import annotations

from .core import (
    MCTSConfig,
    MCTSNode,
    MCTSReroot,
    MCTSResult,
    MCTSTrace,
    UnifiedMCTS,
    search,
)
from .env import MCTSEnvironment
from .models import PolicyModel, TransitionModel, ValueModel
from .adapters import ObservedTransitionModel

__all__ = [
    "MCTSConfig",
    "MCTSNode",
    "MCTSReroot",
    "MCTSResult",
    "MCTSTrace",
    "UnifiedMCTS",
    "search",
    "MCTSEnvironment",
    "TransitionModel",
    "PolicyModel",
    "ValueModel",
    "ObservedTransitionModel",
]
