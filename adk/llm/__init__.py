"""LLM provider layer — auto-detecting router across backends."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .base import (
    DegenerationDetector,
    LLMProvider,
    LLMResponse,
    Message,
    StreamChunk,
    ToolCall,
    llm_retry,
    strip_internal_tags,
)
from .continuation import run_continuation, stitch

if TYPE_CHECKING:
    from adk.config import Config

    from .cache import ResponseCache

logger = logging.getLogger("adk.llm")


def _onboarding_menu() -> str:
    """Guided-backend menu for no-backend errors; never let import issues mask
    the original ConnectionError."""
    try:
        from .onboarding import provider_menu
        return provider_menu()
    except Exception:
        return "Run `adk backend guide` for per-provider setup help."

__all__ = [
    "DegenerationDetector",
    "LLMProvider",
    "LLMResponse",
    "LLMRouter",
    "Message",
    "StreamChunk",
    "ToolCall",
    "llm_retry",
    "run_continuation",
    "stitch",
    "strip_internal_tags",
]

# Effort-based model selection defaults (static fallback when llmfit unavailable)
_EFFORT_MODELS = {
    "gateway": {
        "small": "aither-small",
        "medium": "aither-orchestrator",
        "large": "aither-reasoning",
    },
    "ollama": {
        "small": "gemma4:4b",
        "medium": "nemotron-orchestrator-8b",
        "large": "deepseek-r1:14b",
    },
    "openai": {
        "small": "gpt-4o-mini",
        "medium": "gpt-4o",
        "large": "o1",
    },
    "anthropic": {
        "small": "claude-haiku-4-5-20251001",
        "medium": "claude-sonnet-4-6",
        "large": "claude-opus-4-6",
    },
    "deepseek": {
        "small": "deepseek-chat",
        "medium": "deepseek-chat",
        "large": "deepseek-reasoner",
    },
    "moonshot": {
        "small": "kimi-k3",
        "medium": "kimi-k3",
        "large": "kimi-k3",
    },
    "picolm": {
        "small": "picolm",
        "medium": "picolm",
        "large": "picolm",
    },
    "desktop": {
        "small": "",          # let MicroScheduler choose
        "medium": "",
        "large": "",
    },
    "dual": {
        "small": "gemma4:4b",               # local Ollama
        "medium": "aither-orchestrator",     # remote desktop
        "large": "aither-reasoning",         # remote desktop
    },
}

# Providers that serve models from the LOCAL machine, where an ODS
# hardware-scored pick is meaningful. Cloud providers keep their static table:
# their catalogs are fixed and unrelated to this box's VRAM, and a local GGUF id
# sent to a cloud endpoint is a guaranteed 404. "dual" is excluded on purpose —
# only its `small` tier is local, so a single tier-wide substitution would break
# its remote tiers.
_HARDWARE_SCORED_PROVIDERS = frozenset({"ollama", "llamacpp", "vllm", "local"})

# Default inference URL — mcp.aitherium.com hosts the OpenAI-compatible
# /v1/chat/completions endpoint with ACTA auth and tenant scoping.
_GATEWAY_INFERENCE_URL = "https://mcp.aitherium.com/v1"
_DEMO_URL = "https://demo.aitherium.com"

# Per-model chat_template_kwargs applied on the OpenAI-compatible family. qwen3.6
# MUST run with thinking disabled (an enabled thinking pass burns ~15 min and
# returns empty content). Keyed by case-insensitive model-id substring, so it
# holds whether qwen is served via the gateway or a local vLLM, and a non-qwen
# model (e.g. gemma4 vision) on the same provider is unaffected.
# bonsai (Bonsai 2 27B is a Qwen3.6 derivative with the same thinking template):
# measured 2026-09-21 on the fleet lane (-c 32768 -np 2, --reasoning-budget 2048),
# a 9k-token prompt with thinking ON spent all 1024 completion tokens thinking and
# returned finish_reason=length with the reasoning prefix as content; the same
# request with enable_thinking=false answered in 2.4 s. Every daemon turn to
# bonsai2-27b read "model returned an empty completion" until this line.
_DEFAULT_CTK_BY_MODEL = {
    "qwen": {"enable_thinking": False},
    "bonsai": {"enable_thinking": False},
}

# llmfit-derived model cache (populated lazily)
_llmfit_models: dict[str, str] | None = None
_llmfit_checked: bool = False


class LLMRouter:
    """Multi-backend LLM router with auto-detection and effort-based model selection.

    Usage:
        # Auto-detect (tries Ollama localhost first)
        router = LLMRouter()

        # Explicit backend
        router = LLMRouter(provider="openai", api_key="sk-...")

        # Explicit with custom URL (vLLM, LM Studio, etc.)
        router = LLMRouter(provider="openai", base_url="http://localhost:8000/v1")
    """

    def __init__(
        self,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        config: Config | None = None,
        response_cache: "ResponseCache | None" = None,
    ):
        self._provider_name: str = ""
        self._provider: LLMProvider | None = None
        # config.model (AITHER_MODEL) must win when the caller didn't pin an
        # explicit model — otherwise every config/env model choice is silently
        # dropped and providers fall back to their static per-backend default
        # (e.g. "gpt-4o-mini"), which upstream routers may then remap to an
        # arbitrary model that was never meant to be called directly (verified
        # live: an unrecognized model name got silently remapped to a non-
        # tool-calling model on the shared fleet backend).
        self._model = model or (getattr(config, "model", "") if config else "") or None
        self._config = config
        # OQ16 lever 2 — opt-in response cache. OFF unless a ResponseCache is passed
        # here AND a call sets cacheable=True; default behavior is unchanged. Skipping
        # the LLM for an identical request changes determinism, so it must be opted into.
        self._response_cache = response_cache
        # Dual-mode: local provider for low effort, remote for high effort
        self._local_provider: LLMProvider | None = None
        self._remote_provider: LLMProvider | None = None
        self._remote_provider_name: str = ""
        self._remote_healthy: bool = True
        self._remote_health_checked: float = 0.0
        # Hybrid reasoning backend (set via set_reasoning_backend or config)
        self._reasoning_provider: LLMProvider | None = None
        self._reasoning_provider_name: str = ""
        self._reasoning_model: str | None = None
        # Cluster backend — dedicated CPU cluster for effort 9+ (grid mode)
        self._cluster_provider: LLMProvider | None = None
        self._cluster_provider_name: str = ""
        self._cluster_model: str | None = None
        # Perception backend — vision/multimodal requests (modality-based routing)
        self._perception_provider: LLMProvider | None = None
        self._perception_provider_name: str = ""
        self._perception_model: str | None = None
        # Failover chain — tried in order when primary returns 429/5xx/timeout
        self._failover_providers: list[tuple[str, LLMProvider]] = []
        self._failover_cooldown: float = 0.0

        if provider:
            self._provider = self._create_provider(provider, base_url, api_key)
            self._provider_name = provider
        else:
            self._deferred_base_url = base_url
            self._deferred_api_key = api_key

    # Well-known OpenAI-compatible base URLs for named providers
    _COMPAT_URLS: dict[str, str] = {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "moonshot": "https://api.moonshot.ai/v1",
        "groq": "https://api.groq.com/openai/v1",
        "together": "https://api.together.xyz/v1",
        "genesis": "https://localhost:8001/v1",  # Local AitherOS Genesis (HTTPS, internal AitherNet CA)
        # `adk bonsai-local` (Docker, :8090) and aitherium.com/install-bonsai.sh
        # (llama-server, :8080). Both are plain-http OpenAI-compatible servers;
        # the alias exists so `--backend bonsai-local` and `adk backend set
        # bonsai-local` resolve instead of raising "Unknown provider" and
        # silently falling back to auto-detect (measured 2026-09-12).
        "bonsai-local": "http://127.0.0.1:8090/v1",
        "bonsai": "http://127.0.0.1:8080/v1",
    }

    # Default models per named provider
    _COMPAT_MODELS: dict[str, str] = {
        "openai": "gpt-4o-mini",
        "deepseek": "deepseek-chat",
        "moonshot": "kimi-k3",
        "groq": "llama-3.3-70b-versatile",
        "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "genesis": "workflow",  # Genesis routes by priority; "workflow" = default fleet model
        "bonsai-local": "bonsai-27b",
        "bonsai": "bonsai-selfhost",  # the --alias install-bonsai.sh gives llama-server
    }

    def _create_provider(
        self, name: str, base_url: str | None = None, api_key: str | None = None
    ) -> LLMProvider:
        if name == "gateway":
            from .openai_compat import OpenAIProvider
            gateway_url = base_url or _GATEWAY_INFERENCE_URL
            return OpenAIProvider(
                base_url=gateway_url,
                api_key=api_key or "",
                default_model=self._model or "aither-orchestrator",
                ctk_by_model=_DEFAULT_CTK_BY_MODEL,
            )
        elif name == "ollama":
            from .ollama import OllamaProvider
            return OllamaProvider(
                host=base_url or "http://localhost:11434",
                default_model=self._model or "gemma4:4b",
            )
        elif name in ("openai", "vllm", "lmstudio", "llamacpp", "groq", "together", "deepseek", "moonshot", "genesis", "bonsai-local", "bonsai"):
            from .openai_compat import OpenAIProvider
            default_url = self._COMPAT_URLS.get(name, "https://api.openai.com/v1")
            default_model = self._COMPAT_MODELS.get(name, "gpt-4o-mini")
            # Self-hosted endpoints (genesis/vllm/llamacpp/lmstudio) may be served
            # over https with the internal AitherNet CA — trust it via tls_verify().
            # Public providers (openai/groq/together/deepseek) keep system trust
            # (None), since the internal CA bundle would REPLACE system roots and
            # break their public certs. Harmless on plain-http local endpoints.
            verify = None
            if name in ("genesis", "vllm", "llamacpp", "lmstudio", "bonsai-local", "bonsai"):
                from .._tls import tls_verify
                verify = tls_verify()
            return OpenAIProvider(
                base_url=base_url or default_url,
                api_key=api_key or "",
                default_model=self._model or default_model,
                ctk_by_model=_DEFAULT_CTK_BY_MODEL,
                verify=verify,
            )
        elif name == "anthropic":
            from .anthropic import AnthropicProvider
            return AnthropicProvider(
                api_key=api_key or "",
                default_model=self._model or "claude-sonnet-4-6",
            )
        elif name == "gemini":
            from .gemini import GeminiProvider
            return GeminiProvider(
                api_key=api_key or "",
                default_model=self._model or "gemini-2.0-flash",
            )
        elif name == "picolm":
            from .picolm import PicoLMProvider
            return PicoLMProvider(
                binary=base_url or "",  # Overload base_url as binary path
                model=self._model or "",
            )
        elif name == "acp":
            # External ACP agent (claude-agent-acp, codex-acp, gemini-cli, ...)
            # as the model. The command comes from `adk backend add acp --command`
            # (persisted config) or AITHER_ACP_COMMAND / AITHER_ACP_ARGS for
            # headless use. A missing command raises LOUD at construction — a
            # misconfigured backend must never degrade into an inert provider.
            from .acp_provider import ACPProvider

            cmd = os.environ.get("AITHER_ACP_COMMAND", "").strip()
            acp_args: list[str] = []
            _env_args = os.environ.get("AITHER_ACP_ARGS", "").strip()
            if _env_args:
                import shlex

                acp_args = shlex.split(_env_args)
            if not cmd:
                try:
                    from adk.config import load_saved_config

                    saved = load_saved_config()
                    cmd = str(saved.get("acp_command", "") or "").strip()
                    acp_args = list(saved.get("acp_args") or []) or acp_args
                except Exception:  # noqa: BLE001 — a read failure must not hide the error below
                    cmd = ""
            return ACPProvider(command=cmd, args=acp_args, model=self._model or "acp")
        else:
            raise ValueError(
                f"Unknown provider: {name}. "
                "Use 'gateway', 'ollama', 'openai', 'anthropic', 'gemini', 'deepseek', "
                "'moonshot', 'groq', 'together', 'vllm', 'lmstudio', 'llamacpp', "
                "'genesis', 'bonsai-local', 'bonsai', 'picolm', or 'acp'."
            )

    async def _try_local_selfhost(self) -> LLMProvider | None:
        """A Bonsai (or any OpenAI-compatible server) the user started themselves.

        aitherium.com/install-bonsai.sh serves on :8080, `adk bonsai-local` on
        :8090, the llamacpp container on :8092 — none of which the vLLM scan
        below ever probed, so a freshly installed Bonsai was invisible until the
        user typed `adk backend set` by hand (2026-09-12). The ladder lives in
        adk.local_inference.SELFHOST_PORTS; this only adopts an endpoint that
        lists a model (source env|port) and leaves MicroScheduler (:8150) to
        _try_desktop, which requires explicit config on purpose.
        """
        try:
            from ..local_inference import discover_local_endpoint
            found = await discover_local_endpoint()
        except Exception as e:  # noqa: BLE001 — detection is best-effort
            logger.debug("Self-host discovery failed: %s", e)
            return None
        if not found.found or found.source not in ("env", "port") or not found.endpoint_url:
            return None
        try:
            from .openai_compat import OpenAIProvider
            base = found.endpoint_url.rstrip("/")
            if not base.endswith("/v1"):
                base = f"{base}/v1"
            p = OpenAIProvider(
                base_url=base,
                api_key="not-needed",
                default_model=self._model or found.model or "",
                ctk_by_model=_DEFAULT_CTK_BY_MODEL,
            )
            if await p.health_check():
                # A configured model must actually be served here. Measured
                # 2026-09-23: with the spine (:8150) unreachable, discovery found
                # Ollama on :11434 and every turn asked it for bonsai2-27b ->
                # 'model not found' -> 'empty completion' in awsh. Same rule the
                # vLLM port scan already applies (Skipping localhost:%d).
                if self._model:
                    try:
                        served = await p.list_models()
                    except Exception:  # noqa: BLE001 — no list = cannot confirm
                        served = []
                    if served and self._model not in served:
                        logger.info(
                            "Skipping self-host %s: serves %s, not %s",
                            base, ", ".join(served[:4]), self._model,
                        )
                        return None
                self._provider_name = "local"
                logger.info(
                    "Auto-detected self-hosted inference at %s (model: %s, via %s)",
                    base, p.default_model, found.source,
                )
                return p
        except Exception as e:  # noqa: BLE001 — detection is best-effort
            logger.debug("Self-host endpoint %s rejected: %s", found.endpoint_url, e)
        return None

    async def _try_ollama(self) -> LLMProvider | None:
        """Try Ollama on localhost. Returns provider or None."""
        try:
            from .ollama import OllamaProvider
            host = (self._config.ollama_host if self._config else None) or "http://localhost:11434"
            model = self._model or ""
            p = OllamaProvider(host=host, default_model=model or "gemma4:4b")
            if await p.health_check():
                if model:
                    # An explicitly configured model Ollama does not have 404s every
                    # chat; skip so the ladder can reach a backend that serves it.
                    try:
                        installed_all = await p.list_models()
                    except Exception:  # noqa: BLE001 — no list = cannot confirm
                        installed_all = []
                    tagged = model if ":" in model else f"{model}:latest"
                    if installed_all and model not in installed_all and tagged not in installed_all:
                        logger.info(
                            "Skipping Ollama at %s: has %s, not %s",
                            host, ", ".join(installed_all[:4]), model,
                        )
                        return None
                if not model:
                    # Validate against what is actually installed — a hardcoded
                    # default that does not exist on this host 404s every chat
                    # (measured: a box with only llama3.2:3b was defaulted to a
                    # phantom gemma4:4b). Prefer a chat model over embedding tags.
                    try:
                        installed = [
                            m for m in await p.list_models()
                            if "embed" not in m.lower()
                        ]
                        if installed:
                            p.default_model = installed[0]
                    except Exception as e:  # noqa: BLE001 — list_models is best-effort
                        logger.debug("Ollama model list unavailable, keeping default: %s", e)
                self._provider_name = "ollama"
                logger.info("Auto-detected Ollama at %s (model: %s)", host, p.default_model)
                return p
        except Exception as e:  # noqa: BLE001 — detection is best-effort
            logger.debug("Ollama auto-detect failed: %s", e)
        return None

    async def _try_vllm(self) -> LLMProvider | None:
        """Try vLLM on configured URL, standard AitherOS ports, and vLLM default.

        vLLM is the PRIMARY local inference backend — it runs optimized containers
        on the user's GPU with proper batching, paged attention, and tensor parallelism.

        Priority: AITHER_VLLM_URL env → VLLM_URL env → port scan (8120, 8200-8203, 8000)
        """
        import os
        from .openai_compat import OpenAIProvider

        # Check explicit env var first. AITHER_LLM_BASE_URL is what the daemon
        # launchers (adk-daemon-start.cmd/.ps1) actually set; until 2026-09-10 it
        # was read only by the explicit-backend branch, so a launcher that named
        # MicroScheduler still landed in the port scan below.
        vllm_env = (
            os.environ.get("AITHER_VLLM_URL")
            or os.environ.get("VLLM_URL", "")
            or (getattr(self._config, "llm_base_url", "") if self._config else "")
            or os.environ.get("AITHER_LLM_BASE_URL", "")
        )
        # WHEN THE OPERATOR NAMED AN ENDPOINT, A SLOW ONE IS NOT AN ABSENT ONE.
        # health_check gives it 5 s; on a loaded host the named server routinely
        # takes longer, and the port scan below then finds something else in
        # milliseconds. Measured 2026-09-20 on the #agents responder: the same
        # process alternated between real answers and
        # "[agent error: 503 ... on-device inference failed (gemma3npc-1b)]",
        # because the fallback landed on :8200 -- which health-checks, lists
        # models and 503s every chat -- and that 503 was POSTED TO THE CHANNEL as
        # the agent's own reply. Set AITHER_LLM_STRICT_BASE_URL=1 where the
        # endpoint is policy (this platform routes every call through
        # MicroScheduler) and a named endpoint becomes the ONLY candidate:
        # a slow answer, never a different brain.
        strict = str(os.environ.get("AITHER_LLM_STRICT_BASE_URL", "")).strip().lower() in (
            "1", "true", "yes", "on")
        if vllm_env:
            try:
                url = vllm_env.rstrip("/")
                if not url.endswith("/v1"):
                    url = f"{url}/v1"
                p = OpenAIProvider(base_url=url, api_key="not-needed", default_model=self._model or "",
                                   ctk_by_model=_DEFAULT_CTK_BY_MODEL)
                if strict:
                    self._provider_name = "vllm"
                    logger.info("vLLM pinned by AITHER_LLM_STRICT_BASE_URL at %s", url)
                    return p
                if await p.health_check():
                    try:
                        models = await p.list_models()
                        if models and not self._model:
                            p.default_model = models[0]
                    except Exception:
                        pass
                    self._provider_name = "vllm"
                    logger.info("vLLM from env var at %s (model: %s)", url, p.default_model)
                    return p
            except Exception:
                pass

        # Build port list: standard ports + user-configured extras
        ports = [8120, 8200, 8201, 8202, 8203, 8000]
        extra_ports = (self._config.vllm_extra_ports if self._config else "") or os.environ.get("AITHER_VLLM_PORTS", "")
        if extra_ports:
            for p_str in extra_ports.split(","):
                p_str = p_str.strip()
                if p_str.isdigit() and int(p_str) not in ports:
                    ports.insert(0, int(p_str))  # User ports checked first

        for port in ports:
            try:
                url = f"http://localhost:{port}/v1"
                p = OpenAIProvider(
                    base_url=url,
                    api_key="not-needed",
                    default_model=self._model or "",
                    ctk_by_model=_DEFAULT_CTK_BY_MODEL,
                )
                if await p.health_check():
                    # Discover what model is loaded
                    models: list[str] = []
                    try:
                        models = await p.list_models()
                        if models and not self._model:
                            p.default_model = models[0]
                    except Exception:
                        pass
                    # A port that answers /v1/models is not thereby the server we
                    # want. localhost:8200 is Media Forge (xybrid local models):
                    # it health-checks, lists models, and 503s every chat for
                    # the requested model - so with the real backend down the daemon
                    # "auto-detected vLLM" there and every turn failed (2026-09-10).
                    # When a model was asked for, the port must actually serve it.
                    if self._model and models and self._model not in models:
                        logger.info(
                            "Skipping localhost:%d: serves %s, not %s",
                            port, ", ".join(models[:4]), self._model,
                        )
                        continue
                    self._provider_name = "vllm"
                    logger.info("Auto-detected vLLM at localhost:%d (model: %s)", port, p.default_model)
                    return p
            except Exception:
                continue

        # Check DGX Spark / remote vLLM endpoint
        dgx_url = (self._config.dgx_url if self._config else "") or os.environ.get("AITHER_DGX_URL", "")
        if dgx_url:
            try:
                dgx_base = dgx_url.rstrip("/")
                if not dgx_base.endswith("/v1"):
                    dgx_base = f"{dgx_base}/v1"
                p = OpenAIProvider(base_url=dgx_base, api_key="not-needed", default_model=self._model or "",
                                   ctk_by_model=_DEFAULT_CTK_BY_MODEL)
                if await p.health_check():
                    try:
                        models = await p.list_models()
                        if models and not self._model:
                            p.default_model = models[0]
                    except Exception:
                        pass
                    self._provider_name = "vllm"
                    logger.info("vLLM from DGX/remote at %s (model: %s)", dgx_base, p.default_model)
                    return p
            except Exception:
                pass

        return None

    async def _try_desktop(self) -> LLMProvider | None:
        """Try connecting to a desktop AitherOS MicroScheduler for remote inference.

        Reads AITHER_CORE_LLM_URL from env or ~/.aither/config.json. The saved
        config stores the spine under ``inference_url`` (the key ``adk setup``
        writes) — ``core_llm_url`` is honored as a legacy alias. The MicroScheduler
        serves TLS with the AitherNet internal CA, so the provider must be given
        ``verify=tls_verify()``; a default system-trust client fails the handshake
        and the whole desktop path silently returns None, letting a blind vLLM
        port scan shadow the configured spine.
        Returns an OpenAI-compatible provider pointing at the desktop's MicroScheduler.
        """
        import os
        from .openai_compat import OpenAIProvider
        from adk._tls import tls_verify
        from adk.config import load_saved_config

        # Check env var first, then saved config
        desktop_url = os.environ.get("AITHER_CORE_LLM_URL", "")
        default_model = self._model or ""
        if not desktop_url:
            try:
                saved = load_saved_config()
                desktop_url = (
                    saved.get("inference_url", "")
                    or saved.get("core_llm_url", "")
                )
                if not default_model:
                    default_model = saved.get("default_model", "") or ""
            except (OSError, ValueError):
                pass

        if not desktop_url:
            return None

        desktop_url = desktop_url.rstrip("/")
        if not desktop_url.endswith("/v1"):
            desktop_url = f"{desktop_url}/v1"

        token = os.environ.get("AITHER_NODE_TOKEN", "")
        if not token:
            try:
                saved = load_saved_config()
                token = saved.get("node_token", "")
            except (OSError, ValueError):
                pass

        try:
            p = OpenAIProvider(
                base_url=desktop_url,
                api_key=token or "not-needed",
                default_model=default_model,
                ctk_by_model=_DEFAULT_CTK_BY_MODEL,
                verify=tls_verify(),
            )
            if await p.health_check():
                try:
                    models = await p.list_models()
                    if models and not self._model:
                        p.default_model = models[0]
                except Exception:
                    pass
                logger.info("Connected to desktop MicroScheduler at %s (model: %s)", desktop_url, p.default_model)
                return p
        except Exception:
            pass
        return None

    async def _check_remote_health(self) -> bool:
        """Check if the remote desktop provider is healthy (30s cache)."""
        import time
        now = time.time()
        if now - self._remote_health_checked < 30.0:
            return self._remote_healthy
        self._remote_health_checked = now
        if self._remote_provider is None:
            self._remote_healthy = False
            return False
        try:
            self._remote_healthy = await self._remote_provider.health_check()
        except Exception:
            self._remote_healthy = False
        return self._remote_healthy

    def _setup_reasoning_backend(self) -> None:
        """Configure a separate provider for reasoning (effort 7+) if config specifies one.

        Hybrid mode: local Nemotron for effort 1-6, cloud API for effort 7-10.
        """
        cfg = self._config
        if not cfg:
            return
        backend = cfg.reasoning_backend
        if not backend:
            return

        # Resolve API key for the reasoning backend
        api_key = cfg.reasoning_api_key
        if not api_key:
            if backend == "anthropic":
                api_key = cfg.anthropic_api_key
            elif backend == "openai":
                api_key = cfg.openai_api_key
            elif backend == "deepseek":
                api_key = cfg.deepseek_api_key
            elif backend == "moonshot":
                api_key = cfg.moonshot_api_key
            elif backend == "gateway":
                api_key = cfg.aither_api_key

        if not api_key and backend not in ("vllm", "ollama"):
            logger.debug("Reasoning backend '%s' configured but no API key available", backend)
            return

        # Ollama uses native /api/chat, not OpenAI /v1 — strip /v1 suffix
        base_url = cfg.reasoning_base_url or None
        if base_url and backend == "ollama":
            base_url = base_url.rstrip("/").removesuffix("/v1")

        try:
            self._reasoning_provider = self._create_provider(
                backend,
                base_url=base_url,
                api_key=api_key,
            )
            self._reasoning_provider_name = backend
            self._reasoning_model = cfg.reasoning_model or None
            logger.info(
                "Hybrid reasoning: %s (model=%s) for effort 7+",
                backend, self._reasoning_model or "default",
            )
        except Exception as e:
            logger.warning("Failed to set up reasoning backend '%s': %s", backend, e)

    def _setup_cluster_backend(self) -> None:
        """Configure a dedicated cluster provider for effort 9+ (CPU inference).

        Grid mode: local GPU (1-6) → reasoning (7-8) → cluster (9-10).
        The cluster runs llama.cpp with --api-oai on CPU mini PCs.
        """
        cfg = self._config
        if not cfg:
            return
        backend = cfg.cluster_backend
        if not backend:
            return

        try:
            self._cluster_provider = self._create_provider(
                backend, base_url=cfg.cluster_base_url or None, api_key="not-needed",
            )
            self._cluster_provider_name = backend
            self._cluster_model = cfg.cluster_model or None
            logger.info(
                "Grid cluster: %s (model=%s) for effort 9+",
                backend, self._cluster_model or "default",
            )
        except Exception as e:
            logger.warning("Failed to set up cluster backend '%s': %s", backend, e)

    def _setup_perception_backend(self) -> None:
        """Configure a separate provider for perception/vision requests if config specifies one.

        Modality-based routing: vision/multimodal requests route to a
        specialized vision model (e.g., GPT-4V, Gemini, Claude Vision).
        """
        cfg = self._config
        if not cfg:
            return
        backend = cfg.perception_backend
        if not backend:
            return

        # Resolve API key for the perception backend
        api_key = cfg.perception_api_key
        if not api_key:
            if backend == "anthropic":
                api_key = cfg.anthropic_api_key
            elif backend == "openai":
                api_key = cfg.openai_api_key
            elif backend == "gemini":
                # Gemini has no Config field — read its own env var (the
                # GeminiProvider also self-resolves these). Do NOT borrow the
                # anthropic key: a wrong-provider key is a silent auth failure.
                api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
            elif backend == "gateway":
                api_key = cfg.aither_api_key

        if not api_key and backend not in ("vllm", "ollama"):
            logger.debug("Perception backend '%s' configured but no API key available", backend)
            return

        # Ollama uses native /api/chat, not OpenAI /v1 — strip /v1 suffix
        base_url = cfg.perception_base_url or None
        if base_url and backend == "ollama":
            base_url = base_url.rstrip("/").removesuffix("/v1")

        try:
            self._perception_provider = self._create_provider(
                backend,
                base_url=base_url,
                api_key=api_key,
            )
            self._perception_provider_name = backend
            self._perception_model = cfg.perception_model or None
            logger.info(
                "Perception backend: %s (model=%s) for vision/multimodal",
                backend, self._perception_model or "default",
            )
        except Exception as e:
            logger.warning("Failed to set up perception backend '%s': %s", backend, e)

    async def _try_cloud_apis(self) -> LLMProvider | None:
        """Try cloud API keys from Config (populated by provider_keys.json).

        Returns the first available cloud provider, preferring the order:
        Anthropic → OpenAI → DeepSeek (matches the quality tier ordering).
        """
        import os
        if self._config:
            if self._config.anthropic_api_key:
                self._provider_name = "anthropic"
                return self._create_provider("anthropic", api_key=self._config.anthropic_api_key)
            if self._config.openai_api_key:
                self._provider_name = "openai"
                return self._create_provider(
                    "openai", base_url=self._config.openai_base_url,
                    api_key=self._config.openai_api_key,
                )
            if self._config.deepseek_api_key:
                self._provider_name = "deepseek"
                return self._create_provider("deepseek", api_key=self._config.deepseek_api_key)
            if self._config.moonshot_api_key:
                self._provider_name = "moonshot"
                return self._create_provider("moonshot", api_key=self._config.moonshot_api_key)

        # Fallback: env vars
        for name, env in [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"), ("deepseek", "DEEPSEEK_API_KEY"), ("moonshot", "MOONSHOT_API_KEY")]:
            key = os.getenv(env, "")
            if key:
                self._provider_name = name
                kw = {"api_key": key}
                if name == "openai":
                    kw["base_url"] = os.getenv("OPENAI_BASE_URL")
                return self._create_provider(name, **kw)
        return None

    async def _auto_detect(self) -> LLMProvider:
        """Try backends in priority order:
        explicit → desktop (configured spine) → self-hosted (install-bonsai.sh :8080,
        `adk bonsai-local` :8090, ...) → vLLM scan → Ollama → gateway → cloud APIs → demo.

        LOCAL FIRST. Desktop MicroScheduler is tried before anything blind because it
        requires explicit config. A self-hosted server the user started comes next —
        see _try_local_selfhost. vLLM containers, then Ollama for AMD/Apple/no-Docker.
        Gateway is cloud fallback when no local GPU.

        When cloud_mode is "cloud_first" or "cloud_only" (set by `adk setup --mode cloud`),
        skip local probes and go straight to cloud API keys.
        """
        import os

        # 0. Explicit backend (--backend / AITHER_LLM_BACKEND / config) ALWAYS wins.
        # For a self-hosted agent the operator's chosen brain (deepseek, their own
        # vllm/ollama, etc.) must take precedence over gateway auto-selection — a
        # saved AITHER_API_KEY must not silently hijack an explicit --backend.
        # Default is "auto" (and "gateway" still flows through detection), so this
        # only fires when a concrete backend was explicitly requested.
        explicit = (
            (getattr(self._config, "llm_backend", "") or "").strip().lower()
            if self._config else ""
        )
        if explicit and explicit not in ("auto", "gateway"):
            key_for = {
                "deepseek": getattr(self._config, "deepseek_api_key", ""),
                "moonshot": getattr(self._config, "moonshot_api_key", ""),
                "openai": getattr(self._config, "openai_api_key", ""),
                "anthropic": getattr(self._config, "anthropic_api_key", ""),
                "gemini": getattr(self._config, "gemini_api_key", ""),
            }
            api_key = key_for.get(explicit, "") or os.getenv(f"{explicit.upper()}_API_KEY", "")
            # Guided onboarding: an explicitly chosen CLOUD backend with no key
            # is a dead end (it would 401 later with no hint) — fail NOW with
            # the exact setup steps instead of silently falling back.
            if not api_key and explicit in key_for:
                from .onboarding import missing_key_message
                raise ConnectionError(missing_key_message(explicit))
            # Self-hosted OpenAI-compatible backends (llamacpp/vllm/lmstudio) need a
            # base_url pointing at the operator's OWN server — otherwise
            # _create_provider falls back to _COMPAT_URLS' openai.com default and
            # the agent talks to the wrong brain. Read the generic llm_base_url
            # (env AITHER_LLM_BASE_URL) for that family; openai keeps its own field.
            _generic_base = (
                getattr(self._config, "llm_base_url", "") or os.getenv("AITHER_LLM_BASE_URL", "")
            )
            base_url_for = {
                "openai": getattr(self._config, "openai_base_url", "") or os.getenv("OPENAI_BASE_URL", ""),
                "llamacpp": _generic_base,
                "vllm": _generic_base,
                "lmstudio": _generic_base,
                "bonsai-local": _generic_base,
                "bonsai": _generic_base,
            }
            explicit_base_url = base_url_for.get(explicit, "") or None
            try:
                p = self._create_provider(explicit, base_url=explicit_base_url, api_key=api_key)
                self._provider_name = explicit
                logger.info(
                    "Using explicit backend '%s' (model=%s)",
                    explicit, self._model or "default",
                )
                return p
            except Exception as e:
                logger.warning(
                    "Explicit backend '%s' failed (%s); falling back to auto-detect",
                    explicit, e,
                )

        gateway_key = (
            (self._config.aither_api_key if self._config else "")
            or os.getenv("AITHER_API_KEY", "")
        )

        # Cloud-first / cloud-only mode: skip local GPU probes
        cloud_mode = getattr(self._config, "cloud_mode", "") if self._config else ""
        if cloud_mode in ("cloud_only", "cloud_first"):
            logger.info("Cloud mode '%s' — skipping local backend probes", cloud_mode)
            provider = await self._try_cloud_apis()
            if provider:
                return provider
            if cloud_mode == "cloud_only":
                raise ConnectionError(
                    "Cloud-only mode but no cloud API keys configured.\n\n"
                    "  Set keys:   adk keys set openai sk-...\n"
                    "  Or switch:  adk setup --mode auto\n"
                )
            # cloud_first: fall through to try local as fallback
            logger.info("Cloud-first: no cloud providers available, trying local backends")

        # 1. Desktop MicroScheduler — the AitherOS spine. Tried FIRST because it
        # requires explicit config (AITHER_CORE_LLM_URL env or config inference_url);
        # a configured spine must never be shadowed by a blind vLLM port scan that
        # picks up an unrelated local container (e.g. a swap model on a stray port).
        desktop = await self._try_desktop()
        if desktop:
            self._remote_provider = desktop
            self._remote_provider_name = "desktop"
            self._remote_healthy = True
            # Also try local Ollama as an explicit low-effort fast path.
            ollama = await self._try_ollama()
            if ollama:
                self._local_provider = ollama
                self._provider_name = "dual"
                logger.info(
                    "Dual-mode: local Ollama (%s) as low-effort fast path, "
                    "desktop MicroScheduler (%s) as the default brain",
                    getattr(ollama, "default_model", "?"),
                    getattr(desktop, "default_model", "?"),
                )
            else:
                self._provider_name = "desktop"
            # The configured desktop spine is the DEFAULT provider. A configured
            # spine must never be shadowed by local inference by default; the
            # local fast path is only reached on an explicit low effort (<=3),
            # routed in chat().
            return desktop

        # 2. A self-hosted server the user started (install-bonsai.sh :8080,
        # `adk bonsai-local` :8090, ...). Before the vLLM scan: those ports are
        # not in it, and a person who just ran the installer expects the agent
        # to use what they installed.
        selfhost = await self._try_local_selfhost()
        if selfhost:
            return selfhost

        # 3. vLLM containers — standalone local backend (best GPU utilization)
        vllm = await self._try_vllm()
        if vllm:
            return vllm

        # 3. Ollama — fallback local backend (AMD, Apple Silicon, no Docker)
        ollama = await self._try_ollama()
        if ollama:
            return ollama

        # 3. Gateway — cloud inference via gateway.aitherium.com
        if gateway_key:
            gateway_url = (
                (self._config.gateway_url if self._config else "")
                or os.getenv("AITHER_GATEWAY_URL", _GATEWAY_INFERENCE_URL)
            )
            if not gateway_url.endswith("/v1"):
                gateway_url = gateway_url.rstrip("/") + "/v1"
            try:
                from .openai_compat import OpenAIProvider
                p = OpenAIProvider(
                    base_url=gateway_url,
                    api_key=gateway_key,
                    default_model=self._model or "aither-orchestrator",
                    ctk_by_model=_DEFAULT_CTK_BY_MODEL,
                )
                if await p.health_check():
                    self._provider_name = "gateway"
                    logger.info("Connected to AitherOS gateway at %s", gateway_url)
                    return p
            except Exception:
                logger.debug("Gateway not reachable, trying cloud API keys")

        # 4. Cloud API keys (Anthropic/OpenAI direct)
        if self._config:
            if self._config.anthropic_api_key:
                self._provider_name = "anthropic"
                return self._create_provider(
                    "anthropic", api_key=self._config.anthropic_api_key
                )
            if self._config.openai_api_key:
                self._provider_name = "openai"
                return self._create_provider(
                    "openai",
                    base_url=self._config.openai_base_url,
                    api_key=self._config.openai_api_key,
                )

        if os.getenv("ANTHROPIC_API_KEY"):
            self._provider_name = "anthropic"
            return self._create_provider("anthropic", api_key=os.getenv("ANTHROPIC_API_KEY"))
        if os.getenv("OPENAI_API_KEY"):
            self._provider_name = "openai"
            return self._create_provider(
                "openai",
                base_url=os.getenv("OPENAI_BASE_URL"),
                api_key=os.getenv("OPENAI_API_KEY"),
            )
        if os.getenv("DEEPSEEK_API_KEY"):
            self._provider_name = "deepseek"
            return self._create_provider("deepseek", api_key=os.getenv("DEEPSEEK_API_KEY"))
        if self._config and self._config.deepseek_api_key:
            self._provider_name = "deepseek"
            return self._create_provider("deepseek", api_key=self._config.deepseek_api_key)
        if os.getenv("MOONSHOT_API_KEY"):
            self._provider_name = "moonshot"
            return self._create_provider("moonshot", api_key=os.getenv("MOONSHOT_API_KEY"))
        if self._config and self._config.moonshot_api_key:
            self._provider_name = "moonshot"
            return self._create_provider("moonshot", api_key=self._config.moonshot_api_key)

        # 5. PicoLM — edge inference (pure C, zero dependencies)
        picolm_binary = os.getenv("PICOLM_BINARY", "")
        picolm_model = os.getenv("PICOLM_MODEL", "")
        if picolm_binary and picolm_model:
            try:
                from .picolm import PicoLMProvider
                p = PicoLMProvider(binary=picolm_binary, model=picolm_model)
                if await p.health_check():
                    self._provider_name = "picolm"
                    logger.info("Auto-detected PicoLM at %s (model: %s)", picolm_binary, p.default_model)
                    return p
            except Exception:
                pass

        # 6. No backend available
        raise ConnectionError(
            "No LLM backend available.\n\n"
            "  Run setup:        python -m adk.setup\n"
            f"  Try the demo:     {_DEMO_URL}\n"
            "  Get an API key:   https://gateway.aitherium.com\n\n"
            + _onboarding_menu()
        )

    def _log_cost(self, resp, provider_name: str = "") -> None:
        """Append a cost entry to ~/.aither/cloud_costs.jsonl for local tracking."""
        import json as _json
        import os
        from datetime import datetime, timezone
        from pathlib import Path
        try:
            data_dir = self._config.data_dir if self._config else os.path.expanduser("~/.aither")
            log_path = Path(data_dir) / "cloud_costs.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Determine if this is a local or cloud request
            _prov = provider_name or self._provider_name
            is_local = _prov in ("ollama", "vllm", "llamacpp", "picolm", "desktop")
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "provider": "local" if is_local else _prov,
                "model": getattr(resp, "model", "") or "",
                "input_tokens": getattr(resp, "input_tokens", 0) or 0,
                "output_tokens": getattr(resp, "output_tokens", 0) or 0,
                "cost_usd": 0.0 if is_local else getattr(resp, "cost_usd", 0.0) or 0.0,
            }
            with open(log_path, "a") as f:
                f.write(_json.dumps(entry) + "\n")
        except Exception:
            pass  # Cost logging is best-effort, never crash

    async def get_provider(self) -> LLMProvider:
        """Return the active provider, auto-detecting if needed."""
        if self._provider is None:
            self._provider = await self._auto_detect()
            # Set up hybrid reasoning backend after primary detection
            if self._reasoning_provider is None:
                self._setup_reasoning_backend()
            # Set up cluster backend for grid mode (effort 9+)
            if self._cluster_provider is None:
                self._setup_cluster_backend()
            # Set up perception backend for vision/multimodal requests
            if self._perception_provider is None:
                self._setup_perception_backend()
        return self._provider

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def switch_backend(
        self,
        provider: str,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """Switch the LLM backend at runtime without recreating the agent.

        Usage:
            agent.llm.switch_backend("anthropic", api_key="sk-ant-...")
            agent.llm.switch_backend("deepseek")
            agent.llm.switch_backend("vllm", base_url="http://dgx-spark:8000/v1")
        """
        if model:
            self._model = model
        self._provider = self._create_provider(provider, base_url, api_key)
        self._provider_name = provider
        logger.info("Switched backend to %s (model=%s)", provider, model or "default")

    def set_reasoning_backend(
        self,
        provider: str,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """Set a separate backend for high-effort (7+) reasoning tasks.

        Usage:
            agent.llm.set_reasoning_backend("anthropic", api_key="sk-ant-...")
            agent.llm.set_reasoning_backend("deepseek")
        """
        self._reasoning_provider = self._create_provider(provider, base_url, api_key)
        self._reasoning_provider_name = provider
        self._reasoning_model = model
        logger.info("Set reasoning backend to %s (model=%s)", provider, model or "default")

    def set_failover_chain(self, providers: list[tuple[str, str | None, str | None]]) -> None:
        """Set backup providers tried when the primary fails (429/5xx/timeout).

        Args:
            providers: List of (provider_name, base_url, api_key) tuples.

        Usage:
            router.set_failover_chain([
                ("deepseek", None, deepseek_key),
                ("moonshot", None, moonshot_key),
                ("ollama", None, None),
            ])
        """
        self._failover_providers = []
        for name, url, key in providers:
            try:
                p = self._create_provider(name, url, key)
                self._failover_providers.append((name, p))
            except Exception as e:
                logger.warning("Failover provider %s failed to init: %s", name, e)
        names = [n for n, _ in self._failover_providers]
        logger.info("Failover chain: %s", " → ".join(names) if names else "none")

    def set_perception_backend(
        self,
        provider: str,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        """Set a separate backend for vision/multimodal (perception) requests.

        Usage:
            agent.llm.set_perception_backend("openai", api_key="sk-...")
            agent.llm.set_perception_backend("gemini", api_key="...")
        """
        self._perception_provider = self._create_provider(provider, base_url, api_key)
        self._perception_provider_name = provider
        self._perception_model = model
        logger.info("Set perception backend to %s (model=%s)", provider, model or "default")

    def get_backends(self) -> dict:
        """Return info about all configured backends."""
        info = {
            "primary": self._provider_name or "not initialized",
            "model": self._model or "auto",
        }
        if self._local_provider is not None:
            info["local"] = "ollama"
        if self._remote_provider is not None:
            info["remote"] = self._remote_provider_name
        if hasattr(self, "_reasoning_provider") and self._reasoning_provider is not None:
            info["reasoning"] = self._reasoning_provider_name
            if self._reasoning_model:
                info["reasoning_model"] = self._reasoning_model
        if hasattr(self, "_cluster_provider") and self._cluster_provider is not None:
            info["cluster"] = self._cluster_provider_name
            if self._cluster_model:
                info["cluster_model"] = self._cluster_model
        if hasattr(self, "_perception_provider") and self._perception_provider is not None:
            info["perception"] = self._perception_provider_name
            if self._perception_model:
                info["perception_model"] = self._perception_model
        return info

    def _has_image_content(self, messages: list[Message]) -> bool:
        """Check if any message contains image/visual content.

        Returns True if messages contain image attachments, base64 images,
        or image URLs that would benefit from a vision model.
        """
        for msg in messages:
            if not msg:
                continue
            # Check content field (could be string or list of content blocks)
            content = getattr(msg, "content", None)
            if content is None:
                continue
            # String content — check for common image URL patterns or base64
            if isinstance(content, str):
                if any(pat in content.lower() for pat in ("data:image", "http", ".jpg", ".png", ".gif", ".webp", "[image]")):
                    return True
            # List of content blocks (OpenAI format: [{"type": "image_url", ...}, ...])
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") in ("image", "image_url"):
                            return True
            # Check for attachments field (if present)
            attachments = getattr(msg, "attachments", None)
            if attachments:
                return True
        return False

    @property
    def model(self) -> "str | None":
        """The model name this router will send by default -- the pinned one, else the
        active provider's default. The ReAct loop budgets context against THIS name
        (``context_limit_for(getattr(self.llm, 'model', None))``). Measured 2026-09-21:
        without it the loop saw ``None`` for every backend, budgeted DeepSeek v4-pro
        (128k) and Bonsai (16k) alike as an unknown 32k, compacted DeepSeek at 16k
        tokens and never compacted Bonsai before its slot overflowed."""
        if self._model:
            return self._model
        prov = getattr(self, "_provider", None)
        return getattr(prov, "default_model", None) or None

    def model_for_effort(self, effort: int) -> str:
        """Select model based on effort level (1-10).

        Priority:
        1. Explicit model (from constructor or env)
        2. Config profile models (from hardware profile YAML)
        3. llmfit hardware-scored recommendations (if sidecar available)
        4. Static provider defaults (fallback)

        llmfit provides real hardware-scored model selection instead of
        static lookup tables. When available, it accounts for actual VRAM,
        dynamic quantization, MoE architectures, and speed estimation.
        """
        if self._model:
            return self._model

        effort = int(effort) if effort is not None else 5
        tier = "small" if effort <= 3 else "medium" if effort <= 6 else "large"

        # Check config profile models first (from hardware profile YAML)
        if self._config and getattr(self._config, "profile_models", None):
            pm = self._config.profile_models
            profile_map = {
                "small": pm.get("small", ""),
                "medium": pm.get("default", pm.get("chat", "")),
                "large": pm.get("large", pm.get("reasoning", "")),
            }
            if profile_map.get(tier):
                return profile_map[tier]

        # Hardware-scored recommendation — LOCAL providers only. The ODS catalog
        # is a library of local GGUF/Ollama models, so handing `qwen2.5-1.5b`
        # to the OpenAI or Anthropic API is a guaranteed 404. This path used to
        # be unreachable (a dead is_available() gate meant it always returned
        # None), which hid the fact that it applied to EVERY provider; removing
        # that gate made a cloud router start answering with local model ids.
        if self._provider_name in _HARDWARE_SCORED_PROVIDERS:
            llmfit_model = self._llmfit_model_for_tier(tier)
            if llmfit_model:
                return llmfit_model

        # Fall back to static provider defaults
        models = _EFFORT_MODELS.get(self._provider_name, {})
        return models.get(tier, self._model or "")

    @staticmethod
    def _llmfit_model_for_tier(tier: str) -> str | None:
        """Query llmfit for hardware-optimal model for a tier (cached).

        Maps ADK tiers to llmfit use_case categories:
        - small → chat (fast, low-latency)
        - medium → general (balanced)
        - large → reasoning (quality over speed)

        Returns Ollama-compatible model name or None if llmfit unavailable.
        """
        global _llmfit_models, _llmfit_checked

        if _llmfit_checked and _llmfit_models is not None:
            return _llmfit_models.get(tier)

        if _llmfit_checked:
            # Already tried, llmfit not available
            return None

        _llmfit_checked = True

        try:
            import asyncio
            from adk.llmfit import get_llmfit

            async def _fetch():
                fit = get_llmfit()
                # NO is_available() gate. That probes the external llmfit
                # binary/REST, but recommend_config() resolves from the vendored
                # ODS catalog offline with llmfit as optional refinement. Gating
                # on the probe meant that on any box WITHOUT llmfit — the normal
                # case — this returned None and the caller silently fell back to
                # the static per-provider table, so the resolver was never
                # reached. Same dead gate as the one removed from adk/setup.py.
                config = await fit.recommend_config()
                if "error" in config:
                    return None

                result = {}
                # Map: fast → small, balanced → medium, reasoning → large
                if config.get("fast") and config["fast"].get("model"):
                    result["small"] = config["fast"]["model"]
                if config.get("balanced") and config["balanced"].get("model"):
                    result["medium"] = config["balanced"]["model"]
                if config.get("reasoning") and config["reasoning"].get("model"):
                    result["large"] = config["reasoning"]["model"]
                return result if result else None

            # Try to run in existing event loop or create one
            try:
                loop = asyncio.get_running_loop()
                # Already in async context — can't await synchronously
                # Schedule as a task and return None for now;
                # the cache will populate on the next call from an async context
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, _fetch())
                    _llmfit_models = future.result(timeout=8)
            except RuntimeError:
                # No event loop running — safe to asyncio.run()
                _llmfit_models = asyncio.run(_fetch())

            if _llmfit_models:
                logger.info(
                    "llmfit models loaded: small=%s, medium=%s, large=%s",
                    _llmfit_models.get("small", "?"),
                    _llmfit_models.get("medium", "?"),
                    _llmfit_models.get("large", "?"),
                )
                return _llmfit_models.get(tier)

        except Exception as e:
            logger.debug("llmfit model selection unavailable: %s", e)

        return None

    @llm_retry(max_retries=5, base_delay_ms=500, max_delay_ms=16000)
    async def chat(
        self,
        messages: list[Message],
        model: str | None = None,
        effort: int | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        role: str | None = None,
        **kwargs,
    ) -> LLMResponse:
        """Route a chat request to the active provider.

        In dual-mode (local + desktop), routes based on effort:
        - effort 1-3: local provider (fast, small models)
        - effort 4+: remote desktop provider (large models, GPU)
        Falls back to local if remote is unreachable.

        If role="perception" or messages contain images, routes to the perception
        backend (if configured).
        """
        # OQ16 lever 2 — opt-in response cache. Only active when a ResponseCache was
        # attached AND this call passes cacheable=True. We recurse once with cacheable
        # popped so the cached thunk runs the normal routing below exactly as before.
        if self._response_cache is not None and kwargs.pop("cacheable", False):
            from .cache import cache_key
            key = cache_key(
                model or self._model or "", messages,
                kwargs.get("tools"), kwargs.get("temperature", 0.7),
            )
            return await self._response_cache.get_or_call(
                key,
                lambda: self.chat(
                    messages, model=model, effort=effort, tool_choice=tool_choice,
                    top_p=top_p, repetition_penalty=repetition_penalty, role=role, **kwargs,
                ),
            )

        # headroom context compression — flag-gated (AITHER_HEADROOM_ENABLED, default
        # OFF), sidecar-based, graceful no-op. Mirrors the AitherOS fleet's LLMGateway
        # pre-send hook so standalone / BYO adk agents get the same token savings against
        # their own backend. ONE point before every provider dispatch below. Never raises.
        try:
            from adk.compression import maybe_compress
            messages = await maybe_compress(messages, model=model)
        except Exception:  # never let compression break the LLM path
            pass

        # Perception routing: explicit role or image detection
        perception_prov = getattr(self, "_perception_provider", None)
        if perception_prov is not None and (role == "perception" or self._has_image_content(messages)):
            perception_model = getattr(self, "_perception_model", None) or model
            try:
                resp = await perception_prov.chat(
                    messages, model=perception_model, tool_choice=tool_choice,
                    top_p=top_p, repetition_penalty=repetition_penalty, **kwargs,
                )
                self._log_cost(resp, self._perception_provider_name)
                return resp
            except Exception as e:
                logger.warning("Perception backend failed, falling back to primary: %s", e)

        provider = await self.get_provider()
        if effort is not None:
            effort = int(effort)
        if model is None and effort is not None:
            model = self.model_for_effort(effort)

        # Grid cluster: effort 9+ → dedicated CPU cluster (big model, slow)
        cluster_prov = getattr(self, "_cluster_provider", None)
        if effort is not None and effort >= 9 and cluster_prov is not None:
            cluster_model = getattr(self, "_cluster_model", None) or model
            try:
                resp = await cluster_prov.chat(
                    messages, model=cluster_model, tool_choice=tool_choice,
                    top_p=top_p, repetition_penalty=repetition_penalty, **kwargs,
                )
                resp.effort_level = effort
                self._log_cost(resp, "cluster")
                return resp
            except Exception as e:
                logger.warning("Cluster backend failed, falling back to reasoning: %s", e)

        # Hybrid reasoning: effort 7+ → dedicated reasoning provider (cloud API)
        reasoning_prov = getattr(self, "_reasoning_provider", None)
        if (
            effort is not None
            and effort >= 7
            and reasoning_prov is not None
        ):
            reasoning_model = getattr(self, "_reasoning_model", None) or model
            try:
                resp = await reasoning_prov.chat(
                    messages, model=reasoning_model, tool_choice=tool_choice,
                    top_p=top_p, repetition_penalty=repetition_penalty, **kwargs,
                )
                resp.effort_level = effort
                self._log_cost(resp, self._reasoning_provider_name)
                return resp
            except Exception as e:
                logger.warning("Reasoning backend failed, falling back to primary: %s", e)

        # Dual-mode routing: high effort → remote desktop, low effort → local,
        # default (None) and mid effort → the default provider (the configured
        # desktop spine when present).
        if (
            effort is not None
            and effort <= 3
            and getattr(self, "_local_provider", None) is not None
        ):
            local = self._local_provider
            try:
                resp = await local.chat(
                    messages, model=model, tool_choice=tool_choice,
                    top_p=top_p, repetition_penalty=repetition_penalty, **kwargs,
                )
                resp.effort_level = effort
                self._log_cost(resp, "local")
                return resp
            except Exception as e:
                logger.warning("Local fast-path inference failed, falling back to primary: %s", e)

        if (
            effort is not None
            and effort > 3
            and self._remote_provider is not None
            and await self._check_remote_health()
        ):
            try:
                resp = await self._remote_provider.chat(
                    messages, model=model, tool_choice=tool_choice,
                    top_p=top_p, repetition_penalty=repetition_penalty, **kwargs,
                )
                resp.effort_level = effort
                self._log_cost(resp, self._remote_provider_name)
                return resp
            except Exception as e:
                logger.warning("Remote desktop inference failed, falling back to local: %s", e)
                self._remote_healthy = False

        try:
            resp = await provider.chat(
                messages, model=model, tool_choice=tool_choice,
                top_p=top_p, repetition_penalty=repetition_penalty, **kwargs,
            )
            if effort is not None:
                resp.effort_level = effort
            self._log_cost(resp, self._provider_name)
            return resp
        except Exception as primary_err:
            # Failover chain: try each backup provider in order
            if not self._failover_providers:
                raise
            import time
            if time.time() < self._failover_cooldown:
                raise
            for fb_name, fb_provider in self._failover_providers:
                try:
                    logger.warning("Primary (%s) failed: %s — trying %s", self._provider_name, primary_err, fb_name)
                    resp = await fb_provider.chat(
                        messages, model=None, tool_choice=tool_choice,
                        top_p=top_p, repetition_penalty=repetition_penalty, **kwargs,
                    )
                    if effort is not None:
                        resp.effort_level = effort
                    self._log_cost(resp, fb_name)
                    self._failover_cooldown = time.time() + 60
                    return resp
                except Exception:
                    continue
            raise primary_err

    async def chat_with_continuation(
        self,
        messages: list[Message],
        *,
        max_continuations: int | None = None,
        max_total_output_tokens: int | None = None,
        **chat_kwargs,
    ) -> LLMResponse:
        """``chat()`` that auto-continues+stitches a completion truncated at the
        output token cap. Router-level parity with ``LLMProvider`` so any
        call-site holding the router (not just the ReAct loop) inherits the one
        shared continuation primitive (``adk.llm.continuation``)."""
        from .continuation import run_continuation

        first = await self.chat(messages, **chat_kwargs)

        async def _again(_msgs: list[Message]) -> LLMResponse:
            return await self.chat(_msgs, **chat_kwargs)

        return await run_continuation(
            _again, messages, first,
            max_continuations=max_continuations,
            max_total_output_tokens=max_total_output_tokens,
        )

    async def chat_stream(
        self,
        messages: list[Message],
        model: str | None = None,
        effort: int | None = None,
        tool_choice: str | dict | None = None,
        top_p: float | None = None,
        repetition_penalty: float | None = None,
        **kwargs,
    ):
        """Stream a chat response with degeneration detection."""
        provider = await self.get_provider()
        if effort is not None:
            effort = int(effort)
        if model is None and effort is not None:
            model = self.model_for_effort(effort)
        detector = DegenerationDetector()
        async for chunk in provider.chat_stream(
            messages, model=model, tool_choice=tool_choice,
            top_p=top_p, repetition_penalty=repetition_penalty, **kwargs,
        ):
            if chunk.content and detector.feed(chunk.content):
                # Degeneration detected — signal done with special finish_reason
                yield StreamChunk(
                    content="", done=True, model=chunk.model,
                    finish_reason="degeneration",
                )
                return
            yield chunk

    async def list_models(self) -> list[str]:
        provider = await self.get_provider()
        return await provider.list_models()
