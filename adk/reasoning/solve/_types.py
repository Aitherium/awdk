"""Core types of the general reasoning loop: the Environment seam.

This module is the part of ``adk.reasoning.solve`` that does not depend on the
reasoning core itself (``docs/reasoning-loop-design.md`` section 2). It holds the
contract every domain implements -- ARC-AGI-3 games, toy environments, and later
Genesis interactive drivers -- so adapters and eval suites can be written and
tested before the loop lands.

The shapes are structurally identical to the reasoning-loop prototype's interfaces
(``Obs`` / ``Environment``), so a prototype environment satisfies this protocol unchanged,
and :data:`HOOK_ARGS` records the optional hooks with the arguments the prototype
loop actually passes them.

Stdlib only: ``state`` is typed ``Any`` so importing this module never pulls numpy.
3.10-compatible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

#: ``(action_id, x, y)``; ``x = y = -1`` when the action takes no coordinates.
Action = Tuple[int, int, int]

#: The optional hooks, keyed by name, with the POSITIONAL arguments the loop passes.
#:
#: The loop probes each with ``getattr(hooks, name)(*args)`` and never requires one;
#: an absent hook falls back to a core default. The arities are the loop's call
#: sites, not a wish list: a hook that cannot be called with exactly these
#: arguments raises ``TypeError`` inside the loop, so
#: :func:`adk.reasoning.solve.conformance.check_environment` rejects it.
#:
#: Called by the loop:
#:
#: * ``primer() -> str`` -- domain API and knowledge for the system prompt
#: * ``render(obs, last) -> str`` -- the SITUATION text for one turn (``last`` is the
#:   previous transition or None)
#: * ``describe(t) -> str`` -- one transition as text (``t`` has ``before``,
#:   ``after``, ``action``, ``changed``, ``level_up``, ``died``)
#: * ``tools(loop) -> dict[str, tuple[Callable, str]]`` -- extra REPL tools; the
#:   loop passes ITSELF so a tool can reach its history, caps and namespace
#: * ``candidates() -> list[Action]`` -- actions worth a systematic scan
#: * ``state_key(state) -> str`` -- novelty key (masks clocks / HUDs)
#: * ``auto_action() -> Action | None`` -- one action from a non-LLM explorer;
#:   None ends the automatic run (the "handoff" strategy and fallbacks use it)
#: * ``needs_xy(action_id) -> bool`` -- the action takes coordinates
#: * ``significant_change(before, after) -> bool`` -- a real change, as opposed to
#:   a HUD / clock tick (the prediction learner's ``changed`` claim)
#:
#: Declared for domains, not called by the loop core:
#:
#: * ``handoff(n) -> dict`` -- run ``n`` actions of a non-LLM policy and report
#: * ``hud() -> mask | None`` -- the cells a HUD / meter occupies, for renderers and tools
HOOK_ARGS: Dict[str, Tuple[str, ...]] = {
    "primer": (),
    "render": ("obs", "last"),
    "describe": ("t",),
    "tools": ("loop",),
    "candidates": (),
    "state_key": ("state",),
    "auto_action": (),
    "needs_xy": ("action_id",),
    "significant_change": ("before", "after"),
    "handoff": ("n",),
    "hud": (),
}

#: The optional hook names (the keys of :data:`HOOK_ARGS`), in contract order.
OPTIONAL_HOOKS: Tuple[str, ...] = tuple(HOOK_ARGS)


@dataclass
class Obs:
    """One observation.

    ``state`` is whatever the domain chooses (a numpy array for ARC).
    ``died`` means the action ended in a loss; the environment has already
    restarted play, so the next ``observe()`` shows the post-restart state.
    """

    state: Any
    level: int = 0
    level_up: bool = False
    died: bool = False
    done: bool = False
    win_state: Any = None
    info: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Environment(Protocol):
    """What the reasoning loop needs from a world.

    ``act`` is BLOCKING and returns the next observation. ``source`` names who
    chose the action (``"model"``, ``"tool"``, ``"policy"``...) for logging only.
    """

    def observe(self) -> Obs: ...

    def act(self, action: Action, source: str = "model") -> Obs: ...

    def available_actions(self) -> List[int]: ...

    def done(self) -> bool: ...


@runtime_checkable
class Memory(Protocol):
    """Namespaced JSON key/value persistence (the h30 ``MemoryBackend`` shape).

    The loop keeps PRISM scores and the skill library here (namespace
    ``procedural``). ``update`` is a read-modify-write that must be atomic per key.
    Implementations: ``adk.reasoning.solve.memory.InMemory`` / ``FileMemory``.
    """

    def get(self, namespace: str, key: str, default: Any = None) -> Any: ...

    def put(self, namespace: str, key: str, value: Any) -> None: ...

    def update(self, namespace: str, key: str, fn: Callable[[Any], Any]) -> Any: ...


@dataclass(frozen=True)
class Strategy:
    """A PRISM arm, as reported in ``SolveResult.strategy_trace``.

    ``name`` is the vendored strategy id (``rule_first``, ``goal_first``,
    ``analogy``, ``handoff``, ``click_scan``); ``kind`` is ``"llm"`` or ``"auto"``
    (auto arms act without a model call); ``overlay`` is the principle text the
    arm adds to the prompt.
    """

    name: str
    overlay: str = ""
    kind: str = "llm"


@dataclass(frozen=True)
class Hypothesis:
    """Read-only view of one hypothesis the loop kept or refuted.

    ``id`` is ``sha256(source)[:16]`` (of the name when no source was captured),
    stable across runs. ``status`` is ``"active"`` (replays perfectly with enough
    support), ``"pending"`` (consistent so far, not enough support) or
    ``"refuted"``; ``detail`` carries the core's own status word.
    """

    id: str
    name: str
    kind: str
    source: str
    support: int
    status: str
    detail: str = ""


@dataclass(frozen=True)
class Budget:
    """Hard limits, enforced by the Governor at every act(), before every model
    call, at the head of every turn and on every traced line of model code.

    ``max_llm_calls`` defaults to the h30 ``max_calls`` (40). Once it is spent
    the core falls back to the environment's ``auto_action`` hook, as h30 does.
    ``max_tokens`` sums backend-reported usage and falls back to an estimate
    (``SolveResult.tokens["estimated"]``) when a backend reports none.
    """

    max_llm_calls: int = 40
    max_actions: Optional[int] = None
    max_wall_s: Optional[float] = None
    max_tokens: Optional[int] = None
    turn_s: float = 20.0
    llm_timeout_s: float = 120.0


@dataclass
class LoopConfig:
    """Configuration of one ``solve()`` run.

    ``sase=False`` is the h30 ``plain`` ablation (one python block per reply, no
    intent classifier or prediction protocol). ``prism`` switches strategy
    rotation. ``strategies`` restricts PRISM to those vendored strategy ids.
    ``core`` passes any other h30 ``LoopConfig`` field through unchanged
    (``stuck_actions``, ``fallback_actions``, ``ctx_tokens`` ...).
    ``planning`` adds the ``plan()`` / ``disagree()`` tools (sase only) and
    ``daydream`` the offline search between exploring/stuck turns, capped at
    ``daydream_s`` seconds and ``plan_states`` imagined states.
    ``grounded`` (sase only) is h31's data-grounded synthesis: before each model
    turn the core builds an evidence table from episodic memory (one row per
    action family), replays the environment's ``propose()`` candidate rules and
    shows only those that pass; a rule the model writes must cite a table row
    and beat the candidates' coverage.  Off by default until measured on the
    host's own environments.
    """

    budget: Budget = field(default_factory=Budget)
    sase: bool = True
    prism: bool = True
    strategies: Optional[Tuple[str, ...]] = None
    wm_tokens: int = 1800
    temperature: float = 0.4
    max_tokens: int = 1100
    run_dir: Optional[str] = None
    core: Dict[str, Any] = field(default_factory=dict)
    #: sase mode: ``plan()`` / ``disagree()`` tools and the daydream step (``_daydream``).
    planning: bool = True
    daydream: bool = True
    daydream_s: float = 2.0
    plan_states: int = 5000
    #: sase mode: h31 evidence table + induced candidate rules + cited rules.
    grounded: bool = False
    #: Calibrated prediction ledger (``_ledger.py``): an awdecide SQLite path. Every
    #: scored prediction and the identity baseline on the same transition are booked
    #: with a Brier score; ``stats["ledger"]`` says which sources beat "nothing changes".
    ledger: Optional[str] = None
    #: Context permission (``context.py``, ``_context_gate.py``): ``"off"`` | ``"shadow"``
    #: (record what the context WOULD refuse) | ``"enforce"``. plan() needs a predict rule
    #: replay-verified on ``context_level_support`` of THIS level's transitions.
    context: str = "off"
    context_level_support: int = 2
    #: :class:`adk.reasoning.solve.context.Invariants`, built by the host; enforced in
    #: every ``context`` mode, including ``"off"``.
    invariants: Optional[Any] = None
    #: Episode capture (``harvest.py``): a JSONL path; each run appends one record of
    #: every model call (context + decision + served model), the outcome, the verified
    #: hypotheses and provenance. Every reply must be served by the requested model
    #: (a mismatch is FATAL). ``None`` = off.
    harvest: Optional[str] = None


#: Every value ``SolveResult.finish_reason`` can take.
FINISH_REASONS = (
    "won",
    "done",
    "budget:llm_calls",
    "budget:actions",
    "budget:wall",
    "budget:tokens",
    "cancelled",
    "llm_error",
    "error",
)


@dataclass
class SolveResult:
    """What one ``solve()`` run did. ``exit_code``: 0 won; 2 when the model
    never answered (``llm_error`` with zero successful calls) or the run errored
    before its first turn; 1 otherwise (ran, did not win)."""

    finish_reason: str
    won: bool
    levels: int
    level_actions: List[int]
    actions: int
    turns: int
    llm_calls: int
    tokens: Dict[str, Any]
    wall_s: float
    hypotheses: List[Hypothesis] = field(default_factory=list)
    calibration: Optional[float] = None
    strategy_trace: List[str] = field(default_factory=list)
    log_path: Optional[str] = None
    error: Optional[str] = None
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        if self.won:
            return 0
        if self.finish_reason == "llm_error" and self.llm_calls == 0:
            return 2
        if self.finish_reason == "error" and self.turns == 0:
            return 2
        return 1
