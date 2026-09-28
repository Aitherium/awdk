"""World models on awm: a backend for the agent loop, and an agent that plans with one.

Two consumers of the same dynamics table (awm schema v3, ``awm/world.py``):

``AwmWorldModelBackend``
    Satisfies :class:`adk.worldmodel.WorldModelBackend`. ``record`` becomes
    ``observe_transition`` on a synthetic, bounded state (the loop's 8-dim vector,
    quantized); ``advise`` ranks tool candidates by the TABULAR success rate the
    table observed for this state (RECALLED) or, failing that, for the action over
    every state (GENERALIZED), and abstains (None) when nothing is known. Selected with
    ``AITHER_AGENT_WM_BACKEND=awm``; the default stays the builtin. Action text is
    persisted through ``adk.worldmodel.redact_action`` -- the same rule the federation
    export uses -- so a tool name outside ``AITHER_AGENT_WM_ALLOWED_ACTIONS`` lands on
    disk as ``redacted:<hmac12>``, keyed by a per-install secret. An awm file older
    than v3 opens in COMPAT mode and is
    NEVER migrated from here: the backend degrades and ``stats()["degraded"]`` carries
    the NeedsMigration message naming the command.

``WorldModelAgent``
    understand -> predict -> plan -> step, over any environment adapter shaped like
    awpredict's ``EnvironmentAdapter`` (``domain``, ``observe``, ``actions``,
    ``step``). The adapter's observation is mirrored into awm slots under one prefix
    (through ``reconcile_and_remember``, so an unchanged slot writes nothing), every
    executed step is an awm transition scored for surprise, and predictions keep
    their provenance, never merged:

        RECALLED     exact (state, action) seen before -- the table's own answer
        GENERALIZED  the action's outcome over all states, when it was seen in
                     at least GENERALIZE_MIN_SUPPORT DISTINCT states and every
                     observation AGREES (state-independent), confidence shrunk by
                     n/(n+1); a disagreeing marginal, or one seen in a single state
                     however often, is state-blind and ranks below a learned model
        PREDICTED    a learner (adk/world_adapters.py) answered a table miss
        NONE         nothing to go on; never a guess

    ``plan`` is receding-horizon MPC: exact beam search over imagined states for a
    small action set, CEM with elites for a large one (a seeded ``random.Random``,
    never the global generator). ``step`` executes ONLY the first action.

Every optional plane (awm, awgraph, a learner) is imported GUARDED; a missing one is
logged once and listed in ``TELEMETRY["degraded"]``.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import os
import random
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from adk.worldmodel import (
    AWM_CKPT_SUFFIX,
    DEFAULT_GOAL,
    STATE_DIMS,
    TRAINED_MIN,
    WARM_MIN,
    BuiltinWorldModel,
    allowed_actions_from_env,
    bounded_vector,
    redact_action,
    redaction_salt,
    register_world_model,
    wm_agent_id,
    wm_root,
)

logger = logging.getLogger("adk.world")

# Provenance labels. Equal to awm.world's constants; restated so this module imports
# without awm (and then reports why nothing works).
RECALLED = "RECALLED"
GENERALIZED = "GENERALIZED"
PREDICTED = "PREDICTED"
NONE = "NONE"

# Plan verdicts.
OK = "OK"
NO_MODEL = "NO_MODEL"
UNREACHABLE = "UNREACHABLE"
#: No path found by a search that did NOT cover every modelled state within the horizon
#: (the beam dropped states past ``beam_width``; CEM only samples): not a proof that
#: the goal is unreachable. Widen the beam, raise the samples, or explore.
INCOMPLETE = "INCOMPLETE"
#: A plan whose weakest step is below this confidence is flagged ``speculative``.
SPECULATIVE_BELOW = 0.6
# Run-only verdicts.
BUDGET = "BUDGET"
DONE = "DONE"

#: Fewest observations of an action (over all states) before its marginal outcome may
#: be read as GENERALIZED ahead of a learner, or ranked by ``advise``. One observation
#: says what the action did ONCE, in one state: it is not a generalization. For the
#: world model's GENERALIZED-ahead-of-a-learner gate this counts DISTINCT before-states:
#: ten agreeing observations in one state say nothing about any other state.
GENERALIZE_MIN_SUPPORT = 2
#: The score ``advise`` gives a tool it knows nothing (or too little) about: it ranks
#: below a tool that mostly succeeds and ABOVE a tool that mostly fails.
UNKNOWN_PRIOR = 0.5


#: The most a STATE-BLIND marginal may claim (seen in too few distinct states, or its
#: outcomes disagree across states): below SPECULATIVE_BELOW, so a plan resting on it
#: is flagged speculative and a failed search through it is never called UNREACHABLE.
#: Repeating an action in ONE state is that state's outcome, not evidence it holds
#: elsewhere -- ten same-state observations must not read as 0.909.
STATE_BLIND_CAP = 0.5


#: agent.py records a failed call under ``f"{tc.name}[denied]"`` / ``[blocked]`` /
#: ``[circuit_break]`` (and older callers ``"name [error]"``), ok=False, but asks
#: ``advise`` about the bare ``tc.name``. One trailing bracketed marker is split off.
_FAILURE_MARKER = re.compile(r"^(?P<base>.*?\S)\s*\[(?P<marker>[^\[\]]*)\]\s*$")


def split_failure_marker(action: Any) -> Tuple[Any, bool]:
    """``("flaky[denied]") -> ("flaky", True)``; anything else is returned unchanged.

    The tool that failed is the TOOL, not a second action named after its failure:
    recorded under the bracketed name, a failure never reached the bare name's
    success rate and ``advise`` scored a tool failing 9 times in 10 at 1.0.
    """
    if not isinstance(action, str):
        return action, False
    m = _FAILURE_MARKER.match(action)
    if m is None:
        return action, False
    return m.group("base").strip(), True


def _shrunk(agreement: float, n: int) -> float:
    """``agreement * n / (n + 1)``: n observations are never certainty."""
    return float(agreement) * n / (n + 1.0) if n > 0 else 0.0


#: Degraded planes and counters, process-wide. Read by tests and `stats()`.
TELEMETRY: Dict[str, Any] = {"degraded": [], "counters": {}}


def _degrade(reason: str) -> None:
    if reason not in TELEMETRY["degraded"]:
        TELEMETRY["degraded"].append(reason)
        logger.warning("[WORLD] plane degraded: %s", reason)


def _count(name: str, n: int = 1) -> None:
    TELEMETRY["counters"][name] = TELEMETRY["counters"].get(name, 0) + n


def _awm() -> Any:
    """``(awm, awm.world)`` or None, with the reason recorded. Guarded: awm is a sibling."""
    try:
        import awm  # type: ignore[import-not-found]
        from awm import world as awm_world  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 -- reported through telemetry
        _degrade(f"awm:{type(exc).__name__}: {exc}")
        return None
    if not hasattr(awm_world, "observe_transition"):
        _degrade(f"awm:{getattr(awm, '__version__', '?')} has no world model (needs >= 0.5)")
        return None
    return awm, awm_world


def _canon(delta: Dict[str, Optional[str]]) -> str:
    # The same canonical form awm.world uses to compare outcomes.
    return json.dumps(delta, sort_keys=True, ensure_ascii=False)


def _satisfies(slots: Dict[str, str], goal: Dict[str, Optional[str]]) -> bool:
    return all(slots.get(k) == v for k, v in goal.items())


def _mismatch(slots: Dict[str, str], goal: Dict[str, Optional[str]]) -> int:
    return sum(1 for k, v in goal.items() if slots.get(k) != v)


class _MonotonicClock:
    """A store clock that never repeats an instant.

    ``as_of`` reconstruction and ``unexplained_changes`` both rely on a step's
    before-state, its slot writes and its after-state having ORDERED timestamps; two
    writes stamped with one clock reading make "before" and "after" the same instant.
    Wraps whatever clock the store had (tests pin one), bumping by one ulp on a tie.
    """

    def __init__(self, base: Callable[[], float]):
        self.base = base
        self.last = -math.inf

    def __call__(self) -> float:
        t = float(self.base())
        if t <= self.last:
            t = math.nextafter(self.last, math.inf)
        self.last = t
        return t


def _install_monotonic_clock(store: Any) -> None:
    clock = getattr(store, "_clock", None)
    if clock is not None and not isinstance(clock, _MonotonicClock):
        store._clock = _MonotonicClock(clock)


# ============================================================================
# W1 -- AwmWorldModelBackend
# ============================================================================

#: Slot prefix of the backend's synthetic states.
BACKEND_PREFIX = "wm"
#: Quantization of each state dimension: 0.1 steps in [0, 1] -> 11 values.
BACKEND_BUCKETS = 10
#: The outcome slot every recorded after-state carries ("1" ok, "0" failed).
OK_SLOT = BACKEND_PREFIX + ".ok"
#: Memory kind of the digest -> slots rows the backend keeps for offline training.
STATE_KIND = "wm_state"
#: The segment the bookkeeping scope adds (see ``states_scope_text``).
STATES_SEGMENT = "wm-states"


def states_scope_text(scope_text: str) -> str:
    """Where the backend keeps its digest -> slots rows: a scope NOT visible from its own.

    Bookkeeping is not world state. Written as memories AT the transition scope under
    the ``wm`` prefix, every row read as a slot change nothing explained (awm's
    ``world stats`` counted 120 unexplained changes against 50 transitions) and
    ``encode_state`` / ``awm_predict`` digested them into the agent's state. A sibling
    project (or a descendant, under a wildcard) is never among ``visible_scopes`` of
    the transition scope, so awm's world tools there never see these rows.

    ``t:u:p -> t:u:p.wm-states``; ``t:u:* -> t:u:wm-states``; ``t:*:* -> t:wm-states:*``.
    """
    bits = scope_text.split(":")
    if len(bits) != 3:
        return scope_text  # the Scope parse refuses it later, loudly
    t, u, proj = bits
    if proj != "*":
        return f"{t}:{u}:{proj}.{STATES_SEGMENT}"
    if u != "*":
        return f"{t}:{u}:{STATES_SEGMENT}"
    return f"{t}:{STATES_SEGMENT}:*"


def _vector_slots(vec: List[float]) -> Dict[str, str]:
    return {f"{BACKEND_PREFIX}.{name}": f"{round(v * BACKEND_BUCKETS) / BACKEND_BUCKETS:.1f}"
            for name, v in zip(STATE_DIMS, vec)}


def _serialised(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run a backend method under the instance's lock (see ``AwmWorldModelBackend``)."""
    @functools.wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


#: What ``{agent}`` may not expand to in ``AITHER_AGENT_WM_AWM_SCOPE`` (crystal.py's rule).
_AGENT_SEGMENT_BAD = re.compile(r"[*:\x00-\x1f\x7f]")


def _agent_scope(template: str, agent_id: str) -> Tuple[str, Optional[str]]:
    """``AITHER_AGENT_WM_AWM_SCOPE`` for one agent -> (scope text, refusal or None).

    ``{agent}`` is replaced by the agent id so every agent keeps a scope of its own,
    like ``ADK_CRYSTAL_SCOPE``. A template WITHOUT ``{agent}`` is refused, never
    used: every agent in the process would share one scope, advise() and stats()
    would read the other agents' tool outcomes, and ``adk wm reset <one agent>``
    would delete all of them.
    """
    if "{agent}" not in template:
        return template, (f"AITHER_AGENT_WM_AWM_SCOPE={template!r} has no {{agent}}: it "
                          f"would pool every agent's transitions into one scope. Use "
                          f"e.g. 'acme:ops:{{agent}}'")
    if not agent_id or len(agent_id) > 128 or _AGENT_SEGMENT_BAD.search(agent_id):
        return template, f"agent id {agent_id!r} cannot be one scope segment"
    return template.replace("{agent}", agent_id), None


class AwmWorldModelBackend:
    """The agent loop's world model on an awm transition table. Never raises.

    Thread-safe: ``get_world_model`` caches ONE instance per agent and a host may call
    it from a thread other than the one that loaded it (the gobbonet pack runs each turn
    in a fresh thread). The sqlite connection is opened with ``check_same_thread=False``
    and every method that touches it holds one re-entrant lock.

    Storage: ``AITHER_AGENT_WM_AWM_DB`` or ``<wm_root>/awm_world.db`` -- a file of its
    own by default, so selecting the backend never touches the user's memory store.
    Scope: ``AITHER_AGENT_WM_AWM_SCOPE`` or ``local:<agent_id>:wm``.
    """

    backend_name = "awm"

    def scope_is_own(self) -> bool:
        """True when this agent's id is one segment of its scope (nobody else writes it).

        A scope configured without ``{agent}`` (or a checkpoint that recorded one) is
        shared by every agent bound to it: reading it pools their outcomes, and
        clearing it deletes every agent's transitions.
        """
        return self.agent_id in self.scope_text.split(":")

    def __init__(self, agent_name: str, root: str | None = None, *, db: str | Path | None = None,
                 scope: str | None = None, allowed_actions: Any = None,
                 agent_id: str | None = None) -> None:
        # `agent_id` = an id already canonical (the CLI holds ids, not names).
        self.agent_id = agent_id or wm_agent_id(agent_name)
        self.agent_name = agent_name
        self._root = root or wm_root()
        env_db = os.environ.get("AITHER_AGENT_WM_AWM_DB", "").strip()
        self.db_path = Path(db or env_db or os.path.join(self._root, "awm_world.db"))
        #: Where db_path came from: "arg", "env" (AITHER_AGENT_WM_AWM_DB) or "root"
        #: (<wm root>/awm_world.db). A "root" path is RELATIVE to the root: a copied or
        #: moved root carries its own table (see commands/wm.py ``_awm_backend``).
        self.db_source = "arg" if db else ("env" if env_db else "root")
        #: Why the configured scope was refused (the backend then degrades); None = ok.
        self._scope_refused: Optional[str] = None
        env_scope = os.environ.get("AITHER_AGENT_WM_AWM_SCOPE", "").strip()
        if scope:
            self.scope_text = scope
        elif env_scope:
            self.scope_text, self._scope_refused = _agent_scope(env_scope, self.agent_id)
        else:
            self.scope_text = f"local:{self.agent_id}:wm"
        self._allowed = (allowed_actions if allowed_actions is not None
                         else allowed_actions_from_env())
        self._store: Any = None
        self._scope: Any = None
        self._aw: Any = None
        self._loaded = False
        self.degraded: Optional[str] = None
        self._stage = "cold"
        self._n = 0
        self._known_states: set = set()
        self._state_rows: Dict[str, str] = {}
        self._rejected = 0
        self._goal = dict(DEFAULT_GOAL)
        # Its OWN checkpoint name: ``<agent_id>.wm.json`` is the builtin's, and writing
        # there would drop the builtin's learned action_stats/bias/gain.
        self._ckpt_path = Path(self._root) / f"{self.agent_id}{AWM_CKPT_SUFFIX}"
        self._salt: Optional[bytes] = None
        self._lock = threading.RLock()

    def _redact(self, name: Any) -> Optional[str]:
        # Keyed by the install's secret under this backend's root (worldmodel.py).
        if self._salt is None:
            self._salt = redaction_salt(self._root)
        return redact_action(name, self._allowed, on_miss="hash", salt=self._salt)

    # -- lifecycle ---------------------------------------------------------
    @_serialised
    def load(self) -> None:
        """Open the awm file (never migrating it) and count what it holds. Never raises."""
        if self._loaded:
            return
        self._loaded = True
        if self._scope_refused:
            self.degraded = self._scope_refused
            _degrade("awm-backend:shared-scope (AITHER_AGENT_WM_AWM_SCOPE has no {agent})")
            return
        try:
            got = _awm()
            if got is None:
                self.degraded = "awm is not importable: " + "; ".join(
                    d for d in TELEMETRY["degraded"] if d.startswith("awm"))
                return
            awm, self._aw = got
            self._scope = awm.Scope.parse(self.scope_text)
            self._states_scope = awm.Scope.parse(states_scope_text(self.scope_text))
            # auto_migrate=False overrides AWM_AUTO_MIGRATE: migrating a shared file
            # locks every older installed reader out of it, and that is the owner's
            # decision, never a side effect of selecting a backend.
            try:
                store = awm.MemoryStore(self.db_path, auto_migrate=False,
                                        check_same_thread=False)
            except TypeError:
                # An awm without the flag: usable from the loading thread only. Said.
                store = awm.MemoryStore(self.db_path, auto_migrate=False)
                _degrade("awm-backend:thread-bound (awm has no check_same_thread)")
            if getattr(store, "compat", False):
                exc = awm.NeedsMigration(
                    "adk world-model backend (transitions)", awm.SCHEMA_VERSION,
                    store.file_version, self.db_path)
                store.close()
                self.degraded = str(exc)
                _degrade(f"awm-backend:NeedsMigration:{self.db_path}")
                return
            _install_monotonic_clock(store)
            self._store = store
            self._n = len(store.transitions(self._scope))
            self._stage = self._stage_for(self._n)
        except Exception as exc:  # noqa: BLE001
            self.degraded = f"{type(exc).__name__}: {exc}"
            _degrade(f"awm-backend:{type(exc).__name__}")
            self._store = None

    def _ready(self) -> bool:
        if not self._loaded:
            self.load()
        return self._store is not None

    @staticmethod
    def _stage_for(n: int) -> str:
        return "cold" if n < WARM_MIN else ("warm" if n < TRAINED_MIN else "trained")

    # -- WorldModelBackend -------------------------------------------------
    def observe(self, context: dict | None = None) -> list[float] | None:
        """The builtin's 8-dim vector, so both backends see one state space."""
        return BuiltinWorldModel.observe(self, context)  # type: ignore[arg-type]

    def _state(self, slots: Dict[str, str], ts: float) -> Any:
        digest = self._aw.state_digest(slots)
        return self._aw.WorldState(digest=digest, slots=dict(slots), scope=str(self._scope),
                                   prefix=BACKEND_PREFIX, as_of=None, ts=ts)

    def _remember_state(self, slots: Dict[str, str]) -> None:
        # digest -> slots, so an offline learner can rebuild states from the table
        # (train_from_transitions). Only quantized numbers: nothing redactable here.
        digest = self._aw.state_digest(slots)
        if digest in self._known_states:
            return
        self._known_states.add(digest)
        # At the bookkeeping scope, never the transition scope (``states_scope_text``).
        self._store.remember(self._states_scope, f"{BACKEND_PREFIX}.state.{digest[:16]}",
                             json.dumps(slots, sort_keys=True), kind=STATE_KIND)

    @_serialised
    def record(self, state_before: Any, action: str, state_after: Any, ok: bool = True) -> None:
        """One (state, tool, state_after) transition into the awm table. Never raises."""
        try:
            if not self._ready():
                return
            vb = bounded_vector(state_before, len(STATE_DIMS))
            va = bounded_vector(state_after, len(STATE_DIMS))
            action, failed = split_failure_marker(action)
            if failed:
                ok = False
            act = self._redact(action)
            if vb is None or va is None or act is None:
                self._rejected += 1
                _count("backend.rejected")
                return
            sb = _vector_slots(vb)
            sa = dict(_vector_slots(va))
            sa[OK_SLOT] = "1" if ok else "0"
            now = float(self._store._clock())
            self._remember_state(sb)
            self._remember_state(sa)
            self._store.observe_transition(self._scope, self._state(sb, now), act,
                                           self._state(sa, now), source="adk-agent")
            self._n += 1
        except Exception as exc:  # noqa: BLE001
            _count("backend.record_error")
            logger.debug("awm backend record() failed for %s: %s", self.agent_id, exc)

    @_serialised
    def slots_for_transition(self, t: Any) -> Optional[Dict[str, str]]:
        """The before-slots of a recorded transition, for offline training. None if unknown."""
        if not self._ready():
            return None
        key = f"{BACKEND_PREFIX}.state.{t.state_digest[:16]}"
        if key not in self._state_rows:
            own = str(self._states_scope)
            self._state_rows = {m.key: m.value for m in self._store.recall(
                self._states_scope, kind=STATE_KIND, limit=10_000_000) if m.scope == own}
        raw = self._state_rows.get(key)
        if raw is None:
            return None
        slots = json.loads(raw)
        return slots if self._aw.state_digest(slots) == t.state_digest else None

    @_serialised
    def bootstrap(self) -> str:
        """Stage from the table size. Tabular: nothing to refit. Never raises."""
        try:
            if self._ready():
                self._stage = self._stage_for(self._n)
                if self._n and self._n % 50 == 0:
                    self.save()
        except Exception as exc:  # noqa: BLE001
            logger.debug("awm backend bootstrap() failed: %s", exc)
        return self._stage

    @_serialised
    def advise(self, state: Any, candidates: list[str]) -> dict | None:
        """Rank candidates by confidence-weighted success rate; None when none is known.

        Per candidate: the share of recorded outcomes with ``wm.ok == "1"`` for this
        exact state (RECALLED), else over all states (GENERALIZED, discounted -- it is
        state-blind). ``confidence`` is ``n / (n + 1)`` for n observations (halved for
        GENERALIZED): one observation is not certainty.

        Abstains PER TOOL: a GENERALIZED tool with fewer than
        ``GENERALIZE_MIN_SUPPORT`` observations is not scored (listed in ``unknown``
        with the untried ones). ``order`` holds EVERY candidate: unknown tools sit at
        ``UNKNOWN_PRIOR`` -- after a tool that mostly succeeds, before one that mostly
        fails -- so steering never runs a known-failing tool ahead of an untried one.
        """
        try:
            if not self._ready() or not candidates:
                return None
            vec = bounded_vector(state, len(STATE_DIMS))
            if vec is None:
                return None
            digest = self._aw.state_digest(_vector_slots(vec))
            scores: Dict[str, float] = {}
            conf: Dict[str, float] = {}
            source: Dict[str, str] = {}
            support: Dict[str, int] = {}
            index: Dict[str, int] = {}
            unknown: List[str] = []
            for i, name in enumerate(candidates):
                if not isinstance(name, str) or name in scores or name in index:
                    continue
                index[name] = i
                act = self._redact(split_failure_marker(name)[0])
                if act is None:
                    unknown.append(name)
                    continue
                rows = self._store.transitions(self._scope, state=digest, action=act)
                src = RECALLED
                if not rows:
                    rows = self._store.transitions(self._scope, action=act)
                    src = GENERALIZED
                if not rows or (src == GENERALIZED and len(rows) < GENERALIZE_MIN_SUPPORT):
                    unknown.append(name)
                    continue
                n = len(rows)
                oks = sum(1 for t in rows if t.delta.get(OK_SLOT) == "1")
                scores[name] = oks / n
                c = n / (n + 1.0)
                conf[name] = c if src == RECALLED else c * 0.5
                source[name] = src
                support[name] = n
            if not scores:
                return None

            def rank(a: str) -> Tuple[float, float, int]:
                # The score shrunk toward the unknown prior by its confidence: a tool
                # seen twice somewhere else (GENERALIZED, halved confidence) must not
                # outrank one seen ten times in THIS state. Unknown tools sit at the
                # prior, so a mostly-failing tool still ranks below an untried one.
                if a in scores:
                    return (-(UNKNOWN_PRIOR + conf[a] * (scores[a] - UNKNOWN_PRIOR)),
                            -conf[a], index[a])
                return (-UNKNOWN_PRIOR, 0.0, index[a])

            order = sorted(list(scores) + unknown, key=rank)
            return {"stage": self._stage, "scores": scores, "order": order, "n": self._n,
                    "backend": self.backend_name, "sources": source, "confidence": conf,
                    "support": support, "unknown": unknown}
        except Exception as exc:  # noqa: BLE001
            logger.debug("awm backend advise() failed: %s", exc)
            return None

    @_serialised
    def save(self) -> None:
        """A small checkpoint so ``adk wm status`` lists this agent. The table is the
        awm file itself (committed per write). Never raises.

        A backend with no open table (degraded, or never loaded) does not know how
        many transitions the table holds: it never overwrites an existing checkpoint
        (``adk wm train`` with awm unimportable reset n/last_trained_n to 0 for a
        60-transition table). Said in the log and counted, never silent.
        """
        try:
            if self._store is None and Path(self._ckpt_path).exists():
                _count("backend.save_skipped")
                logger.warning("awm backend %s: no open table (%s); checkpoint %s kept "
                               "as it is", self.agent_id, self.degraded or "not loaded",
                               self._ckpt_path)
                return
            Path(self._root).mkdir(parents=True, exist_ok=True)
            ckpt = {"version": 1, "agent_id": self.agent_id, "backend": self.backend_name,
                    "state_dims": STATE_DIMS, "n": self._n, "stage": self._stage,
                    "last_trained_n": self._n, "goal": self._goal,
                    "db": str(self.db_path), "db_source": self.db_source,
                    # A refused (shared) scope is never recorded: a checkpoint's scope
                    # is used as-is later, which would bypass the refusal.
                    "scope": None if self._scope_refused else self.scope_text,
                    "degraded": self.degraded}
            tmp = Path(str(self._ckpt_path) + ".tmp")
            tmp.write_text(json.dumps(ckpt, separators=(",", ":")), encoding="utf-8")
            os.replace(str(tmp), str(self._ckpt_path))
        except Exception as exc:  # noqa: BLE001
            logger.debug("awm backend save() failed: %s", exc)

    @_serialised
    def stats(self) -> dict:
        out: Dict[str, Any] = {
            "agent_id": self.agent_id, "backend": self.backend_name, "stage": self._stage,
            "n": self._n, "actions": 0, "state_dim": len(STATE_DIMS),
            "last_trained_n": self._n, "goal": dict(self._goal),
            "transitions": self._n, "surprise": None, "degraded": self.degraded,
            "rejected": self._rejected, "db": str(self.db_path), "scope": self.scope_text,
        }
        try:
            if self._ready():
                rows = self._store.transitions(self._scope)
                out["n"] = out["transitions"] = out["last_trained_n"] = len(rows)
                # From the same rows as n: `out` was filled before the lazy load.
                out["stage"] = self._stage_for(len(rows))
                out["actions"] = len({t.action for t in rows})
                s = self._store.surprise_stats(self._scope)
                out["surprise"] = {k: getattr(s, k) for k in (
                    "count", "novel", "mean", "p50", "p90", "max", "ece", "unexplained")}
                # The backend's own digest -> slots rows (_remember_state) are
                # bookkeeping, never a world slot: no transition delta names them, so
                # counted they read as two teleports per new state pair.
                book = f"{BACKEND_PREFIX}.state."
                out["surprise"]["unexplained"] = sum(
                    1 for c in self._store.unexplained_changes(self._scope, 0.0)
                    if not str(getattr(c, "key", "")).startswith(book))
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    @_serialised
    def close(self) -> None:
        if self._store is not None:
            try:
                self._store.close()
            finally:
                self._store = None
                self._loaded = False


def awm_backend_factory(agent_name: str) -> AwmWorldModelBackend:
    return AwmWorldModelBackend(agent_name)


awm_backend_factory.backend_name = "awm"  # type: ignore[attr-defined]


def register_awm_backend() -> None:
    """Install the awm backend as the world-model factory (``AITHER_AGENT_WM_BACKEND=awm``)."""
    register_world_model(awm_backend_factory)


# ============================================================================
# W2 -- the environment contract and the agent
# ============================================================================

@runtime_checkable
class EnvironmentAdapter(Protocol):
    """Local mirror of ``awpredict.contracts.EnvironmentAdapter``.

    ``observe(env_state)`` returns a flat ``{slot: value}`` dict of non-empty,
    unpadded strings (``env_state=None`` = the adapter's current state);
    ``actions()`` the action vocabulary; ``step(action)`` executes for real and
    returns ``(next_env_state, reward, done, info)``. ``info["facts"]`` may carry
    ``[{"subject": s, "fact": f}, ...]`` for the agent to reconcile into memory.
    Optional attributes: ``prefix`` (slot namespace, default ``world.<domain>``) and
    ``mirrors_state`` (default True; False = the adapter writes the store itself).
    """

    domain: str

    def observe(self, env_state: Any = None) -> Any: ...

    def actions(self) -> Sequence[Any]: ...

    def step(self, action: Any) -> Tuple[Any, float, bool, Dict[str, Any]]: ...


@dataclass(frozen=True)
class Plan:
    """A receding-horizon plan. ``actions`` is empty unless ``verdict == OK``."""

    actions: Tuple[str, ...]
    expected_cost: float
    verdict: str
    method: str = "none"
    sources: Tuple[str, ...] = ()
    segments: int = 1
    note: str = ""
    #: The weakest imagined step's confidence. OK means "executable", not "known": a plan
    #: resting on one observation or a factored guess is still worth one MPC step (the
    #: step is how the model learns), but a caller must be able to tell it from a plan
    #: on RECALLED dynamics without re-deriving it from ``sources``.
    confidence: float = 1.0

    @property
    def speculative(self) -> bool:
        return self.verdict == OK and self.confidence < SPECULATIVE_BELOW

    def to_dict(self) -> Dict[str, Any]:
        return {"actions": list(self.actions), "expected_cost": self.expected_cost,
                "verdict": self.verdict, "method": self.method, "sources": list(self.sources),
                "segments": self.segments, "note": self.note,
                "confidence": self.confidence, "speculative": self.speculative}


@dataclass
class StepResult:
    executed: bool
    plan: Optional[Plan]
    action: Optional[str] = None
    prediction: Any = None
    transition: Any = None
    surprise: Optional[float] = None
    reward: float = 0.0
    done: bool = False
    info: Dict[str, Any] = field(default_factory=dict)
    facts: List[Any] = field(default_factory=list)


@dataclass
class RunResult:
    reached: bool
    verdict: str
    steps: List[StepResult] = field(default_factory=list)

    @property
    def surprise_total(self) -> float:
        return sum(s.surprise or 0.0 for s in self.steps)


@dataclass
class ExploreResult:
    steps: int
    complete: bool
    actions: List[str] = field(default_factory=list)


@dataclass
class Understanding:
    """What the agent knows now: the state, its entities, and where code anchors point."""

    state: Any
    slots: Dict[str, str]
    entities: List[Dict[str, Any]]
    anchors: List[Dict[str, Any]]
    stale: List[Dict[str, Any]]
    degraded: List[str]


#: (delta, confidence, support)
_Outcome = Tuple[Dict[str, Optional[str]], float, int]
#: (delta, agreement, observations, distinct before-states)
_Marginal = Tuple[Dict[str, Optional[str]], float, int, int]


class _Table:
    """In-memory mirror of the visible awm transitions: exact and marginal outcomes.

    Built from ``store.transitions(scope)`` and appended on every executed step, so a
    plan's thousands of imagined lookups never touch SQLite. RECALLED = the MAJORITY
    outcome for the exact (state, action), ties to the most recent, confidence = the
    share agreeing with it -- one anomaly after five consistent observations does not
    become the prediction. The marginal is the most frequent outcome over all states,
    ties to the most recent.

    It also keeps the evidence for "one observation is the outcome": how many exact
    (state, action) pairs were observed more than once, and how many of those
    disagreed. See ``deterministic``.
    """

    def __init__(self) -> None:
        #: Repeated (state, action) pairs needed before a world counts as deterministic.
        self.repeats_needed = 2
        self.repeated = 0
        self.disagreed: set = set()
        self.exact: Dict[Tuple[str, str], List[str]] = {}
        self.deltas: Dict[str, Dict[str, Optional[str]]] = {}
        self.marg: Dict[str, Dict[str, List[int]]] = {}
        self.states: Dict[str, set] = {}
        self.seq = 0
        self.n = 0

    def add(self, digest: str, action: str, delta: Dict[str, Optional[str]]) -> None:
        c = _canon(delta)
        self.deltas.setdefault(c, dict(delta))
        seen = self.exact.setdefault((digest, action), [])
        if len(seen) == 1:
            self.repeated += 1
        if seen and c != seen[0]:
            self.disagreed.add((digest, action))
        seen.append(c)
        m = self.marg.setdefault(action, {})
        cnt = m.get(c, [0, 0])
        m[c] = [cnt[0] + 1, self.seq]
        self.states.setdefault(action, set()).add(digest)
        self.seq += 1
        self.n += 1

    def has(self, digest: str, action: str) -> bool:
        return (digest, action) in self.exact

    def deterministic(self) -> bool:
        """True when the table SHOWS deterministic dynamics: at least
        ``repeats_needed`` (state, action) pairs were observed more than once, and
        no pair ever gave two different outcomes. Only then is a single observation
        of an edge evidence enough for an impossibility claim (UNREACHABLE)."""
        return self.repeated >= self.repeats_needed and not self.disagreed

    def recalled(self, digest: str, action: str) -> Optional[_Outcome]:
        rows = self.exact.get((digest, action))
        if not rows:
            return None
        counts: Dict[str, int] = {}
        latest: Dict[str, int] = {}
        for i, c in enumerate(rows):
            counts[c] = counts.get(c, 0) + 1
            latest[c] = i
        best = max(counts, key=lambda c: (counts[c], latest[c]))
        return self.deltas[best], counts[best] / len(rows), len(rows)

    def generalized(self, action: str) -> Optional[_Marginal]:
        m = self.marg.get(action)
        if not m:
            return None
        best = max(m, key=lambda c: (m[c][0], m[c][1]))
        total = sum(v[0] for v in m.values())
        return self.deltas[best], m[best][0] / total, total, len(self.states[action])


def _ignorance_note(unmodelled: Any) -> str:
    names = sorted(unmodelled)
    shown = ", ".join(names[:8]) + (f" (+{len(names) - 8} more)" if len(names) > 8 else "")
    return (f"goal not reached with the modelled actions; no model for {len(names)} "
            f"action(s) met in the search: {shown}")


def _unpack_step(out: Any) -> Tuple[Any, float, bool, Dict[str, Any]]:
    if isinstance(out, tuple) and len(out) == 4:
        nxt, reward, done, info = out
        return nxt, float(reward or 0.0), bool(done), dict(info or {})
    raise TypeError(f"adapter.step must return (next_env_state, reward, done, info), "
                    f"got {type(out).__name__}")


class WorldModelAgent:
    """understand / predict / plan / step / run over one adapter, one awm scope.

    ``store`` is an ``awm.MemoryStore`` (or a path to open one); ``scope`` an
    ``awm.Scope`` or its text. The store must be schema v3: a compat-mode file raises
    ``NeedsMigration`` here and is not touched. The store's clock is wrapped to be
    strictly increasing (see ``_MonotonicClock``).

    ``learner`` is a learner object, ``None`` (no learned tail: a table miss answers
    GENERALIZED or NONE), or a NAME -- ``"auto"`` / ``"factored"`` -- which builds one
    with ``world_adapters.make_learner`` and WARM-STARTS it from every transition
    already in the store (``train_from_transitions``), so a reopened agent's learned
    tail knows its history. The report is ``self.learner_training``.
    """

    def __init__(self, store: Any, scope: Any, adapter: Any, *, prefix: Optional[str] = None,
                 learner: Any = None, code: Any = None, reconciler: Any = None,
                 horizon: int = 12, beam_width: int = 256, beam_max_actions: int = 12,
                 cem_samples: int = 64, cem_elites: int = 8, cem_iters: int = 6,
                 cem_smoothing: float = 0.7, seed: int = 0,
                 generalize_min_agreement: float = 1.0, uncertainty_penalty: float = 4.0,
                 generalize_min_support: int = GENERALIZE_MIN_SUPPORT) -> None:
        got = _awm()
        if got is None:
            raise RuntimeError("WorldModelAgent needs awm (>= 0.5): "
                               + "; ".join(TELEMETRY["degraded"]))
        self._awm, self._aw = got
        if isinstance(store, (str, Path)):
            store = self._awm.MemoryStore(Path(store), auto_migrate=False)
        if getattr(store, "compat", False):
            raise self._awm.NeedsMigration("WorldModelAgent (transitions)",
                                           self._awm.SCHEMA_VERSION, store.file_version,
                                           store.path)
        self.store = store
        self.scope = (scope if isinstance(scope, self._awm.Scope)
                      else self._awm.Scope.parse(str(scope)))
        self.adapter = adapter
        self.domain = str(getattr(adapter, "domain", "") or "env")
        self.prefix = prefix or getattr(adapter, "prefix", None) or f"world.{self.domain}"
        self._mirrors = bool(getattr(adapter, "mirrors_state", True))
        if not self._mirrors:
            a_store, a_scope = getattr(adapter, "store", None), getattr(adapter, "scope", None)
            if a_store is not None and a_store is not store:
                raise ValueError("an adapter that writes the store itself must share the "
                                 "agent's store")
            if a_scope is not None and str(a_scope) != str(self.scope):
                raise ValueError(f"adapter scope {a_scope} != agent scope {self.scope}")
        warm_start = isinstance(learner, str)
        if warm_start:
            if learner not in ("auto", "factored"):
                raise ValueError(f"learner={learner!r}: expected a learner object, None, "
                                 "'auto' or 'factored'")
            from adk.world_adapters import make_learner
            learner = make_learner(learner)
        self.learner = learner
        #: ``train_from_transitions``' report for a named learner; None otherwise.
        self.learner_training: Optional[Dict[str, Any]] = None
        self.code = code
        self.reconciler = reconciler
        self.horizon = int(horizon)
        self.beam_width = int(beam_width)
        self.beam_max_actions = int(beam_max_actions)
        self.cem_samples = int(cem_samples)
        self.cem_elites = int(cem_elites)
        self.cem_iters = int(cem_iters)
        self.cem_smoothing = float(cem_smoothing)
        self.seed = int(seed)
        self.generalize_min_agreement = float(generalize_min_agreement)
        self.generalize_min_support = max(1, int(generalize_min_support))
        self.uncertainty_penalty = float(uncertainty_penalty)
        _install_monotonic_clock(store)
        self._table = _Table()
        self._pcache: Dict[Tuple[str, str], Any] = {}
        #: step()'s subgoal progress: {(goal, subgoals): (subgoals already passed,
        #: digest of the state the last step left the world in)}.
        self._step_progress: Dict[Tuple[str, Tuple[str, ...]], Tuple[int, str]] = {}
        self.reload()
        if learner is None:
            _degrade("learner:unbound (table misses answer GENERALIZED or NONE)")
        elif warm_start:
            from adk.world_adapters import train_from_transitions
            self.learner_training = train_from_transitions(self, learner)
            skipped = self.learner_training.get("skipped") or {}
            if any(skipped.values()):
                _degrade(f"learner:warm-start skipped {skipped}")

    # -- model -------------------------------------------------------------
    def reload(self) -> int:
        """Rebuild the in-memory table from the store. Returns the row count."""
        self._table = _Table()
        self._pcache.clear()
        rows = self.store.transitions(self.scope)
        # A row with a delta is this agent's when every key it names is in the
        # prefix. An EMPTY delta names no key (all() is vacuously true): another
        # prefix's no-ops in this scope would enter the marginal and an action
        # never tried here would read GENERALIZED. A no-op is kept only when this
        # agent recorded it (its `agent:<domain>` source) or it starts from a state
        # digest this prefix's own rows start or end in.
        mine = {d for t in rows if t.delta and all(self._in_prefix(k) for k in t.delta)
                for d in (t.state_digest, t.next_digest)}
        src = f"agent:{self.domain}"
        for t in rows:
            if t.delta:
                if all(self._in_prefix(k) for k in t.delta):
                    self._table.add(t.state_digest, t.action, t.delta)
            elif t.source == src or t.state_digest in mine:
                self._table.add(t.state_digest, t.action, t.delta)
        return self._table.n

    def _in_prefix(self, key: str) -> bool:
        return key == self.prefix or key.startswith(self.prefix + ".")

    def _full(self, key: str) -> str:
        return key if self._in_prefix(key) else f"{self.prefix}.{key}"

    def goal(self, goal: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
        """A goal with relative keys expanded under this agent's prefix."""
        if not isinstance(goal, dict) or not goal:
            raise ValueError("a goal is a non-empty {slot: value|None} dict")
        return {self._full(str(k)): (None if v is None else str(v)) for k, v in goal.items()}

    def _actions(self) -> List[Tuple[str, Any]]:
        seen: Dict[str, Any] = {}
        for a in self.adapter.actions():
            k = self._aw.action_key(a)
            seen.setdefault(k, a)
        return list(seen.items())

    def _learned(self, slots: Dict[str, str], action: str) -> Any:
        if self.learner is None:
            return None
        try:
            out = self.learner.predict(slots, action)
        except Exception as exc:  # noqa: BLE001 -- a learner fault is a miss, reported
            _degrade(f"learner:{type(exc).__name__}: {exc}")
            return None
        if out is None:
            return None
        delta = out if isinstance(out, dict) else getattr(out, "delta", None)
        if not isinstance(delta, dict):
            _degrade(f"learner:{type(out).__name__} is not a delta")
            return None
        delta = {k: v for k, v in delta.items() if self._in_prefix(k)
                 and (v is None or isinstance(v, str)) and slots.get(k) != v}
        c = getattr(out, "confidence", self._aw.PREDICTOR_DEFAULT_CONFIDENCE)
        c = min(1.0, max(0.0, float(c))) if isinstance(c, (int, float)) else 0.5
        engine = str(getattr(out, "engine", None) or getattr(self.learner, "name", None)
                     or type(self.learner).__name__)
        return delta, c, engine

    def _predict(self, slots: Dict[str, str], digest: str, action: str) -> Any:
        key = (digest, action)
        hit = self._pcache.get(key)
        if hit is not None:
            return hit
        P = self._aw.Prediction
        apply = self._aw.apply_delta
        sd = self._aw.state_digest
        r = self._table.recalled(digest, action)
        if r is not None:
            d, c, n = r
            p = P(delta=dict(d), source=RECALLED, confidence=c, support=n,
                  next_digest=sd(apply(slots, d)))
        else:
            # GENERALIZED confidence is the agreement shrunk by n / (n + 1), the backend's
            # rule: one observation of an action is what it did ONCE, somewhere else.
            g = self._table.generalized(action)
            # State-independence needs evidence from more than one state: agreeing
            # observations all made in ONE state are that state's outcome, repeated.
            if (g is not None and g[1] >= self.generalize_min_agreement
                    and g[3] >= max(2, self.generalize_min_support)):
                p = P(delta=dict(g[0]), source=GENERALIZED, confidence=_shrunk(g[1], g[2]),
                      support=g[2], next_digest=sd(apply(slots, g[0])))
            else:
                learned = self._learned(slots, action)
                if learned is not None:
                    d, c, engine = learned
                    p = P(delta=d, source=PREDICTED, confidence=c, support=0,
                          next_digest=sd(apply(slots, d)), engine=engine)
                elif g is not None:
                    why = ("state-blind: this action's outcomes disagree across states"
                           if g[1] < self.generalize_min_agreement else
                           f"state-blind: seen in {g[3]} distinct state(s), fewer than "
                           f"{max(2, self.generalize_min_support)}")
                    p = P(delta=dict(g[0]), source=GENERALIZED,
                          confidence=min(STATE_BLIND_CAP, _shrunk(g[1], g[2])),
                          support=g[2], next_digest=sd(apply(slots, g[0])), note=why)
                else:
                    p = P(delta={}, source=NONE, confidence=0.0, support=0,
                          note="no transition recorded for this action")
        self._pcache[key] = p
        return p

    def predict(self, action: Any, state: Any = None) -> Any:
        """What ``action`` does from ``state`` (default: the stored current state).

        ``state`` may be an awm ``WorldState`` or a slot dict (relative or full keys).
        Returns an awm ``Prediction`` whose ``source`` says where it came from.
        """
        if state is None:
            slots = dict(self.store.encode_state(self.scope, prefix=self.prefix).slots)
        elif isinstance(state, dict):
            slots = {self._full(k): v for k, v in state.items()}
        else:
            slots = dict(state.slots)
        return self._predict(slots, self._aw.state_digest(slots), self._aw.action_key(action))

    # -- sensing -----------------------------------------------------------
    def _sync(self, obs: Any) -> None:
        if not isinstance(obs, dict):
            raise self._aw.WorldError(f"adapter.observe must return a slot dict, "
                                      f"got {type(obs).__name__}")
        want: Dict[str, str] = {}
        for k, v in obs.items():
            if not isinstance(k, str) or not k.strip():
                raise self._aw.WorldError(f"slot name {k!r} must be a non-empty string")
            if not isinstance(v, str) or not v.strip() or v != v.strip():
                raise self._aw.WorldError(f"slot {k!r} value {v!r} must be a non-empty, "
                                          f"unpadded string")
            want[self._full(k)] = v
        have = self.store.encode_state(self.scope, prefix=self.prefix).slots
        for k in sorted(want):
            if have.get(k) != want[k]:
                # SlotReconciler on the slot's own key: add, update, or nothing.
                self.store.reconcile_and_remember(self.scope, want[k], subject=k)
        for k in sorted(set(have) - set(want)):
            if not self.store.forget(self.scope, k):
                raise self._aw.WorldError(
                    f"slot {k!r} is held by an ancestor of {self.scope}; it cannot be "
                    f"removed here, so the observed state is unrepresentable")

    def sense(self, env_state: Any = None) -> Any:
        """Mirror the adapter's observation into the store (if it mirrors) and encode it."""
        if self._mirrors:
            self._sync(self.adapter.observe(env_state))
        return self.store.encode_state(self.scope, prefix=self.prefix)

    def _reconcile_facts(self, info: Dict[str, Any]) -> List[Any]:
        out = []
        for f in info.get("facts") or []:
            if isinstance(f, dict):
                subject, fact = f.get("subject"), f.get("fact")
            else:
                subject, fact = f
            out.append(self.store.reconcile_and_remember(self.scope, str(fact),
                                                         subject=str(subject),
                                                         reconciler=self.reconciler))
        return out

    def _execute(self, before: Any, key: str, action: Any, plan: Optional[Plan]) -> StepResult:
        pred = self._predict(dict(before.slots), before.digest, key)
        nxt, reward, done, info = _unpack_step(self.adapter.step(action))
        facts = self._reconcile_facts(info)
        after = self.sense(nxt)
        tr = self.store.observe_transition(self.scope, before, key, after, pred,
                                           source=f"agent:{self.domain}")
        self._table.add(before.digest, key, tr.delta)
        self._pcache.clear()
        if self.learner is not None:
            try:
                self.learner.observe(dict(before.slots), key, dict(after.slots))
            except Exception as exc:  # noqa: BLE001
                _degrade(f"learner.observe:{type(exc).__name__}: {exc}")
        return StepResult(executed=True, plan=plan, action=key, prediction=pred,
                          transition=tr, surprise=tr.surprise, reward=reward, done=done,
                          info=info, facts=facts)

    def act(self, action: Any) -> StepResult:
        """Execute one CHOSEN action (no planning): predict, step, record, score."""
        before = self.sense()
        return self._execute(before, self._aw.action_key(action), action, None)

    # -- understanding -----------------------------------------------------
    def anchor(self, symbol: str) -> Optional[str]:
        """Pin ``symbol`` to the file the code graph places it in (slot ``anchor.<symbol>``)."""
        if self.code is None:
            _degrade("awgraph:unbound (anchor)")
            return None
        locs = self.code.locate(symbol)
        if not locs:
            return None
        path = locs[0][0]
        self.store.remember(self.scope, f"anchor.{symbol}", path, kind="anchor")
        return path

    def understand(self) -> Understanding:
        """The current state, its entities with code anchors, and every stale anchor."""
        state = self.store.encode_state(self.scope, prefix=self.prefix)
        degraded: List[str] = []
        entities: List[Dict[str, Any]] = []
        try:
            ents = self.store.entities(self.scope)
        except Exception as exc:  # noqa: BLE001 -- an old file has no entities
            ents = []
            degraded.append(f"entities:{type(exc).__name__}")
        if self.code is None:
            degraded.append("awgraph:unbound")
        for e in ents:
            loc = None
            if self.code is not None:
                locs = self.code.locate(e.canonical)
                loc = {"path": locs[0][0], "line": locs[0][1]} if locs else None
            entities.append({"id": e.id, "canonical": e.canonical, "anchor": loc})
        anchors: List[Dict[str, Any]] = []
        stale: List[Dict[str, Any]] = []
        pinned = self.store.encode_state(self.scope, prefix="anchor").slots
        for key in sorted(pinned):
            symbol, path = key[len("anchor."):], pinned[key]
            row: Dict[str, Any] = {"symbol": symbol, "path": path, "verified": False}
            if self.code is not None:
                locs = self.code.locate(symbol)
                row["verified"] = True
                if not locs:
                    stale.append(dict(row, reason="missing", now=None))
                elif path not in [p for p, _ in locs]:
                    stale.append(dict(row, reason="moved", now=locs[0][0]))
            anchors.append(row)
        for d in degraded:
            _degrade(d)
        return Understanding(state=state, slots=dict(state.slots), entities=entities,
                             anchors=anchors, stale=stale, degraded=degraded)

    # -- planning ----------------------------------------------------------
    def _step_cost(self, p: Any) -> float:
        return 1.0 + self.uncertainty_penalty * (1.0 - float(p.confidence))

    def _rng(self, root_digest: str, goal: Dict[str, Optional[str]]) -> random.Random:
        h = hashlib.sha256(f"{self.seed}|{root_digest}|{_canon(goal)}".encode("utf-8"))
        return random.Random(int(h.hexdigest()[:16], 16))

    def _root_known(self, slots: Dict[str, str], digest: str, keys: List[str]) -> bool:
        return any(self._predict(slots, digest, k).source != NONE for k in keys)

    def _beam(self, slots: Dict[str, str], goal: Dict[str, Optional[str]], keys: List[str],
              horizon: int) -> Plan:
        sd, apply = self._aw.state_digest, self._aw.apply_delta
        root = sd(slots)
        if _satisfies(slots, goal):
            return Plan((), 0.0, OK, "beam")
        if not self._root_known(slots, root, keys):
            return Plan((), math.inf, NO_MODEL, "beam", note="no action is known from this state")
        frontier = [(0.0, slots, root, (), ())]
        best_cost = {root: 0.0}
        found = None
        unmodelled: set = set()
        guessed: set = set()  # actions met only as a guess (confidence < SPECULATIVE_BELOW)
        thin: set = set()  # actions met only as ONE observation in a world not shown deterministic
        pruned = 0  # states the beam dropped: a miss is then not a proof
        for _depth in range(horizon):
            nxt: Dict[str, Any] = {}
            for cost, s, d, acts, srcs in frontier:
                for k in keys:
                    p = self._predict(s, d, k)
                    if p.source == NONE:
                        unmodelled.add(k)
                        continue
                    if float(p.confidence) < SPECULATIVE_BELOW:
                        guessed.add(k)
                    elif (p.source == RECALLED and int(p.support or 0) < 2
                          and not self._table.deterministic()):
                        # ONE observation, in a world not shown deterministic: what the
                        # action did once, not what it does. A miss through it is no proof.
                        thin.add(k)
                    ns = apply(s, p.delta)
                    nd = p.next_digest or sd(ns)
                    c = cost + self._step_cost(p)
                    if nd in best_cost and best_cost[nd] <= c:
                        continue
                    best_cost[nd] = c
                    node = (c, ns, nd, acts + (k,), srcs + (p.source,))
                    if _satisfies(ns, goal):
                        if found is None or c < found[0]:
                            found = node
                        continue
                    if found is not None and c >= found[0]:
                        continue
                    nxt[nd] = node
            if not nxt:
                break
            pruned += max(0, len(nxt) - self.beam_width)
            frontier = sorted(nxt.values(),
                              key=lambda n: (n[0] + _mismatch(n[1], goal), n[3]))[:self.beam_width]
        cut = (f"; the beam (width {self.beam_width}) dropped {pruned} imagined state(s)"
               if pruned else "")
        if found is None:
            if unmodelled:
                return Plan((), math.inf, NO_MODEL, "beam",
                            note=_ignorance_note(unmodelled) + cut)
            if guessed:
                # A miss through a guessed edge proves nothing: the guess (a factored
                # "did nothing" extrapolation, a state-blind marginal) may be wrong.
                names = sorted(guessed)
                return Plan((), math.inf, NO_MODEL, "beam",
                            note=(f"goal not reached with the modelled actions; "
                                  f"{len(names)} action(s) only guessed at (confidence "
                                  f"< {SPECULATIVE_BELOW}): {', '.join(names[:8])}" + cut))
            if thin:
                names = sorted(thin)
                return Plan((), math.inf, NO_MODEL, "beam",
                            note=(f"goal not reached with the modelled actions; "
                                  f"{len(names)} action(s) observed only once in a world "
                                  f"not shown deterministic (no repeated observation "
                                  f"agrees yet): {', '.join(names[:8])}" + cut))
            if pruned:
                return Plan((), math.inf, INCOMPLETE, "beam",
                            note=f"goal not reached within horizon {horizon}{cut}, so this "
                                 f"is not a proof it is unreachable; raise beam_width")
            return Plan((), math.inf, UNREACHABLE, "beam",
                        note=f"goal not reached within horizon {horizon}")
        return Plan(found[3], found[0], OK, "beam", sources=found[4],
                    note=(f"not guaranteed cheapest{cut}" if pruned else ""))

    def _cem(self, slots: Dict[str, str], goal: Dict[str, Optional[str]], keys: List[str],
             horizon: int) -> Plan:
        sd, apply = self._aw.state_digest, self._aw.apply_delta
        root = sd(slots)
        if _satisfies(slots, goal):
            return Plan((), 0.0, OK, "cem")
        if not self._root_known(slots, root, keys):
            return Plan((), math.inf, NO_MODEL, "cem", note="no action is known from this state")
        rng = self._rng(root, goal)
        n_act = len(keys)
        probs = [[1.0 / n_act] * n_act for _ in range(horizon)]
        miss = 1000.0
        unmodelled: set = set()

        def rollout(seq: List[int]) -> Tuple[float, int, bool, Tuple[str, ...]]:
            s, d, cost, srcs = slots, root, 0.0, []
            for t, i in enumerate(seq):
                p = self._predict(s, d, keys[i])
                if p.source == NONE:
                    unmodelled.add(keys[i])
                    return miss * 2 + cost, t, False, tuple(srcs)
                s = apply(s, p.delta)
                d = p.next_digest or sd(s)
                cost += self._step_cost(p)
                srcs.append(p.source)
                if _satisfies(s, goal):
                    return cost, t + 1, True, tuple(srcs)
            return miss + 10.0 * _mismatch(s, goal) + cost, len(seq), False, tuple(srcs)

        best: Optional[Tuple[float, List[int], int, bool, Tuple[str, ...]]] = None
        elites_n = max(1, min(self.cem_elites, self.cem_samples))
        for _it in range(self.cem_iters):
            batch = []
            for _n in range(self.cem_samples):
                seq = [rng.choices(range(n_act), weights=probs[t])[0] for t in range(horizon)]
                cost, used, reached, srcs = rollout(seq)
                batch.append((cost, seq, used, reached, srcs))
            batch.sort(key=lambda b: (b[0], b[1]))
            elites = batch[:elites_n]
            if best is None or elites[0][0] < best[0]:
                best = elites[0]
            for t in range(horizon):
                counts = [0.0] * n_act
                for e in elites:
                    counts[e[1][t]] += 1.0
                fresh = [c / len(elites) for c in counts]
                probs[t] = [max(1e-3, self.cem_smoothing * f + (1 - self.cem_smoothing) * o)
                            for f, o in zip(fresh, probs[t])]
                z = sum(probs[t])
                probs[t] = [p / z for p in probs[t]]
        assert best is not None
        cost, seq, used, reached, srcs = best
        if not reached:
            if unmodelled:
                return Plan((), math.inf, NO_MODEL, "cem", note=_ignorance_note(unmodelled))
            # Sampling covers some sequences, never all: a miss is not a proof.
            return Plan((), math.inf, INCOMPLETE, "cem",
                        note=f"no sampled sequence reached the goal within horizon {horizon}")
        return Plan(tuple(keys[i] for i in seq[:used]), cost, OK, "cem", sources=srcs)

    def _plan_from(self, slots: Dict[str, str], goal: Dict[str, Optional[str]],
                   subgoals: Optional[List[Dict[str, Optional[str]]]] = None) -> Plan:
        keys = [k for k, _ in self._actions()]
        if not keys:
            return Plan((), math.inf, NO_MODEL, note="the adapter offers no actions")
        search = self._beam if len(keys) <= self.beam_max_actions else self._cem
        chain = list(subgoals or []) + [goal]
        acts: List[str] = []
        srcs: List[str] = []
        total = 0.0
        cur = dict(slots)
        method = "none"
        weakest = 1.0
        notes: List[str] = []
        for i, g in enumerate(chain):
            seg = search(cur, g, keys, self.horizon)
            method = seg.method
            if seg.verdict != OK:
                return Plan((), math.inf, seg.verdict, seg.method, segments=len(chain),
                            note=f"segment {i + 1}/{len(chain)}: {seg.note}")
            for k in seg.actions:
                pred = self._predict(cur, self._aw.state_digest(cur), k)
                weakest = min(weakest, float(getattr(pred, "confidence", 0.0) or 0.0))
                cur = self._aw.apply_delta(cur, pred.delta)
            acts.extend(seg.actions)
            srcs.extend(seg.sources)
            total += seg.expected_cost
            if seg.note:
                notes.append(f"segment {i + 1}/{len(chain)}: {seg.note}")
        return Plan(tuple(acts), total, OK, method, sources=tuple(srcs), segments=len(chain),
                    confidence=weakest if acts else 1.0, note="; ".join(notes))

    def plan(self, goal: Dict[str, Optional[str]],
             subgoals: Optional[List[Dict[str, Optional[str]]]] = None,
             state: Any = None) -> Plan:
        """Search imagined futures for a path to ``goal`` (through ``subgoals`` in order).

        Verdict OK with the actions and expected cost; NO_MODEL when nothing is known
        from the start state, OR when no path was found and the search met actions it
        has no model for (the note names them: the goal may be one untried action
        away, so this is ignorance, not impossibility); UNREACHABLE only when every
        action the search met was modelled with confidence >= SPECULATIVE_BELOW (a
        guessed edge makes a miss NO_MODEL), the search dropped no state (an exhaustive
        beam), and still no path fits the horizon; INCOMPLETE when no path was found
        by a search that was not exhaustive (the beam pruned past ``beam_width``, or
        CEM, which samples) -- not a proof either way. Each
        subgoal segment gets its own horizon, so a chain reaches past one.
        """
        if state is None:
            slots = dict(self.store.encode_state(self.scope, prefix=self.prefix).slots)
        elif isinstance(state, dict):
            slots = {self._full(k): v for k, v in state.items()}
        else:
            slots = dict(state.slots)
        return self._plan_from(slots, self.goal(goal),
                               [self.goal(g) for g in (subgoals or [])])

    def step(self, goal: Dict[str, Optional[str]],
             subgoals: Optional[List[Dict[str, Optional[str]]]] = None) -> StepResult:
        """MPC: sense, plan, execute ONLY the first action, record it. Replans next call.

        Subgoal progress is KEPT across calls for the same (goal, subgoals), as ``run``
        keeps it: a subgoal once reached stays passed even when the path to the goal
        leaves it (re-deriving the chain from the current state each call made such a
        chain oscillate back to the subgoal forever). Reaching the goal, or a new
        (goal, subgoals) pair, starts over.

        Progress is only kept while the trajectory is CONTINUOUS: it is stored with the
        digest of the state this agent's last step left the world in, and a call that
        senses any other state (an episode reset, another actor) re-derives the chain
        from the current state. Kept across a reset, the passed subgoals stayed skipped
        and the planner was asked for the flat goal beyond its horizon (UNREACHABLE
        where a fresh agent plans OK). ``reset_progress`` clears it explicitly.
        """
        before = self.sense()
        g = self.goal(goal)
        subs = [self.goal(s) for s in (subgoals or [])]
        pkey = (_canon(g), tuple(_canon(s) for s in subs))
        prev = self._step_progress.get(pkey)
        passed = prev[0] if prev is not None and prev[1] == before.digest else 0
        while passed < len(subs) and _satisfies(before.slots, subs[passed]):
            passed += 1
        if _satisfies(before.slots, g):
            self._step_progress.pop(pkey, None)
        else:
            self._step_progress = {pkey: (passed, before.digest)}
        subs = subs[passed:]
        plan = self._plan_from(dict(before.slots), g, subs)
        if plan.verdict != OK or not plan.actions:
            return StepResult(executed=False, plan=plan)
        key = plan.actions[0]
        original = dict(self._actions()).get(key, key)
        res = self._execute(before, key, original, plan)
        if pkey in self._step_progress and res.transition is not None:
            self._step_progress[pkey] = (passed, res.transition.next_digest)
        return res

    def reset_progress(self) -> None:
        """Forget step()'s subgoal progress (call at an episode boundary)."""
        self._step_progress.clear()

    def run(self, goal: Dict[str, Optional[str]], budget: int = 50,
            subgoals: Optional[List[Dict[str, Optional[str]]]] = None) -> RunResult:
        """Step until the goal holds, the planner refuses, or ``budget`` steps are spent."""
        g = self.goal(goal)
        remaining = [self.goal(s) for s in (subgoals or [])]
        steps: List[StepResult] = []
        for _ in range(int(budget)):
            ws = self.sense()
            if _satisfies(ws.slots, g):
                return RunResult(True, OK, steps)
            while remaining and _satisfies(ws.slots, remaining[0]):
                remaining.pop(0)
            plan = self._plan_from(dict(ws.slots), g, remaining)
            if plan.verdict != OK or not plan.actions:
                return RunResult(False, plan.verdict, steps)
            key = plan.actions[0]
            st = self._execute(ws, key, dict(self._actions()).get(key, key), plan)
            steps.append(st)
            if st.done:
                ws = self.sense()
                return RunResult(_satisfies(ws.slots, g), OK if _satisfies(ws.slots, g) else DONE,
                                 steps)
        reached = _satisfies(self.store.encode_state(self.scope, prefix=self.prefix).slots, g)
        return RunResult(reached, OK if reached else BUDGET, steps)

    def explore(self, budget: int = 1000) -> ExploreResult:
        """Curiosity: try every untried action where you stand; else walk (on RECALLED
        dynamics only) to the nearest state that still has one. ``complete`` = no
        reachable (state, action) pair is left untried."""
        done: List[str] = []
        for _ in range(int(budget)):
            ws = self.sense()
            acts = self._actions()
            untried = [(k, a) for k, a in acts if not self._table.has(ws.digest, k)]
            if untried:
                k, a = untried[0]
            else:
                first = self._path_to_frontier(dict(ws.slots), ws.digest, [k for k, _ in acts])
                if first is None:
                    return ExploreResult(len(done), True, done)
                k, a = first, dict(acts)[first]
            self._execute(ws, k, a, None)
            done.append(k)
        return ExploreResult(len(done), False, done)

    def _path_to_frontier(self, slots: Dict[str, str], digest: str,
                          keys: List[str]) -> Optional[str]:
        """First action of the shortest RECALLED path to a state with an untried action."""
        from collections import deque

        apply, sd = self._aw.apply_delta, self._aw.state_digest
        seen = {digest}
        q: Any = deque([(slots, digest, None)])
        while q:
            s, d, first = q.popleft()
            for k in keys:
                r = self._table.recalled(d, k)
                if r is None:
                    continue
                ns = apply(s, r[0])
                nd = sd(ns)
                if nd in seen:
                    continue
                seen.add(nd)
                f = first or k
                if any(not self._table.has(nd, kk) for kk in keys):
                    return f
                q.append((ns, nd, f))
        return None

    def stats(self) -> Dict[str, Any]:
        # prefix: only this world's slots can teleport. The facts the agent itself
        # reconciled from info["facts"] live outside it and are not world changes.
        s = self.store.surprise_stats(self.scope, prefix=self.prefix)
        return {"domain": self.domain, "prefix": self.prefix, "transitions": self._table.n,
                "surprise": s.to_dict(), "degraded": list(TELEMETRY["degraded"]),
                "learner": getattr(self.learner, "name", None)}


class MemoryAdapter:
    """User statements as actions: each one goes through ``reconcile_and_remember``.

    The adapter writes the store itself (``mirrors_state = False``); the state is the
    slots under ``subject``. "prefers dark mode" then "switched to light mode" are two
    actions whose transitions the agent records like any other, and the current state
    holds ONE answer.
    """

    domain = "memory"
    mirrors_state = False

    def __init__(self, store: Any, scope: Any, subject: str, reconciler: Any = None) -> None:
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject must be a non-empty slot name")
        self.store = store
        self.scope = scope
        self.subject = subject.strip()
        self.prefix = self.subject
        self.reconciler = reconciler
        self.pending: List[str] = []

    def say(self, fact: str) -> str:
        fact = fact.strip()
        if not fact:
            raise ValueError("a statement must be non-empty")
        self.pending.append(fact)
        return fact

    def observe(self, env_state: Any = None) -> Dict[str, str]:
        return dict(self.store.encode_state(self.scope, prefix=self.subject).slots)

    def actions(self) -> List[str]:
        return list(self.pending)

    def step(self, action: Any) -> Tuple[Any, float, bool, Dict[str, Any]]:
        fact = str(action).strip()
        decision = self.store.reconcile_and_remember(self.scope, fact, subject=self.subject,
                                                     reconciler=self.reconciler)
        if fact in self.pending:
            self.pending.remove(fact)
        return self.observe(), 0.0, False, {"decision": decision.to_dict()}


__all__ = [
    "AwmWorldModelBackend", "BACKEND_PREFIX", "BUDGET", "DONE", "EnvironmentAdapter",
    "ExploreResult", "GENERALIZED", "GENERALIZE_MIN_SUPPORT", "MemoryAdapter", "NONE",
    "INCOMPLETE", "NO_MODEL", "OK", "OK_SLOT", "PREDICTED", "Plan", "RECALLED", "RunResult",
    "SPECULATIVE_BELOW", "STATES_SEGMENT", "STATE_BLIND_CAP", "StepResult",
    "TELEMETRY", "UNKNOWN_PRIOR", "UNREACHABLE", "Understanding", "WorldModelAgent",
    "awm_backend_factory", "register_awm_backend", "split_failure_marker",
    "states_scope_text",
]
