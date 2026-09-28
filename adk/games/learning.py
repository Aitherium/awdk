"""Play, remember, improve -- the Agent Home game-learning loop.

Each session the agent:

1. RECALLS what it learned about this room before (a persisted transition
   model + action values, and the lessons it wrote to adk memory).
2. PLAYS a bounded number of steps. Actions come from the agent's policy (its
   own model, if one is configured) or the learned values, with curiosity
   toward actions it has never tried in this situation -- the same
   "highest surprise first" idea as ``adk.packs.world_model.safe_explore``.
3. RECORDS every transition three ways: the local transition model (surprise =
   how badly it predicted the next state), the shared world-model pack
   (``wm_observe``, fail-soft -- a missing world-model service never stops
   play), and a session summary + per-situation lessons in adk memory.
4. SAVES, so the next session starts smarter. ``progress()`` reports whether
   reward is rising and surprise is falling across sessions -- the same test
   ``env_enroll`` applies to prove an environment was learned.

Persisting learning across sessions is the premium half of Agent Home (license
pack ``agent-home``); a one-off unpersisted session is free.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .base import GameClient, Observation

logger = logging.getLogger("adk.games.learning")

#: policy(observation, legal_actions, hints) -> an action, or None to defer.
Policy = Callable[[Observation, List[str], List[str]], Optional[str]]
WMObserve = Callable[..., Dict[str, Any]]

OPTIMISM = 0.3  # value assumed for an action never tried in a situation


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", text).strip("_")[:120] or "game"


def _default_state_dir() -> Path:
    from adk.home.config import home_dir

    return home_dir() / "games"


class TransitionModel:
    """Tabular world model + action values for one game domain.

    ``counts[s][a][s2]`` is how often action ``a`` in situation ``s`` led to
    ``s2``; surprise is ``1 - P(s2 | s, a)`` (1.0 when never seen). ``q[s][a]``
    is a one-step TD estimate of the action's value.
    """

    def __init__(self, domain: str, alpha: float = 0.5, gamma: float = 0.9) -> None:
        self.domain = domain
        self.alpha = alpha
        self.gamma = gamma
        self.counts: Dict[str, Dict[str, Dict[str, int]]] = {}
        self.q: Dict[str, Dict[str, float]] = {}
        self.sessions: List[Dict[str, Any]] = []

    # ── model ────────────────────────────────────────────────────────────
    def surprise(self, s: str, a: str, s2: str) -> float:
        seen = self.counts.get(s, {}).get(a)
        if not seen:
            return 1.0
        total = sum(seen.values())
        return 1.0 - seen.get(s2, 0) / total

    def predict(self, s: str, a: str) -> Optional[str]:
        seen = self.counts.get(s, {}).get(a)
        if not seen:
            return None
        return max(seen.items(), key=lambda kv: kv[1])[0]

    def value(self, s: str, a: str) -> float:
        return self.q.get(s, {}).get(a, OPTIMISM)

    def tried(self, s: str, a: str) -> bool:
        return a in self.counts.get(s, {})

    def update(self, s: str, a: str, r: float, s2: str, done: bool,
               next_actions: Sequence[str] = ()) -> float:
        """Learn one transition; returns the surprise it caused (pre-update)."""
        surprise = self.surprise(s, a, s2)
        row = self.counts.setdefault(s, {}).setdefault(a, {})
        row[s2] = row.get(s2, 0) + 1
        future = 0.0
        if not done and next_actions:
            future = max(self.value(s2, na) for na in next_actions)
        qs = self.q.setdefault(s, {})
        old = qs.get(a, 0.0)
        qs[a] = old + self.alpha * (r + self.gamma * future - old)
        return surprise

    def choose(self, s: str, actions: Sequence[str], epsilon: float,
               rng: random.Random) -> Tuple[str, str]:
        """-> (action, why). Untried actions carry optimistic value (curiosity)."""
        acts = list(actions)
        if rng.random() < epsilon:
            return rng.choice(acts), "explore"
        best = max(self.value(s, a) for a in acts)
        top = [a for a in acts if self.value(s, a) == best]
        untried = [a for a in top if not self.tried(s, a)]
        if untried:
            return untried[0], "curious"
        return top[0], "learned"

    def best_known(self, s: str) -> Optional[Tuple[str, float]]:
        qs = self.q.get(s)
        if not qs:
            return None
        a = max(qs, key=lambda k: qs[k])
        return a, qs[a]

    # ── persistence ──────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "domain": self.domain, "alpha": self.alpha,
                "gamma": self.gamma, "counts": self.counts, "q": self.q,
                "sessions": self.sessions}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransitionModel":
        m = cls(str(data.get("domain", "game")), float(data.get("alpha", 0.5)),
                float(data.get("gamma", 0.9)))
        m.counts = data.get("counts") or {}
        m.q = data.get("q") or {}
        m.sessions = list(data.get("sessions") or [])
        return m

    @staticmethod
    def path_for(domain: str, state_dir: Optional[Path] = None) -> Path:
        return (state_dir or _default_state_dir()) / f"{_slug(domain)}.json"

    @classmethod
    def load(cls, domain: str, state_dir: Optional[Path] = None) -> "TransitionModel":
        path = cls.path_for(domain, state_dir)
        try:
            with open(path, encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except FileNotFoundError:
            return cls(domain)
        except (OSError, ValueError) as exc:
            logger.warning("game model %s unreadable (%s); starting fresh", path, exc)
            return cls(domain)

    def save(self, state_dir: Optional[Path] = None) -> Path:
        path = self.path_for(self.domain, state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)
        os.replace(tmp, path)
        return path

    def progress(self) -> Dict[str, Any]:
        """Is the agent getting better at this game across sessions?"""
        rewards = [float(s.get("total_reward", 0.0)) for s in self.sessions]
        surprises = [s.get("mean_surprise") for s in self.sessions
                     if isinstance(s.get("mean_surprise"), (int, float))]
        half = max(1, len(rewards) // 2)
        improving = None
        if len(rewards) >= 2:
            early = sum(rewards[:half]) / half
            late = sum(rewards[-half:]) / half
            improving = late > early
        return {"domain": self.domain, "sessions": len(self.sessions),
                "reward_by_session": rewards,
                "surprise_by_session": surprises,
                "states_known": len(self.counts),
                "improving": improving}


@dataclass
class StepRecord:
    step: int
    state: str
    action: str
    why: str
    reward: float
    next_state: str
    surprise: float
    done: bool


@dataclass
class SessionReport:
    domain: str
    session: int
    steps: int = 0
    total_reward: float = 0.0
    mean_surprise: Optional[float] = None
    done: bool = False
    new_states: int = 0
    wm_recorded: int = 0
    wm_reason: str = ""
    persisted: bool = False
    memory_written: bool = False
    trace: List[StepRecord] = field(default_factory=list)

    def to_dict(self, with_trace: bool = False) -> Dict[str, Any]:
        d = asdict(self)
        if not with_trace:
            d.pop("trace", None)
        return d


def _run_coro(coro: Any) -> Any:
    """Run a coroutine from sync code, even when a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            box["e"] = exc

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _default_wm_observe() -> Optional[WMObserve]:
    try:
        from adk.packs.world_model.tools import wm_observe

        return wm_observe
    except Exception as exc:  # noqa: BLE001 -- the pack is optional here
        logger.debug("world-model pack unavailable: %s", exc)
        return None


class GameLearner:
    """Drives one agent through sessions of one game and keeps what it learns."""

    def __init__(self, client: GameClient, *, policy: Optional[Policy] = None,
                 memory: Any = None, persist: bool = True,
                 state_dir: Optional[Path] = None, epsilon: float = 0.1,
                 seed: Optional[int] = None,
                 wm_observe_fn: Optional[WMObserve] = None,
                 use_world_model: bool = True) -> None:
        if persist:
            from adk.home.entitlement import require

            require("game_learning")
        self.client = client
        self.policy = policy
        self.memory = memory
        self.persist = persist
        self.state_dir = state_dir
        self.epsilon = epsilon
        self.rng = random.Random(seed)
        self.wm_observe_fn = wm_observe_fn if wm_observe_fn is not None else (
            _default_wm_observe() if use_world_model else None)
        self.model: Optional[TransitionModel] = None

    def _load_model(self) -> TransitionModel:
        if self.persist:
            return TransitionModel.load(self.client.domain, self.state_dir)
        return TransitionModel(self.client.domain)

    def hints(self, obs: Observation) -> List[str]:
        """What the agent remembers that bears on this situation."""
        out: List[str] = []
        if self.model is not None:
            known = self.model.best_known(obs.signature)
            if known:
                out.append(f"last time here, '{known[0]}' was worth {known[1]:.2f}")
        return out

    def play_session(self, budget: int = 30) -> SessionReport:
        client = self.client
        obs = client.join() if not client.joined else client.observe()
        self.model = model = self._load_model()
        known_before = set(model.counts)
        report = SessionReport(domain=client.domain,
                               session=len(model.sessions) + 1)
        surprises: List[float] = []
        for i in range(int(budget)):
            acts = list(obs.actions)
            if obs.done or not acts:
                report.done = obs.done
                break
            action, why = None, ""
            if self.policy is not None:
                try:
                    picked = self.policy(obs, acts, self.hints(obs))
                except Exception as exc:  # noqa: BLE001 -- a bad model never stops play
                    logger.warning("policy failed (%s); using learned values", exc)
                    picked = None
                if picked in acts:
                    action, why = picked, "policy"
            if action is None:
                action, why = model.choose(obs.signature, acts, self.epsilon, self.rng)
            step = client.act(action)
            nxt = step.observation
            s = model.update(obs.signature, action, step.reward, nxt.signature,
                             step.done, nxt.actions)
            surprises.append(s)
            if self.wm_observe_fn is not None:
                try:
                    res = self.wm_observe_fn(obs.signature, action, nxt.signature,
                                             reward=step.reward, done=step.done,
                                             domain=client.domain)
                    if res.get("ok"):
                        report.wm_recorded += 1
                    else:
                        report.wm_reason = str(res.get("reason", ""))[:200]
                except Exception as exc:  # noqa: BLE001 -- fail-soft by contract
                    report.wm_reason = f"{type(exc).__name__}: {exc}"[:200]
            report.trace.append(StepRecord(i + 1, obs.signature, action, why,
                                           float(step.reward), nxt.signature,
                                           s, bool(step.done)))
            report.steps += 1
            report.total_reward += float(step.reward)
            obs = nxt
            if step.done:
                report.done = True
                break
        report.total_reward = round(report.total_reward, 4)
        report.mean_surprise = (round(sum(surprises) / len(surprises), 4)
                                if surprises else None)
        report.new_states = len(set(model.counts) - known_before)
        model.sessions.append({"at": time.time(), "steps": report.steps,
                               "total_reward": report.total_reward,
                               "mean_surprise": report.mean_surprise,
                               "done": report.done})
        if self.persist:
            model.save(self.state_dir)
            report.persisted = True
        if self.memory is not None:
            report.memory_written = self._remember(report, model)
        return report

    def _remember(self, report: SessionReport, model: TransitionModel) -> bool:
        lessons = []
        for s in list(model.q)[:20]:
            best = model.best_known(s)
            if best and best[1] > 0:
                lessons.append(f"in {s}: {best[0]} ({best[1]:.2f})")
        summary = (f"session {report.session} of {report.domain}: "
                   f"{report.steps} steps, reward {report.total_reward}, "
                   f"surprise {report.mean_surprise}, finished={report.done}. "
                   f"Lessons: {'; '.join(lessons) or 'none yet'}")
        try:
            _run_coro(self.memory.remember(
                f"{report.domain}:session:{report.session}", summary,
                category="game",
                metadata={"domain": report.domain, "reward": report.total_reward}))
            return True
        except Exception as exc:  # noqa: BLE001 -- memory is best-effort
            logger.warning("could not write game memory: %s", exc)
            return False

    def play(self, sessions: int = 1, budget: int = 30) -> List[SessionReport]:
        out = []
        for _ in range(int(sessions)):
            out.append(self.play_session(budget))
            self.client.leave()
        return out


def llm_policy(llm: Any, system_prompt: str = "") -> Policy:
    """A policy that asks the agent's own model to pick one legal action."""
    from adk.llm.base import Message

    def _pick(obs: Observation, actions: List[str], hints: List[str]) -> Optional[str]:
        numbered = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(actions))
        user = (f"{obs.text}\n\nWhat you remember: {'; '.join(hints) or 'nothing yet'}"
                f"\n\nChoose exactly one action by number:\n{numbered}\n"
                "Answer with the number only.")
        msgs = [Message(role="system", content=system_prompt or
                        "You are an agent playing a game. Pick good actions."),
                Message(role="user", content=user)]
        resp = _run_coro(llm.chat(msgs))
        return match_action(getattr(resp, "content", "") or "", actions)

    return _pick


def match_action(reply: str, actions: Sequence[str]) -> Optional[str]:
    """Map a model reply ("2", "2. go left", "go left") onto a legal action."""
    text = reply.strip().lower()
    m = re.match(r"\D*(\d+)", text)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(actions):
            return actions[idx]
    for a in sorted(actions, key=len, reverse=True):
        if a.lower() in text:
            return a
    return None


def game_memory(name: str = "agent-home", db_path: Optional[Path] = None,
                local_only: bool = True) -> Any:
    """An adk Memory for game lessons; local-only unless told otherwise."""
    from adk.memory import Memory

    if db_path is None:
        from adk.home.config import home_dir

        db_path = home_dir() / "memory" / f"{_slug(name)}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    mem = Memory(db_path=db_path, agent_name=name)
    if local_only:
        mem._spirit_enabled = False
        mem._fleet_enabled = False
    return mem


def enroll_game(url: str, token: Optional[str] = None, episodes: int = 5,
                budget: int = 20, name: Optional[str] = None,
                **client_kwargs: Any) -> Dict[str, Any]:
    """Run the world-model pack's ``env_enroll`` on a game room (premium)."""
    from adk.home.entitlement import require
    from adk.packs.world_model.env_enroll import env_enroll

    require("game_learning")
    kwargs: Dict[str, Any] = {"url": url, **client_kwargs}
    if token:
        kwargs["token"] = token
    return env_enroll("adk.games.env_adapter:GameEnvAdapter", kwargs,
                      episodes=episodes, budget=budget, name=name)
