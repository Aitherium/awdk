"""
AitherShell Onboarding Wizard
===============================

`aither setup` — walks a new user through the complete self-service flow:

1. Register / Login (get API key)
2. Detect local GPU (optional)
3. Connect to AitherOS (local or cloud)
4. Deploy first model (optional)
5. Configure Claude Code MCP (optional)

Can also be driven programmatically by agents via:
    from adk.shell.onboarding import run_onboarding
    result = await run_onboarding(mode="auto", email="...", ...)

Agent-readable result:
    {
        "status": "success",
        "api_key": "aither_sk_live_...",
        "tenant_id": "...",
        "endpoint": "https://...",
        "mcp_configured": true,
        "model_deployed": "nemotron-8b",
    }
"""

import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from adk.shell.auth import AuthStore
from adk.shell.config import AitherConfig, CONFIG_DIR, CONFIG_FILE, load_config, save_config

logger = logging.getLogger(__name__)

GATEWAY_URL = "https://gateway.aitherium.com"
PORTAL_URL = "https://api.aitherium.com"
CLAUDE_CODE_SETTINGS = Path.home() / ".claude" / "settings.json"


class OnboardingResult:
    """Structured result for agent consumption."""

    def __init__(self):
        self.status = "incomplete"
        self.api_key: Optional[str] = None
        self.tenant_id: Optional[str] = None
        self.email: Optional[str] = None
        self.endpoint: Optional[str] = None
        self.model_deployed: Optional[str] = None
        self.mcp_configured = False
        self.gpu_detected: Optional[str] = None
        self.local_llm: Optional[Dict[str, Any]] = None
        self.framework_installed: Optional[Dict[str, Any]] = None
        self.errors: list = []
        self.steps_completed: list = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "api_key": self.api_key,
            "tenant_id": self.tenant_id,
            "email": self.email,
            "endpoint": self.endpoint,
            "model_deployed": self.model_deployed,
            "mcp_configured": self.mcp_configured,
            "gpu_detected": self.gpu_detected,
            "local_llm": self.local_llm,
            "framework_installed": self.framework_installed,
            "errors": self.errors,
            "steps_completed": self.steps_completed,
        }


def _print(msg: str, style: str = ""):
    """Print with optional styling."""
    if style == "header":
        print(f"\n{'=' * 60}")
        print(f"  {msg}")
        print(f"{'=' * 60}")
    elif style == "step":
        print(f"\n  [{len(_step_counter)}] {msg}")
        _step_counter.append(True)
    elif style == "ok":
        print(f"      OK: {msg}")
    elif style == "skip":
        print(f"      SKIP: {msg}")
    elif style == "error":
        print(f"      ERROR: {msg}", file=sys.stderr)
    elif style == "info":
        print(f"      {msg}")
    else:
        print(msg)


_step_counter: list = []


def _prompt(msg: str, default: str = "") -> str:
    """Interactive prompt with default."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"      {msg}{suffix}: ").strip()
        return val or default
    except (EOFError, KeyboardInterrupt):
        return default


def _confirm(msg: str, default: bool = True) -> bool:
    """Yes/no prompt."""
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        val = input(f"      {msg}{suffix}: ").strip().lower()
        if not val:
            return default
        return val in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return default


def _detect_gpu() -> Optional[str]:
    """Detect local GPU."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _detect_local_genesis() -> Optional[str]:
    """Check if local Genesis is running."""
    try:
        resp = httpx.get("http://localhost:8001/health", timeout=3.0)
        if resp.status_code == 200:
            return "http://localhost:8001"
    except Exception:
        pass
    return None


async def _register_account(email: str, password: str) -> Dict[str, Any]:
    """Register via gateway."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/auth/register",
            json={"email": email, "password": password},
        )
        if resp.status_code == 200:
            return resp.json()
        # Try login if already registered
        resp = await client.post(
            f"{GATEWAY_URL}/v1/auth/login",
            json={"email": email, "password": password},
        )
        if resp.status_code == 200:
            return resp.json()
        raise Exception(f"Auth failed: {resp.status_code} {resp.text[:200]}")


async def _get_api_key(token: str) -> Dict[str, Any]:
    """Get or create API key."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{GATEWAY_URL}/v1/auth/api-key",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": f"aither-setup-{platform.node()}"},
        )
        if resp.status_code == 200:
            return resp.json()
        raise Exception(f"API key creation failed: {resp.status_code}")


def _save_auth(email: str, token: str, api_key: str, tenant_id: str = ""):
    """Save auth to ~/.aither/auth.json."""
    AuthStore.save({
        "version": 1,
        "active_profile": "default",
        "profiles": {
            "default": {
                "email": email,
                "token": token,
                "api_key": api_key,
                "tenant_id": tenant_id,
                "gateway_url": GATEWAY_URL,
                "created_at": __import__("datetime").datetime.now().isoformat(),
            }
        },
    })


def _configure_mcp() -> bool:
    """Add AitherOS MCP server to Claude Code settings."""
    settings_path = CLAUDE_CODE_SETTINGS
    settings: Dict[str, Any] = {}

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except Exception:
            pass

    mcp_servers = settings.setdefault("mcpServers", {})
    if "aitheros" in mcp_servers:
        return True  # Already configured

    mcp_servers["aitheros"] = {
        "command": "aither",
        "args": ["mcp", "serve"],
    }

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2))
    return True


async def run_onboarding(
    mode: str = "interactive",
    email: Optional[str] = None,
    password: Optional[str] = None,
    skip_gpu: bool = False,
    skip_deploy: bool = False,
    skip_mcp: bool = False,
    skip_llm: bool = False,
    skip_framework: bool = False,
    install_llm: Optional[bool] = None,
    llm_backend: str = "vllm",
    llm_model: Optional[str] = None,
    llm_port: int = 8080,
    install_framework: Optional[str] = None,
    framework_local_llm: Optional[bool] = None,
    framework_model: Optional[str] = None,
    framework_agent_name: Optional[str] = None,
) -> OnboardingResult:
    """
    Full end-to-end onboarding.

    Args:
        mode: "interactive" (prompts) or "auto" (no prompts, uses args)
        email / password: pre-filled for auto mode
        skip_gpu / skip_deploy / skip_mcp: existing skip flags
        skip_llm / skip_framework: skip the new local-LLM / framework steps
        install_llm: in auto mode, ``True`` to install local vLLM/Ollama,
            ``False`` to explicitly skip. ``None`` defaults to skip in auto.
        llm_backend / llm_model / llm_port: passed to the local_llm helper
        install_framework: ``"hermes"`` / ``"openclaw"`` / ``None`` (skip).
            In auto mode, ``None`` means skip.
        framework_local_llm: if True, point the installed framework at the
            local LLM endpoint. Defaults to True when ``install_llm`` is True.
        framework_model: model name to wire into the framework config.
        framework_agent_name: portal agent name for the framework's onboard step.

    Returns:
        OnboardingResult with all details for agent consumption
    """
    result = OnboardingResult()
    interactive = mode == "interactive"
    _step_counter.clear()

    if interactive:
        _print("AitherOS Setup Wizard", style="header")
        _print("This will register your account, detect your hardware,")
        _print("and connect you to AitherOS in about 60 seconds.")

    # ── Step 1: Check existing auth ──────────────────────────────────────────
    if interactive:
        _print("Checking existing authentication", style="step")

    existing = AuthStore.get_active_profile()
    if existing and existing.get("api_key"):
        result.api_key = existing["api_key"]
        result.email = existing.get("email")
        result.tenant_id = existing.get("tenant_id", "")
        result.steps_completed.append("auth_existing")
        if interactive:
            _print(f"Already logged in as {result.email}", style="ok")
    else:
        # ── Step 2: Register / Login ─────────────────────────────────────────
        if interactive:
            _print("Create account or login", style="step")
            if not email:
                email = _prompt("Email")
            if not password:
                import getpass
                password = getpass.getpass("      Password: ")
        if not email or not password:
            result.errors.append("Email and password required")
            result.status = "failed"
            return result

        try:
            auth_result = await _register_account(email, password)
            token = auth_result.get("token", "")
            user_id = auth_result.get("user_id", "")

            # Get API key
            key_result = await _get_api_key(token)
            api_key = key_result.get("api_key", key_result.get("key", ""))

            result.api_key = api_key
            result.email = email
            result.tenant_id = user_id

            # Save locally
            _save_auth(email, token, api_key, user_id)
            result.steps_completed.append("auth_registered")

            if interactive:
                _print(f"Registered as {email}", style="ok")
                _print(f"API Key: {api_key[:20]}...", style="info")

        except Exception as e:
            result.errors.append(f"Registration failed: {e}")
            if interactive:
                _print(str(e), style="error")
            result.status = "failed"
            return result

    # ── Step 3: Detect GPU ───────────────────────────────────────────────────
    if not skip_gpu:
        if interactive:
            _print("Detecting local GPU", style="step")

        gpu = _detect_gpu()
        if gpu:
            result.gpu_detected = gpu
            result.steps_completed.append("gpu_detected")
            if interactive:
                _print(f"Found: {gpu}", style="ok")
        else:
            if interactive:
                _print("No NVIDIA GPU detected (cloud inference available)", style="skip")

    # ── Step 4: Detect / connect backend ─────────────────────────────────────
    if interactive:
        _print("Connecting to AitherOS", style="step")

    local_genesis = _detect_local_genesis()
    if local_genesis:
        result.endpoint = local_genesis
        result.steps_completed.append("connected_local")
        if interactive:
            _print(f"Local Genesis at {local_genesis}", style="ok")
    else:
        result.endpoint = GATEWAY_URL
        result.steps_completed.append("connected_cloud")
        if interactive:
            _print(f"Using cloud gateway: {GATEWAY_URL}", style="ok")

    # ── Step 5: Save config ──────────────────────────────────────────────────
    if interactive:
        _print("Saving configuration", style="step")

    config = load_config()
    config.api_key = result.api_key or ""
    config.url = result.endpoint or config.url
    config.gateway_url = GATEWAY_URL
    save_config(config)
    result.steps_completed.append("config_saved")

    if interactive:
        _print(f"Saved to {CONFIG_FILE}", style="ok")

    # ── Step 6: Deploy model (optional) ──────────────────────────────────────
    if not skip_deploy and result.endpoint:
        should_deploy = True
        if interactive:
            _print("Deploy first model", style="step")
            should_deploy = _confirm("Deploy a model now?", default=False)

        if should_deploy and local_genesis:
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.get(f"{local_genesis}/health/deep")
                    if resp.status_code == 200:
                        data = resp.json()
                        # Check if vLLM is already running
                        services = data.get("services", {})
                        if any("vllm" in k.lower() for k in services):
                            result.model_deployed = "orchestrator (already running)"
                            result.steps_completed.append("model_running")
                            if interactive:
                                _print("vLLM already running locally", style="ok")
                        else:
                            result.steps_completed.append("model_skipped")
                            if interactive:
                                _print("No local vLLM. Use `aither deploy` to start one.", style="skip")
            except Exception:
                result.steps_completed.append("model_skipped")
        else:
            result.steps_completed.append("model_skipped")

    # ── Step 7: Configure MCP for Claude Code ────────────────────────────────
    if not skip_mcp:
        should_mcp = True
        if interactive:
            _print("Claude Code MCP integration", style="step")
            should_mcp = _confirm("Configure MCP for Claude Code?", default=True)

        if should_mcp:
            try:
                _configure_mcp()
                result.mcp_configured = True
                result.steps_completed.append("mcp_configured")
                if interactive:
                    _print(f"Added 'aitheros' MCP server to {CLAUDE_CODE_SETTINGS}", style="ok")
            except Exception as e:
                result.errors.append(f"MCP config failed: {e}")
                if interactive:
                    _print(str(e), style="error")
        else:
            if interactive:
                _print("Skipped. Run `aither mcp serve` to use manually.", style="skip")

    # ── Step 8: Local LLM (vLLM / Ollama) ────────────────────────────────────
    do_install_llm = False
    if not skip_llm:
        if interactive:
            _print("Local LLM server (optional)", style="step")
            default_yes = bool(result.gpu_detected) or _which("ollama")
            do_install_llm = _confirm(
                "Install a local LLM (vLLM if GPU, otherwise Ollama)?",
                default=default_yes,
            )
            if do_install_llm:
                default_backend = "vllm" if result.gpu_detected else "ollama"
                llm_backend = _prompt("Backend (vllm/ollama)", default=default_backend) or default_backend
                if llm_backend not in ("vllm", "ollama"):
                    llm_backend = default_backend
                default_model = (
                    "meta-llama/Meta-Llama-3.1-8B-Instruct"
                    if llm_backend == "vllm" else "llama3.1:8b"
                )
                llm_model = _prompt("Model", default=llm_model or default_model) or default_model
        else:
            do_install_llm = bool(install_llm)

        if do_install_llm:
            llm_outcome = _install_local_llm(
                backend=llm_backend,
                model=llm_model or (
                    "meta-llama/Meta-Llama-3.1-8B-Instruct"
                    if llm_backend == "vllm" else "llama3.1:8b"
                ),
                port=llm_port,
                interactive=interactive,
            )
            result.local_llm = llm_outcome
            if llm_outcome.get("ok"):
                result.steps_completed.append("local_llm_installed")
                if interactive:
                    _print(f"Local LLM ready at {llm_outcome.get('endpoint')}", style="ok")
            else:
                result.errors.append(f"local LLM install failed: {llm_outcome.get('error')}")
                if interactive:
                    _print(str(llm_outcome.get("error")), style="error")
        else:
            if interactive:
                _print("Skipped local LLM install", style="skip")

    # ── Step 9: Install an agent framework (Hermes / OpenClaw) ───────────────
    chosen_framework = None
    if not skip_framework:
        if interactive:
            _print("Agent framework (Hermes / OpenClaw)", style="step")
            do_framework = _confirm("Download & deploy Hermes or OpenClaw now?", default=False)
            if do_framework:
                chosen_framework = _prompt(
                    "Framework (hermes/openclaw)", default="hermes"
                ).lower().strip() or "hermes"
                if chosen_framework not in ("hermes", "openclaw"):
                    chosen_framework = "hermes"
        else:
            chosen_framework = install_framework

        if chosen_framework:
            use_local = (
                framework_local_llm
                if framework_local_llm is not None
                else bool(do_install_llm and result.local_llm and result.local_llm.get("ok"))
            )
            fw_outcome = _install_agent_framework(
                framework=chosen_framework,
                use_local_llm=use_local,
                model=framework_model or llm_model,
                agent_name=framework_agent_name,
                interactive=interactive,
            )
            result.framework_installed = fw_outcome
            if fw_outcome.get("ok"):
                result.steps_completed.append(f"framework_{chosen_framework}_installed")
                if interactive:
                    _print(f"Installed {chosen_framework} at {fw_outcome.get('install_dir')}", style="ok")
            else:
                result.errors.append(f"framework install failed: {fw_outcome.get('error')}")
                if interactive:
                    _print(str(fw_outcome.get("error")), style="error")
        else:
            if interactive:
                _print("Skipped framework install", style="skip")

    # ── Done ─────────────────────────────────────────────────────────────────
    result.status = "success"

    if interactive:
        _print("Setup Complete!", style="header")
        _print("")
        _print("  Next steps:")
        _print(f"    aither \"hello\"                    # chat with AitherOS")
        _print(f"    aither mcp serve                  # start MCP server for Claude Code")
        _print(f"    aither --status                   # check connection")
        _print(f"    api.aitherium.com/downloads    # get the desktop app")
        _print("")
        if result.api_key:
            _print(f"  Your API key: {result.api_key[:25]}...")
            _print(f"  Keep it safe. Regenerate at: api.aitherium.com/account/api-keys")
        _print("")

    return result


# ---------------------------------------------------------------------------
# Helpers for the new optional steps + agent-driven planning
# ---------------------------------------------------------------------------

def _which(binary: str) -> Optional[str]:
    import shutil as _shutil
    return _shutil.which(binary)


def _run_adk_module(module: str, args: list) -> Dict[str, Any]:
    """Invoke ``python -m aither_adk.integrations.<module> ...`` and parse JSON.

    Falls back to the legacy ``adk.integrations.<module>`` import path.
    Returns the parsed JSON dict or ``{"ok": False, "error": "..."}``.
    """
    attempts = [
        [sys.executable, "-m", f"aither_adk.integrations.{module}", *args],
        [sys.executable, "-m", f"adk.integrations.{module}", *args],
    ]
    last_err: Optional[str] = None
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            stdout = (proc.stdout or "").strip()
            if not stdout and proc.returncode != 0:
                last_err = (proc.stderr or "").strip()[-300:] or f"exit {proc.returncode}"
                if "No module named" in (proc.stderr or ""):
                    continue
                return {"ok": False, "error": last_err}
            try:
                parsed = json.loads(stdout) if stdout else {}
            except Exception:
                return {"ok": proc.returncode == 0, "raw": stdout,
                        "error": None if proc.returncode == 0 else (proc.stderr or "")[-300:]}
            if proc.returncode != 0 and "error" not in parsed:
                parsed["error"] = (proc.stderr or "").strip()[-300:] or f"exit {proc.returncode}"
                parsed.setdefault("ok", False)
            else:
                parsed.setdefault("ok", proc.returncode == 0)
            return parsed
        except FileNotFoundError as e:
            last_err = str(e)
            continue
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timeout running {' '.join(cmd)}"}
    return {"ok": False, "error": last_err or "aither_adk module not importable"}


def _install_local_llm(*, backend: str, model: str, port: int,
                        interactive: bool) -> Dict[str, Any]:
    """Install local LLM via the framework_installer.local_llm helper.

    Returns a dict with at minimum ``{"ok": bool}`` plus ``endpoint`` on success.
    """
    if interactive:
        _print(f"Installing {backend} ({model}) — this may take several minutes...", style="info")
    out = _run_adk_module("local_llm", [
        "install", "--backend", backend, "--model", model, "--port", str(port),
    ])
    if not out.get("success", out.get("ok")):
        return {"ok": False, "backend": backend, "model": model,
                "error": out.get("error") or "install reported failure"}
    if interactive:
        _print("Starting local LLM daemon...", style="info")
    start = _run_adk_module("local_llm", ["start"])
    return {
        "ok": bool(start.get("ok")),
        "backend": backend,
        "model": model,
        "port": port,
        "endpoint": start.get("url") or f"http://127.0.0.1:{port}",
        "pid": start.get("pid"),
        "log": start.get("log"),
        "error": start.get("error"),
    }


def _install_agent_framework(*, framework: str, use_local_llm: bool,
                              model: Optional[str], agent_name: Optional[str],
                              interactive: bool) -> Dict[str, Any]:
    """Install Hermes or OpenClaw via framework_installer."""
    args = ["install", framework]
    if use_local_llm:
        args.append("--local-llm")
    else:
        args += ["--llm-backend", "portal"]
    if model:
        args += ["--model", model]
    if agent_name:
        args += ["--agent-name", agent_name]
    if interactive:
        target = "local LLM" if use_local_llm else "portal inference"
        _print(f"Cloning + installing {framework} ({target})...", style="info")
    out = _run_adk_module("framework_installer", args)
    return {
        "ok": bool(out.get("success", out.get("ok"))),
        "framework": framework,
        "install_dir": out.get("install_dir"),
        "llm_backend": out.get("llm_backend"),
        "llm_url": out.get("llm_url"),
        "next_steps": out.get("next_steps"),
        "portal_onboard": out.get("portal_onboard"),
        "warnings": out.get("warnings"),
        "error": out.get("error") if not out.get("success", out.get("ok")) else None,
    }


def plan_onboarding(
    *,
    install_llm: bool = False,
    llm_backend: str = "vllm",
    llm_model: Optional[str] = None,
    llm_port: int = 8080,
    install_framework: Optional[str] = None,
    framework_local_llm: Optional[bool] = None,
    framework_model: Optional[str] = None,
    framework_agent_name: Optional[str] = None,
    skip_gpu: bool = False,
    skip_deploy: bool = False,
    skip_mcp: bool = False,
) -> Dict[str, Any]:
    """Return a machine-readable plan of the steps `run_onboarding` will take.

    Designed for autonomous agents (Claude Code, etc.) to fetch the executable
    step list, dry-run them, or replay them via subprocess one at a time.
    Each step has ``id``, ``title``, ``cmd`` (the shell command an agent
    could run instead of using the in-process flow), ``optional``, and
    ``conditions`` describing when the step runs.
    """
    use_local = (
        framework_local_llm
        if framework_local_llm is not None
        else install_llm
    )
    resolved_model = framework_model or llm_model
    default_llm_model = (
        "meta-llama/Meta-Llama-3.1-8B-Instruct" if llm_backend == "vllm" else "llama3.1:8b"
    )
    steps = [
        {
            "id": "auth",
            "title": "Register or login at api.aitherium.com",
            "cmd": "aither login",
            "optional": False,
            "skipped_if": "already-authenticated",
        },
        {
            "id": "detect-gpu",
            "title": "Detect local NVIDIA GPU",
            "cmd": "aither llm detect",
            "optional": True,
            "skipped_if": "skip_gpu=True",
            "active": not skip_gpu,
        },
        {
            "id": "connect",
            "title": "Connect to local Genesis or cloud gateway",
            "cmd": "aither --status",
            "optional": False,
        },
        {
            "id": "save-config",
            "title": "Persist API key + endpoint to ~/.aithershell/config.yaml",
            "cmd": None,
            "optional": False,
        },
        {
            "id": "deploy-model",
            "title": "Probe local Genesis for an already-running vLLM",
            "cmd": "aither deploy",
            "optional": True,
            "active": not skip_deploy,
        },
        {
            "id": "mcp",
            "title": "Add 'aitheros' MCP server to ~/.claude/settings.json",
            "cmd": "aither setup --skip-gpu --skip-deploy --auto",
            "optional": True,
            "active": not skip_mcp,
        },
        {
            "id": "local-llm",
            "title": (
                f"Install local LLM ({llm_backend} / "
                f"{llm_model or default_llm_model}) on port {llm_port}"
            ),
            "cmd": (
                f"aither llm install --backend {llm_backend} "
                f"--model {llm_model or default_llm_model} --port {llm_port} && "
                f"aither llm start"
            ),
            "optional": True,
            "active": bool(install_llm),
        },
        {
            "id": "framework",
            "title": (
                f"Clone + install {install_framework or '<hermes|openclaw>'} into "
                f"~/.aither/frameworks/ "
                f"({'local LLM' if use_local else 'portal inference'})"
            ),
            "cmd": (
                (
                    f"aither install {install_framework} "
                    f"{'--local-llm' if use_local else '--portal-inference'}"
                    + (f" --model {resolved_model}" if resolved_model else "")
                    + (f" --agent-name {framework_agent_name}" if framework_agent_name else "")
                )
                if install_framework
                else "aither install hermes --local-llm   # or: aither install openclaw"
            ),
            "optional": True,
            "active": bool(install_framework),
        },
    ]
    return {
        "version": 1,
        "interactive_command": "aither setup",
        "agent_command": (
            "aither setup --auto --json"
            + (" --install-llm" if install_llm else "")
            + (f" --llm-backend {llm_backend}" if install_llm else "")
            + (f" --llm-model {llm_model}" if install_llm and llm_model else "")
            + (f" --install-framework {install_framework}" if install_framework else "")
        ),
        "steps": steps,
        "notes": [
            "Agents should prefer `aither setup --auto --json` for a single-call orchestration.",
            "Individual steps remain runnable via the `cmd` field for step-by-step execution.",
            "On hosts without a CUDA GPU, pass --llm-backend ollama (requires ollama already on PATH).",
        ],
    }
