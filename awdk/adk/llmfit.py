"""LLMFit integration — hardware-aware model recommendations for ADK agents.

Two-tier model selector combining ODS (vendored, deterministic) and llmfit
(external Rust CLI/TUI for optional refinement).

SELECTOR PRIORITY:
  1. ODS Resolver (PRIMARY) — deterministic, offline, no dependencies
     - Scores 200+ curated models across quality, speed, fit, context
     - Runs synchronously in Python (importlib.resources)
     - Caches results; fail-closed on catalog error

  2. llmfit (FALLBACK, optional) — extended model library, real-time scoring
     Execution modes (tried in order):
     a. REST API — if ``llmfit serve`` is running (or Docker sidecar up)
     b. CLI subprocess — shells out to ``llmfit`` binary with ``--json`` flags
     c. None — gracefully degrades if unavailable

HARDWARE DETECTION (system_info()):
  1. llmfit REST API
  2. llmfit CLI binary
  3. detect_system() fallback — psutil-based, offline

Upstream ODS:
    Repository: https://github.com/Osmantic/ODS
    License:    Apache License 2.0
    Reference:  adk/ods/adk_integration.md for detailed architecture

Upstream llmfit:
    Repository: https://github.com/AlexsJones/llmfit
    Author:     Alex Jones (@AlexsJones)
    License:    MIT

Usage:
    from adk.llmfit import get_llmfit, LLMFitClient

    # Singleton client (auto-resolves URL, falls back to CLI)
    fit = get_llmfit()

    # Check if llmfit is available (REST server or CLI binary)
    # Note: system_info() and recommend_config() degrade gracefully
    if await fit.is_available():
        # Get top models for coding tasks (from llmfit)
        models = await fit.top_models(use_case="coding", limit=5)
        for m in models:
            print(f"{m['name']} — score={m['score']}, tps={m['estimated_tps']}")

        # Get hardware info (tries ODS-internal detect, llmfit REST/CLI, then psutil)
        hw = await fit.system_info()
        print(f"GPU: {hw['gpu_name']} ({hw['gpu_vram_gb']}GB)")

        # Get recommended model for each ADK tier (ODS primary, llmfit fallback)
        config = await fit.recommend_config()
        print(f"Fast model: {config['fast']['model']} (source={config['fast'].get('source')})")
        print(f"Reasoning: {config['reasoning']['model']}")
"""

from __future__ import annotations

import json as _json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict

logger = logging.getLogger("adk.llmfit")

# Port 8793 is AitherOS convention; upstream default is 8787
_DEFAULT_PORT = 8793
_HEALTH_TTL = 30.0
_SYSTEM_TTL = 300.0
_MODELS_TTL = 60.0

# llmfit / hardware-probe backend name -> ODS backend vendor.
# Matched by PREFIX, because real probes report versioned backends ("cuda_12",
# "rocm_6"). An exact-match dict here silently mapped every versioned backend to
# "cpu", which made the resolver size the pick from system RAM (35%, capped at
# 8GB) instead of VRAM — a 24GB RTX 4090 was handed a 3B model while every log
# line and return value still reported success. Unrecognised backends must map to
# "unknown", NOT "cpu": upstream treats "unknown" as low-confidence and still
# uses the VRAM envelope when one was reported, whereas "cpu" throws the GPU away.
_ODS_BACKEND_PREFIXES = (
    ("cuda", "nvidia"),
    ("nvidia", "nvidia"),
    ("rocm", "amd"),
    ("hip", "amd"),
    ("amd", "amd"),
    ("metal", "apple"),
    ("apple", "apple"),
    ("mps", "apple"),
    ("sycl", "intel"),
    ("intel", "intel"),
    ("arc", "intel"),
    ("cpu", "cpu"),
)


def _canonical_embedding_tier() -> dict[str, Any]:
    """The embedding tier, which ODS structurally cannot answer.

    `adk.embeddings` is the SDK's single embedding provider (768-d
    nomic-embed-text) — every scope resolves through it so vectors stay
    portable. Reporting that here keeps `recommend_config()`'s five-tier shape
    honest instead of handing back a chat model with an "embedding" label.
    """
    try:
        from adk.embeddings import CANONICAL_DIM, CANONICAL_MODEL
    except ImportError:  # pragma: no cover - embeddings module is always present
        CANONICAL_MODEL, CANONICAL_DIM = "nomic-embed-text", 768
    return {
        "model": CANONICAL_MODEL,
        "provider": "aither-embeddings",
        "source": "canonical_constant",
        "score": 1.0,
        "estimated_tps": 0.0,
        "fit_level": "good",
        "best_quant": "",
        "params_b": 0.0,
        "dimension": CANONICAL_DIM,
        "reason": (
            "The ODS catalog contains no embedding models; embeddings resolve "
            "through adk.embeddings, the SDK's canonical 768-d provider."
        ),
    }


def _to_ods_backend(raw: str | None) -> str:
    """Map a probe-reported backend to an ODS backend vendor (prefix match)."""
    key = str(raw or "").strip().lower()
    if not key:
        return "unknown"
    for prefix, vendor in _ODS_BACKEND_PREFIXES:
        if key.startswith(prefix):
            return vendor
    logger.warning(
        "Unrecognised backend %r from hardware probe; treating as 'unknown' so the "
        "reported VRAM envelope is still honoured. Add a prefix to "
        "_ODS_BACKEND_PREFIXES if this is a real backend.", raw,
    )
    return "unknown"


@dataclass
class ModelFit:
    """A single model fit result from llmfit scoring."""
    name: str = ""
    provider: str = ""
    params_b: float = 0.0
    context_length: int = 0
    use_case: str = ""
    is_moe: bool = False
    fit_level: str = "too_tight"
    run_mode: str = "cpu_only"
    score: float = 0.0
    estimated_tps: float = 0.0
    best_quant: str = ""
    score_quality: float = 0.0
    score_speed: float = 0.0
    score_fit: float = 0.0
    score_context: float = 0.0
    vram_used_pct: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> ModelFit:
        sc = data.get("score_components", {})
        return cls(
            name=data.get("name", ""),
            provider=data.get("provider", ""),
            params_b=data.get("params_b", 0.0),
            context_length=data.get("context_length", 0),
            use_case=data.get("use_case", ""),
            is_moe=data.get("is_moe", False),
            fit_level=data.get("fit_level", "too_tight"),
            run_mode=data.get("run_mode", "cpu_only"),
            score=data.get("score", 0.0),
            estimated_tps=data.get("estimated_tps", 0.0),
            best_quant=data.get("best_quant", ""),
            score_quality=sc.get("quality", 0.0),
            score_speed=sc.get("speed", 0.0),
            score_fit=sc.get("fit", 0.0),
            score_context=sc.get("context", 0.0),
            vram_used_pct=data.get("utilization_pct", data.get("mem_pct", 0.0)),
            raw=data,
        )

    @property
    def runnable(self) -> bool:
        return self.fit_level in ("perfect", "good", "marginal")


class LLMFitClient:
    """Async client for llmfit — REST API with CLI subprocess fallback.

    Execution priority:
      1. REST API (``llmfit serve`` or Docker sidecar)
      2. CLI binary (``llmfit recommend --json``, ``llmfit --json system``, etc.)

    Resilient by design — all methods return sensible defaults when llmfit
    is unavailable so callers can degrade gracefully.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 5.0):
        self._base_url = (base_url or self._resolve_url()).rstrip("/")
        self._timeout = timeout
        self._client = None
        self._available: bool | None = None
        self._last_health: float = 0.0
        self._system_cache: dict | None = None
        self._system_cache_time: float = 0.0
        self._models_cache: dict[str, list[ModelFit]] = {}
        self._models_cache_time: float = 0.0

    @staticmethod
    def _resolve_url() -> str:
        """Resolve llmfit URL from env or convention."""
        url = os.environ.get("AITHER_LLMFIT_URL")
        if url:
            return url.rstrip("/")
        if os.environ.get("AITHER_DOCKER_MODE", "").lower() in ("1", "true"):
            return f"http://aither-llmfit:{_DEFAULT_PORT}"
        return f"http://localhost:{_DEFAULT_PORT}"

    @staticmethod
    def _find_binary() -> str | None:
        """Find the llmfit binary on PATH."""
        return shutil.which("llmfit")

    def _cli_run(self, args: list[str], timeout: float = 30.0) -> dict | None:
        """Run llmfit CLI with --json and return parsed JSON, or None."""
        binary = self._find_binary()
        if not binary:
            return None
        cmd = [binary] + args
        # Ensure --json is present for machine-readable output
        if "--json" not in cmd and "recommend" not in cmd:
            # recommend defaults to JSON; others need --json flag
            cmd.insert(1, "--json")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                logger.debug("llmfit CLI returned %d: %s", result.returncode, result.stderr[:200])
                return None
            return _json.loads(result.stdout)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug("llmfit CLI unavailable: %s", e)
            return None
        except _json.JSONDecodeError as e:
            logger.debug("llmfit CLI returned invalid JSON: %s", e)
            return None

    async def _get_client(self):
        if self._client is None:
            try:
                import httpx
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(self._timeout),
                    follow_redirects=True,
                )
            except ImportError:
                raise ImportError(
                    "httpx is required for llmfit integration. "
                    "Install with: pip install httpx"
                )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Health ──────────────────────────────────────────────────────────────

    async def is_available(self, force: bool = False) -> bool:
        """Check if llmfit is reachable (REST API or local CLI binary). Cached for 30s."""
        now = time.monotonic()
        if not force and self._available is not None and (now - self._last_health) < _HEALTH_TTL:
            return self._available
        # Try REST first
        try:
            client = await self._get_client()
            resp = await client.get("/health")
            self._available = resp.status_code == 200
        except Exception:
            self._available = False
        # Fall back to CLI binary detection
        if not self._available:
            self._available = self._find_binary() is not None
        self._last_health = now
        return self._available

    # ── System Info ─────────────────────────────────────────────────────────

    async def system_info(self) -> dict | None:
        """Get detected hardware specs. Cached for 5 minutes.

        Path priority:
          1. llmfit REST API
          2. llmfit CLI binary
          3. detect_system() fallback (psutil-based, offline)

        Returns dict or None if all unavailable.
        """
        now = time.monotonic()
        if self._system_cache and (now - self._system_cache_time) < _SYSTEM_TTL:
            return self._system_cache

        sys_data = None
        source = None

        # Try REST API first
        try:
            client = await self._get_client()
            resp = await client.get("/api/v1/system")
            if resp.status_code == 200:
                data = resp.json()
                sys_data = data.get("system", data)
                source = "llmfit_rest"
        except Exception as e:
            logger.debug("llmfit REST system info unavailable: %s", e)

        # Fall back to CLI: `llmfit --json system`
        if sys_data is None:
            cli_out = self._cli_run(["system"])
            if cli_out:
                sys_data = cli_out.get("system", cli_out)
                source = "llmfit_cli"

        # Final fallback: detect_system() (psutil-based, offline)
        if sys_data is None:
            try:
                from adk.hardware_probe import detect_system
                hw = detect_system()

                # Map gpu_vendor to backend
                gpu_vendor_map = {
                    "nvidia": "cuda",
                    "amd": "rocm",
                    "apple": "metal",
                    "none": "cpu_x86",
                }
                backend = gpu_vendor_map.get(hw.gpu_vendor, "cpu_x86")

                sys_data = {
                    "cpu_cores": hw.cpu_cores,
                    "cpu_name": "",
                    "total_ram_gb": hw.ram_gb,
                    "available_ram_gb": hw.ram_gb * 0.85,  # Estimate 85% available
                    "has_gpu": hw.gpu_vendor != "none",
                    "gpu_name": hw.gpu_name,
                    "gpu_vram_gb": hw.gpu_vram_mb / 1024.0 if hw.gpu_vram_mb else 0.0,
                    "backend": backend,
                    "unified_memory": hw.gpu_vendor == "apple",
                    "gpu_count": 1 if hw.gpu_vendor != "none" else 0,
                }
                source = "fallback_psutil"
            except ImportError:
                logger.debug("detect_system() unavailable, all hardware detection paths failed")
                return None
            except Exception as e:
                logger.debug("detect_system() fallback failed: %s", e)
                return None

        if sys_data:
            self._system_cache = {
                "cpu_cores": sys_data.get("cpu_cores", 0),
                "cpu_name": sys_data.get("cpu_name", ""),
                "total_ram_gb": sys_data.get("total_ram_gb", 0.0),
                "available_ram_gb": sys_data.get("available_ram_gb", 0.0),
                "has_gpu": sys_data.get("has_gpu", False),
                "gpu_name": sys_data.get("gpu_name", ""),
                "gpu_vram_gb": sys_data.get("gpu_vram_gb", 0.0),
                "backend": sys_data.get("backend", "cpu_x86"),
                "unified_memory": sys_data.get("unified_memory", False),
                "gpu_count": sys_data.get("gpu_count", 0),
                "raw": sys_data,
            }
            self._system_cache_time = now
            logger.debug("system_info() populated from %s", source)
            return self._system_cache
        return None

    # ── Model Queries ───────────────────────────────────────────────────────

    async def top_models(
        self,
        use_case: str | None = None,
        min_fit: str = "good",
        limit: int = 5,
        sort: str = "score",
    ) -> list[ModelFit]:
        """Get top-fitting models for this hardware."""
        cache_key = f"top:{use_case}:{min_fit}:{limit}:{sort}"
        now = time.monotonic()
        if cache_key in self._models_cache and (now - self._models_cache_time) < _MODELS_TTL:
            return self._models_cache[cache_key]

        models_list = None

        # Try REST API first
        try:
            client = await self._get_client()
            params: dict[str, Any] = {"min_fit": min_fit, "limit": limit, "sort": sort}
            if use_case:
                params["use_case"] = use_case

            resp = await client.get("/api/v1/models/top", params=params)
            if resp.status_code == 200:
                data = resp.json()
                models_list = data.get("models", data if isinstance(data, list) else [])
        except Exception as e:
            logger.debug("llmfit REST top_models unavailable: %s", e)

        # Fall back to CLI: `llmfit recommend --json --limit N [--use-case X]`
        if models_list is None:
            cli_args = ["recommend", "--json", "--limit", str(limit), "--min-fit", min_fit]
            if use_case:
                cli_args.extend(["--use-case", use_case])
            cli_out = self._cli_run(cli_args)
            if cli_out:
                models_list = cli_out.get("models", [])

        if models_list:
            results = [ModelFit.from_json(m) for m in models_list]
            self._models_cache[cache_key] = results
            self._models_cache_time = now
            return results
        return []

    async def search_model(self, query: str) -> list[ModelFit]:
        """Search for a model by name."""
        models_list = None
        # Try REST API first
        try:
            client = await self._get_client()
            resp = await client.get(f"/api/v1/models/{query}")
            if resp.status_code == 200:
                data = resp.json()
                models_list = data.get("models", data if isinstance(data, list) else [])
        except Exception as e:
            logger.debug("llmfit REST search unavailable: %s", e)

        # Fall back to CLI: `llmfit info "query" --json`
        if models_list is None:
            cli_out = self._cli_run(["info", query])
            if cli_out:
                models_list = cli_out.get("models", [])

        if models_list:
            return [ModelFit.from_json(m) for m in models_list]
        return []

    async def best_for_task(
        self,
        use_case: str = "general",
        min_tps: float = 0.0,
        min_fit: str = "good",
    ) -> ModelFit | None:
        """Get the single best model for a task type."""
        models = await self.top_models(use_case=use_case, min_fit=min_fit, limit=10)
        if min_tps > 0:
            models = [m for m in models if m.estimated_tps >= min_tps]
        return models[0] if models else None

    async def recommend_config(self, use_llmfit: bool = False) -> dict[str, Any]:
        """Generate hardware-optimized model configuration for ADK tiers.

        Uses ODS (deterministic, offline) as PRIMARY selector. Falls back to
        llmfit (optional refinement) if ODS unavailable or use_llmfit=True.

        Args:
            use_llmfit: If True, force llmfit path instead of ODS.

        Returns:
            {
                "hardware": {"gpu": ..., "vram_gb": ..., "backend": ...},
                "fast": {"model": ..., "score": ..., "tps": ..., "provider": "ods"|"llmfit"},
                "balanced": {"model": ..., ...},
                "reasoning": {"model": ..., ...},
                "coding": {"model": ..., ...},
                "embedding": {"model": ..., ...},
            }
            or {"error": "reason"} if both paths fail

            Each tier dict has consistent shape for backward compatibility.
            Never returns None — always returns dict, either valid config or error dict.
        """
        # Primary path: OdsResolver (deterministic, offline)
        if not use_llmfit:
            try:
                from adk.ods.hardware import classify_host  # noqa: E402
                from adk.ods.model_types import OdsError  # noqa: E402
                from adk.ods.resolver import OdsResolver

                resolver = OdsResolver()
                hw = await self.system_info()
                if hw:
                    probed_backend = _to_ods_backend(hw.get("backend"))
                    vram_mb = int((hw.get("gpu_vram_gb", 0) or 0) * 1024)
                    ram_gb = int(hw.get("total_ram_gb", 0) or 0)

                    # Classify the host against the vendored ODS hardware data.
                    # This is what makes `tier` real: passing tier=None pins it
                    # to "1", which makes upstream's Spark/GB10 arch-policy guard
                    # unreachable. known_gpus can also CORRECT the probe (a
                    # Strix Halo APU reports as discrete AMD but is unified).
                    # memory_type must be a REAL token, never "". The vendored
                    # classifier compares it exactly against the heuristic
                    # ladder, so an empty/unknown value matches no NVIDIA class
                    # and silently drops a 24GB GPU host to the cpu/T1 default —
                    # sized from RAM instead of VRAM. known_gpus still overrides
                    # this when it recognises the device.
                    host = classify_host(
                        gpu_name=hw.get("gpu_name", ""),
                        cpu_name=hw.get("cpu_name", ""),
                        vendor=probed_backend,
                        memory_type="unified" if hw.get("unified_memory") else "discrete",
                        vram_mb=vram_mb,
                        ram_gb=ram_gb,
                    )
                    ods_backend = host.backend
                    memory_type = host.memory_type
                    if host.vram_mb:
                        vram_mb = host.vram_mb

                    # An UNCLASSIFIED host must not lose its GPU. Upstream's
                    # heuristic ladder enumerates nvidia/amd/apple/none only —
                    # there is no `intel` vendor — so an Intel Arc box classifies
                    # as cpu/T1, and taking that backend would discard its VRAM
                    # and size from RAM: a 16GB Arc dropped from a 12B model to a
                    # 3B one. Upstream's own usable_memory_gb() sizes ANY backend
                    # key it doesn't recognise from VRAM, so keep the probed
                    # backend here and keep only the classifier's tier default.
                    # Only applies when the classifier matched NOTHING — a real
                    # match (including its cpu rungs) is authoritative.
                    if (
                        host.source == "unknown"
                        and probed_backend not in {"cpu", "unknown", "none"}
                        and vram_mb > 0
                    ):
                        logger.debug(
                            "ODS does not enumerate vendor %r; keeping the probed "
                            "backend so its %dMB of VRAM is not discarded.",
                            probed_backend, vram_mb,
                        )
                        ods_backend = probed_backend
                        memory_type = "unified" if hw.get("unified_memory") else "discrete"

                    config: dict[str, Any] = {
                        "hardware": {
                            "gpu": hw.get("gpu_name", ""),
                            "vram_gb": hw.get("gpu_vram_gb", 0),
                            "ram_gb": int(hw.get("total_ram_gb", 0) or 0),
                            "cpu_cores": hw.get("cpu_cores", 0),
                            "backend": hw.get("backend", ""),
                            "ods_class": host.id,
                            "ods_tier": host.tier,
                            # The backend/memory_type actually USED for sizing,
                            # which differs from host.backend when the probe is
                            # kept for a vendor upstream does not enumerate.
                            "ods_backend": ods_backend,
                            "memory_type": memory_type,
                            "classified_backend": host.backend,
                            "classified_by": host.source,
                            # Only when the host was actually placed. On an
                            # unclassified host (upstream enumerates no `intel`
                            # vendor, so every Arc/SYCL box lands here) the
                            # classifier returns the cpu_x86 DEFAULT of 70GB/s —
                            # ~6x wrong for an Arc B580. classify_host stays
                            # faithful to upstream; publishing that default as a
                            # measured fact is what would be dishonest. None
                            # says "unknown", which is true.
                            "bandwidth_gbps": (
                                host.bandwidth_gbps if host.source != "unknown" else None
                            ),
                            # NO compose_overlays here. classify_host() returns
                            # them (upstream's classifier does), but publishing
                            # them in THIS dict would promise a deployment input
                            # that nothing consumes: adk's own node_bootstrap
                            # deliberately renders self-contained compose from
                            # the recipe, because fleet-repo overlay templates do
                            # not ship in the public wheel. Callers deploying
                            # from ODS's own compose tree can read them off
                            # HostClass directly.
                        },
                    }

                    # One resolve PER ROLE. Calling resolve() five times with the
                    # same envelope returns the same model five times —
                    # upstream answers "one model for this box", not "one per
                    # role", so the role narrowing lives in resolve_role().
                    tier_failures = []
                    for tier_name in ("fast", "balanced", "reasoning", "coding"):
                        try:
                            ods_rec = resolver.resolve_role(
                                tier_name,
                                backend=ods_backend,
                                memory_type=memory_type,
                                vram_mb=vram_mb,
                                ram_gb=ram_gb,
                                profile="qwen",
                                tier=host.tier,
                            )
                            # Build complete tier dict with confidence/source tagging
                            config[tier_name] = {
                                "model": ods_rec.selected.llm_model_name,
                                "provider": "ods",
                                "source": "local_resolver",  # Confidence source tag
                                "score": ods_rec.confidence,
                                "estimated_tps": ods_rec.selected.tokens_per_sec_estimate,
                                "fit_level": "good",  # ODS always scores "good"
                                "best_quant": ods_rec.selected.quantization,
                                "params_b": ods_rec.selected.size_mb / 1000.0,
                                "specialty": ods_rec.selected.specialty,
                                "reason": ods_rec.reason,  # Human-readable explanation
                            }
                            logger.debug(
                                "ODS %s: %s (confidence=%.2f)",
                                tier_name,
                                ods_rec.selected.llm_model_name,
                                ods_rec.confidence,
                            )
                        except OdsError as e:
                            config[tier_name] = None
                            tier_failures.append(f"{tier_name}:{str(e)[:50]}")
                            logger.debug("ODS %s failed: %s", tier_name, e)

                    # The ODS catalog is a GENERATION-model library with zero
                    # embedding records — resolve_role("embedding") raises by
                    # design. Answer from the SDK's canonical embedder instead
                    # of letting a chat model masquerade as an embedding tier.
                    config["embedding"] = _canonical_embedding_tier()

                    # Did any tier ODS is actually responsible for succeed?
                    # Deliberately EXCLUDES "embedding": it is a static constant
                    # that never fails, so counting it would make this check
                    # vacuously true and the llmfit fallback unreachable.
                    successful_tiers = [
                        t for t in ["fast", "balanced", "reasoning", "coding"]
                        if config.get(t) is not None
                    ]
                    if successful_tiers:
                        logger.info(
                            "recommend_config() using ODS (deterministic resolver); "
                            "successful tiers: %s", ", ".join(successful_tiers)
                        )
                        return config
                    else:
                        # All tiers failed; fall through to llmfit fallback
                        logger.warning(
                            "ODS resolver failed for all tiers (failures: %s); "
                            "falling back to llmfit", ", ".join(tier_failures)
                        )
            except ImportError as e:
                logger.debug("ODS resolver import failed: %s", e)
                # Fall through to llmfit below
            except Exception as e:
                logger.debug("ODS resolver initialization failed: %s", e)
                # Fall through to llmfit below

        # Fallback path: llmfit (REST API or CLI binary)
        logger.debug("Falling back to llmfit for recommend_config()")
        hw = await self.system_info()
        if not hw:
            err_msg = "No hardware detection available (llmfit unavailable, psutil missing)"
            logger.warning(err_msg)
            return {"error": err_msg}

        tier_map = {
            "fast": ("chat", 30.0),
            "balanced": ("general", 10.0),
            "reasoning": ("reasoning", 5.0),
            "coding": ("coding", 10.0),
            "embedding": ("embedding", 0.0),
        }

        config: dict[str, Any] = {
            "hardware": {
                "gpu": hw.get("gpu_name", ""),
                "vram_gb": hw.get("gpu_vram_gb", 0),
                "ram_gb": int(hw.get("total_ram_gb", 0) or 0),
                "cpu_cores": hw.get("cpu_cores", 0),
                "backend": hw.get("backend", ""),
            },
        }

        # Ensure all tiers are populated (or None if unavailable)
        tier_failures = []
        for tier, (use_case, min_tps) in tier_map.items():
            try:
                best = await self.best_for_task(use_case=use_case, min_tps=min_tps)
                if best:
                    config[tier] = {
                        "model": best.name,
                        "provider": best.provider,
                        "source": "llmfit_binary" if self._find_binary() else "llmfit_rest",
                        "score": best.score,
                        "estimated_tps": best.estimated_tps,
                        "fit_level": best.fit_level,
                        "best_quant": best.best_quant,
                        "params_b": best.params_b,
                    }
                    logger.debug(
                        "llmfit %s: %s (score=%.2f)",
                        tier,
                        best.name,
                        best.score,
                    )
                else:
                    config[tier] = None
                    tier_failures.append(f"{tier}:no_fit_found")
                    logger.debug("llmfit %s: no suitable model found", tier)
            except Exception as e:
                config[tier] = None
                tier_failures.append(f"{tier}:{str(e)[:50]}")
                logger.debug("llmfit %s failed: %s", tier, e)

        logger.info(
            "recommend_config() using llmfit (REST/CLI); failures: %s",
            ",".join(tier_failures) if tier_failures else "none",
        )
        return config

    async def recommended_ollama_models(self, limit: int = 5) -> list[str]:
        """Get Ollama-compatible model names recommended for this hardware.

        Returns model names in Ollama format (e.g. 'deepseek-r1:14b'),
        useful for auto-pulling during setup.
        """
        models = await self.top_models(min_fit="good", limit=limit)
        # Filter for models that have Ollama-compatible names
        ollama_names = []
        for m in models:
            name = m.name.lower()
            # llmfit uses HuggingFace names — convert common patterns to Ollama
            if "/" in name:
                # e.g. "meta-llama/Llama-3.2-3B" → approximate
                continue
            # Already Ollama format (e.g. from Ollama provider detection)
            ollama_names.append(m.name)
        return ollama_names


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════════════════

_instance: LLMFitClient | None = None


def get_llmfit(base_url: str | None = None) -> LLMFitClient:
    """Get or create the singleton LLMFitClient."""
    global _instance
    if _instance is None:
        _instance = LLMFitClient(base_url=base_url)
    return _instance
