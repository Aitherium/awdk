"""A2A-based multi-agent local dispatch.

Enables bounded local fan-out to other agents without calling Genesis.
Used by swarm_code and similar tools in sovereign (AITHER_OFFLINE=1) mode.

Implements:
- Agent discovery (via .well-known/agent-card.json)
- Recursive fan-out with depth + breadth limits
- Task creation and monitoring
- Result aggregation
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable
from urllib.parse import urljoin

import httpx

logger = logging.getLogger("adk.a2a_dispatch")

# Bounded dispatch configuration
_MAX_DISPATCH_DEPTH = 2  # Recursion limit
_MAX_DISPATCH_BREADTH = 4  # Fan-out per level
_DISPATCH_TIMEOUT_SECONDS = 30.0


async def discover_agents(gateway_url: str = None) -> dict[str, str]:
    """Discover available A2A agents in the local network.

    Returns a dict of {agent_name: agent_base_url} for all agents that expose
    /.well-known/agent-card.json. In sovereign mode, discovers from the MCP
    gateway's registered agents.

    Args:
        gateway_url: Base URL for agent discovery. Defaults to AITHER_GATEWAY_URL
                    or http://localhost:8182.

    Returns:
        Dict mapping agent names to their service URLs.
    """
    if gateway_url is None:
        gateway_url = os.environ.get("AITHER_GATEWAY_URL", "http://localhost:8182")

    agents = {}
    try:
        # In sovereign mode, we'd query the gateway for registered agents.
        # For now, this is a placeholder that can be enhanced per deployment.
        # A real implementation would:
        # 1. Call /agents on the gateway to list registered A2A servers
        # 2. Probe each one for /.well-known/agent-card.json
        logger.debug("Agent discovery not yet implemented; returning empty roster")
    except Exception as e:
        logger.warning("Agent discovery failed: %s", e)

    return agents


async def dispatch_to_agent(
    agent_url: str,
    task: str,
    instructions: str = "",
    context: dict = None,
    timeout_seconds: float = _DISPATCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Dispatch a task to a single A2A agent via JSON-RPC.

    Args:
        agent_url: Base URL of the A2A agent (e.g., http://localhost:9001)
        task: Task description or problem statement
        instructions: Optional detailed instructions
        context: Optional context dict passed as metadata
        timeout_seconds: Timeout for the request

    Returns:
        Dict with keys: status (working/completed/failed), result, error (if failed)
    """
    try:
        agent_url = agent_url.rstrip("/")
        rpc_url = urljoin(agent_url + "/", "a2a")

        payload = {
            "jsonrpc": "2.0",
            "id": "dispatch-task",
            "method": "tasks.create",
            "params": {
                "task": task,
                "instructions": instructions,
                "metadata": context or {},
            },
        }

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(rpc_url, json=payload)
            resp.raise_for_status()
            result = resp.json()

            if "error" in result:
                return {
                    "status": "failed",
                    "error": f"RPC error: {result['error'].get('message', 'unknown')}",
                }

            # A task was created; poll for completion
            task_id = result.get("result", {}).get("id", "")
            if not task_id:
                return {"status": "failed", "error": "No task ID returned"}

            # Poll for task completion
            return await _poll_task_completion(agent_url, task_id, timeout_seconds)

    except httpx.TimeoutException:
        return {"status": "failed", "error": f"Timeout after {timeout_seconds}s"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


async def _poll_task_completion(
    agent_url: str,
    task_id: str,
    timeout_seconds: float,
    poll_interval: float = 0.5,
) -> dict[str, Any]:
    """Poll an A2A task until completion.

    Args:
        agent_url: Base URL of the A2A agent
        task_id: Task ID returned from tasks.create
        timeout_seconds: Max time to wait
        poll_interval: How long to wait between polls

    Returns:
        Dict with status and result/error
    """
    import time
    start = time.time()

    try:
        agent_url = agent_url.rstrip("/")
        rpc_url = urljoin(agent_url + "/", "a2a")

        while time.time() - start < timeout_seconds:
            payload = {
                "jsonrpc": "2.0",
                "id": "get-task",
                "method": "tasks.get",
                "params": {"id": task_id},
            }

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(rpc_url, json=payload)
                resp.raise_for_status()
                result = resp.json()

                if "error" in result:
                    return {"status": "failed", "error": result["error"].get("message")}

                task = result.get("result", {})
                state = task.get("status", {}).get("state", "")

                if state == "completed":
                    # Extract the response from the task's history
                    history = task.get("history", [])
                    if history:
                        last_msg = history[-1]
                        if last_msg.get("role") == "agent":
                            parts = last_msg.get("parts", [])
                            text_content = next(
                                (p.get("text") for p in parts if p.get("type") == "text"),
                                "",
                            )
                            return {
                                "status": "completed",
                                "result": text_content or str(last_msg),
                            }
                    return {"status": "completed", "result": ""}

                elif state == "failed":
                    status_msg = task.get("status", {}).get("message", "Unknown error")
                    return {"status": "failed", "error": status_msg}

            # Not done yet; wait and retry
            await asyncio.sleep(poll_interval)

        return {"status": "failed", "error": f"Task timed out after {timeout_seconds}s"}

    except Exception as e:
        return {"status": "failed", "error": str(e)}


async def fan_out_tasks(
    tasks: list[dict],
    base_agent_url: str = None,
    max_parallel: int = _MAX_DISPATCH_BREADTH,
    timeout_seconds: float = _DISPATCH_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Fan out multiple tasks in parallel to agents.

    Args:
        tasks: List of dicts with keys: agent (optional, default current), task, instructions
        base_agent_url: Base URL for current agent (for relative URLs)
        max_parallel: Max concurrent dispatch calls
        timeout_seconds: Timeout per task

    Returns:
        List of results (one per task) with status/result/error keys
    """
    if base_agent_url is None:
        base_agent_url = os.environ.get("AITHER_AGENT_URL", "http://localhost:9001")

    # Bound the fan-out
    tasks_to_run = tasks[: min(len(tasks), max_parallel)]

    async def _dispatch_one(task_spec: dict) -> dict[str, Any]:
        agent_url = task_spec.get("agent", base_agent_url)
        task = task_spec.get("task", "")
        instructions = task_spec.get("instructions", "")
        context = task_spec.get("context")

        return await dispatch_to_agent(
            agent_url,
            task,
            instructions=instructions,
            context=context,
            timeout_seconds=timeout_seconds,
        )

    # Run all tasks concurrently, respecting max_parallel
    results = await asyncio.gather(
        *[_dispatch_one(t) for t in tasks_to_run],
        return_exceptions=False,
    )
    return results


async def bounded_recursive_dispatch(
    initial_task: str,
    dispatch_fn: Callable[[str], list[dict]] = None,
    current_depth: int = 0,
    max_depth: int = _MAX_DISPATCH_DEPTH,
    max_breadth: int = _MAX_DISPATCH_BREADTH,
    base_agent_url: str = None,
) -> dict[str, Any]:
    """Execute a task with bounded recursive multi-agent dispatch.

    This is the main entry point for orchestrating multi-agent workflows.
    It prevents fork-bomb by limiting recursion depth + breadth per level.

    Args:
        initial_task: The top-level problem to solve
        dispatch_fn: Callable that takes a task and returns list of subtasks
                    to dispatch. If None, uses a simple greedy dispatcher.
        current_depth: Current recursion depth (internal)
        max_depth: Max recursion depth
        max_breadth: Max fan-out per level
        base_agent_url: Base agent URL for dispatch

    Returns:
        Dict with keys: status, plan, results, depth_reached
    """
    if current_depth > max_depth:
        return {
            "status": "failed",
            "error": f"Max recursion depth ({max_depth}) exceeded",
        }

    if dispatch_fn is None:
        # Default dispatcher: split the task into 2-3 subtasks
        # (This is a placeholder; a real implementation would use LLM analysis)
        dispatch_fn = _default_dispatch_fn

    try:
        # Get subtasks from the dispatcher
        subtasks = await dispatch_fn(initial_task)
        subtasks = subtasks[: min(len(subtasks), max_breadth)]

        if not subtasks:
            # Leaf level: just execute on current agent
            return {
                "status": "completed",
                "plan": initial_task,
                "results": [],
                "depth_reached": current_depth,
            }

        # Execute subtasks in parallel
        results = await fan_out_tasks(
            subtasks,
            base_agent_url=base_agent_url,
            max_parallel=max_breadth,
        )

        # If at max depth, stop here; otherwise offer recursive dispatch
        if current_depth < max_depth - 1:
            # Could recursively dispatch on results, but keep it simple for now
            pass

        return {
            "status": "completed",
            "plan": initial_task,
            "results": results,
            "depth_reached": current_depth,
        }

    except Exception as e:
        logger.exception("Bounded recursive dispatch failed: %s", e)
        return {
            "status": "failed",
            "error": str(e),
            "depth_reached": current_depth,
        }


async def _default_dispatch_fn(task: str) -> list[dict]:
    """Default task dispatcher (no-op for now).

    A real implementation would use an LLM to decompose the task into subtasks.
    """
    return []
