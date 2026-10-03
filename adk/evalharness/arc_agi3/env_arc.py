"""ARC-AGI-3 games as an :class:`adk.reasoning.solve.Environment`.

``ArcAgi3Environment(game_id, seed, env_dir=...)`` plays one game on the OFFLINE
``arc_agi.Arcade`` (no network, no API key) and charges actions exactly as the
competition scorecard does, through :class:`.rhae.ActionLedger`:

* actions 1..7 cost 1; the opening RESET is free; every later RESET costs 1;
* a RESET at 0 actions on the current level does NOT step the engine (offline it
  would restart the whole game at level 1; the competition API returns the last
  frame unchanged) but is still charged;
* ``auto_reset_on_death`` (default on): an action that ends in GAME_OVER is
  followed by a RESET, charged +1, and ``Obs.died`` is set. The returned
  observation shows the death frame; the next ``observe()`` shows the restart.

``arc_agi`` / ``arcengine`` are OPTIONAL dependencies. They are imported only when
an environment is built; when absent :class:`ArcUnavailableError` says what to install.

``state`` is the last layer of the engine frame, a 64x64 numpy int array.

The optional reasoning-loop hooks (``adk.reasoning.solve._types.HOOK_ARGS``) are
implemented: ``primer``, ``render``, ``describe``, ``tools``, ``candidates``,
``needs_xy``, ``hud``, ``significant_change`` and ``state_key``. ``state_key`` masks
the cells a learned :class:`.perception.HudMask` has identified as a step counter,
budget bar or timer, so a HUD tick is not a new state.

The games directory is never guessed: pass ``env_dir=`` or set ``ADK_ARC_ENV_DIR``.
"""

from __future__ import annotations

import logging
import os
import threading
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adk.reasoning.solve._types import Action, Obs

from .rhae import (
    COUNTED_ACTION_IDS,
    RESET_ACTION_ID,
    ActionLedger,
    load_baselines,
    short_id,
)

#: Names the games directory (the public games' ``environment_files`` folder) when
#: ``env_dir=`` is not passed. There is no default location.
ENV_DIR_VAR = "ADK_ARC_ENV_DIR"

#: Seeds reserved as the held-out band. No eval in this package may play them.
HELDOUT_SEEDS = range(10, 70)

CLICK_ACTION_ID = 6
INSTALL_HINT = "pip install 'awdk[arc]'  (arc-agi==0.9.9, arcengine==0.9.3)"


class ArcUnavailableError(RuntimeError):
    """The ARC engine or its games cannot be used here (missing dep or games dir)."""


def check_seed(seed: int) -> int:
    """Return ``seed`` or raise ``ValueError`` if it is in the held-out band."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an int, got %r" % (seed,))
    if seed in HELDOUT_SEEDS:
        raise ValueError(
            "seed %d is in the reserved held-out band %d-%d; use 0-9 or >= %d"
            % (seed, HELDOUT_SEEDS.start, HELDOUT_SEEDS.stop - 1, HELDOUT_SEEDS.stop)
        )
    return seed


def resolve_env_dir(env_dir: Optional[str | os.PathLike[str]] = None) -> Path:
    """``env_dir`` > ``$ADK_ARC_ENV_DIR``; with neither set, :class:`ArcUnavailableError`."""
    chosen = env_dir or os.environ.get(ENV_DIR_VAR, "").strip()
    if not chosen:
        raise ArcUnavailableError(
            "no ARC-AGI-3 games directory given: pass env_dir= (--env-dir) or set %s to "
            "the folder holding <game>/<version>/metadata.json" % ENV_DIR_VAR
        )
    return Path(chosen)


def require_arc() -> Tuple[Any, Any]:
    """Import ``(arc_agi, arcengine)`` or raise :class:`ArcUnavailableError`."""
    try:
        import arc_agi  # type: ignore
        import arcengine  # type: ignore
    except ImportError as exc:
        raise ArcUnavailableError(
            "ARC-AGI-3 engine not installed (%s); install it with: %s" % (exc, INSTALL_HINT)
        ) from exc
    return arc_agi, arcengine


def _quiet_arc_loggers() -> None:
    if os.environ.get("ADK_ARC_VERBOSE"):
        return
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("arc"):
            logging.getLogger(name).setLevel(logging.WARNING)


_ARCADES: Dict[str, Any] = {}
_ARCADE_LOCK = threading.Lock()


def get_arcade(env_dir: Optional[str | os.PathLike[str]] = None) -> Any:
    """One offline Arcade per games directory per process."""
    d = resolve_env_dir(env_dir)
    if not d.is_dir():
        raise ArcUnavailableError(
            "ARC games directory %s does not exist; pass env_dir= or set %s" % (d, ENV_DIR_VAR)
        )
    key = str(d.resolve())
    with _ARCADE_LOCK:
        arcade = _ARCADES.get(key)
        if arcade is None:
            arc_agi, _ = require_arc()
            os.environ.setdefault("OPERATION_MODE", "offline")
            _quiet_arc_loggers()
            arcade = arc_agi.Arcade(
                operation_mode=arc_agi.OperationMode.OFFLINE, environments_dir=key
            )
            _quiet_arc_loggers()
            lg = getattr(arcade, "logger", None)
            if lg is not None and not os.environ.get("ADK_ARC_VERBOSE"):
                lg.setLevel(logging.WARNING)
            _ARCADES[key] = arcade
    return arcade


def list_games(env_dir: Optional[str | os.PathLike[str]] = None) -> List[str]:
    """Short ids of every game with a baseline under the games directory."""
    return sorted(load_baselines(resolve_env_dir(env_dir)))


def _last_layer(frame: Any) -> Any:
    if frame is None or len(frame) == 0:
        return None
    return frame[-1].copy() if hasattr(frame[-1], "copy") else frame[-1]


def _as_array(x: Any) -> Any:
    import numpy as np

    return np.asarray(x)


def _before_after(t: Any) -> Tuple[Any, Any]:
    if hasattr(t, "before") and hasattr(t, "after"):
        return t.before, t.after
    if isinstance(t, (tuple, list)) and len(t) == 2:
        return t[0], t[1]
    return None, None


def _component_centres(frame: Any, mask: Any, limit: int) -> List[Tuple[int, int]]:
    """(x, y) centres of 4-connected same-colour components, background excluded.

    Background = the most frequent colour. Cells under ``mask`` (the learned HUD)
    are skipped. Largest components first, capped at ``limit``. Pure numpy.
    """
    import numpy as np

    f = np.asarray(frame)
    if f.ndim != 2 or f.size == 0:
        return []
    vals, counts = np.unique(f, return_counts=True)
    bg = vals[int(np.argmax(counts))]
    seen = np.zeros(f.shape, dtype=bool)
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        if m.shape == f.shape:
            seen |= m
    seen |= f == bg
    h, w = f.shape
    comps: List[Tuple[int, int, int]] = []
    for r0 in range(h):
        for c0 in range(w):
            if seen[r0, c0]:
                continue
            colour = f[r0, c0]
            stack = [(r0, c0)]
            seen[r0, c0] = True
            cells = []
            while stack:
                r, c = stack.pop()
                cells.append((r, c))
                for rr, cc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                    if 0 <= rr < h and 0 <= cc < w and not seen[rr, cc] and f[rr, cc] == colour:
                        seen[rr, cc] = True
                        stack.append((rr, cc))
            # the member cell nearest the mean, so the click lands ON the component
            mr = sum(r for r, _ in cells) / len(cells)
            mc = sum(c for _, c in cells) / len(cells)
            r, c = min(cells, key=lambda p: (p[0] - mr) ** 2 + (p[1] - mc) ** 2)
            comps.append((len(cells), c, r))
    comps.sort(key=lambda t: -t[0])
    return [(x, y) for _n, x, y in comps[:max(0, limit)]]


class ArcAgi3Environment:
    """One ARC-AGI-3 game, one seed, competition-mode accounting."""

    def __init__(
        self,
        game_id: str,
        seed: int = 0,
        *,
        env_dir: Optional[str | os.PathLike[str]] = None,
        auto_reset_on_death: bool = True,
    ) -> None:
        self.seed = check_seed(seed)
        self.game_id = short_id(game_id)
        self.env_dir = resolve_env_dir(env_dir)
        self.auto_reset_on_death = auto_reset_on_death
        _, arcengine = require_arc()
        self._actions_enum = {a.value: a for a in arcengine.GameAction}
        arcade = get_arcade(self.env_dir)
        env = arcade.make(self.game_id, seed=seed)
        _quiet_arc_loggers()
        if env is None:
            raise ValueError(
                "unknown ARC game %r under %s; known: %s"
                % (game_id, self.env_dir, list_games(self.env_dir))
            )
        self._env = env
        self.baseline: List[int] = load_baselines(self.env_dir).get(self.game_id, [])
        self.ledger = ActionLedger()
        self.deaths = 0
        self._raw: Any = None
        self._frame: Any = None
        self._absorb(env.observation_space)
        from .perception import HudMask  # numpy; present whenever the engine is

        self.hud_mask = HudMask()
        if self._frame is not None:
            self.hud_mask.new_level(self._frame)
        #: ``(action, before, after)`` of the last charged action, for describe/tools.
        self.last: Optional[Tuple[Action, Any, Any]] = None
        # Arcade.make already opened the play; account for the opening RESET
        # the competition API performs, which is free.
        self.ledger.record(RESET_ACTION_ID, self.levels)

    # -- engine plumbing -------------------------------------------------------
    def _absorb(self, raw: Any) -> None:
        if raw is None:
            return
        self._raw = raw
        layer = _last_layer(getattr(raw, "frame", None))
        if layer is not None:
            self._frame = layer

    @property
    def state_name(self) -> str:
        st = getattr(self._raw, "state", None)
        return getattr(st, "name", str(st)) if st is not None else "NOT_PLAYED"

    @property
    def levels(self) -> int:
        return int(getattr(self._raw, "levels_completed", 0) or 0)

    @property
    def win_levels(self) -> int:
        return int(getattr(self._raw, "win_levels", 0) or 0)

    @property
    def actions(self) -> int:
        return self.ledger.actions

    @property
    def level_actions(self) -> List[int]:
        return list(self.ledger.level_actions)

    @property
    def resets(self) -> int:
        return self.ledger.resets

    def _engine_level_count(self) -> Optional[int]:
        n = getattr(getattr(self._env, "_game", None), "_action_count", None)
        return int(n) if isinstance(n, int) else None

    def _send_reset(self) -> None:
        # Competition mode: a RESET at 0 level actions returns the last frame
        # unchanged (offline it would be a FULL restart back to level 1).
        if self._engine_level_count() != 0:
            self._absorb(self._env.reset())
        self.ledger.record(RESET_ACTION_ID, self.levels)

    def _info(self) -> Dict[str, Any]:
        return {
            "game": self.game_id,
            "seed": self.seed,
            "engine_state": self.state_name,
            "win_levels": self.win_levels,
            "actions": self.actions,
            "level_actions": self.level_actions,
            "resets": self.resets,
        }

    # -- Environment -----------------------------------------------------------
    def observe(self) -> Obs:
        """Read-only: never steps the engine and never charges an action."""
        return Obs(
            state=self._frame.copy() if hasattr(self._frame, "copy") else self._frame,
            level=self.levels,
            done=self.done(),
            info=self._info(),
        )

    def act(self, action: Action, source: str = "model") -> Obs:
        if isinstance(action, int):
            action = (action, -1, -1)
        aid, x, y = (int(v) for v in (tuple(action) + (-1, -1))[:3])
        if aid != RESET_ACTION_ID and aid not in COUNTED_ACTION_IDS:
            raise ValueError("action id %r is not RESET (0) or 1..7" % (aid,))
        if self.done():
            return self.observe()
        before = self.levels
        prev = self._frame.copy() if hasattr(self._frame, "copy") else self._frame
        if aid == RESET_ACTION_ID:
            self._send_reset()
        else:
            data: Dict[str, int] = {}
            if aid == CLICK_ACTION_ID:
                data = {"x": max(0, x), "y": max(0, y)}
            raw = self._env.step(self._actions_enum[aid], data=data)
            if raw is None:
                raise RuntimeError("engine returned no frame for action %d" % aid)
            self._absorb(raw)
            self.ledger.record(aid, self.levels)
        frame = self._frame.copy() if hasattr(self._frame, "copy") else self._frame
        died = self.state_name == "GAME_OVER"
        if died:
            self.deaths += 1
            if self.auto_reset_on_death:
                self._send_reset()
        self.last = ((aid, x, y), prev, frame)
        self._learn_hud(aid, prev, frame, level_up=self.levels > before, died=died)
        return Obs(
            state=frame,
            level=self.levels,
            level_up=self.levels > before,
            died=died,
            done=self.done(),
            info=dict(self._info(), source=source),
        )

    def available_actions(self) -> List[int]:
        acts = getattr(self._raw, "available_actions", None) or []
        return [int(getattr(a, "value", a)) for a in acts]

    def done(self) -> bool:
        return self.state_name == "WIN"

    # -- optional hooks --------------------------------------------------------
    #: Most click candidates offered per frame (one per colour component).
    MAX_CLICK_CANDIDATES = 40

    def candidates(self) -> List[Action]:
        """Every action worth trying from the current frame.

        Simple actions (1-5, 7) the game offers, then one click at the centre of
        each connected non-background component when the game offers clicks.
        The reasoning loop's ``scan`` and ``auto_action`` both read this.
        """
        avail = [a for a in self.available_actions() if a != RESET_ACTION_ID]
        out: List[Action] = [(a, -1, -1) for a in avail if a != CLICK_ACTION_ID]
        if CLICK_ACTION_ID in avail and self._frame is not None:
            out.extend((CLICK_ACTION_ID, x, y) for x, y in _component_centres(
                _as_array(self._frame), self.hud(), self.MAX_CLICK_CANDIDATES))
        return out

    def auto_action(self) -> Optional[Action]:
        """One action from a non-LLM novelty explorer, or None when none is offered.

        Before this hook existed the loop's explorer fallbacks (after
        ``zero_act_turns`` turns with no action, on a model error, a prism
        strategy's ``auto_actions``) took ZERO actions on ARC: ``auto()`` stops at
        the first ``None``. Measured 2026-10-03 on ls20 seed 1: 40 model calls,
        0 actions, while every turn's code only wrote hypotheses.

        Untried-in-this-state first (in candidate order), else the least-tried
        candidate in this state. Counts are per HUD-masked state key, so a ticking
        step counter does not make every state look new.
        """
        cands = self.candidates()
        if not cands or self._frame is None:
            return None
        key = self.state_key(self._frame)
        tried = self._auto_tried.setdefault(key, {})
        best = min(cands, key=lambda a: tried.get(a, 0))
        tried[best] = tried.get(best, 0) + 1
        return best

    @property
    def _auto_tried(self) -> Dict[str, Dict[Action, int]]:
        d = self.__dict__.get("_auto_tried_d")
        if d is None:
            d = self.__dict__["_auto_tried_d"] = {}
        return d

    def primer(self) -> str:
        return (
            "ARC-AGI-3 game %s: a 64x64 grid of colour indices 0-15. Actions are "
            "(id, x, y): 1-5 and 7 take no coordinates (x = y = -1), 6 is a click at "
            "(x, y), 0 is RESET (restarts the current level and costs one action). "
            "Clear every level in as few actions as possible." % self.game_id
        )

    def _learn_hud(self, aid: int, prev: Any, frame: Any, *, level_up: bool, died: bool) -> None:
        if frame is None:
            return
        if level_up:
            self.hud_mask.new_level(self._frame)
        elif died or aid == RESET_ACTION_ID:
            # a restarted life refills bars; the confirmed mask stays
            self.hud_mask.new_life(self._frame)
        elif prev is not None:
            self.hud_mask.observe(prev, frame, aid)

    def state_key(self, state: Any) -> str:
        """Novelty key with the learned HUD / meter cells blanked out.

        A step counter or budget bar that ticks on every action leaves the key
        unchanged once :class:`.perception.HudMask` has confirmed it (two ticks on
        one line). The mask is per level, so keys are comparable within a level.
        """
        if not hasattr(state, "tobytes"):
            return "%08x" % zlib.crc32(repr(state).encode())
        return self.hud_mask.key(state)

    def hud(self) -> Any:
        """The learned HUD mask (a bool array) or None when nothing is masked."""
        return self.hud_mask.current()

    def significant_change(self, before: Any, after: Any) -> bool:
        """A board change; a HUD / meter / border-bar tick does not count."""
        from .perception import edge_tick_split

        b, a = _as_array(before), _as_array(after)
        if b.shape != a.shape:
            return True
        board, _hud = edge_tick_split(b, a, self.hud_mask.current())
        return bool(board.any())

    def needs_xy(self, action_id: int) -> bool:
        return int(action_id) == CLICK_ACTION_ID

    def describe(self, t: Any) -> str:
        """One transition as text; ``t`` has ``before``/``after`` or is ``(before, after)``."""
        from .perception import diff_text

        before, after = _before_after(t)
        if before is None or after is None:
            return "no transition"
        return diff_text(before, after, self.hud_mask.current())

    def render(self, obs: Obs, last: Any) -> str:
        """The SITUATION text: header, last transition, colours and a 2x2 board map."""
        from .perception import board_map, colour_counts

        state = obs.state if obs is not None else self._frame
        on_level = self.ledger.current_level_actions
        lines = [
            "GAME %s | level %d of %d | actions used %d (this level %d) | available %s"
            % (
                self.game_id,
                self.levels + 1,
                self.win_levels,
                self.actions,
                on_level,
                self.available_actions(),
            )
        ]
        if last is None:
            lines.append("Last action: none yet")
        else:
            act = getattr(last, "action", None)
            shown = act if act is not None else last
            lines.append("Last action: %r -> %s" % (shown, self.describe(last)))
        if state is None:
            lines.append("(no frame)")
            return "\n".join(lines)
        cc = colour_counts(state)
        lines.append(
            "Background colour %d. Colours (colour:cells): %s"
            % (cc[0][0] if cc else -1, ", ".join("%d:%d" % kv for kv in cc[:8]))
        )
        mask = self.hud_mask.current()
        if mask is not None:
            lines.append("HUD / meter cells masked from the state key: %d ('#')" % int(mask.sum()))
        lines.append(
            "Board map (1 char = 2x2 cells, '.' background, else the rarest colour, hex):\n%s"
            % board_map(state, 2, mask)
        )
        return "\n".join(lines)

    def tools(self, loop: Any) -> Dict[str, Tuple[Any, str]]:
        """REPL tools: ``view``, ``diff``, ``colours``, ``hud_cells``. ``loop`` may be None."""
        from .perception import colour_counts, diff_text, view_text

        env = self

        def _frame(f: Any = None) -> Any:
            return env._frame if f is None else f

        def view(r0: int = 0, c0: int = 0, r1: int = 63, c1: int = 63, f: Any = None) -> str:
            return view_text(_frame(f), r0, c0, r1, c1)

        def diff(a: Any = None, b: Any = None) -> str:
            if a is not None and b is not None:
                return diff_text(a, b, env.hud_mask.current())
            history = getattr(loop, "history", None)
            if history is not None and len(history):
                return env.describe(history[-1])
            if env.last is None:
                return "no transitions yet"
            return "%r: %s" % (env.last[0], env.describe(env.last[1:]))

        def colours(f: Any = None) -> List[Tuple[int, int]]:
            return colour_counts(_frame(f))

        def hud_cells() -> Dict[str, Any]:
            mask = env.hud_mask.current()
            if mask is None:
                return {"cells": 0}
            rows = [int(r) for r in mask.any(axis=1).nonzero()[0]]
            cols = [int(c) for c in mask.any(axis=0).nonzero()[0]]
            return {
                "cells": int(mask.sum()),
                "rows": [rows[0], rows[-1]],
                "cols": [cols[0], cols[-1]],
            }

        return {
            "view": (view, "view(r0, c0, r1, c1, f=None): hex crop of the frame"),
            "diff": (diff, "diff(a=None, b=None): the last transition, or two frames, as text"),
            "colours": (colours, "colours(f=None): [(colour, cells)] most common first"),
            "hud_cells": (hud_cells, "hud_cells(): the learned HUD / meter mask extent"),
        }
