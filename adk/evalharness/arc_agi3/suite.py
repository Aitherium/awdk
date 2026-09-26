"""ARC-AGI-3 eval suite: any policy or loop over N games x seeds, scored by RHAE.

A *policy* here is any callable that plays one episode::

    def episode(env: Environment, ctx: EpisodeContext) -> Mapping[str, Any] | None:
        while not env.done():
            env.act((1, -1, -1), source="policy")
        return {"llm_calls": 3}          # optional extras merged into the row

The suite hands it a capped view of the environment: once ``cap_actions``
counted actions (or ``cap_s`` seconds) are spent, ``done()`` turns True and a
further ``act()`` raises :class:`CapReachedError`, which the suite records as the
row's ``abandon_reason``. A reasoning loop (``adk.reasoning.solve``) plugs in
the same way -- ``lambda env, ctx: solve_sync(env, ...)`` -- because it only
needs the Environment protocol. :func:`step_policy` adapts a per-step chooser.

Rows are written in the local_loop schema (``game, policy, seed, levels,
level_actions, baseline, local_rhae, wall_s, actions, resets, deaths, llm_calls,
llm_s, abandon_reason, state, run, ts``) to ``<out>/<run>.jsonl`` with a
``<run>.summary.json`` beside it.

Scoring honesty: competition mode is ONE run per game, so the headline is
``mean_rhae_first_seed`` (the first seed of every game). ``mean_rhae_max_over_seeds``
is reported separately and labelled as such. Seeds 10-69 are the held-out band
and are refused.

    python -m adk.evalharness.arc_agi3.suite --games ls20,ft09 --seeds 1 --cap-actions 200
    python -m adk.evalharness.arc_agi3.suite --self-test

    python -m adk.evalharness.arc_agi3.suite --games ls20 --policy llm --cap-actions 30

Exit codes: 0 mean RHAE above the random floor, 1 at or below it (or a failed
self-test), 2 cannot judge (engine or games missing, zero rows, or the model
backend dead on every row -- an exception carrying ``backend_dead = True``).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from adk.reasoning.solve._types import Action, Environment, Obs

from . import rhae
from .env_arc import (
    CLICK_ACTION_ID,
    ArcAgi3Environment,
    ArcUnavailableError,
    check_seed,
    list_games,
    resolve_env_dir,
)

EpisodeFn = Callable[[Environment, "EpisodeContext"], Optional[Mapping[str, Any]]]
EnvFactory = Callable[[str, int], Environment]


class CapReachedError(RuntimeError):
    """The episode spent its action or wall budget."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class EpisodeContext:
    game: str
    seed: int
    cap_actions: int
    cap_s: Optional[float]
    baseline: List[int]
    run: str
    rng: random.Random = field(repr=False, default_factory=random.Random)


class CappedEnvironment:
    """Wraps an Environment; enforces the suite's action and wall caps.

    ``actions`` is read from the wrapped environment (its ledger is the scored
    count), so a RESET's +1 counts against the cap exactly as it counts
    against the score.
    """

    def __init__(self, env: Environment, cap_actions: int, cap_s: Optional[float] = None) -> None:
        self._env = env
        self.cap_actions = int(cap_actions)
        self.cap_s = cap_s
        # act() calls, charged or not: a loop of uncharged calls (or an env whose
        # ledger never moves) must still end.
        self.cap_steps = 4 * self.cap_actions + 16
        self.steps = 0
        self._t0 = time.perf_counter()
        self.cap_reason: Optional[str] = None

    def _spent(self) -> Optional[str]:
        if int(getattr(self._env, "actions", 0)) >= self.cap_actions:
            return "cap_actions"
        if self.steps >= self.cap_steps:
            return "cap_steps"
        if self.cap_s is not None and time.perf_counter() - self._t0 >= self.cap_s:
            return "cap_s"
        return None

    def observe(self) -> Obs:
        return self._env.observe()

    def act(self, action: Action, source: str = "model") -> Obs:
        reason = self._spent()
        if reason:
            self.cap_reason = reason
            raise CapReachedError(reason)
        self.steps += 1
        return self._env.act(action, source=source)

    def available_actions(self) -> List[int]:
        return self._env.available_actions()

    def done(self) -> bool:
        if self._env.done():
            return True
        reason = self._spent()
        if reason:
            self.cap_reason = reason
            return True
        return False

    def __getattr__(self, name: str) -> Any:  # optional hooks + ledger properties
        return getattr(self._env, name)


# ----------------------------------------------------------------------------
# policies
# ----------------------------------------------------------------------------
def step_policy(
    choose: Callable[[Obs, List[int], random.Random], Action], name: Optional[str] = None
) -> EpisodeFn:
    """Turn a per-step ``choose(obs, available, rng) -> Action`` into an episode."""

    def episode(env: Environment, ctx: EpisodeContext) -> None:
        obs = env.observe()
        while not env.done():
            obs = env.act(choose(obs, env.available_actions(), ctx.rng), source="policy")
        return None

    episode.__name__ = name or getattr(choose, "__name__", "step_policy")
    return episode


def _random_choice(obs: Obs, available: List[int], rng: random.Random) -> Action:
    choices = [a for a in available if a in rhae.COUNTED_ACTION_IDS] or sorted(
        rhae.COUNTED_ACTION_IDS
    )
    a = rng.choice(choices)
    if a == CLICK_ACTION_ID:
        return (a, rng.randint(0, 63), rng.randint(0, 63))
    return (a, -1, -1)


#: Uniform random over the available actions (the RHAE floor's policy).
random_policy: EpisodeFn = step_policy(_random_choice, name="random")


# ----------------------------------------------------------------------------
# seeds / games
# ----------------------------------------------------------------------------
def resolve_seeds(seeds: Union[int, Iterable[int]]) -> List[int]:
    """``N`` -> ``[0..N-1]``; an iterable is taken as-is. Held-out seeds are refused."""
    if isinstance(seeds, bool):
        raise TypeError("seeds must be an int count or a list of ints")
    out = list(range(seeds)) if isinstance(seeds, int) else [int(s) for s in seeds]
    if not out:
        raise ValueError("no seeds")
    for s in out:
        check_seed(s)
    return out


def resolve_games(
    games: Union[str, Sequence[str]], baselines: Mapping[str, List[int]]
) -> List[str]:
    if isinstance(games, str):
        if games.strip().lower() == "all":
            return sorted(baselines)
        games = [g for g in games.split(",") if g.strip()]
    wanted = [rhae.short_id(g.strip()) for g in games]
    missing = [g for g in wanted if g not in baselines]
    if missing:
        raise ValueError("unknown game(s) %s; known: %s" % (missing, sorted(baselines)))
    return wanted


# ----------------------------------------------------------------------------
# the suite
# ----------------------------------------------------------------------------
@dataclass
class SuiteResult:
    rows: List[Dict[str, Any]]
    summary: Dict[str, Any]
    rows_path: Optional[Path]
    summary_path: Optional[Path]

    @property
    def exit_code(self) -> int:
        if not self.rows or all(r.get("backend_dead") for r in self.rows):
            return 2
        return 0 if self.summary["mean_rhae_first_seed"] > rhae.RANDOM_FLOOR else 1


def play_episode(
    policy: EpisodeFn,
    env: Environment,
    ctx: EpisodeContext,
    *,
    policy_name: str,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    capped = CappedEnvironment(env, ctx.cap_actions, ctx.cap_s)
    extras: Mapping[str, Any] = {}
    reason: Optional[str] = None
    try:
        got = policy(capped, ctx)
        extras = got or {}
    except CapReachedError as exc:
        reason = exc.reason
    except Exception as exc:  # noqa: BLE001 - a broken policy is a measured failure
        if getattr(exc, "backend_dead", False):
            reason = "backend_dead: %s" % exc
            extras = {"backend_dead": True}
        else:
            reason = "policy_error: %s: %s" % (type(exc).__name__, exc)
    if reason is None and not env.done():
        reason = capped.cap_reason or "policy_returned"
    level_actions = list(getattr(env, "level_actions", []) or [])
    try:
        score = rhae.game_rhae(level_actions, ctx.baseline) if ctx.baseline else 0.0
    except ValueError:
        score = 0.0
    row: Dict[str, Any] = {
        "game": ctx.game,
        "policy": policy_name,
        "seed": ctx.seed,
        "levels": len(level_actions),
        "level_actions": level_actions,
        "baseline": list(ctx.baseline),
        "local_rhae": score,
        "wall_s": round(time.perf_counter() - t0, 4),
        "actions": int(getattr(env, "actions", 0) or 0),
        "resets": int(getattr(env, "resets", 0) or 0),
        "deaths": int(getattr(env, "deaths", 0) or 0),
        "llm_calls": 0,
        "llm_s": 0.0,
        "abandon_reason": None if env.done() else reason,
        "state": getattr(env, "state_name", None),
        "run": ctx.run,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for k, v in dict(extras).items():
        if k not in ("game", "seed", "level_actions", "local_rhae", "baseline", "actions"):
            row[k] = v
    return row


def summarize(rows: List[Dict[str, Any]], seeds: Sequence[int]) -> Dict[str, Any]:
    first = seeds[0] if seeds else None
    first_rows = {r["game"]: r["local_rhae"] for r in rows if r["seed"] == first}
    max_rows: Dict[str, float] = {}
    for r in rows:
        max_rows[r["game"]] = max(max_rows.get(r["game"], 0.0), r["local_rhae"])
    mean_first = rhae.mean_rhae(first_rows)
    return {
        "rows": len(rows),
        "games": len(max_rows),
        "seeds": list(seeds),
        "mean_rhae_first_seed": mean_first,
        "mean_rhae_max_over_seeds": rhae.mean_rhae(max_rows),
        "mean_rhae_rows": (sum(r["local_rhae"] for r in rows) / len(rows)) if rows else 0.0,
        "random_floor": rhae.RANDOM_FLOOR,
        "above_floor": mean_first > rhae.RANDOM_FLOOR,
        "games_with_a_level": sorted({r["game"] for r in rows if r["levels"] > 0}),
        "total_actions": sum(r["actions"] for r in rows),
        "llm_calls": sum(int(r.get("llm_calls") or 0) for r in rows),
        "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in rows),
        "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in rows),
        "backend_dead_rows": sum(1 for r in rows if r.get("backend_dead")),
        "wall_s": round(sum(r["wall_s"] for r in rows), 3),
        "abandon_reasons": sorted({str(r["abandon_reason"]) for r in rows if r["abandon_reason"]}),
    }


def run_suite(
    policy: EpisodeFn,
    games: Union[str, Sequence[str]] = "all",
    *,
    seeds: Union[int, Iterable[int]] = 1,
    cap_actions: int = 400,
    cap_s: Optional[float] = None,
    env_dir: Optional[str] = None,
    out: Optional[Union[str, Path]] = None,
    run: Optional[str] = None,
    policy_name: Optional[str] = None,
    make_env: Optional[EnvFactory] = None,
    baselines: Optional[Mapping[str, List[int]]] = None,
) -> SuiteResult:
    """Play ``policy`` on every game x seed and score each row with RHAE.

    ``make_env(game, seed)`` defaults to :class:`ArcAgi3Environment` over
    ``env_dir``; ``baselines`` default to the games directory's metadata.json.
    Raises :class:`ArcUnavailableError` when the default engine or games are missing,
    ``ValueError`` on a held-out seed or an unknown game.
    """
    seed_list = resolve_seeds(seeds)
    if cap_actions <= 0:
        raise ValueError("cap_actions must be positive")
    if baselines is None:
        d = resolve_env_dir(env_dir)
        if not d.is_dir():
            raise ArcUnavailableError("ARC games directory %s does not exist" % d)
        baselines = rhae.load_baselines(d)
        if not baselines:
            raise ArcUnavailableError("no metadata.json baselines under %s" % d)
    game_list = resolve_games(games, baselines)
    if make_env is None:

        def make_env(game: str, seed: int) -> Environment:
            return ArcAgi3Environment(game, seed, env_dir=env_dir)

    name = policy_name or getattr(policy, "__name__", "policy")
    run = run or "%s-%s-%dx%d" % (
        name,
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        len(game_list),
        len(seed_list),
    )
    rows_path: Optional[Path] = None
    fh = None
    if out is not None:
        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        rows_path = out_dir / ("%s.jsonl" % run)
        fh = open(rows_path, "w", encoding="utf-8")
    rows: List[Dict[str, Any]] = []
    try:
        for seed in seed_list:
            for game in game_list:
                ctx = EpisodeContext(
                    game=game,
                    seed=seed,
                    cap_actions=cap_actions,
                    cap_s=cap_s,
                    baseline=list(baselines.get(game, [])),
                    run=run,
                    rng=random.Random(seed * 1000003 + zlib.crc32(game.encode())),
                )
                try:
                    env = make_env(game, seed)
                except ArcUnavailableError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one broken game is a row
                    row = {
                        "game": game,
                        "policy": name,
                        "seed": seed,
                        "levels": 0,
                        "level_actions": [],
                        "baseline": ctx.baseline,
                        "local_rhae": 0.0,
                        "wall_s": 0.0,
                        "actions": 0,
                        "resets": 0,
                        "deaths": 0,
                        "llm_calls": 0,
                        "llm_s": 0.0,
                        "state": None,
                        "run": run,
                        "abandon_reason": "setup_error: %s: %s" % (type(exc).__name__, exc),
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                else:
                    row = play_episode(policy, env, ctx, policy_name=name)
                rows.append(row)
                if fh is not None:
                    fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
                    fh.flush()
    finally:
        if fh is not None:
            fh.close()
    summary = dict(
        summarize(rows, seed_list), run=run, policy=name, cap_actions=cap_actions, cap_s=cap_s
    )
    summary_path: Optional[Path] = None
    if rows_path is not None:
        summary_path = rows_path.with_name("%s.summary.json" % run)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return SuiteResult(rows=rows, summary=summary, rows_path=rows_path, summary_path=summary_path)


# ----------------------------------------------------------------------------
# cli
# ----------------------------------------------------------------------------
POLICIES: Dict[str, EpisodeFn] = {"random": random_policy}
#: Policies built at run time because they need a model (``--policy llm``).
MODEL_POLICIES = ("llm",)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="ARC-AGI-3 eval suite (RHAE)")
    p.add_argument("--games", default="all")
    p.add_argument("--seeds", type=int, default=1, help="seed count: 0..N-1 (N <= 10)")
    p.add_argument("--seed-list", default=None, help="explicit comma list, e.g. 0,1,70")
    p.add_argument("--cap-actions", type=int, default=400)
    p.add_argument("--cap-s", type=float, default=None)
    p.add_argument("--env-dir", default=None)
    p.add_argument("--out", default=None, help="directory for <run>.jsonl + summary")
    p.add_argument("--policy", default="random", choices=sorted(POLICIES) + list(MODEL_POLICIES))
    p.add_argument("--model", default=None, help="llm policy: model id, or auto (default)")
    p.add_argument("--scheduler-url", default=None, help="llm policy: MicroScheduler base URL")
    p.add_argument("--self-test", action="store_true", help="the RHAE scorer self-test")
    args = p.parse_args(argv)
    if args.self_test:
        return rhae.self_test()
    seeds: Union[int, List[int]] = args.seeds
    if args.seed_list:
        seeds = [int(s) for s in args.seed_list.split(",") if s.strip()]
    backend: Any = None
    policy: EpisodeFn
    if args.policy in MODEL_POLICIES:
        from adk.core.backends.microscheduler import MicroSchedulerBackend

        from .llm_policy import llm_policy

        backend = MicroSchedulerBackend(base_url=args.scheduler_url, model=args.model)
        try:
            print(
                "model: %s (%s) via %s"
                % (backend.resolve_model(), backend.model_source, backend.base_url)
            )
        except Exception as exc:  # noqa: BLE001 - reported, then judged below
            if not getattr(exc, "backend_dead", False):
                raise
            print("cannot judge: model backend dead: %s" % exc)
            return 2
        policy = llm_policy(backend)
    else:
        policy = POLICIES[args.policy]
    try:
        res = run_suite(
            policy,
            args.games,
            seeds=seeds,
            cap_actions=args.cap_actions,
            cap_s=args.cap_s,
            env_dir=args.env_dir,
            out=args.out,
            policy_name=args.policy,
        )
    except ArcUnavailableError as exc:
        print("cannot judge: %s" % exc)
        return 2
    except ValueError as exc:
        print("refused: %s" % exc)
        return 2
    for r in res.rows:
        print(
            "  %-6s seed=%-3d levels=%d actions=%-4d rhae=%.4f  %s"
            % (
                r["game"],
                r["seed"],
                r["levels"],
                r["actions"],
                r["local_rhae"],
                r["abandon_reason"] or "",
            )
        )
    s = res.summary
    print(
        "rows=%d games=%d  mean RHAE (first seed, headline)=%.5f  "
        "(max over seeds=%.5f)  floor=%.5f"
        % (
            s["rows"],
            s["games"],
            s["mean_rhae_first_seed"],
            s["mean_rhae_max_over_seeds"],
            rhae.RANDOM_FLOOR,
        )
    )
    if backend is not None:
        print(
            "model %(model)s: %(llm_calls)d calls, %(prompt_tokens)d prompt + "
            "%(completion_tokens)d completion tokens, %(llm_s).1f s" % backend.stats()
        )
    if res.rows_path:
        print("wrote %s and %s" % (res.rows_path, res.summary_path))
    if res.exit_code == 2 and res.rows:
        print("cannot judge: the model backend was dead on every row")
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CapReachedError",
    "CappedEnvironment",
    "EpisodeContext",
    "EpisodeFn",
    "SuiteResult",
    "list_games",
    "play_episode",
    "random_policy",
    "resolve_games",
    "resolve_seeds",
    "run_suite",
    "step_policy",
    "summarize",
]
