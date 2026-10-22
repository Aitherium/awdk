"""`adk bonsai-local` natively: size selection, the size/file/server mapping, and the flows.

A non-technical customer's Windows PC has no container engine, so `adk bonsai-local`
(which ran only a Docker image, and only the 27B) could not give him a local model.
These pin what the native path decides, without downloading a byte.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk import bonsai_native as bn  # noqa: E402
from adk import llamacpp_setup as lcs  # noqa: E402
from adk import local_model_server as lms  # noqa: E402


def accel(kind: str = "cpu", vram: float = 0.0, ram: float = 16.0, os_family: str = "windows",
          arch: str = "x64") -> lcs.AccelInfo:
    return lcs.AccelInfo(kind=kind, name="test", vram_gb=vram, ram_gb=ram,
                         os_family=os_family, arch=arch)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(bn, "home_dir", lambda: tmp_path / ".aither")
    return tmp_path / ".aither"


# --- the catalogue -----------------------------------------------------------------

def test_every_supported_size_is_offered_and_pinned():
    assert set(bn.SIZE_ORDER) == set(bn.BONSAI_SIZES) == {
        "bonsai-1.7b", "bonsai-4b", "bonsai-8b", "bonsai-27b", "bonsai2-27b"}
    for size in bn.BONSAI_SIZES.values():
        for quant, w in size.weights.items():
            assert re.fullmatch(r"[0-9a-f]{64}", w.sha256), (size.id, quant)
            assert w.size_bytes > 100_000_000
            assert w.file.endswith(".gguf")


def test_bonsai1_is_q1_0_and_bonsai2_ships_both_prismml_files():
    for sid in ("bonsai-1.7b", "bonsai-4b", "bonsai-8b", "bonsai-27b"):
        assert list(bn.BONSAI_SIZES[sid].weights) == ["Q1_0"]
        assert bn.BONSAI_SIZES[sid].generation == 1
    b2 = bn.BONSAI_SIZES["bonsai2-27b"]
    assert b2.generation == 2
    assert b2.weights["PQ2_0"].file == "Ternary-Bonsai-2-27B-PQ2_0.gguf"
    assert b2.weights["PTQ1_0"].file == "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    # The measured mirror byte counts.
    assert b2.weights["PQ2_0"].size_bytes == 7_206_168_928
    assert b2.weights["PTQ1_0"].size_bytes == 5_946_648_928
    assert bn.BONSAI_SIZES["bonsai-1.7b"].weights["Q1_0"].size_bytes == 248_302_272


def test_size_order_is_largest_first():
    disk = [max(w.size_bytes for w in bn.BONSAI_SIZES[i].weights.values())
            for i in bn.SIZE_ORDER]
    assert disk == sorted(disk, reverse=True)


# --- choosing a size ----------------------------------------------------------------

@pytest.mark.parametrize("hw, want_id, want_quant, want_where, want_build", [
    # CPU only: half the RAM is the pool.
    (accel("cpu", ram=8), "bonsai-4b", "Q1_0", "cpu", "cpu"),
    (accel("cpu", ram=16), "bonsai-27b", "Q1_0", "cpu", "cpu"),
    (accel("cpu", ram=32), "bonsai2-27b", "PTQ1_0", "cpu", "cpu"),
    # A 12 GB NVIDIA card holds Bonsai 2 PQ2_0 (the CUDA fast file).
    (accel("cuda", vram=12, ram=32), "bonsai2-27b", "PQ2_0", "gpu", "cuda"),
    # An 8 GB card: the largest that fits ON THE GPU wins over a bigger size in RAM.
    (accel("cuda", vram=8, ram=64), "bonsai-8b", "Q1_0", "gpu", "cuda"),
    # Vulkan serves Bonsai 2 from PTQ1_0, never PQ2_0.
    (accel("vulkan", vram=16, ram=32), "bonsai2-27b", "PTQ1_0", "gpu", "vulkan"),
    # An integrated GPU reports no VRAM: it is a CPU box.
    (accel("vulkan", vram=0, ram=16), "bonsai-27b", "Q1_0", "cpu", "cpu"),
    # Apple Silicon: one unified pool, the Metal build, PQ2_0.
    (accel("metal", ram=24, os_family="macos", arch="arm64"),
     "bonsai2-27b", "PQ2_0", "gpu", "metal"),
    (accel("metal", ram=16, os_family="macos", arch="arm64"),
     "bonsai-27b", "Q1_0", "gpu", "metal"),
])
def test_auto_picks_the_largest_size_that_fits(hw, want_id, want_quant, want_where, want_build):
    rec = bn.recommend(hw)
    assert rec is not None
    assert (rec.id, rec.quant, rec.placement, rec.build) == (
        want_id, want_quant, want_where, want_build)


def test_nothing_fits_a_tiny_machine():
    assert bn.recommend(accel("cpu", ram=2)) is None


def test_plan_lists_every_size_with_a_reason():
    plans = bn.plan_sizes(accel("cpu", ram=8))
    assert [p.id for p in plans] == bn.SIZE_ORDER
    assert [p.fits for p in plans] == [False, False, False, True, True]
    assert all(p.reason for p in plans)


def test_backend_override_moves_bonsai2_to_the_matching_file():
    size = bn.BONSAI_SIZES["bonsai2-27b"]
    hw = accel("cuda", vram=24, ram=64)
    forced = bn.override_placement(bn.plan_size(size, hw), size, hw, backend="cpu")
    assert (forced.placement, forced.build, forced.quant) == ("cpu", "cpu", "PTQ1_0")
    assert forced.file == "Ternary-Bonsai-2-27B-PTQ1_0.gguf"
    with pytest.raises(ValueError):
        bn.override_placement(bn.plan_size(size, hw), size, hw, quant="Q1_0")


# --- the server binary --------------------------------------------------------------

def test_server_asset_is_the_pinned_prismml_fork():
    url, sha, build = bn.server_asset("windows", "x64", "cuda")
    assert url.endswith(
        "/prism-b10685-7dffb15/llama-prism-b10685-7dffb15-bin-win-cuda-12.4-x64.zip")
    assert re.fullmatch(r"[0-9a-f]{64}", sha) and build == "cuda"
    assert "github.com/PrismML-Eng/llama.cpp/" in url


@pytest.mark.parametrize("os_family, arch, build, want", [
    ("macos", "x64", "metal", "cpu"),       # Intel Macs: no Metal build
    ("linux", "arm64", "cuda", "cpu"),      # no arm64 CUDA build
    ("windows", "arm64", "vulkan", "cpu"),  # no arm64 Vulkan build on Windows
    ("linux", "x64", "vulkan", "vulkan"),
])
def test_unshipped_builds_fall_back_to_the_cpu_build(os_family, arch, build, want):
    assert bn.server_asset(os_family, arch, build)[2] == want


def test_unknown_os_has_no_build():
    with pytest.raises(LookupError):
        bn.server_asset("plan9", "x64", "cpu")


def test_server_argv_is_loopback_single_slot_with_tool_calling(tmp_path):
    hw = accel("cpu", ram=64)
    cpu = bn.plan_size(bn.BONSAI_SIZES["bonsai-8b"], hw)
    argv = bn.server_argv(tmp_path / "llama-server", tmp_path / "m.gguf", 8090, cpu)
    joined = " ".join(argv)
    assert "--host 127.0.0.1" in joined and "-np 1" in joined and "--jinja" in joined
    assert "--alias bonsai-local" in joined and "-ngl 0" in joined
    assert "-fa on" not in joined  # Bonsai 1 gets llama.cpp's defaults
    gpu = bn.plan_size(bn.BONSAI_SIZES["bonsai2-27b"], accel("cuda", vram=24, ram=64))
    joined2 = " ".join(bn.server_argv(tmp_path / "s", tmp_path / "m", 8090, gpu))
    assert "-ngl 99" in joined2 and "-fa on" in joined2 and "--reasoning-budget 2048" in joined2


def test_safe_extract_refuses_a_path_outside_the_install_dir(tmp_path):
    import zipfile
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(ValueError):
        bn._safe_extract(bad, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


# --- weights ------------------------------------------------------------------------

def test_weights_try_the_mirror_then_huggingface(home):
    w = bn.BONSAI_SIZES["bonsai-4b"].weights["Q1_0"]
    tried: List[str] = []

    def fetch(url, dest, wf):
        tried.append(url)
        if "huggingface" in url:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            return True
        return False

    assert bn.install_weights(w, fetch=fetch) == home / "models" / w.file
    assert tried[0] == "https://weights.aitherium.com/Bonsai-4B-Q1_0.gguf"
    assert tried[1] == ("https://huggingface.co/prism-ml/Bonsai-4B-gguf/resolve/main/"
                        "Bonsai-4B-Q1_0.gguf")


def test_weights_never_overwrite_a_foreign_file(home):
    w = bn.BONSAI_SIZES["bonsai-4b"].weights["Q1_0"]
    (home / "models").mkdir(parents=True)
    (home / "models" / w.file).write_bytes(b"not the model")
    assert bn.install_weights(w, fetch=lambda *a: pytest.fail("must not download")) is None
    assert (home / "models" / w.file).read_bytes() == b"not the model"


# --- the smoke test -----------------------------------------------------------------

def _post(tool_name: str):
    def post(url: str, body: Dict[str, Any], timeout: float = 0) -> Dict[str, Any]:
        if body.get("tools"):
            calls = [{"function": {"name": tool_name, "arguments": '{"city": "Paris"}'}}]
            return {"choices": [{"message": {"content": None, "tool_calls": calls}}]}
        return {"choices": [{"message": {"content": "Hello there, nice day!"}}]}
    return post


def test_smoke_test_needs_a_chat_answer_and_a_tool_call():
    good = bn.smoke_test(8090, post=_post("get_weather"))
    assert good["chat"] and good["tool_call"]
    bad = bn.smoke_test(8090, post=_post("something_else"))
    assert bad["chat"] and not bad["tool_call"]


# --- status, and what Desk / `adk onboard` find -------------------------------------

def test_status_report_is_what_the_desk_reads(home):
    report = bn.status_report(accel("cpu", ram=8), is_serving=lambda port: False)
    assert report["installed"] is False and report["recommended"] == "bonsai-4b"
    assert [s["id"] for s in report["sizes"]] == bn.SIZE_ORDER
    assert report["endpoint"] == "http://127.0.0.1:8090/v1"
    json.dumps(report)  # the Desk parses it as JSON
    bn.write_state({"model": "bonsai-4b", "quant": "Q1_0", "placement": "cpu", "port": 18990})
    report = bn.status_report(accel("cpu", ram=8), is_serving=lambda port: port == 18990)
    assert report["installed"] and report["running"] and report["model"] == "bonsai-4b"


def test_local_model_server_starts_the_native_install_first(tmp_path):
    aither = tmp_path / ".aither"
    exe = aither / "bonsai" / "llama-server.exe"
    gguf = aither / "models" / "Bonsai-8B-Q1_0.gguf"
    exe.parent.mkdir(parents=True)
    gguf.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    gguf.write_bytes(b"x")
    argv = [str(exe), "-m", str(gguf), "--port", "8090"]
    (aither / "bonsai-local.json").write_text(json.dumps(
        {"argv": argv, "weights": str(gguf), "port": 8090}), encoding="utf-8")
    spec = lms.find_local_model_server(env={"LOCALAPPDATA": str(tmp_path)}, platform="win32",
                                       home=tmp_path)
    assert spec and spec["kind"] == "bonsai-local" and spec["argv"] == argv
    assert spec["health"] == "http://127.0.0.1:8090/health"
    gguf.unlink()  # weights gone: not a startable server any more
    assert lms.find_local_model_server(env={"LOCALAPPDATA": str(tmp_path)},
                                       platform="win32", home=tmp_path) is None


# --- CLI ----------------------------------------------------------------------------

def test_cli_offers_every_size_and_keeps_the_container_path(monkeypatch, capsys):
    from adk import cli

    calls: Dict[str, Any] = {}

    def fake_install(**kw):
        calls.update(kw)
        return bn.InstallOutcome(True, model=kw["model"] or "bonsai-4b", port=kw["port"])

    monkeypatch.setattr(bn, "install", fake_install)
    monkeypatch.setattr(cli, "_bonsai_local_save_backend", lambda *a: None)
    args = argparse.Namespace(model="bonsai-1.7b", port=8090, auto=False, backend="cpu",
                              quant="", no_autostart=True, no_smoke=False, dry_run=False,
                              json=False, status=False, remove=False, stop=False,
                              container=False, no_configure=True)
    assert cli.cmd_bonsai_local(args) == 0
    assert calls["model"] == "bonsai-1.7b" and calls["backend"] == "cpu"
    # ling-tiny-8b is container-only; the native path refuses it plainly.
    args.model = "ling-tiny-8b"
    assert cli.cmd_bonsai_local(args) == 1
    assert "--container" in capsys.readouterr().out
    # No --model = --auto.
    args.model = ""
    cli.cmd_bonsai_local(args)
    assert calls["auto"] is True
    # Every native size is an accepted --model value.
    assert set(bn.BONSAI_SIZES) <= set(cli.BONSAI_LOCAL_MODEL_CHOICES)


def test_windows_autostart_is_a_named_hidden_task(home, monkeypatch, capsys):
    monkeypatch.setattr(lcs, "LOG_DIR", home / "logs")
    # A real profile path; pytest's tmp dir is longer than schtasks' 261-char /tr limit.
    # Dry run: nothing is written there.
    assert lcs._install_windows_task(["llama-server.exe", "-m", "m.gguf"], True,
                                     task_name=bn.TASK_NAME,
                                     wrapper_dir=Path("C:/Users/jason/.aither/bonsai"),
                                     log_stem="bonsai-local")
    out = capsys.readouterr().out
    assert "schtasks /create /tn AitherBonsaiLocal" in out
    assert "wscript.exe //B //Nologo" in out and "AitherBonsaiLocal.cmd" in out
