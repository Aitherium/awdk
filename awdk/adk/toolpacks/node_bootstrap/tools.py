"""Node bootstrap pack — node_* agent tools.

Design rules (same doctrine as other tool packs):
  * Fail soft with actionable guidance — every tool returns a dict, never raises.
  * Missing credentials/URLs → {"error": ..., "fix": ...}, never anonymous.
  * Pure tools (node_detect_hardware, node_resolve_recipe, node_plan_deployment)
    have no side effects.
  * Deployment tools (node_apply) are idempotent; dry_run=True shows commands
    without executing.
  * Remote deployment uses SSH key-based auth only (no passwords, TLS stays verified).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shlex
import subprocess
from pathlib import Path
from typing import Optional

import httpx

from adk.toolpacks.node_bootstrap.recipes import (
    RECIPE_IDS,
    get_recipe,
    list_recipes,
    resolve_recipe,
)

logger = logging.getLogger("node_bootstrap_pack")

_BOOTSTRAP_DIR = Path.home() / ".aither" / "node-bootstrap"
_TIMEOUT_DEFAULT = 60.0


# ── 1. DETECTION ─────────────────────────────────────────────────────────


def node_detect_hardware(verbose: bool = False) -> dict:
    """Detect system hardware (CPU, RAM, GPU).

    Pure local operation — no network, no filesystem writes.
    Returns {system_info, recommendation, recommended_recipe}.
    """
    try:
        from adk.hardware_probe import detect_system, recommend_setup

        sysinfo = detect_system()
        rec = recommend_setup(sysinfo)

        # Convert dataclass to dict
        system_dict = {
            "ram_gb": sysinfo.ram_gb,
            "cpu_cores": sysinfo.cpu_cores,
            "gpu_vendor": sysinfo.gpu_vendor,
            "gpu_name": sysinfo.gpu_name,
            "gpu_vram_mb": sysinfo.gpu_vram_mb,
            "ollama_installed": sysinfo.ollama_installed,
            "python_version": sysinfo.python_version,
        }

        rec_dict = {
            "backend": rec.backend,
            "local_model": rec.local_model,
            "local_model_gb": rec.local_model_gb,
            "local_vram_gb": rec.local_vram_gb,
            "rationale": rec.rationale,
            "warnings": rec.warnings,
        }

        # Map recommendation backend to recipe ID
        # Use the REAL resolver for the recommendation — the coarse
        # recommend_setup tier map said "cuda-vllm-8gb" for a 32GB card
        # (it only knows cloud/ollama/local-8b/local-3b tiers). Keep the
        # tier map only as a fallback if resolution errors.
        resolved = resolve_recipe(system_dict, prefer_backend="auto")
        if isinstance(resolved, dict) and resolved.get("recipe"):
            recommended_recipe = resolved["recipe"].get("id", "")
        else:
            recommended_recipe = _map_recommendation_to_recipe(rec)

        return {
            "system_info": system_dict,
            "recommendation": rec_dict,
            "recommended_recipe": recommended_recipe,
        }
    except Exception as e:
        logger.exception("Hardware detection failed")
        return {
            "error": f"hardware detection failed: {e}",
            "fix": "check system permissions and hardware accessibility",
        }


def _map_recommendation_to_recipe(rec) -> str:
    """Map a Recommendation object to a recipe ID."""
    if rec.backend == "local-8b":
        if "nvidia" in (rec.local_model or "").lower():
            return "cuda-vllm-24gb"
        return "cuda-vllm-8gb"
    elif rec.backend == "local-3b":
        return "cpu-ollama"
    elif rec.backend == "ollama":
        return "metal-ollama" if "apple" in platform.system().lower() \
               else "cpu-ollama"
    else:
        return "cloud-api"


# ── 2. RECIPE RESOLUTION ────────────────────────────────────────────────


def node_resolve_recipe(
    prefer_backend: str = "auto",
    recipe_id: str = "",
) -> dict:
    """Resolve the best recipe for this system.

    Pure operation — calls detect_hardware internally when needed.
    Returns {recipe, match_score, rationale, warnings}.
    """
    try:
        # Explicit recipe_id wins
        if recipe_id:
            if recipe_id not in RECIPE_IDS:
                return {
                    "error": f"unknown recipe: {recipe_id}",
                    "available": list_recipes(),
                }
            recipe = get_recipe(recipe_id)
            if not recipe:
                return {
                    "error": f"failed to load recipe: {recipe_id}",
                    "available": list_recipes(),
                }
            return {
                "recipe": recipe,
                "match_score": 10.0,
                "rationale": f"Explicit recipe_id: {recipe_id}",
                "warnings": recipe.get("inference_config", {})
                    .get("platform_traps", []),
            }

        # Auto-detect hardware and resolve
        from adk.hardware_probe import detect_system

        sysinfo = detect_system()
        system_dict = {
            "ram_gb": sysinfo.ram_gb,
            "cpu_cores": sysinfo.cpu_cores,
            "gpu_vendor": sysinfo.gpu_vendor,
            "gpu_name": sysinfo.gpu_name,
            "gpu_vram_mb": sysinfo.gpu_vram_mb,
            "unified_memory": sysinfo.gpu_vendor == "apple",
        }

        result = resolve_recipe(
            system_dict,
            prefer_backend=prefer_backend,
            recipe_id="",
        )
        return result
    except Exception as e:
        logger.exception("Recipe resolution failed")
        return {
            "error": f"recipe resolution failed: {e}",
            "fix": "check hardware detection and recipe files",
        }


# ── 3. DEPLOYMENT PLANNING ──────────────────────────────────────────────


def _render_compose(recipe_id: str, recipe: dict) -> str:
    """Render a minimal, self-contained docker-compose YAML from recipe fields.

    Public-safe: image/args/ports all come from the recipe. Engines:
      vllm   -> command = model + serve_args (vllm/vllm-openai entrypoint)
      ollama -> env vars from serve_args (KEY=VALUE strings), model pulled later
    """
    inference = recipe.get("inference_config", {})
    deployment = recipe.get("deployment", {})
    engine = inference.get("engine", "")
    image = inference.get("image", "") or "vllm/vllm-openai:latest"
    port = deployment.get("port", 8000)
    models = inference.get("models", [])
    serve_args: list = []
    for arg in inference.get("serve_args", []) or []:
        if isinstance(arg, str):
            serve_args.extend(arg.split())
    gpu_block = ""
    if engine == "vllm":
        model_src = str(models[0].get("source", "")) if models else ""
        model_ref = model_src.split("://", 1)[1] if "://" in model_src else model_src
        cmd = ["--model", model_ref, *serve_args]
        command_line = "    command: " + json.dumps(cmd)
        env_lines = [
            "      HF_TOKEN: ${HF_TOKEN:-}",
            "      VLLM_USE_V2_MODEL_RUNNER: ${VLLM_USE_V2_MODEL_RUNNER:-0}",
        ]
        gpu_block = (
            "    deploy:\n"
            "      resources:\n"
            "        reservations:\n"
            "          devices:\n"
            "            - driver: nvidia\n"
            "              count: 1\n"
            "              capabilities: [gpu]\n"
        )
        volumes = "    volumes:\n      - node-bootstrap-hf-cache:/root/.cache/huggingface\n"
    elif engine == "llamacpp":
        # llama-server takes plain CLI flags and the weights are baked into the
        # image (or bind-mounted), so there is no --model injection like vllm.
        #
        # Without this branch, llamacpp fell into the env-driven `else` below and
        # rendered SILENT NONSENSE: the env filter requires "=" and no spaces, so
        # every flag ("--ctx-size 32768", "--reasoning-budget 256", …) was dropped,
        # `command` was omitted entirely, and the service inherited the vllm image
        # default plus an /root/.ollama volume. Verified 2026-07-25 by rendering
        # cpu-1bit-llamacpp: 0 of 7 serve_args survived.
        if not inference.get("image"):
            # Fail LOUD rather than emit a compose that names the wrong runtime.
            # Q1_0 needs the PrismML llama.cpp fork — there is no safe public
            # default to fall back on.
            raise ValueError(
                f"recipe {recipe_id}: engine 'llamacpp' requires an explicit "
                "inference_config.image (no safe default — Q1_0 needs the "
                "PrismML fork), got empty"
            )
        command_line = "    command: " + json.dumps(list(serve_args))
        env_lines = []
        volumes = "    volumes:\n      - node-bootstrap-llamacpp:/models\n"
    else:  # ollama and other env-driven engines
        command_line = ""
        env_lines = [f"      {a}" for a in inference.get("serve_args", []) or []
                     if isinstance(a, str) and "=" in a and " " not in a]
        env_lines = [ln.replace("=", ": ", 1) for ln in env_lines]
        volumes = "    volumes:\n      - node-bootstrap-ollama:/root/.ollama\n"
    parts = [
        f"# Generated by adk node_bootstrap from recipe {recipe_id} — do not edit by hand.",
        "services:",
        f"  {recipe_id}:",
        f"    image: {image}",
        f"    ports:\n      - \"{port}:{port}\"",
        "    restart: unless-stopped",
    ]
    if command_line:
        parts.append(command_line)
    if env_lines:
        parts.append("    environment:\n" + "\n".join(env_lines))
    parts.append(volumes.rstrip("\n"))
    if gpu_block:
        parts.append(gpu_block.rstrip("\n"))
    parts.append(
        "volumes:\n  node-bootstrap-hf-cache:\n  node-bootstrap-ollama:"
        "\n  node-bootstrap-llamacpp:"
    )
    return "\n".join(parts) + "\n"


def node_plan_deployment(
    recipe_id: str,
    node_ip: str = "",
    ssh_user: str = "",
) -> dict:
    """Render a deployment plan (pure, no side effects).

    Returns {steps, compose_template or systemd_unit or delegate, env_dict,
             ports, download_size_mb, est_duration_min}.
    """
    if not recipe_id:
        return {"error": "recipe_id is required"}
    if recipe_id not in RECIPE_IDS:
        return {"error": f"unknown recipe: {recipe_id}"}

    try:
        recipe = get_recipe(recipe_id)
        if not recipe:
            return {"error": f"failed to load recipe: {recipe_id}"}

        deployment = recipe.get("deployment", {})
        target = deployment.get("target", "docker-compose")
        compose_template = deployment.get("compose_template", "")
        delegate = deployment.get("delegate", "")
        port = deployment.get("port", 8000)

        inference = recipe.get("inference_config", {})
        models = inference.get("models", [])
        dl_size = sum(m.get("size_gb", 0) for m in models)

        # Build steps
        steps = []
        steps.append(f"Resolve recipe: {recipe_id}")
        if target == "docker-compose":
            steps.append(f"Load template: {compose_template}")
            steps.append("Start containers via docker compose")
            steps.append(f"Wait for health check on :{port}")
        elif target == "systemd":
            steps.append("Generate systemd unit")
            steps.append("Enable and start service")
        elif delegate:
            steps.append(f"Delegate to: {delegate}")

        # Environment for the deployment
        env = {
            "AITHER_RECIPE_ID": recipe_id,
            "AITHER_DEPLOYMENT_TARGET": target,
            "AITHER_INFERENCE_PORT": str(port),
        }

        backend_cfg = recipe.get("backend_config", {})
        if backend_cfg:
            env["AITHER_BACKEND_TYPE"] = backend_cfg.get("backend_type", "")

        result = {
            "recipe_id": recipe_id,
            "steps": steps,
            "env": env,
            "port": port,
            "download_size_mb": int(dl_size * 1024),
            "est_duration_min": 10 if target == "docker-compose" else 5,
        }

        if target == "docker-compose":
            # Self-contained: render the compose content FROM the recipe so apply
            # never depends on fleet-repo template files (which don't ship in the
            # public wheel). compose_template is kept as advisory metadata for
            # fleet-side deployments that prefer the richer template.
            result["compose_template"] = compose_template
            result["compose_yaml"] = _render_compose(recipe_id, recipe)
        elif target == "native":
            # e.g. Apple Silicon: Docker has no Metal GPU passthrough — the engine
            # must run natively (brew-installed ollama). apply() pulls models if
            # the binary is present; install itself stays a documented manual step.
            models = inference.get("models", [])
            pulls = [
                f"ollama pull {m['source'].split('://', 1)[1]}"
                for m in models
                if str(m.get("source", "")).startswith("ollama://")
            ]
            result["native_commands"] = [
                "# install (once): brew install ollama && brew services start ollama",
                *pulls,
            ]
        elif delegate:
            # Recipe defers to a proven fleet playbook/script (e.g. the 5090
            # dual-stack or the Bonsai systemd node). The public pack never
            # shells into fleet repos — it reports the exact delegate to run.
            result["delegate"] = delegate

        if node_ip:
            result["mode"] = "remote"
            result["node_ip"] = node_ip
            result["ssh_user"] = ssh_user or "aither"
        else:
            result["mode"] = "local"

        return result
    except Exception as e:
        logger.exception("Deployment planning failed")
        return {
            "error": f"deployment planning failed: {e}",
            "fix": "check recipe structure and system permissions",
        }


# ── 4. DEPLOYMENT APPLICATION ──────────────────────────────────────────


def node_apply(
    recipe_id: str,
    node_ip: str = "",
    ssh_user: str = "",
    ssh_key: str = "",
    dry_run: bool = False,
) -> dict:
    """Apply a deployment plan.

    Local mode (no node_ip): docker compose or systemd unit.
    Remote mode (node_ip): SSH key-based only.
    Idempotent: checks existing containers/units first.
    dry_run=True: show commands without executing.
    """
    if not recipe_id:
        return {"error": "recipe_id is required"}

    try:
        # Get the plan first
        plan = node_plan_deployment(recipe_id, node_ip, ssh_user)
        if "error" in plan:
            return plan

        mode = plan.get("mode", "local")
        if plan.get("compose_yaml"):
            target = "docker-compose"
        elif plan.get("native_commands"):
            target = "native"
        elif plan.get("delegate"):
            target = "delegate"
        else:
            target = "none"

        if dry_run:
            # Honest dry run: exactly what apply would execute, nothing executed.
            commands = []
            if target == "docker-compose":
                compose_file = _BOOTSTRAP_DIR / f"{recipe_id}-compose.yml"
                commands.append(f"# write compose file: {compose_file}")
                commands.append(f"docker compose -f {compose_file} up -d")
            elif target == "native":
                commands.extend(plan.get("native_commands", []))
            elif target == "delegate":
                commands.append(f"# run the fleet delegate: {plan['delegate']}")
            return {
                "planned": True,
                "dry_run": True,
                "recipe_id": recipe_id,
                "target": target,
                "commands": commands,
                "env": plan.get("env", {}),
            }

        # Actual execution
        if mode == "remote" and node_ip:
            return _apply_remote(recipe_id, node_ip, ssh_user, ssh_key, plan, target)
        return _apply_local(recipe_id, plan, target)

    except Exception as e:
        logger.exception("Deployment apply failed")
        return {
            "error": f"deployment apply failed: {e}",
            "fix": "check recipe, permissions, and system state",
        }


def _run(cmd: list, timeout: int = 900) -> tuple:
    """Run a command; return (rc, tail_of_output). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc.returncode, out[-2000:]
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd[:3])}"


def _apply_local(recipe_id: str, plan: dict, target: str) -> dict:
    """Apply deployment locally — actually executes.

    Contract: {"applied": True, ...} ONLY after the engine really started (or the
    model pull really ran). Anything short of that is an error dict or
    {"planned": True} for delegate targets the public pack must not shell into.
    """
    try:
        if target == "docker-compose":
            compose_yaml = plan.get("compose_yaml", "")
            if not compose_yaml:
                return {"error": "plan has no compose_yaml", "fix": "re-run node_plan_deployment"}
            _BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)
            compose_file = _BOOTSTRAP_DIR / f"{recipe_id}-compose.yml"
            compose_file.write_text(compose_yaml, encoding="utf-8")
            rc, out = _run(["docker", "compose", "-f", str(compose_file), "up", "-d"])
            if rc != 0:
                return {
                    "error": f"docker compose up failed (rc={rc})",
                    "output": out,
                    "compose_file": str(compose_file),
                    "fix": "check docker is running and the GPU runtime is configured",
                }
            return {
                "applied": True,
                "recipe_id": recipe_id,
                "mode": "docker-compose",
                "compose_file": str(compose_file),
                "next": f"node_verify(base_url='http://localhost:{plan.get('port', 8000)}')",
            }
        if target == "native":
            # Native engine (e.g. ollama on Apple Silicon). Pull models only if
            # the binary exists; installing system packages is out of scope.
            rc, _ = _run(["ollama", "--version"], timeout=15)
            if rc != 0:
                return {
                    "error": "ollama binary not found",
                    "fix": "install natively first: brew install ollama && "
                           "brew services start ollama (Docker on macOS has no "
                           "Metal GPU passthrough)",
                }
            pulled, failures = [], []
            for cmd in plan.get("native_commands", []):
                if not cmd.startswith("ollama pull "):
                    continue
                rc, out = _run(cmd.split(), timeout=3600)
                (pulled if rc == 0 else failures).append(cmd.split()[-1])
                if rc != 0:
                    logger.warning("pull failed: %s: %s", cmd, out[-200:])
            if not pulled:
                return {"error": "no models pulled", "failed": failures,
                        "fix": "check network and model tags"}
            return {"applied": True, "recipe_id": recipe_id, "mode": "native",
                    "models_pulled": pulled, "failed": failures}
        if target == "delegate":
            # Fleet playbooks/scripts are not shipped in the public wheel — report
            # the exact command instead of pretending to run it.
            return {
                "planned": True,
                "recipe_id": recipe_id,
                "mode": "delegate",
                "delegate": plan.get("delegate", ""),
                "fix": f"run the fleet delegate: {plan.get('delegate', '')}",
            }
        return {"error": f"unsupported deployment target: {target}"}
    except Exception as e:
        logger.exception("Local deployment failed")
        return {"error": f"local deployment failed: {e}"}


def _apply_remote(
    recipe_id: str,
    node_ip: str,
    ssh_user: str,
    ssh_key: str,
    plan: dict,
    target: str,
) -> dict:
    """Apply deployment remotely via SSH (key-based only) — actually executes."""
    if not ssh_key:
        return {
            "error": "ssh_key is required for remote deployment",
            "fix": "provide ssh_key path or content",
        }
    if target != "docker-compose":
        return {
            "error": f"remote apply supports docker-compose targets only (got {target})",
            "fix": "run delegate/native recipes on the node itself",
        }

    ssh_user = ssh_user or "aither"

    try:
        key_path = Path(ssh_key).expanduser()
        if not key_path.exists():
            return {
                "error": f"ssh key not found: {ssh_key}",
                "fix": "provide valid path to SSH private key",
            }
        compose_yaml = plan.get("compose_yaml", "")
        if not compose_yaml:
            return {"error": "plan has no compose_yaml", "fix": "re-run node_plan_deployment"}

        # Stage locally, ship, and start — every param shell-quoted (ssh_user/
        # node_ip/recipe_id are caller-supplied; unquoted they can smuggle
        # arbitrary shell into the remote command).
        _BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)
        local_file = _BOOTSTRAP_DIR / f"{recipe_id}-compose.yml"
        local_file.write_text(compose_yaml, encoding="utf-8")
        q_key = shlex.quote(str(key_path))
        q_dest = shlex.quote(f"{ssh_user}@{node_ip}")
        q_remote = shlex.quote(f"~/.aither/node-bootstrap/{recipe_id}-compose.yml")
        steps = [
            ["ssh", "-i", str(key_path), "-o", "BatchMode=yes",
             f"{ssh_user}@{node_ip}", "mkdir -p ~/.aither/node-bootstrap"],
            ["scp", "-i", str(key_path), str(local_file),
             f"{ssh_user}@{node_ip}:.aither/node-bootstrap/"],
            ["ssh", "-i", str(key_path), "-o", "BatchMode=yes",
             f"{ssh_user}@{node_ip}",
             f"docker compose -f {q_remote} up -d"],
        ]
        outputs = []
        for cmd in steps:
            rc, out = _run(cmd, timeout=600)
            outputs.append({"cmd": cmd[0], "rc": rc, "tail": out[-400:]})
            if rc != 0:
                return {
                    "error": f"remote step failed: {cmd[0]} (rc={rc})",
                    "steps": outputs,
                    "fix": "check SSH key auth (BatchMode), docker on the node, "
                           f"and reachability of {node_ip}",
                }
        return {
            "applied": True,
            "recipe_id": recipe_id,
            "mode": "remote-ssh",
            "node_ip": node_ip,
            "ssh_user": ssh_user,
            "steps": outputs,
            "next": f"node_verify(base_url='http://{node_ip}:{plan.get('port', 8000)}')",
        }
    except Exception as e:
        logger.exception("Remote deployment failed")
        return {
            "error": f"remote deployment failed: {e}",
            "fix": "check SSH key, node IP, and network connectivity",
        }


# ── 5. ENROLLMENT ──────────────────────────────────────────────────────


def node_enroll(
    control_plane_url: str = "",
    token: str = "",
) -> dict:
    """Enroll this node with the control plane.

    Fail-closed: missing token or URL => error dict, never anonymous.
    Wraps adk.enrollment (build_registration + rich_enroll).
    Redacts token in output.

    Authorization model: node_id is derived from this host (hostname), but the
    BINDING of that node_id to a tenant/workspace is enforced SERVER-SIDE by
    AitherIdentity from the bearer token's identity — a token cannot claim a
    node into someone else's tenant, and re-registering an existing node_id
    under a different tenant is rejected upstream. This client never decides
    permission; it only presents identity.
    """
    if not token:
        return {
            "error": "authentication token required",
            "fix": "provide token from 'adk login' or AITHER_AUTH_TOKEN env",
        }
    if not control_plane_url:
        control_plane_url = os.environ.get(
            "AITHER_CONTROL_PLANE_URL",
            "",
        )
    if not control_plane_url:
        return {
            "error": "control plane URL required",
            "fix": "provide control_plane_url or set AITHER_CONTROL_PLANE_URL",
        }

    try:
        import uuid

        from adk.enrollment import build_registration, rich_enroll

        node_id = f"node-{uuid.uuid4().hex[:8]}"

        # Run async enrollment synchronously
        result = asyncio.run(
            rich_enroll(control_plane_url, token, node_id, enable_heartbeat=True)
        )

        # Redact token in output
        if "bearer_token" in result:
            result["bearer_token"] = _mask_token(result["bearer_token"])
        if token:
            token_masked = _mask_token(token)
        else:
            token_masked = "(empty)"

        # Add enrollment summary
        enrolled = result.get("enrolled", False)
        return {
            "enrolled": enrolled,
            "node_id": result.get("node_id", ""),
            "tenant_id": result.get("tenant_id", ""),
            "workspace_id": result.get("workspace_id", ""),
            "auth_token": token_masked,
            "error": result.get("error") if not enrolled else None,
            "registration": result.get("registration", {}),
            "_enrolled_at": result.get("_enrolled_at", ""),
        }
    except Exception as e:
        logger.exception("Enrollment failed")
        return {
            "error": f"enrollment failed: {e}",
            "fix": "check control plane URL, token, and network connectivity",
        }


def _mask_token(token: str, show_chars: int = 4) -> str:
    """Mask a token, showing only the first N characters."""
    if len(token) <= show_chars:
        return "(redacted)"
    return token[:show_chars] + "..." + token[-show_chars:]


# ── 6. BACKEND REGISTRATION ────────────────────────────────────────────


def node_register_backend(
    genesis_url: str = "",
    base_url: str = "",
    backend_type: str = "",
    models: Optional[list] = None,
    preferred: bool = False,
    token: str = "",
) -> dict:
    """Register a backend with Genesis.

    POST {genesis_url}/deploy/cloud-model/register-backend, authenticated with a
    Bearer token (arg or AITHER_AUTH_TOKEN env). Fail-closed on missing URL or
    token — an unauthenticated register would either 401 silently or, worse,
    let any caller point fleet routing at an arbitrary URL. AUTHORIZATION is
    enforced server-side by Genesis against the token's identity; this client
    never decides permission, it just always presents identity.
    """
    if not token:
        token = os.environ.get("AITHER_AUTH_TOKEN", "")
    if not token:
        return {
            "error": "auth token required to register a backend",
            "fix": "pass token= or set AITHER_AUTH_TOKEN (a tenant-scoped "
                   "bearer from `adk login` / the control plane)",
        }
    if not genesis_url:
        genesis_url = os.environ.get("AITHER_GENESIS_URL", "")
    if not genesis_url:
        return {
            "error": "genesis URL required",
            "fix": "provide genesis_url or set AITHER_GENESIS_URL",
        }
    if not base_url:
        return {
            "error": "base_url required (backend endpoint)",
            "fix": "provide the backend service URL (e.g., http://localhost:8000)",
        }
    if not backend_type:
        return {
            "error": "backend_type required",
            "fix": "specify backend_type (vllm, ollama, etc.)",
        }

    try:
        models = models or []
        payload = {
            "base_url": base_url,
            "backend_type": backend_type,
            "models": models,
            "preferred": bool(preferred),
        }

        genesis_base = genesis_url.rstrip("/")
        url = f"{genesis_base}/deploy/cloud-model/register-backend"

        # Always present identity; Genesis authorizes server-side (Pattern 4:
        # unauthenticated internal calls 401 and look like "worked but empty").
        r = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )

        if r.status_code == 200:
            try:
                body = r.json()
                return {
                    "registered": True,
                    "backend_type": backend_type,
                    "base_url": base_url,
                    "models": models,
                    "response": body,
                }
            except ValueError:
                return {
                    "registered": True,
                    "backend_type": backend_type,
                    "base_url": base_url,
                    "status": r.status_code,
                }
        else:
            return {
                "error": f"genesis API HTTP {r.status_code}",
                "detail": r.text[:200],
                "fix": "check genesis URL and backend connectivity",
            }
    except httpx.HTTPError as e:
        logger.exception("Backend registration failed")
        return {
            "error": f"backend registration failed: {e}",
            "fix": "check genesis URL and network connectivity",
        }
    except Exception as e:
        logger.exception("Backend registration failed")
        return {
            "error": f"backend registration failed: {e}",
            "fix": "check configuration and system state",
        }


# ── 7. BACKEND VERIFICATION ────────────────────────────────────────────


def node_verify(
    base_url: str,
    backend_type: str = "vllm",
    model: str = "",
    timeout_s: float = 60.0,
) -> dict:
    """Verify backend health and inference capability.

    GET health path + POST one completion, return {status, health,
    completion_text, latency_ms}. Coherence = non-empty text.
    """
    if not base_url:
        return {
            "error": "base_url required",
            "fix": "provide the backend service URL",
        }

    backend_type = backend_type or "vllm"

    try:
        base = base_url.rstrip("/")
        timeout = float(timeout_s)

        # Health check
        health_path = _health_path_for(backend_type)
        health_url = f"{base}{health_path}"

        try:
            r = httpx.get(health_url, timeout=timeout)
            health_ok = r.status_code == 200
        except httpx.HTTPError:
            health_ok = False

        # Completion test
        completion_text = ""
        latency_ms = 0.0

        if health_ok:
            import time

            start = time.time()
            try:
                completion = _test_completion(
                    base, backend_type, model or "default", timeout
                )
                latency_ms = (time.time() - start) * 1000
                completion_text = completion.get("text", "")
            except Exception as e:
                logger.debug("Completion test failed: %s", e)

        coherent = bool(completion_text and len(completion_text.strip()) > 5)

        return {
            "status": "healthy" if health_ok and coherent else "degraded",
            "health": health_ok,
            "backend_type": backend_type,
            "completion_text": completion_text[:100] if completion_text else "",
            "latency_ms": round(latency_ms, 1),
            "coherent": coherent,
        }
    except Exception as e:
        logger.exception("Backend verification failed")
        return {
            "error": f"verification failed: {e}",
            "fix": "check backend URL and service health",
        }


def _health_path_for(backend_type: str) -> str:
    """Get the health check path for a backend."""
    paths = {
        "vllm": "/health",
        "ollama": "/api/tags",
        "llamacpp": "/health",
    }
    return paths.get(backend_type, "/health")


def _test_completion(
    base_url: str,
    backend_type: str,
    model: str,
    timeout: float,
) -> dict:
    """Test inference with a simple completion."""
    prompt = "What is machine learning? Answer in 1-2 sentences."

    if backend_type == "vllm":
        url = f"{base_url}/v1/completions"
        payload = {
            "model": model,
            "prompt": prompt,
            "max_tokens": 16,
            "temperature": 0.1,
        }
        r = httpx.post(url, json=payload, timeout=timeout)
        if r.status_code == 200:
            body = r.json()
            choices = body.get("choices") or []
            if choices:
                return {"text": choices[0].get("text", "").strip()}
        return {}

    elif backend_type == "ollama":
        url = f"{base_url}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        r = httpx.post(url, json=payload, timeout=timeout)
        if r.status_code == 200:
            body = r.json()
            return {"text": body.get("response", "").strip()}
        return {}

    return {}
