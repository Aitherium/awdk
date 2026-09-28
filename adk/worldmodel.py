"""Native world-model bootstrapping for ADK agents.

Each agent gets a self-initializing, self-training world model as a lifecycle faculty.
It loads its last checkpoint on construction, records one transition per tool each turn,
promotes itself cold -> warm -> trained as data accumulates, and (when enabled) advises
on tool ordering.

The module is disabled by default (env AITHER_AGENT_WM unset). With AITHER_AGENT_WM=learn,
shadow, or steer, agents begin bootstrapping their own world models in ~/.aither/wm/.
No external state is touched; every exception is caught and logged.

HARD CONSTRAINTS:
  1. No AitherOS imports at module scope — only lazy + try/except inside functions.
  2. Default OFF — AITHER_AGENT_WM unset -> get_world_model() returns None, no files created.
  3. Pure stdlib builtin backend — no numpy, torch, or new dependencies.
  4. Every public method exception-safe — no exception propagates to agent.chat().
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger("adk.worldmodel")

# ============================================================================
# Constants & Configuration
# ============================================================================

MODE_OFF = "off"
MODE_LEARN = "learn"
MODE_SHADOW = "shadow"
MODE_STEER = "steer"

# Stage progression: n < WARM_MIN -> "cold"; n < TRAINED_MIN -> "warm"; else "trained"
WARM_MIN = int(os.environ.get("AITHER_AGENT_WM_WARM_MIN", "50"))
TRAINED_MIN = int(os.environ.get("AITHER_AGENT_WM_TRAINED_MIN", "200"))
TRAIN_EVERY = int(os.environ.get("AITHER_AGENT_WM_TRAIN_EVERY", "25"))
MAX_BUFFER = int(os.environ.get("AITHER_AGENT_WM_MAX_BUFFER", "20000"))

# Default goal: prioritize success, recall, and error avoidance
DEFAULT_GOAL = {
    "success": 1.0,
    "recall": 0.3,
    "errors": -1.0,
    "latency": -0.2,
    "tokens": -0.1,
}

# State dimension names — observed and scored by the learner
STATE_DIMS = ["tools", "errors", "latency", "tokens", "recall", "depth", "novelty", "success"]


# ============================================================================
# Redaction -- the ONE rule for what a transition may carry off this process
# ============================================================================
#
# record() accepts any non-empty string as an action and never bounds-checks the
# vectors it stores, so an action named after a client file, or a float with enough
# precision to smuggle text, is reachable from the ordinary recording path. Every
# surface that persists or exports transition text goes through these helpers:
# BuiltinWorldModel.export_redacted_transitions (federation) and the awm backend in
# adk/world.py (on-disk dynamics). Tests: adk/tests/test_worldmodel_redaction.py,
# tests/test_world_backend.py.

#: The only keys an exported transition may carry. A key added to the in-memory row
#: later does NOT ride along.
EXPORT_KEYS = ("action", "state_before", "state_after", "delta", "ok")

#: Prefix of a hashed (non-whitelisted) action name. Stable, so a tabular model can
#: still learn from it; content-free, so nothing about the name leaves.
REDACTED_PREFIX = "redacted:"


def _parse_allowed(allowed_actions: Any) -> frozenset | None:
    if allowed_actions is None:
        return None
    if isinstance(allowed_actions, str):
        allowed_actions = [allowed_actions]
    try:
        return frozenset(str(a).strip() for a in allowed_actions if str(a).strip())
    except TypeError:
        return frozenset()


def allowed_actions_from_env() -> frozenset | None:
    """AITHER_AGENT_WM_ALLOWED_ACTIONS (comma list), or None when unset."""
    raw = os.environ.get("AITHER_AGENT_WM_ALLOWED_ACTIONS")
    if raw is None:
        return None
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


#: Env override for the redaction key (e.g. one key shared by an HA pair).
REDACTION_SALT_ENV = "AITHER_AGENT_WM_REDACTION_SALT"
#: Per-install secret key file under the world-model root (32 random bytes, mode 600).
REDACTION_SALT_FILE = ".redaction_salt"
#: The awm backend's checkpoint suffix (``<agent_id>.awm.json``); the builtin owns
#: ``<agent_id>.wm.json``. Separate files, so switching backends never overwrites one.
AWM_CKPT_SUFFIX = ".awm.json"
#: A salt (file or env) shorter than this is not a key: a file is replaced, an env
#: value is refused (logged, counted in ``SALT_TELEMETRY``) and the file key used.
SALT_MIN_BYTES = 16
#: Counters a host can read: ``env_rejected`` = times a too-short env salt was refused.
SALT_TELEMETRY: dict = {"env_rejected": 0}
_SALTS: dict = {}
_SALT_LOCK = threading.Lock()


def redaction_salt(root: str | None = None) -> bytes:
    """The per-install secret that keys ``redacted:<token>``. Never raises.

    ``AITHER_AGENT_WM_REDACTION_SALT`` if set and at least ``SALT_MIN_BYTES`` long
    (a shorter one is refused, logged and counted); else ``<root>/.redaction_salt``
    (``root`` defaults to ``wm_root()``), created once with 32 random bytes. An
    unsalted sha256 of a low-entropy tool name is reversible by dictionary
    ("acme_payroll_export" was); an HMAC under a secret the reader does not hold is
    not. When the file cannot be read or written the salt is process-local random
    (logged): tokens then stay unlinkable, only not stable across processes.
    """
    env = os.environ.get(REDACTION_SALT_ENV, "")
    if env.strip():
        raw = env.strip().encode("utf-8")
        if len(raw) >= SALT_MIN_BYTES:
            return raw
        # The same floor as the file: an HMAC under a 1-byte key is reversible by
        # dictionary. Refused loudly, never adopted; the per-install file key is used.
        with _SALT_LOCK:
            SALT_TELEMETRY["env_rejected"] = SALT_TELEMETRY.get("env_rejected", 0) + 1
        logger.warning("[WM] %s is %d bytes, under the %d-byte floor; ignoring it and "
                       "using the per-install salt file", REDACTION_SALT_ENV, len(raw),
                       SALT_MIN_BYTES)
    path = Path(root or wm_root()) / REDACTION_SALT_FILE
    key = str(path.resolve()) if path.parent.exists() else str(path)
    with _SALT_LOCK:
        hit = _SALTS.get(key)
        if hit is not None:
            return hit
        salt = b""
        try:
            existed = path.is_file()
            if existed:
                salt = path.read_bytes()
            if len(salt) < SALT_MIN_BYTES:
                path.parent.mkdir(parents=True, exist_ok=True)
                salt = secrets.token_bytes(32)
                tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(salt)
                if existed:
                    # A short (e.g. empty) salt is not a key: an HMAC under b"" is
                    # reversible by dictionary. Replaced, never adopted.
                    logger.warning("[WM] redaction salt at %s is %d bytes; replacing it",
                                   path, len(path.read_bytes()) if path.is_file() else 0)
                    os.replace(str(tmp), str(path))
                    tmp = None  # type: ignore[assignment]
                else:
                    try:
                        # A concurrent creator may have won: keep ITS salt, not ours
                        # -- if it is a real one.
                        os.link(str(tmp), str(path))
                    except FileExistsError:
                        won = path.read_bytes()
                        if len(won) >= SALT_MIN_BYTES:
                            salt = won
                        else:
                            os.replace(str(tmp), str(path))
                            tmp = None  # type: ignore[assignment]
                    except OSError:
                        os.replace(str(tmp), str(path))
                        tmp = None  # type: ignore[assignment]
                if tmp is not None and Path(tmp).exists():
                    os.unlink(str(tmp))
        except OSError as exc:
            logger.warning("[WM] redaction salt unavailable at %s (%s): using a "
                           "process-local key; redacted tokens are not stable across "
                           "processes", path, exc)
            salt = secrets.token_bytes(32)
        _SALTS[key] = salt
        return salt


def redaction_token(name: str, salt: bytes) -> str:
    """``redacted:<hmac-sha256(salt, name)[:12]>``."""
    return REDACTED_PREFIX + hmac.new(salt, name.encode("utf-8"),
                                      hashlib.sha256).hexdigest()[:12]


def redact_action(action: Any, allowed_actions: Any = None, *, on_miss: str = "drop",
                  salt: bytes | None = None) -> str | None:
    """Action text as it may be persisted or exported. Never raises.

    A whitelisted name passes verbatim. Anything else is dropped (``on_miss="drop"``,
    the export rule: fail closed) or replaced by ``redacted:<hmac[:12]>``
    (``on_miss="hash"``, the persistence rule: same name + same install -> same
    token; keyed by ``redaction_salt()`` unless ``salt`` is given, so the token is
    not reversible by hashing a dictionary of likely tool names).
    No whitelist at all (None or empty) whitelists nothing.
    """
    if not isinstance(action, str):
        return None
    name = action.strip()
    if not name:
        return None
    allowed = _parse_allowed(allowed_actions)
    if allowed and name in allowed:
        return name
    if on_miss == "hash":
        try:
            key = salt if salt is not None else redaction_salt()
            return redaction_token(name, key)
        except Exception:  # noqa: BLE001 -- never raises; fail closed
            return None
    return None


def bounded_vector(vec: Any, dim: int | None = None) -> list[float] | None:
    """``vec`` as a list of finite floats in [0, 1] (length ``dim`` if given), else None.

    Unbounded numbers are a covert channel; a vector that fails is refused whole,
    never clamped into looking valid.
    """
    if not isinstance(vec, list) or not vec:
        return None
    if dim is not None and len(vec) != dim:
        return None
    out: list[float] = []
    for v in vec:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        f = float(v)
        if math.isnan(f) or not 0.0 <= f <= 1.0:
            return None
        out.append(f)
    return out


def redact_transition(row: Any, allowed_actions: Any, dim: int | None = None) -> dict | None:
    """One transition reduced to EXPORT_KEYS, or None when any part fails. Never raises."""
    try:
        if not isinstance(row, dict):
            return None
        action = redact_action(row.get("action"), allowed_actions, on_miss="drop")
        if action is None:
            return None
        before = bounded_vector(row.get("state_before"), dim)
        after = bounded_vector(row.get("state_after"), dim)
        if before is None or after is None or len(before) != len(after):
            return None
        ok = row.get("ok")
        if not isinstance(ok, bool):
            return None
        # Recomputed, never copied: a stored delta is one more unbounded field.
        delta = [a - b for a, b in zip(after, before)]
        return {"action": action, "state_before": before, "state_after": after,
                "delta": delta, "ok": ok}
    except Exception:  # noqa: BLE001 -- an export must never break a turn
        return None


# ============================================================================
# Module-level Functions
# ============================================================================

def wm_mode() -> str:
    """Read AITHER_AGENT_WM env var; return normalized mode or MODE_OFF."""
    mode = os.environ.get("AITHER_AGENT_WM", "").lower().strip()
    if mode in (MODE_LEARN, MODE_SHADOW, MODE_STEER):
        return mode
    return MODE_OFF


def wm_agent_id(agent_name: str) -> str:
    """Normalize agent name to canonical id: EXACTLY lib/cognitive/agent_action_feed.py agent_id_of.

    Examples:
      "AitherAgent" -> "agent.aither"
      "Atlas Agent" -> "agent.atlas"
      "iris" -> "agent.iris"

    The rule is the host's, letter for letter, and must stay so: this id names the
    checkpoint files on disk (``<id>.wm.json``) and is the ``agent_id`` the host's
    FleetWorldModel and its action feeds write into one shared corpus. A "better" rule
    here orphans every existing checkpoint and splits the join. Its known collision
    ("Reagent" and "Re" both -> ``agent.re``) is the host's too; changing it means
    changing both at once, with a checkpoint migration. It is NOT idempotent on an id
    (``agent.atlas`` -> ``agent..atlas``): code holding an id passes ``agent_id=``.
    """
    slug = (agent_name or "unknown").lower().replace("agent", "").replace(" ", "-").strip("-")
    return "agent." + (slug or "unknown")


def wm_root() -> str:
    """Return the root directory for world-model checkpoints and transitions."""
    root = os.environ.get("AITHER_AGENT_WM_DIR")
    if root:
        return root
    return os.path.join(os.path.expanduser("~"), ".aither", "wm")


# ============================================================================
# WorldModelBackend Protocol
# ============================================================================

class WorldModelBackend(Protocol):
    """Interface for world-model backends."""

    agent_id: str

    def observe(self, context: dict | None = None) -> list[float] | None:
        """Build a state vector from turn context. Returns None on error; never raises."""
        ...

    def record(self, state_before: Any, action: str, state_after: Any, ok: bool = True) -> None:
        """Record a (state, action, state_after) transition. Never raises."""
        ...

    def bootstrap(self) -> str:
        """Promote stage (cold -> warm -> trained), refit if due. Returns stage. Never raises."""
        ...

    def advise(self, state: Any, candidates: list[str]) -> dict | None:
        """Score candidate actions for a state. Returns None in cold stage or when nothing to say."""
        ...

    def save(self) -> None:
        """Persist checkpoint + transitions to disk. Never raises."""
        ...

    def load(self) -> None:
        """Load checkpoint + transitions from disk. Never raises."""
        ...

    def stats(self) -> dict:
        """Return {'agent_id','backend','stage','n','actions','state_dim','last_trained_n','goal'}."""
        ...


# ============================================================================
# Registry & Factory
# ============================================================================

_registry_factory: Callable[[str], WorldModelBackend] | None = None
_registry_instances: dict[str, WorldModelBackend] = {}


def register_world_model(factory: Callable[[str], WorldModelBackend]) -> None:
    """Override the builtin backend with a custom factory. Idempotent."""
    global _registry_factory
    _registry_factory = factory


def registered_backend_name() -> str | None:
    """Return the registered backend's name, or None when the builtin is in use.

    A backend advertises its own name via a class-level ``backend_name`` attribute
    (falling back to the class name) so ``adk wm status`` reports what is ACTUALLY
    serving rather than a generic "custom".
    """
    if _registry_factory is None:
        return None
    try:
        name = getattr(_registry_factory, "backend_name", None)
        if isinstance(name, str) and name:
            return name
        return getattr(_registry_factory, "__name__", "custom")
    except Exception:
        return "custom"


#: Module path a host platform may provide to install its own backend. It must
#: self-register (call register_world_model) at import time. Overridable via
#: AITHER_AGENT_WM_BACKEND; set it empty to disable the autoload entirely.
HOST_BACKEND_MODULE = "lib.cognitive.adk_wm_backend"

#: AITHER_AGENT_WM_BACKEND value that selects adk.world.AwmWorldModelBackend.
AWM_BACKEND_NAME = "awm"

_autoload_attempted = False


def _autoload_host_backend() -> None:
    """Try ONCE to import a host-provided backend module so it can self-register.

    Silent and non-fatal by design: on a machine with no host platform the import
    simply fails and the builtin is used. Never raises.
    """
    global _autoload_attempted
    if _autoload_attempted or _registry_factory is not None:
        return
    _autoload_attempted = True
    module = os.environ.get("AITHER_AGENT_WM_BACKEND", HOST_BACKEND_MODULE).strip()
    if not module:
        return
    if module.lower() == AWM_BACKEND_NAME:
        # The awm-backed tabular dynamics model (adk/world.py). Selected by NAME, not
        # module path. It degrades inside the backend (stats() says why) when awm is
        # missing or the file needs migration, so selecting it is never silent.
        try:
            from adk.world import register_awm_backend
            register_awm_backend()
            logger.info("World-model backend 'awm' selected by AITHER_AGENT_WM_BACKEND")
        except Exception as e:  # noqa: BLE001
            logger.warning("AITHER_AGENT_WM_BACKEND=awm but adk.world failed to load (%s); "
                           "using builtin", e)
        return
    try:
        import importlib
        importlib.import_module(module)
        if _registry_factory is not None:
            logger.info("World-model backend '%s' installed from %s",
                        registered_backend_name(), module)
    except Exception as e:  # noqa: BLE001
        logger.debug("No host world-model backend at %s (%s); using builtin", module, e)


def get_world_model(agent_name: str) -> WorldModelBackend | None:
    """Get or create a cached world-model instance for the agent.

    Returns None if wm_mode()==MODE_OFF. Otherwise builds via the registered factory
    (falling back to BuiltinWorldModel on any exception), caches one instance per agent_id,
    and returns it. Never raises.
    """
    mode = wm_mode()
    if mode == MODE_OFF:
        return None

    agent_id = wm_agent_id(agent_name)
    if agent_id in _registry_instances:
        return _registry_instances[agent_id]

    # Give a host platform one chance to install a richer backend before we fall back to
    # the builtin. AitherOS ships lib.cognitive.adk_wm_backend, which self-registers an
    # affect-grounded, cross-agent-corpus model on import. The lazy, guarded import is the
    # same decoupling pattern the agent-action feed uses: adk never depends on AitherOS,
    # it just notices when it is there. Without this the fleet backend would sit unused in
    # production, because nothing else imports it.
    _autoload_host_backend()

    wm: WorldModelBackend | None = None
    try:
        if _registry_factory is not None:
            wm = _registry_factory(agent_name)
        else:
            wm = BuiltinWorldModel(agent_name)

        if wm is not None:
            wm.load()  # load prior checkpoint if it exists
            _registry_instances[agent_id] = wm
            logger.debug("Initialized world model for %s (mode=%s)", agent_id, mode)
    except Exception as e:
        logger.debug("Failed to create world model for %s: %s; falling back to BuiltinWorldModel", agent_name, e)
        try:
            wm = BuiltinWorldModel(agent_name)
            wm.load()
            _registry_instances[agent_id] = wm
        except Exception as e2:
            logger.warning("Failed to create fallback BuiltinWorldModel for %s: %s", agent_name, e2)
            return None

    return wm


def clear_world_model_registry() -> None:
    """Clear the registry, the instance cache and the autoload latch. Test hook."""
    global _registry_factory, _registry_instances, _autoload_attempted
    _registry_factory = None
    _autoload_attempted = False
    _registry_instances.clear()


# ============================================================================
# BuiltinWorldModel — Pure-stdlib diagonal-affine residual learner
# ============================================================================

class BuiltinWorldModel:
    """Pure-stdlib world model backend.

    Learns a per-action diagonal-affine residual model: pred_i = x_i + b[a]_i + w[a]_i * x_i
    where b (bias) and w (gain) are per-action vectors fit by SGD over a fixed-size buffer.

    Persistence:
      <agent_id>.wm.json — checkpoint (version, agent_id, state_dim, counts, action stats, weights, goal)
      <agent_id>.transitions.jsonl — append-only ring buffer (rewrite-truncate when > MAX_BUFFER)
    """

    #: How this backend identifies itself in stats()/advise() and in `adk wm status`.
    #: Subclasses (e.g. AitherOS's FleetWorldModel) override it so the CLI reports
    #: what is ACTUALLY serving rather than always claiming "builtin".
    backend_name = "builtin"

    def __init__(self, agent_name: str, root: str | None = None, *,
                 agent_id: str | None = None) -> None:
        # `agent_id` = an id already canonical (the CLI holds ids, not names).
        self.agent_id = agent_id or wm_agent_id(agent_name)
        self.agent_name = agent_name
        self._root = root or wm_root()
        self._stage = "cold"
        self._n = 0  # total transitions recorded
        self._last_trained_n = 0
        self._state_dim = len(STATE_DIMS)
        self._state_dims = STATE_DIMS[:]  # snapshot at init

        # Per-action learned coefficients: action -> (bias_vec, gain_vec)
        # where bias_vec and gain_vec are lists of floats, one per state dim
        self._bias: dict[str, list[float]] = {}
        self._gain: dict[str, list[float]] = {}

        # Per-action online statistics: action -> {count, sum_delta_per_dim}
        self._action_stats: dict[str, dict[str, Any]] = {}

        # Goal: {dim_name: weight} for scoring
        self._goal: dict[str, float] = DEFAULT_GOAL.copy()
        self._load_goal_from_env()

        # Transitions ring buffer (in-memory)
        self._transitions: list[dict] = []

        # Filesystem paths
        self._ckpt_path = Path(self._root) / f"{self.agent_id}.wm.json"
        self._trans_path = Path(self._root) / f"{self.agent_id}.transitions.jsonl"

    def _load_goal_from_env(self) -> None:
        """Load goal weights from AITHER_AGENT_WM_GOAL env var (JSON)."""
        goal_str = os.environ.get("AITHER_AGENT_WM_GOAL")
        if goal_str:
            try:
                goal = json.loads(goal_str)
                if isinstance(goal, dict):
                    self._goal.update({str(k): float(v) for k, v in goal.items()})
                    logger.debug("Loaded goal from env for %s: %s", self.agent_id, self._goal)
            except Exception as e:
                logger.debug("Failed to parse AITHER_AGENT_WM_GOAL: %s", e)

    def set_goal(self, goal: dict[str, float]) -> None:
        """Update the goal dict. Dims are matched by name."""
        self._goal.update(goal)

    @property
    def state_dims(self) -> list[str]:
        """Return the state dimension names."""
        return self._state_dims[:]

    def observe(self, context: dict | None = None) -> list[float] | None:
        """Build an 8-dim state vector from turn context dict.

        STATE_DIMS = ["tools", "errors", "latency", "tokens", "recall", "depth", "novelty", "success"]
        All normalized roughly to [0,1] and clamped. Missing keys -> 0.0. Never raises.
        """
        if context is None:
            context = {}

        state = []
        try:
            # Clamp to [0, 1]
            def clamp(x: Any, lo: float = 0.0, hi: float = 1.0) -> float:
                try:
                    v = float(x)
                except (TypeError, ValueError):
                    return 0.0
                return max(lo, min(hi, v))

            # tools: count of tool calls available (normalized by a max of 20)
            state.append(clamp(context.get("tools", 0) / 20.0))

            # errors: error rate (fraction of tool calls that failed)
            errors = context.get("errors", 0.0)
            state.append(clamp(errors))

            # latency: turn latency in seconds (normalized; assume max 30s)
            latency = context.get("latency", 0.0) / 30.0
            state.append(clamp(latency))

            # tokens: cumulative tokens used (normalized; assume max 100k)
            tokens = context.get("tokens", 0.0) / 100000.0
            state.append(clamp(tokens))

            # recall: retrieval quality (0.0-1.0 confidence)
            recall = context.get("recall", 0.0)
            state.append(clamp(recall))

            # depth: reasoning depth (number of steps, normalized by max 20)
            depth = context.get("depth", 0.0) / 20.0
            state.append(clamp(depth))

            # novelty: action novelty (0.0-1.0, new actions higher)
            novelty = context.get("novelty", 0.0)
            state.append(clamp(novelty))

            # success: success indicator (0.0-1.0 confidence)
            success = context.get("success", 0.0)
            state.append(clamp(success))

            return state
        except Exception as e:
            logger.debug("observe() failed for %s: %s", self.agent_id, e)
            return None

    def record(self, state_before: Any, action: str, state_after: Any, ok: bool = True) -> None:
        """Record a transition: (state_before, action, state_after).

        Appends to the in-memory ring buffer and updates online statistics.
        Never raises.
        """
        try:
            if not isinstance(state_before, list) or not isinstance(state_after, list):
                return
            if len(state_before) != self._state_dim or len(state_after) != self._state_dim:
                logger.debug(
                    "record(): dimension mismatch for %s (expected %d, got %d/%d)",
                    self.agent_id, self._state_dim, len(state_before), len(state_after),
                )
                return
            if not isinstance(action, str) or not action.strip():
                return

            action = action.strip()

            # Compute delta
            delta = [state_after[i] - state_before[i] for i in range(self._state_dim)]

            # Append transition
            trans = {
                "action": action,
                "state_before": state_before,
                "state_after": state_after,
                "delta": delta,
                "ok": bool(ok),
            }
            self._transitions.append(trans)

            # Manage ring buffer
            if len(self._transitions) > MAX_BUFFER:
                self._transitions = self._transitions[-MAX_BUFFER:]

            # Update online stats
            if action not in self._action_stats:
                self._action_stats[action] = {
                    "count": 0,
                    "sum_delta": [0.0] * self._state_dim,
                }
            stats = self._action_stats[action]
            stats["count"] += 1
            for i in range(self._state_dim):
                stats["sum_delta"][i] += delta[i]

            self._n += 1
            logger.debug("Recorded transition %d for %s: action=%s, ok=%s", self._n, self.agent_id, action, ok)
        except Exception as e:
            logger.debug("record() failed for %s: %s", self.agent_id, e)

    def bootstrap(self) -> str:
        """Promote stage and refit if due. Return the new stage. Never raises."""
        try:
            # Promote stage
            if self._n < WARM_MIN:
                self._stage = "cold"
            elif self._n < TRAINED_MIN:
                self._stage = "warm"
            else:
                self._stage = "trained"

            # Refit in warm/trained stages when TRAIN_EVERY new records accumulate
            if self._stage in ("warm", "trained") and (self._n - self._last_trained_n) >= TRAIN_EVERY:
                self._fit()
                self._last_trained_n = self._n

            # Periodically save checkpoint
            if self._n % 50 == 0:
                self.save()

            logger.debug("bootstrap() for %s: stage=%s, n=%d", self.agent_id, self._stage, self._n)
            return self._stage
        except Exception as e:
            logger.debug("bootstrap() failed for %s: %s", self.agent_id, e)
            return self._stage

    def _fit(self) -> None:
        """Fit the diagonal-affine model to the current transitions buffer.

        Warm stage: fit bias only (per-action EMA delta, requires >=3 observations).
        Trained stage: fit bias and gain together via plain SGD.

        Never raises.
        """
        try:
            if self._stage == "warm":
                self._fit_warm()
            elif self._stage == "trained":
                self._fit_trained()
        except Exception as e:
            logger.debug("_fit() failed for %s: %s", self.agent_id, e)

    def _fit_warm(self) -> None:
        """Fit bias vectors only (warm stage). EMA of observed deltas."""
        for action, stats in self._action_stats.items():
            count = stats["count"]
            if count < 3:
                continue

            # Bias = average observed delta for this action
            bias = [stats["sum_delta"][i] / count for i in range(self._state_dim)]
            self._bias[action] = bias
            # No gain in warm stage
            if action in self._gain:
                del self._gain[action]

            logger.debug(
                "Fit warm bias for %s/%s: count=%d, bias=%.3f...",
                self.agent_id, action, count, bias[0] if bias else 0.0,
            )

    def _fit_trained(self) -> None:
        """Fit both bias and gain via plain SGD (trained stage).

        Model: pred_i = x_i + b[a]_i + w[a]_i * x_i
        Residual loss: sum_i (delta_i - b[a]_i - w[a]_i * x_i)^2

        Uses a few epochs of SGD (deterministic order, no RNG) with learning rate ~0.05.
        """
        if not self._transitions:
            return

        # Initialize weights if not present
        for action in self._action_stats.keys():
            if action not in self._bias:
                self._bias[action] = [0.0] * self._state_dim
            if action not in self._gain:
                self._gain[action] = [0.0] * self._state_dim

        lr = 0.05  # learning rate
        epochs = 3

        for epoch in range(epochs):
            for trans in self._transitions:
                action = trans["action"]
                if action not in self._bias:
                    self._bias[action] = [0.0] * self._state_dim
                    self._gain[action] = [0.0] * self._state_dim

                x = trans["state_before"]
                delta = trans["delta"]

                # Gradient for bias and gain
                for i in range(self._state_dim):
                    pred_delta = self._bias[action][i] + self._gain[action][i] * x[i]
                    residual = delta[i] - pred_delta

                    # Update bias: grad_b = -residual
                    self._bias[action][i] += lr * residual

                    # Update gain: grad_w = -residual * x
                    self._gain[action][i] += lr * residual * x[i]

        logger.debug(
            "Fit trained %s/%d actions via SGD (%d epochs) for %s",
            len(self._bias), len(self._action_stats), epochs, self.agent_id,
        )

    def predict(self, state: Any, action: str) -> list[float] | None:
        """Predict the next state for (state, action) under the learned model.

            pred_i = x_i + b[a]_i + w[a]_i * x_i

        Returns None for an unknown action, a dim mismatch, or a non-list state --
        never a silently-wrong answer. This is the same predictor advise() scores
        with, exposed publicly so prediction error can actually be MEASURED against
        a do-nothing baseline instead of asserted. Never raises.
        """
        try:
            if not isinstance(state, list) or len(state) != self._state_dim:
                return None
            if action not in self._bias:
                return None
            bias = self._bias[action]
            gain = self._gain.get(action)
            pred = list(state)
            for i in range(self._state_dim):
                g = gain[i] if (gain is not None and i < len(gain)) else 0.0
                pred[i] += bias[i] + g * float(state[i])
            return pred
        except Exception:
            return None

    def advise(self, state: Any, candidates: list[str]) -> dict | None:
        """Score candidate actions for a state.

        Returns None in cold stage or if fewer than 2 candidates are known.
        Otherwise returns {'stage', 'scores', 'order', 'n', 'backend'}.

        Never raises.
        """
        try:
            if self._stage == "cold" or not isinstance(state, list):
                return None

            if len(state) != self._state_dim:
                logger.debug("advise(): state dim mismatch (%d vs %d)", len(state), self._state_dim)
                return None

            if not candidates:
                return None

            # Score only known actions
            scores = {}
            for action in candidates:
                pred = self.predict(state, action)
                if pred is None:
                    continue  # Unknown actions omitted, NOT scored 0

                # Compute goal score: dot(goal_weights, (pred - state))
                score = 0.0
                for i, dim in enumerate(self._state_dims):
                    weight = self._goal.get(dim, 0.0)
                    delta = pred[i] - state[i]
                    score += weight * delta

                scores[action] = score

            if len(scores) < 2:
                return None

            # Sort by score (highest first)
            sorted_actions = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            order = [a for a, _ in sorted_actions]

            return {
                "stage": self._stage,
                "scores": scores,
                "order": order,
                "n": self._n,
                "backend": self.backend_name,
            }
        except Exception as e:
            logger.debug("advise() failed for %s: %s", self.agent_id, e)
            return None

    def save(self) -> None:
        """Persist checkpoint and transitions to disk. Never raises."""
        try:
            Path(self._root).mkdir(parents=True, exist_ok=True)

            # Write checkpoint atomically (write to .tmp, then os.replace)
            ckpt = {
                "version": 1,
                "agent_id": self.agent_id,
                # Recorded so `adk wm status` can report which backend wrote this
                # checkpoint without having to construct the backend to ask it.
                "backend": self.backend_name,
                "state_dims": self._state_dims,
                "n": self._n,
                "stage": self._stage,
                "last_trained_n": self._last_trained_n,
                "goal": self._goal,
                "bias": self._bias,
                "gain": self._gain,
                "action_stats": {
                    k: {"count": v["count"], "sum_delta": v["sum_delta"]}
                    for k, v in self._action_stats.items()
                },
            }

            ckpt_tmp = Path(str(self._ckpt_path) + ".tmp")
            ckpt_tmp.write_text(json.dumps(ckpt, separators=(",", ":")), encoding="utf-8")
            os.replace(str(ckpt_tmp), str(self._ckpt_path))

            # Write transitions ring buffer (truncate if needed)
            trans_tmp = Path(str(self._trans_path) + ".tmp")
            with trans_tmp.open("w", encoding="utf-8") as f:
                for trans in self._transitions:
                    f.write(json.dumps(trans, separators=(",", ":")) + "\n")
            os.replace(str(trans_tmp), str(self._trans_path))

            logger.debug("Saved checkpoint for %s: n=%d, stage=%s", self.agent_id, self._n, self._stage)
        except Exception as e:
            logger.debug("save() failed for %s: %s", self.agent_id, e)

    def load(self) -> None:
        """Load checkpoint and transitions from disk. Never raises."""
        try:
            # Load checkpoint
            if self._ckpt_path.exists():
                ckpt_text = self._ckpt_path.read_text(encoding="utf-8")
                ckpt = json.loads(ckpt_text)

                # Refuse checkpoint if state_dims mismatch (like Prospector's state_keys_hash)
                ckpt_dims = ckpt.get("state_dims", [])
                if ckpt_dims != self._state_dims:
                    logger.debug(
                        "Refusing checkpoint for %s: state_dims changed (%s -> %s)",
                        self.agent_id, ckpt_dims, self._state_dims,
                    )
                    return

                self._n = ckpt.get("n", 0)
                self._stage = ckpt.get("stage", "cold")
                self._last_trained_n = ckpt.get("last_trained_n", 0)
                self._goal.update(ckpt.get("goal", {}))
                self._bias = ckpt.get("bias", {})
                self._gain = ckpt.get("gain", {})

                # Restore action stats
                for action, stats in ckpt.get("action_stats", {}).items():
                    self._action_stats[action] = {
                        "count": stats.get("count", 0),
                        "sum_delta": stats.get("sum_delta", [0.0] * self._state_dim),
                    }

                logger.debug("Loaded checkpoint for %s: n=%d, stage=%s", self.agent_id, self._n, self._stage)

            # Load transitions
            if self._trans_path.exists():
                self._transitions = []
                with self._trans_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            trans = json.loads(line)
                            self._transitions.append(trans)
                        except json.JSONDecodeError:
                            pass

                # Keep only most recent MAX_BUFFER transitions
                if len(self._transitions) > MAX_BUFFER:
                    self._transitions = self._transitions[-MAX_BUFFER:]

                logger.debug("Loaded %d transitions for %s", len(self._transitions), self.agent_id)
        except Exception as e:
            logger.debug("load() failed for %s: %s", self.agent_id, e)

    def export_redacted_transitions(self, allowed_actions: Any = None,
                                    limit: int | None = None) -> list[dict]:
        """Transitions safe to federate: whitelisted actions, bounded vectors, EXPORT_KEYS only.

        Fail closed: no whitelist exports nothing. A row that fails any check is dropped
        whole. ``limit`` keeps the most recent survivors. Never raises.
        """
        try:
            if not _parse_allowed(allowed_actions):
                return []
            out = []
            for row in list(self._transitions):
                red = redact_transition(row, allowed_actions, self._state_dim)
                if red is not None:
                    out.append(red)
            if limit is not None and limit >= 0:
                out = out[-limit:] if limit else []
            return out
        except Exception as e:  # noqa: BLE001
            logger.debug("export_redacted_transitions() failed for %s: %s", self.agent_id, e)
            return []

    def stats(self) -> dict:
        """Return statistics dict."""
        return {
            "agent_id": self.agent_id,
            "backend": self.backend_name,
            "stage": self._stage,
            "n": self._n,
            "actions": len(self._action_stats),
            "state_dim": self._state_dim,
            "last_trained_n": self._last_trained_n,
            "goal": self._goal.copy(),
        }
