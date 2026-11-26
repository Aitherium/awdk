"""Thin compatibility shim over :mod:`adk.typed_memory` for ``adk.core``.

All authority/decay/ranking/constraint logic lives in :mod:`adk.typed_memory`.
This module re-exports that surface and preserves the ``adk.core`` default of
backing :class:`TypedMemory` with :class:`~adk.core.memory.InMemoryStore` when
no ``backing`` store is passed.
"""

from __future__ import annotations

from adk.core.memory import InMemoryStore, Memory
from adk.typed_memory import (
    ACTIVATION_BONUS_CAP,
    RANK_DECAY,
    REINFORCE_BONUS_CAP,
    ROLE_AUTHORITY,
    STALE_FACTOR,
    STALE_FRESHNESS_THRESHOLD,
    SUPERSEDED_FACTOR,
    TIER_HALF_LIFE,
    TIER_TTL,
    RecalledItem,
    Role,
    ScoreBreakdown,
    Tier,
    created_at_of,
    default_tier_for,
    freshness,
    infer_role,
    is_expired,
    label_content,
    labels,
    make_metadata,
    parse_constraint,
    role_of,
    score,
    tier_of,
)
from adk.typed_memory import TypedMemory as _BaseTypedMemory


class TypedMemory(_BaseTypedMemory):
    """Authority/activation wrapper defaulting to :class:`InMemoryStore`."""

    def __init__(self, backing: Memory | None = None) -> None:
        super().__init__(backing if backing is not None else InMemoryStore())


def as_typed(memory: Memory | None) -> TypedMemory:
    """Wrap ``memory`` in a :class:`TypedMemory` if it isn't already one."""
    if isinstance(memory, TypedMemory):
        return memory
    return TypedMemory(memory)


__all__ = [
    "Role",
    "Tier",
    "ROLE_AUTHORITY",
    "TIER_HALF_LIFE",
    "RecalledItem",
    "ScoreBreakdown",
    "TypedMemory",
    "as_typed",
    "default_tier_for",
    "freshness",
    "infer_role",
    "is_expired",
    "label_content",
    "labels",
    "parse_constraint",
    "role_of",
    "score",
    "tier_of",
]
