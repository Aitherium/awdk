"""`adk models`: the hardware probe, the licence gate, the recommendation, the CLI.

The gate is the part that must not fail open, so each refusal reason has its own test
AND the permitted case has one -- a gate that refuses everything passes every negative
test while shipping nothing.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from adk.models import catalogue, probe, serve
from adk.models import cli as models_cli

OK = {"record": "m", "name": "Apache-2.0", "url": "", "redistribution_ok": True,
      "commercial_ok": True, "attribution_required": True}


def _model(mid, size_mb, licence, role="chat", **kw):
    return dict({"id": mid, "role": role, "file": f"{mid}.gguf", "size_mb": size_mb,
                 "parts": 0, "join": False, "urls": [f"http://x/{mid}.gguf"],
                 "opt_in": False, "purpose": "", "runtime": "llama.cpp",
                 "licence_id": mid, "licence": licence}, **kw)


def _cat(**models):
    return {"version": 1, "min_budget_mb": 900, "headroom_divisor": 2,
            "ladder": [m for m in models if models[m]["role"] == "chat"],
            "models": models}


@pytest.fixture()
def cat():
    """A ladder whose middle rung has no licence record and whose top is refused."""
    return _cat(small=_model("small", 400, dict(OK)),
                mid=_model("mid", 1000, None),
                big=_model("big", 2000, dict(OK)),
                huge=_model("huge", 5000, dict(OK, redistribution_ok=False)),
                embed=_model("embed", 600, dict(OK), role="embedding"))


# ── the licence gate ────────────────────────────────────────────────────────────────

def test_missing_record_is_refused():
    v = catalogue.licence_verdict(_model("m", 1, None))
    assert not v.allowed and "no licence record for 'm'" in v.reason


def test_redistribution_false_is_refused():
    v = catalogue.licence_verdict(_model("m", 1, dict(OK, redistribution_ok=False)))
    assert not v.allowed and "redistribution_ok: false" in v.reason


def test_unstated_redistribution_is_refused_not_assumed():
    """A record that omits the flag grants nothing: fail closed, with its own reason."""
    v = catalogue.licence_verdict(_model("m", 1, dict(OK, redistribution_ok=None)))
    assert not v.allowed and "does not state redistribution_ok" in v.reason
    lic = dict(OK)
    del lic["redistribution_ok"]
    assert not catalogue.licence_verdict(_model("m", 1, lic)).allowed


@pytest.mark.parametrize("truthy", ["true", 1, "yes"])
def test_only_a_literal_true_permits(truthy):
    assert not catalogue.licence_verdict(_model("m", 1, dict(OK, redistribution_ok=truthy))).allowed


def test_permitted_model_is_allowed():
    """The positive case: a gate that refuses everything must fail here."""
    v = catalogue.licence_verdict(_model("m", 1, dict(OK)))
    assert v.allowed and "Apache-2.0" in v.reason


def test_shipped_catalogue_gate_matches_its_licence_snapshot():
    """Every shipped model: allowed exactly when its snapshot says redistribution true."""
    cat = catalogue.load()
    for mid, m in cat["models"].items():
        want = isinstance(m["licence"], dict) and m["licence"]["redistribution_ok"] is True
        assert catalogue.licence_verdict(m).allowed is want, mid
    allowed = {k for k, m in cat["models"].items() if catalogue.licence_verdict(m).allowed}
    assert "bonsai2-27b" in allowed, "the default model must be pullable"
    # Licence records corrected 2026-10-02: Gemma 4 Apache-2.0 and DeepSeek V4 Flash
    # MIT (read from their model pages), the orchestrator by owner determination.
    # If one of these turns refused, a licence record changed: someone must have read it.
    assert {"orchestrator", "gemma4-12b", "deepseek-v4-flash", "bonsai-4b"} <= allowed


def test_shipped_catalogue_has_the_embedder_as_an_embedding_role():
    m = catalogue.get(catalogue.load(), "aither-code-embed")
    assert m["role"] == "embedding" and m["file"] == "aither-code-embed.q8_0.gguf"
    assert m["size_bytes"] == 639145920 and not m["on_ladder"]


# ── the recommendation ──────────────────────────────────────────────────────────────

def test_recommend_takes_the_largest_permitted_model_that_fits(cat):
    assert catalogue.recommend(cat, 4096) == "big"      # 2000 MB fits 4096 / 2
    assert catalogue.recommend(cat, 3999) == "small"    # 'mid' fits but has no record
    assert catalogue.recommend(cat, 1024) == "small"


def test_recommend_never_picks_a_gated_model(cat):
    """At every budget, whatever is recommended passes the gate."""
    for budget in range(0, 40000, 257):
        pick = catalogue.recommend(cat, budget)
        if pick is not None:
            assert catalogue.licence_verdict(cat["models"][pick]).allowed, (budget, pick)
    # 'huge' fits 20 GB and is the largest rung, but redistribution_ok is false.
    assert catalogue.recommend(cat, 20000) == "big"
    assert catalogue.refused_rungs(cat, 20000) == ["mid", "huge"]


def test_recommend_is_none_when_only_gated_models_fit(cat):
    only_gated = copy.deepcopy(cat)
    only_gated["models"]["small"]["licence"] = None
    only_gated["models"]["big"]["licence"]["redistribution_ok"] = False
    assert catalogue.recommend(only_gated, 20000) is None
    assert catalogue.refused_rungs(only_gated, 20000) == ["small", "mid", "big", "huge"]


def test_recommend_respects_the_floor_and_never_picks_an_embedder(cat):
    assert catalogue.recommend(cat, 899) is None, "below min_budget_mb nothing is chosen"
    assert catalogue.recommend(cat, 0) is None
    cat["ladder"].append("embed")
    assert catalogue.recommend(cat, 1300) == "small", "an embedder is not a chat pick"


def test_shipped_recommendation_is_always_pullable():
    cat = catalogue.load()
    for budget in (0, 512, 1024, 4096, 8192, 12288, 24576, 65536, 262144):
        pick = catalogue.recommend(cat, budget)
        if pick:
            assert catalogue.licence_verdict(cat["models"][pick]).allowed
    assert catalogue.recommend(cat, 24576) == "bonsai2-27b"


def test_a_cpu_is_never_handed_a_model_too_slow_to_use():
    """60 GB of RAM fits the 27B; on a CPU it ran at 0.43 tokens/s. The 4B, not the 27B."""
    cat = catalogue.load()
    assert catalogue.recommend(cat, 60000) == "bonsai2-27b"          # a GPU with that much
    assert catalogue.recommend(cat, 60000, cpu_only=True) == "bonsai-4b"
    pick = catalogue.recommend(cat, 60000, cpu_only=True)
    assert int(cat["models"][pick]["size_mb"]) <= catalogue.CPU_RECOMMEND_MAX_MB


def test_a_missing_or_empty_catalogue_raises(tmp_path):
    with pytest.raises(catalogue.CatalogueError):
        catalogue.load(tmp_path / "nope.json")
    (tmp_path / "empty.json").write_text('{"models": {}}', encoding="utf-8")
    with pytest.raises(catalogue.CatalogueError):
        catalogue.load(tmp_path / "empty.json")
    with pytest.raises(catalogue.CatalogueError, match="no model 'ghost'"):
        catalogue.get(catalogue.load(), "ghost")


# ── the hardware probe, on mocked systems ───────────────────────────────────────────

def _mock(monkeypatch, *, ram=0, tools=None, sysfs=None, registry=None):
    """Replace every source the probe reads. ``tools`` maps an executable to its stdout."""
    tools = tools or {}
    monkeypatch.setattr(probe, "_ram_mb", lambda system: ram)
    monkeypatch.setattr(probe, "_which", lambda name: name if name in tools else None)
    monkeypatch.setattr(probe, "_run", lambda argv, timeout=15.0: tools.get(argv[0]))
    monkeypatch.setattr(probe, "_sysfs_vram", lambda: list(sysfs or []))
    monkeypatch.setattr(probe, "_registry_vram", lambda: list(registry or []))


def test_no_gpu_falls_back_to_ram(monkeypatch):
    _mock(monkeypatch, ram=16000)
    for system in ("Linux", "Windows"):
        hw = probe.detect(system, "x86_64")
        assert (hw.kind, hw.budget_mb) == ("ram", 16000)


def test_no_nvidia_smi_is_not_an_error(monkeypatch):
    """nvidia-smi absent AND failing are both normal answers, never a crash."""
    _mock(monkeypatch, ram=8000, tools={"nvidia-smi": None})
    assert probe.detect("Linux", "x86_64").kind == "ram"


def test_small_nvidia_vram(monkeypatch):
    _mock(monkeypatch, ram=32000, tools={"nvidia-smi": "GeForce GTX 1050 Ti, 4096\n"})
    hw = probe.detect("Linux", "x86_64")
    assert (hw.kind, hw.budget_mb, hw.ram_mb) == ("nvidia", 4096, 32000)
    assert "1050 Ti" in hw.detail


def test_large_nvidia_vram_takes_the_largest_card(monkeypatch):
    _mock(monkeypatch, ram=64000, tools={
        "nvidia-smi": "NVIDIA GeForce GTX 1650, 4096\nNVIDIA GeForce RTX 5090, 32607\n"})
    hw = probe.detect("Windows", "AMD64")
    assert (hw.kind, hw.budget_mb) == ("nvidia", 32607) and "5090" in hw.detail


def test_apple_silicon_uses_unified_memory(monkeypatch):
    # nvidia-smi on PATH must not matter on a Mac.
    _mock(monkeypatch, ram=36864, tools={"nvidia-smi": "Fake, 99999\n"})
    hw = probe.detect("Darwin", "arm64")
    assert (hw.kind, hw.budget_mb) == ("apple", 36864)
    assert "unified memory" in hw.describe()


def test_intel_mac_is_cpu_ram(monkeypatch):
    _mock(monkeypatch, ram=16384)
    assert probe.detect("Darwin", "x86_64").kind == "ram"


def test_amd_gpu_from_the_driver(monkeypatch):
    _mock(monkeypatch, ram=32000, sysfs=[("GPU card0", 16368)])
    hw = probe.detect("Linux", "x86_64")
    assert (hw.kind, hw.budget_mb) == ("gpu", 16368)
    _mock(monkeypatch, ram=32000, registry=[("AMD Radeon RX 7900 XT", 20464)])
    assert probe.detect("Windows", "AMD64").budget_mb == 20464


def test_integrated_gpu_is_sized_as_ram(monkeypatch):
    """128 MB of 'dedicated' iGPU memory must not refuse a 16 GB machine."""
    _mock(monkeypatch, ram=16000, registry=[("Intel(R) UHD Graphics", 128)])
    hw = probe.detect("Windows", "AMD64")
    assert (hw.kind, hw.budget_mb) == ("ram", 16000)


VULKANINFO = """
GPU0:
VkPhysicalDeviceProperties:
	deviceType        = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
	deviceName        = Intel(R) UHD Graphics 770
VkPhysicalDeviceMemoryProperties:
memoryHeaps: count = 1
	memoryHeaps[0]:
		size   = 33000000000 (0x7aef40a00) (30.73 GiB)
		flags: count = 1
			MEMORY_HEAP_DEVICE_LOCAL_BIT
memoryTypes: count = 1
GPU1:
VkPhysicalDeviceProperties:
	deviceType        = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
	deviceName        = Intel(R) Arc(TM) A770 Graphics
VkPhysicalDeviceMemoryProperties:
memoryHeaps: count = 2
	memoryHeaps[0]:
		size   = 17179869184 (0x400000000) (16.00 GiB)
		flags: count = 1
			MEMORY_HEAP_DEVICE_LOCAL_BIT
	memoryHeaps[1]:
		size   = 34359738368 (0x800000000) (32.00 GiB)
		flags:
			None
memoryTypes: count = 2
"""


def test_vulkan_discrete_device_local_heap(monkeypatch):
    """The DISCRETE device's device-local heap; not the iGPU's, not the host-RAM heap."""
    _mock(monkeypatch, ram=65536, tools={"vulkaninfo": VULKANINFO})
    hw = probe.detect("Linux", "x86_64")
    assert (hw.kind, hw.budget_mb) == ("gpu", 16384) and "A770" in hw.detail


def test_unreadable_machine_is_none(monkeypatch):
    _mock(monkeypatch, ram=0)
    hw = probe.detect("Linux", "x86_64")
    assert (hw.kind, hw.budget_mb) == ("none", 0)
    assert catalogue.recommend(catalogue.load(), hw.budget_mb) is None


def test_real_probe_does_not_raise():
    assert probe.detect().kind in ("nvidia", "apple", "gpu", "ram", "none")


# ── the CLI ─────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def fake_catalogue(tmp_path, monkeypatch, cat):
    p = tmp_path / "models_catalogue.json"
    p.write_text(json.dumps(cat), encoding="utf-8")
    monkeypatch.setattr(catalogue, "CATALOGUE_PATH", p)
    monkeypatch.setenv("AITHER_BONSAI_ROOT", str(tmp_path / "bonsai"))
    return cat


def test_list_shows_every_model_including_refused(fake_catalogue, capsys):
    assert models_cli.main(["list", "--budget-mb", "4096"]) == 0
    out = capsys.readouterr().out
    for mid in fake_catalogue["models"]:
        assert mid in out
    assert out.count("[REFUSED]") == 2 and out.count("[ok]") == 3
    assert "no licence record for 'mid'" in out


def test_list_json_reports_fit_and_gate(fake_catalogue, capsys):
    assert models_cli.main(["list", "--budget-mb", "4096", "--json"]) == 0
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)["models"]}
    assert rows["big"]["fits"] and not rows["huge"]["fits"]
    assert rows["big"]["floor_mb"] == 4000 and not rows["mid"]["allowed"]


def test_recommend_cli(fake_catalogue, capsys):
    assert models_cli.main(["recommend", "--budget-mb", "4096"]) == 0
    assert "Recommended: big" in capsys.readouterr().out
    assert models_cli.main(["recommend", "--budget-mb", "500"]) == 1


@pytest.mark.parametrize("verb", ["pull", "use"])
@pytest.mark.parametrize("mid,why", [("mid", "no licence record"),
                                     ("huge", "redistribution_ok: false")])
def test_pull_and_use_refuse_a_gated_model_in_one_line(fake_catalogue, capsys, monkeypatch,
                                                       verb, mid, why):
    from adk.models import download

    def boom(*a, **k):
        raise AssertionError("a refused model must never reach the network or the server")

    monkeypatch.setattr(download, "fetch_model", boom)
    monkeypatch.setattr(serve, "start", boom)
    assert models_cli.main([verb, mid]) == 1
    err = capsys.readouterr().err.strip()
    assert why in err and len(err.splitlines()) == 1


def test_pull_of_a_permitted_model_downloads_into_the_installer_layout(
        fake_catalogue, capsys, monkeypatch, tmp_path):
    from adk.models import download
    seen = {}

    def fake_fetch(model, models_dir, say=print, timeout=0):
        seen.update(id=model["id"], dir=Path(models_dir))
        return Path(models_dir) / model["file"], False

    monkeypatch.setattr(download, "fetch_model", fake_fetch)
    assert models_cli.main(["pull", "big"]) == 0
    assert seen == {"id": "big", "dir": tmp_path / "bonsai" / "models"}
    assert "NOT hash-verified" in capsys.readouterr().out


def test_use_requires_the_pulled_file(fake_catalogue, capsys):
    assert models_cli.main(["use", "big"]) == 1
    assert "adk models pull big" in capsys.readouterr().err


def _use(monkeypatch, tmp_path, mid, started):
    import adk.config

    saved = {}
    monkeypatch.setattr(adk.config, "save_saved_config", lambda d, *a, **k: saved.update(d))
    monkeypatch.setattr(serve, "start",
                        lambda m, gguf, port, **k: started.append((m["id"], gguf, port)) or 1)
    d = tmp_path / "bonsai" / "models"
    d.mkdir(parents=True)
    (d / f"{mid}.gguf").write_bytes(b"GGUF")
    assert models_cli.main(["use", mid]) == 0
    return saved


def test_use_chat_points_the_backend_at_the_installer_endpoint(fake_catalogue, monkeypatch,
                                                               tmp_path):
    started = []
    saved = _use(monkeypatch, tmp_path, "big", started)
    assert started == [("big", tmp_path / "bonsai" / "models" / "big.gguf", 8080)]
    assert saved == {"default_backend": "vllm", "inference_url": "http://127.0.0.1:8080/v1",
                     "default_model": "bonsai-selfhost", "local_model_id": "big"}


def test_use_embedder_configures_embedding_only(fake_catalogue, monkeypatch, tmp_path):
    """An embedder must never become the chat backend."""
    started = []
    saved = _use(monkeypatch, tmp_path, "embed", started)
    assert started[0][2] == 8229
    assert saved == {"embeddings_url": "http://127.0.0.1:8229/v1",
                     "embeddings_model": "embed", "embed_space": "embed"}
    assert not {"default_backend", "inference_url", "default_model"} & set(saved)


def test_server_args_match_the_installer(fake_catalogue):
    chat = serve.server_args(fake_catalogue["models"]["big"], Path("m.gguf"), 8080)
    assert chat[chat.index("--host") + 1] == "127.0.0.1", "loopback only"
    assert chat[chat.index("--reasoning-budget") + 1] == "2048"
    assert chat[chat.index("--alias") + 1] == "bonsai-selfhost"
    emb = serve.server_args(fake_catalogue["models"]["embed"], Path("e.gguf"), 8229)
    assert "--embedding" in emb and "--reasoning-budget" not in emb
    assert emb[emb.index("--host") + 1] == "127.0.0.1"


def test_install_root_is_the_installers(monkeypatch):
    monkeypatch.delenv("AITHER_BONSAI_ROOT", raising=False)
    root = serve.install_root().as_posix().lower()
    assert root.endswith("/aitherium/bonsai") or root.endswith("/.aitherium/bonsai")


def test_use_refuses_a_port_someone_else_holds(fake_catalogue, tmp_path, monkeypatch):
    root = tmp_path / "bonsai"
    (root / "bin").mkdir(parents=True)
    serve.server_binary(root).write_bytes(b"")
    monkeypatch.setattr(serve, "_answers", lambda port, *a, **k: True)
    monkeypatch.setattr(serve.time, "sleep", lambda s: None)
    clock = iter(range(0, 10000, 10))
    monkeypatch.setattr(serve.time, "monotonic", lambda: next(clock))
    with pytest.raises(serve.ServeError, match="in use by a program this did not start"):
        serve.start(fake_catalogue["models"]["big"], root / "m.gguf", 8080, root=root,
                    say=lambda s: None)


def test_adk_cli_runs_models_list():
    """`adk models` is wired into the real CLI, not only importable on its own."""
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "adk.cli", "models", "list", "--json",
                        "--budget-mb", "24576"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300,
                       cwd=str(Path(models_cli.__file__).resolve().parents[2]))
    assert r.returncode == 0, r.stderr[-2000:]
    ids = {m["id"] for m in json.loads(r.stdout)["models"]}
    assert {"bonsai2-27b", "aither-code-embed", "gemma4-12b"} <= ids
