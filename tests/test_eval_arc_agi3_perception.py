"""ARC-AGI-3 perception: the learned HUD mask, the loop hooks, and the games-dir rule.

The synthetic tests need numpy only. The engine test needs ``arc_agi`` and the
public games (``ADK_ARC_ENV_DIR``); it skips cleanly when either is absent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Tuple

import pytest
from adk.evalharness.arc_agi3.env_arc import ArcUnavailableError, resolve_env_dir
from adk.reasoning.solve.conformance import check_environment

try:
    import numpy as np
except ImportError:  # the env-dir tests below still run without numpy
    np = None  # type: ignore[assignment]

needs_numpy = pytest.mark.skipif(np is None, reason="numpy not installed")

BAR_ROW, BAR_COLOUR, PLAYER = 63, 11, 9


class SyntheticGame:
    """A 64x64 board: a 2x2 player and a step bar on the bottom row that loses one
    cell per action, whatever the action (the shape most public games use)."""

    def __init__(self) -> None:
        self.r, self.c, self.t = 30, 30, 0

    def frame(self) -> Any:
        f = np.zeros((64, 64), np.int16)
        f[BAR_ROW, : 40 - self.t] = BAR_COLOUR
        f[self.r : self.r + 2, self.c : self.c + 2] = PLAYER
        return f

    def step(self, a: int) -> Any:
        dr, dc = {1: (-1, 0), 2: (1, 0), 3: (0, -1), 4: (0, 1)}[a]
        self.r = min(60, max(4, self.r + dr))
        self.c = min(60, max(4, self.c + dc))
        self.t += 1
        return self.frame()


def _play(actions: List[int]) -> Tuple[Any, List[Any], List[str]]:
    from adk.evalharness.arc_agi3.perception import HudMask

    game = SyntheticGame()
    hud = HudMask()
    prev = game.frame()
    hud.new_level(prev)
    frames, keys = [], []
    for a in actions:
        cur = game.step(a)
        hud.observe(prev, cur, a)
        frames.append(cur)
        keys.append(hud.key(cur))
        prev = cur
    return hud, frames, keys


@needs_numpy
def test_step_bar_is_masked_and_the_key_ignores_its_ticks() -> None:
    # up/down in place: the board repeats every 2 actions while the bar ticks
    hud, frames, keys = _play([1, 2] * 10)
    assert len({f.tobytes() for f in frames}) == 20  # raw frames: all distinct
    mask = hud.current()
    assert mask is not None and mask[BAR_ROW, :40].all()
    assert mask[:BAR_ROW].sum() == 0  # nothing on the board is masked
    warm = 2  # two ticks confirm the line
    assert len(set(keys[warm:])) == 2
    for t in range(warm, len(keys) - 2):
        assert keys[t] == keys[t + 2]


@needs_numpy
def test_board_change_still_changes_the_key() -> None:
    _hud, _frames, keys = _play([4] * 8)  # the player walks right: new states
    assert len(set(keys[2:])) == len(keys[2:])


@needs_numpy
def test_a_piece_pushed_one_way_is_not_a_meter() -> None:
    hud, _frames, _keys = _play([4] * 20)
    mask = hud.current()
    assert mask is None or mask[:BAR_ROW].sum() == 0


@needs_numpy
def test_a_moving_piece_on_the_border_reverts_and_is_not_masked() -> None:
    from adk.evalharness.arc_agi3.perception import HudMask

    hud = HudMask()
    f0 = np.zeros((64, 64), np.int16)
    f0[0, 10] = PLAYER
    hud.new_level(f0)
    prev = f0
    for c in (11, 10, 11, 10, 11):  # back and forth on row 0
        cur = np.zeros((64, 64), np.int16)
        cur[0, c] = PLAYER
        hud.observe(prev, cur, 4 if c == 11 else 3)
        prev = cur
    assert hud.current() is None


@needs_numpy
def test_level_change_clears_and_death_keeps_the_mask() -> None:
    hud, frames, _keys = _play([1, 2] * 4)
    assert hud.current() is not None
    hud.new_life(frames[0])
    assert hud.current() is not None
    hud.new_level(frames[0])
    assert hud.current() is None


@needs_numpy
def test_mask_is_capped() -> None:
    from adk.evalharness.arc_agi3.perception import HudMask

    hud = HudMask(max_frac=0.001)
    _g = SyntheticGame()
    prev = _g.frame()
    hud.new_level(prev)
    for a in [1, 2, 1, 2]:
        cur = _g.step(a)
        hud.observe(prev, cur, a)
        prev = cur
    assert hud.current() is None and hud.refused >= 1


@needs_numpy
def test_diff_text_separates_hud_from_board() -> None:
    from adk.evalharness.arc_agi3.perception import diff_text

    g = SyntheticGame()
    a = g.frame()
    g.t += 1  # the bar ticks, nothing else
    assert diff_text(a, g.frame()).startswith("no board change")
    b = g.step(4)
    assert "board cells changed" in diff_text(a, b)


# ----------------------------------------------------------------------------
# the games directory is never guessed
# ----------------------------------------------------------------------------
def test_env_dir_unset_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADK_ARC_ENV_DIR", raising=False)
    with pytest.raises(ArcUnavailableError, match="ADK_ARC_ENV_DIR"):
        resolve_env_dir()
    assert resolve_env_dir("x/y") == Path("x/y")


def test_suite_cli_exits_2_without_a_games_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    from adk.evalharness.arc_agi3.suite import main

    monkeypatch.delenv("ADK_ARC_ENV_DIR", raising=False)
    assert main(["--games", "ls20", "--cap-actions", "5"]) == 2


# ----------------------------------------------------------------------------
# a real game, when the engine and the public games are here
# ----------------------------------------------------------------------------
@pytest.fixture()
def public_games() -> Path:
    pytest.importorskip("arc_agi")
    pytest.importorskip("arcengine")
    d = os.environ.get("ADK_ARC_ENV_DIR", "")
    if not d or not (Path(d) / "ls20").is_dir():
        pytest.skip("public ARC-AGI-3 games not found (set ADK_ARC_ENV_DIR)")
    return Path(d)


@needs_numpy
def test_real_game_state_key_is_stable_across_hud_ticks(public_games: Path) -> None:
    from adk.evalharness.arc_agi3.env_arc import ArcAgi3Environment

    env = ArcAgi3Environment("ls20", 0, env_dir=public_games)
    assert check_environment(env, probe=True) == []
    raw, keys = [], []
    for _ in range(8):  # a closed walk: the board repeats every 4 actions
        for a in (1, 2, 3, 4):
            obs = env.act((a, -1, -1))
            assert not obs.died and not obs.level_up
            raw.append(obs.state.tobytes())
            keys.append(env.state_key(obs.state))
    mask = env.hud()
    assert mask is not None and 0 < mask.sum() <= 0.10 * mask.size
    warm = 4
    assert len(set(raw[warm:])) == len(raw) - warm  # the step bar makes every frame new
    assert len(set(keys[warm:])) <= 4
    for t in range(warm, len(keys) - 4):
        assert keys[t] == keys[t + 4], t
    # hooks render and describe that same game
    text = env.render(env.observe(), None)
    assert "GAME ls20" in text and "Board map" in text and "masked" in text
    _act, before, after = env.last
    assert env.describe((before, after)).endswith("HUD cells ignored)")
    tick = before.copy()
    tick[mask] = (tick[mask] + 1) % 16  # only the HUD changes
    assert env.describe((before, tick)).startswith("no board change")
    assert env.significant_change(before, tick) is False
    assert env.significant_change(before, after) is True
    tools = env.tools(None)
    assert set(tools) == {"view", "diff", "colours", "hud_cells"}
    assert tools["hud_cells"][0]()["cells"] == int(mask.sum())
    assert "rows 0-3" in tools["view"][0](0, 0, 3, 3)
    assert "board cells changed" in tools["diff"][0]()
    assert env.needs_xy(6) and not env.needs_xy(1)
