"""`adk bonsai-local --model ling-tiny-8b`: a MOUNTED model, not a baked image.

The two Bonsai entries are images with the weights inside. The MoE tier is a public
llama.cpp image plus a GGUF downloaded once and mounted read-only (issue #10547). That adds
ways to be wrong that a baked image cannot have:

  * serving a download that is not the catalogued file (truncated, swapped bytes);
  * overwriting a file the user already had at that path;
  * changing the command the Bonsai entries run while adding the new one.

These tests fail on each.
"""

import hashlib
import io
import shlex
import types

import pytest
from adk import cli


def _args(**kw):
    base = {"model": "ling-tiny-8b", "port": 0, "dry_run": True, "stop": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _fake_docker(monkeypatch, tmp_path, runtimes: str):
    """Docker 'exists', reports `runtimes`, has no container running; calls are recorded."""
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        out = runtimes if cmd[:2] == ["docker", "info"] else ""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("AITHER_BONSAI_GGUF", str(tmp_path / "model.gguf"))
    for var in ("AITHER_BONSAI_IMAGE", "AITHER_BONSAI_MODEL", "AITHER_BONSAI_PORT",
                "AITHER_BONSAI_CONTAINER", "AITHER_BONSAI_GPUS"):
        monkeypatch.delenv(var, raising=False)
    return calls


@pytest.fixture
def cpu_docker(monkeypatch, tmp_path):
    return _fake_docker(monkeypatch, tmp_path, "{}")


@pytest.fixture
def nvidia_docker(monkeypatch, tmp_path):
    return _fake_docker(monkeypatch, tmp_path, '{"nvidia":{"path":"nvidia-container-runtime"}}')


def _command_line(out: str) -> list[str]:
    line = next(ln for ln in out.splitlines() if "command    :" in ln)
    return shlex.split(line.split(":", 1)[1])


MOUNTED = sorted(k for k, v in cli.BONSAI_LOCAL_MODELS.items() if v.get("gguf_file"))
BAKED = sorted(k for k, v in cli.BONSAI_LOCAL_MODELS.items() if not v.get("gguf_file"))


def test_the_split_is_what_this_file_assumes():
    assert MOUNTED == ["ling-tiny-8b"] and BAKED == ["bonsai-27b", "bonsai2-27b"]


@pytest.mark.parametrize("model", MOUNTED)
def test_every_mounted_entry_pins_url_size_sha_and_an_image_digest(model):
    spec = cli.BONSAI_LOCAL_MODELS[model]
    assert len(spec["gguf_sha256"]) == 64 and int(spec["gguf_sha256"], 16) >= 0
    assert int(spec["gguf_bytes"]) > 1 << 30
    assert spec["gguf_url"].startswith("https://")
    assert spec["gguf_url"].endswith(spec["gguf_file"])
    assert "@sha256:" in spec["image"], "a floating tag changes the engine under a pinned model"


def test_dry_run_mounts_the_gguf_and_passes_server_args(cpu_docker, capsys, monkeypatch, tmp_path):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("a dry run must not download"))
    assert cli.cmd_bonsai_local(_args()) == 0
    spec = cli.BONSAI_LOCAL_MODELS["ling-tiny-8b"]
    f = spec["gguf_file"]
    assert _command_line(capsys.readouterr().out) == [
        "docker", "run", "-d", "--name", "aither-bonsai-local", "--restart", "unless-stopped",
        "-v", f"{tmp_path / 'model.gguf'}:/models/{f}:ro",
        "-p", "127.0.0.1:8090:8090", spec["image"],
        "-m", f"/models/{f}", "--host", "0.0.0.0", "--port", "8090", "-ngl", "0", "-c", "8192",
    ]
    assert not (tmp_path / "model.gguf").exists()


def test_the_mounted_tier_stays_cpu_even_when_docker_has_nvidia(nvidia_docker, capsys):
    assert cli.cmd_bonsai_local(_args()) == 0
    cmd = _command_line(capsys.readouterr().out)
    assert "--gpus" not in cmd and cmd[cmd.index("-ngl") + 1] == "0"


@pytest.mark.parametrize("model", BAKED)
def test_the_baked_command_is_exactly_what_it_was_on_cpu(cpu_docker, capsys, model):
    assert cli.cmd_bonsai_local(_args(model=model)) == 0
    assert _command_line(capsys.readouterr().out) == [
        "docker", "run", "-d", "--name", "aither-bonsai-local", "--restart", "unless-stopped",
        "-p", "127.0.0.1:8090:8090", cli.BONSAI_LOCAL_MODELS[model]["image"],
    ]


@pytest.mark.parametrize("model", BAKED)
def test_the_baked_command_is_exactly_what_it_was_on_gpu(nvidia_docker, capsys, model):
    assert cli.cmd_bonsai_local(_args(model=model)) == 0
    assert _command_line(capsys.readouterr().out) == [
        "docker", "run", "-d", "--name", "aither-bonsai-local", "--restart", "unless-stopped",
        "--gpus", "all", "-p", "127.0.0.1:8090:8090", cli.BONSAI_LOCAL_MODELS[model]["image"],
    ]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


GOOD = b"GGUF" + b"\x01" * 4096


def _spec_for(payload: bytes) -> dict[str, str]:
    return {"gguf_file": "m.gguf", "gguf_url": "https://example.invalid/m.gguf",
            "gguf_sha256": hashlib.sha256(payload).hexdigest(), "gguf_bytes": str(len(payload))}


def test_a_matching_download_is_kept(tmp_path, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(GOOD))
    path = str(tmp_path / "models" / "m.gguf")
    spec = _spec_for(GOOD)
    assert cli._bonsai_local_fetch_gguf(spec, path) is True
    assert cli._bonsai_local_gguf_present(spec, path) is True
    assert not (tmp_path / "models" / "m.gguf.part").exists()


@pytest.mark.parametrize("served", [b"GGUF" + b"\x02" * 4096, b"GGUF" + b"\x01" * 100],
                         ids=["same-size-different-bytes", "truncated"])
def test_a_tampered_or_truncated_download_is_deleted(tmp_path, monkeypatch, served):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(served))
    assert cli._bonsai_local_fetch_gguf(_spec_for(GOOD), str(tmp_path / "m.gguf")) is False
    assert list(tmp_path.iterdir()) == [], "nothing may be left to be mounted by a later run"


class _Breaks(_Resp):
    def __init__(self, exc):
        super().__init__(b"")
        self._exc = exc
        self._sent = False

    def read(self, n=-1):
        if not self._sent:
            self._sent = True
            return b"GGUF"
        raise self._exc


def test_a_connection_cut_mid_stream_is_reported_and_cleaned_up(tmp_path, monkeypatch):
    import http.client
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Breaks(http.client.IncompleteRead(b"GGUF")))
    assert cli._bonsai_local_fetch_gguf(_spec_for(GOOD), str(tmp_path / "m.gguf")) is False
    assert list(tmp_path.iterdir()) == []


def test_ctrl_c_propagates_and_leaves_no_partial_file(tmp_path, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Breaks(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        cli._bonsai_local_fetch_gguf(_spec_for(GOOD), str(tmp_path / "m.gguf"))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("missing", ["gguf_url", "gguf_sha256", "gguf_bytes"])
def test_an_entry_without_a_full_pin_never_downloads(tmp_path, monkeypatch, missing):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not fetch an unverifiable file"))
    spec = _spec_for(GOOD)
    del spec[missing]
    assert cli._bonsai_local_fetch_gguf(spec, str(tmp_path / "m.gguf")) is False
    assert cli._bonsai_local_gguf_present(spec, str(tmp_path / "m.gguf")) is False


def _docker_runs(calls):
    return [c for c in calls if c[:2] == ["docker", "run"]]


def test_a_real_run_refuses_to_start_on_a_bad_download(cpu_docker, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(b"not the model"))
    assert cli.cmd_bonsai_local(_args(dry_run=False)) == 1
    assert _docker_runs(cpu_docker) == []
    assert ["docker", "rm", "-f", "aither-bonsai-local"] not in cpu_docker, \
        "a failed download must not remove the container that was there before"


def test_a_real_run_never_overwrites_a_file_already_at_the_path(cpu_docker, monkeypatch, tmp_path):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not download over the user's file"))
    mine = tmp_path / "model.gguf"
    mine.write_bytes(b"the user's own quant")
    assert cli.cmd_bonsai_local(_args(dry_run=False)) == 1
    assert mine.read_bytes() == b"the user's own quant"
    assert _docker_runs(cpu_docker) == []
