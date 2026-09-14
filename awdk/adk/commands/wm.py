"""World model management commands: status, inspect, train, reset.

Provides CLI access to the world model bootstrapping facility:
  adk wm status            - List all agents with checkpoints
  adk wm inspect <agent>   - Show learned effects for one agent
  adk wm train <agent>     - Force a bootstrap/refit now
  adk wm reset <agent>     - Delete checkpoint + transitions (requires --yes)
"""

from __future__ import annotations

import json
import logging
import os
import sys
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
    for ckpt_file in root.glob("*.wm.json"):
        agent_id = ckpt_file.name.replace(".wm.json", "")
        agent_ids.add(agent_id)

    return sorted(list(agent_ids))


def _load_checkpoint(agent_id: str) -> dict | None:
    """Load a checkpoint file for the given agent_id."""
    root = _get_wm_root()
    ckpt_path = root / f"{agent_id}.wm.json"

    if not ckpt_path.exists():
        return None

    try:
        with open(ckpt_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load checkpoint %s: %s", ckpt_path, e)
        return None


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
        from adk import worldmodel

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
            ckpt = _load_checkpoint(agent_id)

            if ckpt is None:
                print("  {:<20} {:<10} {:<10} {:<8} {:<12} {:<10}".format(
                    agent_id, "unknown", "unknown", "0", "0", "0"
                ))
                continue

            backend = ckpt.get("backend", "unknown")
            stage = ckpt.get("stage", "cold")
            n = ckpt.get("n", 0)
            actions = len(ckpt.get("action_stats", {}))
            state_dim = ckpt.get("state_dim", 8)

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


def cmd_wm_train(args: Any) -> int:
    """Force a bootstrap/refit now and print the resulting stage."""
    agent_id = getattr(args, "agent", None)

    if not agent_id:
        print("  Usage: adk wm train <agent>")
        return 1

    try:
        from adk import worldmodel

        ckpt = _load_checkpoint(agent_id)

        if ckpt is None:
            print(f"  No checkpoint found for agent: {agent_id}")
            return 1

        # Create a temporary instance and try to bootstrap
        wm = worldmodel.BuiltinWorldModel(agent_id)
        wm.load()

        print()
        print(f"  Forcing bootstrap for: {agent_id}")
        stage = wm.bootstrap()
        wm.save()

        print(f"  Stage after bootstrap: {stage}")
        stats = wm.stats()
        print(f"  Total transitions:    {stats.get('n', 0)}")
        print()

        return 0

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

    try:
        root = _get_wm_root()
        ckpt_path = root / f"{agent_id}.wm.json"
        trans_path = root / f"{agent_id}.transitions.jsonl"

        if not ckpt_path.exists() and not trans_path.exists():
            print(f"  No checkpoint or transitions found for agent: {agent_id}")
            return 1

        if not yes_flag:
            print()
            print(f"  This will DELETE:")
            if ckpt_path.exists():
                print(f"    - {ckpt_path}")
            if trans_path.exists():
                print(f"    - {trans_path}")
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

        print()
        print(f"  Deleted: {', '.join(deleted)}")
        print()
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"  Error resetting world model: {e}")
        logger.exception("cmd_wm_reset failed")
        return 1
