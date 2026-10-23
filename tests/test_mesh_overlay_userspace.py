"""One overlay detection for the lend and the worker, userspace-aware, and the heartbeat
fields that carry lending/worker/gaming state and the device's own tailnet IPs.

Measured 2026-10-22 on the owner's Steam Deck: the lend served on 100.64.0.42 (bound
127.0.0.1, userspace tailscale) while `worker-on`, run by the beat, said "Tailscale is
not installed" -- the beat had no ~/.local/bin on its PATH. And the Tunnel dashboard
listed one phone and one desktop twice, because Identity never knew their tailnet IPs.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from adk import enrollment, mesh, mesh_lend, mesh_worker

KEY = "ab" * 32
NODE = "steamdeck"
STATUS = {"BackendState": "Running", "TUN": False,
          "Self": {"HostName": "steamdeck", "TailscaleIPs": ["100.64.0.42", "fd7a::2a"]}}


@pytest.fixture(autouse=True)
def _dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_LEND_DIR", str(tmp_path / "lend"))
    monkeypatch.setenv("AITHER_WORKER_DIR", str(tmp_path / "worker"))
    monkeypatch.delenv("TS_SOCKET", raising=False)
    monkeypatch.delenv("AITHER_TAILSCALE_SOCKET", raising=False)
    yield tmp_path


# ── finding tailscale ────────────────────────────────────────────────────────


def test_a_user_install_in_the_home_directory_is_found(tmp_path, monkeypatch):
    home = tmp_path / "home"
    binary = home / ".local" / "bin" / "tailscale"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(mesh.shutil, "which", lambda name: None)
    monkeypatch.setattr(mesh, "_TAILSCALE_KNOWN_PATHS", ())
    assert Path(mesh._tailscale(proc=str(tmp_path / "noproc"))) == binary


def test_the_running_tailscaled_names_its_socket_and_its_cli(tmp_path):
    proc = tmp_path / "proc" / "321"
    proc.mkdir(parents=True)
    sock = tmp_path / "ts.sock"
    sock.write_text("")
    (proc / "cmdline").write_bytes(b"\x00".join([
        b"/home/deck/tailscale/tailscaled", b"--tun=userspace-networking",
        b"--socket=" + str(sock).encode(), b"--state=/home/deck/ts.state"]) + b"\x00")
    procs = mesh._tailscaled_procs(str(tmp_path / "proc"))
    assert procs and procs[0][1][0].endswith("tailscaled")
    assert mesh._socket_from_argv(procs[0][1]) == str(sock)
    assert mesh._socket_from_argv(["tailscaled", "--socket", "/run/x.sock"]) == "/run/x.sock"


def test_no_cli_but_tailscaled_answers_on_its_socket(monkeypatch):
    """Old code: tailscale_missing. Now: tailscaled's LocalAPI is asked directly."""
    monkeypatch.setattr(mesh, "_tailscale", lambda proc="/proc": None)
    monkeypatch.setattr(mesh, "tailscale_socket", lambda proc="/proc": "/home/deck/ts.sock")
    asked = []
    monkeypatch.setattr(mesh, "_localapi_status",
                        lambda sock, timeout=5.0: asked.append(sock) or STATUS)
    ov = mesh.overlay_status()
    assert ov["joined"] is True and ov["code"] == "ok" and ov["via"] == "localapi"
    assert ov["tun"] is False and ov["tailnet_ip"] == "100.64.0.42"
    assert ov["tailnet_ips"] == ["100.64.0.42"]  # IPv6 / off-tailnet addresses dropped
    assert asked[0] == "/home/deck/ts.sock"


def test_the_cli_is_pointed_at_a_userspace_socket(monkeypatch):
    monkeypatch.setattr(mesh, "_tailscale", lambda proc="/proc": "/home/deck/tailscale/tailscale")
    monkeypatch.setattr(mesh, "tailscale_socket", lambda proc="/proc": "/home/deck/ts.sock")
    seen = []

    def run(argv, **k):
        seen.append(argv)
        out = json.dumps(STATUS) if "status" in argv else "{}"
        return SimpleNamespace(returncode=0, stdout=out)

    monkeypatch.setattr(mesh.subprocess, "run", run)
    ov = mesh.overlay_status()
    assert ov["joined"] and seen[0][:3] == ["/home/deck/tailscale/tailscale", "--socket",
                                            "/home/deck/ts.sock"]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name == "nt",
                    reason="unix sockets")
def test_localapi_status_over_a_real_unix_socket(tmp_path):
    path = str(tmp_path / "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    req = []

    def serve():
        conn, _ = srv.accept()
        req.append(conn.recv(4096))
        body = json.dumps(STATUS).encode()
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + body)
        conn.close()

    t = threading.Thread(target=serve)
    t.start()
    st = mesh._localapi_status(path, timeout=5)
    t.join(5)
    srv.close()
    assert st == STATUS and req[0].startswith(b"GET /localapi/v0/status ")


# ── one detection for the lend and the worker ───────────────────────────────


def _lend(pid=4242, ip="100.64.0.42", bind="127.0.0.1"):
    mesh_lend.write_state({"pid": pid, "port": 50052, "ip": ip, "bind": bind,
                           "lend_mb": 14336, "exe": "/home/deck/.aither/lend/rpc-server"})


def test_a_running_lend_vouches_for_the_overlay(monkeypatch):
    _lend()
    missing = {"joined": False, "code": "tailscale_missing",
               "detail": "Tailscale is not installed"}
    ov = mesh_lend.serving_overlay(lambda: missing, pid_alive=lambda pid: pid == 4242)
    assert ov["joined"] and ov["tailnet_ip"] == "100.64.0.42" and ov["tun"] is False
    assert ov["via"] == "mesh-lend" and "not installed" in ov["detail"]
    # a dead lend vouches for nothing
    assert mesh_lend.serving_overlay(lambda: missing, pid_alive=lambda pid: False) == missing


def test_worker_on_uses_the_lend_overlay_and_binds_loopback_in_userspace(monkeypatch, tmp_path):
    """The Deck case end to end: tailscale invisible to the beat, the lend alive on
    100.64.0.42 in userspace mode -> the worker serves on 127.0.0.1, never 0.0.0.0."""
    _lend()
    monkeypatch.setattr(mesh_lend, "_overlay",
                        lambda: {"joined": False, "code": "tailscale_missing",
                                 "detail": "Tailscale is not installed"})
    monkeypatch.setattr(mesh_lend, "_pid_alive", lambda pid: pid == 4242)
    from adk import game_guard

    monkeypatch.setattr(game_guard, "pid_is", lambda pid, exe, **k: pid == 4242 and bool(exe))
    monkeypatch.setattr(mesh_worker, "install_persistence", lambda *a, **k: "none")
    monkeypatch.setattr(mesh_worker, "_allow_firewall", lambda *a, **k: True)
    monkeypatch.setattr(mesh_worker, "running_state", lambda *a, **k: None)
    started = {}

    class _P:
        pid = 555

    def popen(argv, **kw):
        started["argv"] = argv
        return _P()

    lines = []
    ns = argparse.Namespace(worker_action="on", model="", parallel=2, port=8488, json=False)
    rc = mesh_worker.cmd_on(
        ns, key_record=(NODE, KEY), popen=popen,
        accel=SimpleNamespace(kind="cpu", name="AMD Custom APU 0405", vram_gb=0.0,
                              ram_gb=16.0, os_family="linux", arch="x64"),
        install=lambda m, p, a: (tmp_path / "llama-server", tmp_path / "w.gguf"),
        probe=lambda url: True, sleep=lambda s: None,
        say=lambda t, e=False: lines.append(t))
    assert rc == 0, lines
    argv = started["argv"]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in argv
    st = mesh_worker.read_state()
    assert st["ip"] == "100.64.0.42" and st["bind"] == "127.0.0.1"


def test_the_worker_never_binds_all_interfaces():
    with pytest.raises(mesh_worker.WorkerError):
        mesh_worker.server_argv("srv", "m.gguf", "0.0.0.0", 8488, "k", "bonsai-1.7b", 1, False)
    ov = {"tun": True, "tailnet_ip": "100.64.0.51"}
    assert mesh_lend.bind_host(ov) == "100.64.0.51"
    assert mesh_lend.bind_host({"tun": False, "tailnet_ip": "100.64.0.42"}) == "127.0.0.1"


# ── the heartbeat carries it ─────────────────────────────────────────────────


def test_mesh_beat_fields(monkeypatch):
    monkeypatch.setattr(enrollment, "_tailnet_ips", lambda now=None: ["100.64.0.51"])
    monkeypatch.setattr(mesh_lend, "status_dict", lambda: {
        "lending": True, "lend_mb": 14336, "ip": "100.64.0.51", "port": 50052})
    monkeypatch.setattr(mesh_worker, "read_state", lambda: {"model": "bonsai-1.7b"})
    monkeypatch.setattr(mesh_worker, "status_dict", lambda: {
        "serving": True, "healthy": True, "model": "bonsai-1.7b", "parallel": 2,
        "ip": "100.64.0.51", "port": 8488})
    from adk import game_guard

    monkeypatch.setattr(game_guard, "status", lambda st=None: {"gaming": False,
                                                                "yielding": False})
    f = enrollment.mesh_beat_fields()
    assert f["tailnet_ips"] == ["100.64.0.51"]
    assert f["mesh"] == {
        "gaming": False, "yielding": False, "lending": True, "lend_mb": 14336,
        "endpoint": "100.64.0.51:50052",
        "worker": {"serving": True, "healthy": True, "model": "bonsai-1.7b", "parallel": 2,
                   "yielding": False, "endpoint": "100.64.0.51:8488"}}


def test_a_yielding_worker_is_reported_as_yielding(monkeypatch):
    monkeypatch.setattr(enrollment, "_tailnet_ips", lambda now=None: [])
    monkeypatch.setattr(mesh_lend, "status_dict", lambda: {"lending": True, "lend_mb": 1024,
                                                           "ip": "100.64.0.42", "port": 50052})
    monkeypatch.setattr(mesh_worker, "read_state", lambda: {"model": "bonsai-8b"})
    monkeypatch.setattr(mesh_worker, "status_dict", lambda: {"serving": False,
                                                             "model": "bonsai-8b"})
    from adk import game_guard

    monkeypatch.setattr(game_guard, "status", lambda st=None: {"gaming": True,
                                                                "yielding": True})
    f = enrollment.mesh_beat_fields()
    assert "tailnet_ips" not in f
    assert f["mesh"]["gaming"] and f["mesh"]["yielding"] and f["mesh"]["lending"]
    assert f["mesh"]["worker"]["serving"] is False and f["mesh"]["worker"]["yielding"] is True


def test_tailnet_ips_are_cached(monkeypatch):
    monkeypatch.undo()  # the conftest stub off: this tests the real reader
    calls = []
    monkeypatch.setattr(mesh_lend, "serving_overlay",
                        lambda: calls.append(1) or {"tailnet_ips": ["100.64.0.42"]})
    enrollment._tailnet_cache.update(at=0.0, ips=[])
    assert enrollment._tailnet_ips(now=10_000.0) == ["100.64.0.42"]
    assert enrollment._tailnet_ips(now=10_100.0) == ["100.64.0.42"] and len(calls) == 1
    assert enrollment._tailnet_ips(now=10_000.0 + enrollment.TAILNET_IP_TTL_S + 1)
    assert len(calls) == 2


def test_every_beat_posts_the_mesh_fields(monkeypatch, tmp_path):
    import asyncio

    import httpx

    from adk import fleet_enroll

    monkeypatch.setattr(fleet_enroll, "_AITHER_DIR", tmp_path)
    monkeypatch.setattr(fleet_enroll, "_NODE_AUTH_FILE", tmp_path / "node_auth.json")
    monkeypatch.setattr(enrollment, "_heartbeat_task", None)
    monkeypatch.setattr(enrollment, "_heartbeat_state", enrollment._new_heartbeat_state())
    monkeypatch.setattr(enrollment, "build_registration", lambda node_id, **k: {
        "inference_ready": False, "available_models": [], "gpu_vram_mb": 0,
        "inference_url": "", "inference_kind": "none"})
    monkeypatch.setattr(enrollment, "mesh_beat_fields", lambda: {
        "tailnet_ips": ["100.64.0.51"], "mesh": {"lending": True, "lend_mb": 14336}})
    posts = []

    class _R:
        status_code = 200
        text = "{}"

        def json(self):
            return {"status": "ok"}

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            posts.append(k.get("json"))
            return _R()

        async def aclose(self):
            return None

    monkeypatch.setattr(httpx, "AsyncClient", _C)
    asyncio.run(enrollment.heartbeat_loop("https://idp.example.test", "t", "n", interval=0,
                                          max_beats=1))
    assert posts and posts[0]["tailnet_ips"] == ["100.64.0.51"]
    assert posts[0]["mesh"] == {"lending": True, "lend_mb": 14336}


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")
def test_a_group_or_world_writable_user_binary_is_not_trusted(tmp_path, monkeypatch):
    home = tmp_path / "home"
    binary = home / ".local" / "bin" / "tailscale"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o777)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(mesh.shutil, "which", lambda name: None)
    monkeypatch.setattr(mesh, "_TAILSCALE_KNOWN_PATHS", ())
    assert mesh._tailscale(proc=str(tmp_path / "noproc")) is None
    binary.chmod(0o755)
    assert Path(mesh._tailscale(proc=str(tmp_path / "noproc"))) == binary


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_another_users_tailscaled_is_ignored(tmp_path, monkeypatch):
    proc = tmp_path / "proc" / "321"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"tailscaled\x00--socket=/tmp/evil.sock\x00")
    monkeypatch.setattr(mesh, "_trusted_owner", lambda path: False)
    assert mesh._tailscaled_procs(str(tmp_path / "proc")) == []
