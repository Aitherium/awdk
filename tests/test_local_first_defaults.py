"""awdk is local first: the AitherOS spine before anything, no paid-cloud or Ollama default.

Owner orders 2026-11-12: the DeepSeek API account answers 402 Payment Required, so a
DEEPSEEK_API_KEY in the env must never route a turn there; local work runs on the spine
(MicroScheduler, https://127.0.0.1:8150, AitherNet CA) fronting bonsai2-27b, gemma4-12b
and DeepSeek-V4 served locally on the DGX Spark (deepseek-v4-flash-pool). Never Ollama.
Interactive calls carry user priority. The awdk-demo unit carries the spine env durably
and its installer never uninstalls adk ahead of a pip flag the host rejects.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UNIT = REPO / ".DEPLOYMENT" / "standalone" / "systemd" / "awdk-demo.service"
INSTALLER = REPO / ".DEPLOYMENT" / "scripts" / "install-awdk-demo.sh"

_ENV = (
    "AITHER_BACKEND", "AITHER_VLLM_URL", "AITHER_LLAMACPP_URL", "AITHER_MICROSCHEDULER_URL",
    "AITHER_LOCAL_LLM_URL", "AITHER_CORE_LLM_URL", "ADK_ALLOW_PAID_CLOUD",
    "ADK_SCHEDULER_METADATA", "AITHER_SPECULATIVE", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY", "OLLAMA_HOST", "OLLAMA_URL", "OLLAMA_API_URL",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# -- adk.core.model.auto_backend ---------------------------------------------------------

def test_cloud_keys_in_env_never_beat_the_spine(clean_env):
    from adk.core.backends.microscheduler import MicroSchedulerBackend
    from adk.core.model import INTERACTIVE_MODELS, auto_backend

    clean_env.setenv("DEEPSEEK_API_KEY", "test-not-a-key")
    clean_env.setenv("OPENAI_API_KEY", "test-not-a-key")
    clean_env.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    b = auto_backend()
    assert isinstance(b, MicroSchedulerBackend)
    assert b.base_url == "https://127.0.0.1:8150"
    assert b.prefer == INTERACTIVE_MODELS
    assert b.local_only is True
    assert b.metadata["priority"] == "user"


def test_interactive_ladder_is_local_and_bonsai_first():
    from adk.core.model import INTERACTIVE_MODELS

    assert INTERACTIVE_MODELS[:2] == ("aither-orchestrator", "bonsai2-27b")
    assert "deepseek-v4-flash-pool" in INTERACTIVE_MODELS and "gemma4-12b" in INTERACTIVE_MODELS
    # the cloud ids (deepseek-chat / deepseek-v4-flash / or-*) are never on it
    assert all(m.endswith("-pool") or not m.startswith(("deepseek", "or-")) for m in INTERACTIVE_MODELS)


def test_configured_spine_url_is_forced_to_https(clean_env):
    from adk.core.model import auto_backend

    clean_env.setenv("AITHER_LOCAL_LLM_URL", "http://127.0.0.1:8150/v1")
    assert auto_backend().base_url == "https://127.0.0.1:8150"


def test_a_non_spine_local_url_is_not_mistaken_for_the_spine(clean_env):
    from adk.core.model import spine_url_from_env

    clean_env.setenv("AITHER_LOCAL_LLM_URL", "http://127.0.0.1:8092")
    assert spine_url_from_env() == ""


def test_deepseek_cloud_only_when_named(clean_env):
    from adk.core.backends.microscheduler import MicroSchedulerBackend
    from adk.core.backends.openai_compat import DeepSeekBackend
    from adk.core.model import auto_backend

    clean_env.setenv("DEEPSEEK_API_KEY", "test-not-a-key")
    clean_env.setenv("ADK_ALLOW_PAID_CLOUD", "1")
    # even with paid cloud allowed, a DeepSeek key alone is not auto-picked
    assert isinstance(auto_backend(), MicroSchedulerBackend)
    clean_env.setenv("AITHER_BACKEND", "deepseek")
    assert isinstance(auto_backend(), DeepSeekBackend)


def test_paid_openai_needs_the_opt_in(clean_env):
    from adk.core.backends.openai_compat import OpenAIBackend
    from adk.core.model import auto_backend

    clean_env.setenv("OPENAI_API_KEY", "test-not-a-key")
    assert not isinstance(auto_backend(), OpenAIBackend)
    clean_env.setenv("ADK_ALLOW_PAID_CLOUD", "1")
    assert isinstance(auto_backend(), OpenAIBackend)


def test_env_metadata_overrides_the_user_default(clean_env):
    from adk.core.model import default_scheduler_metadata

    assert default_scheduler_metadata("x") == {"priority": "user", "source": "x"}
    clean_env.setenv("ADK_SCHEDULER_METADATA", '{"priority": "agent", "source": "desk:awsh"}')
    assert default_scheduler_metadata() == {"priority": "agent", "source": "desk:awsh"}


def test_loader_spine_spec(clean_env):
    from adk.core.loader import _resolve_model

    b = _resolve_model("spine:bonsai2-27b")
    assert b.model == "bonsai2-27b" and b.base_url == "https://127.0.0.1:8150"


# -- MicroSchedulerBackend sends the hints -------------------------------------------------

def test_spine_call_carries_user_priority_and_local_only(clean_env):
    from adk.core.model import spine_backend

    b = spine_backend()
    sent = {}

    def fake_request(method, path, **kw):
        if path == "/v1/models":
            return {"data": [{"id": "deepseek-v4-flash"}, {"id": "gemma4-12b"}, {"id": "bonsai2-27b"}]}
        sent.update(kw["json"])
        return {"choices": [{"message": {"content": "ok"}}], "model": "bonsai2-27b",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    b._request = fake_request
    reply = b.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "ok"
    # "auto" took the interactive ladder, never the cloud id listed first
    assert sent["model"] == "bonsai2-27b"
    assert sent["metadata"]["priority"] == "user"
    assert sent["metadata"]["local_only"] is True
    assert sent["metadata"]["source"] == "adk"


# -- LLMRouter (adk.llm) ------------------------------------------------------------------

def test_router_cloud_ladder_skips_a_deepseek_key(clean_env):
    from adk.llm import LLMRouter

    clean_env.setenv("DEEPSEEK_API_KEY", "test-not-a-key")
    router = LLMRouter()
    router._config = None
    assert asyncio.run(router._try_cloud_apis()) is None


def test_router_desktop_reads_the_spine_env_and_picks_a_local_model(clean_env, monkeypatch):
    from adk.llm import LLMRouter
    from adk.llm import openai_compat as oc

    seen = {}

    async def healthy(self):
        seen["base"] = self.base_url
        return True

    async def models(self):
        return ["deepseek-v4-flash", "bonsai2-27b", "gemma4-12b"]

    monkeypatch.setattr(oc.OpenAIProvider, "health_check", healthy)
    monkeypatch.setattr(oc.OpenAIProvider, "list_models", models)
    monkeypatch.setattr("adk.config.load_saved_config", lambda: {})
    clean_env.setenv("AITHER_LOCAL_LLM_URL", "http://127.0.0.1:8150")
    router = LLMRouter()
    router._model = ""
    p = asyncio.run(router._try_desktop())
    assert p is not None
    assert seen["base"] == "https://127.0.0.1:8150/v1"
    assert p.default_model == "bonsai2-27b"


# -- local discovery: no Ollama port, no plain-http spine ---------------------------------

def test_discovery_never_probes_ollama_or_plain_http():
    from adk import local_inference as li

    assert 11434 not in li.SELFHOST_PORTS
    assert li.SCHEDULER_URLS and all(u.startswith("https://127.0.0.1:") for u in li.SCHEDULER_URLS)


# -- awdelegate ---------------------------------------------------------------------------

def test_awdelegate_fallback_and_synth_are_local():
    from adk import awdelegate as d

    cloud = {"deepseek-v4-flash", "deepseek-chat", "or-deepseek-v4.1-flash"}
    assert d.DEFAULTS["fallback_model"] not in cloud
    assert not cloud.intersection(d.DEFAULTS["synth_model"])
    assert d.DEFAULTS["fallback_model"] == "deepseek-v4-flash-pool"
    assert d.spine_address(d.DEFAULTS["spine"]) == ("https", "127.0.0.1", 8150)
    # a bare host:port (older delegate.json) is https; plain http never reaches :8150
    assert d.spine_address("127.0.0.1:8150") == ("https", "127.0.0.1", 8150)
    assert d.spine_address("http://127.0.0.1:8150/v1")[0] == "https"
    md = d.DEFAULTS["scheduler_metadata"]
    assert md["priority"] == "user" and md["local_only"] is True


# -- daemon + cli -------------------------------------------------------------------------

def test_daemon_defaults_to_the_spine_at_user_priority(clean_env):
    from adk.server import _apply_local_first_env_defaults

    clean_env.setattr("adk.core.backends.microscheduler._in_container", lambda: False)
    env: dict = {}
    applied = _apply_local_first_env_defaults(env)
    assert set(applied) == {"ADK_SCHEDULER_METADATA", "AITHER_LOCAL_LLM_URL"}
    assert json.loads(env["ADK_SCHEDULER_METADATA"])["priority"] == "user"
    assert env["AITHER_LOCAL_LLM_URL"] == "https://127.0.0.1:8150"
    # an operator's choice is never overridden
    env = {"ADK_SCHEDULER_METADATA": '{"priority": "agent"}', "AITHER_LLAMACPP_URL": "http://127.0.0.1:8092"}
    assert _apply_local_first_env_defaults(env) == {}
    assert env["ADK_SCHEDULER_METADATA"] == '{"priority": "agent"}'
    assert "AITHER_LOCAL_LLM_URL" not in env


def test_cli_bare_model_goes_to_the_spine_not_deepseek(clean_env, tmp_path, monkeypatch):
    from adk import cli

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    info = cli._resolve_explicit_backend(None, "bonsai2-27b")
    assert info["base_url"] == "https://127.0.0.1:8150/v1"
    assert info["model"] == "bonsai2-27b"
    assert info["provider"] == "vllm"
    assert "deepseek" not in info["display"].lower()


# -- durable awdk-demo unit + installer ---------------------------------------------------

def _unit_env() -> dict:
    env = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("Environment="):
            continue
        val = line[len("Environment="):]
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1].replace('\\"', '"')
        k, _, v = val.partition("=")
        env[k] = v
    return env


def test_unit_carries_the_spine_env():
    env = _unit_env()
    assert env["AITHER_LOCAL_LLM_URL"] == "https://127.0.0.1:8150"
    md = json.loads(env["ADK_SCHEDULER_METADATA"])
    assert md == {"priority": "user", "source": "desk:awsh"}
    assert env["AITHER_LLM_BASE_URL"].startswith("https://127.0.0.1:8150")


def test_installer_never_uninstalls_before_a_flag_the_host_may_reject():
    text = INSTALLER.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "pip uninstall" not in code
    # the flag is only ever added after probing this pip's --help
    flagged = [ln.strip() for ln in code.splitlines() if "--break-system-packages" in ln]
    assert flagged and all(ln.startswith(("if ", "PIP_FLAGS+=")) for ln in flagged), flagged
    assert "pip install --help" in code and "EXTERNALLY-MANAGED" in code
    assert "\r" not in text


# -- review findings 2026-11-12 (each failed on the builder's first cut) ------------------

def test_ollama_host_env_does_not_beat_the_spine(clean_env):
    from adk.core.backends.microscheduler import MicroSchedulerBackend
    from adk.core.model import auto_backend

    clean_env.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    b = auto_backend()
    assert isinstance(b, MicroSchedulerBackend), type(b)
    assert b.base_url == "https://127.0.0.1:8150"


def _cli_offline(tmp_path, monkeypatch, keys):
    import types

    import httpx

    import adk.config
    import adk.local_inference
    from adk import cli

    (tmp_path / ".aither").mkdir()
    (tmp_path / ".aither" / "provider_keys.json").write_text(json.dumps(keys))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(adk.config, "load_saved_config", lambda: {})

    async def _none(*a, **k):
        return types.SimpleNamespace(found=False, source="", endpoint_url="", model="")

    monkeypatch.setattr(adk.local_inference, "discover_local_endpoint", _none)

    def _down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _down)
    monkeypatch.delenv("AITHER_API_KEY", raising=False)
    return cli


def test_adk_start_never_auto_picks_a_saved_deepseek_key(tmp_path, clean_env):
    cli = _cli_offline(tmp_path, clean_env, {"deepseek": "test-not-a-key", "openrouter": "x"})
    info = cli._detect_llm_backend()
    assert "api.deepseek.com" not in (info.get("base_url") or "")
    assert "openrouter" not in (info.get("base_url") or "")
    assert info["base_url"] == "https://127.0.0.1:8150/v1"


def test_adk_start_prefers_a_live_spine(tmp_path, clean_env):
    cli = _cli_offline(tmp_path, clean_env, {"deepseek": "test-not-a-key"})
    import httpx

    class _R:
        status_code = 200

    seen = []

    def _get(url, *a, **k):
        seen.append(url)
        if url.startswith("https://127.0.0.1:8150"):
            return _R()
        raise httpx.ConnectError("down")

    clean_env.setattr(httpx, "get", _get)
    info = cli._detect_llm_backend()
    assert info["base_url"] == "https://127.0.0.1:8150/v1"
    assert seen and seen[0] == "https://127.0.0.1:8150/health"


def test_deepseek_model_shortcuts_land_on_the_local_pool(clean_env):
    from adk import cli

    for name in ("deepseek-flash", "deepseek-pro", "deepseek"):
        info = cli._resolve_explicit_backend(model=name)
        assert info["base_url"] == "https://127.0.0.1:8150/v1", name
        assert info["model"] == "deepseek-v4-flash-pool"


def test_llm_router_cloud_keys_need_the_paid_opt_in():
    import inspect

    from adk.llm import LLMRouter

    src = inspect.getsource(LLMRouter._auto_detect)
    step4 = src[src.index("# 4. Cloud API keys"):src.index("# 5. PicoLM")]
    gate = step4.index("ADK_ALLOW_PAID_CLOUD\") or")
    assert gate < step4.index("ANTHROPIC_API_KEY")
