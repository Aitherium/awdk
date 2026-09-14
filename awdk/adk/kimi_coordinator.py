"""
Kimi-K3 Mesh RPC Coordinator Planner
====================================

Pure planning functions for deploying Kimi-K3 across AitherNet mesh nodes via
llama.cpp RPC backend. Stage S2 of the awdk split_inference toolpack.

Public API:
    NodeBudget: dataclass for node resource budget
    select_quant(total_pool_gb) -> Optional[str]: largest quant by pool size
    plan_kimi_split(nodes, quant="auto") -> dict: coordinator node + RPC list
    validate_rpc_bind(host) -> bool: RFC1918/CGNAT/loopback only
    assert_rpc_bind(host): raises ValueError for public IPs
    render_deploy_commands(plan, ...) -> dict: step-by-step deployment commands
    deploy_kimi_split(plan, ..., dry_run=True) -> dict: thin orchestrator wrapper

All functions are pure (no side effects) except deploy_kimi_split, which is
a thin wrapper returning steps + commands without executing them. Actual
remote execution lands with `adk mesh serve` (S3).
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import Optional

from adk.unsloth_gguf_download import KIMI_K3_QUANTS

logger = logging.getLogger("kimi_coordinator")


@dataclass
class NodeBudget:
    """Node resource budget: node_id, overlay IP, RAM+VRAM."""

    node_id: str
    host: str
    ram_gb: float
    vram_gb: float

    @property
    def total_gb(self) -> float:
        """Combined RAM+VRAM in GB."""
        return self.ram_gb + self.vram_gb


def select_quant(total_pool_gb: float) -> Optional[str]:
    """
    Select largest quant that fits the total pool.

    Args:
        total_pool_gb: Combined RAM+VRAM across all participating nodes (GB)

    Returns:
        Quantization name (e.g., "UD-IQ1_S") or None if pool < 610GB (minimum)
    """
    # Sort quants by memory requirement (ascending)
    sorted_quants = sorted(
        KIMI_K3_QUANTS.items(),
        key=lambda x: x[1]["min_total_memory_gb"]
    )

    # Return largest quant that fits
    for quant_name, quant_info in reversed(sorted_quants):
        if total_pool_gb >= quant_info["min_total_memory_gb"]:
            return quant_name

    return None


def validate_rpc_bind(host: str) -> bool:
    """
    Validate that a bind address is private (RFC1918, CGNAT, loopback).

    rpc-server is UNAUTHENTICATED and executes tensor ops from any client
    that reaches it. Returns False for public IPs, 0.0.0.0, ::, [::].

    Args:
        host: IP address string (IPv4 or IPv6)

    Returns:
        True if the address is safe (private network); False for public/unrestricted
    """
    if not host or host in ("0.0.0.0", "::", "[::]"):
        return False

    try:
        addr = ipaddress.ip_address(host)
        # RFC1918 private: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
        # CGNAT: 100.64.0.0/10 (RFC 6598)
        # Loopback: 127.0.0.1, ::1
        return (
            addr.is_private or
            addr.is_loopback or
            ipaddress.ip_address("100.64.0.0") <= addr <=
            ipaddress.ip_address("100.127.255.255")
        )
    except ValueError:
        return False


def assert_rpc_bind(host: str) -> None:
    """
    Raise ValueError if the bind address is public or unrestricted.

    Args:
        host: IP address string

    Raises:
        ValueError: If the address is not safe for rpc-server binding
    """
    if not validate_rpc_bind(host):
        raise ValueError(
            f"rpc-server bind rejected: {host} is not a private/overlay address. "
            "rpc-server is UNAUTHENTICATED — bind RFC1918 (10.x, 172.16-31.x, "
            "192.168.x), CGNAT (100.64-127.x), or loopback (127.0.0.1) only."
        )


def plan_kimi_split(
    nodes: list[NodeBudget],
    quant: str = "auto",
) -> dict:
    """
    Plan Kimi-K3 split across mesh nodes.

    Selects quant by pool size, designates coordinator (largest node),
    skips tiny backends, computes tensor_split weights.

    Args:
        nodes: List of NodeBudget (node resource budgets)
        quant: Quantization name ("UD-IQ1_S", "UD-Q2_K_XL", etc.) or "auto"

    Returns:
        Dict with keys:
          - "quant": selected quantization name
          - "coordinator": NodeBudget of the coordinator (largest total_gb)
          - "rpc_backends": list of NodeBudget for RPC servers
          - "tensor_split": list of floats (normalized, coordinator-first)
          - "pool_total_gb": combined total_gb across participating nodes
          - "required_gb": minimum_total_memory_gb for the selected quant
          - "skipped_nodes": list of node_ids below 32GB threshold (str)
          - "est_tok_s": "unproven at this scale" (string, not a number)

    Raises:
        ValueError: If pool < required_gb, or if nodes is empty
    """
    if not nodes:
        raise ValueError("nodes list cannot be empty")

    # Auto-select quant or validate explicit choice
    if quant == "auto":
        total_gb = sum(n.total_gb for n in nodes)
        selected_quant = select_quant(total_gb)
        if selected_quant is None:
            shortfall_gb = 610 - total_gb
            raise ValueError(
                f"Total pool {total_gb:.1f}GB < 610GB minimum "
                f"(shortfall: {shortfall_gb:.1f}GB). Cannot run Kimi-K3."
            )
        quant = selected_quant
    elif quant not in KIMI_K3_QUANTS:
        available = ", ".join(sorted(KIMI_K3_QUANTS.keys()))
        raise ValueError(
            f"Unknown quant '{quant}'. Available: {available}"
        )

    required_gb = KIMI_K3_QUANTS[quant]["min_total_memory_gb"]

    # Partition: coordinator (largest) + backends (>= 32GB)
    sorted_nodes = sorted(nodes, key=lambda n: n.total_gb, reverse=True)
    coordinator = sorted_nodes[0]

    backends = []
    skipped = []
    for node in sorted_nodes[1:]:
        if node.total_gb > 32:
            backends.append(node)
        else:
            skipped.append(node.node_id)

    # Calculate combined pool
    all_participating = [coordinator] + backends
    pool_total_gb = sum(n.total_gb for n in all_participating)

    # Fail if pool < requirement
    if pool_total_gb < required_gb:
        shortfall_gb = required_gb - pool_total_gb
        raise ValueError(
            f"Total pool {pool_total_gb:.1f}GB < required {required_gb}GB "
            f"(shortfall: {shortfall_gb:.1f}GB). Cannot run Kimi-K3-{quant}."
        )

    # Compute tensor_split: weights normalized to sum ~1.0, coordinator first
    total_budget = sum(n.total_gb for n in all_participating)
    tensor_split = [
        round(n.total_gb / total_budget, 3)
        for n in all_participating
    ]

    return {
        "quant": quant,
        "coordinator": coordinator,
        "rpc_backends": backends,
        "tensor_split": tensor_split,
        "pool_total_gb": round(pool_total_gb, 1),
        "required_gb": required_gb,
        "skipped_nodes": skipped,
        "est_tok_s": "unproven at this scale",
    }


def render_deploy_commands(
    plan: dict,
    model_dir: str,
    bin_dir: str,
    mmproj_path: str,
    port: int = 50052,
) -> dict:
    """
    Render step-by-step deployment commands (pure, no execution).

    Generates rpc-server commands for each backend + coordinator llama-server
    command. Validates all bind addresses are private (no public/0.0.0.0).

    Args:
        plan: Output of plan_kimi_split()
        model_dir: Directory containing Kimi-K3 GGUF shards (e.g., /work/kimi)
        bin_dir: Directory containing llama-server/rpc-server binaries
                (e.g., /work/build-rpc/bin)
        mmproj_path: Path to mmproj-BF16.gguf (vision tower)
        port: Starting port for rpc-servers (each backend gets port, port+1, …)

    Returns:
        Dict with keys:
          - "rpc_commands": list of {"host", "port", "command"} dicts
          - "coordinator_command": full llama-server invocation
          - "steps": ordered step list (download → verify → start servers)
          - "warnings": list of deployment traps

    Raises:
        ValueError: If any rpc-server bind address is public/0.0.0.0
    """
    # Validate all binds are private
    coordinator = plan["coordinator"]
    backends = plan.get("rpc_backends", [])
    for backend in backends:
        assert_rpc_bind(backend.host)
    assert_rpc_bind(coordinator.host)

    # Render rpc-server commands
    rpc_commands = []
    rpc_targets = []
    for i, backend in enumerate(backends):
        backend_port = port + i
        cmd = (
            f"{bin_dir}/rpc-server --host {backend.host} "
            f"--port {backend_port}"
        )
        rpc_commands.append({
            "host": backend.host,
            "port": backend_port,
            "command": cmd,
        })
        rpc_targets.append(f"{backend.host}:{backend_port}")

    # Build coordinator command
    model_arg = f"{model_dir}/kimi-k3-UD-{plan['quant']}.gguf"
    tensor_split_str = ",".join(str(w) for w in plan["tensor_split"])
    rpc_flag = ",".join(rpc_targets) if rpc_targets else ""

    coordinator_cmd = (
        f"{bin_dir}/llama-server "
        f"--model {model_arg} "
        f"--mmproj {mmproj_path} "
        f"--gpu-layers 99 "
        f"--temp 1.0 "
        f"--top-p 0.95 "
    )
    if rpc_flag:
        coordinator_cmd += f"--rpc {rpc_flag} "
    coordinator_cmd += (
        f"--tensor-split {tensor_split_str} "
        f"--host {coordinator.host} "
        f"--port 8080"
    )

    steps = [
        "Download Kimi-K3 shards to all nodes",
        "Verify Kimi-K3 binaries (llama-server, rpc-server)",
        "Start rpc-server on each backend (detached, listens for ONE client)",
        "Start coordinator llama-server with --rpc list",
        "Run split_verify to confirm RPC devices attached",
    ]

    warnings = [
        "Each rpc-server serves ONE client and must restart between runs.",
        "Coordinator host must have durable access to all mmproj and first shard.",
        "A protocol/fork mismatch fails at RPC connect; both sides must be "
        "built from the same unsloth fork branch (pull/48).",
        "Unreachable backends or silent connection failures can leave the "
        "coordinator running LOCAL-ONLY; split_verify is mandatory.",
    ]

    return {
        "rpc_commands": rpc_commands,
        "coordinator_command": coordinator_cmd,
        "steps": steps,
        "warnings": warnings,
    }


async def deploy_kimi_split(
    plan: dict,
    model_dir: str,
    bin_dir: str,
    mmproj_path: str,
    port: int = 50052,
    dry_run: bool = True,
) -> dict:
    """
    Thin deployment orchestrator wrapper (S2 → S3 handoff).

    Renders commands + ordered steps. Under dry_run=True, returns steps + commands
    without executing. Under dry_run=False, raises NotImplementedError because
    actual remote execution (SSH, mesh overlay) lands with `adk mesh serve` (S3).

    Args:
        plan: Output of plan_kimi_split()
        model_dir: GGUF shard directory
        bin_dir: Compiled binary directory
        mmproj_path: Path to vision tower
        port: Starting port for rpc-servers
        dry_run: If False, raises NotImplementedError (S3 work)

    Returns:
        Dict with keys:
          - "plan": the input plan dict
          - "commands": output of render_deploy_commands()
          - "steps": ordered deployment steps
          - "dry_run": boolean

    Raises:
        NotImplementedError: If dry_run=False (S3 executor not yet written)
    """
    if not dry_run:
        raise NotImplementedError(
            "deploy_kimi_split: actual remote execution (SSH/mesh overlay) "
            "lands with `adk mesh serve` (S3). dry_run=False is a S3 concern."
        )

    commands = render_deploy_commands(plan, model_dir, bin_dir, mmproj_path, port)

    return {
        "plan": plan,
        "commands": commands,
        "steps": commands["steps"],
        "dry_run": dry_run,
    }
