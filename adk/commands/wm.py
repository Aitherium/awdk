"""World model management commands: status, inspect, train, reset.

Provides CLI access to the world model bootstrapping facility:
  adk wm status            - List all agents with checkpoints
  adk wm inspect <agent>   - Show learned effects for one agent
  adk wm train <agent>     - Force a bootstrap/refit now
  adk wm reset <agent>     - Delete checkpoint + transitions (requires --yes)

Both backends are known here: the builtin keeps ``<agent_id>.wm.json`` (+ a
``.transitions.jsonl`` buffer), the awm backend keeps ``<agent_id>.awm.json`` and
its transitions in an awm file (``awm_world.db`` by default) at the agent's scope.
A reset deletes BOTH; a command that could only see one would report success while
the other's transitions reload on the next start.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.commands.wm")


def _get_wm_root() -> Path:
    """Get the world model root directory."""
    root = os.environ.get("AITHER_AGENT_WM_DIR")
    if root:
        return Path(root)
    return Path(os.path.expanduser("~")) / ".aither" / "wm"


def _list_agent_checkpoints() -> list[str]:
    """List all agent IDs that have checkpoints."""
    root = _get_wm_root()
    if not root.exists():
        return []

    agent_ids = set()
    for suffix in _SUFFIXES:
        for ckpt_file in root.glob(f"*{suffix}"):
            agent_ids.add(ckpt_file.name[:-len(suffix)])

    return sorted(list(agent_ids))


#: Every checkpoint suffix, builtin first (the order ``status``/``train`` walk them).
_SUFFIXES = (".wm.json", ".awm.json")


def _preferred_suffixes() -> tuple:
    """The CONFIGURED backend's checkpoint first: under ``AITHER_AGENT_WM_BACKEND=awm``
    ``inspect`` must show the awm backend, not a builtin file left from before."""
    if os.environ.get("AITHER_AGENT_WM_BACKEND", "").strip().lower() == "awm":
        return (".awm.json", ".wm.json")
    return _SUFFIXES


def _cli_id(agent: str) -> str:
    """The CLI takes an agent id (``agent.atlas``, as ``status`` lists) or a name."""
    if agent.startswith("agent."):
        return agent
    from adk.worldmodel import wm_agent_id

    return wm_agent_id(agent)


def _checkpoint_paths(agent_id: str, suffixes: tuple = _SUFFIXES) -> list[Path]:
    root = _get_wm_root()
    return [root / f"{agent_id}{sfx}" for sfx in suffixes
            if (root / f"{agent_id}{sfx}").exists()]


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load checkpoint %s: %s", path, e)
        return None


def _checkpoint_db(ckpt: dict) -> str | None:
    """The db a checkpoint pins, or None = resolve it the way the RUNTIME does.

    The runtime opens ``AITHER_AGENT_WM_AWM_DB`` or ``<configured wm root>/awm_world.db``.
    A checkpoint records an ABSOLUTE path; when that path was only the root's default
    ("db_source": "root", or an older checkpoint naming ``awm_world.db``), obeying it
    after the root was copied or moved made ``reset`` delete the SOURCE root's
    transitions, report success, and leave the configured root's table for the
    runtime to reload. Only a db configured explicitly (env or argument) is pinned,
    and the environment, when set, wins exactly as it does at runtime.
    """
    if os.environ.get("AITHER_AGENT_WM_AWM_DB", "").strip():
        return None
    db = ckpt.get("db") or None
    if not db:
        return None
    src = ckpt.get("db_source")
    if src == "root" or (src is None and Path(db).name == "awm_world.db"):
        return None
    return db


def _awm_backend(agent_id: str, ckpt: dict | None) -> Any:
    """An (unloaded) awm backend on the db the runtime would open, and the ckpt's scope."""
    from adk.world import AwmWorldModelBackend

    ckpt = ckpt or {}
    return AwmWorldModelBackend(agent_id, root=str(_get_wm_root()), agent_id=agent_id,
                                db=_checkpoint_db(ckpt), scope=ckpt.get("scope") or None)


def _awm_live(agent_id: str, ckpt: dict | None) -> dict:
    """Stats READ FROM the awm backend's table, not from its checkpoint.

    The ``.awm.json`` checkpoint is written every 50 steps and carries no per-action
    stats: reading it showed 'Known Actions: 0' for a backend with a live table.
    Returns ``stats()`` plus ``effects`` = {action: {"count", "slots": [(slot, n)]}}.
    """
    backend = _awm_backend(agent_id, ckpt)
    backend.load()
    try:
        out = dict(backend.stats())
        effects: dict = {}
        store = backend._store
        if store is not None:
            for t in store.transitions(backend._scope):
                e = effects.setdefault(t.action, {"count": 0, "slots": {}})
                e["count"] += 1
                for slot in t.delta:
                    e["slots"][slot] = e["slots"].get(slot, 0) + 1
        out["effects"] = {a: {"count": e["count"],
                              "slots": sorted(e["slots"].items(),
                                              key=lambda kv: (-kv[1], kv[0]))}
                          for a, e in effects.items()}
        return out
    finally:
        backend.close()


def _load_checkpoint(agent_id: str) -> dict | None:
    """Load the CONFIGURED backend's checkpoint for agent_id (see _preferred_suffixes).

    The returned dict carries ``_path`` (the file read) so the caller can tell which
    backend's file it is looking at."""
    paths = _checkpoint_paths(agent_id, _preferred_suffixes())
    if not paths:
        return None
    ckpt_path = paths[0]

    try:
        with open(ckpt_path, "r") as f:
            ckpt = json.load(f)
    except Exception as e:
        logger.error("Failed to load checkpoint %s: %s", ckpt_path, e)
        return None
    if isinstance(ckpt, dict):
        ckpt["_path"] = str(ckpt_path)
    return ckpt


def _count_transitions(agent_id: str) -> int:
    """Count transitions in the buffer file."""
    root = _get_wm_root()
    trans_path = root / f"{agent_id}.transitions.jsonl"

    if not trans_path.exists():
        return 0

    try:
        count = 0
        with open(trans_path, "r") as f:
            for _ in f:
                count += 1
        return count
    except Exception as e:
        logger.error("Failed to count transitions %s: %s", trans_path, e)
        return 0


def cmd_wm_status(args: Any) -> int:
    """List all agents with checkpoints: agent_id, backend, stage, n, actions, state_dim."""
    try:
        agent_ids = _list_agent_checkpoints()

        if not agent_ids:
            print("  No world model checkpoints found.")
            print()
            return 0

        print()
        print("  World Model Status")
        print("  " + "=" * 100)
        print(
            "  {:<20} {:<10} {:<10} {:<8} {:<12} {:<10}".format(
                "Agent ID", "Backend", "Stage", "N", "Actions", "State Dim"
            )
        )
        print("  " + "-" * 100)

        for agent_id in agent_ids:
            for path in _checkpoint_paths(agent_id):
                ckpt = _read_json(path)

                if ckpt is None:
                    print("  {:<20} {:<10} {:<10} {:<8} {:<12} {:<10}".format(
                        agent_id, "unknown", "unknown", "0", "0", "0"
                    ))
                    continue

                backend = ckpt.get("backend", "unknown")
                stage = ckpt.get("stage", "cold")
                n = ckpt.get("n", 0)
                actions = len(ckpt.get("action_stats", {}))
                state_dim = ckpt.get("state_dim", len(ckpt.get("state_dims") or []) or 8)
                if path.name.endswith(".awm.json"):
                    # The table is the truth; the checkpoint has no action stats.
                    live = _awm_live(agent_id, ckpt)
                    if live.get("error") or live.get("degraded"):
                        # Unreadable table: flagged, and the row keeps the checkpoint's
                        # last known N -- a degraded read's 0 is "unknown", not zero.
                        backend = f"{backend}!"
                    else:
                        # Every field of the row from ONE source: the checkpoint's stage
                        # may be a degraded save's "cold" next to a live N of 60.
                        stage = live.get("stage", stage)
                        n, actions = live.get("n", n), live.get("actions", 0)

                print("  {:<20} {:<10} {:<10} {:<8} {:<12} {:<10}".format(
                    agent_id, backend, stage, n, actions, state_dim
                ))

        print("  " + "=" * 100)
        print()
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"  Error listing world model status: {e}")
        logger.exception("cmd_wm_status failed")
        return 1


def cmd_wm_inspect(args: Any) -> int:
    """Show learned effects for a specific agent."""
    agent_id = getattr(args, "agent", None)

    if not agent_id:
        print("  Usage: adk wm inspect <agent>")
        return 1
    agent_id = _cli_id(agent_id)

    try:
        from adk import worldmodel

        ckpt = _load_checkpoint(agent_id)

        if ckpt is None:
            print(f"  No checkpoint found for agent: {agent_id}")
            return 1

        print()
        print(f"  World Model Statistics: {agent_id}")
        print("  " + "=" * 80)

        backend = ckpt.get("backend", "unknown")
        stage = ckpt.get("stage", "cold")
        n = ckpt.get("n", 0)
        state_dim = ckpt.get("state_dim", 8)
        last_trained_n = ckpt.get("last_trained_n", 0)

        state_dims = ckpt.get("state_dims", worldmodel.STATE_DIMS)
        goal = ckpt.get("goal", worldmodel.DEFAULT_GOAL)
        action_stats = ckpt.get("action_stats", {})
        if str(ckpt.get("_path", "")).endswith(".awm.json"):
            return _inspect_awm(agent_id, ckpt, state_dim, state_dims, goal)

        print(f"  Backend:            {backend}")
        print(f"  Stage:              {stage}")
        print(f"  Total Transitions:  {n}")
        print(f"  Last Trained @ N:   {last_trained_n}")
        print(f"  State Dimensions:   {state_dim} {state_dims}")
        print(f"  Known Actions:      {len(action_stats)}")
        print(f"  Goal Weights:       {goal}")

        print()
        print("  Learned effects (which dimensions each action actually moves):")
        print("  " + "-" * 80)
        print("  {:<20} {:<8} {:<48}".format("Action", "Count", "Top effects (avg delta per dim)"))
        print("  " + "-" * 80)

        # Show the dims an action MOVES, ranked by magnitude -- printing the first few
        # dims positionally is useless when (as is typical) most of them never change.
        for action, stats in sorted(action_stats.items()):
            count = stats.get("count", 0)
            sum_delta = stats.get("sum_delta", [])

            if count > 0 and sum_delta:
                avgs = [(state_dims[i] if i < len(state_dims) else f"dim{i}", d / count)
                        for i, d in enumerate(sum_delta)]
                top = [x for x in sorted(avgs, key=lambda kv: -abs(kv[1])) if abs(x[1]) >= 1e-6][:3]
                effect_str = ("  ".join(f"{name}{val:+.3f}" for name, val in top)
                              if top else "(no measurable effect)")
            else:
                effect_str = "N/A"

            print("  {:<20} {:<8} {:<48}".format(action, count, effect_str))

        print("  " + "=" * 80)
        print()
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"  Error inspecting world model: {e}")
        logger.exception("cmd_wm_inspect failed")
        return 1


def _inspect_awm(agent_id: str, ckpt: dict, state_dim: Any, state_dims: Any,
                 goal: Any) -> int:
    """``inspect`` for the awm backend: every number read from its live table."""
    live = _awm_live(agent_id, ckpt)
    effects = live.get("effects", {})
    print(f"  Backend:            {live.get('backend', 'awm')}  (checkpoint {ckpt['_path']})")
    print(f"  Table:              {live.get('db')} @ {live.get('scope')}")
    print(f"  Stage:              {live.get('stage', ckpt.get('stage', 'cold'))}")
    print(f"  Total Transitions:  {live.get('n', 0)}")
    print(f"  State Dimensions:   {state_dim} {state_dims}")
    print(f"  Known Actions:      {live.get('actions', len(effects))}")
    print(f"  Goal Weights:       {goal}")
    bad = live.get("error") or live.get("degraded")
    if bad:
        print(f"  DEGRADED:           {bad}")
    print()
    print("  Learned effects (which slots each action changed, and how often):")
    print("  " + "-" * 80)
    print("  {:<20} {:<8} {:<48}".format("Action", "Count", "Top changed slots"))
    print("  " + "-" * 80)
    for action, e in sorted(effects.items()):
        top = "  ".join(f"{s} x{n}" for s, n in e["slots"][:3]) or "(no slot changed)"
        print("  {:<20} {:<8} {:<48}".format(action, e["count"], top))
    print("  " + "=" * 80)
    print()
    return 1 if bad else 0


def cmd_wm_train(args: Any) -> int:
    """Force a bootstrap/refit now and print the resulting stage."""
    agent_id = getattr(args, "agent", None)

    if not agent_id:
        print("  Usage: adk wm train <agent>")
        return 1
    agent_id = _cli_id(agent_id)

    try:
        from adk import worldmodel

        paths = _checkpoint_paths(agent_id)
        if not paths:
            print(f"  No checkpoint found for agent: {agent_id}")
            return 1

        rc = 0
        for path in paths:
            if path.name.endswith(".awm.json"):
                wm = _awm_backend(agent_id, _read_json(path))
            else:
                wm = worldmodel.BuiltinWorldModel(agent_id, agent_id=agent_id)
            try:
                wm.load()
                print()
                print(f"  Forcing bootstrap for: {agent_id} ({wm.backend_name})")
                stage = wm.bootstrap()
                wm.save()
                stats = wm.stats()
            finally:
                close = getattr(wm, "close", None)
                if callable(close):
                    close()
            if stats.get("degraded") or stats.get("error"):
                print(f"  DEGRADED: {stats.get('degraded') or stats.get('error')}")
                rc = 1
            print(f"  Stage after bootstrap: {stage}")
            print(f"  Total transitions:    {stats.get('n', 0)}")
        print()

        return rc

    except Exception as e:  # noqa: BLE001
        print(f"  Error training world model: {e}")
        logger.exception("cmd_wm_train failed")
        return 1


def cmd_wm_reset(args: Any) -> int:
    """Delete checkpoint + transitions for an agent (requires --yes flag)."""
    agent_id = getattr(args, "agent", None)
    yes_flag = getattr(args, "yes", False)

    if not agent_id:
        print("  Usage: adk wm reset <agent> [--yes]")
        return 1
    agent_id = _cli_id(agent_id)

    try:
        root = _get_wm_root()
        ckpt_path = root / f"{agent_id}.wm.json"
        trans_path = root / f"{agent_id}.transitions.jsonl"
        awm_ckpt_path = root / f"{agent_id}.awm.json"
        awm_ckpt = _read_json(awm_ckpt_path) if awm_ckpt_path.exists() else None
        # The awm table is looked for even without an awm checkpoint: the backend
        # writes its checkpoint only every 50 steps, the transitions on every one.
        backend = _awm_backend(agent_id, awm_ckpt)
        awm_n = _awm_count(backend)
        if awm_n is None:
            # The awm table EXISTS (``_awm_count`` returns 0 for no file) but cannot be
            # read. Whether or not an awm checkpoint was written yet (every 50 steps),
            # deleting only the other files would report success and leave every
            # transition to reload on the next start.
            print(f"  Cannot read the awm transitions for {agent_id} in "
                  f"{backend.db_path} ({backend.degraded}); refusing a partial reset")
            return 1
        if awm_n and not backend.scope_is_own():
            # A scope without this agent's id is shared: clearing it would delete
            # every other agent's transitions too.
            print(f"  {backend.scope_text} is shared by other agents (it does not name "
                  f"{agent_id}); refusing to delete {awm_n} transition(s) that are not "
                  f"only this agent's")
            return 1

        if not ckpt_path.exists() and not trans_path.exists() \
                and not awm_ckpt_path.exists() and not awm_n:
            print(f"  No checkpoint or transitions found for agent: {agent_id}")
            return 1

        if not yes_flag:
            print()
            print("  This will DELETE:")
            for path in (ckpt_path, trans_path, awm_ckpt_path):
                if path.exists():
                    print(f"    - {path}")
            if awm_n:
                print(f"    - {awm_n} transition(s) at {backend.scope_text} "
                      f"in {backend.db_path}")
            print()
            response = input("  Proceed? (type 'yes' to confirm): ").strip().lower()

            if response != "yes":
                print("  Cancelled.")
                return 0

        # Delete the files
        deleted = []
        if ckpt_path.exists():
            try:
                ckpt_path.unlink()
                deleted.append("checkpoint")
            except Exception as e:
                print(f"  Failed to delete checkpoint: {e}")
                return 1

        if trans_path.exists():
            try:
                trans_path.unlink()
                deleted.append("transitions")
            except Exception as e:
                print(f"  Failed to delete transitions: {e}")
                return 1

        if awm_n:
            try:
                n = _awm_clear(backend)
                deleted.append(f"{n} awm transition(s)")
            except Exception as e:
                print(f"  Failed to delete awm transitions: {e}")
                return 1

        if awm_ckpt_path.exists():
            try:
                awm_ckpt_path.unlink()
                deleted.append("awm checkpoint")
            except Exception as e:
                print(f"  Failed to delete awm checkpoint: {e}")
                return 1

        print()
        print(f"  Deleted: {', '.join(deleted)}")
        print()
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"  Error resetting world model: {e}")
        logger.exception("cmd_wm_reset failed")
        return 1


def _awm_count(backend: Any) -> int | None:
    """Transitions at EXACTLY the backend's scope; 0 when there is no file; None = unreadable."""
    if not Path(backend.db_path).exists():
        return 0
    backend.load()
    try:
        if backend._store is None:
            return None
        rows = backend._store.transitions(backend._scope)
        return sum(1 for t in rows if t.scope == backend.scope_text)
    finally:
        backend.close()


def _awm_clear(backend: Any) -> int:
    """Delete the backend's transitions and state rows at exactly its scope."""
    backend.load()
    try:
        store = backend._store
        if store is None:
            raise RuntimeError(backend.degraded or "awm backend unavailable")
        n = store.clear_transitions(backend._scope)
        from adk.world import STATE_KIND

        # The digest -> slots rows live at the bookkeeping scope (states_scope_text).
        states = backend._states_scope
        for m in store.recall(states, kind=STATE_KIND, limit=10_000_000):
            if m.scope == str(states):
                store.forget(states, m.key)
        return n
    finally:
        backend.close()
