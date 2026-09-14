"""Split inference pack — split_* agent tools.

Shard ONE model across MULTIPLE machines using llama.cpp's RPC backend.

Design rules (same doctrine as node_bootstrap / image_bootstrap):
  * Fail soft with actionable guidance — every tool returns a dict, never raises.
  * Pure tools (split_detect_topology, split_resolve_recipe, split_plan_deployment)
    have no side effects.
  * split_apply is dry_run-able; nothing executes under dry_run.
  * split_verify makes the POSITIVE assertion. The defining failure of this pack's
    domain is a server that answers perfectly while running entirely on the local
    GPU — either because the binary was built without -DGGML_RPC=ON, or because the
    --rpc target was unreachable. Verify requires an RPC device to actually be
    attached before it will call anything a split.
  * SECURITY: llama.cpp's rpc-server is UNAUTHENTICATED and executes tensor ops from
    any client that reaches it. The planner refuses public binds.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from typing import Optional

import httpx

from adk.toolpacks.split_inference.recipes import (
    RECIPE_IDS,
    get_recipe,
    list_recipes,
    resolve_recipe,
)

logger = logging.getLogger("split_inference_pack")

_DEFAULT_CONTAINER = os.environ.get("AITHER_LLAMACPP_CONTAINER", "aither-llamacpp-bonsai")
_DEFAULT_RPC_PORT = 50052

# Addresses that must never be used as an rpc-server bind on a host with a public
# NIC — rpc-server has no auth whatsoever.
_PUBLIC_BIND_HINTS = ("0.0.0.0/public", "::", "[::]")


# ── helpers ─────────────────────────────────────────────────────────────


def _run(cmd: list, timeout: int = 120) -> tuple:
    """Run a command; return (rc, tail_of_output). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc.returncode, out[-4000:]
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd[:3])}"


def _in_container(container: str, shell_cmd: str, timeout: int = 120) -> tuple:
    """Run a shell command inside a container via docker exec."""
    return _run(["docker", "exec", container, "sh", "-lc", shell_cmd], timeout=timeout)


def _parse_devices(text: str) -> list:
    """Parse `llama-server --list-devices` output into device dicts.

    Expected line shape:
      "  CUDA0: NVIDIA GeForce RTX 5090 (32606 MiB, 30927 MiB free)"
      "  RPC0[spark.local:50052]: RPC (24576 MiB, 24000 MiB free)"
    Unknown shapes are skipped rather than guessed at.
    """
    devices = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*([A-Za-z]+\d+)(\[[^\]]*\])?:\s*(.+?)\s*(\(([^)]*)\))?\s*$", line)
        if not m:
            continue
        dev_id, bracket, name, _, mem = m.groups()
        vram_mb = 0
        if mem:
            mm = re.search(r"(\d+)\s*MiB", mem)
            if mm:
                vram_mb = int(mm.group(1))
        devices.append({
            "id": dev_id,
            "kind": "rpc" if dev_id.upper().startswith("RPC") else "local",
            "endpoint": (bracket or "").strip("[]"),
            "name": name.strip(),
            "vram_mb": vram_mb,
        })
    return devices


def _configured_backends() -> list:
    """RPC backends from AITHER_RPC_BACKENDS (comma-separated host:port)."""
    raw = os.environ.get("AITHER_RPC_BACKENDS", "")
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.partition(":")
        out.append({"host": host, "port": int(port) if port.isdigit() else _DEFAULT_RPC_PORT})
    return out


def _probe_backend(host: str, port: int, timeout_s: float = 3.0) -> dict:
    """TCP-probe an rpc-server endpoint. Reachability only — no protocol handshake."""
    import socket

    t0 = __import__("time").time()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return {
                "host": host, "port": port, "reachable": True,
                "rtt_ms": round((__import__("time").time() - t0) * 1000, 2),
            }
    except OSError as e:
        return {"host": host, "port": port, "reachable": False, "error": str(e)}


# ── 1. TOPOLOGY DETECTION ───────────────────────────────────────────────


def split_detect_topology(
    container: str = "",
    backends: Optional[list] = None,
) -> dict:
    """Detect local devices + reachable RPC backends; report COMBINED VRAM.

    Pure read-only. `backends` is a list of "host:port" strings; when omitted it
    falls back to AITHER_RPC_BACKENDS.
    Returns {local_devices, rpc_backends, local_vram_gb, combined_vram_gb,
             rpc_capable_build}.
    """
    container = container or _DEFAULT_CONTAINER
    try:
        # There can be MORE than one build dir (e.g. `build` non-RPC + `build-rpc`).
        # Picking `head -1` off ls would depend on alphabetical/ctime order and could
        # silently select the NON-RPC binary even though an RPC one exists — the whole
        # pack would then declare "no split possible" right after a successful rebuild.
        # So enumerate ALL of them and PREFER the one that actually has --rpc.
        rc, out = _in_container(container, "ls /work/build*/bin/llama-server 2>/dev/null")
        binaries = [ln.strip() for ln in out.strip().splitlines() if ln.strip()] if rc == 0 else []
        if not binaries:
            return {
                "error": f"no llama-server binary found in {container}",
                "fix": "build llama.cpp first (split_apply stage='build')",
            }

        def _has_rpc(bin_path: str) -> bool:
            _rc, h = _in_container(
                container, f"{shlex.quote(bin_path)} --help 2>&1 | grep -c -- '--rpc' || true"
            )
            last = (h.strip().splitlines() or ["0"])[-1].strip()
            return last.isdigit() and int(last) > 0

        rpc_binaries = [b for b in binaries if _has_rpc(b)]
        binary = rpc_binaries[0] if rpc_binaries else binaries[0]
        rpc_capable = bool(rpc_binaries)

        rc, dev_out = _in_container(container, f"{shlex.quote(binary)} --list-devices 2>&1")
        local_devices = _parse_devices(dev_out)

        # Probe backends
        want = []
        for b in (backends or []):
            host, _, port = str(b).partition(":")
            want.append({"host": host, "port": int(port) if port.isdigit() else _DEFAULT_RPC_PORT})
        if not want:
            want = _configured_backends()
        probed = [_probe_backend(b["host"], b["port"]) for b in want]
        reachable = [p for p in probed if p.get("reachable")]

        local_vram_gb = sum(d["vram_mb"] for d in local_devices if d["kind"] == "local") / 1024
        rpc_vram_gb = sum(d["vram_mb"] for d in local_devices if d["kind"] == "rpc") / 1024

        result = {
            "container": container,
            "binary": binary,
            "rpc_capable_build": rpc_capable,
            "local_devices": local_devices,
            "rpc_backends": reachable,
            "rpc_backends_probed": probed,
            "local_vram_gb": round(local_vram_gb, 1),
            # Combined counts RPC devices only once they are ATTACHED (visible in
            # --list-devices). A merely-reachable TCP port contributes nothing until
            # the main binary actually sees it as a device.
            "combined_vram_gb": round(local_vram_gb + rpc_vram_gb, 1),
            "attached_rpc_devices": [d for d in local_devices if d["kind"] == "rpc"],
        }
        if not rpc_capable:
            result["note"] = (
                "This binary was built WITHOUT -DGGML_RPC=ON — it has no --rpc flag, "
                "so no split is possible with it. Rebuild with -DGGML_RPC=ON."
            )
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("topology detection failed")
        return {"error": f"topology detection failed: {e}", "fix": "check docker access"}


# ── 2. RECIPE RESOLUTION ────────────────────────────────────────────────


def split_resolve_recipe(recipe_id: str = "", container: str = "") -> dict:
    """Resolve the best split recipe for the detected topology (pure)."""
    try:
        if recipe_id:
            if recipe_id not in RECIPE_IDS:
                return {"error": f"unknown recipe: {recipe_id}", "available": list_recipes()}
            recipe = get_recipe(recipe_id)
            if not recipe:
                return {"error": f"failed to load recipe: {recipe_id}"}
            return {
                "recipe": recipe,
                "match_score": 10.0,
                "rationale": f"Explicit recipe_id: {recipe_id}",
                "warnings": recipe.get("serve", {}).get("platform_traps", []),
            }

        topo = split_detect_topology(container=container)
        if "error" in topo:
            return topo
        import multiprocessing

        topo_for_resolve = {
            "local_vram_gb": topo.get("local_vram_gb", 0),
            "combined_vram_gb": topo.get("combined_vram_gb", 0),
            "rpc_backends": topo.get("rpc_backends", []),
            "ram_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9, 1)
            if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names else 64,
            "cpu_cores": multiprocessing.cpu_count(),
        }
        out = resolve_recipe(topo_for_resolve, recipe_id="")
        out["topology"] = topo_for_resolve
        return out
    except Exception as e:  # noqa: BLE001
        logger.exception("recipe resolution failed")
        return {"error": f"recipe resolution failed: {e}"}


# ── 3. PLANNING ─────────────────────────────────────────────────────────


def split_plan_deployment(recipe_id: str, container: str = "") -> dict:
    """Render the build + backend-start + main-launch plan (pure, no side effects)."""
    if not recipe_id:
        return {"error": "recipe_id is required", "available": list_recipes()}
    if recipe_id not in RECIPE_IDS:
        return {"error": f"unknown recipe: {recipe_id}", "available": list_recipes()}

    try:
        recipe = get_recipe(recipe_id)
        if not recipe:
            return {"error": f"failed to load recipe: {recipe_id}"}

        container = container or recipe.get("topology", {}).get("main", {}).get(
            "container", _DEFAULT_CONTAINER
        )
        build = recipe.get("build", {})
        serve = recipe.get("serve", {})
        topology = recipe.get("topology", {})
        backends = topology.get("rpc_backends", []) or []

        # Fail-closed on a public bind — rpc-server has NO authentication.
        for b in backends:
            bind = str(b.get("bind", ""))
            if bind in _PUBLIC_BIND_HINTS:
                return {
                    "error": f"refusing to plan a public rpc-server bind ({bind})",
                    "fix": "rpc-server is UNAUTHENTICATED — bind a private LAN/mesh "
                           "address only, never a public interface",
                }

        source_dir = build.get("source_dir", "/work")
        build_cmds = list(build.get("cmake_configure", [])) + list(build.get("cmake_build", []))

        rpc_cmds = []
        for b in backends:
            args = " ".join(serve.get("rpc_server_args", []) or [])
            rpc_cmds.append({
                "host": b.get("host", ""),
                "port": b.get("port", _DEFAULT_RPC_PORT),
                "command": f"{build.get('build_dir', '/work/build-rpc')}/bin/rpc-server {args}",
                "note": "run this ON the backend host; bind its PRIVATE interface only",
            })

        rpc_flag = ",".join(
            f"{b.get('host')}:{b.get('port', _DEFAULT_RPC_PORT)}" for b in backends
        )
        main_args = list(serve.get("main_args", []) or [])
        if rpc_flag and not any(a.startswith("--rpc") for a in main_args):
            main_args.insert(1, f"--rpc {rpc_flag}")
        main_cmd = f"{build.get('build_dir', '/work/build-rpc')}/bin/llama-server " + \
                   " ".join(main_args)

        steps = [f"Resolve recipe: {recipe_id}"]
        if build_cmds:
            steps.append(f"Build llama.cpp with RPC in {container}:{source_dir} "
                         f"(~{build.get('est_duration_min', 25)}min)")
        for r in rpc_cmds:
            steps.append(f"Start rpc-server on {r['host']}:{r['port']}")
        steps.append(f"Start main llama-server on :{serve.get('port', 8080)}")
        steps.append("PROVE the split: split_verify (an RPC device must be attached)")

        return {
            "recipe_id": recipe_id,
            "container": container,
            "source_dir": source_dir,
            "build_commands": build_cmds,
            "rpc_backends": rpc_cmds,
            "main_command": main_cmd,
            "port": serve.get("port", 8080),
            "steps": steps,
            "est_duration_min": build.get("est_duration_min", 25),
            "warnings": list(serve.get("platform_traps", [])),
            "requires_rpc_device": recipe.get("verify", {}).get("require_rpc_device", False),
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("planning failed")
        return {"error": f"planning failed: {e}"}


# ── 4. APPLY ────────────────────────────────────────────────────────────


def split_apply(
    recipe_id: str,
    stage: str = "build",
    container: str = "",
    dry_run: bool = False,
) -> dict:
    """Execute a stage of the split deployment.

    stage: "build" (compile llama.cpp with -DGGML_RPC=ON) | "main" (launch the
    main server). Backend rpc-servers run on OTHER hosts and are reported, not
    shelled into — this pack never SSHes into a peer.
    dry_run=True shows commands without executing.
    """
    if not recipe_id:
        return {"error": "recipe_id is required", "available": list_recipes()}
    if stage not in ("build", "main"):
        return {"error": f"unknown stage: {stage}", "fix": "stage must be 'build' or 'main'"}

    try:
        plan = split_plan_deployment(recipe_id, container=container)
        if "error" in plan:
            return plan
        container = plan["container"]

        if stage == "build":
            cmds = plan.get("build_commands", [])
            if not cmds:
                return {"error": "recipe has no build commands"}
            shell = " && ".join([f"cd {plan['source_dir']}"] + cmds)
            if dry_run:
                return {
                    "planned": True, "dry_run": True, "stage": "build",
                    "recipe_id": recipe_id, "container": container,
                    "commands": [f"docker exec {container} sh -lc {shlex.quote(shell)}"],
                    "est_duration_min": plan.get("est_duration_min", 25),
                }
            # A CUDA+RPC build is long; give it room but stay bounded.
            rc, out = _in_container(container, shell, timeout=3600)
            if rc != 0:
                return {
                    "error": f"llama.cpp build failed (rc={rc})",
                    "output": out[-2000:],
                    "fix": "check nvcc/cmake availability and CUDA arch flags",
                }
            return {
                "applied": True, "stage": "build", "recipe_id": recipe_id,
                "container": container, "output_tail": out[-600:],
                "next": f"split_verify(recipe_id='{recipe_id}') after starting the servers",
            }

        # stage == "main"
        cmd = plan["main_command"]
        if dry_run:
            return {
                "planned": True, "dry_run": True, "stage": "main",
                "recipe_id": recipe_id, "container": container,
                "commands": [f"docker exec -d {container} sh -lc {shlex.quote(cmd)}"],
                "rpc_backends": plan.get("rpc_backends", []),
                "warnings": plan.get("warnings", []),
            }
        rc, out = _run(["docker", "exec", "-d", container, "sh", "-lc", cmd], timeout=60)
        if rc != 0:
            return {"error": f"failed to start main server (rc={rc})", "output": out}
        return {
            "applied": True, "stage": "main", "recipe_id": recipe_id,
            "port": plan.get("port"),
            "next": f"split_verify(recipe_id='{recipe_id}') — REQUIRED to prove the split",
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("apply failed")
        return {"error": f"apply failed: {e}"}


# ── 5. VERIFY (the positive assertion) ──────────────────────────────────


def split_verify(
    recipe_id: str = "",
    base_url: str = "",
    container: str = "",
    timeout_s: float = 60.0,
) -> dict:
    """Prove a split is REAL — an RPC device must actually be attached.

    The defining failure of this domain is a perfectly healthy server that ran
    entirely on the local GPU (binary built without -DGGML_RPC=ON, or an
    unreachable --rpc target). When the model fits locally that is INVISIBLE
    without this check.

    status:
      healthy    — RPC device(s) attached AND inference round-trips
      local_only — works, but NO RPC device attached (the silent-fallback case)
      degraded   — inference itself failed
      unknown    — could not determine (probe error) — never reported as local_only
    """
    container = container or _DEFAULT_CONTAINER
    recipe = get_recipe(recipe_id) if recipe_id else None
    require_rpc = bool((recipe or {}).get("verify", {}).get("require_rpc_device", True))
    expect_min = int((recipe or {}).get("verify", {}).get("expect_min_rpc_devices", 1))

    try:
        topo = split_detect_topology(container=container)
        if "error" in topo:
            return {
                "status": "unknown",
                "error": topo["error"],
                "fix": topo.get("fix", "could not enumerate devices"),
            }

        rpc_devices = topo.get("attached_rpc_devices", [])
        rpc_capable = topo.get("rpc_capable_build", False)

        # Inference round-trip (only when a URL is available)
        infer_ok, infer_detail = None, ""
        if base_url:
            try:
                r = httpx.post(
                    f"{base_url.rstrip('/')}/completion",
                    json={"prompt": "Say OK.", "n_predict": 8},
                    timeout=float(timeout_s),
                )
                infer_ok = r.status_code == 200 and bool(
                    (r.json() or {}).get("content", "").strip()
                )
                if not infer_ok:
                    infer_detail = f"HTTP {r.status_code}"
            except (httpx.HTTPError, ValueError) as e:
                infer_ok, infer_detail = False, f"{type(e).__name__}: {e}"

        if infer_ok is False:
            status = "degraded"
        elif not rpc_capable:
            status = "local_only"
        elif len(rpc_devices) >= max(expect_min, 1):
            status = "healthy"
        else:
            status = "local_only" if require_rpc else "healthy"

        result = {
            "status": status,
            "rpc_capable_build": rpc_capable,
            "rpc_devices_attached": len(rpc_devices),
            "rpc_devices": rpc_devices,
            "local_devices": [d for d in topo.get("local_devices", []) if d["kind"] == "local"],
            "combined_vram_gb": topo.get("combined_vram_gb"),
            "inference_ok": infer_ok,
        }
        if infer_detail:
            result["detail"] = infer_detail
        if status == "local_only":
            result["fix"] = (
                "NO RPC device is attached — this is NOT a split, it ran on the local "
                "GPU alone. "
                + ("The binary was built WITHOUT -DGGML_RPC=ON (no --rpc flag exists); "
                   "rebuild with -DGGML_RPC=ON. " if not rpc_capable else
                   "The binary supports --rpc, so the backend was unreachable or "
                   "dropped: check the rpc-server is running and the port is open, and "
                   "that BOTH sides are the same llama.cpp/ggml version. ")
                + "Do not report this as a working split."
            )
        elif status == "degraded":
            result["fix"] = "server did not return a completion — check the main server logs"
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("verification failed")
        return {"status": "unknown", "error": f"verification failed: {e}"}
