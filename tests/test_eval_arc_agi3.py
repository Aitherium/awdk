"""ARC-AGI-3 eval: RHAE scorer, RESET accounting, seed guard, suite, engine adapter.

The engine tests need the optional ``arc_agi``/``arcengine`` packages; the
real-game test also needs the public games directory. Both skip cleanly when
absent. Everything else is hermetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, List

import pytest
from adk.evalharness.arc_agi3 import rhae
from adk.evalharness.arc_agi3.env_arc import (
    ArcAgi3Environment,
    ArcUnavailableError,
    check_seed,
    resolve_env_dir,
)
from adk.evalharness.arc_agi3.rhae import ActionLedger, game_rhae, level_actions_for
from adk.evalharness.arc_agi3.suite import (
    CappedEnvironment,
    CapReachedError,
    random_policy,
    resolve_seeds,
    run_suite,
)
from adk.reasoning.solve import Action, Environment, Obs
from adk.reasoning.solve.conformance import check_environment

AWDK = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------
# RHAE, three hand-computed cases
# ----------------------------------------------------------------------------
def test_rhae_known_case_partial_game() -> None:
    # L1 17/17 -> 1.0; L2 38/76 -> 0.25; L3 not done.  (1*1 + 2*.25 + 0) / 6
    assert game_rhae([17, 76], [17, 38, 31]) == pytest.approx(0.25)


def test_rhae_cap_and_unfinished_tail() -> None:
    # L1 (10/5)^2 = 4 -> capped at 1.15; L2 not done.  1*1.15 / (1+2)
    assert game_rhae([5], [10, 4]) == pytest.approx(1.15 / 3)


def test_rhae_with_a_reset_charged() -> None:
    # opening RESET free; 3x ACTION1; RESET (+1); ACTION1 clears L1 at 5.
    # 3x ACTION1 more, the last clears L2 at 3.
    steps = [(0, 0), (1, 0), (1, 0), (1, 0), (0, 0), (1, 1), (1, 1), (1, 1), (1, 2)]
    la = level_actions_for(steps)
    assert la == [5, 3]
    # (1*(4/5)^2 + 2*min(1.15, (6/3)^2)) / 3 = (0.64 + 2.3) / 3
    assert game_rhae(la, [4, 6]) == pytest.approx(0.98)
    # Had the RESET been free, L1 would read 4 actions and score 1.0, not 0.64.
    assert game_rhae([4, 3], [4, 6]) != pytest.approx(0.98)


def test_ledger_charges_every_reset_after_the_opening() -> None:
    led = ActionLedger()
    assert led.cost(0) == 0
    led.record(0, 0)
    assert led.cost(0) == 1
    led.record(0, 0)
    led.record(0, 0)
    assert (led.actions, led.resets, led.level_actions) == (2, 2, [])
    led.record(3, 1)
    assert led.level_actions == [3]


def test_rhae_more_levels_than_baselines_is_an_error() -> None:
    with pytest.raises(ValueError):
        game_rhae([1, 1], [1])


def _run_selftest(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "adk.evalharness.arc_agi3.rhae", "--self-test"],
        cwd=str(AWDK),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def test_rhae_selftest_passes_and_its_break_arm_fails() -> None:
    env = {k: v for k, v in os.environ.items() if k != "RHAE_SELFTEST_BREAK"}
    ok = _run_selftest(env)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    broken = _run_selftest(dict(env, RHAE_SELFTEST_BREAK="1"))
    assert broken.returncode == 1, broken.stdout + broken.stderr


# ----------------------------------------------------------------------------
# seed guard: 10-69 is the held-out band
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [10, 42, 69])
def test_heldout_seed_is_refused(seed: int) -> None:
    with pytest.raises(ValueError, match="held-out"):
        check_seed(seed)
    with pytest.raises(ValueError, match="held-out"):
        resolve_seeds([0, seed])
    with pytest.raises(ValueError, match="held-out"):
        ArcAgi3Environment("ls20", seed)  # refused before the engine is touched


def test_seed_band_edges_and_counts() -> None:
    assert check_seed(9) == 9 and check_seed(70) == 70
    assert resolve_seeds(10) == list(range(10))
    with pytest.raises(ValueError):
        resolve_seeds(11)  # 0..10 reaches into the band
    with pytest.raises(TypeError):
        check_seed(True)  # type: ignore[arg-type]


def test_run_suite_refuses_heldout_seed_before_playing() -> None:
    played: List[int] = []

    def policy(env: Environment, ctx: Any) -> None:
        played.append(ctx.seed)

    with pytest.raises(ValueError, match="held-out"):
        run_suite(
            policy,
            ["fake"],
            seeds=[0, 50],
            baselines={"fake": [3]},
            make_env=lambda g, s: FakeEnv(),
        )
    assert played == []


# ----------------------------------------------------------------------------
# suite mechanics on a hermetic environment
# ----------------------------------------------------------------------------
class FakeEnv:
    """One level: three ACTION1 clear it. ACTION2 does nothing."""

    def __init__(self) -> None:
        self.ledger = ActionLedger()
        self.ledger.record(0, 0)
        self.count = 0
        self.levels_done = 0

    @property
    def actions(self) -> int:
        return self.ledger.actions

    @property
    def level_actions(self) -> List[int]:
        return list(self.ledger.level_actions)

    @property
    def resets(self) -> int:
        return self.ledger.resets

    def observe(self) -> Obs:
        return Obs(state=self.count, level=self.levels_done, done=self.done())

    def act(self, action: Action, source: str = "model") -> Obs:
        if action[0] == 1:
            self.count += 1
        elif action[0] == 0:
            self.count = 0
        if self.count >= 3:
            self.levels_done = 1
        self.ledger.record(action[0], self.levels_done)
        return self.observe()

    def available_actions(self) -> List[int]:
        return [1, 2]

    def done(self) -> bool:
        return self.levels_done >= 1


def test_suite_scores_writes_jsonl_and_summary(tmp_path: Path) -> None:
    def solver(env: Environment, ctx: Any) -> dict:
        env.act((1, -1, -1))
        env.act((0, -1, -1))  # a RESET: +1, and it restarts the count
        for _ in range(3):
            env.act((1, -1, -1))
        return {"llm_calls": 2}

    res = run_suite(
        solver,
        ["fake"],
        seeds=[0, 70],
        baselines={"fake": [5]},
        make_env=lambda g, s: FakeEnv(),
        out=tmp_path,
        run="t1",
    )
    assert [r["seed"] for r in res.rows] == [0, 70]
    row = res.rows[0]
    assert row["level_actions"] == [5] and row["resets"] == 1
    assert row["local_rhae"] == pytest.approx(1.0)
    assert row["abandon_reason"] is None and row["llm_calls"] == 2
    lines = (tmp_path / "t1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["game"] == "fake"
    summary = json.loads((tmp_path / "t1.summary.json").read_text(encoding="utf-8"))
    assert summary["mean_rhae_first_seed"] == pytest.approx(1.0)
    assert summary["llm_calls"] == 4
    assert res.exit_code == 0


def test_suite_action_cap_counts_resets(tmp_path: Path) -> None:
    def stubborn(env: Environment, ctx: Any) -> None:
        while True:
            env.act((0, -1, -1))  # RESETs only: each costs one action

    res = run_suite(
        stubborn,
        ["fake"],
        seeds=1,
        cap_actions=7,
        baselines={"fake": [3]},
        make_env=lambda g, s: FakeEnv(),
    )
    row = res.rows[0]
    assert row["actions"] == 7 and row["abandon_reason"] == "cap_actions"
    assert row["local_rhae"] == 0.0
    assert res.exit_code == 1  # at the random floor


def test_suite_ends_a_loop_of_uncharged_calls() -> None:
    class Free(FakeEnv):
        def act(self, action: Action, source: str = "model") -> Obs:
            return self.observe()  # never charges, never progresses

    def spinner(env: Environment, ctx: Any) -> None:
        while True:
            env.act((2, -1, -1))

    res = run_suite(
        spinner,
        ["fake"],
        seeds=1,
        cap_actions=5,
        baselines={"fake": [3]},
        make_env=lambda g, s: Free(),
    )
    assert res.rows[0]["abandon_reason"] == "cap_steps"


def test_suite_records_a_broken_policy_and_continues() -> None:
    def broken(env: Environment, ctx: Any) -> None:
        raise KeyError("nope")

    res = run_suite(
        broken, ["fake"], seeds=[0, 1], baselines={"fake": [3]}, make_env=lambda g, s: FakeEnv()
    )
    assert len(res.rows) == 2
    assert all(r["abandon_reason"].startswith("policy_error: KeyError") for r in res.rows)


def test_capped_env_is_an_environment_and_raises_at_the_cap() -> None:
    capped = CappedEnvironment(FakeEnv(), cap_actions=1)
    assert check_environment(capped, probe=True) == []
    capped.act((2, -1, -1))
    assert capped.done()
    with pytest.raises(CapReachedError):
        capped.act((2, -1, -1))


def test_random_policy_on_fake_env_wins_or_caps() -> None:
    res = run_suite(
        random_policy,
        ["fake"],
        seeds=[0, 1, 2],
        cap_actions=50,
        baselines={"fake": [3]},
        make_env=lambda g, s: FakeEnv(),
    )
    assert all(r["actions"] <= 50 for r in res.rows)
    assert all(r["levels"] == 1 or r["abandon_reason"] == "cap_actions" for r in res.rows)


# ----------------------------------------------------------------------------
# the real engine (optional deps)
# ----------------------------------------------------------------------------
TT01_SRC = """
from arcengine import ARCBaseGame, GameAction, Level, Sprite


class Tt01(ARCBaseGame):
    def __init__(self, seed: int = 0) -> None:
        levels = [Level(sprites=[Sprite([[i + 1]], name="p", x=0, y=i)], name="L%d" % i)
                  for i in range(3)]
        self.count = 0
        super().__init__(game_id="tt01", levels=levels, available_actions=[1, 2], seed=seed)

    def on_set_level(self, level) -> None:
        self.count = 0

    def step(self) -> None:
        aid = self.action.id
        if aid == GameAction.ACTION1:
            self.count += 1
            self.current_level.get_sprites_by_name("p")[0].set_position(self.count, 0)
            if self.count >= 2:
                self.next_level()
        elif aid == GameAction.ACTION2:
            self.lose()
        self.complete_action()
"""


@pytest.fixture()
def arc_engine() -> Any:
    return pytest.importorskip("arc_agi"), pytest.importorskip("arcengine")


@pytest.fixture()
def tt01_dir(tmp_path: Path, arc_engine: Any) -> Path:
    """A three-level scripted game: two ACTION1 clear a level, ACTION2 loses."""
    d = tmp_path / "environment_files" / "tt01" / "00000001"
    d.mkdir(parents=True)
    (d / "tt01.py").write_text(TT01_SRC, encoding="utf-8")
    (d / "metadata.json").write_text(
        json.dumps(
            {
                "game_id": "tt01-00000001",
                "title": "TT01",
                "default_fps": 20,
                "tags": [],
                "baseline_actions": [2, 2, 2],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path / "environment_files"


@pytest.fixture()
def games_dir(arc_engine: Any) -> Path:
    try:
        d = resolve_env_dir()
    except ArcUnavailableError:
        pytest.skip("public ARC-AGI-3 games not configured (set ADK_ARC_ENV_DIR)")
    if not (d / "ls20").is_dir() or not (d / "ft09").is_dir():
        pytest.skip("public ARC-AGI-3 games not found at %s (set ADK_ARC_ENV_DIR)" % d)
    return d


# The competition scorecard charges this script [4, 7, 2] (measured against
# arc_agi's own COMPETITION-mode API): a mid-level RESET, two RESETs at 0 level
# actions right after a flip (no engine step, +1 each), a GAME_OVER and the
# RESET after it. The opening RESET is the environment's construction.
SCRIPT = [1, 0, 1, 1, 0, 0, 1, 2, 0, 1, 1, 1, 1]


def test_adapter_charges_resets_like_the_competition_scorecard(tt01_dir: Path) -> None:
    env = ArcAgi3Environment("tt01", 0, env_dir=tt01_dir, auto_reset_on_death=False)
    assert check_environment(env, probe=True) == []
    assert env.baseline == [2, 2, 2]
    for aid in SCRIPT:
        env.act((aid, -1, -1))
    assert env.done() and env.state_name == "WIN"
    assert env.level_actions == [4, 7, 2]
    assert env.actions == 13
    assert game_rhae(env.level_actions, env.baseline) == pytest.approx(
        (0.25 + 2 * (2 / 7) ** 2 + 3 * 1.0) / 6
    )


def test_adapter_auto_reset_after_death_costs_the_same(tt01_dir: Path) -> None:
    env = ArcAgi3Environment("tt01", 0, env_dir=tt01_dir)
    died: List[bool] = []
    for aid in [a for i, a in enumerate(SCRIPT) if i != 8]:  # drop the explicit RESET
        died.append(env.act((aid, -1, -1)).died)
    assert died.count(True) == 1 and env.deaths == 1
    assert env.level_actions == [4, 7, 2]
    assert env.observe().done


def test_unknown_game_is_a_clear_error(tt01_dir: Path) -> None:
    with pytest.raises(ValueError, match="unknown ARC game"):
        ArcAgi3Environment("zz99", 0, env_dir=tt01_dir)


def test_random_policy_plays_two_real_games(games_dir: Path, tmp_path: Path) -> None:
    res = run_suite(
        random_policy,
        "ls20,ft09",
        seeds=1,
        cap_actions=60,
        env_dir=str(games_dir),
        out=tmp_path,
        run="rnd",
    )
    assert [r["game"] for r in res.rows] == ["ls20", "ft09"]
    for r in res.rows:
        assert 0 < r["actions"] <= 60
        assert r["baseline"], r
        assert r["abandon_reason"] in (None, "cap_actions"), r
        assert r["local_rhae"] == pytest.approx(game_rhae(r["level_actions"], r["baseline"]))
    assert len((tmp_path / "rnd.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert (tmp_path / "rnd.summary.json").is_file()
    assert res.exit_code in (0, 1)
    assert rhae.RANDOM_FLOOR > 0
