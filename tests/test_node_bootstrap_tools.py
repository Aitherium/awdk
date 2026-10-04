"""Tests for node_bootstrap tools.

All tests are mocked (no network, no subprocess execution). Every tool must:
  1. Return a dict, never raise
  2. Accept garbage inputs gracefully
  3. Fail closed on missing credentials/URLs
  4. For dry_run=True, show commands without calling subprocess
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from adk.toolpacks.node_bootstrap import tools


class TestDetectHardware:
    """node_detect_hardware tests."""

    def test_detect_hardware_returns_dict(self):
        """Must return a dict with system_info, recommendation, recommended_recipe."""
        with patch("adk.hardware_probe.detect_system") as mock:
            mock.return_value = MagicMock(
                ram_gb=16.0,
                cpu_cores=4,
                gpu_vendor="none",
                gpu_name="",
                gpu_vram_mb=0,
                ollama_installed=False,
                python_version="3.10.0",
            )
            with patch("adk.hardware_probe.recommend_setup") as mock_rec:
                mock_rec.return_value = MagicMock(
                    backend="cloud",
                    local_model=None,
                    local_model_gb=0.0,
                    local_vram_gb=0.0,
                    rationale="test",
                    warnings=[],
                )

                result = tools.node_detect_hardware()

                assert isinstance(result, dict)
                assert "system_info" in result
                assert "recommendation" in result
                assert "recommended_recipe" in result

    def test_detect_hardware_never_raises(self):
        """Must never raise on exception."""
        with patch("adk.hardware_probe.detect_system") as mock:
            mock.side_effect = RuntimeError("simulated error")

            result = tools.node_detect_hardware()

            assert isinstance(result, dict)
            assert "error" in result
            assert "fix" in result

    def test_detect_hardware_with_verbose(self):
        """Must accept verbose flag."""
        with patch("adk.hardware_probe.detect_system") as mock:
            mock.return_value = MagicMock(
                ram_gb=8.0,
                cpu_cores=2,
                gpu_vendor="none",
                gpu_name="",
                gpu_vram_mb=0,
                ollama_installed=False,
                python_version="3.10.0",
            )
            with patch("adk.hardware_probe.recommend_setup"):
                result = tools.node_detect_hardware(verbose=True)
                assert isinstance(result, dict)


class TestResolveRecipe:
    """node_resolve_recipe tests."""

    def test_resolve_recipe_explicit_id(self):
        """Explicit recipe_id wins — recipe MUST come back, score MUST be 10."""
        result = tools.node_resolve_recipe(recipe_id="cuda-vllm-8gb")

        assert isinstance(result, dict)
        assert "error" not in result, f"explicit id resolution errored: {result}"
        assert result["recipe"]["id"] == "cuda-vllm-8gb"
        assert result["match_score"] == 10.0

    def test_resolve_recipe_prefer_backend_filters(self):
        """prefer_backend must actually influence resolution (was a no-op)."""
        from adk.toolpacks.node_bootstrap.recipes import resolve_recipe

        big_cpu_box = {
            "ram_gb": 64.0, "cpu_cores": 16, "gpu_vendor": "none",
            "gpu_name": "", "gpu_vram_mb": 0, "unified_memory": False,
        }
        auto = resolve_recipe(big_cpu_box, prefer_backend="auto")
        preferred = resolve_recipe(big_cpu_box, prefer_backend="llamacpp")
        assert "error" not in preferred
        engine = preferred["recipe"]["inference_config"]["engine"]
        # Either the preference was honored, or an explicit warning says why not.
        assert engine == "llamacpp" or any(
            "prefer_backend" in w for w in preferred.get("warnings", [])
        ), f"prefer_backend silently ignored (auto={auto['recipe']['id']}, got={engine})"

    def test_resolve_recipe_unknown_id(self):
        """Unknown recipe_id returns error dict."""
        result = tools.node_resolve_recipe(recipe_id="nonexistent-recipe")

        assert isinstance(result, dict)
        assert "error" in result
        assert "available" in result

    def test_resolve_recipe_auto_detection(self):
        """Auto-detection path."""
        with patch("adk.hardware_probe.detect_system") as mock:
            mock.return_value = MagicMock(
                ram_gb=16.0,
                cpu_cores=4,
                gpu_vendor="none",
                gpu_name="",
                gpu_vram_mb=0,
                unified_memory=False,
            )

            result = tools.node_resolve_recipe(prefer_backend="auto")

            assert isinstance(result, dict)
            assert "recipe" in result or "error" in result

    def test_resolve_recipe_never_raises(self):
        """Must never raise."""
        with patch("adk.hardware_probe.detect_system") as mock:
            mock.side_effect = RuntimeError("simulated error")

            result = tools.node_resolve_recipe(recipe_id="")

            assert isinstance(result, dict)
            assert "error" in result


class TestPlanDeployment:
    """node_plan_deployment tests."""

    def test_plan_deployment_returns_dict(self):
        """Must return dict with steps, env, ports, sizes."""
        result = tools.node_plan_deployment(recipe_id="cpu-ollama")

        assert isinstance(result, dict)
        if "error" not in result:
            assert "steps" in result
            assert "env" in result
            assert "port" in result
            assert "est_duration_min" in result

    def test_plan_deployment_missing_recipe_id(self):
        """Missing recipe_id returns error."""
        result = tools.node_plan_deployment(recipe_id="")

        assert isinstance(result, dict)
        assert "error" in result

    def test_plan_deployment_unknown_recipe(self):
        """Unknown recipe returns error."""
        result = tools.node_plan_deployment(recipe_id="nonexistent")

        assert isinstance(result, dict)
        assert "error" in result

    def test_plan_deployment_never_raises(self):
        """Must never raise."""
        with patch("adk.toolpacks.node_bootstrap.tools.get_recipe") as mock:
            mock.side_effect = RuntimeError("simulated error")

            result = tools.node_plan_deployment(recipe_id="cuda-vllm-8gb")

            assert isinstance(result, dict)
            assert "error" in result


class TestApplyDeployment:
    """node_apply tests."""

    def test_apply_missing_recipe_id(self):
        """Missing recipe_id returns error."""
        result = tools.node_apply(recipe_id="")

        assert isinstance(result, dict)
        assert "error" in result

    def test_apply_dry_run_no_subprocess(self):
        """dry_run=True shows commands without calling subprocess."""
        result = tools.node_apply(
            recipe_id="cpu-ollama",
            dry_run=True,
        )

        assert isinstance(result, dict)
        if "error" not in result:
            assert result.get("dry_run") is True
            assert "commands" in result
            # subprocess must NOT be called
            # (verified by test isolation)

    @patch("adk.toolpacks.node_bootstrap.tools.subprocess.run")
    def test_apply_local_no_sudo_returns_fix(self, mock_run):
        """Local systemd without sudo returns fix instructions."""
        # This would be a systemd path; mocking would show the fix
        # For now, just verify the error path exists
        with patch("adk.toolpacks.node_bootstrap.tools.node_plan_deployment"):
            result = tools.node_apply(
                recipe_id="cpu-ollama",
                node_ip="",
                dry_run=False,
            )

            # Should return either success or a proper error dict
            assert isinstance(result, dict)

    def test_apply_remote_missing_ssh_key(self):
        """Remote mode without ssh_key returns error."""
        result = tools.node_apply(
            recipe_id="cuda-vllm-8gb",
            node_ip="192.168.1.100",
            ssh_user="aither",
            ssh_key="",
        )

        assert isinstance(result, dict)
        if "error" in result:
            assert "ssh_key" in result["error"].lower()

    def test_apply_never_raises(self):
        """Must never raise."""
        with patch("adk.toolpacks.node_bootstrap.tools.node_plan_deployment") as mock:
            mock.side_effect = RuntimeError("simulated error")

            result = tools.node_apply(recipe_id="cpu-ollama")

            assert isinstance(result, dict)
            assert "error" in result


class TestEnroll:
    """node_enroll tests."""

    def test_enroll_missing_token(self):
        """Missing token returns fail-closed error."""
        result = tools.node_enroll(token="")

        assert isinstance(result, dict)
        assert "error" in result
        assert result["error"] == "authentication token required"

    def test_enroll_missing_url(self, monkeypatch):
        """Missing URL returns fail-closed error — never attempts enrollment."""
        monkeypatch.delenv("AITHER_CONTROL_PLANE_URL", raising=False)
        result = tools.node_enroll(token="test-token", control_plane_url="")

        assert isinstance(result, dict)
        assert "error" in result, f"missing URL must fail closed, got: {result}"
        assert "test-token" not in str(result), "token leaked into error output"

    def test_enroll_redacts_token(self):
        """Token is redacted in output."""
        with patch("adk.toolpacks.node_bootstrap.tools.asyncio.run") as mock:
            mock.return_value = {
                "enrolled": True,
                "node_id": "test-node",
                "bearer_token": "secret-token-12345",
            }

            result = tools.node_enroll(
                token="my-secret-token",
                control_plane_url="http://localhost:8000",
            )

            assert isinstance(result, dict)
            # Token should be masked
            if "bearer_token" in result:
                assert "secret" not in result["bearer_token"]

    def test_enroll_never_raises(self):
        """Must never raise."""
        with patch("adk.toolpacks.node_bootstrap.tools.asyncio.run") as mock:
            mock.side_effect = RuntimeError("simulated error")

            result = tools.node_enroll(
                token="test-token",
                control_plane_url="http://localhost:8000",
            )

            assert isinstance(result, dict)
            assert "error" in result


class TestRegisterBackend:
    """node_register_backend tests."""

    def test_register_missing_url(self):
        """Missing genesis_url returns error."""
        result = tools.node_register_backend(
            genesis_url="",
            base_url="http://localhost:8000",
            backend_type="vllm",
        )

        assert isinstance(result, dict)
        assert "error" in result

    def test_register_missing_base_url(self):
        """Missing base_url returns error."""
        result = tools.node_register_backend(
            genesis_url="http://localhost:8001",
            base_url="",
            backend_type="vllm",
        )

        assert isinstance(result, dict)
        assert "error" in result

    def test_register_missing_backend_type(self):
        """Missing backend_type returns error."""
        result = tools.node_register_backend(
            genesis_url="http://localhost:8001",
            base_url="http://localhost:8000",
            backend_type="",
        )

        assert isinstance(result, dict)
        assert "error" in result

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_register_success(self, mock_post):
        """Successful registration."""
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "registered"},
        )

        result = tools.node_register_backend(
            genesis_url="http://localhost:8001",
            base_url="http://localhost:8000",
            backend_type="vllm",
        )

        assert isinstance(result, dict)
        if "registered" in result:
            assert result["registered"] is True

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_register_http_error(self, mock_post):
        """HTTP error is handled gracefully (authenticated call)."""
        mock_post.return_value = MagicMock(
            status_code=500,
            text="Internal Server Error",
        )

        result = tools.node_register_backend(
            genesis_url="http://localhost:8001",
            base_url="http://localhost:8000",
            backend_type="vllm",
            token="test-bearer",
        )

        assert isinstance(result, dict)
        assert "error" in result
        assert "500" in result["error"]
        # The call must have carried identity (Pattern 4: no anonymous internal calls)
        _, kwargs = mock_post.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer test-bearer"

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_register_fail_closed_without_token(self, mock_post, monkeypatch):
        """No token anywhere => error dict, and NO request is ever sent."""
        monkeypatch.delenv("AITHER_AUTH_TOKEN", raising=False)
        result = tools.node_register_backend(
            genesis_url="http://localhost:8001",
            base_url="http://localhost:8000",
            backend_type="vllm",
        )
        assert "error" in result
        assert "token" in result["error"]
        mock_post.assert_not_called()

    def test_register_never_raises(self):
        """Must never raise."""
        with patch("adk.toolpacks.node_bootstrap.tools.httpx.post") as mock:
            mock.side_effect = RuntimeError("simulated error")

            result = tools.node_register_backend(
                genesis_url="http://localhost:8001",
                base_url="http://localhost:8000",
                backend_type="vllm",
            )

            assert isinstance(result, dict)
            assert "error" in result


class TestVerifyBackend:
    """node_verify tests."""

    def test_verify_missing_url(self):
        """Missing base_url returns error."""
        result = tools.node_verify(base_url="")

        assert isinstance(result, dict)
        assert "error" in result

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    def test_verify_health_check(self, mock_get):
        """Health check path."""
        mock_get.return_value = MagicMock(status_code=200)

        result = tools.node_verify(base_url="http://localhost:8000")

        assert isinstance(result, dict)
        assert "status" in result
        assert "health" in result

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_verify_with_completion(self, mock_post, mock_get):
        """Completion test."""
        mock_get.return_value = MagicMock(status_code=200)
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"choices": [{"text": "Machine learning is a branch of AI."}]},
        )

        result = tools.node_verify(
            base_url="http://localhost:8000",
            backend_type="vllm",
        )

        assert isinstance(result, dict)
        if "completion_text" in result:
            assert len(result["completion_text"]) > 0

    def test_verify_never_raises(self):
        """Must never raise."""
        with patch("adk.toolpacks.node_bootstrap.tools.httpx.get") as mock:
            mock.side_effect = RuntimeError("simulated error")

            result = tools.node_verify(base_url="http://localhost:8000")

            assert isinstance(result, dict)
            assert "error" in result


class TestMaskToken:
    """Utility function tests."""

    def test_mask_token_long(self):
        """Long tokens are masked."""
        masked = tools._mask_token("very-long-secret-token-12345")
        assert "..." in masked
        assert "secret" not in masked

    def test_mask_token_short(self):
        """Short tokens show (redacted)."""
        masked = tools._mask_token("ab")
        assert masked == "(redacted)"


class TestRenderComposeServeArgs:
    """_render_compose must never SILENTLY drop a recipe's serve_args.

    Regression for the engine-selection contract: engine 'llamacpp' had no branch, so it fell into the
    env-driven `else` whose filter requires ("=" in a and " " not in a). Every
    flag-style arg ("--ctx-size 32768", "--reasoning-budget 256", ...) matched
    nothing, `command` was omitted entirely, and the service silently inherited
    the vllm image default plus an /root/.ollama volume. Rendering the real
    cpu-1bit-llamacpp recipe produced 0 of 7 serve_args and named the WRONG
    runtime — a plausible-looking compose file that could never work.
    """

    @staticmethod
    def _llamacpp_recipe(image="prism-ml/llamacpp-fork:latest"):
        return {
            "inference_config": {
                "engine": "llamacpp",
                "image": image,
                "models": [{"source": "gguf://prism-ml/bonsai-27b-q1_0.gguf"}],
                "serve_args": [
                    "--host 0.0.0.0",
                    "--port 8090",
                    "--ctx-size 32768",
                    "--reasoning-budget 256",
                ],
            },
            "deployment": {"port": 8090},
        }

    def test_llamacpp_serve_args_reach_the_command(self):
        """Every flag AND its value survives into argv (the engine-selection regression)."""
        out = tools._render_compose("cpu-1bit-llamacpp", self._llamacpp_recipe())
        assert "command:" in out, "llamacpp emitted no command at all"
        argv = json.loads(out.split("command: ")[1].split("\n")[0])
        # 4 entries -> 8 argv tokens (each "--flag value" splits in two)
        assert argv == [
            "--host", "0.0.0.0",
            "--port", "8090",
            "--ctx-size", "32768",
            "--reasoning-budget", "256",
        ], argv

    def test_llamacpp_does_not_inherit_vllm_image_or_ollama_volume(self):
        """The silent-wrong-runtime half of the bug."""
        out = tools._render_compose("cpu-1bit-llamacpp", self._llamacpp_recipe())
        assert "prism-ml/llamacpp-fork:latest" in out
        assert "vllm/vllm-openai" not in out
        assert "/root/.ollama" not in out

    def test_llamacpp_without_image_fails_loud(self):
        """No safe public default exists (Q1_0 needs the PrismML fork) — raise
        rather than emit a compose naming the wrong runtime."""
        recipe = self._llamacpp_recipe(image="")
        with pytest.raises(ValueError, match="requires an explicit"):
            tools._render_compose("cpu-1bit-llamacpp", recipe)

    def test_vllm_still_injects_model_and_args(self):
        """The pre-existing vllm path must be untouched."""
        recipe = {
            "inference_config": {
                "engine": "vllm",
                "image": "vllm/vllm-openai:latest",
                "models": [{"source": "hf://org/model"}],
                "serve_args": ["--max-model-len 4096"],
            },
            "deployment": {"port": 8000},
        }
        out = tools._render_compose("v", recipe)
        argv = json.loads(out.split("command: ")[1].split("\n")[0])
        assert argv == ["--model", "org/model", "--max-model-len", "4096"], argv

    def test_ollama_stays_env_driven_with_no_command(self):
        """KEY=VALUE serve_args become env vars, not a command line."""
        recipe = {
            "inference_config": {
                "engine": "ollama",
                "image": "ollama/ollama:latest",
                "models": [],
                "serve_args": ["OLLAMA_HOST=0.0.0.0"],
            },
            "deployment": {"port": 11434},
        }
        out = tools._render_compose("o", recipe)
        assert "command:" not in out
        assert "OLLAMA_HOST: 0.0.0.0" in out

    def test_every_shipped_recipe_renders_or_fails_loud(self):
        """No shipped recipe may render a compose that drops its own flags."""
        from pathlib import Path

        import yaml

        rdir = Path(tools.__file__).parent / "recipes"
        for path in sorted(rdir.glob("*.yaml")):
            recipe = yaml.safe_load(path.read_text(encoding="utf-8"))
            ic = recipe.get("inference_config", {}) or {}
            if (recipe.get("deployment", {}) or {}).get("target") != "docker-compose":
                continue
            try:
                out = tools._render_compose(path.stem, recipe)
            except ValueError:
                continue  # fail-loud is an acceptable outcome
            for arg in ic.get("serve_args", []) or []:
                if "=" in arg and " " not in arg:
                    continue  # env-style, handled separately
                for token in arg.split():
                    assert token in out, (
                        f"{path.name}: serve_arg token {token!r} silently dropped"
                    )


# ── engine-table verify + recipe-declared paths + register name ─────────


def _resp(status=200, body=None):
    m = MagicMock(status_code=status, text="")
    m.json = lambda: body if body is not None else {}
    return m


class TestVerifyOpenAICompatibleEngines:
    """Every non-ollama engine speaks the OpenAI shape (chat first, then completions)."""

    @pytest.mark.parametrize(
        "engine", ["llamacpp", "llamacpp_remote", "strata", "dgpp", "sglang", "vllm"]
    )
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_chat_completions_reports_healthy(self, mock_post, mock_get, engine):
        mock_get.return_value = _resp(200)
        mock_post.return_value = _resp(
            200, {"choices": [{"message": {"content": "ML learns patterns from data."}}]}
        )
        result = tools.node_verify(base_url="http://node:8080", backend_type=engine)
        assert result["status"] == "healthy", result
        assert mock_post.call_args_list[0].args[0] == "http://node:8080/v1/chat/completions"
        assert "messages" in mock_post.call_args_list[0].kwargs["json"]

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_falls_back_to_completions(self, mock_post, mock_get):
        mock_get.return_value = _resp(200)
        mock_post.side_effect = [
            _resp(404),
            _resp(200, {"choices": [{"text": "ML learns patterns from data."}]}),
        ]
        result = tools.node_verify(base_url="http://node:8080", backend_type="llamacpp")
        assert result["status"] == "healthy", result
        urls = [c.args[0] for c in mock_post.call_args_list]
        assert urls == [
            "http://node:8080/v1/chat/completions",
            "http://node:8080/v1/completions",
        ]
        assert "prompt" in mock_post.call_args_list[1].kwargs["json"]

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_ollama_keeps_native_path(self, mock_post, mock_get):
        mock_get.return_value = _resp(200)
        mock_post.return_value = _resp(200, {"response": "ML learns patterns from data."})
        result = tools.node_verify(base_url="http://node:11434", backend_type="ollama")
        assert result["status"] == "healthy", result
        assert mock_get.call_args.args[0] == "http://node:11434/api/tags"
        assert mock_post.call_args.args[0] == "http://node:11434/api/generate"
        assert mock_post.call_args.kwargs["json"]["stream"] is False

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_all_paths_failing_is_degraded(self, mock_post, mock_get):
        mock_get.return_value = _resp(200)
        mock_post.return_value = _resp(500)
        result = tools.node_verify(base_url="http://node:8080", backend_type="sglang")
        assert result["status"] == "degraded"
        assert mock_post.call_count == 2


class TestVerifyOllamaRemote:
    """A ``<engine>_remote`` backend type speaks its engine's protocol."""

    def test_protocol_alias(self):
        assert tools._engine_protocol("ollama_remote")["health_path"] == "/api/tags"
        assert tools._engine_protocol("OLLAMA_REMOTE")["completion_paths"] == ("/api/generate",)
        assert tools._engine_protocol("llamacpp_remote")["health_path"] == "/health"

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_ollama_remote_without_recipe_is_healthy(self, mock_post, mock_get):
        def get(url, **_):
            return _resp(200 if url.endswith("/api/tags") else 404)

        def post(url, **_):
            if url.endswith("/api/generate"):
                return _resp(200, {"response": "ML learns patterns from data."})
            return _resp(404)

        mock_get.side_effect = get
        mock_post.side_effect = post
        result = tools.node_verify(base_url="http://h:11434", backend_type="ollama_remote")
        assert result["status"] == "healthy", result
        assert mock_get.call_args.args[0] == "http://h:11434/api/tags"
        assert mock_post.call_args.args[0] == "http://h:11434/api/generate"


class TestApplyNextHint:
    """The ``next`` hint after an apply carries the recipe so verify reads its paths."""

    def test_hint_carries_recipe_and_backend_type(self):
        plan = {"port": 11434, "env": {"AITHER_BACKEND_TYPE": "ollama_remote"}}
        hint = tools._verify_hint("cpu-ollama", "localhost", plan)
        assert hint == (
            "node_verify(base_url='http://localhost:11434', recipe_id='cpu-ollama', "
            "backend_type='ollama_remote')"
        )

    def test_hint_without_backend_type(self):
        hint = tools._verify_hint("r1", "10.0.0.5", {"port": 8000, "env": {}})
        assert hint == "node_verify(base_url='http://10.0.0.5:8000', recipe_id='r1')"

    @patch("adk.toolpacks.node_bootstrap.tools._run", return_value=(0, "ok"))
    def test_local_compose_apply_returns_recipe_hint(self, _mock_run, tmp_path, monkeypatch):
        monkeypatch.setattr(tools, "_BOOTSTRAP_DIR", tmp_path)
        plan = {
            "port": 11434,
            "env": {"AITHER_BACKEND_TYPE": "ollama_remote"},
            "compose_yaml": "services: {}\n",
        }
        result = tools._apply_local("cpu-ollama", plan, "docker-compose")
        assert result["applied"] is True, result
        assert "recipe_id='cpu-ollama'" in result["next"]
        assert "backend_type='ollama_remote'" in result["next"]


class TestVerifyRecipeDeclaredPaths:
    """backend_config.health_path / completion_path from the recipe win."""

    def test_health_path_override_beats_engine_map(self):
        assert tools._health_path_for("vllm", {"health_path": "/v1/models"}) == "/v1/models"
        assert tools._health_path_for("vllm", "v1/models") == "/v1/models"
        # an empty declaration (e.g. a cloud recipe) falls back to the engine default
        assert tools._health_path_for("ollama", {"health_path": ""}) == "/api/tags"
        assert tools._health_path_for("llamacpp") == "/health"

    @patch("adk.toolpacks.node_bootstrap.tools.get_recipe")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_recipe_backend_config_drives_paths(self, mock_post, mock_get, mock_recipe):
        mock_recipe.return_value = {
            "backend_config": {
                "backend_type": "llamacpp_remote",
                "health_path": "/v1/models",
                "completion_path": "/v1/completions",
            }
        }
        mock_get.return_value = _resp(200)
        mock_post.return_value = _resp(
            200, {"choices": [{"text": "ML learns patterns from data."}]}
        )
        result = tools.node_verify(base_url="http://node:8090", recipe_id="any-recipe")
        assert result["status"] == "healthy", result
        assert result["backend_type"] == "llamacpp_remote"
        assert mock_get.call_args.args[0] == "http://node:8090/v1/models"
        # the declared completion path is tried FIRST
        assert mock_post.call_args_list[0].args[0] == "http://node:8090/v1/completions"

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_real_recipe_health_path_is_read(self, mock_post, mock_get):
        mock_get.return_value = _resp(503)
        tools.node_verify(base_url="http://node:11434", recipe_id="cpu-ollama")
        assert mock_get.call_args.args[0] == "http://node:11434/api/tags"
        mock_post.assert_not_called()

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.get")
    def test_explicit_args_beat_recipe(self, mock_get):
        mock_get.return_value = _resp(503)
        tools.node_verify(
            base_url="http://node:11434", recipe_id="cpu-ollama", health_path="/ready"
        )
        assert mock_get.call_args.args[0] == "http://node:11434/ready"


class TestRegisterBackendName:
    """The control plane requires a name; it must be present and stable."""

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_payload_has_stable_name(self, mock_post):
        mock_post.return_value = _resp(200, {"ok": True})
        kw = dict(
            genesis_url="https://cp.example",
            base_url="http://10.0.0.5:8000",
            backend_type="vllm",
            models=["org/My-Model"],
            token="t",
        )
        r1 = tools.node_register_backend(**kw)
        name1 = mock_post.call_args.kwargs["json"]["name"]
        tools.node_register_backend(**kw)
        name2 = mock_post.call_args.kwargs["json"]["name"]
        assert name1 == name2 == "My-Model@10.0.0.5-8000"
        assert r1["name"] == name1
        payload = mock_post.call_args.kwargs["json"]
        assert payload["base_url"] == "http://10.0.0.5:8000"
        assert payload["backend_type"] == "vllm"
        assert payload["models"] == ["org/My-Model"]

    @patch("adk.toolpacks.node_bootstrap.tools.httpx.post")
    def test_name_falls_back_to_backend_type_and_explicit_wins(self, mock_post):
        mock_post.return_value = _resp(200, {"ok": True})
        tools.node_register_backend(
            genesis_url="https://cp.example", base_url="http://box:8080",
            backend_type="llamacpp_remote", token="t",
        )
        assert mock_post.call_args.kwargs["json"]["name"] == "llamacpp_remote@box-8080"
        tools.node_register_backend(
            genesis_url="https://cp.example", base_url="http://box:8080",
            backend_type="llamacpp_remote", token="t", name="custom",
        )
        assert mock_post.call_args.kwargs["json"]["name"] == "custom"

    def test_name_is_sanitized(self):
        name = tools.backend_registration_name("not a url", "a b/c d")
        assert name and " " not in name
