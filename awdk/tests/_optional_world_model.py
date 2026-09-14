"""Shared guard for tests that need the IN-PROCESS world-model learner.

`packages/world-model` is a monorepo sibling that sync-adk.yml deliberately
EXCLUDES from the public payload (`--exclude='packages'` — that tree is reserved
for licence-gated products). So in a public install `_get_offline_engine()`
returns None by design and every test that drives the in-process learner fails.

That matters because the release gate ("Test the public payload before releasing
it") installs the payload into a clean environment and runs this suite
there — reproducing the public install on purpose. Eight tests across
test_world_model_pack / test_env_enroll / test_arc_world_pack failed that way and
blocked EVERY adk-v* release from 2026-07-30 onward, while passing locally where
the monorepo sibling resolves. They are not defects: the dependency is genuinely
absent, and the pack documents the remote service as the public path
(`adk/packs/world_model/tools.py`: "The package is optional; if missing, callers
can still use the remote service").

So they skip — the same treatment sync-adk.yml already documents for payload
tests that "locate optional monorepo siblings by relative path and skip at module
level when absent".

Module-level `skipif` (evaluated at decoration) on purpose: a `pytest.skip()`
inside a test body fires AFTER partial execution and reports a real failure as a
skip, which this project bans.
"""

from __future__ import annotations

import pytest


def world_model_engine_available() -> bool:
    """True when the in-process MLPWorldModel can actually be constructed.

    Asks the pack itself rather than probing for a path, so this tracks the real
    resolution order (installed package, cwd, then monorepo root) instead of
    duplicating it.
    """
    try:
        from adk.packs.world_model.tools import _get_offline_engine
    except Exception:  # noqa: BLE001 — pack absent entirely => not available
        return False
    try:
        return _get_offline_engine() is not None
    except Exception:  # noqa: BLE001 — any import/ctor failure => not available
        return False


_AVAILABLE = world_model_engine_available()

requires_world_model_engine = pytest.mark.skipif(
    not _AVAILABLE,
    reason=(
        "in-process world-model engine unavailable — packages/world-model is a "
        "monorepo sibling excluded from the public payload; the remote service is "
        "the public path"
    ),
)
