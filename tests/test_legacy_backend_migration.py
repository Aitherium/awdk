"""A config written before the local model server was retired loads as llama.cpp.

Saved configs (``~/.aither/config.yaml`` / ``config.json``), env vars and agent
home configs from older releases name the retired backend ``ollama``; every
reader maps it to ``llamacpp`` (llama-server) instead of failing or silently
falling back to auto-detect.
"""
from __future__ import annotations

import json

import pytest

from adk import legacy_backend as lb


@pytest.fixture(autouse=True)
def _fresh_notice():
    lb._reset_for_tests()
    yield
    lb._reset_for_tests()


def test_backend_name_maps_to_llamacpp_case_insensitively():
    assert lb.migrate_backend_name("ollama") == "llamacpp"
    assert lb.migrate_backend_name(" Ollama ") == "llamacpp"
    assert lb.migrate_backend_name("vllm") == "vllm"
    assert lb.migrate_backend_name(None) is None


def test_saved_config_file_loads_as_llamacpp(tmp_path):
    from adk.config import load_saved_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "default_backend": "ollama",
        "reasoning_backend": "ollama",
        "ollama_host": "http://localhost:11434",
        "api_key": "keep-me",
    }), encoding="utf-8")
    cfg = load_saved_config(path)
    assert cfg["default_backend"] == "llamacpp"
    assert cfg["reasoning_backend"] == "llamacpp"
    assert "ollama_host" not in cfg
    assert cfg["llm_base_url"] == lb.LLAMACPP_DEFAULT_URL
    assert cfg["api_key"] == "keep-me"


def test_env_backend_loads_as_llamacpp_with_its_url(monkeypatch):
    from adk.config import Config

    monkeypatch.setenv("AITHER_LLM_BACKEND", "ollama")
    monkeypatch.delenv("AITHER_LLM_BASE_URL", raising=False)
    cfg = Config()
    assert cfg.llm_backend == "llamacpp"
    assert cfg.llm_base_url == lb.LLAMACPP_DEFAULT_URL


def test_router_given_the_old_name_builds_a_llamacpp_provider():
    from adk.llm import LLMRouter

    router = LLMRouter(provider="ollama")
    assert router.provider_name == "llamacpp"
    assert router._provider.base_url.startswith("http://127.0.0.1:8080")


def test_agent_home_config_with_the_old_provider_loads_as_llamacpp():
    from adk.home.config import ModelConfig

    m = ModelConfig(provider="ollama", base_url="http://localhost:11434", model="gemma4:4b")
    assert m.provider == "llamacpp"
    assert m.base_url == lb.LLAMACPP_DEFAULT_URL
    assert m.model == ""


def test_model_spec_with_the_old_backend_resolves_to_llamacpp():
    from adk.core.backends.openai_compat import LlamaCppBackend
    from adk.core.loader import _resolve_model

    backend = _resolve_model("ollama:default")
    assert isinstance(backend, LlamaCppBackend)


def test_one_notice_per_process(caplog):
    caplog.set_level("WARNING", logger="adk.legacy_backend")
    lb.migrate_backend_name("ollama")
    lb.migrate_backend_name("ollama")
    assert sum("retired local model server" in r.message for r in caplog.records) == 1
