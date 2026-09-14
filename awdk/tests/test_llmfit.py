"""Tests for the ADK llmfit integration."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adk.llmfit import LLMFitClient, ModelFit, get_llmfit


def _agent_viable_pool_size(hw: dict) -> int:
    """How many agent-viable models actually fit THIS machine.

    Every distinctness assertion below depends on it. A GitHub runner (~3GB
    envelope) has a pool of 2 — both Fast-specialty — so all roles legitimately
    converge there, while a workstation has ~19 and convergence would be the
    regression of that class. A constant bound cannot tell those apart, and these tests'
    envelope is whatever machine they run on.
    """
    from adk.llmfit import _to_ods_backend
    from adk.ods.hardware import classify_host
    from adk.ods.resolver import OdsResolver, is_agent_viable

    ram_gb = int(hw.get("total_ram_gb", 0) or 0)
    host = classify_host(
        gpu_name=hw.get("gpu_name", ""),
        cpu_name=hw.get("cpu_name", ""),
        vendor=_to_ods_backend(hw.get("backend")),
        memory_type="unified" if hw.get("unified_memory") else "discrete",
        vram_mb=int((hw.get("gpu_vram_gb", 0) or 0) * 1024),
        ram_gb=ram_gb,
    )
    env = OdsResolver()._envelope(  # noqa: SLF001 - test needs the candidate pool
        host.backend, host.memory_type, host.vram_mb, ram_gb, "qwen",
        host.tier, "x86_64", None, False,
    )
    return len([m for m in env.ranked if is_agent_viable(m)])


# ── ModelFit dataclass ────────────────────────────────────────────────────


class TestModelFit:
    def test_from_json_full(self):
        data = {
            "name": "deepseek-r1:14b",
            "provider": "ollama",
            "params_b": 14.0,
            "context_length": 16384,
            "use_case": "reasoning",
            "is_moe": False,
            "fit_level": "perfect",
            "run_mode": "gpu_full",
            "score": 0.87,
            "estimated_tps": 42.0,
            "best_quant": "Q4_K_M",
            "utilization_pct": 65.2,
            "score_components": {
                "quality": 0.85,
                "speed": 0.90,
                "fit": 0.88,
                "context": 0.82,
            },
        }
        m = ModelFit.from_json(data)
        assert m.name == "deepseek-r1:14b"
        assert m.provider == "ollama"
        assert m.params_b == 14.0
        assert m.context_length == 16384
        assert m.score == 0.87
        assert m.estimated_tps == 42.0
        assert m.best_quant == "Q4_K_M"
        assert m.score_quality == 0.85
        assert m.score_speed == 0.90
        assert m.score_fit == 0.88
        assert m.score_context == 0.82
        assert m.vram_used_pct == 65.2
        assert m.runnable is True

    def test_from_json_minimal(self):
        m = ModelFit.from_json({"name": "tiny"})
        assert m.name == "tiny"
        assert m.score == 0.0
        assert m.runnable is False  # fit_level defaults to "too_tight"

    def test_from_json_empty(self):
        m = ModelFit.from_json({})
        assert m.name == ""
        assert m.provider == ""
        assert m.runnable is False

    def test_runnable_levels(self):
        for level in ("perfect", "good", "marginal"):
            m = ModelFit.from_json({"fit_level": level})
            assert m.runnable is True, f"fit_level={level} should be runnable"

        for level in ("too_tight", "impossible", ""):
            m = ModelFit.from_json({"fit_level": level})
            assert m.runnable is False, f"fit_level={level} should NOT be runnable"


# ── LLMFitClient ─────────────────────────────────────────────────────────


class TestLLMFitClientResolveUrl:
    def test_default_port(self):
        """Default URL uses AitherOS convention port 8793."""
        with patch.dict("os.environ", {}, clear=True):
            client = LLMFitClient()
            assert "8793" in client._base_url

    def test_env_override(self):
        with patch.dict("os.environ", {"AITHER_LLMFIT_URL": "http://custom:9999"}):
            client = LLMFitClient()
            assert client._base_url == "http://custom:9999"

    def test_docker_mode(self):
        with patch.dict("os.environ", {"AITHER_DOCKER_MODE": "true"}, clear=True):
            client = LLMFitClient()
            assert "aither-llmfit" in client._base_url
            assert "8793" in client._base_url

    def test_explicit_url(self):
        client = LLMFitClient(base_url="http://test:1234")
        assert client._base_url == "http://test:1234"


class TestLLMFitClientHealth:
    @pytest.mark.asyncio
    async def test_available_when_healthy(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_httpx_client

        assert await client.is_available(force=True) is True

    @pytest.mark.asyncio
    async def test_unavailable_on_error(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(side_effect=ConnectionError("refused"))
        client._client = mock_httpx_client
        # `is_available` has TWO paths: the REST probe above and a CLI-binary
        # fallback (`_find_binary`). Mocking only the REST half left the verdict to
        # whether an `llmfit` binary happens to be on the machine, so this asserted
        # "unavailable" while testing the environment rather than the code — it
        # passed on hosted runners only because neither the port nor the binary was
        # there. Control both, or the test means nothing.
        client._find_binary = lambda: None

        assert await client.is_available(force=True) is False

    @pytest.mark.asyncio
    async def test_health_caching(self):
        """Health check result is cached for TTL period."""
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_httpx_client

        await client.is_available(force=True)
        await client.is_available()  # should use cache
        # Only one actual call due to caching
        assert mock_httpx_client.get.call_count == 1


class TestLLMFitClientSystemInfo:
    @pytest.mark.asyncio
    async def test_system_info_success(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "system": {
                "cpu_cores": 16,
                "cpu_name": "AMD Ryzen 9",
                "total_ram_gb": 64.0,
                "available_ram_gb": 48.0,
                "has_gpu": True,
                "gpu_name": "RTX 4090",
                "gpu_vram_gb": 24.0,
                "backend": "cuda_12",
                "unified_memory": False,
                "gpu_count": 1,
            }
        }
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_httpx_client

        info = await client.system_info()
        assert info is not None
        assert info["gpu_name"] == "RTX 4090"
        assert info["gpu_vram_gb"] == 24.0
        assert info["has_gpu"] is True
        assert info["cpu_cores"] == 16

    @pytest.mark.asyncio
    async def test_system_info_unavailable(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(side_effect=ConnectionError())
        client._client = mock_httpx_client

        info = await client.system_info()
        # CONTRACT CHANGE (ODS vendoring): an unreachable llmfit is no longer a
        # dead end. system_info() falls back to local psutil/probe detection, so
        # it returns real hardware instead of None. Asserting None here would
        # re-encode the old llmfit-required behaviour.
        assert info is not None
        assert info["cpu_cores"] > 0
        assert "backend" in info


class TestLLMFitClientModels:
    @pytest.mark.asyncio
    async def test_top_models(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "deepseek-r1:14b", "score": 0.9, "fit_level": "perfect",
                 "estimated_tps": 40.0, "score_components": {}},
                {"name": "llama3.2:3b", "score": 0.8, "fit_level": "good",
                 "estimated_tps": 80.0, "score_components": {}},
            ]
        }
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_httpx_client

        models = await client.top_models(use_case="coding", limit=5)
        assert len(models) == 2
        assert models[0].name == "deepseek-r1:14b"
        assert models[0].score == 0.9
        assert models[1].name == "llama3.2:3b"

    @pytest.mark.asyncio
    async def test_top_models_unavailable(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(side_effect=ConnectionError())
        client._client = mock_httpx_client
        # See test_unavailable_on_error: the CLI-binary fallback is the other half of
        # "unavailable", and without stubbing it this returned REAL models from a
        # live llmfit on the runner.
        client._find_binary = lambda: None

        models = await client.top_models()
        assert models == []

    @pytest.mark.asyncio
    async def test_best_for_task(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "best-model", "score": 0.95, "fit_level": "perfect",
                 "estimated_tps": 50.0, "score_components": {}},
            ]
        }
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_httpx_client

        best = await client.best_for_task(use_case="reasoning")
        assert best is not None
        assert best.name == "best-model"

    @pytest.mark.asyncio
    async def test_best_for_task_none_available(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(side_effect=ConnectionError())
        client._client = mock_httpx_client
        # See test_unavailable_on_error: without stubbing the CLI-binary fallback
        # this returned a REAL ModelFit from a live llmfit on the runner.
        client._find_binary = lambda: None

        best = await client.best_for_task()
        assert best is None

    @pytest.mark.asyncio
    async def test_search_model(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "deepseek-r1:14b", "score": 0.85, "fit_level": "good",
                 "estimated_tps": 35.0, "score_components": {}},
            ]
        }
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_httpx_client

        results = await client.search_model("deepseek")
        assert len(results) == 1
        assert results[0].name == "deepseek-r1:14b"


class TestLLMFitClientRecommendConfig:
    @pytest.mark.asyncio
    async def test_recommend_config_full(self):
        """Test full config recommendation across all tiers."""
        client = LLMFitClient(base_url="http://localhost:8793")

        system_resp = MagicMock()
        system_resp.status_code = 200
        system_resp.json.return_value = {
            "system": {
                "cpu_cores": 16,
                "total_ram_gb": 64.0,
                "available_ram_gb": 48.0,
                "has_gpu": True,
                "gpu_name": "RTX 4090",
                "gpu_vram_gb": 24.0,
                "backend": "cuda_12",
            }
        }

        # Each tier query returns a different top model
        tier_models = {
            "chat": {"name": "llama3.2:3b", "score": 0.92, "estimated_tps": 80.0},
            "general": {"name": "nemotron-orchestrator-8b", "score": 0.88, "estimated_tps": 40.0},
            "reasoning": {"name": "deepseek-r1:14b", "score": 0.85, "estimated_tps": 25.0},
            "coding": {"name": "qwen2.5-coder:14b", "score": 0.86, "estimated_tps": 30.0},
            "embedding": {"name": "nomic-embed-text", "score": 0.90, "estimated_tps": 100.0},
        }

        async def mock_get(path, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/system" in path:
                resp.json.return_value = system_resp.json.return_value
            else:
                # Determine use_case from params
                params = kwargs.get("params", {})
                use_case = params.get("use_case", "general")
                model = tier_models.get(use_case, tier_models["general"])
                resp.json.return_value = {
                    "models": [{
                        **model,
                        "fit_level": "perfect",
                        "provider": "ollama",
                        "params_b": 14.0,
                        "best_quant": "Q4_K_M",
                        "score_components": {},
                    }]
                }
            return resp

        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = mock_get
        client._client = mock_httpx_client

        # ODS is now the PRIMARY selector, so exercise the llmfit branch
        # explicitly — otherwise this llmfit-mocking test would silently stop
        # testing llmfit at all and just re-assert the ODS path.
        config = await client.recommend_config(use_llmfit=True)

        assert "hardware" in config
        assert config["hardware"]["gpu"] == "RTX 4090"
        assert config["hardware"]["vram_gb"] == 24.0

        assert config["fast"]["model"] == "llama3.2:3b"
        assert config["balanced"]["model"] == "nemotron-orchestrator-8b"
        assert config["reasoning"]["model"] == "deepseek-r1:14b"
        assert config["coding"]["model"] == "qwen2.5-coder:14b"
        assert config["embedding"]["model"] == "nomic-embed-text"

    @pytest.mark.asyncio
    async def test_recommend_config_unavailable(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(side_effect=ConnectionError())
        client._client = mock_httpx_client

        config = await client.recommend_config()
        # CONTRACT CHANGE (ODS vendoring): with llmfit unreachable, the vendored
        # ODS catalog still resolves offline. POSITIVE assertion — a real model id
        # per tier — so an inert resolver returning nothing fails this test.
        assert "error" not in config
        for tier in ("fast", "balanced", "reasoning", "coding", "embedding"):
            assert isinstance(config[tier], dict), f"{tier} tier missing"
            assert config[tier]["model"], f"{tier} tier has no model"

        generation_tiers = ("fast", "balanced", "reasoning", "coding")
        for tier in generation_tiers:
            assert config[tier]["provider"] == "ods"
        # CONTRACT CHANGE: the four generation tiers must not collapse
        # to ONE model — calling resolve() once per tier used to return the same
        # pick every time.
        #
        # The bound is HOST-DERIVED, because this test's envelope is whatever
        # machine it runs on. A GitHub runner (2 cores, 7GB RAM, no GPU) has a
        # ~3GB envelope where only two agent-viable models fit and both are
        # Fast-specialty, so roles converging there is CORRECT, not the
        # regression. Two constants were tried and both encoded this
        # workstation's hardware as a contract. The strong per-envelope
        # assertions live in adk/ods/tests/test_hardware_and_roles.py, where the
        # hardware is a fixture rather than an accident.
        distinct = {config[t]["model"] for t in generation_tiers}
        # Bound comes from the host, not a constant — see
        # _agent_viable_pool_size.
        if _agent_viable_pool_size(await client.system_info() or {}) >= 3:
            assert len(distinct) >= 2, config
        # CONTRACT CHANGE: the ODS catalog holds no embedding models, so this
        # tier is answered by the SDK's canonical embedder, not by a chat model
        # wearing an "embedding" label.
        assert config["embedding"]["provider"] == "aither-embeddings"
        assert config["embedding"]["model"] == "nomic-embed-text"


class TestLLMFitClientOllamaModels:
    @pytest.mark.asyncio
    async def test_recommended_ollama_models(self):
        client = LLMFitClient(base_url="http://localhost:8793")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "deepseek-r1:14b", "score": 0.9, "fit_level": "perfect",
                 "score_components": {}, "estimated_tps": 40.0},
                {"name": "llama3.2:3b", "score": 0.85, "fit_level": "good",
                 "score_components": {}, "estimated_tps": 80.0},
                # HF-style names are filtered out
                {"name": "meta-llama/Llama-3.2-3B", "score": 0.80, "fit_level": "good",
                 "score_components": {}, "estimated_tps": 60.0},
            ]
        }
        mock_httpx_client = AsyncMock()
        mock_httpx_client.get = AsyncMock(return_value=mock_resp)
        client._client = mock_httpx_client

        names = await client.recommended_ollama_models()
        assert "deepseek-r1:14b" in names
        assert "llama3.2:3b" in names
        # HF-style names should be excluded
        assert "meta-llama/Llama-3.2-3B" not in names


# ── Singleton ─────────────────────────────────────────────────────────────


class TestSingleton:
    def test_get_llmfit_returns_same_instance(self):
        """get_llmfit() should return the same client each time."""
        import adk.llmfit as llmfit_mod
        llmfit_mod._instance = None  # reset singleton

        c1 = get_llmfit()
        c2 = get_llmfit()
        assert c1 is c2

        llmfit_mod._instance = None  # cleanup

    def test_get_llmfit_with_custom_url(self):
        import adk.llmfit as llmfit_mod
        llmfit_mod._instance = None

        c = get_llmfit(base_url="http://custom:9999")
        assert c._base_url == "http://custom:9999"

        llmfit_mod._instance = None


# ── Integration with setup.py ─────────────────────────────────────────────


class TestSetupIntegration:
    @pytest.mark.asyncio
    async def test_recommended_models_llmfit_success(self):
        """_recommended_models_llmfit returns model names when llmfit is up."""
        from adk.setup import _recommended_models_llmfit

        mock_config = {
            "hardware": {"gpu": "RTX 4090", "vram_gb": 24},
            "fast": {"model": "llama3.2:3b"},
            "balanced": {"model": "nemotron-orchestrator-8b"},
            "reasoning": {"model": "deepseek-r1:14b"},
            "coding": {"model": "qwen2.5-coder:14b"},
            "embedding": None,
        }

        mock_client = AsyncMock()
        mock_client.is_available = AsyncMock(return_value=True)
        mock_client.recommend_config = AsyncMock(return_value=mock_config)

        with patch("adk.llmfit.get_llmfit", return_value=mock_client):
            models = await _recommended_models_llmfit()

        assert models is not None
        assert "nemotron-orchestrator-8b" in models
        assert "llama3.2:3b" in models
        assert "deepseek-r1:14b" in models
        assert "qwen2.5-coder:14b" in models
        assert "nomic-embed-text" in models  # always appended

    @pytest.mark.asyncio
    async def test_recommended_models_still_work_when_llmfit_unavailable(self):
        """An absent llmfit must NOT disable model recommendations.

        REGRESSION: this previously asserted `result is None` when
        `is_available()` was False, and `_recommended_models_llmfit` short-circuited
        on that gate. Once ODS became the offline primary selector, that gate meant
        every box without the llmfit binary — i.e. the normal case — silently fell
        back to the static model list and never reached the resolver. The gate is
        gone; an unreachable llmfit is no longer a failure.
        """
        from adk.llmfit import LLMFitClient
        from adk.setup import _recommended_models_llmfit

        # Use a REAL client with llmfit genuinely unreachable (REST dead, no CLI
        # binary) rather than an AsyncMock, so this exercises the actual offline
        # ODS resolution path instead of asserting against a stubbed return value.
        client = LLMFitClient(base_url="http://localhost:8793")
        dead = AsyncMock()
        dead.get = AsyncMock(side_effect=ConnectionError())
        client._client = dead
        client._find_binary = lambda: None
        client._available = None

        with patch("adk.llmfit.get_llmfit", return_value=client):
            assert await client.is_available() is False
            result = await _recommended_models_llmfit()

        # POSITIVE assertion: real model names come back with llmfit absent.
        assert result, "llmfit absent must still yield ODS-resolved models"
        assert all(isinstance(m, str) and m for m in result)
        assert "nomic-embed-text" in result  # always appended

    @pytest.mark.asyncio
    async def test_recommended_models_none_on_genuine_failure(self):
        """A real selection failure (error dict) still returns None — fail closed."""
        from adk.setup import _recommended_models_llmfit

        mock_client = AsyncMock()
        mock_client.is_available = AsyncMock(return_value=True)
        mock_client.recommend_config = AsyncMock(return_value={"error": "catalog missing"})

        with patch("adk.llmfit.get_llmfit", return_value=mock_client):
            result = await _recommended_models_llmfit()

        assert result is None

    @pytest.mark.asyncio
    async def test_recommended_models_llmfit_filters_hf_names(self):
        """HuggingFace-style names (with /) are excluded."""
        from adk.setup import _recommended_models_llmfit

        mock_config = {
            "hardware": {},
            "fast": {"model": "meta-llama/Llama-3.2-3B"},  # HF name
            "balanced": {"model": "nemotron-orchestrator-8b"},  # Ollama name
            "reasoning": None,
            "coding": None,
        }

        mock_client = AsyncMock()
        mock_client.is_available = AsyncMock(return_value=True)
        mock_client.recommend_config = AsyncMock(return_value=mock_config)

        with patch("adk.llmfit.get_llmfit", return_value=mock_client):
            models = await _recommended_models_llmfit()

        assert models is not None
        assert "nemotron-orchestrator-8b" in models
        # HF names should be filtered
        for m in models:
            assert "/" not in m

    def test_static_recommended_models_still_works(self):
        """Static fallback still returns models per profile."""
        from adk.setup import _recommended_models

        models = _recommended_models("nvidia_high")
        assert "nemotron-orchestrator-8b" in models
        assert "nomic-embed-text" in models

        models = _recommended_models("cpu_only")
        assert "gemma4:4b" in models

        models = _recommended_models("unknown_profile")
        assert models == _recommended_models("cpu_only")


# ── Config integration ────────────────────────────────────────────────────


class TestConfigIntegration:
    def test_config_has_llmfit_url(self):
        from adk.config import Config
        config = Config()
        assert hasattr(config, "llmfit_url")
        assert config.llmfit_url == ""  # default empty

    def test_config_llmfit_url_from_env(self):
        from adk.config import Config
        with patch.dict("os.environ", {"AITHER_LLMFIT_URL": "http://custom:9999"}):
            config = Config()
        assert config.llmfit_url == "http://custom:9999"

    def test_get_llmfit_client(self):
        from adk.config import Config
        config = Config()
        client = config.get_llmfit_client()
        assert client is not None
        # Cleanup singleton
        import adk.llmfit as llmfit_mod
        llmfit_mod._instance = None

    def test_get_llmfit_client_with_url(self):
        from adk.config import Config
        import adk.llmfit as llmfit_mod
        llmfit_mod._instance = None

        config = Config(llmfit_url="http://test:1234")
        client = config.get_llmfit_client()
        assert client is not None
        assert client._base_url == "http://test:1234"

        llmfit_mod._instance = None


class TestOdsBackendMapping:
    """Regression: versioned backends must not silently degrade to CPU."""

    def test_versioned_backends_map_to_vendor(self):
        from adk.llmfit import _to_ods_backend

        # Real probes report versioned backends. An exact-match map sent these
        # to "cpu", which sized the pick from RAM and handed a 24GB GPU a 3B model.
        assert _to_ods_backend("cuda_12") == "nvidia"
        assert _to_ods_backend("cuda_11") == "nvidia"
        assert _to_ods_backend("rocm_6") == "amd"
        assert _to_ods_backend("cpu_x86") == "cpu"
        assert _to_ods_backend("metal") == "apple"
        assert _to_ods_backend("sycl") == "intel"

    def test_unknown_backend_is_unknown_not_cpu(self):
        from adk.llmfit import _to_ods_backend

        # "unknown" preserves the reported VRAM envelope; "cpu" would discard it.
        assert _to_ods_backend("wgpu") == "unknown"
        assert _to_ods_backend(None) == "unknown"

    @pytest.mark.asyncio
    async def test_gpu_backend_is_not_sized_from_ram(self):
        """A 24GB GPU must not receive a tiny RAM-sized model."""
        from adk.llmfit import LLMFitClient

        client = LLMFitClient(base_url="http://localhost:8793")
        mock = AsyncMock()
        mock.get = AsyncMock(side_effect=ConnectionError())
        client._client = mock
        client._find_binary = lambda: None
        client._system_cache = {
            "backend": "cuda_12", "gpu_vram_gb": 24.0, "total_ram_gb": 64.0,
            "cpu_cores": 16, "gpu_name": "RTX 4090", "has_gpu": True,
        }
        client._system_cache_time = float("inf")

        config = await client.recommend_config()
        assert "error" not in config
        # 24GB VRAM must yield a substantial model, not a ~3B RAM-tier fallback.
        # Asserted on `balanced`, NOT `fast`: since the tier-contract change the fast tier is
        # deliberately a small low-latency model on every box, so it can no
        # longer discriminate a mis-sized envelope. With the backend map broken
        # (cuda_12 -> cpu) capacity collapses to 8GB and balanced drops to ~2B,
        # so this still catches the original defect.
        assert config["balanced"]["params_b"] >= 10, config["balanced"]
        # The host must also be classified off the real VRAM, not RAM.
        assert config["hardware"]["ods_tier"] == "T3", config["hardware"]

    @pytest.mark.asyncio
    async def test_unclassified_gpu_vendor_keeps_its_vram(self):
        """An Intel Arc host must not be sized from RAM.

        Upstream's heuristic ladder enumerates nvidia/amd/apple/none only — there
        is no `intel` vendor — so an Arc box classifies as cpu/T1. Adopting that
        backend discards its VRAM: a 16GB Arc dropped from a 12B model to a 3B
        one. Regression introduced when `backend` began coming from the
        classifier, caught by asking what happens to a vendor upstream omits.
        """
        from adk.llmfit import LLMFitClient

        client = LLMFitClient(base_url="http://localhost:8793")
        mock = AsyncMock()
        mock.get = AsyncMock(side_effect=ConnectionError())
        client._client = mock
        client._find_binary = lambda: None
        client._system_cache = {
            "backend": "sycl", "gpu_vram_gb": 16.0, "total_ram_gb": 32.0,
            "cpu_cores": 16, "gpu_name": "Intel Arc B580", "has_gpu": True,
        }
        client._system_cache_time = float("inf")

        config = await client.recommend_config()
        assert "error" not in config
        # 16GB of VRAM, not min(max(32*0.35, 3), 8) = 8GB of RAM.
        assert config["balanced"]["params_b"] >= 5, config["balanced"]
        # The classifier genuinely has no class for it — that is upstream's
        # behaviour and is faithfully reported; only the SIZING is preserved.
        assert config["hardware"]["ods_class"] == "unknown", config["hardware"]
        assert config["hardware"]["classified_backend"] == "cpu", config["hardware"]
        # ...and the dict must report the backend actually USED for sizing,
        # not the classification it diverged from.
        assert config["hardware"]["ods_backend"] == "intel", config["hardware"]
        # ...and must NOT publish the cpu_x86 default (70GB/s) as this card's
        # bandwidth. Upstream enumerates no `intel` vendor, so the classifier
        # falls back to that default; reporting it as fact is ~6x wrong for an
        # Arc B580. None is the honest answer.
        assert config["hardware"]["bandwidth_gbps"] is None, config["hardware"]


class TestLlmRouterTierResolution:
    """The SECOND dead `is_available()` gate, in adk/llm.

    `adk/setup.py` had a gate that probed the external llmfit binary before
    calling `recommend_config()` — which resolves offline from the vendored ODS
    catalog. On any box without llmfit (the normal case) it returned None and
    the caller silently used a static table, so the resolver was never reached.
    That gate was removed from setup.py; an identical one survived in
    `LLMRouter._llmfit_model_for_tier` and is removed here.
    """

    def test_tiers_resolve_offline_without_the_llmfit_binary(self):
        import adk.llm as llm_mod

        # The module caches globally; reset so this test actually resolves.
        llm_mod._llmfit_models = None
        llm_mod._llmfit_checked = False

        resolved = {
            tier: llm_mod.LLMRouter._llmfit_model_for_tier(tier)
            for tier in ("small", "medium", "large")
        }
        # POSITIVE assertion: real model names, not None. With the old gate in
        # place every one of these was None on a box without llmfit.
        for tier, model in resolved.items():
            assert model, f"{tier} did not resolve (dead is_available() gate is back?)"
            assert "/" not in model, f"{tier} returned an HF-style name: {model}"

        # small/medium/large map to fast/balanced/reasoning, which used
        # to be the same model three times. Bound is host-derived for the same
        # reason as in TestLLMFitClientRecommendConfig — on a machine where only
        # two agent-viable models fit, converging is the correct answer, not the
        # regression. The POSITIVE assertions above still run unconditionally.
        import asyncio

        from adk.llmfit import get_llmfit

        if _agent_viable_pool_size(asyncio.run(get_llmfit().system_info()) or {}) >= 3:
            assert len(set(resolved.values())) >= 2, resolved


class TestHardwareScoredProviderScope:
    """A local GGUF id must never be handed to a cloud provider.

    `LLMRouter.model_for_effort()` applied the ODS/llmfit pick to EVERY
    provider. That was invisible while a dead `is_available()` gate made the
    path unreachable; removing the gate made an `openai` router start answering
    `qwen2.5-1.5b-instruct`, which is a guaranteed 404 against the OpenAI API.
    Caught by the public repo's CI, not by the blast radius I had grepped.
    """

    def _fresh_router(self, **kwargs):
        import adk.llm as llm_mod

        llm_mod._llmfit_models = None
        llm_mod._llmfit_checked = False
        return llm_mod, llm_mod.LLMRouter(**kwargs)

    def test_cloud_providers_keep_their_own_catalog(self):
        _, router = self._fresh_router(provider="openai", api_key="sk-test")
        assert router.model_for_effort(1) == "gpt-4o-mini"
        assert router.model_for_effort(9) == "o1"

        _, router = self._fresh_router(provider="anthropic", api_key="sk-test")
        assert router.model_for_effort(1).startswith("claude-")

    def test_local_providers_do_get_the_hardware_scored_pick(self):
        """POSITIVE half — the feature must not be scoped into inertness."""
        llm_mod, router = self._fresh_router(provider="ollama")
        picked = router.model_for_effort(1)
        static_default = llm_mod._EFFORT_MODELS["ollama"]["small"]
        assert picked, "ollama resolved nothing"
        # It must be a REAL resolver answer, not the static table.
        assert picked != static_default, (
            "ollama fell back to the static default — the resolver is inert again"
        )

    def test_scope_set_contains_only_local_providers(self):
        import adk.llm as llm_mod

        assert "openai" not in llm_mod._HARDWARE_SCORED_PROVIDERS
        assert "anthropic" not in llm_mod._HARDWARE_SCORED_PROVIDERS
        # "dual" is deliberately excluded: only its small tier is local.
        assert "dual" not in llm_mod._HARDWARE_SCORED_PROVIDERS
        assert "ollama" in llm_mod._HARDWARE_SCORED_PROVIDERS
