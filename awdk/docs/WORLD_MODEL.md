# World Model Bootstrapping

## Overview

The ADK world model is a self-training faculty that allows agents to learn from their own turn-by-turn behavior and optimize their own tool use. Each agent gets a personal, persistent world model that:

- Observes 8-dimensional state features from every turn (tools used, errors, latency, token count, recall, conversation depth, novelty, success)
- Records one transition (state_before, action, state_after) per tool call
- Automatically progresses through three stages (cold → warm → trained) as data accumulates
- Learns a simple diagonal-affine residual model: `pred_i = x_i + b[a]_i + w[a]_i * x_i`
- Scores candidate tool choices in advise mode, so agents can prioritize high-impact actions

## Modes

World model behavior is controlled by the **AITHER_AGENT_WM** environment variable:

### MODE_OFF (default)
- No world model activity
- No files created on disk
- No performance impact
- Set when **AITHER_AGENT_WM is unset** or invalid

### MODE_LEARN
- **AITHER_AGENT_WM=learn**
- Record every transition to disk
- Train and progress stages automatically
- Do NOT advise — the model is building only
- **Best for bootstrapping**: gather data safely without affecting agent decisions

### MODE_SHADOW
- **AITHER_AGENT_WM=shadow**
- Record every transition to disk
- Train and progress stages automatically
- Generate tool scores (advise) but do NOT apply them
- Log the scores for analysis and validation (useful for debugging and measuring)
- **Best for validation**: measure behavioral impact before enabling live steering

### MODE_STEER
- **AITHER_AGENT_WM=steer**
- Record every transition to disk
- Train and progress stages automatically
- Apply advice: reorder candidates by predicted impact score
- **DANGEROUS: only enable after a measured behavioral win exists**
- See [SAFETY](#safety) section below

## Stage Machine

A world model progresses through three stages as it accumulates transitions:

| Stage | Condition | Behavior |
|-------|-----------|----------|
| **cold** | n < WARM_MIN (50) | No learning; advise returns None. |
| **warm** | WARM_MIN ≤ n < TRAINED_MIN (200) | Learn per-action bias (EMA mean delta). Needs ≥3 observations per action. |
| **trained** | n ≥ TRAINED_MIN (200) | Learn per-action bias + gain (SGD, 3 epochs, lr=0.05). Full model active. |

Promotion is automatic: call `bootstrap()` to check and promote, refitting if due (every TRAIN_EVERY=25 new transitions after TRAINED_MIN).

## Environment Variables

### AITHER_AGENT_WM
**Mode**: off | learn | shadow | steer  
**Default**: unset (MODE_OFF)  
Controls whether the world model is active and how it advises.

### AITHER_AGENT_WM_DIR
**Path**: root directory for checkpoints and transitions  
**Default**: `~/.aither/wm`  
World models persist to:
- `<agent_id>.wm.json` — checkpoint (version, state dims, counts, learned weights, goal)
- `<agent_id>.transitions.jsonl` — append-only ring buffer (capped at MAX_BUFFER)

### AITHER_AGENT_WM_GOAL
**Format**: JSON dict  
**Default**: `{"success": 1.0, "recall": 0.3, "errors": -1.0, "latency": -0.2, "tokens": -0.1}`  
Goal weights for scoring: dot product of goal with predicted state delta. Dimensions must match the model's state dimension names (e.g., "success", "recall", "errors", "latency", "tokens", "tools", "depth", "novelty").

### AITHER_AGENT_WM_WARM_MIN
**Type**: integer  
**Default**: 50  
Number of transitions required to enter warm stage.

### AITHER_AGENT_WM_TRAINED_MIN
**Type**: integer  
**Default**: 200  
Number of transitions required to enter trained stage.

### AITHER_AGENT_WM_TRAIN_EVERY
**Type**: integer  
**Default**: 25  
Refit interval: retrain every N new transitions once in trained stage.

### AITHER_AGENT_WM_MAX_BUFFER
**Type**: integer  
**Default**: 20000  
Maximum number of transitions to keep in the ring buffer. When exceeded, the oldest entries are removed (rewrite-truncate).

## CLI Management

### adk wm status
List all agents with checkpoints and their metadata:

```
adk wm status
```

Output:
```
  World Model Status
  ====================================================================================================
  Agent ID             Backend    Stage      N        Actions      State Dim
  ----------------------------------------------------------------------------------------------------
  agent.aither         builtin    trained    300      12           8
  agent.atlas          builtin    warm       120      8            8
  ====================================================================================================
```

### adk wm inspect <agent>
Show learned effects for a specific agent:

```
adk wm inspect agent.aither
```

Output:
```
  World Model Statistics: agent.aither
  ================================================================================
  Backend:            builtin
  Stage:              trained
  Total Transitions:  300
  Last Trained @ N:   275
  State Dimensions:   8 ['tools', 'errors', 'latency', 'tokens', 'recall', 'depth', 'novelty', 'success']
  Known Actions:      12
  Goal Weights:       {'success': 1.0, 'recall': 0.3, 'errors': -1.0, 'latency': -0.2, 'tokens': -0.1}

  Action Statistics:
  ----------------
  Action               Count        Avg Delta/Dim
  ----------------
  web_search           45           [0.120, -0.050, 0.008, ...]
  python_exec          38           [0.085, 0.030, -0.012, ...]
  read_file            30           [-0.010, 0.002, 0.015, ...]
  ================================================================================
```

### adk wm train <agent>
Force a bootstrap/refit now (useful for manual testing):

```
adk wm train agent.aither
```

Output:
```
  Forcing bootstrap for: agent.aither
  Stage after bootstrap: trained
  Total transitions:    300
```

### adk wm reset <agent> [--yes]
Delete checkpoint and transitions for an agent (requires confirmation unless --yes is used):

```
adk wm reset agent.aither --yes
```

Output:
```
  Deleted: checkpoint, transitions
```

## Checkpoint Format

Checkpoints are stored as JSON in `<agent_id>.wm.json`:

```json
{
  "version": 1,
  "agent_id": "agent.aither",
  "backend": "builtin",
  "stage": "trained",
  "n": 300,
  "last_trained_n": 275,
  "state_dim": 8,
  "state_dims": ["tools", "errors", "latency", "tokens", "recall", "depth", "novelty", "success"],
  "goal": {"success": 1.0, "recall": 0.3, "errors": -1.0, "latency": -0.2, "tokens": -0.1},
  "action_stats": {
    "web_search": {"count": 45, "sum_delta": [5.4, -2.25, 0.36, ...]},
    ...
  },
  "bias": {
    "web_search": [0.12, -0.05, 0.008, ...],
    ...
  },
  "gain": {
    "web_search": [1.02, 0.98, 1.0, ...],
    ...
  }
}
```

## Transition Buffer Format

Transitions are stored as append-only JSONL in `<agent_id>.transitions.jsonl`, one JSON object per line:

```json
{"state_before": [0.5, 0.1, 0.2, ...], "action": "web_search", "state_after": [0.55, 0.05, 0.19, ...], "ok": true}
{"state_before": [0.55, 0.05, 0.19, ...], "action": "python_exec", "state_after": [0.60, 0.0, 0.18, ...], "ok": true}
...
```

When the buffer exceeds MAX_BUFFER lines, it is rewritten with the oldest entries removed.

## Custom Backends

Override the builtin world model by registering a custom backend factory:

```python
from adk import worldmodel

class MyWorldModel(worldmodel.WorldModelBackend):
    # Advertised to `adk wm status`, registered_backend_name() and stats(), so an
    # operator can see WHICH backend is actually serving. Falls back to the class
    # name when omitted.
    backend_name = "mybackend"

    def __init__(self, agent_name: str) -> None:
        self.agent_id = worldmodel.wm_agent_id(agent_name)
        # ...

    def observe(self, context=None):
        # Return list[float] or None
        ...

    def record(self, state_before, action, state_after, ok=True):
        # Record a transition
        ...

    def bootstrap(self):
        # Return stage: "cold" | "warm" | "trained"
        ...

    def advise(self, state, candidates):
        # Return {"scores": {...}, "order": [...]} or None
        ...

    def save(self):
        # Persist to disk
        ...

    def load(self):
        # Load from disk
        ...

    def stats(self):
        # Return {"agent_id", "backend", "stage", "n", ...}
        ...

# Register the CLASS itself (not a lambda wrapping it) so the registry can read
# `backend_name` off it. A lambda works too, but reports its own name instead.
worldmodel.register_world_model(MyWorldModel)
```

### `predict()` — measuring the model instead of trusting it

`BuiltinWorldModel` also exposes:

```python
predict(state: list[float], action: str) -> list[float] | None
```

It returns the model's next-state estimate (`pred_i = x_i + b[a]_i + w[a]_i * x_i`), or
`None` for an unknown action or a dimension mismatch — never a silently-wrong answer.
This is the same predictor `advise()` scores with, exposed publicly so prediction error
can be **measured** on held-out transitions against the do-nothing baseline
(`next == current`) rather than asserted. `scripts/wm_adk_bootstrap_demo.py` in AitherOS
uses it for exactly that; a claim about model quality that isn't produced this way should
not be believed.

## Safety

### STEER Mode Is Dangerous

Enabling MODE_STEER (AITHER_AGENT_WM=steer) allows the world model to reorder tool candidates and directly influence agent decisions. This is a powerful capability but must be gated behind a measured behavioral win:

**Before enabling STEER mode in production:**

1. Run in LEARN mode for at least 200 turns on representative workloads
2. Switch to SHADOW mode and measure tool ordering improvement (use adk wm inspect to review learned effects)
3. Validate that predicted scores align with measured outcomes
4. Commit a durable benchmark showing improvement (reduced errors, higher success rate, lower latency, etc.)
5. Document the benchmark in the agent's deployment notes
6. Only then enable STEER mode with explicit approval

**In production with STEER mode enabled:**

- World model exceptions are caught and logged (never propagate to agent)
- All tool-ordering advice is wrapped in try/except Exception: pass
- Model refits happen after the turn's work is done (`bootstrap()` is called from
  `_learn_after`), and are dispatched via `asyncio.to_thread` so the CPU-bound SGD
  never stalls other coroutines on the event loop. Measured refit cost: ~0.5 ms at
  200 buffered transitions, ~67 ms at the 20 000 buffer cap.
- Transitions are persisted atomically (checkpoint written last, after transitions are synced)

**Do NOT:**
- Enable STEER mode without a measured behavioral win
- Use the world model to gate agent autonomy (advice only, no blocking)
- Assume the model is correct (always validate; it can learn pathological patterns)

## Aither-OS Integration

The awdk world model is designed to be complemented by a richer AitherOS backend (in `lib/cognitive/adk_wm_backend.py`) that:

- Adds AitherSense interoception (5-D affect dimensions) to the state vector
- Writes transitions to a shared fleet corpus for cross-agent learning
- Optionally trains via a torch-based conditioned model on the cross-agent corpus
- Falls back to the pure-stdlib builtin learner if torch is unavailable

To enable the AitherOS backend, call `adk_wm_backend.install()` in your agent initialization code. The installation is idempotent and guarded by try/except.
