"""Image bootstrap pack — imagegen_* agent tools.

Design rules (same doctrine as node_bootstrap and the other tool packs):
  * Fail soft with actionable guidance — every tool returns a dict, never raises.
  * Missing credentials/URLs → {"error": ..., "fix": ...}, never anonymous.
  * Pure tools (imagegen_detect_hardware, imagegen_resolve_recipe,
    imagegen_plan_deployment) have no side effects.
  * imagegen_apply is idempotent; dry_run=True shows commands without executing.
  * imagegen_verify makes a POSITIVE assertion — health alone is not proof. A
    ComfyUI with zero checkpoints answers 200 and generates nothing, so verify
    asserts models are actually loaded and reports 'degraded' when they are not.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

import httpx

from adk.toolpacks.image_bootstrap.recipes import (
    RECIPE_IDS,
    engine_family,
    get_recipe,
    list_recipes,
    resolve_recipe,
    vram_band,
)

logger = logging.getLogger("image_bootstrap_pack")

_BOOTSTRAP_DIR = Path.home() / ".aither" / "image-bootstrap"
_TIMEOUT_DEFAULT = 60.0


# ── 1. DETECTION ─────────────────────────────────────────────────────────


def _system_dict_from_probe() -> dict:
    """Normalise adk.hardware_probe output into the resolver's system dict."""
    from adk.hardware_probe import detect_system

    sysinfo = detect_system()
    return {
        "ram_gb": sysinfo.ram_gb,
        "cpu_cores": sysinfo.cpu_cores,
        "gpu_vendor": sysinfo.gpu_vendor,
        "gpu_name": sysinfo.gpu_name,
        "gpu_vram_mb": sysinfo.gpu_vram_mb,
        "unified_memory": sysinfo.gpu_vendor == "apple",
    }


def imagegen_detect_hardware(verbose: bool = False) -> dict:
    """Detect hardware and report the image-generation capability band.

    Pure local operation — no network, no filesystem writes.
    Returns {system_info, vram_band, recommended_recipe, capability}.
    """
    try:
        system_dict = _system_dict_from_probe()
        gpu_vram_gb = system_dict["gpu_vram_mb"] / 1024
        band = (
            "unified"
            if system_dict["unified_memory"]
            else vram_band(gpu_vram_gb)
        )

        resolved = resolve_recipe(system_dict, prefer_engine="auto")
        recommended = ""
        if isinstance(resolved, dict) and resolved.get("recipe"):
            recommended = resolved["recipe"].get("id", "")

        capability = {
            "sdxl_1024": band in ("medium", "large", "unified"),
            "sdxl_lowvram": band == "small",
            "video_wan22": band == "large",
            "cpu_only": band == "none" and not system_dict["unified_memory"],
        }

        result = {
            "system_info": system_dict,
            "vram_band": band,
            "recommended_recipe": recommended,
            "capability": capability,
        }
        if verbose:
            result["rationale"] = resolved.get("rationale", "")
            result["rejected"] = resolved.get("rejected", {})
        return result
    except Exception as e:  # noqa: BLE001 — fail soft with guidance
        logger.exception("Image-gen hardware detection failed")
        return {
            "error": f"hardware detection failed: {e}",
            "fix": "check system permissions and GPU driver accessibility",
        }


# ── 2. RECIPE RESOLUTION ────────────────────────────────────────────────


def imagegen_resolve_recipe(
    prefer_engine: str = "auto",
    recipe_id: str = "",
) -> dict:
    """Resolve the best image-gen recipe for this system.

    Pure operation — runs detection internally when needed.
    prefer_engine is a tiebreaker ("comfyui" | "sana"), not a hard gate.
    Returns {recipe, match_score, rationale, warnings}.
    """
    try:
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
                "warnings": recipe.get("imagegen_config", {}).get("platform_traps", []),
            }

        return resolve_recipe(
            _system_dict_from_probe(),
            prefer_engine=prefer_engine,
            recipe_id="",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Image-gen recipe resolution failed")
        return {
            "error": f"recipe resolution failed: {e}",
            "fix": "check hardware detection and recipe files",
        }


# ── 3. DEPLOYMENT PLANNING ──────────────────────────────────────────────


# Hostnames that only resolve INSIDE the fleet's docker network. A self-hosted
# node cannot reach these, and the failure is silent: the entrypoint's curl just
# fails and ComfyUI starts with zero models (which imagegen_verify then reports
# as 'degraded'). Warn at PLAN time so nobody waits for a download that cannot happen.
_INTERNAL_HOSTS = ("aitheros-", "aither-", "://minio", "://localhost", "://127.0.0.1")


def _downloads_reachability_note(downloads: list) -> str:
    """Warn when resolved model URLs point at fleet-internal-only hostnames."""
    internal = 0
    for entry in downloads:
        url = str(entry.get("url", ""))
        if any(h in url for h in _INTERNAL_HOSTS):
            internal += 1
    if not internal:
        return ""
    return (
        f"{internal}/{len(downloads)} model URLs resolve to FLEET-INTERNAL hosts "
        "(e.g. aitheros-minio:9000) which a self-hosted node outside the fleet docker "
        "network cannot reach. Those downloads will fail SILENTLY and the container "
        "will start with fewer models than expected — imagegen_verify will report "
        "'degraded'. Most entries carry a public hf/civitai fallback in their "
        "'sources' list; on an off-fleet box, plan with tenant='' from a host that "
        "can reach the fleet, or set AITHER_MODEL_DOWNLOADS to the public sources."
    )


#: How long the fleet resolver gets before the public one answers instead. Chosen
#: from measurement, not taste: a healthy fleet resolve is well under a second, and
#: the pathological case observed was >110s. Anything in between is still a bad
#: experience for a command whose whole point is being easy.
_FLEET_RESOLVE_TIMEOUT_S = 8.0


def _resolve_model_downloads(profile: str, tenant: str = "") -> tuple:
    """Best-effort resolve a ComfyUI model profile to AITHER_MODEL_DOWNLOADS.

    The fleet resolver (AitherOS lib.compute.comfyui_models) turns profile names
    into the [{url, dest}] array the ComfyUI entrypoint consumes — including
    presigned MinIO/Strata URLs. It is NOT shipped in the public wheel, so this
    degrades to an empty array plus an explicit note rather than pretending the
    models will appear.

    NOTE: the resolved URLs are PRESIGNED and are therefore short-lived
    credentials. They are never written into the compose file — imagegen_apply
    puts them in a 0600 env file. See _write_env_file.

    Returns (downloads_json, note).
    """
    if not profile:
        return "", ""
    # THE FLEET RESOLVER IS BOUNDED, because it can HANG. It presigns MinIO URLs,
    # which reads credentials from the AitherSecrets vault and talks to the object
    # store -- measured 2026-08-24 on the fleet box, `to_downloads('studio')` did not
    # return within 110s, and that is what made `imagegen_plan_deployment` take over
    # two minutes. An unbounded call here is worse than no call: `setup` appears to
    # hang, and a setup command that appears to hang is one nobody runs twice.
    #
    # A thread rather than signal.alarm: this runs on Windows too, where alarm does
    # not exist, and inside a server thread where it would not be the main thread.
    # The worker is left to finish on its own if it is merely slow -- it writes to a
    # list nobody reads after the timeout, so a late arrival cannot corrupt the
    # answer we already returned.
    fleet_result: list = []

    def _fleet() -> None:
        try:
            from lib.compute.comfyui_models import to_downloads  # type: ignore
            fleet_result.append(
                to_downloads(profile) if not tenant else to_downloads(profile, tenant))
        except Exception as exc:  # noqa: BLE001 — absent off-fleet is EXPECTED
            logger.debug("comfyui_models resolver unavailable (%s)", exc)

    t = threading.Thread(target=_fleet, daemon=True, name="imagegen-fleet-resolve")
    t.start()
    t.join(timeout=_FLEET_RESOLVE_TIMEOUT_S)
    if fleet_result and fleet_result[0]:
        downloads = fleet_result[0]
        return json.dumps(downloads), _downloads_reachability_note(downloads or [])
    if t.is_alive():
        logger.info("fleet model resolver still running after %ss; using public sources",
                    _FLEET_RESOLVE_TIMEOUT_S)

    # OFF-FLEET IS THE NORMAL CASE, NOT AN ERROR. `lib.compute.comfyui_models` is a
    # monorepo module that awdk does not ship, so for every stranger who
    # `pip install awdk`s this branch is the ONLY branch -- and it used to return an
    # empty list plus a note saying the container "will start with NO models". The
    # one command that exists to make image generation self-service therefore
    # installed ComfyUI with nothing in it, honestly reported and completely useless.
    # That is gate 1i's UNSHIPPED IMPORT class: not a disclosure, a BROKEN tool that
    # reads as authoritative.
    #
    # `public_models` ships WITH the wheel and resolves the same profile to public
    # HuggingFace / CivitAI URLs. Measured 2026-08-24 on `studio`: 19 of 20 resolve,
    # and the one that cannot (detail_tweaker_xl, source `strata` with no
    # `public_ref`) is NAMED in the note rather than dropped -- a silently short list
    # is exactly how "installed ComfyUI" becomes "installed ComfyUI that generates
    # nothing".
    try:
        from .public_models import ProfileUnavailableError
        from .public_models import to_downloads as _public
        downloads, unavailable = _public(profile)
        if not downloads:
            return "", (f"model profile {profile!r} resolved to NO public downloads — "
                        "refusing to report an empty list as success")
        note = (f"resolved {len(downloads)} model(s) from PUBLIC sources "
                "(HuggingFace/CivitAI); the fleet resolver was not importable, which "
                "is normal off-fleet.")
        if unavailable:
            note += (" NOT included, no public source: "
                     + ", ".join(u["model"] for u in unavailable)
                     + " — ComfyUI will start without these and imagegen_verify will "
                       "still pass on the rest.")
        return json.dumps(downloads), note
    except ProfileUnavailableError as pe:
        return "", (f"model profile {profile!r} could not be read: {pe}. The container "
                    "would start with NO models, so this is reported rather than "
                    "letting an empty list look like success.")
    except Exception as pe:  # noqa: BLE001
        logger.debug("public model resolver failed (%s)", pe)
        return "", (
            f"model profile {profile!r} was NOT resolved to download URLs — neither the "
            f"fleet resolver nor the bundled public one worked ({type(pe).__name__}). "
            "The container will start with NO models. Set AITHER_MODEL_DOWNLOADS "
            "yourself, then re-check imagegen_verify."
        )


def _env_file_name(recipe_id: str) -> str:
    """Name of the 0600 env file that carries presigned model URLs."""
    return f"{recipe_id}.env"


def _write_env_file(path: Path, secret_env: dict) -> None:
    """Write the secret env file with owner-only permissions.

    The presigned model URLs are short-lived credentials — chmod BEFORE writing
    so the content is never briefly world-readable. On Windows chmod is a no-op;
    the file still lands under the user profile directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    try:
        path.chmod(0o600)
    except OSError as e:  # noqa: PERF203 — Windows/network FS may refuse
        logger.debug("could not chmod %s: %s", path, e)
    # docker compose env_file: KEY=VALUE, no quoting, one per line.
    lines = [f"{k}={v}" for k, v in secret_env.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_compose(
    recipe_id: str,
    recipe: dict,
    env: dict,
    use_env_file: bool = False,
    host_port: int = 0,
    network: str = "",
) -> str:
    """Render a self-contained docker-compose YAML from recipe fields.

    Public-safe: image/args/ports all come from the recipe. Engines:
      comfyui -> command = serve_args, models pulled by the entrypoint from
                 AITHER_MODEL_DOWNLOADS
      sana    -> env-var driven (serve_args are KEY=VALUE strings)

    Presigned model URLs are NEVER inlined here — when use_env_file is set the
    compose references a sibling 0600 env file instead.
    """
    cfg = recipe.get("imagegen_config", {})
    deployment = recipe.get("deployment", {})
    engine = engine_family(cfg.get("engine", ""))
    image = cfg.get("image", "")
    port = deployment.get("port", 8188)

    env_lines = [f"      {k}: {json.dumps(str(v))}" for k, v in env.items() if v != ""]

    # BOTH engines are ENTRYPOINT-DRIVEN and configured purely by env. We must
    # NEVER emit a `command:` override here: the comfyui-cloud entrypoint is what
    # syncs AITHER_MODEL_DOWNLOADS before launching ComfyUI, so replacing the
    # command would silently skip the model download and bring up an EMPTY
    # backend. Recipe serve_args are therefore KEY=VALUE env strings
    # (COMFY_PORT / COMFY_EXTRA_ARGS for comfyui, SANA_* for sana), not CLI flags.
    # (metal-comfyui is the exception and is NATIVE — it never reaches this
    # function; its serve_args stay CLI flags for `python main.py`.)
    for arg in cfg.get("serve_args", []) or []:
        if isinstance(arg, str) and "=" in arg:
            k, v = arg.split("=", 1)
            env_lines.append(f"      {k}: {json.dumps(v)}")

    if engine == "sana":
        volumes = (
            "    volumes:\n"
            "      - image-bootstrap-hf-cache:/home/aither/.cache/huggingface\n"
        )
    else:  # comfyui — VOLUME ["/comfyui/models", "/comfyui/output"] in the image
        volumes = (
            "    volumes:\n"
            "      - image-bootstrap-models:/comfyui/models\n"
            "      - image-bootstrap-output:/comfyui/output\n"
        )

    gpu_block = ""
    if recipe.get("hardware_requirements", {}).get("gpu_vendor") == "nvidia":
        gpu_block = (
            "    deploy:\n"
            "      resources:\n"
            "        reservations:\n"
            "          devices:\n"
            "            - driver: nvidia\n"
            "              count: 1\n"
            "              capabilities: [gpu]\n"
        )

    parts = [
        f"# Generated by adk image_bootstrap from recipe {recipe_id} — do not edit by hand.",
        "services:",
        f"  {recipe_id}:",
        f"    image: {image}",
        f'    ports:\n      - "{host_port or port}:{port}"',
        "    restart: unless-stopped",
    ]
    if use_env_file:
        parts.append(f"    env_file:\n      - {_env_file_name(recipe_id)}")
    if env_lines:
        parts.append("    environment:\n" + "\n".join(env_lines))
    parts.append(volumes.rstrip("\n"))
    if gpu_block:
        parts.append(gpu_block.rstrip("\n"))
    if network:
        # Joining an EXISTING network matters more than it looks: a fresh isolated
        # compose bridge can have broken external DNS (observed live 2026-07-24 —
        # only AAAA records resolved, so every model download failed while the
        # entrypoint logged "sync complete" and started an EMPTY ComfyUI). Putting
        # the container on the network the rest of the fleet uses fixes resolution.
        parts.append(f"    networks:\n      - {network}")

    parts.append(
        "volumes:\n"
        "  image-bootstrap-models:\n"
        "  image-bootstrap-output:\n"
        "  image-bootstrap-hf-cache:"
    )
    if network:
        parts.append(f"networks:\n  {network}:\n    external: true")
    return "\n".join(parts) + "\n"


def imagegen_plan_deployment(
    recipe_id: str,
    tenant: str = "",
    host_port: int = 0,
    network: str = "",
    _include_secrets: bool = False,
    target: str = "",
    free_vram_gb: float = 0,
) -> dict:
    """Render an image-gen deployment plan (pure, no side effects).

    Returns {steps, compose_yaml | native_commands | delegate, env,
             secret_env_keys, port, download_size_mb, est_duration_min, notes}.

    Resolved model URLs are PRESIGNED (credential-bearing). The plan therefore
    reports only their KEY NAMES — returning the values would put live signed
    URLs into agent transcripts and logs. `_include_secrets` is the internal
    escape hatch imagegen_apply uses to get the values it must write to the
    0600 env file; it is not part of the agent-facing contract.
    """
    if not recipe_id:
        return {"error": "recipe_id is required", "available": list_recipes()}
    if recipe_id not in RECIPE_IDS:
        return {"error": f"unknown recipe: {recipe_id}", "available": list_recipes()}

    try:
        recipe = get_recipe(recipe_id)
        if not recipe:
            return {"error": f"failed to load recipe: {recipe_id}"}

        cfg = recipe.get("imagegen_config", {})
        deployment = recipe.get("deployment", {})
        backend_cfg = recipe.get("backend_config", {})
        # An explicit target overrides the recipe's default: the fleet
        # plans podman-quadlet where a customer's box plans docker-compose,
        # from the SAME recipe.
        target = target or deployment.get("target", "docker-compose")
        delegate = deployment.get("delegate", "")
        port = deployment.get("port", 8188)
        profile = cfg.get("model_profile", "")

        models = cfg.get("models", []) or []
        dl_size = sum(m.get("size_gb", 0) for m in models)

        # REFUSAL ARM: never place a recipe on a box whose FREE VRAM is
        # smaller than the recipe's need. The resolver sees TOTAL VRAM
        # only, so a 32GB card with 3.5GB free resolves to the 24GB band
        # and an eager placement is exactly what crash-looped the fleet
        # orchestrator 17+ times (measured 2026-08-25). Opt-in: 0 means
        # 'not judged', so nothing already working changes behaviour.
        min_vram = recipe.get("hardware_requirements", {}).get("min_vram_gb", 0) or 0
        if free_vram_gb > 0 and min_vram > free_vram_gb:
            return {
                "error": (
                    f"refused: {recipe_id} needs {min_vram}GB VRAM, "
                    f"this box has {free_vram_gb:.1f}GB free"
                ),
                "refusal": {
                    "recipe_id": recipe_id,
                    "needs_vram_gb": min_vram,
                    "free_vram_gb": free_vram_gb,
                },
                "alternative": (
                    "mesh-spark (place on the DGX Spark), webgpu-browser "
                    "(the visitor's own GPU), or cloud-burst-vast (rented)"
                ),
            }

        notes: list[str] = []
        downloads_json, note = _resolve_model_downloads(profile, tenant)
        if note:
            notes.append(note)
        if downloads_json and not network:
            notes.append(
                "No `network` given: the container will land on a FRESH compose bridge. "
                "On hosts where that bridge has broken external DNS (seen live: only "
                "AAAA records resolve), EVERY model download fails while the entrypoint "
                "still logs 'model cache sync complete' and starts an EMPTY ComfyUI. "
                "Pass network='aither-network' (or whatever the fleet uses) to join a "
                "network with working resolution, and always confirm with imagegen_verify."
            )

        env = {
            "AITHER_IMAGE_RECIPE_ID": recipe_id,
            "AITHER_DEPLOYMENT_TARGET": target,
            "AITHER_IMAGE_PORT": str(port),
            "AITHER_BACKEND_TYPE": backend_cfg.get("backend_type", ""),
        }
        if engine_family(cfg.get("engine", "")) == "comfyui":
            env["COMFY_MODELS_VOLUME"] = "/comfyui/models"

        # AITHER_MODEL_DOWNLOADS carries PRESIGNED (credential-bearing) URLs, so it
        # is kept OUT of the compose file and written to a 0600 env file by apply.
        secret_env = {}
        if downloads_json:
            secret_env["AITHER_MODEL_DOWNLOADS"] = downloads_json

        steps = [f"Resolve recipe: {recipe_id}"]
        if target == "docker-compose":
            steps += [
                f"Write compose file to {_BOOTSTRAP_DIR}",
                f"Pull image: {cfg.get('image', '')}",
                f"Download model profile: {profile or '(none)'} (~{dl_size:.1f}GB)",
                "Start container via docker compose",
                f"Verify models loaded on :{port} (imagegen_verify)",
            ]
        elif target == "native":
            steps += [
                "Install ComfyUI natively (Docker on macOS has no Metal passthrough)",
                f"Download model profile: {profile or '(none)'} (~{dl_size:.1f}GB)",
                f"Start ComfyUI on :{port}",
                f"Verify models loaded on :{port} (imagegen_verify)",
            ]
        elif target == "delegate":
            steps += [f"Delegate to: {delegate}", "Verify models loaded on the burst instance"]
        elif target == "browser":
            steps += [
                "Open the image panel in the visitor's browser",
                "The WebGPU model downloads once and runs on THEIR GPU",
                "Nothing to install or verify on this box",
            ]
        elif target == "podman-quadlet":
            steps += [
                f"Write {recipe_id}.container to the quadlet dir",
                "systemctl daemon-reload",
                f"Start the {recipe_id} unit",
                f"Verify models loaded on :{port} (imagegen_verify)",
            ]

        result = {
            "recipe_id": recipe_id,
            "engine": cfg.get("engine", ""),
            "model_profile": profile,
            "steps": steps,
            "env": env,
            # Redacted in the returned plan — the real values go to the 0600 env
            # file at apply time. Returning presigned URLs in a tool result would
            # put them into agent transcripts and logs.
            "secret_env_keys": sorted(secret_env),
            "port": port,
            "download_size_mb": int(dl_size * 1024),
            "est_duration_min": 25 if dl_size > 20 else 12,
            "notes": notes,
        }

        if target == "docker-compose":
            if not cfg.get("image"):
                return {
                    "error": f"recipe {recipe_id} has no image but targets docker-compose",
                    "fix": "set imagegen_config.image in the recipe",
                }
            result["compose_yaml"] = _render_compose(
                recipe_id, recipe, env,
                use_env_file=bool(secret_env), host_port=host_port,
                network=network,
            )
            result["host_port"] = host_port or port
            result["compose_template"] = deployment.get("compose_template", "")
            result["mode"] = "docker-compose"
            if secret_env:
                result["env_file"] = _env_file_name(recipe_id)
                if _include_secrets:
                    result["_secret_env"] = secret_env
        elif target == "native":
            # Native for two distinct reasons: Apple Silicon (Docker has no Metal
            # passthrough) and CPU-only (the published cloud image is CUDA-only and
            # crashes with "0 active drivers" on a GPU-less host). The setup differs.
            is_apple = recipe.get("hardware_requirements", {}).get("gpu_vendor") == "apple"
            why = (
                "# Apple Silicon: Docker has NO Metal passthrough — install natively."
                if is_apple else
                "# CPU-only: the published comfyui-cloud image is CUDA-only (crashes "
                "'0 active drivers' with no GPU) — install ComfyUI natively."
            )
            native = [
                why,
                "git clone https://github.com/comfyanonymous/ComfyUI ~/ComfyUI",
                "cd ~/ComfyUI && python -m venv .venv && . .venv/bin/activate",
                "pip install -r requirements.txt",
            ]
            if is_apple:
                native.append("export PYTORCH_ENABLE_MPS_FALLBACK=1  # some ops lack MPS kernels")
            else:
                native.append(
                    "pip install torch --index-url https://download.pytorch.org/whl/cpu  "
                    "# CPU torch build"
                )
            native.append(
                "# then drop checkpoints into ~/ComfyUI/models/checkpoints/ "
                "(no AITHER_MODEL_DOWNLOADS on native)"
            )
            native.append(f"python main.py {' '.join(cfg.get('serve_args', []) or [])}")
            result["native_commands"] = native
            result["mode"] = "native"
        elif target == "delegate":
            result["delegate"] = delegate
            result["mode"] = "delegate"
        elif target == "browser":
            result["mode"] = "browser"
            result["browser_url"] = deployment.get("browser_url", "https://aitherium.com")
        elif target == "podman-quadlet":
            if not cfg.get("image"):
                return {
                    "error": f"recipe {recipe_id} has no image but targets podman-quadlet",
                    "fix": "set imagegen_config.image in the recipe",
                }
            result["quadlet_unit"] = _render_quadlet(recipe_id, recipe, env)
            result["quadlet_dir"] = str(
                Path.home() / ".config" / "containers" / "systemd")
            result["mode"] = "podman-quadlet"
            if secret_env:
                result["env_file"] = _env_file_name(recipe_id)
                if _include_secrets:
                    result["_secret_env"] = secret_env

        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("Image-gen deployment planning failed")
        return {
            "error": f"deployment planning failed: {e}",
            "fix": "check recipe structure and system permissions",
        }


def _render_quadlet(recipe_id: str, recipe: dict, env: dict) -> str:
    """Render a podman quadlet .container unit from the recipe.

    The docker-compose target is correct on a customer's box; the FLEET
    runs rootful podman quadlets in WSL2 where `docker compose` cannot
    run at all -- the old pack failed naming docker. A .container unit
    is the same deployment expressed in the engine the fleet runs.
    Env values are rendered INLINE (no EnvironmentFile): the presigned
    model URLs are written to a 0600 env file by apply, exactly like
    the compose path, so the unit itself carries no credentials.
    """
    cfg = recipe.get("imagegen_config", {})
    image = cfg.get("image", "")
    port = recipe.get("deployment", {}).get("port", 8188)
    lines = [
        "[Unit]",
        f"Description=Image bootstrap: {recipe_id}",
        "After=network-online.target",
        "",
        "[Container]",
        f"Image={image}",
        f"PublishPort={port}:{port}",
    ]
    for key in sorted(env):
        lines.append(f"Environment={key}={env[key]}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


# ── 4. DEPLOYMENT APPLICATION ──────────────────────────────────────────


def _run(cmd: list, timeout: int = 1800) -> tuple:
    """Run a command; return (rc, tail_of_output). Never raises."""
    try:
        # encoding= is NOT optional (PQ009). Without it the child's output is decoded
        # with the LOCALE codec -- cp1252 on Windows -- and a single non-ASCII byte
        # from docker raises UnicodeDecodeError. That is a ValueError, which the
        # OSError/SubprocessError guard below does NOT catch, so the tool crashes
        # instead of reporting a failed deploy. This sits on the one-command `setup`
        # path, i.e. the first thing a stranger runs.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
            encoding="utf-8", errors="replace",
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc.returncode, out[-2000:]
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd[:3])}"


def imagegen_apply(
    recipe_id: str,
    tenant: str = "",
    host_port: int = 0,
    network: str = "",
    dry_run: bool = False,
    target: str = "",
    free_vram_gb: float = 0,
) -> dict:
    """Apply an image-gen deployment plan.

    docker-compose: writes to ~/.aither/image-bootstrap/ and runs `docker compose up -d`.
    native:         returns the documented manual steps (macOS has no Metal passthrough).
    delegate:       reports the exact fleet command instead of pretending to run it.
    dry_run=True:   shows commands without executing — subprocess is NOT called.
    """
    if not recipe_id:
        return {"error": "recipe_id is required", "available": list_recipes()}

    try:
        plan = imagegen_plan_deployment(
            recipe_id, tenant=tenant, host_port=host_port, network=network,
            _include_secrets=True, target=target, free_vram_gb=free_vram_gb,
        )
        if "error" in plan:
            return plan

        # Pop immediately so presigned URLs can never leak into a returned dict.
        secret_env = plan.pop("_secret_env", {})
        mode = plan.get("mode", "none")

        if dry_run:
            commands = []
            if mode == "docker-compose":
                compose_file = _BOOTSTRAP_DIR / f"{recipe_id}-compose.yml"
                if secret_env:
                    env_file = _BOOTSTRAP_DIR / _env_file_name(recipe_id)
                    commands.append(
                        f"# write env file (mode 0600, {len(secret_env)} keys): {env_file}"
                    )
                commands.append(f"# write compose file: {compose_file}")
                commands.append(f"docker compose -f {compose_file} up -d")
            elif mode == "native":
                commands.extend(plan.get("native_commands", []))
            elif mode == "delegate":
                commands.append(f"# run the fleet delegate: {plan.get('delegate', '')}")
            elif mode == "browser":
                commands.append(
                    "# nothing to install -- open "
                    f"{plan.get('browser_url', '')} in the visitor's browser")
            elif mode == "podman-quadlet":
                unit = plan.get('quadlet_unit', '')
                quadlet_dir = plan.get('quadlet_dir', '')
                commands.append(f"# write unit: {quadlet_dir}/{recipe_id}.container")
                commands.append("systemctl --user daemon-reload")
                commands.append(f"systemctl --user start {recipe_id}")
            return {
                "planned": True,
                "dry_run": True,
                "recipe_id": recipe_id,
                "mode": mode,
                "commands": commands,
                "env": plan.get("env", {}),
                "notes": plan.get("notes", []),
            }

        if mode == "docker-compose":
            compose_yaml = plan.get("compose_yaml", "")
            if not compose_yaml:
                return {
                    "error": "plan has no compose_yaml",
                    "fix": "re-run imagegen_plan_deployment",
                }
            _BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)
            compose_file = _BOOTSTRAP_DIR / f"{recipe_id}-compose.yml"
            # Env file FIRST: compose references it, and it must already have
            # 0600 perms before the model URLs land in it.
            if secret_env:
                _write_env_file(_BOOTSTRAP_DIR / _env_file_name(recipe_id), secret_env)
            compose_file.write_text(compose_yaml, encoding="utf-8")
            rc, out = _run(["docker", "compose", "-f", str(compose_file), "up", "-d"])
            if rc != 0:
                return {
                    "error": f"docker compose up failed (rc={rc})",
                    "output": out,
                    "compose_file": str(compose_file),
                    "fix": "check docker is running and the NVIDIA container runtime "
                           "is configured (nvidia-ctk runtime configure)",
                }
            port = plan.get("port", 8188)
            return {
                "applied": True,
                "recipe_id": recipe_id,
                "mode": "docker-compose",
                "compose_file": str(compose_file),
                "notes": plan.get("notes", []),
                "next": f"imagegen_verify(base_url='http://localhost:{port}')",
            }

        if mode == "native":
            # Installing a native toolchain is out of scope — report the steps.
            return {
                "planned": True,
                "recipe_id": recipe_id,
                "mode": "native",
                "commands": plan.get("native_commands", []),
                "fix": "run these on the Mac itself — Docker on macOS has no Metal "
                       "GPU passthrough, so a containerised ComfyUI silently runs on CPU",
            }

        if mode == "delegate":
            # Fleet tooling is not shipped in the public wheel — report, don't pretend.
            return {
                "planned": True,
                "recipe_id": recipe_id,
                "mode": "delegate",
                "delegate": plan.get("delegate", ""),
                "fix": f"run the fleet delegate: {plan.get('delegate', '')}",
            }

        if mode == "browser":
            return {
                "planned": True,
                "recipe_id": recipe_id,
                "mode": "browser",
                "browser_url": plan.get("browser_url", ""),
                "fix": "open the image panel in the visitor's browser — "
                        "the WebGPU model runs on THEIR GPU, nothing installs here",
            }

        if mode == "podman-quadlet":
            unit = plan.get("quadlet_unit", "")
            if not unit:
                return {
                    "error": "plan has no quadlet_unit",
                    "fix": "re-run imagegen_plan_deployment",
                }
            quadlet_dir = Path(plan.get("quadlet_dir") or
                               (Path.home() / ".config" / "containers" / "systemd"))
            quadlet_dir.mkdir(parents=True, exist_ok=True)
            unit_file = quadlet_dir / f"{recipe_id}.container"
            if secret_env:
                _write_env_file(_BOOTSTRAP_DIR / _env_file_name(recipe_id), secret_env)
                unit_file.write_text(unit.replace(
                    "[Container]",
                    f"[Container]\nEnvironmentFile={_BOOTSTRAP_DIR / _env_file_name(recipe_id)}",
                    1), encoding="utf-8")
            else:
                unit_file.write_text(unit, encoding="utf-8")
            rc, out = _run(["systemctl", "--user", "daemon-reload"])
            if rc == 0:
                rc, out = _run(["systemctl", "--user", "start", recipe_id])
            if rc != 0:
                return {
                    "error": f"quadlet start failed (rc={rc})",
                    "output": out,
                    "unit_file": str(unit_file),
                    "fix": "inspect the unit file and run systemctl --user "
                            "daemon-reload; fleet (root) placement uses "
                            "/etc/containers/systemd instead of the user dir",
                }
            return {
                "applied": True,
                "recipe_id": recipe_id,
                "mode": "podman-quadlet",
                "unit_file": str(unit_file),
                "notes": plan.get("notes", []),
                "next": f"imagegen_verify(base_url='http://localhost:{plan.get('port', 8188)}')",
            }

        return {"error": f"unsupported deployment mode: {mode}"}
    except Exception as e:  # noqa: BLE001
        logger.exception("Image-gen deployment apply failed")
        return {
            "error": f"deployment apply failed: {e}",
            "fix": "check recipe, permissions, and docker state",
        }


# ── 5. BACKEND REGISTRATION ────────────────────────────────────────────


def imagegen_register_backend(
    genesis_url: str = "",
    base_url: str = "",
    backend_type: str = "",
    models: Optional[list] = None,
    preferred: bool = False,
    token: str = "",
) -> dict:
    """Register an image-gen backend with Genesis.

    Fail-closed on a missing URL or token. An unauthenticated register would
    either 401 silently or, worse, let any caller point fleet image routing at an
    arbitrary URL. AUTHORIZATION is enforced server-side by Genesis against the
    token's identity; this client never decides permission, it just always
    presents identity. TLS verification is never disabled — trust the internal CA.
    """
    if not token:
        token = os.environ.get("AITHER_AUTH_TOKEN", "")
    if not token:
        return {
            "error": "auth token required to register a backend",
            "fix": "pass token= or set AITHER_AUTH_TOKEN (a tenant-scoped bearer "
                   "from `adk login` / the control plane)",
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
            "fix": "provide the backend service URL (e.g., http://localhost:8188)",
        }
    if not backend_type:
        return {
            "error": "backend_type required",
            "fix": "specify backend_type (comfyui, sana)",
        }

    try:
        payload = {
            "base_url": base_url,
            "backend_type": backend_type,
            "models": models or [],
            "preferred": bool(preferred),
            "category": "image",
        }
        url = f"{genesis_url.rstrip('/')}/deploy/cloud-model/register-backend"

        r = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        if r.status_code == 200:
            try:
                body = r.json()
            except ValueError:
                body = {"status": r.status_code}
            return {
                "registered": True,
                "backend_type": backend_type,
                "base_url": base_url,
                "models": models or [],
                "response": body,
            }
        return {
            "error": f"genesis API HTTP {r.status_code}",
            "detail": r.text[:200],
            "fix": "check genesis URL, the token's tenant scope, and connectivity",
        }
    except httpx.HTTPError as e:
        logger.exception("Image backend registration failed")
        return {
            "error": f"backend registration failed: {e}",
            "fix": "check genesis URL and network connectivity",
        }
    except Exception as e:  # noqa: BLE001
        logger.exception("Image backend registration failed")
        return {
            "error": f"backend registration failed: {e}",
            "fix": "check configuration and system state",
        }


# ── 6. VERIFICATION (the positive assertion) ───────────────────────────


def _extract_comfy_checkpoints(body: dict) -> list:
    """Pull the checkpoint list out of /object_info/CheckpointLoaderSimple.

    Shape: {"CheckpointLoaderSimple": {"input": {"required":
             {"ckpt_name": [[<names...>], {...}]}}}}
    Returns [] on any shape mismatch — an empty list means "no models", which is
    exactly the degraded state the caller must be told about.
    """
    try:
        node = body.get("CheckpointLoaderSimple") or {}
        required = node.get("input", {}).get("required", {})
        entry = required.get("ckpt_name") or []
        if entry and isinstance(entry[0], list):
            return [str(x) for x in entry[0]]
    except Exception as e:  # noqa: BLE001 — unknown shape => no models proven
        logger.debug("checkpoint extraction failed: %s", e)
    return []


def _extract_sana_models(body) -> list:
    """Pull model names out of SANA's /v1/backends response (shape-tolerant)."""
    try:
        items = body.get("backends", body) if isinstance(body, dict) else body
        if isinstance(items, dict):
            return [str(k) for k in items]
        if isinstance(items, list):
            out = []
            for it in items:
                if isinstance(it, str):
                    out.append(it)
                elif isinstance(it, dict):
                    name = it.get("model") or it.get("name") or it.get("id")
                    if name:
                        out.append(str(name))
            return out
    except Exception as e:  # noqa: BLE001
        logger.debug("sana model extraction failed: %s", e)
    return []


def imagegen_verify(
    base_url: str,
    backend_type: str = "comfyui",
    timeout_s: float = _TIMEOUT_DEFAULT,
) -> dict:
    """Verify an image-gen backend is up AND actually has models loaded.

    Health alone is NOT proof: ComfyUI answers 200 on /system_stats with zero
    checkpoints installed and generates nothing. This checks health, then probes
    the capability path and asserts at least one model is really there.

    Returns {status, health, models_loaded, model_count, sample_models}.
    status is 'healthy' only when both hold; 'degraded' when up but empty.
    """
    if not base_url:
        return {"error": "base_url required", "fix": "provide the backend service URL"}

    backend_type = backend_type or "comfyui"
    base = base_url.rstrip("/")
    timeout = float(timeout_s)

    if backend_type == "sana":
        health_path, capability_path = "/health", "/v1/backends"
    else:
        health_path, capability_path = "/system_stats", "/object_info/CheckpointLoaderSimple"

    try:
        health_ok = False
        health_detail = ""
        try:
            r = httpx.get(f"{base}{health_path}", timeout=timeout)
            health_ok = r.status_code == 200
            if not health_ok:
                health_detail = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            health_detail = str(e)

        # Three-way outcome, NOT two. "probe failed" must never be reported as
        # "zero models" — a transient hiccup against a fully-loaded backend would
        # otherwise tell an operator their model download failed and send them
        # debugging the wrong thing. (Observed live 2026-07-24 against a healthy
        # fleet ComfyUI holding 17 checkpoints.)
        models: list = []
        probe_ok = False
        probe_detail = ""
        if health_ok:
            try:
                r = httpx.get(f"{base}{capability_path}", timeout=timeout)
                if r.status_code == 200:
                    body = r.json()
                    models = (
                        _extract_sana_models(body)
                        if backend_type == "sana"
                        else _extract_comfy_checkpoints(body)
                    )
                    probe_ok = True
                else:
                    probe_detail = f"HTTP {r.status_code}"
            except (httpx.HTTPError, ValueError) as e:
                probe_detail = f"{type(e).__name__}: {e}"
                logger.debug("capability probe failed: %s", e)

        models_loaded = probe_ok and len(models) > 0
        if not health_ok:
            status = "degraded"
        elif not probe_ok:
            status = "unknown"          # up, but we could not determine models
        elif models_loaded:
            status = "healthy"
        else:
            status = "degraded"         # up, probe worked, genuinely empty

        result = {
            "status": status,
            "health": health_ok,
            "backend_type": backend_type,
            "capability_probe": probe_ok,
            "models_loaded": models_loaded,
            "model_count": len(models) if probe_ok else None,
            "sample_models": models[:5],
        }
        if not health_ok:
            result["detail"] = health_detail
            result["fix"] = (
                f"{backend_type} did not answer {health_path} — check the container is "
                "running and the port is published"
            )
        elif not probe_ok:
            result["detail"] = probe_detail
            result["fix"] = (
                f"backend is UP but the capability probe ({capability_path}) did not "
                "answer, so model state is UNKNOWN — this is NOT proof of zero models. "
                "Retry with a longer timeout_s before concluding the download failed."
            )
        elif not models_loaded:
            result["fix"] = (
                "backend is UP but has ZERO models — the profile download did not land. "
                "Check AITHER_MODEL_DOWNLOADS was set and the entrypoint fetched it; "
                "note only ghcr.io/aitherium/comfyui-cloud consumes that var (the "
                "standard comfyui image ignores it entirely)."
            )
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception("Image-gen verification failed")
        return {
            "error": f"verification failed: {e}",
            "fix": "check backend URL and service health",
        }


# ── one command ──────────────────────────────────────────────────────────────

#: What to hand a person once a backend is really serving. Kept as data next to the
#: code that earns it, because "it works now" with no next step is how a working
#: backend still ends in "how do I use this".
GOBBONET_MOD_URLS = (
    "https://aitherium.com/gobbonet/image-renderer.js",
    "https://aitherium.com/gobbonet/image-renderer.css",
)


def imagegen_setup(
    prefer_engine: str = "auto",
    recipe_id: str = "",
    tenant: str = "",
    host_port: int = 0,
    network: str = "",
    genesis_url: str = "",
    token: str = "",
    register: bool = True,
    dry_run: bool = False,
) -> dict:
    """Stand up image generation in ONE call: detect -> resolve -> plan -> apply ->
    verify -> register, and finish by naming how to use it.

    WHY THIS EXISTS. Every step below already existed and worked; what did not exist
    was a way to run them without being the person who wrote them. The CLI exposed
    `detect`, `resolve`, `plan` and `apply` as separate subcommands, each needing a
    `--recipe-id` you could only get by reading the previous one's output -- so
    "automated setup" still meant eight commands and a copy-paste. Measured against
    the actual ask: a community member vibe-coded their own ComfyUI mod rather than
    find this. That is the tooling's failure, not theirs.

    IT IS IDEMPOTENT, AND ALREADY-SERVING IS A SUCCESS. Re-running must be safe, or
    nobody re-runs it and every failure becomes a fresh manual repair. If the backend
    the recipe names is already healthy, this reports that and deploys nothing.

    IT RE-ASSERTS AFTER ACTING. `imagegen_verify` is called AFTER apply, and a
    `degraded` result -- ComfyUI answering 200 with zero checkpoints -- is a FAILURE
    here, not a pass. That distinction is the whole reason verify exists: health alone
    is not proof, and a backend that is up and empty generates nothing while looking
    perfect.

    REGISTRATION IS OPTIONAL AND NEVER FATAL. It needs a Genesis token that a
    self-hosting stranger does not have and does not need: their backend works
    locally either way. A missing token SKIPS the step with a reason rather than
    failing a setup that otherwise succeeded -- fail-closed on authorization, not on
    usefulness.

    Returns {ok, stage, steps, backend, mod_urls, ...}. `stage` names where it
    stopped, so a failure says which step rather than just "setup failed".
    """
    steps: list = []

    def _step(name: str, result: dict, fatal: bool = True) -> bool:
        entry = {"step": name, "ok": "error" not in result}
        if "error" in result:
            entry["error"] = result["error"]
            if result.get("fix"):
                entry["fix"] = result["fix"]
        steps.append(entry)
        return not (fatal and "error" in result)

    # 1. Which recipe. Resolution runs detection internally, but detect is reported
    #    separately because "what did it think my hardware was" is the first question
    #    anyone asks when the answer surprises them.
    hw = imagegen_detect_hardware()
    _step("detect_hardware", hw, fatal=False)

    resolved = imagegen_resolve_recipe(prefer_engine=prefer_engine, recipe_id=recipe_id)
    if not _step("resolve_recipe", resolved):
        return {"ok": False, "stage": "resolve_recipe", "steps": steps,
                "hardware": hw if "error" not in hw else None}

    recipe = resolved.get("recipe") or {}
    rid = recipe.get("id") or recipe_id
    engine = (recipe.get("engine") or "comfyui").lower()

    # 2. ALREADY SERVING? Ask FIRST, and take the port from the RECIPE rather than
    #    from a plan.
    #
    #    Ordering measured, not guessed. The first cut planned first (the plan is pure,
    #    so it looked like the tidy way to learn the port) and the already-serving path
    #    then cost **over two minutes** -- `imagegen_plan_deployment` resolves model
    #    downloads and probes their reachability over the network. That is the WRONG
    #    two minutes: it is the most common path, it is the one where nothing needs to
    #    happen, and a setup command that appears to hang is one nobody runs twice.
    #    The recipe already carries `deployment.port`, so the plan was never needed to
    #    learn it.
    port = host_port or (recipe.get("deployment") or {}).get("port") or 8188
    base_url = f"http://localhost:{port}"

    pre = imagegen_verify(base_url=base_url, backend_type=engine)
    if pre.get("status") == "healthy":
        steps.append({"step": "verify_existing", "ok": True,
                      "note": f"{engine} already serving at {base_url} with "
                              f"{pre.get('model_count', 0)} model(s) - deployed nothing"})
        return _finish(True, "already_serving", steps, base_url, engine, pre,
                       rid, register, genesis_url, token)

    # 3. Only NOW is the plan worth its cost -- we know we are going to deploy.
    plan = imagegen_plan_deployment(recipe_id=rid, tenant=tenant,
                                    host_port=host_port, network=network)
    if not _step("plan_deployment", plan):
        return {"ok": False, "stage": "plan_deployment", "steps": steps, "recipe": rid,
                "base_url": base_url}
    port = host_port or plan.get("port") or port
    base_url = f"http://localhost:{port}"

    if dry_run:
        # A plan is the deliverable here, not a half-run. Say so rather than
        # reporting success for work that did not happen.
        steps.append({"step": "apply", "ok": True, "note": "dry_run - nothing executed"})
        return {"ok": True, "stage": "dry_run", "steps": steps, "recipe": rid,
                "plan": plan, "base_url": base_url,
                "download_size_mb": plan.get("download_size_mb"),
                "est_duration_min": plan.get("est_duration_min")}

    # 4. Do it.
    applied = imagegen_apply(recipe_id=rid, tenant=tenant, host_port=host_port,
                             network=network, dry_run=False)
    if not _step("apply", applied):
        return {"ok": False, "stage": "apply", "steps": steps, "recipe": rid,
                "plan_notes": plan.get("notes")}

    # 5. Re-assert. A repair that does not re-assert has proven only that it ran.
    post = imagegen_verify(base_url=base_url, backend_type=engine)
    status = post.get("status")
    if status != "healthy":
        steps.append({
            "step": "verify", "ok": False,
            "error": f"backend is '{status}' after apply, not 'healthy'",
            "fix": ("'degraded' means it answered but has NO models loaded - it will "
                    "generate nothing while looking fine. Model downloads are large; "
                    "give it time and re-run this, which is safe."),
            "detail": {k: post.get(k) for k in ("health", "models_loaded", "model_count")},
        })
        return {"ok": False, "stage": "verify", "steps": steps, "recipe": rid,
                "base_url": base_url}
    steps.append({"step": "verify", "ok": True,
                  "note": f"healthy with {post.get('model_count', 0)} model(s)"})

    return _finish(True, "deployed", steps, base_url, engine, post,
                   rid, register, genesis_url, token)


def _finish(ok: bool, stage: str, steps: list, base_url: str, engine: str,
            verified: dict, recipe_id: str, register: bool,
            genesis_url: str, token: str) -> dict:
    """Register if we can, then hand back something the caller can ACT on."""
    if register:
        if not token:
            # Not an error. A self-hoster's backend works without Genesis; failing
            # here would report a broken setup for a working one.
            steps.append({"step": "register_backend", "ok": True,
                          "note": "skipped - no token. The backend works locally; "
                                  "registration only publishes it to the fleet."})
        else:
            reg = imagegen_register_backend(
                genesis_url=genesis_url, base_url=base_url, backend_type=engine,
                models=verified.get("sample_models"), token=token)
            steps.append({"step": "register_backend", "ok": "error" not in reg,
                          **({"error": reg["error"]} if "error" in reg else {})})

    return {
        "ok": ok,
        "stage": stage,
        "recipe": recipe_id,
        "backend": {"engine": engine, "base_url": base_url,
                    "model_count": verified.get("model_count"),
                    "sample_models": verified.get("sample_models")},
        "steps": steps,
        "mod_urls": list(GOBBONET_MOD_URLS),
        "next": ("Add both URLs in GobboNet's Extensions panel. Then point your "
                 "generator at " + base_url + " and dispatch "
                 "`gobbonet:image` with the result - the gallery renders anything "
                 "from a data: URI, a Blob, or a loopback URL like this one."),
    }
