"""ArcGatewayAdapter — an EnvironmentAdapter over the PUBLIC ARC-AGI-3 game API
that contributes every transition it produces to the AitherWorldModel
Contribution Gateway.

This is B1 of the ARC BYO-cognition program: anyone can now enroll a REAL ARC
game with ``env_enroll``. The adapter satisfies the EnvironmentAdapter contract
(observe / actions / step / domain), the learn-safely explore loop drives it
against the caller's own policy (or the built-in curiosity loop), and every
(grid, action, next_grid) transition is submitted to the public gateway's
/v1/observe — quarantined, trust-scored, shown on the leaderboard. The same
loop also records the transitions into the LOCAL world model (in-process when
AITHER_OFFLINE=1, else :8197 domain=arc), so a BYO agent learns the game it is
playing while teaching the shared model.

Design notes:
  * Conforms to world_model.contracts.EnvironmentAdapter: ``observe()`` returns a
    hashable grid string, ``actions()`` the legal non-RESET vocabulary,
    ``step()`` -> (next_obs, reward, done, info), and ``.domain`` is a str
    ("arc:<game_id>"). env_enroll's contract check passes on the probe instance.
  * The heavy client (gateway register/observe, the vendored ARC player) lives in
    adk/toolpacks/arc-brainpack/tools.py. That pack directory is hyphenated and
    therefore NOT importable as a dotted module, so this module file-loads it the
    same way the ADK tool-pack loader does (importlib spec_from_file_location) —
    no code duplication, and no host-repo dotted import.
  * Guarded: a missing ARC_API_KEY raises a clear error at construction (a real
    game cannot be reached without one); a missing contributor token or a dead
    gateway degrades the CONTRIBUTION half only — the local world model still
    learns, so enrollment works with zero setup and contribution kicks in the
    moment a token exists (arc_register).
  * No arcengine dependency: ARC state names are plain strings ("WIN"/"GAME_OVER")
    and ACTION6 (the click action) is the only one needing coordinates — both
    facts come straight off the ARC API's frame, not an enum import.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("arc_world_pack")

# Import smart policy for coordinate selection
try:
    from . import smart_policy
except ImportError:
    smart_policy = None  # type: ignore

_TERMINAL_STATES = ("WIN", "GAME_OVER")
_VOCAB_FALLBACK = [1, 2, 3, 4, 5, 6, 7]  # legal non-RESET actions (0 = RESET)


# --------------------------------------------------------------------------- #
# load the vendored arc-brainpack client (hyphenated dir -> not importable by name)
# --------------------------------------------------------------------------- #
def _load_brainpack() -> Any:
    """File-load adk/toolpacks/arc-brainpack/tools.py and return its module.

    Mirrors the ADK tool-pack loader's file-load path (spec_from_file_location
    over the pack's module) so the adapter reuses the exact gateway client the
    arc_* tools use — a single source of truth for register/observe semantics.
    """
    p = Path(__file__).resolve().parents[2] / "toolpacks" / "arc-brainpack" / "tools.py"
    if not p.is_file():
        raise RuntimeError(f"arc-brainpack client not found at {p}")
    mod_name = "_arc_brainpack_tools"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_BP = _load_brainpack()


class ArcGatewayAdapter:
    """Play one real ARC-AGI-3 game and contribute every transition.

    Each instance = one game session (RESET on construction). env_enroll builds a
    FRESH instance per episode, so episodes restart the game — exactly the
    discontinuity the transition tap must never cross, which is why RESET is never
    submitted as a transition (mirrors the arc-brainpack player).

    Args:
        game_id: ARC game id (e.g. "ls20", "vc33"), or a prefix.
        api_key: ARC-AGI-3 API key. Defaults to $ARC_API_KEY.
        gateway_url: Contribution Gateway base. Defaults to $WM_GATEWAY_URL
            (the public https://arc.aitherium.com/contribute).
        token: Contributor Bearer token. Defaults to $WM_CONTRIB_TOKEN / the
            persisted arc_register token. Empty -> play + learn locally only.
        submit: When True (default) submit every transition to the gateway.
        _http: For tests — inject a stub ARC-API session; construction skips the
            API-key check and the real RESET.
    """

    def __init__(
        self,
        game_id: str,
        api_key: Optional[str] = None,
        gateway_url: Optional[str] = None,
        token: Optional[str] = None,
        submit: bool = True,
        _http: Optional[Any] = None,
    ) -> None:
        self._game = str(game_id).strip()
        if not self._game:
            raise ValueError("game_id is required (e.g. 'ls20')")
        self.domain = f"arc:{self._game}"

        self._http = _http
        self._root = _BP._arc_base()  # no network — just the env-resolved base
        if self._http is None:
            key = (api_key or os.environ.get("ARC_API_KEY") or "").strip()
            if not key:
                raise ValueError(
                    "ARC_API_KEY is unset — a real ARC-AGI-3 game cannot be reached "
                    "without it. Set ARC_API_KEY (or pass api_key=) and retry."
                )
            self._http = _BP._ArcHttp(key)

        self._base = (gateway_url or _BP._gateway()).rstrip("/")
        self._tok = (token or _BP._token()).strip()
        self._submit = bool(submit)

        # Reset the game to a fresh episode and capture its opening state.
        self._frame = self._http.post(
            f"{self._root}/api/cmd/RESET", {"game_id": self._game}
        )
        self._state = str((self._frame or {}).get("state") or "NOT_PLAYED")
        self._guid = (self._frame or {}).get("guid")
        self._pre_raw = self._current_grid_raw()
        self._submitted = 0
        self._accepted = 0
        # Initialize smart policy tracker if available
        self._policy_tracker = (
            smart_policy.SmartPolicyTracker() if smart_policy else None
        )
        logger.info("arc adapter: enrolled game '%s' (state=%s, transitions "
                    "submit=%s)", self._game, self._state, self._submit and bool(self._tok))

    # -- EnvironmentAdapter contract ------------------------------------------ #
    def observe(self, env_state: Any = None) -> str:
        """Return a hashable string of the current (or given) grid."""
        if env_state is not None:
            return self._grid_str(env_state)
        return self._grid_str(self._current_grid_raw())

    def actions(self) -> Sequence[int]:
        """Legal non-RESET actions for the current frame (or the full vocabulary)."""
        if self._frame:
            avail = self._frame.get("available_actions") or []
            try:
                a = [int(x) for x in avail if int(x) != 0]
            except (TypeError, ValueError):
                a = []
            if a:
                return a
        return list(_VOCAB_FALLBACK)

    def step(self, action: int) -> Tuple[str, float, bool, Dict[str, Any]]:
        """Execute one ARC action, submit the (pre, action, post) transition to the
        gateway, and return (next_obs, reward, done, info)."""
        if self._state in _TERMINAL_STATES:
            return self.observe(), 0.0, True, {"state": self._state,
                                               "note": "episode already over"}

        aid = int(action)
        x = y = None
        if aid == 6:  # ACTION6 = click at (x, y) — the only complex action
            # Use smart policy for coordinate selection if available
            if self._policy_tracker and smart_policy:
                grid = self._current_grid_raw()
                x, y = smart_policy.select_smart_coordinates(
                    grid,
                    width=64,
                    height=64,
                    recent_actions=self._policy_tracker.recent_actions,
                )
            else:
                x, y = random.randint(0, 63), random.randint(0, 63)
        # Coords ride in the BODY, never the path: the ARC API is /api/cmd/ACTION6
        # with x/y as body keys (the vendored player does exactly this). The
        # coords-annotated string is for the GATEWAY submission only.
        cmd_path = f"ACTION{aid}"
        action_str = f"ACTION{aid}({x},{y})" if x is not None else f"ACTION{aid}"

        step_body: dict = {"game_id": self._game}
        if self._guid:
            step_body["guid"] = self._guid
        if x is not None:
            step_body["x"] = x
        if y is not None:
            step_body["y"] = y

        pre_raw = self._pre_raw
        try:
            nxt = self._http.post(f"{self._root}/api/cmd/{cmd_path}", step_body)
        except Exception as exc:  # noqa: BLE001 — a dead arc API must not crash the loop
            logger.warning("arc adapter: step %s failed: %s", action_str, exc)
            return self.observe(), 0.0, False, {"error": str(exc),
                                                "action": action_str}
        if not isinstance(nxt, dict):
            nxt = {}

        self._frame = nxt
        self._guid = nxt.get("guid") or self._guid
        self._state = str(nxt.get("state") or self._state)
        post_raw = self._current_grid_raw()

        # Submit a real transition only — never across a RESET / env discontinuity.
        submitted = accepted = False
        if (aid != 0 and pre_raw is not None and post_raw is not None
                and not nxt.get("full_reset")
                and self._submit and self._tok):
            try:
                _, accepted = _BP._submit_observe(self._base, self._tok, pre_raw,
                                                  action_str, post_raw, self._game)
                submitted = True
                self._submitted += 1
                self._accepted += 1 if accepted else 0
            except Exception as exc:  # noqa: BLE001 — contribution must never break play
                logger.debug("arc adapter: submit failed (%s) — continuing local", exc)

        # Track action results with policy tracker
        if self._policy_tracker and x is not None and y is not None:
            grid_after = self._grid_str(post_raw)
            grid_before = self._grid_str(pre_raw)
            self._policy_tracker.add_action(x, y, grid_before, grid_after)

        self._pre_raw = post_raw
        reward = 1.0 if self._state == "WIN" else 0.0
        done = self._state in _TERMINAL_STATES
        return (self.observe(), reward, done, {
            "action": action_str,
            "state": self._state,
            "submitted": submitted,
            "accepted": accepted,
        })

    # -- internals ------------------------------------------------------------ #
    def _current_grid_raw(self) -> Optional[list]:
        """The current frame's last grid (list-of-lists) or None."""
        grids = (self._frame or {}).get("frame") or []
        return grids[-1] if grids else None

    @staticmethod
    def _grid_str(grid: Any) -> str:
        if grid is None:
            return "<none>"
        try:
            return "|".join("".join(str(c) for c in row) for row in grid)
        except Exception:  # noqa: BLE001 — any malformed grid degrades to a token
            return "<grid>"

    def close(self) -> None:
        try:
            if self._http is not None:
                self._http.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("arc adapter: close failed: %s", exc)
