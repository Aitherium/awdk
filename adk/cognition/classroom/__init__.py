"""Classroom games for the cognition slice: the vendored h25 generator (push family
first) and an in-process environment that renders a level as a colour grid.

Provenance: :mod:`adk.cognition.classroom._provenance`.
"""

from __future__ import annotations

from .env import PushEnv, level_frame

__all__ = ["PushEnv", "level_frame"]
