"""ARC-AGI-3 as the first eval of the general reasoning loop.

* :mod:`.env_arc` -- ``ArcAgi3Environment``, an ``adk.reasoning.solve.Environment``
  over the offline ``arc_agi`` engine (optional dependency, imported lazily)
* :mod:`.rhae`    -- the official RHAE scorer and the competition RESET accounting
* :mod:`.suite`   -- ``run_suite(policy, games, seeds=..., cap_actions=...)``

ARC lives here and never in the reasoning core. Importing this package does not
import ``arc_agi``, ``arcengine`` or numpy.
"""

from __future__ import annotations

from .env_arc import HELDOUT_SEEDS, ArcAgi3Environment, ArcUnavailableError, check_seed
from .rhae import ActionLedger, game_rhae, level_score
from .suite import EpisodeContext, SuiteResult, random_policy, run_suite, step_policy

__all__ = [
    "HELDOUT_SEEDS",
    "ActionLedger",
    "ArcAgi3Environment",
    "ArcUnavailableError",
    "EpisodeContext",
    "SuiteResult",
    "check_seed",
    "game_rhae",
    "level_score",
    "random_policy",
    "run_suite",
    "step_policy",
]
