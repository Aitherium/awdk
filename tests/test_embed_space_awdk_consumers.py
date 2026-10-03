"""The awdk consumers of the embedding space follow adk.embeddings, not the nomic lane.

crystal's scheduler embedder, adk doctor / adk status probes, and the setup / deploy
model lists each hard-coded nomic-embed-text, 768-d or :8209. In the aither-code-embed
space that pulled a model of the retired space, probed a lane nothing serves, and let a
768-d vector into a 1024-d store. The nomic space (env unset) must stay byte-identical.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

_ENV = (
    "AITHER_EMBED_SPACE", "AITHER_EMBED_MODEL", "AITHER_EMBEDDINGS_URL",
    "ADK_EMBED_URL", "ADK_EMBED_MODEL", "AITHER_VLLM_PORTS",
)


@pytest.fixture
def space(monkeypatch):
    """``space(name="")`` -> adk.embeddings reloaded in that space (no saved config)."""
    import adk.config as cfg
    import adk.embeddings as mod

    monkeypatch.setattr(cfg, "load_saved_config", lambda *a, **k: {})
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)

    def _enter(name="", **env):
        if name:
            monkeypatch.setenv("AITHER_EMBED_SPACE", name)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(mod)

    yield _enter
    # Restore the runner's env and the real load_saved_config FIRST, then reload: a
    # reload under the patched {} config would leave later tests in the wrong space.
    monkeypatch.undo()
    importlib.reload(mod)


# ── crystal: model + width refusal ─────────────────────────────────────────


def _fake_httpx(monkeypatch, dim, seen):
    import httpx

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1] * dim}]}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            seen.append(json)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def _crystal_embed(texts):
    from adk.crystal import make_scheduler_embedder

    return asyncio.run(make_scheduler_embedder("https://sched.test/v1")(texts))


def test_crystal_nomic_space_is_unchanged(space, monkeypatch):
    space()
    seen: list = []
    _fake_httpx(monkeypatch, 768, seen)
    out = _crystal_embed(["a fact"])
    assert seen[0] == {"model": "nomic-embed-text", "input": "a fact"}
    assert out[0] is not None and len(out[0]) == 768


def test_crystal_code_embed_space_requests_the_canonical_model(space, monkeypatch):
    space("aither-code-embed", ADK_EMBED_MODEL="nomic-embed-text")  # stale leftover
    seen: list = []
    _fake_httpx(monkeypatch, 1024, seen)
    out = _crystal_embed(["x" * 5000])
    assert seen[0]["model"] == "aither-code-embed"
    assert len(seen[0]["input"]) == 1013  # the lane's per-request cut
    assert len(out[0]) == 1024


def test_crystal_code_embed_space_refuses_a_wrong_width_vector(space, monkeypatch):
    space("aither-code-embed")
    _fake_httpx(monkeypatch, 768, [])
    assert _crystal_embed(["a fact"]) == [None]


# ── doctor / status: the probe follows the space ───────────────────────────


def _probed_doctor_ports(monkeypatch):
    import urllib.request

    from adk import doctor

    seen: list = []

    def _urlopen(req, timeout=0):
        seen.append(req.full_url)
        raise OSError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(doctor, "_fail", lambda msg: seen.append(("fail", msg)))
    doctor.check_vllm()
    return seen


def test_doctor_nomic_space_probes_exactly_the_old_ports(space, monkeypatch):
    space()
    seen = _probed_doctor_ports(monkeypatch)
    assert [u for u in seen if isinstance(u, str)] == [
        f"http://localhost:{p}/v1/models" for p in (8000, 8201, 8202, 8203, 8209)]
    assert ("fail", "vLLM: no instances found on ports 8000, 8201-8203, 8209") in seen


def test_doctor_code_embed_space_also_probes_the_code_embed_lane(space, monkeypatch):
    space("aither-code-embed")
    seen = _probed_doctor_ports(monkeypatch)
    assert "http://localhost:8229/v1/models" in seen
    assert ("fail", "vLLM: no instances found on ports 8000, 8201-8203, 8209, 8229") in seen


def test_embed_lane_port_per_space(space):
    assert space().embed_lane_port() == 8209
    assert space("aither-code-embed").embed_lane_port() == 8229


def _run_status(monkeypatch, capsys, up_port):
    """Run `adk status` against a fake network where only ``up_port`` answers 200."""
    import httpx
    from adk import agent_daemon, cli, local_inference

    seen: list = []

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            seen.append(url)
            if f"localhost:{up_port}/" in url:
                return _Resp(200)
            raise httpx.ConnectError("refused")

    async def _no_local():
        raise RuntimeError("no local server")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(agent_daemon, "read_status", lambda: None)
    monkeypatch.setattr(local_inference, "discover_local_endpoint", _no_local)
    for k in ("AITHER_URL", "AITHER_VLLM_URL", "VLLM_URL", "OLLAMA_HOST",
              "AITHER_GATEWAY_URL", "AITHER_FLEET_QDRANT_URL", "AITHER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert cli.cmd_status(type("A", (), {"json": False})()) == 0
    return seen, capsys.readouterr().out


_SCAN = [f"http://localhost:{p}/health" for p in (8201, 8202, 8203, 8209)]


def test_status_code_embed_space_probes_and_labels_the_code_embed_lane(
        space, monkeypatch, capsys):
    space("aither-code-embed")
    seen, out = _run_status(monkeypatch, capsys, 8229)
    assert seen[-5:] == _SCAN + ["http://localhost:8229/health"]
    assert "[+] Embeddings   http://localhost:8229" in out


def test_status_nomic_space_scans_exactly_the_old_ports(space, monkeypatch, capsys):
    space()
    seen, out = _run_status(monkeypatch, capsys, 8209)
    assert seen[-4:] == _SCAN and "http://localhost:8229/health" not in seen
    assert "Embeddings" not in out


# ── setup / deploy: model lists and the generated compose ──────────────────


def test_setup_nomic_space_lists_are_unchanged(space):
    space()
    from adk import setup

    assert setup._recommended_models("cpu_only") == ["gemma4:4b", "nomic-embed-text"]


def test_setup_code_embed_space_swaps_the_embedder(space):
    space("aither-code-embed")
    from adk import setup

    models = setup._recommended_models("nvidia_high")
    assert "aither-code-embed" in models and "nomic-embed-text" not in models


def test_setup_pull_routes_code_embed_to_the_catalogue_not_ollama(space, monkeypatch):
    space("aither-code-embed")
    from adk import setup

    ran: list = []

    async def _run(cmd, timeout=0):
        ran.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(setup, "_run", _run)
    monkeypatch.setattr(setup, "_pull_code_embed", lambda m: ran.append(("gguf", m)) or True)
    s = setup.AgentSetup()
    s._system = type("S", (), {"ollama_models": []})()
    pulled = asyncio.run(s.pull_models(["gemma4:4b", "aither-code-embed"]))
    assert ("gguf", "aither-code-embed") in ran
    assert ["ollama", "pull", "aither-code-embed"] not in ran
    assert pulled == ["gemma4:4b", "aither-code-embed"]


def test_deploy_model_list_follows_the_space(space):
    from adk import deploy

    space()
    assert deploy._space_models(["gemma4:4b", "nomic-embed-text"]) == [
        "gemma4:4b", "nomic-embed-text"]
    space("aither-code-embed")
    assert deploy._space_models(["gemma4:4b", "nomic-embed-text"]) == [
        "gemma4:4b", "aither-code-embed"]


def test_deploy_overlay_embedding_env_follows_the_space(space):
    from adk import deploy

    space()
    nomic = deploy._generate_app_overlay("app", {"slug": "app"})
    assert 'EMBEDDING_MODEL: "nomic-embed-text"' in nomic
    assert "extra_hosts" not in nomic

    assert "code-embed" not in nomic

    space("aither-code-embed")
    ce = deploy._generate_app_overlay("app", {"slug": "app"})
    assert 'EMBEDDING_MODEL: "aither-code-embed"' in ce
    assert 'AITHER_EMBED_SPACE: "aither-code-embed"' in ce
    assert "nomic" not in ce


def test_deploy_overlay_code_embed_is_a_stack_service_not_the_host_lane(space):
    """The host lane binds 127.0.0.1 (models/serve.py), which a container on a native
    Linux engine cannot reach via host-gateway: the overlay runs its own service."""
    yaml = pytest.importorskip("yaml")

    from adk import deploy
    from adk.models import serve

    space("aither-code-embed")
    doc = yaml.safe_load(deploy._generate_app_overlay("app", {"slug": "app"}))
    app, svc = doc["services"]["app"], doc["services"]["code-embed"]
    assert "host.docker.internal" not in str(doc) and "extra_hosts" not in app
    assert app["environment"]["EMBEDDING_URL"] == "http://code-embed:8229/v1"
    assert app["depends_on"]["code-embed"] == {"condition": "service_healthy"}
    assert "ports" not in svc  # stack network only: nothing published to the host/LAN
    assert svc["networks"] == ["default"]
    cmd = svc["command"]
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--port") + 1] == "8229" and "--embedding" in cmd
    assert cmd[cmd.index("--model") + 1] == "/models/aither-code-embed.q8_0.gguf"
    host_dir = str(serve.models_dir()).replace(chr(92), "/")
    assert svc["volumes"] == [f"{host_dir}:/models:ro"]
    assert svc["image"].startswith("ghcr.io/ggml-org/llama.cpp@sha256:")


# ── an unknown space: installers keep nomic, they do not crash ─────────────


def test_unknown_space_keeps_the_nomic_install_paths(space, monkeypatch):
    import adk.embeddings

    space()
    monkeypatch.setenv("AITHER_EMBED_SPACE", "aither-code-embd")  # a typo
    with pytest.raises(ValueError):
        importlib.reload(adk.embeddings)  # memory writes still refuse to guess

    from adk import deploy, setup, setup_cli

    assert setup._recommended_models("cpu_only") == ["gemma4:4b", "nomic-embed-text"]
    assert deploy._space_models(["nomic-embed-text"]) == ["nomic-embed-text"]
    assert 'EMBEDDING_MODEL: "nomic-embed-text"' in deploy._generate_app_overlay(
        "app", {"slug": "app"})
    assert setup_cli._code_embed_model() == ""


# ── adk setup (setup_cli): the Ollama and vLLM paths `adk setup` really runs ──


def _gpu(vendor="none", vram_mb=0):
    from adk.setup_cli import GPUInfo

    return GPUInfo(vendor=vendor, name="test", vram_mb=vram_mb)


def _run_setup_ollama(monkeypatch, gpu):
    from adk import setup_cli

    ran: list = []
    monkeypatch.setattr(setup_cli.shutil, "which", lambda n: "/usr/bin/ollama")
    monkeypatch.setattr(setup_cli, "_run", lambda cmd, timeout=10: "NAME" + chr(10))
    monkeypatch.setattr(setup_cli.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(setup_cli, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(setup_cli, "_serve_code_embed",
                        lambda m, dry_run: ran.append(("gguf", m)))
    assert setup_cli.setup_ollama(gpu) == 0
    return ran


_GPUS = [("none", 0), ("amd", 1024)]  # every model fits / the low-VRAM fallback


@pytest.mark.parametrize("vendor,vram", _GPUS, ids=["cpu", "low-vram"])
def test_setup_cli_ollama_nomic_space_pulls_nomic(space, monkeypatch, vendor, vram):
    space()
    ran = _run_setup_ollama(monkeypatch, _gpu(vendor, vram))
    assert ["ollama", "pull", "nomic-embed-text"] in ran
    assert not [r for r in ran if r[0] == "gguf"]


@pytest.mark.parametrize("vendor,vram", _GPUS, ids=["cpu", "low-vram"])
def test_setup_cli_ollama_code_embed_space_never_pulls_nomic(
        space, monkeypatch, vendor, vram):
    space("aither-code-embed")
    ran = _run_setup_ollama(monkeypatch, _gpu(vendor, vram))
    assert ("gguf", "aither-code-embed") in ran
    assert not [r for r in ran if "nomic-embed-text" in r]
    assert ["ollama", "pull", "aither-code-embed"] not in ran


def test_setup_cli_vllm_compose_follows_the_space(space, monkeypatch):
    from adk import setup_cli

    monkeypatch.setattr(setup_cli, "_port_in_use", lambda port: False)
    space()
    assert "nomic-ai/nomic-embed-text-v1.5" in setup_cli.generate_compose("full")
    space("aither-code-embed")
    ce = setup_cli.generate_compose("full", code_embed=setup_cli._code_embed_model())
    assert "nomic" not in ce and '"8209:8000"' not in ce
    assert "adk-vllm-orchestrator" in ce and "adk-vllm-reasoning" in ce


def _run_cmd_setup(monkeypatch, tmp_path):
    from adk import setup_cli

    served: list = []
    monkeypatch.setattr(setup_cli, "detect_gpu", lambda: _gpu("nvidia", 24576))
    monkeypatch.setattr(setup_cli, "check_docker", lambda: (True, "test"))
    monkeypatch.setattr(setup_cli, "_scan_existing_infra",
                        lambda: setup_cli.ExistingInfra([], False, [], False, False))
    monkeypatch.setattr(setup_cli, "_port_in_use", lambda port: False)
    monkeypatch.setattr(setup_cli, "_save_config", lambda *a, **k: None)
    monkeypatch.setattr(setup_cli, "_serve_code_embed",
                        lambda m, dry_run: served.append((m, dry_run)))
    args = type("A", (), {
        "dry_run": True, "tier": "full", "hf_token": "", "non_interactive": True,
        "output": str(tmp_path / "compose.yml"), "stack": None, "api_key": "",
    })()
    assert setup_cli.cmd_setup(args) == 0
    return served


def test_adk_setup_full_tier_follows_the_space(space, monkeypatch, tmp_path, capsys):
    """cmd_setup is what `adk setup` / `adk deploy vllm` run (cli.py, deploy.deploy_vllm)."""
    space()
    assert _run_cmd_setup(monkeypatch, tmp_path) == []
    assert "nomic-embed-text" in capsys.readouterr().out
    space("aither-code-embed")
    assert _run_cmd_setup(monkeypatch, tmp_path) == [("aither-code-embed", True)]
    out = capsys.readouterr().out
    assert "nomic" not in out and "http://localhost:8229/v1" in out
