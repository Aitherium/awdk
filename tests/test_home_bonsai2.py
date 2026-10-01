"""``adk home model --local bonsai2``: Bonsai 2 27B on the PrismML build we ship.

The traps this guards: a stock llama.cpp loads Bonsai 2 and emits gibberish (so no
PATH fallback, and the binary's --version must name the pinned commit); a download
that does not match its pinned sha256 is never moved into place; and the server
step never touches a process it did not start.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest
from adk.home import bonsai2 as b2
from adk.home import cli as home_cli
from adk.home import config as hc
from adk.home import models


def _hw(**kw):
    base = dict(os="linux", arch="x64", ram_gb=64.0)
    base.update(kw)
    return b2.Hardware(**base)


# ---------------------------------------------------------------- preset

def test_bonsai2_is_a_local_preset_on_its_own_port():
    assert "bonsai2" in models.LOCAL
    cfg = models.choose_model("bonsai2")
    assert (cfg.mode, cfg.model) == ("local", b2.ALIAS)
    assert cfg.base_url == f"http://127.0.0.1:{b2.DEFAULT_PORT}/v1"
    assert b2.DEFAULT_PORT != 8080          # install-bonsai.sh's server owns 8080


def test_bonsai2_router_keeps_thinking_off():
    router = models.build_llm(models.choose_model("bonsai2"))
    assert type(router).__name__ == "LLMRouter"
    from adk import llm

    assert any(k in b2.ALIAS for k in llm._DEFAULT_CTK_BY_MODEL)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_probe_refuses_a_server_that_is_not_ours(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"data": [{"id": "llama"}]}))
    assert models.probe(models.choose_model("bonsai2"))["ok"] is False
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _Resp(200, {"data": [{"id": b2.ALIAS}]}))
    assert models.probe(models.choose_model("bonsai2"))["ok"] is True


# ---------------------------------------------------------------- plan

def test_plan_cuda_box_gets_pq2_and_the_cuda_build():
    p = b2.plan(_hw(vram_total_gb=32, vram_free_gb=30, cuda_level=(12, 8), vulkan=True))
    assert (p.backend, p.quant, p.ngl) == ("cuda", "PQ2_0", 99)
    assert p.assets[0][0].endswith("linux-cuda-12.4-x64.tar.gz")
    assert b2.CTX_MIN <= p.ctx <= b2.CTX_MAX


def test_plan_old_driver_or_no_gpu_uses_vulkan_or_cpu_with_ptq1():
    p = b2.plan(_hw(vram_total_gb=24, vram_free_gb=24, cuda_level=(12, 2), vulkan=True))
    assert (p.backend, p.quant) == ("vulkan", "PTQ1_0")
    assert p.assets[0][0] == "llama-prism-b10685-7dffb15-bin-ubuntu-vulkan-x64.tar.gz"
    p = b2.plan(_hw(ram_gb=16), backend="cpu")
    assert (p.backend, p.quant, p.ngl) == ("cpu", "PTQ1_0", 0) and p.notes


def test_cpu_only_is_never_chosen_silently():
    """~0.5 tok/s measured: auto refuses and names the way out."""
    with pytest.raises(hc.HomeError, match="--backend cpu"):
        b2.plan(_hw(ram_gb=64))
    with pytest.raises(hc.HomeError, match="0.5 tokens/s"):
        b2.plan(_hw(vram_total_gb=32, vram_free_gb=0.9, cuda_level=(13, 0)))


def test_plan_shared_gpu_prefers_the_file_that_fits():
    p = b2.plan(_hw(vram_total_gb=32, vram_free_gb=8, cuda_level=(13, 0)))
    assert (p.quant, p.ngl) == ("PTQ1_0", 99)
    p = b2.plan(_hw(vram_total_gb=32, vram_free_gb=4, cuda_level=(13, 0)))
    assert p.quant == "PTQ1_0" and 0 < p.ngl < b2.N_LAYERS and p.notes
    # the GPU share of weights + KV must fit what is free
    gpu = p.ngl / b2.N_LAYERS * (p.weights.size + p.ctx * b2.KV_BYTES_PER_TOKEN)
    assert gpu <= (4 - 1.5) * b2.GIB and p.ctx == b2.MIN_USEFUL_CTX


def test_plan_low_memory_and_unknown_platform_refuse():
    with pytest.raises(hc.HomeError, match="12 GB"):
        b2.plan(_hw(ram_gb=8))
    with pytest.raises(hc.HomeError, match="gibberish"):
        b2.plan(_hw(arch="riscv64"))
    with pytest.raises(hc.HomeError, match="--quant"):
        b2.plan(_hw(), quant="Q2_0")
    with pytest.raises(hc.HomeError, match="metal"):
        b2.plan(_hw(), backend="metal")


def test_every_platform_asset_is_pinned_to_the_shipped_release():
    for (_os, _arch, _be), assets in b2.ASSETS.items():
        for name, sha in assets:
            assert len(sha) == 64 and int(sha, 16) >= 0
            assert b2.RELEASE in name or name.startswith("cudart-")
    assert b2.GGUFS["PTQ1_0"].sha256 == \
        "53107f530aa52eb00912263ab1ee29bd199261c87cd7b4ad4ca1318c1fe33ee3"  # the bundle ladder


# ---------------------------------------------------------------- fetch

def _serve_bytes(monkeypatch, payload: bytes):
    def fake_download(url, part, say, expected_size=0):
        part.write_bytes(payload)
    monkeypatch.setattr(b2, "_download", fake_download)


def test_checksum_mismatch_is_refused_and_nothing_lands(tmp_path, monkeypatch):
    _serve_bytes(monkeypatch, b"not the model")
    dest = tmp_path / "m.gguf"
    with pytest.raises(hc.HomeError, match="refusing"):
        b2.fetch_verified(["https://a/x", "https://b/x"], dest, "0" * 64, lambda s: None)
    assert not dest.exists()
    assert not (tmp_path / "m.gguf.part").exists()


def test_matching_bytes_land_and_are_stamped(tmp_path, monkeypatch):
    data = b"bonsai bytes"
    sha = hashlib.sha256(data).hexdigest()
    _serve_bytes(monkeypatch, data)
    dest = b2.fetch_verified(["https://a/x"], tmp_path / "m.gguf", sha, lambda s: None)
    assert dest.read_bytes() == data
    assert b2._stamp_ok(tmp_path, dest, sha)
    dest.write_bytes(b"tampered!!!!")             # same size, new mtime -> re-hash
    _serve_bytes(monkeypatch, data)
    assert b2.fetch_verified(["https://a/x"], dest, sha, lambda s: None).read_bytes() == data


def test_a_dropped_stream_resumes_on_the_same_source(tmp_path, monkeypatch):
    data = b"0123456789"
    sha = hashlib.sha256(data).hexdigest()
    calls = []

    def flaky(url, part, say, expected_size=0):
        calls.append(url)
        have = part.stat().st_size if part.exists() else 0
        with open(part, "ab") as f:
            f.write(data[have:have + 4])
        if part.stat().st_size < len(data):
            raise ConnectionError("reset by peer")
    monkeypatch.setattr(b2, "_download", flaky)
    monkeypatch.setattr(b2.time, "sleep", lambda s: None)
    dest = b2.fetch_verified(["https://a/x", "https://b/x"], tmp_path / "m", sha,
                             lambda s: None, len(data))
    assert dest.read_bytes() == data and set(calls) == {"https://a/x"}


def test_resume_sends_a_range_header(tmp_path, monkeypatch):
    import httpx

    part = tmp_path / "m.part"
    part.write_bytes(b"abc")
    seen = {}

    class _Stream:
        status_code = 206
        headers = {"content-length": "3"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, n):
            yield b"def"

    def fake_stream(method, url, headers=None, **kw):
        seen.update(headers or {})
        return _Stream()

    monkeypatch.setattr(httpx, "stream", fake_stream)
    b2._download("https://x/m", part, lambda s: None, 6)
    assert seen["Range"] == "bytes=3-"
    assert part.read_bytes() == b"abcdef"


def test_tar_with_escaping_member_is_refused(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo("../evil")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    arc = tmp_path / "a.tar.gz"
    arc.write_bytes(buf.getvalue())
    with pytest.raises(hc.HomeError, match="unsafe"):
        b2._safe_extract(arc, tmp_path / "out")


# ---------------------------------------------------------------- no stock fallback

def _fake_server(tmp_path: Path, version_line: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    if sys.platform == "win32":
        exe = d / "llama-server.cmd"
        exe.write_text(f"@echo {version_line}\n", encoding="utf-8")
    else:
        exe = d / "llama-server"
        exe.write_text(f"#!/bin/sh\necho '{version_line}'\n", encoding="utf-8")
        exe.chmod(0o755)
    return exe


def test_stock_llama_server_is_refused(tmp_path):
    exe = _fake_server(tmp_path, "version: 6500 (abc1234) built with gcc")
    with pytest.raises(hc.HomeError, match="gibberish"):
        b2.check_prism_build(exe)


def test_pinned_prism_build_is_accepted(tmp_path):
    exe = _fake_server(tmp_path, "version: 10685 (7dffb15) built with gcc")
    assert "7dffb15" in b2.check_prism_build(exe)


def test_install_never_looks_on_path(tmp_path, monkeypatch):
    """A llama-server on PATH must not be used even when the download fails."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda *a, **k: "/usr/bin/llama-server")

    def boom(*a, **k):
        raise hc.HomeError("refusing: offline")
    monkeypatch.setattr(b2, "fetch_verified", boom)
    p = b2.plan(_hw(ram_gb=32), backend="cpu")
    with pytest.raises(hc.HomeError, match="refusing"):
        b2.install(p, tmp_path, lambda s: None)


def test_server_args_bound_thinking_and_bind_loopback(tmp_path):
    p = b2.plan(_hw(ram_gb=32), backend="cpu")
    args = b2.server_args(p, tmp_path / "m.gguf")
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--alias") + 1] == b2.ALIAS
    assert args[args.index("--reasoning-budget") + 1] == "2048"


# ---------------------------------------------------------------- never kill others

def test_stop_does_not_signal_a_pid_that_is_not_ours(tmp_path, monkeypatch):
    (tmp_path / b2.STATE_FILE).write_text(json.dumps(
        {"pid": 4242, "server": str(tmp_path / "bin" / "llama-server")}), encoding="utf-8")
    monkeypatch.setattr(b2, "_cmdline", lambda pid: "/usr/bin/python3 something_else.py")
    killed = []
    monkeypatch.setattr(b2.os, "kill", lambda *a: killed.append(a))
    assert "nothing was signalled" in b2.stop(tmp_path)
    assert killed == []


def test_start_refuses_a_port_held_by_someone_else(tmp_path, monkeypatch):
    monkeypatch.setattr(b2, "_served_models", lambda port: ["some-other-model"])
    p = b2.plan(_hw(ram_gb=32), backend="cpu")
    with pytest.raises(hc.HomeError, match="left alone"):
        b2.start(p, tmp_path / "llama-server", tmp_path / "m.gguf", tmp_path,
                 lambda s: None)


# ---------------------------------------------------------------- CLI

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(hc.HOME_ENV, str(tmp_path / "agent-home"))
    hc.init_home(name="pip")
    return tmp_path / "agent-home"


def _parse(argv):
    import argparse

    ap = argparse.ArgumentParser()
    home_cli.register_parser(ap.add_subparsers(dest="cmd"))
    return ap.parse_args(argv)


def test_cli_flags_parse():
    a = _parse(["home", "model", "--local", "bonsai2", "--quant", "PTQ1_0",
                "--backend", "cpu", "--data-dir", "D:/x", "--port", "9001", "--dry-run"])
    assert (a.local, a.quant, a.backend, a.data_dir, a.port, a.dry_run) == \
        ("bonsai2", "PTQ1_0", "cpu", "D:/x", 9001, True)
    with pytest.raises(SystemExit):
        _parse(["home", "model", "--local", "bonsai2", "--quant", "Q2_0"])


def test_cli_sets_preset_after_install(home, tmp_path, monkeypatch):
    calls = {}

    def fake_install(**kw):
        calls.update(kw)
        return {"base_url": "http://127.0.0.1:9001/v1", "data_dir": str(tmp_path / "d"),
                "model": b2.ALIAS, "plan": {}}
    monkeypatch.setattr(b2, "install_and_start", fake_install)
    a = _parse(["home", "model", "--local", "bonsai2", "--port", "9001"])
    assert home_cli.cmd_model(a) == 0
    cfg = hc.load_config()
    assert (cfg.model.provider, cfg.model.model, cfg.model.base_url) == \
        ("bonsai2", b2.ALIAS, "http://127.0.0.1:9001/v1")
    assert calls["port"] == 9001
    assert json.loads((home / "bonsai2.json").read_text())["data_dir"] == str(tmp_path / "d")


def test_cli_failed_install_leaves_the_preset_alone(home, monkeypatch):
    def fail(**kw):
        raise hc.HomeError("refusing x: sha256 mismatch")
    monkeypatch.setattr(b2, "install_and_start", fail)
    before = hc.load_config().model.provider
    assert home_cli.cmd_model(_parse(["home", "model", "--local", "bonsai2"])) == 1
    assert hc.load_config().model.provider == before
