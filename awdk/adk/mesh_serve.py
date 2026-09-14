"""adk mesh serve — one-command Kimi-K3 community serving (S3).

Role-based on purpose: a customer mesh has no cross-node SSH assumption, so
EACH node runs its own role and the coordinator meets the backends over the
mesh overlay. The llama.cpp rpc-server is UNAUTHENTICATED — every bind goes
through kimi_coordinator.assert_rpc_bind (overlay/private only) and the
default-deny headscale ACL is the reachability boundary (see
.DEPLOYMENT/headscale/POLICY_ROLLOUT.md).

Roles:
  plan         — show the split plan for a node spec (no execution).
  rpc-backend  — build (if needed) + start rpc-server on this node.
  coordinator  — plan, download shards, build, start llama-server with
                 --rpc backends, health-gate, then advertise into the
                 community market via mesh_provider.provide().

Serving params are pinned per the Unsloth Kimi-K3 guide: temp=1.0,
top_p=0.95, mmproj vision tower loaded. Model attribution "Kimi K3" rides the
catalog entry (kimi-k3-community) — required by the Kimi K3 license at scale.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from adk.kimi_coordinator import (
    NodeBudget,
    assert_rpc_bind,
    deploy_kimi_split,
    plan_kimi_split,
    render_deploy_commands,
)
from adk.unsloth_gguf_download import (
    KIMI_K3_QUANTS,
    download_shards,
    list_kimi_shards,
)
from adk.unsloth_llamacpp_setup import (
    install_unsloth_llamacpp,
    verify_kimi_binary,
)

KIMI_MODEL_NAME = "kimi-k3"
COORDINATOR_PORT = 8080
RPC_PORT = 50052


def parse_nodes_spec(spec: str) -> list[NodeBudget]:
    """Parse ``id:host:ram_gb:vram_gb[,id:host:ram_gb:vram_gb...]``.

    Explicit and dumb on purpose: the mesh's own capacity endpoint
    (GET /nodes/kimi-capacity) is the discovery surface; this spec is how the
    operator pins exactly which nodes participate in THIS replica.
    """
    nodes: list[NodeBudget] = []
    for chunk in [c.strip() for c in (spec or "").split(",") if c.strip()]:
        parts = chunk.split(":")
        if len(parts) != 4:
            raise ValueError(
                f"bad node spec '{chunk}' — expected id:host:ram_gb:vram_gb"
            )
        node_id, host, ram_s, vram_s = parts
        try:
            ram_gb = float(ram_s)
            vram_gb = float(vram_s)
        except ValueError as exc:
            raise ValueError(
                f"bad node spec '{chunk}' — ram/vram must be numbers"
            ) from exc
        if not node_id or not host:
            raise ValueError(f"bad node spec '{chunk}' — empty id or host")
        nodes.append(
            NodeBudget(node_id=node_id, host=host, ram_gb=ram_gb, vram_gb=vram_gb)
        )
    if not nodes:
        raise ValueError("empty --nodes spec; expected id:host:ram_gb:vram_gb,...")
    return nodes


def serve_plan(nodes_spec: str, quant: str = "auto") -> dict[str, Any]:
    """Role `plan`: pure planning output for a pinned node set."""
    nodes = parse_nodes_spec(nodes_spec)
    plan = plan_kimi_split(nodes, quant=quant)
    quant_info = KIMI_K3_QUANTS[plan["quant"]]
    plan["download_gb"] = quant_info["size_gb"]
    plan["note"] = (
        "RPC split at this scale is proven at 27B/2-node only — the first "
        "real K3 run is the research edge. rpc-server is unauthenticated: "
        "binds are overlay-only and the default-deny ACL is the boundary."
    )
    return plan


def ensure_build(build_dir: str | Path, dry_run: bool = True) -> dict[str, Any]:
    """Ensure the Unsloth-fork binaries exist (idempotent; skips when verified)."""
    bin_dir = Path(build_dir) / "build" / "bin"
    verdict = verify_kimi_binary(bin_dir)
    if verdict.get("ok") and verdict.get("has_rpc"):
        return {"built": False, "bin_dir": str(bin_dir), "verify": verdict}
    if dry_run:
        return {
            "built": False,
            "bin_dir": str(bin_dir),
            "verify": verdict,
            "would_build": True,
        }
    result = install_unsloth_llamacpp(build_dir, cuda=True, rpc=True)
    if not result.get("success"):
        raise RuntimeError(f"Unsloth llama.cpp build failed: {result.get('error')}")
    return {"built": True, "bin_dir": str(bin_dir), "verify": verify_kimi_binary(bin_dir)}


def serve_rpc_backend(
    bind: str,
    port: int = RPC_PORT,
    build_dir: str | Path = "unsloth-llamacpp",
    dry_run: bool = True,
    _popen: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Role `rpc-backend`: start this node's rpc-server on its OVERLAY address.

    Refuses any non-private bind before anything else runs — an rpc-server on
    a public interface is an unauthenticated tensor-execution endpoint.
    """
    assert_rpc_bind(bind)
    build = ensure_build(build_dir, dry_run=dry_run)
    exe = Path(build["bin_dir"]) / "rpc-server"
    cmd = [str(exe), "--host", bind, "--port", str(port)]
    result: dict[str, Any] = {
        "role": "rpc-backend",
        "bind": bind,
        "port": port,
        "command": cmd,
        "build": build,
        "dry_run": dry_run,
        "started": False,
    }
    if dry_run:
        return result
    proc = _popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    result["pid"] = proc.pid
    result["started"] = True
    return result


def _health_gate(
    base_url: str,
    expected_rpc_devices: int,
    timeout_s: float = 300.0,
    poll_s: float = 5.0,
    _urlopen: Callable[..., Any] = urllib.request.urlopen,
    _sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll the coordinator until healthy AND the RPC devices are attached.

    A coordinator that comes up healthy but LOCAL-ONLY (backends unreachable)
    would silently serve at a fraction of the plan — that is the split_inference
    toolpack's documented trap, so it is a hard failure here, not a warning.
    """
    deadline = time.monotonic() + timeout_s
    last_err = ""
    while time.monotonic() < deadline:
        try:
            with _urlopen(f"{base_url}/props", timeout=10) as resp:
                props = json.loads(resp.read().decode("utf-8", errors="ignore"))
            devices = props.get("devices") or []
            rpc_devices = [
                d for d in devices
                if "rpc" in str(d.get("name", d)).lower()
            ]
            if len(rpc_devices) >= expected_rpc_devices:
                return {"ok": True, "rpc_devices": len(rpc_devices)}
            last_err = (
                f"coordinator up but only {len(rpc_devices)}/"
                f"{expected_rpc_devices} RPC devices attached (local-only trap)"
            )
        except Exception as exc:  # noqa: BLE001 - retried until deadline
            last_err = str(exc)
        _sleep(poll_s)
    return {"ok": False, "error": last_err}


async def serve_coordinator(
    nodes_spec: str,
    backends: str,
    quant: str = "auto",
    model_dir: str | Path = "kimi-k3-model",
    build_dir: str | Path = "unsloth-llamacpp",
    bind: str = "127.0.0.1",
    dry_run: bool = True,
    advertise: bool = True,
    tenant_id: Optional[str] = None,
    _provide: Optional[Callable[..., Any]] = None,
    _popen: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Role `coordinator`: plan → download → build → serve → health → advertise.

    `backends` is the comma-separated ``ip:port`` list of ALREADY-RUNNING
    rpc-servers (each started on its own node via role rpc-backend).
    Advertise reuses the LIVE market path verbatim: mesh_provider.provide()
    (advertise → consent → operator trust poll) — no parallel registry.
    """
    assert_rpc_bind(bind)
    backend_list = [b.strip() for b in (backends or "").split(",") if b.strip()]
    for b in backend_list:
        assert_rpc_bind(b.rsplit(":", 1)[0])

    nodes = parse_nodes_spec(nodes_spec)
    plan = plan_kimi_split(nodes, quant=quant)
    if len(backend_list) < len(plan["rpc_backends"]):
        raise ValueError(
            f"plan needs {len(plan['rpc_backends'])} rpc backends but only "
            f"{len(backend_list)} --backends given — start role rpc-backend "
            "on the missing nodes first"
        )

    model_dir = Path(model_dir)
    mmproj_path = model_dir / "mmproj-BF16.gguf"
    rendered = render_deploy_commands(
        plan,
        model_dir=str(model_dir),
        bin_dir=str(Path(build_dir) / "build" / "bin"),
        mmproj_path=str(mmproj_path),
    )

    steps: list[dict[str, Any]] = [
        {"step": "download", "quant": plan["quant"],
         "size_gb": KIMI_K3_QUANTS[plan["quant"]]["size_gb"]},
        {"step": "build", "build_dir": str(build_dir)},
        {"step": "serve", "command": rendered["coordinator_command"]},
        {"step": "health-gate", "expected_rpc_devices": len(backend_list)},
        {"step": "advertise" if advertise else "advertise-skipped",
         "model": KIMI_MODEL_NAME},
    ]
    result: dict[str, Any] = {
        "role": "coordinator",
        "plan": plan,
        "steps": steps,
        "dry_run": dry_run,
        "served": False,
    }
    if dry_run:
        return result

    # Every step below is long-running SYNC work (HTTP shard enumeration, hundreds
    # of GB of disk I/O, a CUDA build, a polling health gate). This coroutine is
    # CLI-driven today, but a blocking call in a coroutine is one import away from
    # stalling a service's whole event loop, so each is pushed to a thread.
    # asyncio.to_thread is correct HERE specifically because none of these schedule
    # background work via get_event_loop().create_task() — the trap that silently
    # kills fire-and-forget work when a sync subtree is moved off the loop.
    # 1. Shards (resumable; skips completed files via .part handling).
    have = {p.name for p in model_dir.glob("*.gguf")}
    shards = await asyncio.to_thread(list_kimi_shards, plan["quant"])
    need = {Path(s["path"]).name for s in shards}
    if not need.issubset(have):
        model_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(download_shards, plan["quant"], model_dir)

    # 2. Binaries.
    await asyncio.to_thread(ensure_build, build_dir, False)

    # 3. Serve.
    proc = _popen(
        rendered["coordinator_command"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    result["pid"] = proc.pid

    # 4. Health gate — hard-fails on the local-only trap. Polls with time.sleep
    # for up to 5 minutes, so it never runs on the caller's loop.
    gate = await asyncio.to_thread(
        _health_gate,
        f"http://{bind}:{COORDINATOR_PORT}",
        len(backend_list),
    )
    result["health"] = gate
    if not gate.get("ok"):
        raise RuntimeError(f"coordinator health gate failed: {gate.get('error')}")
    result["served"] = True

    # 5. Advertise into the live community market (advertise→consent→trust).
    if advertise:
        if _provide is None:
            from adk.mesh_provider import provide as _provide  # noqa: PLC0415
        result["provide"] = await _provide(
            inference_url=f"http://{bind}:{COORDINATOR_PORT}",
            inference_model=KIMI_MODEL_NAME,
            tenant_id=tenant_id,
        )
    return result


__all__ = [
    "parse_nodes_spec",
    "serve_plan",
    "serve_rpc_backend",
    "serve_coordinator",
    "ensure_build",
    "deploy_kimi_split",
]
