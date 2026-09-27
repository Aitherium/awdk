"""Episode capture: every ``solve()`` run as training data, with provenance.

``LoopConfig(harvest="<path>.jsonl")`` appends ONE record per run to that file:
the context the model saw and the decision it made on every call, the outcome,
the hypotheses the loop verified, and where all of it came from. The record is
the unit a local LoRA / RL trainer consumes (rejection-sampled SFT keeps the
reward-1.0 episodes; RL reads the reward).

Two rules are enforced here, not by the caller:

* **served == requested, on every call.** :class:`HarvestingBackend` compares
  each reply's ``ModelResponse.model`` with the model the backend was asked for.
  A different non-empty name raises :class:`ModelMismatch` -- FATAL in the core,
  so the episode stops calling the model instead of measuring the wrong one. A
  reply that names no model is booked ``unverified``.
* **only verified episodes train.** ``trainable`` is True only when every call
  was answered by the requested model (no mismatch, none unverified) and at least
  one call was made; :func:`episode_to_examples` exports nothing else.

:func:`episode_to_examples` turns a record into harvest-example
dicts (``messages`` + ``metadata`` + ``content_hash`` + ``quality_score``), which is
also the ``{"messages": [...]}`` chat JSONL common SFT trainers read -- so capture
needs no running service.

Stdlib only. 3.10-compatible.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from ._vendor.interfaces import ModelMismatch

__all__ = [
    "SCHEMA",
    "HarvestingBackend",
    "build_episode",
    "write_episode",
    "read_episodes",
    "episode_to_examples",
    "export_examples",
]

#: Version tag of one episode record.
SCHEMA = "aither.solve.episode/1"

#: ``source`` of the examples exported from this module.
SOURCE = "solve_episode"


def _msg(m: Any) -> Dict[str, str]:
    if isinstance(m, dict):
        return {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
    return {"role": str(getattr(m, "role", "user")), "content": str(getattr(m, "content", ""))}


class HarvestingBackend:
    """A ``ModelBackend`` proxy that records every call and asserts the served model.

    ``expected_model`` defaults to the wrapped backend's ``model``. Every attribute
    other than ``generate`` is the wrapped backend's.
    """

    def __init__(self, backend: Any, *, expected_model: Optional[str] = None) -> None:
        self._backend = backend
        self.expected_model = str(expected_model or getattr(backend, "model", "") or "")
        self.calls: List[Dict[str, Any]] = []
        self.mismatches = 0
        self.unverified = 0
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    async def generate(self, messages: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        resp = await self._backend.generate(messages, **kw)
        served = str(getattr(resp, "model", "") or "")
        text = getattr(resp, "text", None)
        if text is None:
            text = getattr(resp, "content", "") or ""
        rec = {
            "i": len(self.calls),
            "messages": [_msg(m) for m in messages],
            "reply": str(text),
            "served_model": served,
            "usage": dict(getattr(resp, "usage", None) or {}),
            "latency_s": round(time.perf_counter() - t0, 3),
            "verified": bool(served) and served == self.expected_model,
        }
        with self._lock:
            self.calls.append(rec)
            if not served:
                self.unverified += 1
            elif served != self.expected_model:
                self.mismatches += 1
        if served and served != self.expected_model:
            raise ModelMismatch(
                "MODEL MISMATCH: requested %r but the reply was served by %r"
                % (self.expected_model, served)
            )
        return resp


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _config(cfg: Any) -> Dict[str, Any]:
    if cfg is None:
        return {}
    try:
        out = dataclasses.asdict(cfg)
    except TypeError:
        return {}
    out.pop("invariants", None)  # host objects; the context gate reports its own summary
    return _jsonable(out)


def _host_of(backend: Any) -> str:
    for attr in ("base_url", "url", "endpoint"):
        v = getattr(backend, attr, None)
        if v:
            return str(v)
    return ""


def build_episode(
    recorder: HarvestingBackend,
    result: Any,
    *,
    episode_id: str,
    env: Any = None,
    goal: str = "",
    config: Any = None,
    reward: Optional[float] = None,
) -> Dict[str, Any]:
    """One JSON-able episode record from a finished run (``result`` is a SolveResult)."""
    from ._provenance import H30_SHA

    served: Dict[str, int] = {}
    for c in recorder.calls:
        served[c["served_model"] or "?"] = served.get(c["served_model"] or "?", 0) + 1
    won = bool(getattr(result, "won", False))
    if reward is None:
        reward = 1.0 if won else 0.0
    hyps = []
    for h in getattr(result, "hypotheses", None) or []:
        if getattr(h, "status", "") == "active":
            hyps.append({"id": h.id, "name": h.name, "kind": h.kind, "source": h.source,
                         "support": h.support})
    try:
        from adk import __version__ as adk_version
    except Exception:  # noqa: BLE001 - provenance never fails a run
        adk_version = ""
    raw_backend = getattr(recorder, "_backend", None)
    trainable = bool(recorder.calls) and recorder.mismatches == 0 and recorder.unverified == 0
    return {
        "schema": SCHEMA,
        "episode_id": episode_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "requested_model": recorder.expected_model,
            "served_models": served,
            "mismatches": recorder.mismatches,
            "unverified": recorder.unverified,
            "backend": type(raw_backend).__name__ if raw_backend is not None else "",
            "backend_url": _host_of(raw_backend),
            "local_only": getattr(raw_backend, "local_only", None),
            "adk_version": str(adk_version),
            "core_sha": H30_SHA,
            "env": type(getattr(env, "_env", env)).__name__ if env is not None else "",
            "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16],
            "config": _config(config),
        },
        "goal": goal,
        "calls": list(recorder.calls),
        "outcome": {
            "finish_reason": getattr(result, "finish_reason", None),
            "won": won,
            "levels": getattr(result, "levels", 0),
            "actions": getattr(result, "actions", 0),
            "turns": getattr(result, "turns", 0),
            "llm_calls": getattr(result, "llm_calls", 0),
            "calibration": getattr(result, "calibration", None),
            "strategy_trace": list(getattr(result, "strategy_trace", None) or []),
            "wall_s": getattr(result, "wall_s", None),
        },
        "reward": float(reward),
        "verified_hypotheses": hyps,
        "trainable": trainable,
    }


_WRITE_LOCK = threading.Lock()


def write_episode(path: str, episode: Dict[str, Any]) -> None:
    """Append one record (UTF-8, ``\\n`` line endings on every platform)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    line = json.dumps(episode, ensure_ascii=False, default=str) + "\n"
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)


def read_episodes(path: str) -> List[Dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def episode_to_examples(
    episode: Dict[str, Any], *, min_reward: float = 1.0, source_file: str = ""
) -> List[Dict[str, Any]]:
    """Harvest-example dicts, one per model call, from a TRAINABLE
    episode whose reward is at least ``min_reward`` (rejection sampling: the
    usual recipe keeps reward-1.0 trajectories). Anything else -> ``[]``."""
    if not episode.get("trainable") or float(episode.get("reward", 0.0)) < float(min_reward):
        return []
    prov = episode.get("provenance", {})
    out: List[Dict[str, Any]] = []
    for c in episode.get("calls", []):
        if not c.get("verified"):
            return []  # one unverified call poisons the episode
        messages = list(c["messages"]) + [{"role": "assistant", "content": c["reply"]}]
        content_hash = hashlib.md5(
            json.dumps(messages, sort_keys=True).encode("utf-8")
        ).hexdigest()
        out.append({
            "id": "%s-%d" % (episode["episode_id"], c["i"]),
            "source": SOURCE,
            "source_file": source_file,
            "data_type": "reasoning",
            "messages": messages,
            "metadata": {
                "episode_id": episode["episode_id"],
                "call": c["i"],
                "reward": episode["reward"],
                "model": c["served_model"],
                "core_sha": prov.get("core_sha"),
                "env": prov.get("env"),
                "finish_reason": episode.get("outcome", {}).get("finish_reason"),
            },
            "quality_score": float(episode["reward"]),
            "source_timestamp": episode.get("created"),
            "content_hash": content_hash,
            "target_model": prov.get("requested_model"),
        })
    return out


def export_examples(paths: Iterable[str], out_path: str, *, min_reward: float = 1.0) -> int:
    """Write every exportable example from episode files to ``out_path``; returns the count."""
    n = 0
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for p in paths:
            for ep in read_episodes(p):
                for ex in episode_to_examples(ep, min_reward=min_reward, source_file=p):
                    fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
                    n += 1
    return n
