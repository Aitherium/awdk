"""adk.node_capabilities: what a node can lend is probed, and lent only when opted in."""
from __future__ import annotations

import asyncio
import json
import types

import httpx

import pytest

from adk import enrollment
from adk import node_capabilities as nc


def _host(**kw):
    base = dict(cpu_cores=8, ram_gib=32.0, gpu_vendor="nvidia", gpu_name="RTX 5090",
                gpu_vram_mb=32607, data_dir="/x", disk_free_gib=500.0,
                embedder="sentence_transformers", llm_runtime="llama-server",
                llm_backend=True, numpy=True, mcp_server_enabled=True, relay_enabled=False)
    base.update(kw)
    return nc.HostFacts(**base)


def test_detect_covers_every_kind_on_a_big_host():
    v = nc.detect(_host())
    assert list(v) == list(nc.KINDS)
    on = {k for k, e in v.items() if e["available"]}
    assert on == set(nc.KINDS) - {"relay"}
    assert v["llm_large"]["detail"]["vram_mb"] == 32607


def test_detect_on_a_bare_cpu_box():
    v = nc.detect(nc.HostFacts(cpu_cores=2, ram_gib=4.0, disk_free_gib=3.0))
    on = {k for k, e in v.items() if e["available"]}
    # No GPU, no runtime, no embedder, no backend, small disk: only tool execution.
    assert on == {"tool_exec"}


def test_gpu_kinds_need_vram_thresholds():
    v = nc.detect(_host(gpu_vram_mb=6000))
    assert v["embed_gpu"]["available"] and v["llm_small"]["available"]
    assert not v["inference_gpu"]["available"] and not v["llm_large"]["available"]


def test_lending_is_opt_in_by_default():
    out = nc.advertise(_host(), saved={}, env={})
    assert out["capabilities"] == []
    assert all(d["lent"] is False for d in out["capability_detail"].values())


def test_lend_config_and_env_override():
    saved = {"lend": ["embed_cpu", "storage", "bogus_kind"]}
    assert nc.lend_set(saved, env={}) == ["embed_cpu", "storage"]
    # env wins; set-but-empty means lend nothing
    assert nc.lend_set(saved, env={"AITHER_LEND": "jobs,relay"}) == ["jobs", "relay"]
    assert nc.lend_set(saved, env={"AITHER_LEND": ""}) == []
    assert nc.lend_set({"lend": "jobs, storage"}, env={}) == ["jobs", "storage"]
    assert nc.lend_set({"lend": 5}, env={}) == []


def test_opted_but_unavailable_is_not_advertised():
    out = nc.advertise(_host(relay_enabled=False), saved={"lend": ["relay", "jobs"]}, env={})
    assert out["capabilities"] == ["jobs"]
    relay = out["capability_detail"]["relay"]
    assert relay["lent"] is False and "unavailable" in relay["reason"]


def test_gather_facts_on_a_fake_host(tmp_path, monkeypatch):
    sysinfo = types.SimpleNamespace(cpu_cores=6, ram_gb=16.0, gpu_vendor="none",
                                    gpu_name="", gpu_vram_mb=0)
    (tmp_path / "llamacpp" / "bin").mkdir(parents=True)
    (tmp_path / "llamacpp" / "bin" / "llama-server").write_text("")
    monkeypatch.setattr(nc.shutil, "which", lambda name: None)
    monkeypatch.setattr(nc, "_find_spec", lambda name: name == "numpy")
    monkeypatch.setattr(nc, "_kvholder_relay_live", lambda: False)
    f = nc.gather_facts(sysinfo=sysinfo, saved={"mcp_server": {"enabled": True}},
                        env={}, data_dir=str(tmp_path))
    assert f.cpu_cores == 6 and f.ram_gib == 16.0 and f.gpu_vram_mb == 0
    assert f.llm_runtime == "llama-server" and f.embedder == "" and f.numpy
    assert f.mcp_server_enabled and not f.relay_enabled and not f.llm_backend
    assert f.disk_free_gib > 0
    v = nc.detect(f)
    assert v["llm_small"]["available"] and v["kv_holder"]["available"]
    assert not v["embed_cpu"]["available"] and not v["agent_loop"]["available"]


def test_gather_facts_relay_env_and_inference_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(nc.shutil, "which", lambda name: None)
    monkeypatch.setattr(nc, "_kvholder_relay_live", lambda: False)
    f = nc.gather_facts(sysinfo=types.SimpleNamespace(), saved={},
                        env={"AITHER_RELAY_ENABLED": "1"}, data_dir=str(tmp_path / "missing"),
                        inference_kind="ollama", inference_ready=True)
    assert f.relay_enabled and f.llm_runtime == "ollama" and f.llm_backend


def test_enrollment_registers_probed_and_opted_capabilities(monkeypatch):
    sysinfo = types.SimpleNamespace(cpu_cores=8, ram_gb=32.0, gpu_vendor="nvidia",
                                    gpu_name="RTX", gpu_vram_mb=12000, python_version="3.11")
    import adk.hardware_probe as hp
    monkeypatch.setattr(hp, "detect_system", lambda: sysinfo)
    monkeypatch.setattr(enrollment, "probe_inference", lambda url: enrollment.InferenceProbe(
        ["m"], "http://127.0.0.1:8080", "llama-server", True))
    captured = {}

    def fake_adv(**kw):
        captured.update(kw)
        facts = _host(gpu_vram_mb=12000)
        return nc.advertise(facts, saved={"lend": ["inference_gpu", "embed_cpu"]}, env={})

    monkeypatch.setattr(nc, "advertise_this_node", fake_adv)
    reg = enrollment.build_registration("n1")
    assert reg["capabilities"] == ["embed_cpu", "inference_gpu"]
    assert set(reg["capability_detail"]) == set(nc.KINDS)
    assert "code_search" not in reg["capabilities"]
    # enrollment hands its own probe results to the detector (no second GPU probe)
    assert captured["sysinfo"] is sysinfo and captured["inference_ready"] is True


def test_enrollment_lends_nothing_when_the_probe_breaks(monkeypatch):
    def boom(**kw):
        raise RuntimeError("probe died")

    monkeypatch.setattr(nc, "advertise_this_node", boom)
    monkeypatch.setattr(enrollment, "probe_inference", lambda url: enrollment.InferenceProbe(
        [], "", "none", False))
    reg = enrollment.build_registration("n1")
    assert reg["capabilities"] == [] and reg["capability_detail"] == {}


@pytest.mark.parametrize("kind", nc.KINDS)
def test_every_kind_is_a_short_token(kind):
    assert kind.isidentifier() and kind.islower() and len(kind) <= 32


def test_every_heartbeat_re_asserts_lending(monkeypatch):
    """A `lend:` change reaches Identity on the next BEAT, not only on re-enrollment."""
    posts = []

    class _Resp:
        status_code = 200
        text = json.dumps({"status": "ok"})

        def json(self):
            return {"status": "ok"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            posts.append(k.get("json") or {})
            return _Resp()

        async def aclose(self):
            return None

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(enrollment, "probe_inference", lambda url: enrollment.InferenceProbe(
        [], "", "none", False))
    lend = iter([[], ["storage"]])

    def fake_adv(**kw):
        return nc.advertise(_host(), saved={"lend": next(lend)}, env={})

    monkeypatch.setattr(nc, "advertise_this_node", fake_adv)

    async def run():
        await enrollment.heartbeat_loop("https://idp.test", "t", "n1", interval=0,
                                        max_beats=2, beat_immediately=True)

    asyncio.run(run())
    beats = [p for p in posts if "inference_ready" in p]
    assert [b["capabilities"] for b in beats] == [[], ["storage"]]
    assert beats[1]["capability_detail"]["storage"]["lent"] is True
    assert beats[0]["capability_detail"]["storage"]["lent"] is False
