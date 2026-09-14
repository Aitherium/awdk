"""Generic Monte Carlo Tree Search engine.

Ported from AitherOS ``lib/cognitive/UnifiedMCTS.py`` with every
AitherOS-specific coupling either deleted or turned into an optional,
default-off injected seam. With all seams left ``None`` the algorithm is
byte-behaviour-identical to the original's no-world-model path:

    SELECT (UCT / PUCT)  ->  EXPAND  ->  SIMULATE  ->  BACKPROPAGATE

Seams (all on :class:`MCTSConfig`, all default ``None``):

* ``transition_model`` — model-based rollout stepping (replaces WorldModel).
* ``policy_model``     — PUCT action priors (replaces routing-prior provider).
* ``value_model``      — leaf value oracle past a depth gate (replaces the
  lazy ``lib.cognitive.MCTSValueModel`` import).
* ``observer``         — ``(event, payload)`` hook (replaces FluxEmitter /
  Strata ``_emit_search_started`` / ``_emit_search_complete``).
* ``artifact_dir``     — optional JSONL exploration persistence directory
  (replaces the hard-coded ``Library/Training`` path).
* ``dedup_embedder``   — optional ``state -> vector`` callable for semantic
  subtree dedup (replaces the OmniVector coupling).

Couplings that were **deleted** (not guarded): ``_aither_verify`` /
``lib.security.TLSConfig``, ``lib.core.FluxEmitter``, ``lib.core.AitherPorts``
+ Strata ``httpx.post``, ``lib.cognitive.SurpriseDetector``,
``lib.cognitive.LatentPredictor``, ``lib.cognitive.LearnedWorldModel``,
``lib.cognitive.MCTSValueModel``, ``lib.cognitive.MCTSValueAdapter``,
``lib.core.platform_flags`` and the ``lib.agents.AitherChronicle`` logger.
"""

from __future__ import annotations

import inspect
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from .env import MCTSEnvironment

if TYPE_CHECKING:  # avoid any runtime import cycle; seams are duck-typed
    from .models import PolicyModel, TransitionModel, ValueModel

log = logging.getLogger("adk.reasoning.mcts")


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _action_key(action: Any) -> Any:
    """Make an action usable as a dict key (hashable, stable)."""
    if isinstance(action, (str, int, float, tuple, bool, type(None))):
        return action
    try:
        hash(action)
        return action
    except TypeError:
        return str(action)


def _call_sync(fn: Callable[..., Any], *args: Any) -> Any:
    """Call ``fn``; if it returns a coroutine (async seam in a sync path),
    close it and return ``None`` so the caller falls back to the base path."""
    res = fn(*args)
    if inspect.isawaitable(res):
        close = getattr(res, "close", None)
        if callable(close):
            close()
        return None
    return res


async def _call_async(fn: Callable[..., Any], *args: Any) -> Any:
    res = fn(*args)
    if inspect.isawaitable(res):
        res = await res
    return res


def _cosine(a: Any, b: Any) -> float:
    """Cosine similarity for two float sequences. Returns 0.0 on any mismatch."""
    try:
        n = min(len(a), len(b))
        if n == 0:
            return 0.0
        dot = sum(float(a[i]) * float(b[i]) for i in range(n))
        na = math.sqrt(sum(float(a[i]) ** 2 for i in range(n)))
        nb = math.sqrt(sum(float(b[i]) ** 2 for i in range(n)))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)
    except Exception:
        return 0.0


def _max_tree_depth(node: "MCTSNode") -> int:
    if not node.children:
        return node.depth
    return max(_max_tree_depth(c) for c in node.children)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


@dataclass
class MCTSNode:
    """A node in the search tree."""

    state_hash: int
    action: Any = None
    parent: Optional["MCTSNode"] = None
    children: List["MCTSNode"] = field(default_factory=list)
    visits: int = 0
    value_sum: float = 0.0
    prior: float = 1.0
    untried_actions: List[Any] = field(default_factory=list)
    terminal: bool = False
    depth: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Optional embedding for the semantic-dedup seam (dedup_embedder).
    embedding: Optional[Any] = None
    # Actions that previously raised during expansion.
    failed_actions: Set[Any] = field(default_factory=set)
    # Stable id for reroot / warmstart.
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def avg_value(self) -> float:
        return self.value_sum / self.visits if self.visits > 0 else 0.0

    @property
    def is_fully_expanded(self) -> bool:
        return len(self.untried_actions) == 0

    def uct_value(
        self,
        exploration_weight: float = 1.41,
        use_puct: bool = False,
        c_puct: float = 1.25,
    ) -> float:
        """UCT or PUCT selection score.

        UCT:  Q + c * sqrt(ln(N_parent) / n)
        PUCT: Q + c_puct * prior * sqrt(N_parent) / (1 + n)
        """
        if self.visits == 0:
            return float("inf")
        if self.parent is None or self.parent.visits == 0:
            return self.avg_value

        exploitation = self.avg_value

        if use_puct:
            parent_visits = max(1, self.parent.visits)
            prior_term = (
                c_puct * self.prior * math.sqrt(parent_visits) / (1 + self.visits)
            )
            return exploitation + prior_term

        exploration = exploration_weight * math.sqrt(
            math.log(self.parent.visits) / self.visits
        )
        return exploitation + exploration

    def best_child(self, exploration_weight: float = 1.41) -> "MCTSNode":
        return max(self.children, key=lambda c: c.uct_value(exploration_weight))

    def get_action_path(self) -> List[Any]:
        """Reconstruct the action sequence from root to this node."""
        actions: List[Any] = []
        node: Optional[MCTSNode] = self
        while node is not None:
            if node.action is not None:
                actions.append(node.action)
            node = node.parent
        actions.reverse()
        return actions


# ---------------------------------------------------------------------------
# Reroot / warmstart
# ---------------------------------------------------------------------------


class MCTSReroot:
    """Operations for rerooting the tree to warmstart from an intermediate node."""

    @staticmethod
    def find_node_by_action_sequence(
        root: MCTSNode, actions: List[Any]
    ) -> Optional[MCTSNode]:
        current = root
        for action in actions:
            found = None
            for child in current.children:
                if child.action == action:
                    found = child
                    break
            if found is None:
                return None
            current = found
        return current

    @staticmethod
    def reroot_to_node(old_root: MCTSNode, target: MCTSNode) -> MCTSNode:
        """Detach ``target`` as the new root; visits/value_sum are preserved."""
        if target.parent is not None:
            target.parent.children = [
                c for c in target.parent.children if c.node_id != target.node_id
            ]
        target.parent = None
        target.depth = 0

        def _redepth(node: MCTSNode, new_depth: int) -> None:
            node.depth = new_depth
            for child in node.children:
                _redepth(child, new_depth + 1)

        _redepth(target, 0)
        return target


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class MCTSConfig:
    """Configuration for :class:`UnifiedMCTS`.

    Numeric defaults match the original ``UnifiedMCTS`` (``time_limit`` is
    expressed here in milliseconds: ``10_000.0`` == the original ``10.0`` s).
    """

    iterations: int = 200
    time_limit_ms: float = 10_000.0
    exploration_weight: float = 1.41  # UCB1 C parameter
    use_puct: bool = False
    c_puct: float = 1.25
    max_depth: int = 50
    simulation_depth: int = 20
    max_branching_factor: int = 10
    # Budget on value-oracle (value_model) calls per search. 0 = unlimited.
    max_llm_calls_per_search: int = 10
    # Distillation trace capture (visit distributions + PV + top-K paths).
    record_trace: bool = False
    trace_top_k: int = 8
    # Depth past which the value_model seam is consulted for leaf value.
    value_model_depth_threshold: int = 3
    # Semantic-dedup cosine threshold (only used when dedup_embedder is set).
    dedup_threshold: float = 0.90
    # Live progress cadence (used by progress_callback / observer).
    progress_every: int = 16
    # Opt-in rollout / expansion tuning (env-agnostic, duck-typed action_type).
    rollout_avoid_early_stop: bool = False
    expand_defer_stop: bool = False

    # -- Injected seams (all default None => identical to the base algorithm) --
    transition_model: Optional["TransitionModel"] = None
    policy_model: Optional["PolicyModel"] = None
    value_model: Optional["ValueModel"] = None
    observer: Optional[Callable[[str, Dict[str, Any]], None]] = None
    artifact_dir: Optional[str] = None
    dedup_embedder: Optional[Callable[[Any], Any]] = None
    # Optional cheap progress tick: called with a dict inside the loop.
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None


# ---------------------------------------------------------------------------
# Trace + Result
# ---------------------------------------------------------------------------


@dataclass
class MCTSTrace:
    """Full search trace — the distillation target for an amortized policy/value.

    ``root_policy`` is the visit-count distribution over first actions,
    ``principal_variation`` is the most-visited chain with backed-up values,
    and ``top_k_paths`` are the best complete root->leaf plans. All actions are
    stored as ``str(action)`` so the trace is JSON-serializable.
    """

    root_visits: int
    root_policy: List[Dict[str, Any]] = field(default_factory=list)
    principal_variation: List[Dict[str, Any]] = field(default_factory=list)
    top_k_paths: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root_visits": self.root_visits,
            "root_policy": self.root_policy,
            "principal_variation": self.principal_variation,
            "top_k_paths": self.top_k_paths,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass
class MCTSResult:
    """Result of a search."""

    best_action: Any
    best_value: float
    root: MCTSNode
    iterations_used: int
    confidence: float
    reasoning: str
    best_action_path: List[Any] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    simulations_run: int = 0  # alias for iterations_used (backward compat)
    trace: Optional[MCTSTrace] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class UnifiedMCTS:
    """Generic MCTS for sequential decision-making.

    The four phases (SELECT -> EXPAND -> SIMULATE -> BACKPROPAGATE) are
    domain-agnostic; domain specifics come from the :class:`MCTSEnvironment`
    adapter, and optional model behavior from the seams on :class:`MCTSConfig`.
    """

    def __init__(self, config: Optional[MCTSConfig] = None) -> None:
        self.config = config or MCTSConfig()
        # Per-search budget counter for the value_model seam.
        self._value_calls = 0

    # -- Entrypoints ------------------------------------------------------

    async def search(self, env: MCTSEnvironment) -> MCTSResult:
        """Run a fresh search from ``env``'s current state."""
        cfg = self.config
        self._value_calls = 0
        self._observe("search_started", self._start_payload(env))

        initial_actions = env.get_actions()
        if not initial_actions:
            return MCTSResult(
                best_action=None,
                best_value=0.0,
                root=MCTSNode(state_hash=env.get_state_hash()),
                iterations_used=0,
                confidence=0.0,
                reasoning="No actions available from initial state",
            )

        root = MCTSNode(
            state_hash=env.get_state_hash(),
            untried_actions=list(initial_actions[: cfg.max_branching_factor]),
        )
        return await self._search_loop(env, root)

    async def search_from_node(
        self,
        env: MCTSEnvironment,
        root_node: Optional[MCTSNode] = None,
        time_limit_ms: Optional[float] = None,
    ) -> MCTSResult:
        """Warmstart/reroot search from an existing (reused) node.

        ``time_limit_ms`` overrides the config limit for this call only — the
        shared config is never mutated.
        """
        cfg = self.config
        self._value_calls = 0

        if root_node is None:
            initial_actions = env.get_actions()
            if not initial_actions:
                return MCTSResult(
                    best_action=None,
                    best_value=0.0,
                    root=MCTSNode(state_hash=env.get_state_hash()),
                    iterations_used=0,
                    confidence=0.0,
                    reasoning="No actions available from initial state",
                )
            root_node = MCTSNode(
                state_hash=env.get_state_hash(),
                untried_actions=list(initial_actions[: cfg.max_branching_factor]),
            )

        _override_s = time_limit_ms / 1000.0 if time_limit_ms is not None else None
        self._observe("search_started", self._start_payload(env))
        return await self._search_loop(env, root_node, time_limit_override=_override_s)

    # -- Core loop --------------------------------------------------------

    async def _search_loop(
        self,
        env: MCTSEnvironment,
        root: MCTSNode,
        time_limit_override: Optional[float] = None,
    ) -> MCTSResult:
        cfg = self.config
        _time_limit = (
            time_limit_override
            if time_limit_override is not None
            else cfg.time_limit_ms / 1000.0
        )
        start_time = time.monotonic()
        best_sim_value = 0.0
        best_sim_action: Any = None

        if root.children:
            best_child = max(root.children, key=lambda c: c.visits)
            best_sim_action = best_child.action
            best_sim_value = best_child.avg_value
        else:
            initial_actions = env.get_actions()
            best_sim_action = initial_actions[0] if initial_actions else None

        _progress_cb = cfg.progress_callback
        _progress_every = max(1, cfg.progress_every or 16)

        for i in range(cfg.iterations):
            if time.monotonic() - start_time > _time_limit:
                break

            sim_env = env.clone()

            # 1. SELECT
            node = self._select(root)

            # 2. EXPAND
            if not node.terminal and not node.is_fully_expanded:
                node = self._expand(node, sim_env)

            # Replay actions from root to this node on sim_env.
            for action in node.get_action_path():
                try:
                    _obs, _r, done = sim_env.step(action)
                    if done:
                        break
                except Exception:
                    break

            # 3. SIMULATE
            value = await self._simulate_async(node, sim_env)

            if value > best_sim_value and node.action is not None:
                best_sim_value = value
                best_sim_action = node.action

            # 4. BACKPROPAGATE
            self._backpropagate(node, value)

            _completed = i + 1
            if _progress_cb and (_completed % _progress_every == 0):
                try:
                    _progress_cb(
                        {
                            "iteration": _completed,
                            "total": cfg.iterations,
                            "best_value": round(best_sim_value, 4),
                            "depth": node.depth,
                            "elapsed_ms": round(
                                (time.monotonic() - start_time) * 1000, 1
                            ),
                        }
                    )
                except Exception:
                    pass  # observability must never break the search

        elapsed = time.monotonic() - start_time

        if root.children:
            best_child = max(root.children, key=lambda c: c.visits)
            tree_action = best_child.action
            tree_value = best_child.avg_value
        else:
            tree_action = best_sim_action
            tree_value = best_sim_value

        if tree_value >= best_sim_value:
            final_action, final_value = tree_action, tree_value
        else:
            final_action, final_value = best_sim_action, best_sim_value

        confidence = self._compute_confidence(root)

        best_path: List[Any] = []
        if root.children:
            path_node = root
            while path_node.children:
                path_node = max(path_node.children, key=lambda c: c.visits)
                if path_node.action is not None:
                    best_path.append(path_node.action)

        result = MCTSResult(
            best_action=final_action,
            best_value=final_value,
            root=root,
            iterations_used=root.visits,
            confidence=confidence,
            best_action_path=best_path,
            elapsed_seconds=elapsed,
            simulations_run=root.visits,
            reasoning=(
                f"MCTS explored {root.visits} simulations in {elapsed:.2f}s. "
                f"Best action value={final_value:.3f}, confidence={confidence:.3f}."
            ),
        )

        if cfg.record_trace:
            try:
                result.trace = self._extract_trace(root, elapsed, cfg.trace_top_k)
            except Exception as exc:  # noqa: BLE001 — tracing must never break search
                log.debug("[MCTS] trace extraction failed: %s", exc)

        log.debug(
            "UnifiedMCTS: %d iters, %.2fs, value=%.3f, confidence=%.3f",
            root.visits, elapsed, final_value, confidence,
        )

        self._observe("search_complete", self._complete_payload(result, env))
        self._persist_exploration(result, env)
        return result

    # -- Phases -----------------------------------------------------------

    def _select(self, node: MCTSNode) -> MCTSNode:
        cfg = self.config
        while not node.terminal and node.is_fully_expanded and node.children:
            node = max(
                node.children,
                key=lambda c: c.uct_value(
                    cfg.exploration_weight, use_puct=cfg.use_puct, c_puct=cfg.c_puct
                ),
            )
        return node

    def _expand(self, node: MCTSNode, env: MCTSEnvironment) -> MCTSNode:
        cfg = self.config
        if not node.untried_actions:
            return node

        # Policy priors: compute once per node on its first expansion, while the
        # full action set is still intact in untried_actions (no children yet).
        if (
            cfg.policy_model is not None
            and "policy_priors" not in node.metadata
            and not node.children
        ):
            try:
                node_env = env.clone()
                for a in node.get_action_path():
                    node_env.step(a)
                acts = list(node.untried_actions)
                priors = _call_sync(cfg.policy_model.prior, node_env, acts)
                if priors and len(priors) == len(acts):
                    node.metadata["policy_priors"] = {
                        _action_key(a): float(p) for a, p in zip(acts, priors)
                    }
            except Exception:
                pass  # priors are best-effort; fall back to uniform prior=1.0

        action = node.untried_actions.pop(0)

        # Defer an env's "stop"-typed terminal action to last (opt-in).
        if cfg.expand_defer_stop and getattr(action, "action_type", None) == "stop":
            _non_stop = [
                a for a in node.untried_actions
                if getattr(a, "action_type", None) != "stop"
            ]
            if _non_stop:
                _swap = _non_stop[0]
                node.untried_actions.remove(_swap)
                node.untried_actions.append(action)
                action = _swap

        # Deprioritize previously-failed actions.
        _swap_attempts = 0
        while (
            action in node.failed_actions
            and node.untried_actions
            and _swap_attempts < 3
        ):
            node.untried_actions.append(action)
            idx = random.randrange(len(node.untried_actions))
            action = node.untried_actions.pop(idx)
            _swap_attempts += 1

        child_env = env.clone()
        try:
            for a in node.get_action_path():
                child_env.step(a)
            _obs, reward, done = child_env.step(action)
            child_state_hash = child_env.get_state_hash()
            self._record_transition(node.state_hash, action, child_state_hash, reward, done)
        except Exception:
            node.failed_actions.add(action)
            child_state_hash = hash((node.state_hash, str(action)))
            reward, done = -0.5, False
            self._record_transition(node.state_hash, action, child_state_hash, reward, done)

        if done or node.depth + 1 >= cfg.max_depth:
            child_actions: List[Any] = []
            terminal = True
        else:
            try:
                child_actions = child_env.get_actions()[: cfg.max_branching_factor]
            except Exception:
                child_actions = []
            terminal = len(child_actions) == 0

        child = MCTSNode(
            state_hash=child_state_hash,
            action=action,
            parent=node,
            untried_actions=child_actions,
            terminal=terminal,
            depth=node.depth + 1,
            prior=node.metadata.get("policy_priors", {}).get(_action_key(action), 1.0),
        )

        # Semantic dedup seam (default off): merge concept-equal siblings.
        if cfg.dedup_embedder is not None and node.children:
            try:
                child.embedding = cfg.dedup_embedder(child_env)
            except Exception:
                child.embedding = None
            existing = self._find_semantic_duplicate(child, node.children)
            if existing is not None:
                existing.visits += 1
                return existing

        node.children.append(child)
        return child

    def _record_transition(
        self, state_hash: int, action: Any, next_hash: int, reward: float, done: bool
    ) -> None:
        """Populate an online transition model, if one is injected."""
        tm = self.config.transition_model
        if tm is not None and hasattr(tm, "record"):
            try:
                tm.record(state_hash, action, next_hash, reward, done)
            except Exception:
                pass

    def _find_semantic_duplicate(
        self, candidate: MCTSNode, siblings: List[MCTSNode]
    ) -> Optional[MCTSNode]:
        if candidate.embedding is None:
            for sib in siblings:
                if sib.state_hash == candidate.state_hash:
                    return sib
            return None
        threshold = self.config.dedup_threshold
        for sib in siblings:
            if sib.embedding is None:
                continue
            if _cosine(candidate.embedding, sib.embedding) >= threshold:
                return sib
        return None

    async def _simulate_async(self, node: MCTSNode, env: MCTSEnvironment) -> float:
        """Async simulate: consult the (possibly-async) value_model past the
        depth gate, else fall through to the sync rollout."""
        cfg = self.config
        if cfg.value_model is not None and node.depth > cfg.value_model_depth_threshold:
            budget = cfg.max_llm_calls_per_search or 0
            if not budget or self._value_calls < budget:
                try:
                    self._value_calls += 1
                    v = await _call_async(cfg.value_model.value, env)
                    if v is not None:
                        return _clamp01(float(v))
                except Exception as exc:  # noqa: BLE001 — must never break search
                    log.debug("[MCTS] value_model failed, falling back: %s", exc)
        return self._simulate(node, env)

    def _simulate(self, node: MCTSNode, env: MCTSEnvironment) -> float:
        """Heuristic / model-based rollout to estimate leaf value.

        The (possibly-async) value_model path lives in :meth:`_simulate_async`;
        here a *sync* value_model is honored for direct callers/tests, then the
        rollout runs (via ``transition_model.step`` when present, else
        ``env.clone().step``).
        """
        cfg = self.config

        if cfg.value_model is not None and node.depth > cfg.value_model_depth_threshold:
            v = _call_sync(cfg.value_model.value, env)
            if v is not None:
                return _clamp01(float(v))

        try:
            value = env.evaluate()
        except Exception:
            value = 0.0

        sim_env = env.clone()
        state_hash = sim_env.get_state_hash()
        total_reward = 0.0
        steps = 0

        for _ in range(cfg.simulation_depth):
            try:
                actions = sim_env.get_actions()
            except Exception:
                break
            if not actions:
                break

            if cfg.rollout_avoid_early_stop and len(actions) > 1:
                _non_stop = [
                    a for a in actions if getattr(a, "action_type", None) != "stop"
                ]
                action = (
                    random.choice(_non_stop)
                    if _non_stop and random.random() < 0.85
                    else random.choice(actions)
                )
            else:
                action = random.choice(actions)

            try:
                state_hash, reward, done = self._rollout_step(sim_env, state_hash, action)
                total_reward += reward
                steps += 1
                if done:
                    break
            except Exception:
                total_reward -= 0.3
                break

        try:
            final_eval = sim_env.evaluate()
        except Exception:
            final_eval = value

        if steps > 0:
            avg_rollout = total_reward / steps
            blended = 0.4 * avg_rollout + 0.6 * final_eval
        else:
            blended = final_eval

        return _clamp01(blended)

    def _rollout_step(
        self, sim_env: MCTSEnvironment, state_hash: int, action: Any
    ) -> Tuple[int, float, bool]:
        """Advance one rollout step. Uses the transition_model when present
        (keeping the env in sync for action generation), else steps the env."""
        tm = self.config.transition_model
        if tm is not None:
            out = _call_sync(tm.step, state_hash, action)
            if out is not None:
                stepped = False
                try:
                    sim_env.step(action)
                    stepped = True
                except Exception:
                    pass
                nxt = sim_env.get_state_hash() if stepped else out[0]
                return nxt, float(out[1]), bool(out[2])
        _obs, reward, done = sim_env.step(action)
        return sim_env.get_state_hash(), reward, done

    def _backpropagate(self, node: MCTSNode, reward: float) -> None:
        current: Optional[MCTSNode] = node
        while current is not None:
            current.visits += 1
            current.value_sum += reward
            current = current.parent

    # -- Trace / confidence ----------------------------------------------

    def _extract_trace(self, root: MCTSNode, elapsed: float, top_k: int) -> MCTSTrace:
        total = max(1, root.visits)

        root_policy = [
            {
                "action": str(c.action),
                "visits": c.visits,
                "avg_value": round(c.avg_value, 4),
                "prob": round(c.visits / total, 4),
            }
            for c in sorted(root.children, key=lambda c: c.visits, reverse=True)
        ]

        principal_variation: List[Dict[str, Any]] = []
        node = root
        while node.children:
            node = max(node.children, key=lambda c: c.visits)
            principal_variation.append(
                {
                    "depth": node.depth,
                    "action": str(node.action),
                    "avg_value": round(node.avg_value, 4),
                    "visits": node.visits,
                }
            )

        paths: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        def _collect(n: MCTSNode) -> None:
            if not n.children:
                actions = [str(a) for a in n.get_action_path()]
                key = "|".join(actions)
                if actions and key not in seen:
                    seen.add(key)
                    paths.append(
                        {
                            "actions": actions,
                            "value": round(n.avg_value, 4),
                            "visits": n.visits,
                        }
                    )
                return
            for ch in n.children:
                _collect(ch)

        _collect(root)
        paths.sort(key=lambda p: p["value"], reverse=True)

        return MCTSTrace(
            root_visits=root.visits,
            root_policy=root_policy,
            principal_variation=principal_variation,
            top_k_paths=paths[: max(1, top_k)],
            elapsed_seconds=round(elapsed, 4),
        )

    @staticmethod
    def _compute_confidence(root: MCTSNode) -> float:
        """Confidence from visit-distribution entropy (one clear path => high)."""
        if not root.children or root.visits < 2:
            return 0.0
        visit_counts = [c.visits for c in root.children]
        total = sum(visit_counts)
        if total == 0:
            return 0.0
        probs = [v / total for v in visit_counts if v > 0]
        max_entropy = math.log(len(probs)) if len(probs) > 1 else 1.0
        entropy = -sum(p * math.log(p) for p in probs) if probs else 0.0
        if max_entropy == 0:
            return 1.0
        return _clamp01(1.0 - entropy / max_entropy)

    # -- Observer / persistence seams ------------------------------------

    def _start_payload(self, env: MCTSEnvironment) -> Dict[str, Any]:
        cfg = self.config
        return {
            "domain": type(env).__name__,
            "max_iterations": cfg.iterations,
            "time_limit_ms": cfg.time_limit_ms,
            "max_depth": cfg.max_depth,
            "exploration_weight": cfg.exploration_weight,
            "use_puct": cfg.use_puct,
        }

    @staticmethod
    def _complete_payload(result: MCTSResult, env: MCTSEnvironment) -> Dict[str, Any]:
        return {
            "domain": type(env).__name__,
            "iterations": result.iterations_used,
            "best_value": result.best_value,
            "confidence": result.confidence,
            "elapsed_seconds": result.elapsed_seconds,
            "tree_depth": _max_tree_depth(result.root),
            "best_action": str(result.best_action) if result.best_action else None,
            "path_length": len(result.best_action_path),
        }

    def _observe(self, event: str, payload: Dict[str, Any]) -> None:
        obs = self.config.observer
        if obs is None:
            return
        try:
            obs(event, payload)
        except Exception as exc:  # noqa: BLE001 — observer must never break search
            log.debug("[MCTS] observer(%s) failed: %s", event, exc)

    def _persist_exploration(self, result: MCTSResult, env: MCTSEnvironment) -> None:
        """Append an exploration record to ``artifact_dir`` (default None: no-op)."""
        artifact_dir = self.config.artifact_dir
        if not artifact_dir:
            return
        try:
            import json
            from pathlib import Path

            _dir = Path(artifact_dir) / "mcts_explorations"
            _dir.mkdir(parents=True, exist_ok=True)
            _file = _dir / f"{time.strftime('%Y-%m-%d')}.jsonl"
            record = {
                "ts": time.time(),
                "domain": type(env).__name__,
                "iterations": result.iterations_used,
                "best_value": result.best_value,
                "confidence": result.confidence,
                "elapsed_seconds": round(result.elapsed_seconds, 4),
                "best_action": str(result.best_action) if result.best_action else None,
                "path_length": len(result.best_action_path),
                "tree_depth": _max_tree_depth(result.root),
            }
            with open(str(_file), "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:  # noqa: BLE001 — persistence must never break search
            log.debug("[MCTS] exploration persist failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


async def search(
    env: MCTSEnvironment, config: Optional[MCTSConfig] = None
) -> MCTSResult:
    """Convenience: run a fresh :class:`UnifiedMCTS` search over ``env``."""
    return await UnifiedMCTS(config).search(env)


__all__ = [
    "MCTSConfig",
    "MCTSNode",
    "MCTSReroot",
    "MCTSResult",
    "MCTSTrace",
    "UnifiedMCTS",
    "search",
]
