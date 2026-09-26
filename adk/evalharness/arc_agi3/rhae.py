"""The official ARC-AGI-3 RHAE scorer, plus the action accounting it depends on.

Port of the ARC agent repo's ``eval/rhae.py`` (formula, baselines, recording parser,
self-test and break arm), with one addition: :class:`ActionLedger`, the RESET rule
as a pure object so every environment adapter charges actions the same way.

Per level i (1-indexed) with human baseline b_i and agent actions a_i::

    s_i = min(1.15, (b_i / a_i) ** 2)   if the level was completed, else 0

A game scores ``sum_i(i * s_i) / sum_i(i)`` over ALL of its levels, so later levels
weigh more and an unfinished tail drags the game down. The total is the mean over
games. Competition mode is ONE run per game; ``score_rows`` reports max-over-runs
only because a local eval may run several seeds -- label it when you use it.

Action accounting (competition mode, arc_agi ``Card.inc_reset_count``): actions
1..7 cost 1 each; the OPENING RESET is free; every later RESET costs 1 action on
the current level; a level's actions are the cumulative count at its flip minus
the count at the previous flip.

The ``arc_agi`` package ships its own scorer and it is NOT this formula (it clamps
a faster-than-human level below 1.15). Never rank a change on
``EnvironmentScorecard.score``; ``self_test`` proves the disagreement when the
package is importable.

    python -m adk.evalharness.arc_agi3.rhae --self-test
    python -m adk.evalharness.arc_agi3.rhae --metrics rows.jsonl [--env-dir DIR]

Exit codes: 0 scored / self-test passed, 1 self-test failure, 2 cannot judge.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LEVEL_CAP = 1.15
COUNTED_ACTION_IDS = frozenset({1, 2, 3, 4, 5, 6, 7})
RESET_ACTION_ID = 0
COUNTED_ACTION_NAMES = frozenset("ACTION%d" % i for i in range(1, 8))

#: Mean RHAE of the uniform-random policy over the 25 public games (measured
#: 2026-09-22, one run per game). An eval at or below it has learned nothing.
RANDOM_FLOOR = 0.00219

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


# ----------------------------------------------------------------------------
# formula
# ----------------------------------------------------------------------------
def level_score(baseline: int, actions: int, completed: bool = True) -> float:
    """``min(1.15, (b/a)^2)`` for a completed level, 0 otherwise."""
    if not completed or actions is None or actions <= 0 or baseline is None:
        return 0.0
    return min(LEVEL_CAP, (float(baseline) / float(actions)) ** 2)


def game_rhae(level_actions: Sequence[int], baselines: Sequence[int]) -> float:
    """Level-weighted RHAE for one game.

    ``level_actions`` holds the action count of each COMPLETED level in order;
    ``baselines`` holds the human baseline for every level of the game. Levels
    beyond ``len(level_actions)`` score 0 but still carry their weight.
    """
    n = len(baselines)
    if n == 0:
        return 0.0
    if len(level_actions) > n:
        raise ValueError("more completed levels (%d) than baselines (%d)" % (len(level_actions), n))
    num = 0.0
    den = 0
    for i in range(1, n + 1):
        den += i
        if i <= len(level_actions):
            num += i * level_score(baselines[i - 1], level_actions[i - 1], True)
    return num / den if den else 0.0


def mean_rhae(game_scores: Dict[str, float]) -> float:
    if not game_scores:
        return 0.0
    return sum(game_scores.values()) / len(game_scores)


# ----------------------------------------------------------------------------
# accounting
# ----------------------------------------------------------------------------
class ActionLedger:
    """Charges actions exactly as the competition scorecard does.

    Call :meth:`record` once per action SENT (including RESETs) with the
    ``levels_completed`` the engine reports afterwards. The ledger does not
    decide what the engine does on a RESET -- only what it costs.
    """

    def __init__(self) -> None:
        self.actions = 0
        self.resets = 0
        self.steps = 0
        self.levels = 0
        self.level_actions: List[int] = []
        self._prev_cum = 0

    @property
    def current_level_actions(self) -> int:
        """Actions charged to the level in progress."""
        return self.actions - self._prev_cum

    def cost(self, action_id: int) -> int:
        """What sending ``action_id`` next would cost (0 or 1)."""
        if action_id == RESET_ACTION_ID:
            return 0 if self.steps == 0 else 1
        return 1 if action_id in COUNTED_ACTION_IDS else 0

    def record(self, action_id: int, levels_completed: int) -> List[int]:
        """Charge one sent action; return the level counts completed by it."""
        charge = self.cost(action_id)
        if action_id == RESET_ACTION_ID and self.steps > 0:
            self.resets += 1
        self.actions += charge
        self.steps += 1
        flipped: List[int] = []
        lc = int(levels_completed or 0)
        while self.levels < lc:  # one flip per step in practice; tolerate a jump
            flipped.append(self.actions - self._prev_cum)
            self._prev_cum = self.actions
            self.levels += 1
        self.level_actions.extend(flipped)
        return flipped


def level_actions_for(steps: Iterable[Tuple[int, int]]) -> List[int]:
    """Level action counts for ``(action_id, levels_completed_after)`` steps."""
    ledger = ActionLedger()
    for aid, lc in steps:
        ledger.record(aid, lc)
    return list(ledger.level_actions)


# ----------------------------------------------------------------------------
# baselines
# ----------------------------------------------------------------------------
def short_id(game_id: str) -> str:
    return str(game_id).split("-", 1)[0]


def load_baselines(env_dir: Optional[Path]) -> Dict[str, List[int]]:
    """``{short_game_id: baseline_actions}`` from every metadata.json under env_dir."""
    out: Dict[str, List[int]] = {}
    if env_dir is None:
        return out
    env_dir = Path(env_dir)
    if not env_dir.is_dir():
        return out
    for meta in sorted(env_dir.rglob("metadata.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        gid = data.get("game_id")
        base = data.get("baseline_actions")
        if not gid or not isinstance(base, list) or not base:
            continue
        out[short_id(str(gid))] = [int(b) for b in base]
    return out


# ----------------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------------
def read_metrics(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _action_id(action_input: Any) -> Optional[int]:
    """Recordings carry ``action_input.id`` as an int or an enum NAME."""
    if not isinstance(action_input, dict):
        return None
    aid = action_input.get("id")
    if isinstance(aid, int):
        return aid
    if isinstance(aid, str):
        if aid.isdigit():
            return int(aid)
        if aid == "RESET":
            return 0
        if aid in COUNTED_ACTION_NAMES:
            return int(aid[-1])
    return None


def recording_to_row(path: Path) -> Dict[str, Any]:
    """Parse one arc_agi recording into ``{game, levels, level_actions, actions, state}``."""
    game: Optional[str] = None
    ledger = ActionLedger()
    state = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            data = rec.get("data") if isinstance(rec, dict) else None
            if not isinstance(data, dict) or "action_input" not in data:
                continue
            game = game or data.get("game_id")
            aid = _action_id(data.get("action_input"))
            lc = data.get("levels_completed")
            if aid is not None:  # an unparseable action is neither charged nor a step
                ledger.record(aid, lc if isinstance(lc, int) else ledger.levels)
            state = data.get("state", state)
    return {
        "game": short_id(game) if game else None,
        "levels": ledger.levels,
        "level_actions": ledger.level_actions,
        "actions": ledger.actions,
        "state": state,
        "source": str(path),
    }


def score_rows(
    rows: Iterable[Dict[str, Any]], baselines: Dict[str, List[int]]
) -> Tuple[Dict[str, float], List[Dict[str, Any]], List[str]]:
    """Score every row; return ``({game: max score}, per-row results, problems)``."""
    per_game: Dict[str, float] = {}
    scored: List[Dict[str, Any]] = []
    problems: List[str] = []
    for row in rows:
        game = row.get("game")
        if not game:
            problems.append("row without game: %s" % repr(row)[:80])
            continue
        game = short_id(str(game))
        base = row.get("baseline") or baselines.get(game)
        if not base:
            problems.append("no baseline for %s" % game)
            continue
        la = row.get("level_actions") or []
        try:
            s = game_rhae(la, base)
        except ValueError as exc:
            problems.append("%s: %s" % (game, exc))
            continue
        scored.append({"game": game, "rhae": s, "levels": len(la), "level_actions": la})
        per_game[game] = max(per_game.get(game, 0.0), s)
    return per_game, scored, problems


# ----------------------------------------------------------------------------
# self-test
# ----------------------------------------------------------------------------
def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def self_test(env_dir: Optional[Path] = None) -> int:
    failures: List[str] = []

    def check(name: str, cond: bool) -> None:
        print("  [%s] %s" % ("ok" if cond else "FAIL", name))
        if not cond:
            failures.append(name)

    base = [17, 38, 31]
    # A: known case. L1 in 17 (=1.0), L2 in 76 (=0.25), L3 not done:
    #    (1*1.0 + 2*0.25 + 3*0) / 6 = 0.25
    check("known case = 0.25", _close(game_rhae([17, 76], base), 0.25))
    # B: the 1.15 cap
    check("cap 1.15 on a single-level game", _close(game_rhae([10], [17]), 1.15))
    # C: nothing done -> 0; everything at baseline -> 1.0
    check("no levels -> 0", _close(game_rhae([], base), 0.0))
    check("all levels at baseline -> 1.0", _close(game_rhae([17, 38, 31], base), 1.0))
    # D: level weights are the level indices
    check("weights are level indices (L1 only = 1/6)", _close(game_rhae([17], base), 1 / 6))
    # E: the unsquared ratio must NOT reproduce the known answer
    wrong = (1 * min(1.15, 17 / 17) + 2 * min(1.15, 38 / 76)) / 6
    check("unsquared b/a disagrees with the official value", not _close(wrong, 0.25))
    # F: mean over games of max over runs
    per_game, _, probs = score_rows(
        [
            {"game": "aaaa-1", "level_actions": [17], "baseline": base},
            {"game": "aaaa-1", "level_actions": [17, 76], "baseline": base},
            {"game": "bbbb", "level_actions": [], "baseline": [5]},
        ],
        {},
    )
    check("max over runs per game", _close(per_game.get("aaaa", -1), 0.25))
    check("mean over games", _close(mean_rhae(per_game), 0.125))
    check("no problems on well-formed rows", not probs)
    # G: baselines load (the shipped fixture, so this arm cannot silently skip)
    b = load_baselines(FIXTURES_DIR)
    check("fixture baselines load lp85", b.get("lp85", [0])[0] == 17)
    if env_dir is not None:
        real = load_baselines(Path(env_dir))
        check("env-dir baselines load (%d games)" % len(real), bool(real))
    # H: RESET accounting -- the opening RESET is free, every later one costs 1
    la = level_actions_for([(0, 0), (1, 0), (0, 0), (1, 0), (1, 1)])
    check("opening RESET free, later RESET +1 -> [4]", la == [4])
    # I: the arc_agi package scorer disagrees -> never rank on it
    try:
        from arc_agi.scorecard import EnvironmentScoreCalculator  # type: ignore

        calc = EnvironmentScoreCalculator(id="probe")
        calc.add_level(level_index=1, completed=True, actions_taken=10, baseline_actions=17)
        pkg = calc.to_score().score / 100.0
        check("arc_agi package scorer disagrees", not _close(pkg, game_rhae([10], [17]), 1e-6))
    except Exception:  # noqa: BLE001 - optional dep, any import/API drift
        print("  [skip] arc_agi scorer not usable; package-disagreement arm untested")
    # J: break arm -- proves this self-test can still fail
    if os.environ.get("RHAE_SELFTEST_BREAK"):
        check("RHAE_SELFTEST_BREAK forces a failure", False)

    if failures:
        print("SELF-TEST FAILED: %s" % ", ".join(failures))
        return 1
    print("SELF-TEST PASSED")
    return 0


# ----------------------------------------------------------------------------
# cli
# ----------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="ARC-AGI-3 RHAE scorer")
    p.add_argument("--metrics", type=Path, help="rows .jsonl from the arc_agi3 suite")
    p.add_argument("--recording", type=Path, action="append", help="arc_agi recording .jsonl")
    p.add_argument("--env-dir", type=Path, default=None, help="environment_files (baselines)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test(args.env_dir)
    rows: List[Dict[str, Any]] = []
    if args.metrics:
        if not args.metrics.is_file():
            print("cannot judge: %s is not a file" % args.metrics)
            return 2
        rows.extend(read_metrics(args.metrics))
    for rec in args.recording or []:
        if not rec.is_file():
            print("cannot judge: %s is not a file" % rec)
            return 2
        rows.append(recording_to_row(rec))
    if not rows:
        print("cannot judge: no rows (pass --metrics or --recording)")
        return 2
    per_game, scored, problems = score_rows(rows, load_baselines(args.env_dir))
    if not per_game:
        print("cannot judge: no scorable rows; problems: %s" % problems[:5])
        return 2
    total = mean_rhae(per_game)
    if args.json:
        print(
            json.dumps(
                {"mean_rhae": total, "games": per_game, "rows": scored, "problems": problems},
                indent=2,
            )
        )
    else:
        for g in sorted(per_game):
            print("  %-6s %.4f" % (g, per_game[g]))
        print("games=%d rows=%d mean_rhae=%.4f" % (len(per_game), len(scored), total))
        for pr in problems[:10]:
            print("  problem: %s" % pr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
