"""The decision door on PRISM's strategy pick (optional, ``LoopConfig.door``).

PRISM picks the next strategy with a fixed scorer (``Prism.best``: best
(levels + verified) per action, ties by registry order). With the door on, the
same pick is asked of the decision door (``adk.choose.decide`` -> the world
model's ``POST /decide``, or the in-process ``awdecide`` backend) and the
strategy's window is reported back (``adk.choose.outcome`` ->
``/decide/outcome``) so the door learns which strategy pays in which situation.

Installed WITHOUT editing the vendored core (it is hash-pinned): the hook sits on
the ``Prism`` INSTANCE -- ``prism.best`` (which the core's own ``rotate`` and the
hand-back path call through ``self``), ``prism.record`` and ``prism.save``.

* options: the candidates ``best()`` would consider, narrowed to those
  :func:`adk.reasoning.solve.context.permits` allows (``ARC_POLICY`` + the
  host's invariants). The door never sees a refused strategy.
* outcome: every ``record()`` for the chosen strategy accumulates into its
  window; the window closes when another strategy is recorded, another door
  decision opens, or PRISM saves (run end). Reward = (levels + new verified
  hypotheses) per action, scaled x10 and capped at 1; a window that spent
  actions and gained nothing is -0.25.
* fallback: the door down, slow, unsure (``source == "none"``) or answering off
  the option list -> the current scorer over the permitted candidates. After the
  first transport failure the door is not asked again this run (deterministic).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ._context_gate import ARC_POLICY
from .context import Context, Invariants, Request, permits

_LOG = logging.getLogger(__name__)

__all__ = ["PrismDoor", "install_door", "window_reward", "DEFAULT_FORK"]

DEFAULT_FORK = "solve.prism"


def window_reward(actions: int, levels: int, verified: int) -> float:
    """The door's reward for one strategy window, in -1..1."""
    gained = int(levels) + int(verified)
    if gained <= 0:
        return -0.25 if int(actions) > 0 else 0.0
    return min(1.0, 10.0 * gained / max(1, int(actions)))


class PrismDoor:
    """One per loop. ``install()`` wraps the loop's ``Prism`` instance."""

    def __init__(
        self,
        loop: Any,
        *,
        decide: Optional[Callable[..., Any]] = None,
        outcome: Optional[Callable[..., Any]] = None,
        fork: str = DEFAULT_FORK,
        invariants: Optional[Invariants] = None,
        timeout_s: float = 8.0,
    ) -> None:
        if decide is None or outcome is None:
            from adk import choose

            decide = decide or choose.decide
            outcome = outcome or choose.outcome
        self.loop = loop
        self.prism = loop.prism
        self._decide = decide
        self._outcome = outcome
        self.fork = fork
        self.invariants = invariants
        self.timeout_s = float(timeout_s)
        self.ctx = Context("%s/door" % getattr(loop, "episode_id", "episode"))
        self.down: Optional[str] = None
        self.window: Optional[Dict[str, Any]] = None
        self._orig_best: Callable[..., Optional[str]] = self.prism.best
        self._orig_record: Callable[..., None] = self.prism.record
        self._orig_save: Callable[[], None] = self.prism.save
        self.stats: Dict[str, Any] = {
            "decisions": 0,
            "fallbacks": 0,
            "fallback_why": {},
            "sources": {},
            "outcomes": 0,
            "outcome_errors": 0,
            "refused_options": {},
            "log": [],
        }

    # ------------------------------------------------------------ permission
    def permitted(self, sid: str) -> bool:
        self.ctx.domain["previous_program"] = bool(getattr(self.loop, "level_programs", {}))
        d = permits(
            self.ctx, Request("strategy", sid), invariants=self.invariants, policy=ARC_POLICY
        )
        if not d.allowed:
            self.stats["refused_options"][sid] = self.stats["refused_options"].get(sid, 0) + 1
        return bool(d.allowed)

    def candidates(self, initial: bool, exclude: Optional[List[str]]) -> List[str]:
        """Exactly the candidate set ``Prism.best`` considers."""
        from ._vendor.prism import BY_ID

        cands = [sid for sid in self.prism.allowed if sid not in (exclude or [])]
        if initial:
            cands = [sid for sid in cands if BY_ID[sid].kind == "llm"] or cands
        return cands

    def state(self, initial: bool, exclude: Optional[List[str]]) -> str:
        """A STABLE descriptor of the situation at this fork (same situation -> same string)."""
        p = self.prism
        gaps = sorted(p.diagnoses[-1].gaps) if p.diagnoses else []
        env = ""
        hooks = getattr(self.loop, "hooks", None)
        for attr in ("game_id", "name"):
            v = getattr(hooks, attr, None)
            if isinstance(v, str) and v:
                env = v
                break
        if not env and hooks is not None:
            env = type(getattr(hooks, "_env", hooks)).__name__
        return "env:%s|phase:%s|stalled:%s|gaps:%s|prev_program:%d|exhausted:%s" % (
            env,
            "initial" if initial else "rotate",
            p.active.id,
            ",".join(gaps) or "-",
            int(bool(getattr(self.loop, "level_programs", {}))),
            ",".join(sorted(set(exclude or []))) or "-",
        )

    # ------------------------------------------------------------ the pick
    def best(self, initial: bool = False, exclude: Optional[List[str]] = None) -> Optional[str]:
        cands = self.candidates(initial, exclude)
        if not cands:
            return None
        options = [sid for sid in cands if self.permitted(sid)]
        if not options:  # nothing permitted: the scorer's answer, the gate still enforces
            return self._fallback("no_permitted_option", initial, exclude, cands)
        if len(options) == 1:
            return self._open(options[0], None, "only_option")
        if self.down is not None:
            return self._fallback("door_down", initial, exclude, cands, options)
        state = self.state(initial, exclude)
        try:
            d = self._decide(self.fork, state, options=options, timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - any door failure is a fallback, never a crash
            self.down = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            _LOG.info("decision door down, PRISM falls back to its scorer: %s", self.down)
            return self._fallback("door_down", initial, exclude, cands, options)
        source = str(getattr(d, "source", "none"))
        self.stats["sources"][source] = self.stats["sources"].get(source, 0) + 1
        answer = str(getattr(d, "answer", ""))
        if source.endswith("none") or answer not in options:
            return self._fallback(
                "door_unsure" if answer in options else "door_off_list",
                initial,
                exclude,
                cands,
                options,
            )
        self.stats["decisions"] += 1
        self.stats["log"].append(
            {
                "state": state,
                "options": options,
                "answer": answer,
                "source": source,
                "confidence": float(getattr(d, "confidence", 0.0) or 0.0),
            }
        )
        del self.stats["log"][:-24]
        return self._open(answer, str(getattr(d, "decision_id", "") or "") or None, source)

    def _fallback(
        self,
        why: str,
        initial: bool,
        exclude: Optional[List[str]],
        cands: List[str],
        options: Optional[List[str]] = None,
    ) -> Optional[str]:
        self.stats["fallbacks"] += 1
        self.stats["fallback_why"][why] = self.stats["fallback_why"].get(why, 0) + 1
        if options:
            refused = [sid for sid in cands if sid not in options]
            pick = self._orig_best(initial=initial, exclude=list(exclude or []) + refused)
        else:
            pick = self._orig_best(initial=initial, exclude=exclude)
        return self._open(pick, None, "scorer") if pick is not None else None

    def _open(self, sid: str, decision_id: Optional[str], source: str) -> str:
        """A pick opens a window; the previous window closes (reported if it acted)."""
        w = self.window
        if w is not None and w["sid"] == sid and decision_id is None and w["decision_id"] is None:
            return sid  # the scorer re-picked the running strategy: same window
        self.close()
        self.window = {
            "sid": sid,
            "decision_id": decision_id,
            "source": source,
            "actions": 0,
            "levels": 0,
            "verified": 0,
            "records": 0,
            "level0": int(getattr(self.loop, "level", 0) or 0),
            "actions0": int(getattr(self.loop, "actions", 0) or 0),
        }
        return sid

    # ------------------------------------------------------------ the outcome
    def record(self, sid: str, actions: int, levels: int, verified: int) -> None:
        self._orig_record(sid, actions, levels, verified)
        w = self.window
        if w is None or w["sid"] != sid:
            self.close()  # another strategy is acting: the chosen one's window is over
            return
        w["actions"] += int(actions)
        w["levels"] += int(levels)
        w["verified"] += int(verified)
        w["records"] += 1

    def close(self, at_end: bool = False) -> None:
        w, self.window = self.window, None
        if w is None or not w["decision_id"]:
            return  # a scorer pick: nothing to teach
        if at_end:
            # the core skips record() when the game ends inside a turn (GameStopped), so
            # the winning window would go unreported: read it off the loop's counters
            lv = int(getattr(self.loop, "level", 0) or 0) - w["level0"]
            acts = int(getattr(self.loop, "actions", 0) or 0) - w["actions0"]
            if lv > w["levels"] or acts > w["actions"]:
                w["levels"] = max(w["levels"], lv)
                w["actions"] = max(w["actions"], acts)
                w["records"] += 1
        if not w["records"]:
            return  # a pick never acted on: nothing to teach
        reward = window_reward(w["actions"], w["levels"], w["verified"])
        try:
            self._outcome(w["decision_id"], reward)
            self.stats["outcomes"] += 1
        except Exception as exc:  # noqa: BLE001 - a lost outcome never costs the run
            self.stats["outcome_errors"] += 1
            _LOG.info("decision door outcome not recorded: %s", exc)
        self.stats["log"].append(
            {
                "outcome": w["sid"],
                "decision_id": w["decision_id"],
                "reward": round(reward, 4),
                "actions": w["actions"],
                "levels": w["levels"],
                "verified": w["verified"],
            }
        )
        del self.stats["log"][:-24]

    def save(self) -> None:
        self.close(at_end=True)
        self._orig_save()

    # ------------------------------------------------------------ install
    def install(self) -> "PrismDoor":
        from ._vendor.prism import BY_ID

        p = self.prism
        p.best = self.best
        p.record = self.record
        p.save = self.save
        first = self.best(initial=True)  # the game's first strategy is a door pick too
        if first is not None:
            p.active = BY_ID[first]
        self.loop.prism_door = self
        return self

    def summary(self) -> Dict[str, Any]:
        return {"fork": self.fork, "down": self.down, **self.stats}


def install_door(loop: Any, **kwargs: Any) -> Optional[PrismDoor]:
    """Install the door on ``loop.prism``; ``loop.summary()`` gains ``door``.
    A loop without PRISM gets nothing (returns ``None``)."""
    if getattr(loop, "prism", None) is None:
        return None
    door = PrismDoor(loop, **kwargs).install()
    summary = loop.summary

    def summary_with_door() -> Dict[str, Any]:
        s = summary()
        s["door"] = door.summary()
        return s

    loop.summary = summary_with_door
    return door
