"""The installed local model server: found where install-bonsai put it, started natively.

Customer report 2026-10-14: `adk onboard --quick` on a Windows PC with the one-click
Bonsai install and no Docker chose the Docker-only `adk bonsai-local` path and failed
("Docker not found"), and the installed llama-server only came back at the next sign-in.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk import local_model_server as lms  # noqa: E402
from adk.cli import cmd_quickstart_local  # noqa: E402


def _selfhost(tmp_path: Path, *, backend: str = "cuda", pid_line: str = "") -> Path:
    root = tmp_path / "Aitherium" / "bonsai"
    (root / "bin").mkdir(parents=True)
    (root / "models").mkdir()
    (root / "bin" / "llama-server.exe").write_bytes(b"MZ")
    old = root / "models" / "Bonsai-8B.gguf"
    old.write_bytes(b"x")
    os.utime(old, (1_000_000, 1_000_000))
    (root / "models" / "Ternary-Bonsai-2-27B-PTQ1_0.gguf").write_bytes(b"x")
    (root / "models" / "mmproj-Bonsai-2-27B-Q8_0.gguf").write_bytes(b"x")
    (root / "release.txt").write_text(f"prism-b10685-7dffb15/{backend}\n", encoding="utf-8")
    if pid_line:
        (root / "server.pid").write_text(pid_line, encoding="utf-8")
    return root


def test_finds_the_windows_installer_layout_and_serves_its_arguments(tmp_path):
    root = _selfhost(tmp_path, pid_line="4242 8081\n")
    spec = lms.find_local_model_server(env={"LOCALAPPDATA": str(tmp_path)}, platform="win32",
                                       home=tmp_path / "home")
    assert spec is not None and spec["kind"] == "bonsai-selfhost"
    argv = spec["argv"]
    assert argv[0] == str(root / "bin" / "llama-server.exe")
    # the newest weights, never the vision tower
    assert argv[argv.index("--model") + 1].endswith("Ternary-Bonsai-2-27B-PTQ1_0.gguf")
    assert argv[argv.index("--port") + 1] == "8081" and spec["port"] == 8081
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--reasoning-budget") + 1] == "2048"
    assert argv[argv.index("--alias") + 1] == "bonsai-selfhost"
    assert "-ngl" in argv
    assert spec["health"] == "http://127.0.0.1:8081/health"


def test_cpu_build_does_not_offload_and_default_port_is_8080(tmp_path):
    _selfhost(tmp_path, backend="cpu")
    spec = lms.find_local_model_server(env={"LOCALAPPDATA": str(tmp_path)}, platform="win32",
                                       home=tmp_path / "home")
    assert "-ngl" not in spec["argv"]
    assert spec["port"] == 8080


def test_nothing_installed_is_none(tmp_path):
    assert lms.find_local_model_server(env={"LOCALAPPDATA": str(tmp_path)}, platform="win32",
                                       home=tmp_path / "home") is None


def test_start_reuses_a_server_that_already_answers():
    popen = MagicMock()
    res = lms.start_local_model_server({"health": "h", "argv": ["x"], "cwd": ".", "log": "l"},
                                       popen=popen, is_healthy=lambda _u: True)
    assert res["ok"] is True and res["started"] is False
    popen.assert_not_called()


def test_start_spawns_and_waits_for_health(tmp_path):
    answers = iter([False, False, True])
    proc = MagicMock(pid=77)
    proc.poll.return_value = None
    popen = MagicMock(return_value=proc)
    spec = {"health": "h", "argv": ["llama-server", "--port", "8080"], "cwd": str(tmp_path),
            "log": str(tmp_path / "server.log")}
    res = lms.start_local_model_server(spec, popen=popen, is_healthy=lambda _u: next(answers),
                                       sleep=lambda _s: None)
    assert res["ok"] is True and res["started"] is True and res["pid"] == 77
    assert popen.call_args[0][0] == ["llama-server", "--port", "8080"]


def test_start_reports_a_server_that_dies_while_loading(tmp_path):
    proc = MagicMock()
    proc.poll.return_value = 1
    spec = {"health": "h", "argv": ["llama-server"], "cwd": str(tmp_path),
            "log": str(tmp_path / "s.log")}
    res = lms.start_local_model_server(spec, popen=MagicMock(return_value=proc),
                                       is_healthy=lambda _u: False, sleep=lambda _s: None)
    assert res["ok"] is False and "exited" in res["error"]


def _args(**kw):
    base = {"backend": "auto", "model": None, "port": 8209, "dry_run": False, "api_key": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _accel():
    return MagicMock(os_family="windows", arch="x64", kind="cuda", name="GPU",
                     vram_gb=32.0, ram_gb=64.0)


def test_quickstart_bonsai_without_docker_starts_the_installed_server():
    spec = {"found": True, "kind": "bonsai-selfhost", "argv": ["llama-server.exe"], "cwd": ".",
            "port": 8080, "model": "bonsai-selfhost", "weights": "w.gguf", "health": "h",
            "inference_url": "http://127.0.0.1:8080/v1", "log": "l"}
    with patch("adk.llamacpp_setup.detect_accel", return_value=_accel()), \
         patch("adk.local_backends.pick_backend", return_value="bonsai"), \
         patch("adk.local_backends.docker_available", return_value=False), \
         patch("adk.local_model_server.find_local_model_server", return_value=spec), \
         patch("adk.local_model_server.start_local_model_server",
               return_value={"ok": True}) as start, \
         patch("adk.cli.cmd_bonsai_local") as docker_path, \
         patch("adk.llamacpp_setup.smoke_test", return_value=True) as smoke, \
         patch("adk.cli.save_saved_config") as save:
        rc = cmd_quickstart_local(_args())
    assert rc == 0
    start.assert_called_once()
    docker_path.assert_not_called()
    assert smoke.call_args.kwargs["port"] == 8080
    saved = save.call_args[0][0]
    assert saved["inference_url"] == "http://127.0.0.1:8080/v1"
    assert saved["default_backend"] == "bonsai-local"


def test_quickstart_bonsai_with_neither_server_nor_docker_falls_to_llamacpp():
    result = MagicMock(success=True, port=8209, quant="Q4_K_M")
    with patch("adk.llamacpp_setup.detect_accel", return_value=_accel()), \
         patch("adk.local_backends.pick_backend", return_value="bonsai"), \
         patch("adk.local_backends.docker_available", return_value=False), \
         patch("adk.local_model_server.find_local_model_server", return_value=None), \
         patch("adk.cli.cmd_bonsai_local") as docker_path, \
         patch("adk.llamacpp_setup.install", return_value=result) as install, \
         patch("adk.llamacpp_setup.status", return_value=MagicMock(running=True)), \
         patch("adk.llamacpp_setup.smoke_test", return_value=True), \
         patch("adk.cli.save_saved_config"):
        rc = cmd_quickstart_local(_args())
    assert rc == 0
    install.assert_called_once()
    docker_path.assert_not_called()
