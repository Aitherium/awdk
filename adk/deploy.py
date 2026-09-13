"""aither deploy — Component deployment for the AitherOS ecosystem.

Deploy individual components (Ollama, vLLM, awnode, core stack, full stack)
or companion apps (AitherConnect, AitherDesktop) from public GitHub releases.
Deploy individual components (Ollama, vLLM, AitherNode, core stack, full stack)
or companion apps (Awconnect, AitherDesktop) from public GitHub releases.

Usage (via CLI):
    aither deploy ollama                   # Install + pull models for your GPU
    aither deploy ollama --models qwen3:8b,phi4  # Pull specific models
    aither deploy vllm                     # Set up vLLM inference (delegates to setup)
    aither deploy node                     # awnode + Genesis via Docker
    aither deploy node --gpu --dashboard   # Node with GPU + Veil dashboard
    aither deploy core                     # Core services (Node, Pulse, Watch, Genesis)
    aither deploy full                     # Full AitherOS stack (~55 containers)
    aither deploy full --profile chat-full # Specific chat profile
    aither deploy connect                  # Awconnect browser extension
    aither deploy desktop                  # AitherDesktop native app
    aither deploy stop node                # Stop a running deployment

Pure stdlib -- no pip dependencies required.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from adk.setup_cli import (
    bold,
    cyan,
    dim,
    green,
    red,
    yellow,
    info,
    warn,
    err,
    step,
    detect_gpu,
    GPUInfo,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Pin compose files to a known-good commit so a new push to main can't break
# an older ADK release.  Bump this hash when deployers are tested against a
# new compose revision.
_COMPOSE_PIN = os.environ.get("AITHER_COMPOSE_PIN", "main")  # override for dev

GITHUB_RAW = f"https://raw.githubusercontent.com/Aitherium/AitherOS/{_COMPOSE_PIN}"
GITHUB_RELEASES = "https://github.com/Aitherium/AitherOS/releases/latest/download"
AITHER_DIR = Path.home() / ".aither"
REGISTRY = "ghcr.io/aitherium"

# Compose file URLs — used only as an opt-in override (AITHER_COMPOSE_SOURCE=github)
# for advanced users who want bleeding-edge config. The private AitherOS monorepo
# these point at requires auth this client never sends, so it 404s for anyone else —
# the DEFAULT path is the bundled copy shipped inside this package (see
# _bundled_compose_path below), which needs no network access at all for the config
# step, only for the (public) GHCR image pull that follows it.
COMPOSE_NODE_URL = f"{GITHUB_RAW}/.DEPLOYMENT/compose/docker-compose.node.yml"
COMPOSE_NODE_ADK_URL = f"{GITHUB_RAW}/.DEPLOYMENT/compose/docker-compose.node-adk.yml"
COMPOSE_FULL_URL = f"{GITHUB_RAW}/.DEPLOYMENT/compose/docker-compose.aitheros.yml"


def _bundled_compose_path(name: str) -> Path:
    """Path to a compose file bundled inside this package (adk/deploy/compose/*.yml —
    force-included into the wheel, see pyproject.toml). This is the default source for
    `adk deploy node`'s config (including its optional --federate profile) — no
    network fetch needed."""
    return Path(__file__).parent / "deploy" / "compose" / name


def _resolve_compose_file(name: str, url: str, dest: Path, dry_run: bool) -> bool:
    """Get a compose file to `dest`: bundled copy by default, or a live download from
    `url` if AITHER_COMPOSE_SOURCE=github is explicitly set (advanced/dev override).
    Returns True on success."""
    if os.environ.get("AITHER_COMPOSE_SOURCE", "").strip().lower() == "github":
        return _download(url, dest, dry_run)
    bundled = _bundled_compose_path(name)
    if not bundled.exists():
        # Dev checkout without the force-included file materialized (e.g. running
        # from source pre-build) — fall back to the network path rather than fail.
        warn(f"Bundled compose file not found at {bundled} — falling back to download")
        return _download(url, dest, dry_run)
    if dry_run:
        info(f"Would use bundled compose file: {bundled}")
        info(f"  -> {dest}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bundled.read_bytes())
    info(f"Using bundled compose file: {bundled}")
    info(f"  -> {dest}")
    return True

# Gateway for auth validation
GATEWAY_URL = "https://gateway.aitherium.com"

# Health-check endpoints (port -> service name)
HEALTH_ENDPOINTS = {
    8001: "Genesis",
    8080: "awnode",
    8081: "Pulse",
    8082: "Watch",
    3000: "AitherVeil",
}

# Ollama model tiers keyed by VRAM range
_OLLAMA_MODELS_BY_VRAM = {
    "none": ["gemma4:4b", "nomic-embed-text"],
    "low":  ["gemma4:4b", "nomic-embed-text"],           # <8GB
    "mid":  ["nemotron-orchestrator-8b", "nomic-embed-text"],  # 8-12GB
    "high": [                                              # 12-24GB
        "nemotron-orchestrator-8b",
        "deepseek-r1:14b",
        "nomic-embed-text",
    ],
    "ultra": [                                             # 24GB+
        "nemotron-orchestrator-8b",
        "deepseek-r1:14b",
        "gemma4:27b",
        "nomic-embed-text",
    ],
}


# ---------------------------------------------------------------------------
# Helper: Authentication gate
# ---------------------------------------------------------------------------

def _get_api_key(api_key_arg: Optional[str] = None) -> Optional[str]:
    """Resolve API key from arg > env > saved config. Returns None if not found."""
    key = api_key_arg or os.environ.get("AITHER_API_KEY", "")
    if key:
        return key
    config_path = AITHER_DIR / "config.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            return data.get("api_key", "") or None
        except Exception:
            pass
    # Also check YAML config
    yaml_path = AITHER_DIR / "config.yaml"
    if yaml_path.exists():
        try:
            text = yaml_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("api_key:"):
                    val = line.split(":", 1)[1].strip().strip("'\"")
                    if val:
                        return val
        except Exception:
            pass
    return None


def _validate_api_key(api_key: str) -> bool:
    """Validate an API key against the Aitherium gateway.

    Returns True if valid, False otherwise. Falls back to format check
    if gateway is unreachable (offline deployments still work with a key).
    """
    # Format check: must look like an Aitherium key
    if not (api_key.startswith("aither_") or api_key.startswith("ak_") or len(api_key) >= 32):
        return False

    # Try gateway validation (non-blocking — offline deploys still allowed)
    try:
        req = Request(
            f"{GATEWAY_URL}/v1/auth/validate",
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "AitherADK/1.0",
            },
        )
        with urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        # Gateway unreachable — allow deployment with valid-format key
        # This enables offline/air-gapped sovereign deployments
        return True


def _require_auth(api_key_arg: Optional[str] = None) -> Optional[str]:
    """Gate function for AitherOS component deployment.

    Returns the API key if valid, or None (and prints error) if not.
    """
    key = _get_api_key(api_key_arg)
    if not key:
        err("AitherOS deployment requires an API key")
        print()
        print("  Get one (free):")
        print(f"    {cyan('aither register')}")
        print()
        print("  Or set it:")
        print(f"    {cyan('export AITHER_API_KEY=your_key')}")
        print(f"    {cyan('aither deploy node --api-key your_key')}")
        print()
        return None

    if not _validate_api_key(key):
        err("Invalid API key")
        print()
        print(f"  Register for a new key: {cyan('aither register')}")
        print()
        return None

    return key


def _check_entitlements(
    api_key: str,
    app_id: Optional[str] = None,
    tenant: Optional[str] = None,
) -> dict:
    """Check entitlements for a deployment against the gateway.

    Validates:
    - API key is authorized for the given tenant
    - License tier permits the requested app (requires_plan gating)
    - App is included in licensed packs / named_agents

    Returns a dict with:
        valid: bool
        tier: str (license tier)
        tenant_id: str (authorized tenant)
        error: str (empty if valid)
        entitlements: dict (pack_tier, named_agents, etc.)

    Falls back to permissive mode if gateway is unreachable (offline deploys).
    """
    result = {
        "valid": True,
        "tier": "unknown",
        "tenant_id": tenant or "default",
        "error": "",
        "entitlements": {},
        "offline": False,
    }

    try:
        payload = json.dumps({
            "app_id": app_id,
            "tenant": tenant,
        }).encode()
        req = Request(
            f"{GATEWAY_URL}/v1/auth/entitlements",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "AitherADK/1.0",
            },
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        result["tier"] = data.get("tier", "unknown")
        result["tenant_id"] = data.get("tenant_id", tenant or "default")
        result["entitlements"] = data.get("entitlements", {})

        # Check tenant authorization
        if tenant and data.get("tenant_id") and data["tenant_id"] != tenant:
            result["valid"] = False
            result["error"] = (
                f"API key is for tenant '{data['tenant_id']}', "
                f"not '{tenant}'"
            )
            return result

        # Check app authorization
        if app_id and not data.get("app_authorized", True):
            required_plan = data.get("required_plan", "developer")
            current_tier = data.get("tier", "community")
            result["valid"] = False
            result["error"] = (
                f"App '{app_id}' requires '{required_plan}' plan "
                f"(current: '{current_tier}')"
            )
            return result

        return result

    except Exception:
        # Gateway unreachable — offline mode.
        # Allow deployment but warn. License validation will happen
        # at runtime when the services boot (LicenseManager).
        result["offline"] = True
        return result


def _docker_login_ghcr(api_key: str) -> bool:
    """Authenticate with GHCR using the API key for private image pulls."""
    try:
        result = subprocess.run(
            ["docker", "login", REGISTRY, "-u", "aither", "--password-stdin"],
            input=api_key,
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace",
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def _require_ghcr_login(api_key: str, dry_run: bool = False) -> bool:
    """Attempt GHCR login; return True on success or dry-run.  Blocks on failure."""
    if dry_run:
        return True
    if _docker_login_ghcr(api_key):
        info(f"Authenticated with {REGISTRY}")
        return True
    err("GHCR login failed — cannot pull container images")
    print()
    print(f"  Verify your API key: {cyan('adk connect --api-key <key>')}")
    print(f"  Or set {cyan('AITHER_API_KEY')} in your environment.")
    return False


# ---------------------------------------------------------------------------
# Helper: subprocess runner
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 30) -> Optional[str]:
    """Run a command and return stdout on success, None on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Helper: file download
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path, dry_run: bool = False) -> bool:
    """Download a file using urllib.request.  Skips if cached copy is fresh.

    Uses ETag / If-None-Match to avoid re-downloading an unchanged file.
    Returns True on success (including cache hit).
    """
    if dry_run:
        info(f"Would download: {url}")
        info(f"  -> {dest}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)

    # ── Cache check: skip download if etag matches ──────────────────
    etag_file = dest.with_suffix(dest.suffix + ".etag")
    headers = {"User-Agent": "AitherADK/1.0"}
    if dest.exists() and etag_file.exists():
        stored_etag = etag_file.read_text(encoding="utf-8").strip()
        if stored_etag:
            headers["If-None-Match"] = stored_etag

    info(f"Downloading: {url}")
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=120) as resp:
            data = resp.read()
            # Save new etag if server provided one
            new_etag = resp.headers.get("ETag", "")
            if new_etag:
                etag_file.write_text(new_etag, encoding="utf-8")
        dest.write_bytes(data)
        size_kb = len(data) / 1024
        if size_kb > 1024:
            info(f"  -> {dest} ({size_kb / 1024:.1f} MB)")
        else:
            info(f"  -> {dest} ({size_kb:.0f} KB)")
        return True
    except HTTPError as exc:
        if exc.code == 304:
            info(f"  -> {dest} (cached, unchanged)")
            return True
        err(f"Download failed: {exc}")
        return False
    except (URLError, OSError) as exc:
        err(f"Download failed: {exc}")
        return False


def _download_bytes(url: str) -> Optional[bytes]:
    """Download a URL and return raw bytes, or None on failure."""
    try:
        req = Request(url, headers={"User-Agent": "AitherADK/1.0"})
        with urlopen(req, timeout=120) as resp:
            return resp.read()
    except (HTTPError, URLError, OSError) as exc:
        err(f"Download failed: {exc}")
        return None


def _url_exists(url: str) -> bool:
    """Send a HEAD request; return True if the server responds 200-399."""
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": "AitherADK/1.0"})
        with urlopen(req, timeout=15) as resp:
            return resp.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helper: Docker Compose
# ---------------------------------------------------------------------------

def _docker_compose(
    compose_file: Path,
    profiles: list[str],
    action: str,
    dry_run: bool,
    timeout: int = 300,
) -> int:
    """Run docker compose with given profiles and action (pull/up -d/down/ps).

    Returns the subprocess exit code, or 0 on dry run.
    """
    cmd = ["docker", "compose", "-f", str(compose_file)]
    for p in profiles:
        cmd += ["--profile", p]

    # Split action into parts (e.g. "up -d" -> ["up", "-d"])
    cmd += action.split()

    if dry_run:
        info(f"Would run: {' '.join(cmd)}")
        return 0

    info(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, timeout=timeout)
        return result.returncode
    except subprocess.TimeoutExpired:
        warn(f"Command timed out after {timeout}s -- containers may still be starting")
        return 0
    except FileNotFoundError:
        err("docker command not found")
        return 1
    except Exception as exc:
        err(f"Command failed: {exc}")
        return 1


# ---------------------------------------------------------------------------
# Helper: Health Check
# ---------------------------------------------------------------------------

def _health_check(url: str, timeout: int = 120, compose_file: Optional[Path] = None) -> bool:
    """Poll a health endpoint until it returns 200 or timeout (seconds).

    If *compose_file* is given and the check fails, tails Docker Compose logs
    so the user can see why the container isn't responding.
    """
    start = time.time()
    dots = 0
    while time.time() - start < timeout:
        try:
            req = Request(url, headers={"User-Agent": "AitherADK/1.0"})
            with urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    if dots > 0:
                        print()
                    return True
        except (HTTPError, URLError, ConnectionError, OSError):
            pass
        if dots == 0:
            print(f"    Waiting for {url}", end="", flush=True)
        print(".", end="", flush=True)
        dots += 1
        time.sleep(3)
    if dots > 0:
        print()
    # On failure, dump the last 30 lines of compose logs if available
    if compose_file and compose_file.exists():
        warn("Service did not become healthy — showing recent container logs:")
        try:
            subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "logs", "--tail", "30"],
                timeout=10,
            )
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Helper: Docker prerequisite check
# ---------------------------------------------------------------------------

def _check_docker() -> tuple[bool, str]:
    """Check if Docker is installed and daemon is running.

    Returns (ok, message).
    """
    docker = shutil.which("docker")
    if not docker:
        return False, "Docker is not installed"
    out = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if not out:
        return False, "Docker daemon is not running"
    return True, f"Docker {out}"


# ---------------------------------------------------------------------------
# Helper: VRAM tier selection
# ---------------------------------------------------------------------------

def _vram_tier(gpu: GPUInfo) -> str:
    """Classify GPU VRAM into an Ollama model tier key."""
    if gpu.vendor == "none":
        return "none"
    vram_gb = gpu.vram_mb / 1024 if gpu.vram_mb else 0
    if vram_gb >= 24:
        return "ultra"
    if vram_gb >= 12:
        return "high"
    if vram_gb >= 8:
        return "mid"
    return "low"


# ---------------------------------------------------------------------------
# Helper: Platform detection
# ---------------------------------------------------------------------------

def _platform_name() -> str:
    """Return a normalized platform name: linux, macos, windows."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows" or sys.platform == "win32":
        return "windows"
    return "linux"


# ===========================================================================
# Component: Ollama
# ===========================================================================

def deploy_ollama(dry_run: bool = False, models: Optional[list[str]] = None) -> int:
    """Deploy Ollama with appropriate models for the detected GPU.

    Args:
        dry_run: If True, show what would happen without executing.
        models: Explicit model list to pull. If None, auto-select by GPU VRAM.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    print()
    print(bold("  Ollama Deployment"))
    print(dim("  Local LLM inference with automatic GPU offloading"))
    print()

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    total_steps = 4
    plat = _platform_name()

    # -- Step 1: Check installation -------------------------------------------
    step(1, total_steps, "Checking Ollama installation")

    ollama = shutil.which("ollama")
    if not ollama:
        warn("Ollama is not installed")
        print()
        if plat == "linux":
            print(f"  Install with:")
            print(f"    {cyan('curl -fsSL https://ollama.com/install.sh | sh')}")
        elif plat == "macos":
            print(f"  Install with:")
            print(f"    {cyan('brew install ollama')}")
        elif plat == "windows":
            print(f"  Install with:")
            print(f"    {cyan('winget install Ollama.Ollama')}")
        print()
        print(f"  Or download from: {cyan('https://ollama.com/download')}")
        return 1

    info(f"Ollama binary: {ollama}")

    # -- Step 2: Detect GPU ---------------------------------------------------
    step(2, total_steps, "Detecting GPU hardware")

    gpu = detect_gpu()
    vram_gb = gpu.vram_mb / 1024 if gpu.vram_mb else 0

    if gpu.vendor == "nvidia":
        info(f"GPU: {bold(gpu.name)} ({vram_gb:.0f} GB VRAM)")
        if gpu.cuda_version:
            info(f"CUDA: {gpu.cuda_version}")
    elif gpu.vendor == "amd":
        info(f"GPU: {bold(gpu.name)} (AMD ROCm)")
    elif gpu.vendor == "apple":
        info(f"GPU: {bold(gpu.name)} (unified memory: {vram_gb:.0f} GB)")
    else:
        warn("No GPU detected -- Ollama will use CPU inference (slow)")

    tier = _vram_tier(gpu)
    info(f"VRAM tier: {bold(tier)}")

    # -- Step 3: Ensure Ollama is running ------------------------------------
    step(3, total_steps, "Starting Ollama service")

    running = _run(["ollama", "list"])
    if running is not None:
        info("Ollama is already running")
    else:
        warn("Ollama is not running -- starting...")
        if not dry_run:
            # Start ollama serve in background
            if plat == "windows":
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                )
            else:
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            time.sleep(3)
            # Verify it started
            verify = _run(["ollama", "list"])
            if verify is not None:
                info("Ollama started successfully")
            else:
                warn("Ollama may still be starting -- continuing anyway")
        else:
            info("Would start: ollama serve (background)")

    # Build set of already-installed models
    existing_models: set[str] = set()
    model_list_out = _run(["ollama", "list"])
    if model_list_out:
        for line in model_list_out.strip().split("\n")[1:]:  # skip header
            parts = line.split()
            if parts:
                # Models appear as "name:tag" -- store both full and base
                full_name = parts[0]
                existing_models.add(full_name)
                existing_models.add(full_name.split(":")[0])

    # -- Step 4: Pull models --------------------------------------------------
    step(4, total_steps, "Pulling models")

    if models:
        models_to_pull = list(models)
        info(f"Using explicit model list: {', '.join(models_to_pull)}")
    else:
        models_to_pull = list(_OLLAMA_MODELS_BY_VRAM.get(tier, _OLLAMA_MODELS_BY_VRAM["none"]))
        info(f"Auto-selected for {bold(tier)} tier: {', '.join(models_to_pull)}")

    # Estimate resource usage
    total_disk_gb = 0.0
    size_estimates = {
        "gemma4:4b": 2.5, "gemma4:27b": 16.0,
        "nemotron-orchestrator-8b": 5.0, "deepseek-r1:14b": 9.0,
        "deepseek-r1:7b": 4.5, "nomic-embed-text": 0.3,
        "qwen3:8b": 5.0, "llama3.2:3b": 2.0,
    }
    for m in models_to_pull:
        total_disk_gb += size_estimates.get(m, 4.0)

    print()
    info(f"Estimated disk usage: ~{total_disk_gb:.1f} GB")
    if tier in ("mid", "high", "ultra"):
        info(f"Estimated VRAM at runtime: models loaded on demand, one at a time")
    else:
        info(f"CPU inference: expect 5-20 tokens/sec depending on model size")
    print()

    pulled = 0
    skipped = 0
    failed = 0

    for model in models_to_pull:
        base = model.split(":")[0]
        if base in existing_models or model in existing_models:
            info(f"Already installed: {model}")
            skipped += 1
            continue

        if dry_run:
            info(f"Would pull: {bold(model)}")
            pulled += 1
            continue

        info(f"Pulling: {bold(model)} ...")
        try:
            result = subprocess.run(
                ["ollama", "pull", model],
                timeout=1800,  # 30-minute timeout per model
            )
            if result.returncode == 0:
                info(f"  {green('OK')}")
                pulled += 1
            else:
                warn(f"  Pull returned exit code {result.returncode}")
                failed += 1
        except subprocess.TimeoutExpired:
            warn(f"  Pull timed out for {model} (30 min limit)")
            failed += 1
        except Exception as exc:
            warn(f"  Failed to pull {model}: {exc}")
            failed += 1

    # -- Summary --------------------------------------------------------------
    print()
    print(bold("  " + "-" * 60))
    print()
    info(f"Pulled: {pulled}  Skipped (already installed): {skipped}  Failed: {failed}")
    if failed > 0:
        warn("Some models failed to pull -- re-run to retry")
    print()
    info(f"Ollama API: {cyan('http://localhost:11434')}")
    info(f"OpenAI-compatible: {cyan('http://localhost:11434/v1')}")
    print()
    info(f"Test it: {cyan('ollama run ' + models_to_pull[0])}")
    print()
    if not dry_run:
        info(f"{green(bold('Ollama is ready!'))}")
    return 1 if failed > 0 and pulled == 0 else 0


# ===========================================================================
# Component: vLLM
# ===========================================================================

def deploy_vllm(dry_run: bool = False, tier: Optional[str] = None, hf_token: str = "") -> int:
    """Deploy vLLM inference containers.

    Delegates to the existing setup_cli logic which handles GPU detection,
    tier selection, compose generation, and container startup.

    Args:
        dry_run: If True, show what would happen without executing.
        tier: Force a specific vLLM tier (nano/lite/standard/full/ollama).
        hf_token: HuggingFace token for gated model access.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    from adk.setup_cli import cmd_setup

    # Synthesize an args namespace matching what cmd_setup expects
    class _SetupArgs:
        pass

    args = _SetupArgs()
    args.dry_run = dry_run
    args.tier = tier
    args.hf_token = hf_token or os.environ.get("HF_TOKEN", "")
    args.non_interactive = bool(tier)  # non-interactive if tier is forced
    args.output = str(AITHER_DIR / "docker-compose.vllm.yml")
    args.stack = None
    args.api_key = ""

    return cmd_setup(args)


# ===========================================================================
# Component: ADK Node — native fallback (no Docker)
# ===========================================================================

def _deploy_adk_node_native(dry_run: bool = False, api_key: str = "") -> int:
    """Start adk-serve natively when Docker is unavailable.

    This runs `adk-serve` as a background process and verifies it responds
    on the health endpoint.  It does NOT require Docker, Redis, or Genesis.
    """
    adk_serve = shutil.which("adk-serve")
    if not adk_serve:
        err("Neither Docker nor adk-serve is available")
        print()
        print(f"  Option 1: Install Docker — {cyan('https://docker.com/products/docker-desktop')}")
        print(f"  Option 2: Ensure adk-serve is in PATH — {cyan('pip install awdk')}")
        return 1

    if dry_run:
        info(f"Would start: {adk_serve}")
        return 0

    info(f"Starting adk-serve natively: {adk_serve}")
    env = os.environ.copy()
    if api_key:
        env["AITHER_API_KEY"] = api_key

    try:
        # Start as detached subprocess (survives parent exit)
        pid_file = AITHER_DIR / "adk-serve.pid"
        if sys.platform == "win32":
            proc = subprocess.Popen(
                [adk_serve],
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            proc = subprocess.Popen(
                [adk_serve],
                env=env,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        pid_file.write_text(str(proc.pid))
        info(f"adk-serve started (PID {proc.pid})")
    except Exception as exc:
        err(f"Failed to start adk-serve: {exc}")
        return 1

    # Health check
    if _health_check("http://localhost:8080/health", timeout=30):
        info(f"ADK server (:{cyan('8080')}): {green('healthy')}")
        print()
        info(f"{green(bold('ADK Node is ready!'))}")
        return 0
    else:
        warn("ADK server started but health check timed out — check logs")
        return 0


# ===========================================================================
# Component: ADK Node (Tier 2 — lightweight, no Genesis)
# ===========================================================================

def deploy_adk_node(
    dry_run: bool = False,
    tag: str = "latest",
    gpu: bool = False,
    dashboard: bool = False,
    memory: bool = False,
    api_key_arg: Optional[str] = None,
    sovereign: bool = False,
    hub_url: str = "https://api.aitherium.com",
    tenant: Optional[str] = None,
    federate: bool = False,
    portal_token: Optional[str] = None,
) -> int:
    """Deploy ADK-native node: ADK server + Ollama + optional profiles.

    No Genesis, no Redis, no PostgreSQL, no MicroScheduler. The ADK server
    IS the brain — ReAct loop, tools, fleet, forge all run in-process.

    Resource estimates:
        Base:      ~768 MB RAM, ~2 GB disk (images)
        + GPU:     +1 GB RAM,   +2 GB disk
        + Memory:  +512 MB RAM, +500 MB disk
        + Dashboard: +768 MB RAM, +1 GB disk

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    print()
    print(bold("  ADK Node Deployment"))
    print(dim("  ADK server (8080) + Ollama — lightweight, no Genesis"))
    print()

    # Auth gate
    api_key = _require_auth(api_key_arg)
    if not api_key:
        return 1

    fed_token = portal_token or os.environ.get("AITHER_PORTAL_TOKEN", "")
    if federate and not fed_token:
        err("--federate requires AITHER_PORTAL_TOKEN — get one from your Aitherium portal account")
        print(f"  {dim('Set it via --portal-token, or export AITHER_PORTAL_TOKEN=...')}")
        return 1

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    total_steps = 5 if sovereign else 4

    # -- Step 1: Prerequisites ------------------------------------------------
    step(1, total_steps, "Checking prerequisites")

    docker_ok, docker_msg = _check_docker()
    if docker_ok:
        info(f"{docker_msg}")
    else:
        # Docker not available — fall back to running adk-serve natively
        warn(f"{docker_msg} — falling back to native adk-serve")
        return _deploy_adk_node_native(dry_run, api_key)

    if not dry_run:
        if not _require_ghcr_login(api_key, dry_run):
            return 1

    # -- Step 2: Compose configuration -----------------------------------------
    step(2, total_steps, "Preparing ADK compose configuration")

    compose_file = AITHER_DIR / "docker-compose.node-adk.yml"
    if not _resolve_compose_file("docker-compose.node-adk.yml", COMPOSE_NODE_ADK_URL, compose_file, dry_run):
        err("Failed to prepare compose file")
        return 1

    # -- Step 3: Pull and start -----------------------------------------------
    step(3, total_steps, "Starting containers")

    profiles = []
    if gpu:
        profiles.append("gpu")
        info("Profile: gpu (vLLM GPU-accelerated inference)")
    if memory:
        profiles.append("memory")
        info("Profile: memory (Spirit persistent vector memory)")
    if dashboard:
        profiles.append("dashboard")
        info("Profile: dashboard (workspace app on port 3000)")
    if federate:
        profiles.append("federate")
        info("Profile: federate (AitherFederate bridge to api.aitherium.com on :8094)")

    if not profiles:
        info("Profile: default (ADK server + Ollama)")

    # Resource estimates
    ram_mb = 768 + (1024 if gpu else 0) + (512 if memory else 0) + (768 if dashboard else 0) + (512 if federate else 0)
    disk_gb = 2.0 + (2.0 if gpu else 0) + (0.5 if memory else 0) + (1.0 if dashboard else 0) + (1.0 if federate else 0)
    print()
    info(f"Estimated resources: ~{ram_mb / 1024:.1f} GB RAM, ~{disk_gb:.0f} GB disk (images)")
    print()

    # _docker_compose's subprocess inherits the real process env (no env= passed
    # through) — must set on os.environ itself, not a discarded local copy.
    if not dry_run:
        os.environ["ADK_IMAGE_TAG"] = tag
        os.environ["AITHEROS_IMAGE_TAG"] = tag
        os.environ["AITHEROS_REGISTRY"] = REGISTRY
        if gpu:
            os.environ.setdefault("VLLM_URL", "http://vllm:8120")
        if memory:
            os.environ.setdefault("AITHER_SPIRIT_URL", "http://spirit:8087")
        if federate:
            os.environ["AITHER_PORTAL_TOKEN"] = fed_token
            os.environ["AITHER_PORTAL_URL"] = hub_url

    rc = _docker_compose(compose_file, profiles, "pull", dry_run, timeout=600)
    if rc != 0:
        err("Failed to pull images")
        return 1

    rc = _docker_compose(compose_file, profiles, "up -d", dry_run, timeout=120)
    if rc != 0:
        err("Failed to start containers")
        return 1

    # -- Step 4: Health checks ------------------------------------------------
    step(4, total_steps, "Verifying services")

    if dry_run:
        info("Would check: http://localhost:8080/health (ADK server)")
        if dashboard:
            info("Would check: http://localhost:3000 (Workspace dashboard)")
        if federate:
            info("Would check: http://localhost:8094/health (AitherFederate)")
    else:
        endpoints = [(8080, "ADK server")]
        if dashboard:
            endpoints.append((3000, "Workspace dashboard"))
        if federate:
            endpoints.append((8094, "AitherFederate"))

        for port, name in endpoints:
            if _health_check(f"http://localhost:{port}/health", timeout=90, compose_file=compose_file):
                info(f"{name} (:{port}): {green('healthy')}")
            else:
                warn(f"{name} (:{port}): not responding yet -- may still be starting")

    # -- Step 5 (optional): Federation registration ----------------------------
    if sovereign:
        step(total_steps, total_steps, "Registering with federation hub")
        tenant_slug = tenant or os.environ.get("AITHER_TENANT") or "default"
        reg_url = f"{hub_url.rstrip('/')}/federation/register"

        if dry_run:
            info(f"Would POST to {reg_url}")
            info(f"  tenant_slug: {tenant_slug}")
        else:
            try:
                import json as _json
                payload = _json.dumps({"tenant_slug": tenant_slug}).encode()
                req = Request(reg_url, data=payload, method="POST",
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=15) as resp:
                    result = _json.loads(resp.read())
                info(f"Registered as node {cyan(result.get('node_id', ''))}")
            except Exception as e:
                warn(f"Federation registration failed: {e}")

    # -- Summary --------------------------------------------------------------
    print()
    print(bold("  " + "-" * 60))
    print()
    info(f"ADK server:  {cyan('http://localhost:8080')}")
    info(f"Ollama:      {cyan('http://localhost:11434')}")
    if gpu:
        info(f"vLLM:        {cyan('http://localhost:8120')}")
    if memory:
        info(f"Spirit:      {cyan('http://localhost:8087')}")
    if dashboard:
        info(f"Dashboard:   {cyan('http://localhost:3000')}")
    if federate:
        info(f"Federate:    {cyan('http://localhost:8094')}")
    print()
    info("Manage:")
    info(f"  Logs:  {cyan(f'docker compose -f {compose_file} logs -f')}")
    info(f"  Stop:  {cyan('adk deploy stop node')}")
    info(f"  PS:    {cyan(f'docker compose -f {compose_file} ps')}")
    print()
    info(f"  Containers: {bold('2')}" + (f" + {len(profiles)} profile(s)" if profiles else ""))
    info(f"  vs 'adk deploy full': {dim('14+ containers with Genesis, Redis, PostgreSQL...')}")
    print()
    if not dry_run:
        info(f"{green(bold('ADK Node is ready!'))}")
    return 0


# ===========================================================================
# Component: awnode (Tier 3 — full Genesis stack)
# ===========================================================================

def deploy_node(
    dry_run: bool = False,
    tag: str = "latest",
    gpu: bool = False,
    dashboard: bool = False,
    mesh: bool = False,
    memory: bool = False,
    api_key_arg: Optional[str] = None,
    sovereign: bool = False,
    hub_url: str = "https://api.aitherium.com",
    tenant: Optional[str] = None,
    storefront: bool = False,
    federate: bool = False,
    portal_token: Optional[str] = None,
) -> int:
    """Deploy awnode (MCP server) + Genesis orchestrator via Docker Compose.

    Downloads the node compose file from GitHub and starts the containers with
    selected profile flags. Requires a valid Aitherium API key.

    `federate` is distinct from `sovereign`: `sovereign` makes a one-shot API call
    to register this deployment with the hub (saves fed_node_id/fed_api_key to
    .env.federation). `federate` starts the actual AitherFederate container
    (port 8094) — an ongoing local bridge other services can call for fleet
    registration, product-catalog sync, and knowledge ingestion routing. Requires
    AITHER_PORTAL_TOKEN (via --portal-token or the env var) when set.

    Resource estimates:
        Base:      ~2 GB RAM,  ~4 GB disk (images)
        + GPU:     +1 GB RAM,  +2 GB disk
        + Memory:  +512 MB RAM, +500 MB disk
        + Dashboard: +512 MB RAM, +1 GB disk
        + Mesh:    +256 MB RAM
        + Federate: +512 MB RAM, +1 GB disk

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    print()
    print(bold("  awnode Deployment"))
    print(dim("  MCP server (8080) + Genesis orchestrator (8001)"))
    print()

    # Auth gate
    api_key = _require_auth(api_key_arg)
    if not api_key:
        return 1

    fed_token = portal_token or os.environ.get("AITHER_PORTAL_TOKEN", "")
    if federate and not fed_token:
        err("--federate requires AITHER_PORTAL_TOKEN — get one from your Aitherium portal account")
        print(f"  {dim('Set it via --portal-token, or export AITHER_PORTAL_TOKEN=...')}")
        return 1

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    total_steps = 5 if sovereign else 4

    # -- Step 1: Prerequisites ------------------------------------------------
    step(1, total_steps, "Checking prerequisites")

    docker_ok, docker_msg = _check_docker()
    if docker_ok:
        info(f"{docker_msg}")
    else:
        err(docker_msg)
        print()
        print(f"  Install Docker: {cyan('https://docker.com/products/docker-desktop')}")
        return 1

    # Authenticate with GHCR for private image pulls
    if not _require_ghcr_login(api_key, dry_run):
        return 1

    # -- Step 2: Compose configuration -----------------------------------------
    step(2, total_steps, "Preparing compose configuration")

    compose_file = AITHER_DIR / "docker-compose.node.yml"
    if not _resolve_compose_file("docker-compose.node.yml", COMPOSE_NODE_URL, compose_file, dry_run):
        err("Failed to prepare compose file")
        return 1

    # -- Step 3: Pull and start -----------------------------------------------
    step(3, total_steps, "Starting containers")

    profiles = []
    if gpu:
        profiles.append("gpu")
        info("Profile: gpu (GPU-accelerated services)")
    if memory:
        profiles.append("memory")
        info("Profile: memory (Spirit + WorkingMemory for persistent vector memory)")
    if dashboard:
        profiles.append("dashboard")
        info("Profile: dashboard (AitherVeil on port 3000)")
    if mesh:
        profiles.append("mesh")
        info("Profile: mesh (multi-node networking)")
    if storefront:
        profiles.append("storefront")
        info("Profile: storefront (public storefront + landing pages)")
    if federate:
        profiles.append("federate")
        info("Profile: federate (AitherFederate bridge to api.aitherium.com on :8094)")

    if not profiles:
        info("Profile: default (Node + Genesis)")

    # Resource estimates
    ram_gb = 2.0 + (1.0 if gpu else 0) + (0.5 if dashboard else 0) + (0.25 if mesh else 0) + (0.5 if storefront else 0) + (0.5 if memory else 0) + (0.5 if federate else 0)
    disk_gb = 4.0 + (2.0 if gpu else 0) + (1.0 if dashboard else 0) + (0.5 if storefront else 0) + (0.5 if memory else 0) + (1.0 if federate else 0)
    print()
    info(f"Estimated resources: ~{ram_gb:.1f} GB RAM, ~{disk_gb:.0f} GB disk (images)")
    print()

    # Set environment variables for the compose file. _docker_compose's subprocess
    # inherits the real process env (no env= is passed through), so these must be
    # set on os.environ itself, not a discarded local copy (that was a pre-existing
    # bug here — harmless while every var had a `:-default` in the compose file, but
    # AITHER_PORTAL_TOKEN below is a *required* compose var with no default).
    if not dry_run:
        os.environ["AITHEROS_IMAGE_TAG"] = tag
        os.environ["AITHEROS_REGISTRY"] = REGISTRY
        if federate:
            os.environ["AITHER_PORTAL_TOKEN"] = fed_token
            os.environ["AITHER_PORTAL_URL"] = hub_url

    # Pull images
    rc = _docker_compose(compose_file, profiles, "pull", dry_run, timeout=600)
    if rc != 0:
        err("Failed to pull images")
        return 1

    # Start containers
    rc = _docker_compose(compose_file, profiles, "up -d", dry_run, timeout=120)
    if rc != 0:
        err("Failed to start containers")
        return 1

    # -- Step 4: Health checks ------------------------------------------------
    step(4, total_steps, "Verifying services")

    if dry_run:
        info("Would check: http://localhost:8080/health (awnode)")
        info("Would check: http://localhost:8001/health (Genesis)")
        if dashboard:
            info("Would check: http://localhost:3000 (AitherVeil)")
        if federate:
            info("Would check: http://localhost:8094/health (AitherFederate)")
    else:
        endpoints = [
            (8080, "awnode"),
            (8001, "Genesis"),
        ]
        if dashboard:
            endpoints.append((3000, "AitherVeil"))
        if federate:
            endpoints.append((8094, "AitherFederate"))

        all_healthy = True
        for port, name in endpoints:
            if _health_check(f"http://localhost:{port}/health", timeout=120, compose_file=compose_file):
                info(f"{name} (:{port}): {green('healthy')}")
            else:
                warn(f"{name} (:{port}): not responding yet -- may still be starting")
                all_healthy = False

    # -- Step 5 (optional): Federation registration ----------------------------
    if sovereign:
        step(total_steps, total_steps, "Registering with federation hub")
        tenant_slug = tenant or os.environ.get("AITHER_TENANT") or "default"
        reg_url = f"{hub_url.rstrip('/')}/federation/register"

        if dry_run:
            info(f"Would POST to {reg_url}")
            info(f"  tenant_slug: {tenant_slug}")
        else:
            try:
                import json as _json
                payload = _json.dumps({"tenant_slug": tenant_slug}).encode()
                req = Request(reg_url, data=payload, method="POST",
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=15) as resp:
                    result = _json.loads(resp.read())
                fed_node_id = result.get("node_id", "")
                fed_api_key = result.get("api_key", "")
                info(f"Registered as node {cyan(fed_node_id)}")

                # Persist federation credentials
                fed_env = AITHER_DIR / ".env.federation"
                fed_env.write_text(
                    f"AITHER_FED_NODE_ID={fed_node_id}\n"
                    f"AITHER_FED_API_KEY={fed_api_key}\n"
                    f"AITHER_FED_HUB={hub_url}\n"
                    f"AITHER_FED_TENANT={tenant_slug}\n",
                    encoding="utf-8",
                )
                info(f"Federation credentials saved to {dim(str(fed_env))}")
            except Exception as e:
                warn(f"Federation registration failed: {e}")
                warn("Node is running but not connected to hub -- register later with 'adk connect'")

    # -- Summary --------------------------------------------------------------
    print()
    print(bold("  " + "-" * 60))
    print()
    info(f"awnode: {cyan('http://localhost:8080')}")
    info(f"Genesis:    {cyan('http://localhost:8001')}")
    if dashboard:
        info(f"Dashboard:  {cyan('http://localhost:3000')}")
    if federate:
        info(f"Federate:   {cyan('http://localhost:8094')}")
    if sovereign and not dry_run:
        info(f"Federation: {cyan(hub_url)} (tenant: {tenant or 'default'})")
    print()
    info(f"Manage:")
    info(f"  Logs:  {cyan(f'docker compose -f {compose_file} logs -f')}")
    info(f"  Stop:  {cyan(f'aither deploy stop node')}")
    info(f"  PS:    {cyan(f'docker compose -f {compose_file} ps')}")
    print()
    if not dry_run:
        info(f"{green(bold('awnode is ready!'))}")
    return 0


# ===========================================================================
# Component: Core Stack
# ===========================================================================

def deploy_core(dry_run: bool = False, tag: str = "latest", api_key_arg: Optional[str] = None) -> int:
    """Deploy AitherOS core services (Node, Genesis, Pulse, Watch).

    Same as deploy_node but always includes GPU and dashboard profiles,
    and health-checks all core ports. Requires a valid Aitherium API key.

    Resource estimates:
        ~4 GB RAM, ~8 GB disk (images)

    Args:
        dry_run: If True, show what would happen without executing.
        tag: Docker image tag (default: latest).
        api_key_arg: Explicit API key (falls back to env/config).

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    print()
    print(bold("  AitherOS Core Deployment"))
    print(dim("  Genesis (8001) + Node (8080) + Pulse (8081) + Watch (8082)"))
    print()

    # Auth gate
    api_key = _require_auth(api_key_arg)
    if not api_key:
        return 1

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    total_steps = 4

    # -- Step 1: Prerequisites ------------------------------------------------
    step(1, total_steps, "Checking prerequisites")

    docker_ok, docker_msg = _check_docker()
    if docker_ok:
        info(docker_msg)
    else:
        err(docker_msg)
        print()
        print(f"  Install Docker: {cyan('https://docker.com/products/docker-desktop')}")
        return 1

    # Authenticate with GHCR
    if not dry_run:
        if _docker_login_ghcr(api_key):
            info(f"Authenticated with {REGISTRY}")
        else:
            warn(f"GHCR login failed -- image pulls may fail if images are private")

    # Resource estimate
    print()
    info("Estimated resources: ~4 GB RAM, ~8 GB disk (images)")
    print()

    # -- Step 2: Download compose file ----------------------------------------
    step(2, total_steps, "Downloading compose configuration")

    compose_file = AITHER_DIR / "docker-compose.node.yml"
    if not _download(COMPOSE_NODE_URL, compose_file, dry_run):
        err("Failed to download compose file")
        return 1

    # -- Step 3: Pull and start -----------------------------------------------
    step(3, total_steps, "Starting core containers")

    profiles = ["gpu", "dashboard"]
    info("Profiles: gpu, dashboard (full core stack)")

    env = os.environ.copy()
    env["AITHEROS_IMAGE_TAG"] = tag
    env["AITHEROS_REGISTRY"] = REGISTRY

    rc = _docker_compose(compose_file, profiles, "pull", dry_run, timeout=600)
    if rc != 0:
        err("Failed to pull images")
        return 1

    rc = _docker_compose(compose_file, profiles, "up -d", dry_run, timeout=120)
    if rc != 0:
        err("Failed to start containers")
        return 1

    # -- Step 4: Health checks ------------------------------------------------
    step(4, total_steps, "Verifying core services")

    core_ports = [
        (8001, "Genesis"),
        (8080, "awnode"),
        (8081, "Pulse"),
        (8082, "Watch"),
        (3000, "AitherVeil"),
    ]

    if dry_run:
        for port, name in core_ports:
            info(f"Would check: http://localhost:{port}/health ({name})")
    else:
        for port, name in core_ports:
            if _health_check(f"http://localhost:{port}/health", timeout=120):
                info(f"{name} (:{port}): {green('healthy')}")
            else:
                warn(f"{name} (:{port}): not responding yet -- may still be starting")

    # -- Summary --------------------------------------------------------------
    print()
    print(bold("  " + "-" * 60))
    print()
    for port, name in core_ports:
        info(f"{name:15s} {cyan(f'http://localhost:{port}')}")
    print()
    info(f"Manage:")
    info(f"  Logs:  {cyan(f'docker compose -f {compose_file} logs -f')}")
    info(f"  Stop:  {cyan('aither deploy stop core')}")
    info(f"  PS:    {cyan(f'docker compose -f {compose_file} ps')}")
    print()
    if not dry_run:
        info(f"{green(bold('AitherOS core is ready!'))}")
    return 0


# ===========================================================================
# Component: Full Stack
# ===========================================================================

def deploy_full(
    dry_run: bool = False,
    tag: str = "latest",
    profile: str = "chat-agents",
    api_key_arg: Optional[str] = None,
) -> int:
    """Deploy the full AitherOS stack via docker compose.

    WARNING: This is a large deployment (~55 containers). Ensure sufficient
    system resources before proceeding. Requires a valid Aitherium API key.

    Resource estimates by profile:
        chat-minimal:  ~20 containers, ~8 GB RAM,  ~15 GB disk
        chat-full:     ~29 containers, ~12 GB RAM, ~20 GB disk
        chat-agents:   ~31 containers, ~14 GB RAM, ~22 GB disk

    Args:
        dry_run: If True, show what would happen without executing.
        tag: Docker image tag (default: latest).
        profile: Docker Compose profile (default: chat-agents).
        api_key_arg: Explicit API key (falls back to env/config).

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    profile_resources = {
        "chat-minimal": ("~20 containers", "~8 GB RAM", "~15 GB disk"),
        "chat-full":    ("~29 containers", "~12 GB RAM", "~20 GB disk"),
        "chat-agents":  ("~31 containers", "~14 GB RAM", "~22 GB disk"),
    }
    containers, ram, disk = profile_resources.get(
        profile, ("unknown", "~14 GB RAM", "~22 GB disk")
    )

    print()
    print(bold("  AitherOS Full Stack Deployment"))
    print(dim(f"  Profile: {profile} ({containers})"))
    print()

    # Auth gate
    api_key = _require_auth(api_key_arg)
    if not api_key:
        return 1

    print(f"  {yellow('WARNING: This is a large deployment.')}")
    print(f"  {yellow(f'Resource requirements: {ram}, {disk} (images)')}")
    print()

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    total_steps = 4

    # -- Step 1: Prerequisites ------------------------------------------------
    step(1, total_steps, "Checking prerequisites")

    docker_ok, docker_msg = _check_docker()
    if docker_ok:
        info(docker_msg)
    else:
        err(docker_msg)
        print()
        print(f"  Install Docker: {cyan('https://docker.com/products/docker-desktop')}")
        return 1

    # Authenticate with GHCR
    if not dry_run:
        if _docker_login_ghcr(api_key):
            info(f"Authenticated with {REGISTRY}")
        else:
            warn(f"GHCR login failed -- image pulls may fail if images are private")

    # Check available RAM (best-effort)
    try:
        if _platform_name() == "linux":
            meminfo = Path("/proc/meminfo").read_text()
            for line in meminfo.split("\n"):
                if line.startswith("MemTotal:"):
                    mem_kb = int(line.split()[1])
                    mem_gb = mem_kb / (1024 * 1024)
                    if mem_gb < 12:
                        warn(f"System RAM: {mem_gb:.0f} GB (recommended: 16+ GB for full stack)")
                    else:
                        info(f"System RAM: {mem_gb:.0f} GB")
                    break
    except Exception:
        pass

    # -- Step 2: Download compose file ----------------------------------------
    step(2, total_steps, "Downloading compose configuration")

    compose_file = AITHER_DIR / "docker-compose.aitheros.yml"
    if not _download(COMPOSE_FULL_URL, compose_file, dry_run):
        err("Failed to download compose file")
        return 1

    # -- Step 3: Pull and start -----------------------------------------------
    step(3, total_steps, f"Starting {profile} stack")

    profiles = [profile]
    info(f"Profile: {bold(profile)}")

    env = os.environ.copy()
    env["AITHEROS_IMAGE_TAG"] = tag
    env["AITHEROS_REGISTRY"] = REGISTRY

    # Pull images (longer timeout for many images)
    info("Pulling images (this may take several minutes on first run)...")
    rc = _docker_compose(compose_file, profiles, "pull", dry_run, timeout=900)
    if rc != 0:
        err("Failed to pull images")
        return 1

    rc = _docker_compose(compose_file, profiles, "up -d", dry_run, timeout=300)
    if rc != 0:
        err("Failed to start containers")
        return 1

    # -- Step 4: Health checks ------------------------------------------------
    step(4, total_steps, "Verifying key services")

    check_ports = [
        (8001, "Genesis"),
        (8080, "awnode"),
        (3000, "AitherVeil"),
    ]

    if dry_run:
        for port, name in check_ports:
            info(f"Would check: http://localhost:{port}/health ({name})")
    else:
        for port, name in check_ports:
            if _health_check(f"http://localhost:{port}/health", timeout=180):
                info(f"{name} (:{port}): {green('healthy')}")
            else:
                warn(f"{name} (:{port}): not responding yet -- may still be starting")

        # Show running container count
        ps_out = _run(["docker", "compose", "-f", str(compose_file),
                        "--profile", profile, "ps", "--format", "json"])
        if ps_out:
            try:
                # docker compose ps --format json outputs one JSON object per line
                running = 0
                for line in ps_out.strip().split("\n"):
                    if line.strip():
                        obj = json.loads(line)
                        if obj.get("State") == "running":
                            running += 1
                info(f"Running containers: {bold(str(running))}")
            except (json.JSONDecodeError, KeyError):
                pass

    # -- Summary --------------------------------------------------------------
    print()
    print(bold("  " + "-" * 60))
    print()
    info(f"Genesis:    {cyan('http://localhost:8001')}")
    info(f"awnode: {cyan('http://localhost:8080')}")
    info(f"Dashboard:  {cyan('http://localhost:3000')}")
    print()
    info(f"Manage:")
    info(f"  Logs:   {cyan(f'docker compose -f {compose_file} --profile {profile} logs -f')}")
    info(f"  Stop:   {cyan('aither deploy stop full')}")
    info(f"  PS:     {cyan(f'docker compose -f {compose_file} --profile {profile} ps')}")
    info(f"  Update: {cyan(f'docker compose -f {compose_file} --profile {profile} pull')}")
    print()
    if not dry_run:
        info(f"{green(bold('AitherOS full stack is ready!'))}")
    return 0


# ===========================================================================
# Component: Addons (self-hosted services via compose)
# ===========================================================================

def deploy_addons(
    addon_ids: list[str] | None = None,
    dry_run: bool = False,
    tag: str = "latest",
    api_key_arg: Optional[str] = None,
    sovereign: bool = False,
    hub_url: str = "https://api.aitherium.com",
    tenant: Optional[str] = None,
) -> int:
    """Deploy self-hosted addon services via Docker Compose.

    Generates a docker-compose.addons.yml from addon manifests, pulls
    images, starts everything with proper networking, and runs health
    checks.  Follows the same pattern as deploy_node() / deploy_full().

    Args:
        addon_ids: Addon IDs to deploy.  None = all available.
        dry_run: Show what would happen without executing.
        tag: Docker image tag (default: latest).
        api_key_arg: Explicit API key (falls back to env/config).
        sovereign: Register with federation hub after deployment.
        hub_url: Federation hub URL.
        tenant: Tenant slug for federation.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    from adk.addon_compose import generate_addon_compose, list_available_addons
    from adk.addon_manager import load_addon_manifest

    # Resolve addon list
    if not addon_ids:
        manifests = list_available_addons()
        addon_ids = [m["id"] for m in manifests]
    if not addon_ids:
        err("No addons found")
        return 1

    print()
    print(bold("  AitherOS Addon Deployment"))
    print(dim(f"  Addons: {', '.join(addon_ids)}"))
    print()

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    total_steps = 5 if sovereign else 4

    # -- Step 1: Prerequisites ------------------------------------------------
    step(1, total_steps, "Checking prerequisites")

    docker_ok, docker_msg = _check_docker()
    if docker_ok:
        info(docker_msg)
    else:
        err(docker_msg)
        print()
        print(f"  Install Docker: {cyan('https://docker.com/products/docker-desktop')}")
        return 1

    # Ensure the shared network exists
    if not dry_run:
        net_check = _run(["docker", "network", "ls", "--filter",
                          "name=aitheros_default", "--format", "{{.Name}}"])
        if not net_check:
            info("Creating aitheros_default network...")
            subprocess.run(["docker", "network", "create", "aitheros_default"],
                           capture_output=True)

    # GHCR login for private images
    has_private = any(
        (load_addon_manifest(a) or {}).get("image", "").startswith("aitherium/")
        for a in addon_ids
    )
    if has_private and not dry_run:
        api_key = _get_api_key(api_key_arg)
        if api_key and _docker_login_ghcr(api_key):
            info(f"Authenticated with {REGISTRY}")

    # Resource estimate
    total_ram = 0.0
    for aid in addon_ids:
        m = load_addon_manifest(aid) or {}
        mem_str = m.get("resources", {}).get("memory", "1Gi")
        # Parse "2Gi" -> 2.0
        try:
            total_ram += float(mem_str.replace("Gi", "").replace("Mi", ""))
        except (ValueError, AttributeError):
            total_ram += 1.0
    info(f"Estimated memory: ~{total_ram:.0f} GB")
    gpu_addons = [
        a for a in addon_ids
        if (load_addon_manifest(a) or {}).get("resources", {}).get("gpu")
    ]
    if gpu_addons:
        info(f"GPU required for: {', '.join(gpu_addons)}")
    print()

    # -- Step 2: Generate compose file ----------------------------------------
    step(2, total_steps, "Generating compose configuration")

    compose_file = AITHER_DIR / "docker-compose.addons.yml"
    try:
        generate_addon_compose(addon_ids, compose_file, tag=tag)
        info(f"Compose file: {dim(str(compose_file))}")
    except ValueError as e:
        err(str(e))
        return 1

    if dry_run:
        info("Compose contents:")
        try:
            print(compose_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # -- Step 3: Pull and start -----------------------------------------------
    step(3, total_steps, "Pulling images and starting containers")

    rc = _docker_compose(compose_file, [], "pull", dry_run, timeout=600)
    if rc != 0:
        err("Failed to pull images")
        return 1

    rc = _docker_compose(compose_file, [], "up -d", dry_run, timeout=120)
    if rc != 0:
        err("Failed to start containers")
        return 1

    # -- Step 4: Health checks ------------------------------------------------
    step(4, total_steps, "Verifying addon services")

    if dry_run:
        for aid in addon_ids:
            m = load_addon_manifest(aid) or {}
            port = m.get("default_port", 8000)
            hc_path = m.get("health_check", {}).get("path", "/health")
            info(f"Would check: http://localhost:{port}{hc_path} ({aid})")
    else:
        all_healthy = True
        for aid in addon_ids:
            m = load_addon_manifest(aid) or {}
            port = m.get("default_port", 8000)
            hc_path = m.get("health_check", {}).get("path", "/health")
            url = f"http://localhost:{port}{hc_path}"
            if _health_check(url, timeout=90):
                info(f"{aid} (:{port}): {green('healthy')}")
            else:
                warn(f"{aid} (:{port}): not responding yet")
                all_healthy = False

    # -- Step 5 (optional): Federation registration ---------------------------
    if sovereign:
        step(total_steps, total_steps, "Registering with federation hub")
        tenant_slug = tenant or os.environ.get("AITHER_TENANT") or "default"
        reg_url = f"{hub_url.rstrip('/')}/federation/register"

        if dry_run:
            info(f"Would POST to {reg_url}")
            info(f"  tenant_slug: {tenant_slug}")
            info(f"  addons: {addon_ids}")
        else:
            try:
                import json as _json
                payload = _json.dumps({
                    "tenant_slug": tenant_slug,
                    "addons": addon_ids,
                }).encode()
                req = Request(reg_url, data=payload, method="POST",
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=15) as resp:
                    result = _json.loads(resp.read())
                info(f"Registered as node {cyan(result.get('node_id', ''))}")
            except Exception as e:
                warn(f"Federation registration failed: {e}")

    # -- Summary --------------------------------------------------------------
    print()
    print(bold("  " + "-" * 60))
    print()
    for aid in addon_ids:
        m = load_addon_manifest(aid) or {}
        port = m.get("default_port", 8000)
        info(f"{m.get('name', aid):20s} {cyan(f'http://localhost:{port}')}")
    print()
    info(f"Manage:")
    info(f"  Logs:  {cyan(f'docker compose -f {compose_file} logs -f')}")
    info(f"  Stop:  {cyan(f'aither deploy stop addons')}")
    info(f"  PS:    {cyan(f'docker compose -f {compose_file} ps')}")
    print()
    if not dry_run:
        info(f"{green(bold('Addon services are ready!'))}")
    return 0


# ===========================================================================
# Component: Sovereign (self-hosted complete stack)
# ===========================================================================

COMPOSE_SOVEREIGN_URL = f"{GITHUB_RAW}/.DEPLOYMENT/compose/docker-compose.sovereign.yml"

# App manifest URLs (private repo — requires auth)
APP_MANIFEST_URL = f"{GITHUB_RAW}/AitherOS/config/app_manifests/{{app_id}}.yaml"
BRAND_YAML_URL = f"{GITHUB_RAW}/.PRODUCTS/.{{product_dir}}/.ELEMENT/brand.yaml"


def _load_app_manifest(app_id: str) -> Optional[dict]:
    """Load an app manifest from local config or GitHub.

    Returns parsed YAML dict, or None if not found.
    """
    # Try local first (dev mode / already cloned)
    local_paths = [
        Path("AitherOS/config/app_manifests") / f"{app_id}.yaml",
        AITHER_DIR / "manifests" / f"{app_id}.yaml",
    ]
    for p in local_paths:
        if p.exists():
            try:
                # Minimal YAML parsing without PyYAML dependency
                text = p.read_text(encoding="utf-8")
                return _parse_simple_yaml(text)
            except Exception:
                pass

    # Try downloading from GitHub
    url = APP_MANIFEST_URL.format(app_id=app_id)
    data = _download_bytes(url)
    if data:
        try:
            text = data.decode("utf-8")
            manifest = _parse_simple_yaml(text)
            # Cache locally
            cache_dir = AITHER_DIR / "manifests"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / f"{app_id}.yaml").write_bytes(data)
            return manifest
        except Exception:
            pass

    return None


def _parse_simple_yaml(text: str) -> dict:
    """Parse YAML using PyYAML if available, else basic key-value extraction."""
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        # Fallback: extract top-level key: value pairs
        result: dict = {}
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line and not line.startswith("-") and not line.startswith(" "):
                key, _, val = line.partition(":")
                val = val.strip().strip("'\"")
                if val:
                    result[key.strip()] = val
        return result


def _generate_app_overlay(
    app_id: str,
    manifest: dict,
    tag: str = "latest",
    tenant: str = "default",
) -> str:
    """Generate a docker-compose overlay YAML for an app-specific container.

    Returns the YAML string for the overlay file.
    """
    slug = manifest.get("slug", app_id)
    image = manifest.get("image", f"ghcr.io/aitherium/{slug}:{tag}")
    # Ensure tag is applied to image
    if ":" not in image.split("/")[-1]:
        image = f"{image}:{tag}"
    port = manifest.get("port", 8900)
    hc_path = "/api/health"
    hc = manifest.get("health_check", {})
    if isinstance(hc, dict):
        hc_path = hc.get("path", "/api/health")

    # Resource limits
    resources = manifest.get("resources", {})
    if not isinstance(resources, dict):
        resources = {}
    mem_limit = resources.get("memory", "2Gi")
    cpu_limit = resources.get("cpu", "1")

    # Build environment section
    prefix = slug.upper().replace("-", "_")
    env_lines = [
        '      AITHER_STANDALONE: "true"',
        '      AITHER_DEPLOYMENT_MODE: "sovereign"',
        '      AITHER_DOCKER_MODE: "true"',
        f'      AITHER_TENANT: "{tenant}"',
    ]

    # Add env_defaults from addon manifest patterns
    env_defaults = manifest.get("env_defaults", {})
    if isinstance(env_defaults, dict):
        for k, v in env_defaults.items():
            env_lines.append(f'      {k}: "{v}"')

    # Standard wiring — connect to sovereign stack services
    env_lines.extend([
        f'      {prefix}_LLM_PROVIDER: "adk"',
        f'      {prefix}_AITHEROS_URL: "http://microscheduler:8150"',
        f'      {prefix}_ADK_URL: "http://adk-server:8080"',
        f'      {prefix}_QDRANT_URL: "http://qdrant:6333"',
        f'      {prefix}_QDRANT_COLLECTION: "{slug}_{tenant}"',
        f'      OLLAMA_URL: "http://ollama:11434"',
        f'      EMBEDDING_PROVIDER: "ollama"',
        f'      EMBEDDING_URL: "http://ollama:11434"',
        f'      EMBEDDING_MODEL: "nomic-embed-text"',
    ])

    env_block = "\n".join(env_lines)
    app_name = manifest.get("name", slug)

    # Resolve local paths for bind mounts (brain pack + brand YAML)
    packs_dir = str(AITHER_DIR / "packs").replace("\\", "/")
    brand_dir = str(AITHER_DIR / "brand").replace("\\", "/")

    lines = [
        f"# Auto-generated app overlay for: {app_name}",
        f"# App: {slug} (port {port})",
        "services:",
        f"  {slug}:",
        f"    image: {image}",
        f"    container_name: sovereign-{slug}",
        "    restart: unless-stopped",
        "    ports:",
        f'      - "{port}:{port}"',
        "    environment:",
        env_block,
        "    volumes:",
        f"      - {slug}-data:/app/data",
        "    depends_on:",
        "      adk-server:",
        "        condition: service_healthy",
        "      qdrant:",
        "        condition: service_healthy",
        "    healthcheck:",
        f'      test: ["CMD", "curl", "-sf", "http://localhost:{port}{hc_path}"]',
        "      interval: 30s",
        "      timeout: 5s",
        "      start_period: 20s",
        "      retries: 3",
        "    deploy:",
        "      resources:",
        "        limits:",
        f"          memory: {mem_limit}",
        f"          cpus: '{cpu_limit}'",
        "    logging:",
        "      driver: json-file",
        "      options:",
        '        max-size: "10m"',
        '        max-file: "3"',
        "    networks:",
        "      - default",
        "",
        "  # Override workspace-app to inject brain pack + brand for this app",
        "  workspace-app:",
        "    environment:",
        f'      APP_ID: "{slug}"',
        f'      AGENT_BRAIN_PACK: "/app/packs/{slug}.yaml"',
        "    volumes:",
        "      - workspace-data:/app/data",
        f"      - {packs_dir}:/app/packs:ro",
        f"      - {brand_dir}:/app/brand:ro",
        "",
        "volumes:",
        f"  {slug}-data:",
        "  workspace-data:",
        "    external: true",
        "",
        "networks:",
        "  default:",
        "    name: sovereign-network",
        "    external: true",
    ]
    return "\n".join(lines) + "\n"


def _generate_sovereign_config(
    tenant: str,
    admin_email: str,
    app_slug: Optional[str] = None,
    gpu: bool = False,
    sync: bool = False,
    tunnel: bool = False,
    backup: bool = False,
    monitoring: bool = False,
) -> str:
    """Generate sovereign-config.yaml content with sensible defaults.

    This is the non-interactive counterpart to setup-sovereign.py's wizard.
    Users who want full customization run setup-sovereign.py separately.
    """
    tls_mode = "tunnel" if tunnel else "none"
    llm_backend = "vllm" if gpu else "ollama"
    federation = "true" if sync else "false"

    lines = [
        "# AitherOS Sovereign Configuration",
        "# Generated by: aither deploy sovereign",
        "# For full customization, run: python AitherOS/scripts/setup-sovereign.py",
        "",
        "tenant:",
        f"  slug: {tenant}",
        f"  name: {tenant.replace('-', ' ').title()}",
        f"  admin_email: {admin_email or 'admin@localhost'}",
        "",
        "network:",
        "  host: localhost",
        "  port: 3000",
        f"  tls:",
        f"    mode: {tls_mode}",
        "",
        "identity:",
        "  mode: standalone",
        "  provider: local",
        "",
        "email:",
        "  provider: none",
        "",
        "branding:",
        f"  app_name: {(app_slug or tenant).replace('-', ' ').title()}",
        "  theme: dark",
        "",
        "federation:",
        f"  enabled: {federation}",
        f"  platform_url: https://api.aitherium.com",
        "",
        "services:",
        "  preset: minimal",
        "  adk: true",
        "  workspace: true",
        "  identity: true",
        "  secrets: true",
        "  directory: true",
        "  spirit: true",
        "  graph: true",
        "  mail: true",
        "  microscheduler: true",
        f"  vllm: {str(gpu).lower()}",
        f"  sync: {str(sync).lower()}",
        f"  recover: {str(backup).lower()}",
        f"  monitoring: {str(monitoring).lower()}",
        "",
        "llm:",
        f"  backend: {llm_backend}",
        f"  model: {'aither-orchestrator' if gpu else 'gemma4:4b'}",
        "  ollama_url: http://ollama:11434",
        f"  vllm_url: {'http://vllm:8120' if gpu else ''}",
        "",
    ]
    return "\n".join(lines)


def deploy_sovereign(
    dry_run: bool = False,
    tag: str = "latest",
    app_template: Optional[str] = None,
    gpu: bool = False,
    sync: bool = True,
    no_memory: bool = False,
    api_key_arg: Optional[str] = None,
    tenant: Optional[str] = None,
    admin_email: str = "",
    tunnel: bool = False,
    backup: bool = False,
    monitoring: bool = False,
) -> int:
    """Deploy a complete sovereign AitherOS stack.

    This is the turnkey self-hosted deployment: workspace UI, agents, memory,
    identity, secrets, directory, mail, graph — everything a customer needs
    to run AitherOS on their own hardware.

    Resource estimates:
        Base:       ~4 GB RAM,  ~8 GB disk (images)
        + GPU:      +1 GB RAM,  +2 GB disk
        + Memory:   included by default (~512 MB)

    Args:
        dry_run: Show what would happen without executing.
        tag: Docker image tag (default: latest).
        app_template: Workspace app template (e.g., 'demo').
        app_template: Workspace app template (e.g., 'assistant').
        gpu: Enable GPU-accelerated vLLM backend.
        sync: Enable platform sync daemon (updates, telemetry, license).
        no_memory: Skip memory stack (Spirit, Graph) for cheaper deployments.
        api_key_arg: Explicit API key (falls back to env/config).
        tenant: Tenant slug.
        admin_email: Admin email for initial setup.
        tunnel: Enable Cloudflare Tunnel for TLS remote access.
        backup: Enable AitherRecover backup/restore service.
        monitoring: Enable Prometheus + Grafana monitoring dashboard.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    print()
    print(bold("  AitherOS Sovereign Deployment"))
    print(dim("  Complete self-hosted stack: workspace + agents + memory + identity"))
    print()

    # Auth gate
    api_key = _require_auth(api_key_arg)
    if not api_key:
        return 1

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    total_steps = 6

    # -- Step 1: Prerequisites ------------------------------------------------
    step(1, total_steps, "Checking prerequisites")

    docker_ok, docker_msg = _check_docker()
    if docker_ok:
        info(docker_msg)
    else:
        err(docker_msg)
        print()
        print(f"  Install Docker: {cyan('https://docker.com/products/docker-desktop')}")
        return 1

    if not dry_run:
        if _docker_login_ghcr(api_key):
            info(f"Authenticated with {REGISTRY}")
        else:
            warn("GHCR login failed -- image pulls may fail if images are private")

    # Entitlement check — verify API key is authorized for this tenant + app
    ent = _check_entitlements(api_key, app_id=app_template, tenant=tenant)
    if not ent["valid"]:
        err(f"Authorization failed: {ent['error']}")
        return 1
    if ent["offline"]:
        warn("Gateway unreachable -- deploying in offline mode")
        warn("License will be validated at service startup")
    else:
        info(f"License: {bold(ent['tier'])} (tenant: {ent['tenant_id']})")

    # Sovereign requires self_host tier or above
    _SOVEREIGN_TIERS = {"self_host", "managed", "founding", "internal", "enterprise"}
    current_tier = ent.get("tier", "unknown")
    if not ent["offline"] and current_tier not in _SOVEREIGN_TIERS:
        err(f"Sovereign deployment requires a Self-Host or Enterprise plan (current: '{current_tier}')")
        print()
        print(f"  Upgrade at: {cyan('https://api.aitherium.com/billing')}")
        print(f"  Or contact: {cyan('sales@aitherium.com')} for enterprise pricing")
        print()
        return 1

    # Resource estimate
    profiles = []
    if gpu:
        profiles.append("gpu")
    if sync:
        profiles.append("sync")
    if tunnel:
        tunnel_token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
        if not tunnel_token:
            warn("--tunnel requires CLOUDFLARE_TUNNEL_TOKEN environment variable")
            warn("Set it with: export CLOUDFLARE_TUNNEL_TOKEN=your_token")
            warn("Continuing without tunnel -- add it to .env.sovereign later")
        profiles.append("tunnel")
    if backup:
        profiles.append("backup")
    if monitoring:
        profiles.append("monitoring")

    # -- Step 1b: Resolve app manifest (if --app specified) -------------------
    app_manifest = None
    app_slug = None
    app_port = None
    if app_template:
        app_manifest = _load_app_manifest(app_template)
        if app_manifest:
            app_slug = app_manifest.get("slug", app_template)
            app_port = int(app_manifest.get("port", 8900))
            info(f"App: {bold(app_manifest.get('name', app_slug))}")
            info(f"  Image: {app_manifest.get('image', 'N/A')}")
            info(f"  Port: {app_port}")

            # Client-side plan check (safety net for offline deploys)
            required_plan = app_manifest.get("requires_plan", "free")
            current_tier = ent.get("tier", "unknown")
            _PLAN_HIERARCHY = ["free", "community", "developer", "professional", "enterprise",
                               "self_host", "managed", "founding", "internal"]
            if (required_plan in _PLAN_HIERARCHY
                    and current_tier in _PLAN_HIERARCHY
                    and _PLAN_HIERARCHY.index(current_tier) < _PLAN_HIERARCHY.index(required_plan)):
                err(f"App '{app_slug}' requires '{required_plan}' plan (current: '{current_tier}')")
                print()
                print(f"  Upgrade at: {cyan('https://api.aitherium.com/billing')}")
                print()
                return 1

            info(f"  Plan: {required_plan} (authorized)")
        else:
            warn(f"App manifest not found for '{app_template}' -- deploying generic stack")

    ram_gb = 4.5 + (1.0 if gpu else 0) + (2.0 if app_manifest else 0)
    disk_gb = 8.0 + (2.0 if gpu else 0) + (2.0 if app_manifest else 0)
    service_count = 11 + (1 if gpu else 0) + (1 if sync else 0) + (1 if app_manifest else 0)
    print()
    info(f"Estimated resources: ~{ram_gb:.0f} GB RAM, ~{disk_gb:.0f} GB disk")
    info(f"Services: {bold(str(service_count))} containers")
    print()

    # -- Step 2: Download compose file ----------------------------------------
    step(2, total_steps, "Downloading sovereign compose configuration")

    compose_file = AITHER_DIR / "docker-compose.sovereign.yml"
    if not _download(COMPOSE_SOVEREIGN_URL, compose_file, dry_run):
        err("Failed to download compose file")
        return 1

    # -- Step 3: Generate .env + app overlay ----------------------------------
    step(3, total_steps, "Generating environment configuration")

    tenant_slug = tenant or os.environ.get("AITHER_TENANT") or "default"
    env_file = AITHER_DIR / ".env.sovereign"

    # Auto-generate secrets if not provided
    import secrets as _secrets
    jwt_secret = os.environ.get("JWT_SECRET") or _secrets.token_urlsafe(32)
    admin_pass = os.environ.get("AITHER_ADMIN_PASSWORD") or _secrets.token_urlsafe(16)

    env_content = (
        f"# AitherOS Sovereign Configuration\n"
        f"# Generated by: aither deploy sovereign\n"
        f"#\n"
        f"AITHER_API_KEY={api_key}\n"
        f"AITHER_TENANT={tenant_slug}\n"
        f"AITHER_IDENTITY={app_slug or app_template or 'aither'}\n"
        f"AITHER_STANDALONE=true\n"
        f"AITHER_DEPLOYMENT_MODE=sovereign\n"
        f"AITHEROS_IMAGE_TAG={tag}\n"
        f"AITHEROS_REGISTRY={REGISTRY}\n"
        f"AITHER_ADMIN_EMAIL={admin_email or 'admin@localhost'}\n"
        f"AITHER_ADMIN_PASSWORD={admin_pass}\n"
        f"JWT_SECRET={jwt_secret}\n"
        f"AITHER_PLATFORM_URL=https://api.aitherium.com\n"
    )

    if gpu:
        env_content += f"VLLM_URL=http://vllm:8120\n"

    # If app manifest found, set brain pack + app ID for workspace container
    if app_manifest and app_slug:
        env_content += f"\n# App-specific configuration ({app_slug})\n"
        env_content += f"APP_ID={app_slug}\n"
        env_content += f"AGENT_BRAIN_PACK=/app/packs/{app_slug}.yaml\n"
        env_content += f"APP_PORT={app_port}\n"
        env_content += f"ADK_APP_PROXY_URL=http://{app_slug}:{app_port}\n"

        # Extract app_tools from manifest and serialize for ADK agent
        agent_section = app_manifest.get("agent", {})
        app_tools = agent_section.get("app_tools", []) if isinstance(agent_section, dict) else []
        if app_tools:
            try:
                tools_json = json.dumps(app_tools, separators=(",", ":"))
                env_content += f"ADK_APP_MANIFEST={tools_json}\n"
            except (TypeError, ValueError):
                pass

        # Download brain pack + brand YAML for the workspace container
        if not dry_run:
            packs_dir = AITHER_DIR / "packs"
            brand_dir = AITHER_DIR / "brand"
            packs_dir.mkdir(parents=True, exist_ok=True)
            brand_dir.mkdir(parents=True, exist_ok=True)
            pack_file = packs_dir / f"{app_slug}.yaml"
            brand_file = brand_dir / "brand.yaml"

            # Try local source first for brain pack
            source_dir = app_manifest.get("source_dir", "")
            up_slug = app_slug.upper()
            brain_pack_paths = [
                Path(f"{source_dir}/backend/app/packs/{app_slug}.yaml") if source_dir else None,
                Path(f".PRODUCTS/.{up_slug}/backend/app/packs/{app_slug}.yaml"),
                Path(f".PRODUCTS/.{up_slug}/backend/app/packs/{app_slug[:4]}.yaml"),
            ]
            pack_found = False
            for lp in brain_pack_paths:
                if lp and lp.exists():
                    import shutil as _sh
                    _sh.copy2(str(lp), str(pack_file))
                    info(f"Brain pack: {dim(str(pack_file))}")
                    pack_found = True
                    break
            if not pack_found:
                info(f"Brain pack will be loaded from app container at /app/packs/{app_slug}.yaml")

            # Copy brand YAML (theme, colors, logos)
            brand_paths = [
                Path(f".PRODUCTS/.{up_slug}/.ELEMENT/brand.yaml"),
                Path(f"{source_dir}/.ELEMENT/brand.yaml") if source_dir else None,
            ]
            for bp in brand_paths:
                if bp and bp.exists():
                    import shutil as _sh2
                    _sh2.copy2(str(bp), str(brand_file))
                    info(f"Brand:      {dim(str(brand_file))}")
                    break

    # Add tunnel token to env if provided
    if tunnel:
        tunnel_token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
        if tunnel_token:
            env_content += f"\n# Cloudflare Tunnel\nCLOUDFLARE_TUNNEL_TOKEN={tunnel_token}\n"

    _secret_keys = {"AITHER_API_KEY", "JWT_SECRET", "AITHER_ADMIN_PASSWORD", "CLOUDFLARE_TUNNEL_TOKEN"}
    if dry_run:
        info("Would write .env.sovereign:")
        for line in env_content.strip().split("\n"):
            if "=" in line and not line.startswith("#"):
                key = line.split("=")[0]
                if key in _secret_keys:
                    info(f"  {key}=***")
                    continue
            info(f"  {line}")
    else:
        env_file.write_text(env_content, encoding="utf-8")
        info(f"Config written to {dim(str(env_file))}")

    # Generate sovereign-config.yaml (non-interactive defaults)
    sov_config_path = AITHER_DIR / "sovereign-config.yaml"
    sov_config_content = _generate_sovereign_config(
        tenant=tenant_slug,
        admin_email=admin_email,
        app_slug=app_slug or app_template,
        gpu=gpu,
        sync=sync,
        tunnel=tunnel,
        backup=backup,
        monitoring=monitoring,
    )
    if dry_run:
        info(f"Would write sovereign-config.yaml to {sov_config_path}")
    else:
        sov_config_path.write_text(sov_config_content, encoding="utf-8")
        info(f"Sovereign config: {dim(str(sov_config_path))}")

    # Generate app overlay compose if an app manifest was resolved
    app_overlay_file = None
    if app_manifest and app_slug:
        app_overlay_file = AITHER_DIR / f"docker-compose.sovereign-{app_slug}.yml"
        overlay_content = _generate_app_overlay(
            app_slug, app_manifest, tag=tag, tenant=tenant_slug,
        )
        if dry_run:
            info(f"Would generate app overlay: {app_overlay_file}")
            info(f"  App container: {app_slug} (port {app_port})")
        else:
            app_overlay_file.write_text(overlay_content, encoding="utf-8")
            info(f"App overlay: {dim(str(app_overlay_file))}")

    # -- Step 4: Pull and start -----------------------------------------------
    step(4, total_steps, "Pulling images and starting sovereign stack")

    if profiles:
        info(f"Profiles: {', '.join(profiles)}")

    # Build compose command with optional app overlay
    compose_files = [compose_file]
    if app_overlay_file and (not dry_run and app_overlay_file.exists() or dry_run):
        compose_files.append(app_overlay_file)

    def _multi_compose(files, profs, action, dr, timeout=300):
        """Run docker compose with multiple -f flags."""
        cmd = ["docker", "compose"]
        for f in files:
            cmd += ["-f", str(f)]
        for p in profs:
            cmd += ["--profile", p]
        cmd += action.split()
        if dr:
            info(f"Would run: {' '.join(cmd)}")
            return 0
        info(f"Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, timeout=timeout)
            return result.returncode
        except subprocess.TimeoutExpired:
            warn(f"Command timed out after {timeout}s")
            return 0
        except FileNotFoundError:
            err("docker command not found")
            return 1

    rc = _multi_compose(compose_files, profiles, "pull", dry_run, timeout=900)
    if rc != 0:
        err("Failed to pull images")
        return 1

    # Set env file for compose
    env = os.environ.copy()
    env["COMPOSE_ENV_FILE"] = str(env_file) if not dry_run else ""
    env["AITHEROS_IMAGE_TAG"] = tag
    env["AITHEROS_REGISTRY"] = REGISTRY
    env["AITHER_STANDALONE"] = "true"
    env["AITHER_DEPLOYMENT_MODE"] = "sovereign"
    env["AITHER_TENANT"] = tenant_slug
    env["AITHER_API_KEY"] = api_key

    rc = _multi_compose(compose_files, profiles, "up -d", dry_run, timeout=300)
    if rc != 0:
        err("Failed to start containers")
        return 1

    # -- Step 5: Health checks ------------------------------------------------
    step(5, total_steps, "Verifying sovereign services")

    check_endpoints = [
        (8080, "ADK Server"),
        (3000, "Workspace", "/api/health"),
        (8150, "MicroScheduler"),
        (8100, "Identity"),
        (8111, "Secrets"),
        (8108, "Directory"),
        (8087, "Spirit"),
        (8135, "Graph"),
    ]
    if gpu:
        check_endpoints.append((8120, "vLLM"))
    if sync:
        check_endpoints.append((8190, "Sync"))
    if backup:
        check_endpoints.append((8163, "Recover"))
    if monitoring:
        check_endpoints.append((9090, "Prometheus", "/-/healthy"))
        check_endpoints.append((3001, "Grafana", "/api/health"))
    if app_manifest and app_port:
        hc = app_manifest.get("health_check", {})
        hc_path = hc.get("path", "/api/health") if isinstance(hc, dict) else "/api/health"
        check_endpoints.append((app_port, app_manifest.get("name", app_slug or "App"), hc_path))

    if dry_run:
        for entry in check_endpoints:
            port, name = entry[0], entry[1]
            hc_path = entry[2] if len(entry) > 2 else "/health"
            info(f"Would check: http://localhost:{port}{hc_path} ({name})")
    else:
        for entry in check_endpoints:
            port, name = entry[0], entry[1]
            hc_path = entry[2] if len(entry) > 2 else "/health"
            if _health_check(f"http://localhost:{port}{hc_path}", timeout=120):
                info(f"{name} (:{port}): {green('healthy')}")
            else:
                warn(f"{name} (:{port}): not responding yet -- may still be starting")

    # -- Step 6: Post-deploy info -------------------------------------------
    step(6, total_steps, "Deployment complete")

    print()
    print(bold("  " + "=" * 60))
    print(bold("  Sovereign AitherOS Stack"))
    print(bold("  " + "=" * 60))
    print()
    info(f"Workspace:       {cyan('http://localhost:3000')}")
    info(f"ADK Server:      {cyan('http://localhost:8080')}")
    info(f"MicroScheduler:  {cyan('http://localhost:8150')}")
    info(f"Ollama:          {cyan('http://localhost:11434')}")
    if gpu:
        info(f"vLLM:            {cyan('http://localhost:8120')}")
    info(f"Identity:        {cyan('http://localhost:8100')}")
    info(f"Secrets:         {cyan('http://localhost:8111')}")
    info(f"Directory:       {cyan('http://localhost:8108')}")
    info(f"Spirit:          {cyan('http://localhost:8087')}")
    info(f"Graph:           {cyan('http://localhost:8135')}")
    if sync:
        info(f"Sync:            {cyan('http://localhost:8190')}")
    if tunnel:
        info(f"Tunnel:          {cyan('Cloudflare Tunnel (see dashboard)')}")
    if backup:
        info(f"Recover:         {cyan('http://localhost:8163')}")
    if monitoring:
        info(f"Prometheus:      {cyan('http://localhost:9090')}")
        info(f"Grafana:         {cyan('http://localhost:3001')}")
    if app_manifest and app_port:
        app_name = app_manifest.get("name", app_slug or "App")
        info(f"{app_name:17s}{cyan(f'http://localhost:{app_port}')}")
    print()
    info(f"Tenant:      {bold(tenant_slug)}")
    if app_slug:
        info(f"App:         {bold(app_slug)}")
    info(f"Config:      {dim(str(env_file))}")
    info(f"Sov config:  {dim(str(AITHER_DIR / 'sovereign-config.yaml'))}")
    if app_overlay_file:
        info(f"App overlay: {dim(str(app_overlay_file))}")
    print()
    if not dry_run:
        print(bold("  Admin Credentials"))
        info(f"  Email:     {admin_email or 'admin@localhost'}")
        info(f"  Password:  {bold(admin_pass)}")
        warn("  Save these credentials! They won't be shown again.")
        print()
    compose_cmd = f"docker compose -f {compose_file}"
    if app_overlay_file:
        compose_cmd += f" -f {app_overlay_file}"
    info("Manage:")
    info(f"  Logs:    {cyan(f'{compose_cmd} logs -f')}")
    info(f"  Stop:    {cyan('aither deploy stop sovereign')}")
    info(f"  Status:  {cyan(f'{compose_cmd} ps')}")
    if sync:
        info(f"  Sync:    {cyan('curl http://localhost:8190/sync/status')}")
    print()
    if not dry_run:
        info(f"{green(bold('Sovereign AitherOS is ready!'))}")
    return 0


# ===========================================================================
# Component: Awconnect (browser extension)
# ===========================================================================

def deploy_connect(dry_run: bool = False, api_key_arg: Optional[str] = None) -> int:
    """Download and extract the Awconnect browser extension.

    The extension is downloaded from the latest GitHub release and extracted
    to ~/.aither/awconnect/. Requires a valid Aitherium API key.

    Args:
        dry_run: If True, show what would happen without executing.
        api_key_arg: Explicit API key (falls back to env/config).

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    print()
    print(bold("  Awconnect Browser Extension"))
    print(dim("  Chrome extension for AitherOS integration"))
    print()

    # Auth gate
    if not _require_auth(api_key_arg):
        return 1

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    total_steps = 3
    connect_url = f"{GITHUB_RELEASES}/Awconnect.zip"
    dest_dir = AITHER_DIR / "Awconnect"

    # -- Step 1: Verify release asset exists ----------------------------------
    step(1, total_steps, "Checking release asset")

    if not dry_run and not _url_exists(connect_url):
        err("Awconnect.zip not found in the latest GitHub release")
        print()
        print(f"  This usually means a release hasn't been cut yet.")
        print(f"  Check releases: {cyan('https://github.com/Aitherium/AitherOS/releases')}")
        return 1
    elif dry_run:
        info(f"Would verify: {connect_url}")

    # -- Step 2: Download + extract -------------------------------------------
    step(2, total_steps, "Downloading Awconnect")

    if dry_run:
        info(f"Would download: {connect_url}")
        info(f"Would extract to: {dest_dir}")
    else:
        data = _download_bytes(connect_url)
        if data is None:
            err("Failed to download Awconnect")
            print()
            print(f"  Check releases: {cyan('https://github.com/Aitherium/AitherOS/releases')}")
            return 1

        # Extract zip
        info(f"Extracting to {dest_dir}...")
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(BytesIO(data)) as zf:
                zf.extractall(dest_dir)
            info(f"Extracted {len(list(dest_dir.rglob('*')))} files")
        except zipfile.BadZipFile:
            err("Downloaded file is not a valid zip archive")
            return 1

        # Validate the extension has a manifest
        manifest = dest_dir / "manifest.json"
        if not manifest.exists():
            # Check one level deep (some zips wrap in a subdirectory)
            nested = list(dest_dir.glob("*/manifest.json"))
            if nested:
                manifest = nested[0]
            else:
                warn("Extension manifest.json not found — extension may not load correctly")

    # -- Step 3: Instructions -------------------------------------------------
    step(3, total_steps, "Installation instructions")

    print()
    print(f"  To install the extension in Chrome:")
    print()
    print(f"    1. Open {cyan('chrome://extensions')}")
    print(f"    2. Enable {bold('Developer mode')} (toggle in top-right)")
    print(f"    3. Click {bold('Load unpacked')}")
    print(f"    4. Select: {cyan(str(dest_dir))}")
    print()
    print(f"  For Edge: use {cyan('edge://extensions')} (same steps)")
    print()
    if not dry_run:
        info(f"{green(bold('Awconnect downloaded!'))}")
    return 0


# ===========================================================================
# Component: AitherDesktop
# ===========================================================================

def deploy_desktop(dry_run: bool = False, api_key_arg: Optional[str] = None) -> int:
    """Download the AitherDesktop native application.

    Platform availability:
        Windows: Portable .exe from GitHub releases
        Linux:   Bootc ISO image (see documentation)
        macOS:   Not yet available

    Requires a valid Aitherium API key.

    Args:
        dry_run: If True, show what would happen without executing.
        api_key_arg: Explicit API key (falls back to env/config).

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    print()
    print(bold("  AitherDesktop Native Application"))
    print()

    # Auth gate
    if not _require_auth(api_key_arg):
        return 1

    if dry_run:
        print(f"  {yellow('DRY RUN -- no changes will be made')}\n")

    plat = _platform_name()

    if plat == "windows":
        total_steps = 2
        desktop_url = f"{GITHUB_RELEASES}/AitherDesktop-win64.zip"
        dest_dir = AITHER_DIR / "AitherDesktop"

        step(1, total_steps, "Downloading AitherDesktop for Windows")

        if dry_run:
            info(f"Would download: {desktop_url}")
            info(f"Would extract to: {dest_dir}")
        else:
            data = _download_bytes(desktop_url)
            if data is None:
                err("Failed to download AitherDesktop")
                print()
                print(f"  Check releases: {cyan('https://github.com/Aitherium/AitherOS/releases')}")
                return 1

            dest_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(BytesIO(data)) as zf:
                    zf.extractall(dest_dir)
                info(f"Extracted to {dest_dir}")
            except zipfile.BadZipFile:
                err("Downloaded file is not a valid zip archive")
                return 1

        step(2, total_steps, "Installation complete")

        # Locate .exe
        exe_path = dest_dir / "AitherDesktop.exe"
        if not dry_run and not exe_path.exists():
            # Search for it in subdirectories
            exe_candidates = list(dest_dir.rglob("AitherDesktop.exe"))
            if exe_candidates:
                exe_path = exe_candidates[0]

        print()
        info(f"Executable: {cyan(str(exe_path))}")
        info(f"Run: {cyan(str(exe_path))}")
        print()
        if not dry_run:
            info(f"{green(bold('AitherDesktop downloaded!'))}")
        return 0

    elif plat == "linux":
        print(f"  AitherDesktop for Linux is available as a {bold('Bootc ISO image')}.")
        print()
        print(f"  This provides a full AitherOS-powered desktop environment")
        print(f"  built on Fedora CoreOS with atomic updates.")
        print()
        print(f"  For more information:")
        print(f"    {cyan('https://github.com/Aitherium/AitherOS/wiki/AitherDesktop-Linux')}")
        print()
        print(f"  To download the ISO:")
        print(f"    {cyan(f'{GITHUB_RELEASES}/AitherDesktop-bootc.iso')}")
        print()
        return 0

    elif plat == "macos":
        print(f"  AitherDesktop for macOS is {yellow('not yet available')}.")
        print()
        print(f"  In the meantime, you can use:")
        print(f"    - {cyan('aither deploy node --dashboard')} (Docker-based, runs in browser)")
        print(f"    - {cyan('aither deploy connect')} (Chrome extension)")
        print()
        print(f"  Follow progress: {cyan('https://github.com/Aitherium/AitherOS/issues')}")
        print()
        return 0

    else:
        err(f"Unsupported platform: {plat}")
        return 1


# ===========================================================================
# Stop / Teardown
# ===========================================================================

def deploy_stop(component: str) -> int:
    """Stop a deployed AitherOS component.

    Args:
        component: One of "ollama", "node", "core", "full", "vllm", "all".

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    print()
    print(bold(f"  Stopping: {component}"))
    print()

    if component == "ollama":
        # Try graceful stop, then kill
        info("Stopping Ollama...")
        result = _run(["ollama", "stop"])
        if result is not None:
            info("Ollama stopped")
            return 0

        # Fallback: find and kill the process
        plat = _platform_name()
        if plat == "windows":
            _run(["taskkill", "/F", "/IM", "ollama.exe"])
            # Also stop the app if running
            _run(["taskkill", "/F", "/IM", "ollama app.exe"])
        else:
            _run(["pkill", "-f", "ollama serve"])
        info("Ollama process terminated")
        return 0

    elif component in ("node", "core"):
        # Try ADK compose first, then Genesis compose
        adk_compose = AITHER_DIR / "docker-compose.node-adk.yml"
        genesis_compose = AITHER_DIR / "docker-compose.node.yml"
        rc = 1
        stopped = False
        if adk_compose.exists():
            rc = _docker_compose(adk_compose, [], "down", dry_run=False, timeout=120)
            if rc == 0:
                info(f"ADK {component} stack stopped")
                stopped = True
        if genesis_compose.exists():
            rc2 = _docker_compose(genesis_compose, [], "down", dry_run=False, timeout=120)
            if rc2 == 0:
                info(f"Genesis {component} stack stopped")
                stopped = True
            rc = rc if stopped else rc2
        if not stopped:
            warn("No compose file found")
            warn("Nothing to stop (was it deployed with 'aither deploy node/core'?)")
            return 1
        return 0 if stopped else rc

    elif component == "full":
        compose_file = AITHER_DIR / "docker-compose.aitheros.yml"
        if not compose_file.exists():
            warn(f"Compose file not found: {compose_file}")
            warn("Nothing to stop (was it deployed with 'aither deploy full'?)")
            return 1
        rc = _docker_compose(compose_file, [], "down", dry_run=False, timeout=120)
        if rc == 0:
            info("Full stack stopped")
        return rc

    elif component == "vllm":
        # Check both possible compose file locations
        compose_candidates = [
            AITHER_DIR / "docker-compose.vllm.yml",
            Path("docker-compose.vllm.yml"),
        ]
        for compose_file in compose_candidates:
            if compose_file.exists():
                rc = _docker_compose(compose_file, [], "down", dry_run=False, timeout=120)
                if rc == 0:
                    info(f"vLLM stack stopped (from {compose_file})")
                return rc
        warn("No vLLM compose file found")
        warn("Nothing to stop (was it deployed with 'aither setup' or 'aither deploy vllm'?)")
        return 1

    elif component == "all":
        info("Stopping all AitherOS components...")
        exit_code = 0

        # Stop full stack
        full_compose = AITHER_DIR / "docker-compose.aitheros.yml"
        if full_compose.exists():
            rc = _docker_compose(full_compose, [], "down", dry_run=False, timeout=120)
            if rc == 0:
                info("Full stack stopped")
            else:
                exit_code = 1

        # Stop node/core stack
        node_compose = AITHER_DIR / "docker-compose.node.yml"
        if node_compose.exists():
            rc = _docker_compose(node_compose, [], "down", dry_run=False, timeout=120)
            if rc == 0:
                info("Node stack stopped")
            else:
                exit_code = 1

        # Stop vLLM
        for vllm_path in [AITHER_DIR / "docker-compose.vllm.yml",
                          Path("docker-compose.vllm.yml")]:
            if vllm_path.exists():
                rc = _docker_compose(vllm_path, [], "down", dry_run=False, timeout=120)
                if rc == 0:
                    info(f"vLLM stopped (from {vllm_path})")
                else:
                    exit_code = 1

        # Stop Ollama
        if shutil.which("ollama"):
            deploy_stop("ollama")

        print()
        if exit_code == 0:
            info(f"{green(bold('All components stopped'))}")
        else:
            warn("Some components may not have stopped cleanly")
        return exit_code

    else:
        err(f"Unknown component: {component}")
        print()
        print(f"  Valid components: ollama, vllm, node, core, full, all")
        return 1


# ===========================================================================
# Tenant agent deployment — download + configure + start + register
# ===========================================================================

def cmd_deploy_tenant_agent(args) -> int:
    """Download a tenant agent app from portal, configure it, and start it.

    This is the customer-facing deploy flow:
        adk deploy agent myapp --tenant customer-acme
        adk deploy agent myapp --tenant customer-acme --inference cloud
        adk deploy agent myapp --from https://api.aitherium.com/api/...

    Steps:
        1. Authenticate (verify tenant credentials from ~/.aither/config.json)
        2. Download agent package from portal
        3. Configure (.env, inference mode, Ed25519 keypair)
        4. Start (docker compose or adk run)
        5. Register with portal fleet
    """
    import tempfile
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError

    agent_slug = args.name
    tenant_slug = getattr(args, "tenant", None) or ""
    from_url = getattr(args, "from_url", None) or ""
    inference_mode = getattr(args, "inference", None) or ""

    if not agent_slug:
        err("Agent name is required: adk deploy agent <name> --tenant <slug>")
        return 1

    total_steps = 5
    # ── Step 1: Authenticate ──────────────────────────────────────────────
    step(1, total_steps, "Authenticating")

    from adk.config import load_saved_config, save_saved_config
    saved = load_saved_config()
    api_key = getattr(args, "api_key", None) or saved.get("api_key", "") or os.environ.get("AITHER_API_KEY", "")
    if not tenant_slug:
        tenant_slug = saved.get("tenant_id", "") or saved.get("tenant_slug", "")

    if not api_key:
        err("No API key found. Run 'adk login' first or pass --api-key.")
        return 1
    if not tenant_slug:
        err("No tenant slug. Run 'adk login' or pass --tenant <slug>.")
        return 1

    info(f"Tenant: {bold(tenant_slug)}")
    info(f"Agent:  {bold(agent_slug)}")

    # ── Step 2: Download agent package ────────────────────────────────────
    step(2, total_steps, "Downloading agent package")

    if from_url:
        download_url = from_url
    else:
        portal_url = saved.get("portal_url", "") or os.environ.get(
            "AITHER_PORTAL_URL", "https://api.aitherium.com"
        )
        # Use Genesis bridge — portal proxies to Genesis /apps/catalog/{slug}/download
        download_url = f"{portal_url}/api/bridge/genesis/apps/catalog/{agent_slug}/download"

    deploy_dir = Path.home() / ".aither" / "agents" / agent_slug
    deploy_dir.mkdir(parents=True, exist_ok=True)

    try:
        req = Request(download_url)
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("X-Tenant-ID", tenant_slug)
        resp = urlopen(req, timeout=60)
        zip_data = resp.read()
        info(f"Downloaded {len(zip_data) / 1024:.1f} KB")
    except HTTPError as e:
        if e.code == 404:
            err(f"Agent '{agent_slug}' not found in the catalog for tenant '{tenant_slug}'.")
        elif e.code == 403:
            err("Access denied. Check your API key and plan tier.")
        else:
            err(f"Download failed: HTTP {e.code}")
        return 1
    except Exception as e:
        err(f"Download failed: {e}")
        return 1

    # Extract zip
    try:
        import zipfile as _zf
        from io import BytesIO
        with _zf.ZipFile(BytesIO(zip_data)) as zf:
            zf.extractall(deploy_dir)
        info(f"Extracted to {deploy_dir}")
    except Exception as e:
        err(f"Failed to extract package: {e}")
        return 1

    # ── Step 3: Configure ─────────────────────────────────────────────────
    step(3, total_steps, "Configuring")

    # Generate instance ID
    from adk.agent_registry import get_or_create_instance_id
    instance_id = get_or_create_instance_id(deploy_dir, agent_slug)
    info(f"Instance ID: {dim(instance_id)}")

    # Write .env from template
    env_template = deploy_dir / ".env.template"
    env_file = deploy_dir / ".env"
    if env_template.exists() and not env_file.exists():
        env_content = env_template.read_text(encoding="utf-8")
        env_content = env_content.replace("AITHER_API_KEY=", f"AITHER_API_KEY={api_key}")
        env_content = env_content.replace("AITHER_TENANT_ID=", f"AITHER_TENANT_ID={tenant_slug}")
        if inference_mode:
            env_content = env_content.replace("AITHER_CLOUD_MODE=", f"AITHER_CLOUD_MODE={inference_mode}")
        env_content += f"\nAITHER_INSTANCE_ID={instance_id}\n"
        env_file.write_text(env_content, encoding="utf-8")
        info("Generated .env")
    elif env_file.exists():
        info(".env already exists, skipping")

    # GPU detection + inference mode selection
    if not inference_mode:
        gpu = detect_gpu()
        if gpu.vendor == "nvidia" and gpu.vram_mb >= 4096:
            inference_mode = "local"
            info(f"GPU detected: {green(gpu.name)} ({gpu.vram_mb}MB) — using local inference")
        elif gpu.vendor in ("nvidia", "amd", "apple"):
            inference_mode = "hybrid"
            info(f"GPU detected: {yellow(gpu.name)} — using hybrid inference")
        else:
            inference_mode = "cloud"
            info("No GPU detected — using cloud inference (BYOK)")

    # Generate Ed25519 keypair for federation identity
    try:
        from adk.federation_lite import generate_keypair
        key_dir = deploy_dir / ".aither" / "keys"
        generate_keypair(key_dir, agent_slug)
        info("Generated Ed25519 federation keypair")
    except Exception as e:
        warn(f"Keypair generation skipped: {e}")

    # ── Step 4: Start ─────────────────────────────────────────────────────
    step(4, total_steps, "Starting agent")

    # Read port from bundle metadata (used by both Docker and native paths)
    port = 8080
    bundle_meta = deploy_dir / "bundle.json"
    if bundle_meta.exists():
        try:
            meta = json.loads(bundle_meta.read_text(encoding="utf-8"))
            port = meta.get("port", 8080)
        except Exception:
            pass

    compose_file = deploy_dir / "docker-compose.yml"
    use_docker = compose_file.exists() and shutil.which("docker")

    if use_docker:
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "up", "-d"],
                cwd=str(deploy_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            if result.returncode == 0:
                info(f"Agent started via Docker Compose")
            else:
                warn(f"Docker start returned {result.returncode}: {result.stderr[:200]}")
                info("Falling back to native ADK mode...")
                use_docker = False
        except Exception as e:
            warn(f"Docker failed: {e}")
            use_docker = False

    if not use_docker:
        info(f"Starting native ADK server on port {port}...")
        # Start in background
        adk_cmd = [sys.executable, "-m", "adk.server",
                    "--identity", agent_slug, "--port", str(port)]
        env = os.environ.copy()
        env["AITHER_API_KEY"] = api_key
        env["AITHER_TENANT_ID"] = tenant_slug
        env["AITHER_INSTANCE_ID"] = instance_id
        if inference_mode:
            env["AITHER_CLOUD_MODE"] = inference_mode
        try:
            proc = subprocess.Popen(
                adk_cmd, cwd=str(deploy_dir), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            info(f"Agent started (PID {proc.pid})")
        except Exception as e:
            err(f"Failed to start agent: {e}")
            return 1

    # ── Step 5: Register with portal ──────────────────────────────────────
    step(5, total_steps, "Registering with portal fleet")

    from adk.agent_registry import register_local_agent
    register_local_agent(
        agent_slug,
        port=port,
        instance_id=instance_id,
        metadata={"tenant_id": tenant_slug, "inference_mode": inference_mode},
    )

    try:
        portal_url = saved.get("portal_url", "") or os.environ.get(
            "AITHER_PORTAL_URL", "https://api.aitherium.com"
        )
        invoke_url = os.environ.get("AITHER_INVOKE_URL", f"http://localhost:{port}")
        import urllib.request
        reg_data = json.dumps({
            "name": agent_slug,
            "scope": {"visibility": "workspace", "tenant_id": tenant_slug},
            "invoke_url": invoke_url,
            "instance_id": instance_id,
            "inference_mode": inference_mode,
            "status": "online",
        }).encode()
        reg_req = Request(
            f"{portal_url}/v1/agents/upsert",
            data=reg_data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        urlopen(reg_req, timeout=15)
        info("Registered with portal fleet")
    except Exception as e:
        warn(f"Portal registration deferred: {e}")
        info("Agent will register on next heartbeat")

    # ── Done ──────────────────────────────────────────────────────────────
    print()
    info(f"{green('Agent deployed successfully!')}")
    print()
    print(f"  Agent:     {bold(agent_slug)}")
    print(f"  Tenant:    {tenant_slug}")
    print(f"  Instance:  {dim(instance_id)}")
    print(f"  Inference: {inference_mode}")
    print(f"  Location:  {deploy_dir}")
    print()
    print(f"  Check status:  {cyan('adk status')}")
    print(f"  View logs:     {cyan(f'docker compose -f {compose_file} logs -f') if use_docker else cyan('adk run --identity ' + agent_slug)}")
    print(f"  Fleet UI:      {cyan('https://api.aitherium.com/portal/fleet')}")
    print()
    return 0


# ===========================================================================
# (removed 2026-08-17) A per-customer deploy shortcut used to live here.
#
# It named a CUSTOMER in a package strangers `pip install`, which is not our
# secret to spend: no secret scanner fires on a company name, and `pip download`
# turns a handful of them into a client list.
#
# Removing rather than renaming, because renaming would have preserved something
# that could never work. It was a thin wrapper around a monorepo script, and both
# paths it searched — `AitherOS/scripts/…` relative to cwd, and `../../../AitherOS/
# scripts/…` relative to this package — exist only inside this repo. For every
# public user it printed "run from repo root" of a repo they do not have and
# returned 1. A neutral name would have shipped the same dead end with the
# evidence filed off.
#
# It was also the only customer-shaped entry in a family of generic platform
# capabilities (ollama, vllm, node, core, sovereign, grid, addons, …). Nothing
# internal is lost: that product's setup script still lives under
# `AitherOS/scripts/` and can be run directly, which is where product-specific
# deployment belongs — in the repo, not in the published SDK.
#
# Naming the customer HERE would have re-leaked exactly what this removal is
# for; a comment ships in the wheel like any other line.
# ===========================================================================

# Tenant app deployment
# ===========================================================================

def deploy_tenant_app(
    app: str,
    dry_run: bool = False,
    tier: Optional[str] = None,
    no_pull: bool = False,
    start: bool = True,
    api_key_arg: Optional[str] = None,
) -> int:
    """Deploy a tenant's sovereign package — single command for the full flow.

    Wraps the app's setup wizard: hardware detect -> tier select -> .env
    generation -> docker compose up -> health check -> print access info.

    `app` names the wizard (scripts/setup-<app>.py). It is a PARAMETER rather
    than a hardcoded name for two reasons. It generalises to any tenant, and the
    previous form embedded a specific customer's name in a package that is
    published to PyPI -- no secret scanner fires on a customer name, but
    `pip download` turns one into a client list.

    Note this path only works from a monorepo checkout: the wizard it invokes is
    not part of the published wheel.
    """
    import shutil

    print()
    print(bold(f"  {app} Sovereign Deployment"))
    print()

    # Locate the setup script (works from repo root or adk package)
    rel = Path("AitherOS") / "scripts" / f"setup-{app}.py"
    setup_script = None
    for candidate in [Path(*rel.parts), Path(__file__).parent.parent.parent / rel]:
        if candidate.exists():
            setup_script = candidate
            break

    if not setup_script:
        warn(f"Cannot find {rel.as_posix()} — run from a repo checkout")
        return 1

    # Build the command
    cmd_parts = [sys.executable, str(setup_script)]
    if tier:
        cmd_parts += ["--tier", tier]
    if no_pull:
        cmd_parts.append("--no-pull")
    if start:
        cmd_parts.append("--start")

    if dry_run:
        print(f"  [dry-run] Would run: {' '.join(cmd_parts)}")
        return 0

    # Inject API key into env if provided
    env = dict(os.environ)
    if api_key_arg:
        env["AITHER_API_KEY"] = api_key_arg

    # Run the setup wizard (it's interactive, so use subprocess with inherited stdio)
    result = subprocess.run(cmd_parts, env=env)
    return result.returncode


# ===========================================================================
# Component: Grid Distributed
# ===========================================================================


def _discover_llamacpp_lan(timeout: float = 1.0) -> str:
    """Scan LAN for a llama.cpp instance with OpenAI API enabled.

    Probes the local /24 subnet for llama.cpp on port 8121 (default grid port).
    Returns the first responding IP or empty string.
    """
    import concurrent.futures
    import socket

    scan_port = int(os.environ.get("LLAMACPP_PORT", "8121"))

    def _probe(ip: str) -> str | None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            if sock.connect_ex((ip, scan_port)) == 0:
                sock.close()
                # Verify it has OpenAI-compatible API (--api-oai)
                try:
                    req = Request(
                        f"http://{ip}:{scan_port}/v1/models",
                        headers={"User-Agent": "AitherADK/1.0"},
                    )
                    with urlopen(req, timeout=2) as resp:
                        if resp.status == 200:
                            return ip
                except Exception:
                    pass
            else:
                sock.close()
        except Exception:
            pass
        return None

    # Get local IP to determine subnet
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return ""

    # Build scan list: common LAN IPs on the same /24 subnet, excluding self
    prefix = ".".join(local_ip.split(".")[:3])
    targets = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != local_ip]

    info(f"Scanning LAN ({prefix}.0/24) for llama.cpp nodes on port {scan_port}...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(_probe, ip): ip for ip in targets}
        for future in concurrent.futures.as_completed(futures, timeout=timeout + 3):
            try:
                result = future.result()
                if result:
                    return result
            except Exception:
                pass
    return ""


def deploy_grid(
    dry_run: bool = False,
    mac_host: str = "",
    cluster_nodes: str = "",
    hf_token: str = "",
    skip_health: bool = False,
) -> int:
    """Deploy grid distributed inference stack.

    Main PC gets vLLM orchestrator (Nemotron-8B TQ4). Mac Mini and cluster
    nodes are validated but set up separately via scripts.

    Returns exit code (0 = success, 1 = failure).
    """
    print()
    print(bold("  AitherADK Grid Distributed Deployment"))
    print(dim("  GPU orchestrator + Mac reasoning + CPU cluster"))
    print()

    mac_host = mac_host or os.environ.get("AITHER_GRID_MAC_HOST", "")
    cluster_nodes = cluster_nodes or os.environ.get("AITHER_GRID_CLUSTER_NODES", "")

    total_steps = 5

    # -- Step 1: Prerequisites --------------------------------------------------
    step(1, total_steps, "Checking prerequisites")

    docker_ok, docker_msg = _check_docker()
    if docker_ok:
        info(docker_msg)
    else:
        err(docker_msg)
        print(f"\n  Install Docker: {cyan('https://docker.com/products/docker-desktop')}")
        return 1

    gpu = detect_gpu()
    if gpu.vendor != "nvidia":
        err(f"NVIDIA GPU required for vLLM orchestrator (detected: {gpu.vendor or 'none'})")
        print(f"\n  For non-NVIDIA setups, use: {cyan('adk setup --tier llamacpp')}")
        return 1

    vram_gb = gpu.vram_mb / 1024 if gpu.vram_mb else 0
    if vram_gb < 6:
        err(f"Need at least 6GB VRAM for TQ4 orchestrator (detected: {vram_gb:.0f}GB)")
        return 1

    info(f"GPU: {gpu.name} ({vram_gb:.0f}GB VRAM)")

    # -- Auto-discover Mac Ollama on LAN if --mac-host not set ------------------
    # Auto-discover llama.cpp nodes on LAN if --mac-host not set
    if not mac_host and not dry_run:
        discovered = _discover_llamacpp_lan()
        if discovered:
            mac_host = discovered
            info(f"Auto-discovered llama.cpp at {mac_host}")

    # -- Step 2: Validate remote nodes ------------------------------------------
    step(2, total_steps, "Checking remote nodes")

    mac_port = os.environ.get("AITHER_GRID_MAC_PORT", "8121")
    mac_ok = False
    if mac_host:
        info(f"Mac reasoning node: {mac_host}:{mac_port}")
        if not dry_run and not skip_health:
            try:
                req = Request(
                    f"http://{mac_host}:{mac_port}/health",
                    headers={"User-Agent": "AitherADK/1.0"},
                )
                with urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        info(f"Mac llama.cpp healthy")
                # Verify OpenAI API is available
                oai_req = Request(
                    f"http://{mac_host}:{mac_port}/v1/models",
                    headers={"User-Agent": "AitherADK/1.0"},
                )
                with urlopen(oai_req, timeout=5) as resp:
                    if resp.status == 200:
                        info(f"Mac OpenAI API available")
                        mac_ok = True
            except Exception:
                warn(f"Mac node at {mac_host}:{mac_port} unreachable")
                print(f"    Run on Mac: {cyan('bash scripts/setup-mac-node.sh')}")
        else:
            info("(skipping health check)")
    else:
        warn("No Mac reasoning node configured")
        print(f"    Set: {cyan('export AITHER_GRID_MAC_HOST=192.168.1.100')}")
        print(f"    Setup: {cyan('bash scripts/setup-mac-node.sh')} (on Mac)")

    cluster_ok = False
    if cluster_nodes and cluster_nodes != "[]":
        try:
            nodes = json.loads(cluster_nodes) if isinstance(cluster_nodes, str) else cluster_nodes
        except json.JSONDecodeError:
            nodes = [cluster_nodes]

        info(f"Cluster nodes: {', '.join(nodes)}")
        if not dry_run and not skip_health:
            for node_ip in nodes:
                port = os.environ.get("LLAMACPP_PORT", "8121")
                try:
                    req = Request(
                        f"http://{node_ip}:{port}/health",
                        headers={"User-Agent": "AitherADK/1.0"},
                    )
                    with urlopen(req, timeout=5) as resp:
                        if resp.status == 200:
                            info(f"  {node_ip}:{port} healthy")
                except Exception:
                    warn(f"  {node_ip}:{port} unreachable")
                    continue
                # Verify OpenAI-compatible API is enabled (--api-oai flag)
                try:
                    oai_req = Request(
                        f"http://{node_ip}:{port}/v1/models",
                        headers={"User-Agent": "AitherADK/1.0"},
                    )
                    with urlopen(oai_req, timeout=5) as resp:
                        if resp.status == 200:
                            info(f"  {node_ip}:{port} OpenAI API available")
                            cluster_ok = True
                        else:
                            warn(f"  {node_ip}:{port} missing --api-oai flag (no /v1 endpoint)")
                            print(f"    Re-run: {cyan('bash scripts/setup-cluster-node.sh')} on that node")
                except Exception:
                    warn(f"  {node_ip}:{port} missing --api-oai flag (no /v1 endpoint)")
                    print(f"    Re-run: {cyan('bash scripts/setup-cluster-node.sh')} on that node")
        else:
            info("(skipping health check)")
    else:
        warn("No cluster nodes configured (optional)")
        print(f"    Set: {cyan('export AITHER_GRID_CLUSTER_NODES=')}'[\"192.168.1.10\"]'")
        print(f"    Setup: {cyan('bash scripts/setup-cluster-node.sh')} (on each node)")

    # -- Step 3: Locate compose file --------------------------------------------
    step(3, total_steps, "Locating compose configuration")

    # Check for compose file in adk package directory or current dir
    compose_candidates = [
        Path(__file__).parent.parent / "docker-compose.grid.yml",
        Path("awdk") / "docker-compose.grid.yml",
        Path("docker-compose.grid.yml"),
        AITHER_DIR / "docker-compose.grid.yml",
    ]

    compose_file = None
    for candidate in compose_candidates:
        if candidate.exists():
            compose_file = candidate
            break

    if not compose_file:
        err("docker-compose.grid.yml not found")
        print(f"    Expected at: {compose_candidates[0]}")
        return 1

    info(f"Using {compose_file}")

    # -- Step 4: Start containers -----------------------------------------------
    step(4, total_steps, "Starting vLLM orchestrator")

    env_overrides = {}
    if hf_token:
        env_overrides["HUGGING_FACE_HUB_TOKEN"] = hf_token
    if mac_host:
        env_overrides["AITHER_GRID_MAC_HOST"] = mac_host
    if cluster_nodes:
        env_overrides["AITHER_GRID_CLUSTER_NODES"] = cluster_nodes

    # Set env vars for docker compose
    old_env = {}
    for k, v in env_overrides.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    try:
        rc = _docker_compose(compose_file, [], "up -d", dry_run, timeout=600)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    if rc != 0:
        err("Failed to start containers")
        return 1

    # -- Step 5: Health checks + summary ----------------------------------------
    step(5, total_steps, "Verifying deployment")

    vllm_ok = False
    if not dry_run and not skip_health:
        info("Waiting for vLLM orchestrator (may take 5-10 min on first run)...")
        vllm_ok = _health_check("http://localhost:8120/health", timeout=600)
        if vllm_ok:
            info("vLLM orchestrator is healthy")
        else:
            warn("vLLM orchestrator not responding yet — may still be downloading model")
            print(f"    Check: {cyan('docker logs -f adk-vllm-orchestrator')}")
    else:
        info("(skipping health check)")

    # Summary
    print()
    print(bold("  Topology Summary"))
    print("  " + "=" * 50)
    vllm_icon = green("+") if vllm_ok or dry_run else yellow("?")
    mac_icon = green("+") if mac_ok else (yellow("?") if mac_host else red("-"))
    cluster_icon = green("+") if cluster_ok else (yellow("?") if cluster_nodes else red("-"))

    mac_display = f"{mac_host}:{mac_port}" if mac_host else "<not set>"
    print(f"  [{vllm_icon}] GPU Orchestrator  localhost:8120   Nemotron-8B TQ4")
    print(f"  [{mac_icon}] Mac Reasoning     {mac_display:18s} DeepSeek-R1 8B")
    print(f"  [{cluster_icon}] CPU Cluster       {cluster_nodes or '<not set>':18s} Qwen2.5-32B Q4")
    print()
    print(dim("  All remote nodes use llama.cpp with --api-oai (OpenAI-compatible)"))

    print()
    print(bold("  Effort Routing"))
    print("  " + "-" * 50)
    print("    1-6:  Nemotron-8B TQ4 (local GPU, 15-25 tok/s)")
    print("    7-8:  DeepSeek-R1 8B  (Mac llama.cpp, 8-15 tok/s)")
    print("    9-10: Qwen2.5-32B Q4  (CPU cluster, 5-10 tok/s)")

    if not mac_host or not mac_ok:
        print()
        print(bold("  Next Steps"))
        print("  " + "-" * 50)
        if not mac_host:
            print(f"  1. Set up Mac: {cyan('bash scripts/setup-mac-node.sh')} (on Mac Mini)")
            print(f"     Then:       {cyan('export AITHER_GRID_MAC_HOST=<mac-ip>')}")
        if not cluster_nodes or cluster_nodes == "[]":
            print(f"  2. Set up cluster: {cyan('bash scripts/setup-cluster-node.sh')} (on each node)")
            # A backslash inside a replacement field is PEP 701 (3.12+) and
            # awdk declares requires-python ">=3.10", where this module was a
            # SyntaxError -- so it failed to import for anyone pip-installing
            # it on the oldest Python we promise to support.
            _grid = 'export AITHER_GRID_CLUSTER_NODES=["<ip>"]'
            print(f"     Then:           {cyan(_grid)}")

    # -- Save routing config for adk shell / adk-serve ----------------------------
    from adk.config import save_saved_config

    grid_config: dict = {
        "profile": "grid_distributed",
        "backend": "vllm",
        "base_url": "http://localhost:8120/v1",
        "model": "aither-orchestrator",
    }
    # Structured node registry for `adk grid status/add/remove`
    grid_nodes: dict = {}

    if mac_host:
        grid_config["reasoning_backend"] = "openai"
        grid_config["reasoning_url"] = f"http://{mac_host}:{mac_port}/v1"
        grid_config["reasoning_model"] = "deepseek-r1-8b"
        grid_nodes["reasoning"] = {"host": mac_host, "port": int(mac_port)}

    if cluster_nodes and cluster_nodes != "[]":
        nodes = json.loads(cluster_nodes) if isinstance(cluster_nodes, str) else cluster_nodes
        if nodes:
            llamacpp_port = int(os.environ.get("LLAMACPP_PORT", "8121"))
            grid_config["cluster_backend"] = "openai"
            grid_config["cluster_url"] = f"http://{nodes[0]}:{llamacpp_port}/v1"
            grid_config["cluster_model"] = "qwen2.5-32b"
            grid_nodes["cluster"] = [{"host": n, "port": llamacpp_port} for n in nodes]

    if grid_nodes:
        grid_config["grid_nodes"] = grid_nodes

    cfg_path = save_saved_config(grid_config)
    info(f"Config saved to {cfg_path}")

    print()
    print(bold("  Start Chatting"))
    print("  " + "-" * 50)
    print(f"    {cyan('adk shell')}                          # Interactive terminal")
    print(f"    {cyan('adk-serve --port 8080')}              # HTTP API (OpenAI-compatible)")
    print()
    print(bold("  Manage Your Grid"))
    print("  " + "-" * 50)
    print(f"    {cyan('adk grid status')}                    # Show topology + health")
    print(f"    {cyan('adk grid add reasoning <ip>')}        # Add Mac node later")
    print(f"    {cyan('adk grid add cluster <ip>')}          # Add CPU node later")
    # Show account nudge only if not logged in
    from adk.config import load_saved_config as _load_cfg
    _saved = _load_cfg()
    if _saved.get("api_key"):
        print(f"    {cyan('adk grid sync')}                      # Push config to workspace")
    else:
        print()
        print(bold("  Optional: Cloud Sync"))
        print("  " + "-" * 50)
        print(f"    {cyan('adk login')}                          # Free account — sync config across machines")
        print(f"    {cyan('adk grid sync')}                      # Backup your grid config to the cloud")
        print(f"    {dim('Upgrade: https://api.aitherium.com/marketplace/grid')}")

    print()
    return 0


# ===========================================================================
# CLI entry point
# ===========================================================================

def deploy_fleet_refresh(args) -> int:
    """`aither/adk deploy fleet-refresh` — rebuild all lib-baking Python service
    images from current source and safe rolling-recreate the running fleet onto
    them. Thin wrapper over the canonical repo script (run from inside a checkout).
    """
    cwd = Path.cwd()
    script = None
    for d in (cwd, *cwd.parents):
        candidate = d / ".DEPLOYMENT" / "scripts" / "fleet-refresh.sh"
        if candidate.exists():
            script = candidate
            break
    if script is None:
        print("ERROR: .DEPLOYMENT/scripts/fleet-refresh.sh not found — run from inside an AitherOS repo.")
        return 1
    flags = []
    for name in ("build_only", "recreate_only", "dry_run"):
        if getattr(args, name, False):
            flags.append("--" + name.replace("_", "-"))
    print(f">>> deploy fleet-refresh -> {script} {' '.join(flags)}")
    import subprocess
    return subprocess.call(["bash", str(script), *flags], cwd=str(script.parents[2]))


def cmd_deploy_component(args) -> int:
    """Main entry point for `aither deploy <component>`.

    Called from cli.py when the deploy-stack or component deploy command
    is invoked. This dispatches to the appropriate deploy_* function.
    """
    component = getattr(args, "component", None)
    dry_run = getattr(args, "dry_run", False)

    if not component:
        print()
        print(bold("  AitherOS Component Deployment"))
        print()
        print(f"  Usage: {cyan('aither deploy <component> [options]')}")
        print()
        print(f"  Components:")
        print(f"    {bold('sovereign')}  Complete self-hosted stack (workspace + agents + memory + auth)")
        print(f"    {bold('ollama')}     Install Ollama + pull models for your GPU")
        print(f"    {bold('vllm')}       Deploy vLLM inference containers (NVIDIA GPU)")
        print(f"    {bold('node')}       ADK-native node (lightweight: ADK server + Ollama)")
        print(f"    {bold('node --genesis')}  Full node (Genesis + Redis + PostgreSQL + 14 services)")
        print(f"    {bold('core')}       Core services (Node, Pulse, Watch, Genesis, Veil)")
        print(f"    {bold('full')}       Full AitherOS stack (~31 containers)")
        print(f"    {bold('fleet-refresh')} Rebuild all lib-baking Python images + safe rolling recreate")
        print(f"    {bold('addons')}     Self-hosted addon services (Qdrant, RAG, etc.)")
        print(f"    {bold('grid')}    Grid distributed (GPU + Mac + cluster)")
        print(f"    {bold('connect')}    Awconnect browser extension")
        print(f"    {bold('desktop')}    AitherDesktop native application")
        print(f"    {bold('stop')}       Stop a running deployment")
        print()
        print(f"  Examples:")
        print(f"    {dim('aither deploy sovereign')}")
        print(f"    {dim('aither deploy sovereign --app demo --gpu')}")
        print(f"    {dim('aither deploy sovereign --app assistant --gpu')}")
        print(f"    {dim('aither deploy ollama')}")
        print(f"    {dim('aither deploy ollama --models qwen3:8b,phi4')}")
        print(f"    {dim('aither deploy grid --mac-host 192.168.1.100')}")
        print(f"    {dim('aither deploy node')}")
        print(f"    {dim('aither deploy node --gpu --dashboard')}")
        print(f"    {dim('aither deploy node --genesis --gpu --dashboard')}")
        print(f"    {dim('aither deploy node --addons qdrant,knowledge-rag')}")
        print(f"    {dim('aither deploy addons qdrant knowledge-rag')}")
        print(f"    {dim('aither deploy full --profile chat-full')}")
        print(f"    {dim('aither deploy stop all')}")
        print()
        return 1

    if component == "fleet-refresh":
        return deploy_fleet_refresh(args)

    if component == "sovereign":
        # --list-apps: show available app templates
        list_apps = getattr(args, "list_apps", False)
        if list_apps:
            manifests_dir = Path("AitherOS/config/app_manifests")
            print()
            print(bold("  Available App Templates"))
            print()
            if manifests_dir.exists():
                for f in sorted(manifests_dir.glob("*.yaml")):
                    manifest = _parse_simple_yaml(f.read_text(encoding="utf-8"))
                    name = manifest.get("name", f.stem)
                    slug = manifest.get("slug", f.stem)
                    port = manifest.get("port", "?")
                    plan = manifest.get("requires_plan", "free")
                    desc = manifest.get("description", "")[:60]
                    print(f"  {bold(slug):15s} {name:30s} :{port:<6} ({plan})")
                    if desc:
                        print(f"  {' ':15s} {dim(desc)}")
            else:
                warn("No app manifests found (run from repo root or use --app <id>)")
            print()
            print(f"  Deploy: {cyan('aither deploy sovereign --app <slug> --tenant <name>')}")
            print()
            return 0

        tag = getattr(args, "tag", "latest") or "latest"
        gpu = getattr(args, "gpu", False)
        no_sync = getattr(args, "no_sync", False)
        no_memory = getattr(args, "no_memory", False)
        app_template = getattr(args, "app", None)
        api_key = getattr(args, "api_key", None)
        tenant_arg = getattr(args, "tenant", None)
        admin_email = getattr(args, "admin_email", "") or ""
        return deploy_sovereign(
            dry_run=dry_run, tag=tag, app_template=app_template,
            gpu=gpu, sync=not no_sync, no_memory=no_memory,
            api_key_arg=api_key, tenant=tenant_arg,
            admin_email=admin_email,
        )

    elif component == "tenant-app":
        tier = getattr(args, "tier", None)
        no_pull = getattr(args, "no_pull", False)
        no_start = getattr(args, "no_start", False)
        api_key = getattr(args, "api_key", None)
        app_name = getattr(args, "app", None) or getattr(args, "app_template", None)
        if not app_name:
            warn("deploy tenant-app needs --app <name> (the setup wizard to run)")
            return 1
        return deploy_tenant_app(
            app_name, dry_run=dry_run, tier=tier, no_pull=no_pull,
            start=not no_start, api_key_arg=api_key,
        )

    elif component == "grid":
        mac_host = getattr(args, "mac_host", "") or ""
        cluster_nodes = getattr(args, "cluster_nodes", "") or ""
        hf_token = getattr(args, "hf_token", "") or ""
        skip_health = getattr(args, "skip_health", False)
        return deploy_grid(
            dry_run=dry_run, mac_host=mac_host,
            cluster_nodes=cluster_nodes, hf_token=hf_token,
            skip_health=skip_health,
        )

    elif component == "ollama":
        models = None
        models_str = getattr(args, "models", None)
        if models_str:
            models = [m.strip() for m in models_str.split(",") if m.strip()]
        return deploy_ollama(dry_run=dry_run, models=models)

    elif component == "vllm":
        tier = getattr(args, "tier", None)
        hf_token = getattr(args, "hf_token", "") or ""
        return deploy_vllm(dry_run=dry_run, tier=tier, hf_token=hf_token)

    elif component == "node":
        tag = getattr(args, "tag", "latest") or "latest"
        gpu = getattr(args, "gpu", False)
        dashboard = getattr(args, "dashboard", False)
        mesh = getattr(args, "mesh", False)
        memory_flag = getattr(args, "memory", False)
        api_key = getattr(args, "api_key", None)
        sovereign = getattr(args, "sovereign", False)
        hub_url = getattr(args, "hub", "https://api.aitherium.com")
        tenant_arg = getattr(args, "tenant", None)
        federate_flag = getattr(args, "federate", False)
        portal_token_arg = getattr(args, "portal_token", None)

        # ADK-native node (Tier 2, lightweight) is the new default
        # --genesis flag opts into the full Genesis stack
        use_genesis = getattr(args, "genesis", False)

        if use_genesis:
            rc = deploy_node(dry_run=dry_run, tag=tag, gpu=gpu,
                             dashboard=dashboard, mesh=mesh, memory=memory_flag,
                             api_key_arg=api_key,
                             sovereign=sovereign, hub_url=hub_url, tenant=tenant_arg,
                             federate=federate_flag, portal_token=portal_token_arg)
        else:
            rc = deploy_adk_node(dry_run=dry_run, tag=tag, gpu=gpu,
                                 dashboard=dashboard, memory=memory_flag,
                                 api_key_arg=api_key,
                                 sovereign=sovereign, hub_url=hub_url, tenant=tenant_arg,
                                 federate=federate_flag, portal_token=portal_token_arg)

        # Co-deploy addons if --addons specified
        addons_str = getattr(args, "addons", None)
        if addons_str and rc == 0:
            addon_list = [a.strip() for a in addons_str.split(",") if a.strip()]
            if addon_list:
                info(f"\nCo-deploying addons: {', '.join(addon_list)}")
                rc = deploy_addons(
                    addon_ids=addon_list, dry_run=dry_run, tag=tag,
                    api_key_arg=api_key, sovereign=sovereign,
                    hub_url=hub_url, tenant=tenant_arg,
                )
        return rc

    elif component == "core":
        tag = getattr(args, "tag", "latest") or "latest"
        api_key = getattr(args, "api_key", None)
        return deploy_core(dry_run=dry_run, tag=tag, api_key_arg=api_key)

    elif component == "full":
        tag = getattr(args, "tag", "latest") or "latest"
        profile = getattr(args, "profile", "chat-agents") or "chat-agents"
        api_key = getattr(args, "api_key", None)
        return deploy_full(dry_run=dry_run, tag=tag, profile=profile, api_key_arg=api_key)

    elif component == "addons":
        addon_ids_raw = getattr(args, "addon_ids", []) or []
        tag = getattr(args, "tag", "latest") or "latest"
        api_key = getattr(args, "api_key", None)
        sovereign = getattr(args, "sovereign", False)
        hub_url = getattr(args, "hub", "https://api.aitherium.com")
        tenant_arg = getattr(args, "tenant", None)
        list_only = getattr(args, "list_addons", False)
        if list_only:
            from adk.addon_compose import list_available_addons
            manifests = list_available_addons()
            print()
            print(bold("  Available Addons"))
            print()
            for m in manifests:
                gpu_tag = " [GPU]" if m.get("resources", {}).get("gpu") else ""
                plan_tag = m.get("requires_plan", "free")
                print(f"  {bold(m['id']):20s} {m.get('name', ''):25s} "
                      f":{m.get('default_port', '?'):<6} "
                      f"({plan_tag}){gpu_tag}")
            print()
            return 0
        return deploy_addons(
            addon_ids=addon_ids_raw or None, dry_run=dry_run, tag=tag,
            api_key_arg=api_key, sovereign=sovereign,
            hub_url=hub_url, tenant=tenant_arg,
        )

    elif component == "connect":
        api_key = getattr(args, "api_key", None)
        return deploy_connect(dry_run=dry_run, api_key_arg=api_key)

    elif component == "desktop":
        api_key = getattr(args, "api_key", None)
        return deploy_desktop(dry_run=dry_run, api_key_arg=api_key)

    elif component == "stop":
        stop_target = getattr(args, "stop_target", None)
        if not stop_target:
            err("Specify what to stop: ollama, vllm, node, core, full, sovereign, addons, all")
            return 1
        if stop_target == "addons":
            compose_file = AITHER_DIR / "docker-compose.addons.yml"
            if not compose_file.exists():
                err("No addon compose file found")
                return 1
            return _docker_compose(compose_file, [], "down", dry_run)
        if stop_target == "sovereign":
            compose_file = AITHER_DIR / "docker-compose.sovereign.yml"
            if not compose_file.exists():
                err("No sovereign compose file found")
                return 1
            # Include any app overlay files in teardown
            compose_cmd = ["docker", "compose", "-f", str(compose_file)]
            for f in sorted(AITHER_DIR.glob("docker-compose.sovereign-*.yml")):
                compose_cmd += ["-f", str(f)]
            compose_cmd += ["--profile", "gpu", "--profile", "sync", "down"]
            if dry_run:
                info(f"Would run: {' '.join(compose_cmd)}")
                return 0
            info(f"Running: {' '.join(compose_cmd)}")
            try:
                result = subprocess.run(compose_cmd, timeout=120)
                return result.returncode
            except Exception as exc:
                err(f"Stop failed: {exc}")
                return 1
        return deploy_stop(stop_target)

    else:
        err(f"Unknown component: {component}")
        print(f"  Run {cyan('aither deploy')} to see available components.")
        return 1
