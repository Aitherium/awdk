"""Environment-based configuration for ADK.

Security boundary:
    LOCAL mode (localhost connections): No auth required. All services on the same
    machine trust each other via localhost binding (127.0.0.1). Docker services are
    only exposed on 127.0.0.1, not 0.0.0.0, so LAN peers cannot reach them.

    REMOTE/CLOUD mode: All requests carry Authorization: Bearer <AITHER_API_KEY>.
    The API key is stored in ~/.aither/config.json or AITHER_API_KEY env var.
    Cloud gateway (mcp.aitherium.com) enforces HMAC tenant isolation.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("adk.config")


# ---------------------------------------------------------------------------
# Self-host handoff
# ---------------------------------------------------------------------------
#: What to tell someone when the hosted platform cannot be reached.
#:
#: Self-hosting is a capability of AitherOS itself, not of any one deployment or
#: tenant — the installer is the same for everybody — so this is a constant here
#: rather than something each surface hardcodes or each operator configures. Any
#: surface that reports "cloud unreachable" should offer it: a status line that
#: says a thing is down and stops there leaves the user with nowhere to go, which
#: is the whole failure this exists to end.
#:
#: The web half of this lives in awkit's SignIn screen, which applies the
#: same values as a default and lets a deployment opt OUT. Keep the two in step.
#:
#: Public URLs only — this module ships to PyPI. Verified 2026-08-17 to serve a
#: real shell script rather than an HTML page: `curl -fsSL` does not fail on a
#: 200, so a wrong URL here pipes a web page straight into a shell.
SELF_HOST_INSTALL_URL = "https://aitherium.com/install.sh"
SELF_HOST_INSTALL_PS1 = "https://aitherium.com/install.ps1"
SELF_HOST_DOCS_URL = "https://api.aitherium.com/get"


def self_host_hint(windows: bool | None = None) -> str:
    """One line telling the user they can run this themselves, with the command.

    ``windows`` selects the PowerShell installer; None auto-detects. Returns a
    plain string so every caller — CLI, daemon log, agent reply — renders it in
    its own voice instead of each inventing its own URL.
    """
    import sys as _sys
    if windows is None:
        windows = _sys.platform == "win32"
    if windows:
        cmd = f"irm {SELF_HOST_INSTALL_PS1} | iex"
    else:
        cmd = f"curl -fsSL {SELF_HOST_INSTALL_URL} | bash"
    return f"Run it yourself instead: {cmd}  ({SELF_HOST_DOCS_URL})"


# ---------------------------------------------------------------------------
# ~/.aither/config.json helpers
# ---------------------------------------------------------------------------

_CONFIG_PATH_JSON = Path.home() / ".aither" / "config.json"
_CONFIG_PATH_YAML = Path.home() / ".aither" / "config.yaml"
# Prefer YAML (shared with AitherShell), fall back to JSON (legacy)
_CONFIG_PATH = _CONFIG_PATH_YAML if _CONFIG_PATH_YAML.exists() else _CONFIG_PATH_JSON


def _active_profile_creds() -> dict[str, Any]:
    """Surface the active login token from ``~/.aither/auth.json``.

    ``adk login`` mirrors the session into auth.json (a profiles store shared
    with the TypeScript shell), but the legacy config.json may not carry the
    token — so ``whoami``/``up``/``enroll`` could report "not logged in" while a
    perfectly valid session exists. This reads the active profile so those paths
    see the session regardless of which store holds it. Fail-soft: any problem
    returns ``{}`` (treated as logged-out), never raises.
    """
    try:
        auth = json.loads((Path.home() / ".aither" / "auth.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    prof = (auth.get("profiles") or {}).get(auth.get("active_profile") or "") or {}
    token = prof.get("access_token") or ""
    if not token:
        return {}
    user = prof.get("user") or {}
    out: dict[str, Any] = {"access_token": token, "api_key": token}
    for src, dst in (("endpoint", "endpoint"), ("genesis_url", "genesis_url")):
        if prof.get(src):
            out[dst] = prof[src]
    for key in ("tenant_id", "tenant_slug", "username"):
        if user.get(key):
            out[key] = user[key]
    return out


def load_saved_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load persisted config from ``~/.aither/config.yaml`` or ``config.json``.

    Tries YAML first (shared with AitherShell), falls back to JSON (legacy).
    When neither carries a login token, backfills it from the active profile in
    ``auth.json`` so the CLI sees a valid session no matter which store holds it.
    Returns an empty dict when nothing is found. Backfill only applies to the
    default path (an explicit ``config_path`` is returned verbatim, preserving
    test isolation).
    """
    cfg: dict[str, Any] = {}

    # Try YAML first
    yaml_path = config_path or _CONFIG_PATH_YAML
    if yaml_path.exists() and yaml_path.suffix in (".yaml", ".yml"):
        try:
            import yaml
            cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.debug("Failed to read YAML config from %s", yaml_path)

    # Fall back to JSON
    if not cfg:
        json_path = config_path or _CONFIG_PATH_JSON
        if json_path.exists():
            try:
                cfg = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                logger.debug("Failed to read JSON config from %s", json_path)

    # Backfill the login token from auth.json (default path only) so a session
    # stored there is not invisible to whoami/up/enroll.
    if config_path is None and not (cfg.get("api_key") or cfg.get("access_token")):
        for k, v in _active_profile_creds().items():
            cfg.setdefault(k, v)

    return cfg


def save_saved_config(data: dict[str, Any], config_path: Path | None = None) -> Path:
    """Merge *data* into the persisted ADK config and write it back.

    Creates ``~/.aither/`` if it does not exist.  Returns the path that was
    written for caller convenience.
    """
    path = config_path or _CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_saved_config(path)

    # FAIL CLOSED on an unreadable file. This is a whole-file overwrite, and
    # load_saved_config swallows every read failure into an empty dict -- so if
    # the read fails for ANY reason (a transient lock, a half-written file, an
    # import error in the yaml branch), the merge below has nothing to merge
    # into and this write DELETES every key the caller did not pass.
    #
    # Not hypothetical. Measured 2026-08-23 on the fleet host: ~/.aither/config.yaml
    # went from 11 keys to exactly the TWO that `adk relay` writes
    # (relay_token, relay_nick), losing url, gateway_url, safety_level, stream,
    # rich_output, show_thinking, show_metadata and session_id. The caller
    # succeeded, nothing logged, and the loss was found only because an unrelated
    # gate happened to read one of the dropped keys an hour later.
    #
    # A config write that cannot see what is already there must REFUSE. Losing a
    # credential silently is strictly worse than a caller getting an error it can
    # retry -- the classic fail-open gate pattern,
    # applied to a file instead of an authz decision.
    if not existing and path.exists() and path.stat().st_size > 0:
        raise OSError(
            "refusing to write %s: it holds %d bytes but parsed to nothing, so this "
            "write would DELETE every key not passed in. Fix or move the file first."
            % (path, path.stat().st_size)
        )

    existing.update(data)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return path


def _sanitize_ollama_host(raw: str) -> str:
    """Convert Ollama's bind address (``0.0.0.0:11434``) to a connectable URL.

    Ollama sets ``OLLAMA_HOST`` to its *listen* address which often contains
    ``0.0.0.0`` — a valid bind address but not a valid connection target on
    Windows/macOS.  Rewrite to ``localhost`` so HTTP clients can connect.
    """
    if not raw:
        return "http://localhost:11434"
    # Strip protocol if present
    if raw.startswith("http://") or raw.startswith("https://"):
        host_part = raw.split("://", 1)[1]
        scheme = raw.split("://", 1)[0]
    else:
        host_part = raw
        scheme = "http"
    # Replace 0.0.0.0 with localhost
    if host_part.startswith("0.0.0.0"):
        host_part = "localhost" + host_part[7:]
    return f"{scheme}://{host_part}"


@dataclass
class Config:
    """ADK configuration, populated from environment variables with sensible defaults.

    If AITHER_PROFILE is set (or auto-detected via AgentSetup), the hardware profile
    YAML is loaded and its model/limits settings are applied as defaults — env vars
    always override profile values.
    """

    # LLM backend: "ollama", "openai", "anthropic", "auto"
    llm_backend: str = field(default_factory=lambda: os.getenv("AITHER_LLM_BACKEND", "auto"))

    # Model selection (env vars override profile)
    model: str = field(default_factory=lambda: os.getenv("AITHER_MODEL", ""))
    small_model: str = field(default_factory=lambda: os.getenv("AITHER_SMALL_MODEL", ""))
    large_model: str = field(default_factory=lambda: os.getenv("AITHER_LARGE_MODEL", ""))

    # Ollama — sanitize OLLAMA_HOST which Ollama sets to a bind address (0.0.0.0:11434)
    ollama_host: str = field(default_factory=lambda: _sanitize_ollama_host(os.getenv("OLLAMA_HOST", "")))

    # Generic base URL for a self-hosted OpenAI-compatible backend (llamacpp /
    # llama-server, a local vLLM, LM Studio, …). Lets a self-hosted operator point
    # `--backend llamacpp` at their OWN server (e.g. a PrismML Bonsai llama.cpp on
    # http://localhost:8090/v1) instead of the provider's public API. Without this,
    # `--backend llamacpp` fell back to _COMPAT_URLS' openai.com default.
    llm_base_url: str = field(default_factory=lambda: os.getenv("AITHER_LLM_BASE_URL", ""))

    # OpenAI-compatible
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))

    # Anthropic
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    # DeepSeek
    deepseek_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))

    # Moonshot (Kimi K3 — OpenAI-compatible, api.moonshot.ai)
    moonshot_api_key: str = field(default_factory=lambda: os.getenv("MOONSHOT_API_KEY", ""))

    # Reasoning backend — separate API for effort 7+ tasks (hybrid mode)
    # Values: "", "anthropic", "openai", "deepseek", "moonshot", "gateway"
    reasoning_backend: str = field(default_factory=lambda: os.getenv("AITHER_REASONING_BACKEND", ""))
    reasoning_api_key: str = field(default_factory=lambda: os.getenv("AITHER_REASONING_API_KEY", ""))
    reasoning_base_url: str = field(default_factory=lambda: os.getenv("AITHER_REASONING_BASE_URL", ""))
    reasoning_model: str = field(default_factory=lambda: os.getenv("AITHER_REASONING_MODEL", ""))

    # Perception backend — vision/multimodal requests (configurable)
    # Values: "", "anthropic", "openai", "gemini", "gateway"
    perception_backend: str = field(default_factory=lambda: os.getenv("AITHER_PERCEPTION_BACKEND", ""))
    perception_api_key: str = field(default_factory=lambda: os.getenv("AITHER_PERCEPTION_API_KEY", ""))
    perception_base_url: str = field(default_factory=lambda: os.getenv("AITHER_PERCEPTION_BASE_URL", ""))
    perception_model: str = field(default_factory=lambda: os.getenv("AITHER_PERCEPTION_MODEL", ""))

    # Cluster backend — dedicated CPU cluster for effort 9+ tasks (grid mode)
    # Values: "", "openai", "vllm", "llamacpp"
    cluster_backend: str = field(default_factory=lambda: os.getenv("AITHER_CLUSTER_BACKEND", ""))
    cluster_base_url: str = field(default_factory=lambda: os.getenv("AITHER_CLUSTER_BASE_URL", ""))
    cluster_model: str = field(default_factory=lambda: os.getenv("AITHER_CLUSTER_MODEL", ""))

    # Extra vLLM ports to scan (comma-separated)
    vllm_extra_ports: str = field(default_factory=lambda: os.getenv("AITHER_VLLM_PORTS", ""))

    # DGX Spark / remote vLLM endpoint
    dgx_url: str = field(default_factory=lambda: os.getenv("AITHER_DGX_URL", ""))

    # General API key (for gateway or fallback)
    aither_api_key: str = field(default_factory=lambda: os.getenv("AITHER_API_KEY", ""))

    # Server
    server_port: int = field(default_factory=lambda: int(os.getenv("AITHER_PORT", "8080")))
    server_host: str = field(default_factory=lambda: os.getenv("AITHER_HOST", "0.0.0.0"))

    # Phonehome (opt-in)
    phonehome_enabled: bool = field(
        default_factory=lambda: os.getenv("AITHER_PHONEHOME", "").lower() in ("true", "1", "yes")
    )
    gateway_url: str = field(
        default_factory=lambda: os.getenv("AITHER_GATEWAY_URL", "https://gateway.aitherium.com")
    )

    # Prefer local inference over gateway even when AITHER_API_KEY is set
    prefer_local: bool = field(
        default_factory=lambda: os.getenv("AITHER_PREFER_LOCAL", "").lower() in ("true", "1", "yes")
    )

    # Cloud mode: "" (auto), "cloud_first", "cloud_only", "local_first", "local_only"
    # Set by `adk setup --mode cloud` or `adk quickstart --cloud`
    cloud_mode: str = field(default_factory=lambda: os.getenv("AITHER_CLOUD_MODE", ""))

    # Register agent with gateway on startup (opt-in)
    register_agent: bool = field(
        default_factory=lambda: os.getenv("AITHER_REGISTER_AGENT", "").lower() in ("true", "1", "yes")
    )

    # Tenant context (set by ``aither connect``, stored in ~/.aither/config.json)
    tenant_id: str = field(
        default_factory=lambda: os.getenv("AITHER_TENANT_ID", "")
    )

    # Data directory
    data_dir: str = field(
        default_factory=lambda: os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither"))
    )

    # Observability — AitherOS service URLs (auto-detected from localhost)
    chronicle_url: str = field(
        default_factory=lambda: os.getenv("AITHER_CHRONICLE_URL", "")
    )
    watch_url: str = field(
        default_factory=lambda: os.getenv("AITHER_WATCH_URL", "")
    )
    pulse_url: str = field(
        default_factory=lambda: os.getenv("AITHER_PULSE_URL", "")
    )

    # LLMFit sidecar — hardware-aware model scoring
    # If empty, the llmfit client auto-resolves via convention (port 8793)
    llmfit_url: str = field(
        default_factory=lambda: os.getenv("AITHER_LLMFIT_URL", "")
    )

    # Fleet memory sync — push local memories to Qdrant/Nexus for RAG.
    # Set by ``aither connect`` or portal provisioning.
    # URL targets: Nexus (:8122), portal gateway, or customer Qdrant.
    fleet_memory_url: str = field(
        default_factory=lambda: os.getenv("AITHER_FLEET_MEMORY_URL", "")
    )
    fleet_memory_collection: str = field(
        default_factory=lambda: os.getenv("AITHER_FLEET_COLLECTION", "memories")
    )
    fleet_sync: str = field(
        default_factory=lambda: os.getenv("AITHER_FLEET_SYNC", "auto")
    )

    # JSON structured logging (default off for standalone ADK; set AITHER_JSON_LOGGING=true for prod)
    json_logging: bool = field(
        default_factory=lambda: os.getenv("AITHER_JSON_LOGGING", "false").lower() in ("true", "1", "yes")
    )

    # Agent identity (from project config.yaml or env)
    identity: str = field(default_factory=lambda: os.getenv("AITHER_IDENTITY", ""))

    # Required tool packs — auto-loaded at agent init (comma-separated env or config.yaml tools.packs)
    required_packs: list[str] = field(default_factory=list)

    # Hardware profile (auto-detected or set via AITHER_PROFILE)
    profile: str = field(default_factory=lambda: os.getenv("AITHER_PROFILE", ""))

    # Profile-derived settings (populated by from_profile/apply_profile)
    max_context: int = 0          # 0 = unlimited (let model decide)
    max_concurrent: int = 0       # 0 = unlimited
    profile_models: dict = field(default_factory=dict)  # {default, small, large, embedding, ...}

    @classmethod
    def from_env(cls) -> Config:
        """Create config from current environment variables.

        If AITHER_PROFILE is set, loads and applies the hardware profile.
        If not set, checks ~/.aither/detected_profile from a previous auto_setup().

        Also loads ``tenant_id`` and ``api_key`` from ``~/.aither/config.json``
        when those values are not already set via environment variables.
        """
        config = cls()
        if config.profile:
            config.apply_profile(config.profile)
        else:
            # Try auto-detected profile from previous setup run
            marker = Path(config.data_dir) / "detected_profile"
            if marker.exists():
                try:
                    detected = marker.read_text(encoding="utf-8").strip()
                    if detected:
                        config.apply_profile(detected)
                except Exception:
                    pass

        # Backfill from saved config.json (env vars always win)
        saved = load_saved_config()
        if not config.tenant_id and saved.get("tenant_id"):
            config.tenant_id = saved["tenant_id"]
        if not config.aither_api_key and saved.get("api_key"):
            config.aither_api_key = saved["api_key"]
        if not config.reasoning_backend and saved.get("reasoning_backend"):
            config.reasoning_backend = saved["reasoning_backend"]
        if not config.reasoning_api_key and saved.get("reasoning_api_key"):
            config.reasoning_api_key = saved["reasoning_api_key"]
        if not config.reasoning_base_url and saved.get("reasoning_url"):
            config.reasoning_base_url = saved["reasoning_url"]
        if not config.reasoning_model and saved.get("reasoning_model"):
            config.reasoning_model = saved["reasoning_model"]
        if not config.perception_backend and saved.get("perception_backend"):
            config.perception_backend = saved["perception_backend"]
        if not config.perception_api_key and saved.get("perception_api_key"):
            config.perception_api_key = saved["perception_api_key"]
        if not config.perception_base_url and saved.get("perception_url"):
            config.perception_base_url = saved["perception_url"]
        if not config.perception_model and saved.get("perception_model"):
            config.perception_model = saved["perception_model"]
        if not config.deepseek_api_key and saved.get("deepseek_api_key"):
            config.deepseek_api_key = saved["deepseek_api_key"]
        if not config.moonshot_api_key and saved.get("moonshot_api_key"):
            config.moonshot_api_key = saved["moonshot_api_key"]
        if not config.dgx_url and saved.get("dgx_url"):
            config.dgx_url = saved["dgx_url"]
        if not config.cluster_backend and saved.get("cluster_backend"):
            config.cluster_backend = saved["cluster_backend"]
        if not config.cluster_base_url and saved.get("cluster_url"):
            config.cluster_base_url = saved["cluster_url"]
        if not config.cluster_model and saved.get("cluster_model"):
            config.cluster_model = saved["cluster_model"]
        # Tool packs persisted by `adk ui`/`adk vault`/save_saved_config live in the
        # SAVED config; without this backfill they never reached config.required_packs,
        # so a required pack (e.g. vault) enabled but its tools never registered at
        # startup — you had to re-enable it after every restart.
        if not config.required_packs and saved.get("required_packs"):
            config.required_packs = list(saved["required_packs"])

        # `adk backend set <provider> --base-url URL --model MODEL` persists
        # default_backend / inference_url / default_model into saved config.
        # Wire those into the live Config so switching a self-hosted brain (e.g.
        # `adk backend set llamacpp --base-url http://localhost:8090/v1`) actually
        # takes effect — before this the base URL was saved but never read, so the
        # provider fell back to its public API default. Env still wins (only fill
        # when the field is empty / at its default).
        if config.llm_backend == "auto" and saved.get("default_backend"):
            config.llm_backend = saved["default_backend"]
        if not config.llm_base_url and saved.get("inference_url"):
            config.llm_base_url = saved["inference_url"]
        if not config.model and saved.get("default_model"):
            config.model = saved["default_model"]

        # Cloud mode from setup --mode cloud/hybrid
        if saved.get("cloud_mode") and config.llm_backend == "auto":
            config.cloud_mode = saved["cloud_mode"]
            # Export to env so Memory and other modules that read env directly see it
            if not os.environ.get("AITHER_CLOUD_MODE"):
                os.environ["AITHER_CLOUD_MODE"] = config.cloud_mode

        # Cloud memory config (saved by `adk quickstart --cloud` gateway test)
        if saved.get("spirit_url") and not os.environ.get("AITHER_SPIRIT_URL"):
            os.environ["AITHER_SPIRIT_URL"] = saved["spirit_url"]
        if saved.get("spirit_teach_path") and not os.environ.get("AITHER_SPIRIT_TEACH_PATH"):
            os.environ["AITHER_SPIRIT_TEACH_PATH"] = saved["spirit_teach_path"]
        if saved.get("spirit_recall_path") and not os.environ.get("AITHER_SPIRIT_RECALL_PATH"):
            os.environ["AITHER_SPIRIT_RECALL_PATH"] = saved["spirit_recall_path"]

        # Fleet memory sync config (saved by `adk connect` or portal provisioning)
        if saved.get("fleet_memory_url") and not os.environ.get("AITHER_FLEET_MEMORY_URL"):
            os.environ["AITHER_FLEET_MEMORY_URL"] = saved["fleet_memory_url"]
            config.fleet_memory_url = saved["fleet_memory_url"]
        if saved.get("fleet_collection") and not os.environ.get("AITHER_FLEET_COLLECTION"):
            os.environ["AITHER_FLEET_COLLECTION"] = saved["fleet_collection"]
            config.fleet_memory_collection = saved["fleet_collection"]
        if saved.get("fleet_sync") and not os.environ.get("AITHER_FLEET_SYNC"):
            os.environ["AITHER_FLEET_SYNC"] = saved["fleet_sync"]
            config.fleet_sync = saved["fleet_sync"]

        # Backfill from provider_keys.json (written by `adk keys set`)
        # This is the bridge between `adk keys` CLI and the LLMRouter.
        config._apply_provider_keys()

        # Required packs from env var (comma-separated)
        env_packs = os.getenv("AITHER_REQUIRED_PACKS", "")
        if env_packs and not config.required_packs:
            config.required_packs = [p.strip() for p in env_packs.split(",") if p.strip()]

        # Load project-level config.yaml (from CWD, created by `adk init`)
        config._apply_project_config()

        return config

    @classmethod
    def from_profile(cls, profile_name: str) -> Config:
        """Create config from a hardware profile name."""
        config = cls(profile=profile_name)
        config.apply_profile(profile_name)
        return config

    def apply_profile(self, profile_name: str) -> None:
        """Load a hardware profile YAML and apply its settings.

        Profile settings are defaults — env vars always win.
        Looks for profiles in: ./profiles/, package profiles/, ~/.aither/profiles/
        """
        try:
            import yaml
        except ImportError:
            logger.debug("PyYAML not installed, skipping profile load")
            return

        # Search paths for profile YAML
        search_dirs = [
            Path("profiles"),                                    # CWD
            Path(__file__).parent.parent / "profiles",           # package root
            Path(self.data_dir) / "profiles",                    # ~/.aither/profiles/
        ]

        profile_path = None
        for d in search_dirs:
            candidate = d / f"{profile_name}.yaml"
            if candidate.exists():
                profile_path = candidate
                break

        if not profile_path:
            logger.debug("Profile '%s' not found in %s", profile_name, [str(d) for d in search_dirs])
            return

        try:
            data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning("Failed to load profile %s: %s", profile_name, e)
            return

        self.profile = profile_name

        # Apply models (env vars override)
        models = data.get("models", {})
        self.profile_models = models
        if not self.model:
            # Use 'default' or 'chat' key from profile
            self.model = models.get("default", models.get("chat", ""))
        if not self.small_model:
            self.small_model = models.get("small", "")
        if not self.large_model:
            self.large_model = models.get("large", models.get("reasoning", ""))

        # Apply limits
        limits = data.get("limits", {})
        if not self.max_context:
            self.max_context = limits.get("max_context", 0)
        if not self.max_concurrent:
            self.max_concurrent = limits.get("max_concurrent", 0)

        logger.info("Applied profile '%s': model=%s, small=%s, large=%s, max_context=%d",
                     profile_name, self.model, self.small_model, self.large_model, self.max_context)

    def _apply_provider_keys(self) -> None:
        """Load API keys from ``~/.aither/provider_keys.json`` (written by ``adk keys set``).

        Only fills in fields that are still empty — env vars and saved config win.
        Also exports to env vars so child processes (LLMRouter providers) can find them.
        """
        keys_path = Path(self.data_dir) / "provider_keys.json"
        if not keys_path.exists():
            return
        try:
            keys = json.loads(keys_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return

        _KEY_MAP = {
            "openai": ("openai_api_key", "OPENAI_API_KEY"),
            "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
            "deepseek": ("deepseek_api_key", "DEEPSEEK_API_KEY"),
            "moonshot": ("moonshot_api_key", "MOONSHOT_API_KEY"),
            "perception": ("perception_api_key", "AITHER_PERCEPTION_API_KEY"),
        }
        for provider, (field_name, env_name) in _KEY_MAP.items():
            key = keys.get(provider, "")
            if key and not getattr(self, field_name, ""):
                setattr(self, field_name, key)
                # Also export to env so provider constructors can find it
                if not os.environ.get(env_name):
                    os.environ[env_name] = key

    def _apply_project_config(self) -> None:
        """Load project-level config.yaml (created by ``adk init``) from CWD.

        Only fills in fields that are still at their defaults (env wins).
        """
        project_config = Path("config.yaml")
        if not project_config.exists():
            return
        try:
            import yaml
        except ImportError:
            return
        try:
            data = yaml.safe_load(project_config.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            return

        # Map config.yaml keys to Config fields
        if not self.identity and data.get("identity"):
            self.identity = data["identity"]
        if self.llm_backend == "auto" and data.get("llm_backend"):
            self.llm_backend = data["llm_backend"]
        # Also accept "backend:" as alias in config.yaml
        if self.llm_backend == "auto" and data.get("backend"):
            self.llm_backend = data["backend"]
        if not self.model and data.get("model"):
            self.model = data["model"]
        if self.server_port == 8080 and data.get("port"):
            self.server_port = int(data["port"])
        # Tool packs from config.yaml: tools.packs or required_packs
        if not self.required_packs:
            _packs = (data.get("tools") or {}).get("packs") or data.get("required_packs") or []
            if isinstance(_packs, list):
                self.required_packs = _packs

    @property
    def backend(self) -> str:
        """Alias for ``llm_backend`` -- matches config.yaml key ``backend:``."""
        return self.llm_backend

    @backend.setter
    def backend(self, value: str) -> None:
        self.llm_backend = value

    def get_api_key(self) -> str:
        """Return the best available API key for the configured backend."""
        if self.llm_backend == "anthropic":
            return self.anthropic_api_key or self.aither_api_key
        if self.llm_backend == "openai":
            return self.openai_api_key or self.aither_api_key
        return self.aither_api_key or self.openai_api_key or self.anthropic_api_key

    def get_llmfit_client(self):
        """Create a LLMFitClient initialized with this config's llmfit_url.

        Returns None if the llmfit module isn't installed.
        """
        try:
            from adk.llmfit import get_llmfit
            return get_llmfit(base_url=self.llmfit_url or None)
        except ImportError:
            logger.debug("adk.llmfit module not available")
            return None
