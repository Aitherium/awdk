"""A deliberately simple LLM policy for the ARC-AGI-3 suite.

It is NOT the reasoning loop. It exists to prove the model path end to end -- a
real model, real tokens, a scored row -- with the smallest amount of machinery:
each call shows the model the environment's ``render`` text and asks for up to
``actions_per_call`` actions, one per line (``A3`` or ``A6 12 40``). Unparseable
replies fall back to one seeded-random available action, so a chatty model still
advances the episode and the row still measures something.

Any object with a blocking ``chat(messages, max_tokens=, temperature=)`` that
returns ``.content`` works as the model (for example
:class:`adk.core.backends.microscheduler.MicroSchedulerBackend`). An exception the
model raises with ``backend_dead = True`` is NOT caught: the suite records the row
as backend-dead and exits 2 when every row is.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional

from adk.reasoning.solve._types import Action, Environment

from .suite import CapReachedError, EpisodeContext, EpisodeFn

_log = logging.getLogger(__name__)

__all__ = ["llm_policy", "parse_actions"]

_ACTION_RE = re.compile(
    r"\bA(?:CTION)?\s*([0-7])(?:\s*[ ,(]\s*(\d{1,2})\s*[ ,]\s*(\d{1,2}))?", re.I
)

SYSTEM = (
    "You are playing an unknown grid game. Discover the rules by experiment and "
    "clear levels in as few actions as possible.\n%s\n"
    "Reply with up to %d actions, one per line: A<id> (e.g. A3), or A6 <x> <y> for a "
    "click at column x, row y. Nothing else is required."
)


def parse_actions(text: str, available: List[int], limit: int) -> List[Action]:
    """Actions named in ``text`` that are available, in order, at most ``limit``."""
    out: List[Action] = []
    for m in _ACTION_RE.finditer(text or ""):
        aid = int(m.group(1))
        if available and aid not in available:
            continue
        if aid == 6:
            if m.group(2) is None:
                continue
            out.append((6, min(63, int(m.group(2))), min(63, int(m.group(3)))))
        else:
            out.append((aid, -1, -1))
        if len(out) >= limit:
            break
    return out


def llm_policy(
    model: Any,
    *,
    actions_per_call: int = 5,
    max_tokens: int = 400,
    temperature: float = 0.3,
    name: str = "llm",
) -> EpisodeFn:
    """An episode function that asks ``model`` for actions every few steps."""

    def episode(env: Environment, ctx: EpisodeContext) -> Mapping[str, Any]:
        before = _counters(model)
        primer = _hook(env, "primer") or ""
        system = SYSTEM % (primer, actions_per_call)
        obs = env.observe()
        last: Optional[Any] = None
        parsed = fallback = 0
        try:
            while not env.done():
                avail = [a for a in env.available_actions() if a != 0] or [1]
                situation = _hook(env, "render", obs, last) or (
                    "level %d, available actions %s" % (obs.level, avail)
                )
                reply = model.chat(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": "%s\nAvailable: %s" % (situation, avail)},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                acts = parse_actions(getattr(reply, "content", ""), avail, actions_per_call)
                if acts:
                    parsed += 1
                else:
                    fallback += 1
                    a = ctx.rng.choice(avail)
                    acts = [
                        (a, ctx.rng.randint(0, 63), ctx.rng.randint(0, 63))
                        if a == 6
                        else (a, -1, -1)
                    ]
                for a in acts:
                    if env.done():
                        break
                    prev = obs
                    obs = env.act(a, source="model")
                    last = (prev.state, obs.state)
                    if obs.level_up or obs.died:
                        break
        except CapReachedError:
            _log.debug("model-call cap reached; the episode ends here", exc_info=True)
        after = _counters(model)
        stats: Dict[str, Any] = {k: after[k] - before[k] for k in after}
        stats.update(
            model=getattr(model, "model", None),
            replies_parsed=parsed,
            replies_fallback=fallback,
        )
        return stats

    episode.__name__ = name
    return episode


def _hook(env: Any, name: str, *args: Any) -> Any:
    fn = getattr(env, name, None)
    return fn(*args) if callable(fn) else None


def _counters(model: Any) -> Dict[str, Any]:
    return {
        "llm_calls": int(getattr(model, "calls", 0) or 0),
        "llm_s": float(getattr(model, "llm_s", 0.0) or 0.0),
        "prompt_tokens": int(getattr(model, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(model, "completion_tokens", 0) or 0),
    }
