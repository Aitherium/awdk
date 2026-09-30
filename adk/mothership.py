"""Compatibility name for :mod:`adk.lookout`.

awdk 3.8.31 shipped this module as ``adk.mothership`` with the class
``Mothership``. It was renamed to ``adk.lookout`` / ``Lookout``; this module
keeps imports written against 3.8.31 working. New code imports ``adk.lookout``.
"""

from __future__ import annotations

from adk.lookout import (
    Event,
    JsonlSource,
    LinearSource,
    Lookout,
    SlackSource,
    Verdict,
    awrun_dispatch,
    build_task,
    judge,
    main,
)

Mothership = Lookout

__all__ = [
    "Event",
    "Verdict",
    "judge",
    "SlackSource",
    "LinearSource",
    "JsonlSource",
    "Lookout",
    "Mothership",
    "awrun_dispatch",
    "build_task",
    "main",
]
