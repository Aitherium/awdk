"""adk mesh worker: a small model on the tailnet takes whole small jobs for the fleet.

Pins the device half of the parallel-worker contract: tailnet-only bind, parallel 1..4,
the API key derived from the command key (a fixed vector the fleet and the Android app
pin too), the hardware -> model choice, the closed worker-on / worker-off verbs, and that
the heartbeat advertises ``llama-worker`` only while the worker runs. No network, no
process is spawned.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from adk import enrollment, mesh_worker, node_commands

KEY = "ab" * 32
NODE = "deck-1"
#: worker_key("00"*32, "node-1"): the cross-implementation vector (Java, Identity, MS).
WORKER_KEY_VECTOR = "80b59082e9e54291084e1dbca1485e1cfe5c2046d550079e59e23fc43098032b"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path))
    monkeypatch.setenv("AITHER_WORKER_DIR", str(tmp_path / "worker"))
    monkeypatch.setattr(mesh_worker, "_command_key_record", lambda: (NODE, KEY))
    yield tmp_path


def _accel(**kw):
    base = dict(kind="cpu", name="Intel", vram_gb=0.0, ram_gb=16.0, os_family="linux",
                arch="x64")
    base.update(kw)
    return SimpleNamespace(**base)


# ── the key ──────────────────────────────────────────────────────────────────


def test_worker_key_is_hmac_of_the_command_key_over_label_and_node():
    want = hmac.new(("00" * 32).encode(), b"aither-worker-api-key/v1|node-1",
                    hashlib.sha256).hexdigest()
    assert mesh_worker.worker_key("00" * 32, "node-1") == want
    assert want == WORKER_KEY_VECTOR


def test_no_command_key_refuses():
    with pytest.raises(mesh_worker.WorkerError, match="no command key"):
        mesh_worker.worker_key("", NODE)


def test_status_and_argv_redaction_never_carry_the_key():
    key = mesh_worker.worker_key(KEY, NODE)
    argv = mesh_worker.server_argv("srv", "m.gguf", "100.64.0.9", 8488, "/k/api.key",
                                   "bonsai-1.7b", 1, False)
    assert key not in " ".join(argv) and "--api-key" not in argv   # never on the cmdline
    assert argv[argv.index("--api-key-file") + 1] == "/k/api.key"
    red = mesh_worker.redact(["srv", "--api-key", key])
    assert key not in " ".join(red)
    assert mesh_worker.key_fingerprint(key) in " ".join(red)


def test_the_key_file_is_created_owner_only(tmp_path):
    p = mesh_worker.write_key_file("abc", tmp_path / "api.key")
    assert p.read_text(encoding="ascii").strip() == "abc"
    if os.name != "nt":
        assert (p.stat().st_mode & 0o777) == 0o600
    with pytest.raises(mesh_worker.WorkerError):
        mesh_worker.write_key_file("", tmp_path / "x.key")


# ── argv ─────────────────────────────────────────────────────────────────────


def test_argv_binds_the_tailnet_with_key_alias_and_slots():
    argv = mesh_worker.server_argv("srv", "m.gguf", "100.64.0.52", 8488, "k", "bonsai-8b", 3,
                                   True)
    i = argv.index
    assert argv[i("--host") + 1] == "100.64.0.52"
    assert argv[i("--api-key-file") + 1] == "k"
    assert argv[i("--alias") + 1] == "bonsai-8b"
    assert argv[i("-np") + 1] == "3"
    assert argv[i("-c") + 1] == str(3 * mesh_worker.CTX_PER_SLOT)
    assert argv[i("-ngl") + 1] == "99"
    assert "--no-webui" in argv


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "10.0.0.2", "100.128.0.1",
                                  "100.63.255.255", "8.8.8.8", "localhost", "", "::",
                                  "fd7a:115c:a1e0::1"])
def test_argv_refuses_a_host_off_the_tailnet(host):
    with pytest.raises(mesh_worker.WorkerError):
        mesh_worker.server_argv("srv", "m", host, 8488, "k", "bonsai-1.7b", 1, False)


@pytest.mark.parametrize("n", [0, 5, -1, 100, "0", "5", "x", True])
def test_parallel_outside_1_to_4_is_refused(n):
    with pytest.raises(mesh_worker.WorkerError):
        mesh_worker.server_argv("srv", "m", "100.64.0.9", 8488, "k", "bonsai-1.7b", n, False)


def test_unknown_model_is_refused():
    with pytest.raises(mesh_worker.WorkerError):
        mesh_worker.server_argv("srv", "m", "100.64.0.9", 8488, "k", "qwen-72b", 1, False)
    with pytest.raises(mesh_worker.WorkerError):
        mesh_worker.choose_model(_accel(), "deepseek-v4")


# ── hardware -> model ────────────────────────────────────────────────────────


def test_steam_deck_runs_bonsai_8b():
    deck = _accel(kind="vulkan", name="AMD Custom GPU 0405")
    assert mesh_worker.is_steam_deck(deck, os_release='ID=steamos\nNAME="SteamOS"',
                                     board="Jupiter", cpu="")
    assert mesh_worker.is_steam_deck(_accel(), os_release="ID=arch", board="",
                                     cpu="model name: AMD Custom APU 0405")
    assert mesh_worker.choose_model(deck, deck=True) == "bonsai-8b"


def test_laptop_runs_bonsai_1_7b():
    lap = _accel(name="Intel Iris Xe")
    assert not mesh_worker.is_steam_deck(lap, os_release="ID=ubuntu", board="20XW",
                                         cpu="model name: Intel Core i7")
    assert not mesh_worker.is_steam_deck(_accel(os_family="windows"))
    assert mesh_worker.choose_model(lap, deck=False) == "bonsai-1.7b"
    assert mesh_worker.choose_model(lap, "bonsai-4b", deck=False) == "bonsai-4b"


def test_the_deck_is_placed_on_the_vulkan_build():
    plan = mesh_worker.plan_for("bonsai-8b", _accel(kind="cpu"), deck=True)
    assert plan.build == "vulkan" and plan.placement == "gpu"


# ── cmd_on: refusals and the happy path, with every side effect stubbed ──────


def _args(**kw):
    ns = argparse.Namespace(worker_action="on", model="", parallel=1,
                            port=mesh_worker.DEFAULT_PORT, json=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class _Proc:
    pid = 4242

    def terminate(self):
        pass


def _collect():
    lines = []
    return lines, (lambda text, err=False: lines.append(text))


def test_on_refuses_without_a_command_key():
    lines, say = _collect()
    rc = mesh_worker.cmd_on(_args(), key_record=("", ""), say=say,
                            overlay={"joined": True, "tailnet_ip": "100.64.0.9", "tun": True})
    assert rc == 2 and "no command key" in lines[-1]


def test_on_refuses_off_the_mesh():
    lines, say = _collect()
    rc = mesh_worker.cmd_on(_args(), key_record=(NODE, KEY), say=say,
                            overlay={"joined": False, "code": "not_running"})
    assert rc == 2


def test_on_starts_on_the_tailnet_and_writes_state(monkeypatch, tmp_path):
    started = {}
    monkeypatch.setattr(mesh_worker, "install_persistence", lambda *a, **k: "none")
    monkeypatch.setattr(mesh_worker, "_allow_firewall", lambda *a, **k: True)
    monkeypatch.setattr(mesh_worker, "running_state", lambda *a, **k: None)

    def popen(argv, **kw):
        started["argv"] = argv
        return _Proc()

    lines, say = _collect()
    rc = mesh_worker.cmd_on(
        _args(parallel=2), key_record=(NODE, KEY), say=say, popen=popen,
        overlay={"joined": True, "tailnet_ip": "100.64.0.9", "tun": True},
        accel=_accel(), install=lambda m, p, a: (tmp_path / "llama-server", tmp_path / "w.gguf"),
        probe=lambda url: True, sleep=lambda s: None)
    assert rc == 0, lines
    argv = started["argv"]
    assert argv[argv.index("--host") + 1] == "100.64.0.9"
    assert argv[argv.index("-np") + 1] == "2"
    key_file = Path(argv[argv.index("--api-key-file") + 1])
    assert key_file.read_text(encoding="ascii").strip() == mesh_worker.worker_key(KEY, NODE)
    assert mesh_worker.worker_key(KEY, NODE) not in " ".join(argv)
    st = json.loads((tmp_path / "worker" / "state.json").read_text())
    assert st["model"] == "bonsai-1.7b" and st["parallel"] == 2 and st["ip"] == "100.64.0.9"
    assert mesh_worker.worker_key(KEY, NODE) not in json.dumps(st)
    assert all(mesh_worker.worker_key(KEY, NODE) not in ln for ln in lines)


# ── the heartbeat advertises the worker only while it runs ───────────────────


def _write_state(**kw):
    st = {"pid": 99, "ip": "100.64.0.9", "bind": "100.64.0.9", "port": 8488,
          "model": "bonsai-1.7b", "parallel": 1,
          "key_fp": mesh_worker.key_fingerprint(mesh_worker.worker_key(KEY, NODE))}
    st.update(kw)
    mesh_worker.write_state(st)


def test_advertised_while_running():
    _write_state()
    ad = mesh_worker.advertised(pid_alive=lambda pid: True, probe=lambda url: True)
    assert ad == {"inference_url": "http://100.64.0.9:8488", "inference_kind": "llama-worker",
                  "available_models": ["bonsai-1.7b"], "inference_ready": True}
    ad = mesh_worker.advertised(pid_alive=lambda pid: True, probe=lambda url: False)
    assert ad["inference_ready"] is False


def test_not_advertised_after_the_command_key_rotated(monkeypatch):
    """A keyver bump changes the derived key: the running server still holds the old one,
    so it is withdrawn from the heartbeat until `adk mesh worker on` re-derives it."""
    _write_state()
    monkeypatch.setattr(mesh_worker, "_command_key_record", lambda: (NODE, "11" * 32))
    assert mesh_worker.advertised(pid_alive=lambda pid: True, probe=lambda u: True) is None
    monkeypatch.setattr(mesh_worker, "_command_key_record", lambda: ("", ""))
    assert mesh_worker.advertised(pid_alive=lambda pid: True, probe=lambda u: True) is None


def test_not_advertised_when_stopped_or_dead():
    assert mesh_worker.advertised(pid_alive=lambda pid: True) is None  # no state file
    _write_state()
    assert mesh_worker.advertised(pid_alive=lambda pid: False, probe=lambda u: True) is None


def test_build_registration_carries_the_worker_only_while_it_runs(monkeypatch):
    monkeypatch.setattr(enrollment, "probe_inference",
                        lambda url=None: enrollment.InferenceProbe([], "", "none", False))
    monkeypatch.setattr(enrollment, "_lending_fields",
                        lambda s, p: {"capabilities": [], "capability_detail": {}})
    monkeypatch.setattr(enrollment, "_worker_advert", lambda: None)
    reg = enrollment.build_registration(NODE, node_class="laptop")
    assert reg["inference_kind"] == "none" and reg["inference_url"] == ""
    monkeypatch.setattr(enrollment, "_worker_advert", lambda: {
        "inference_url": "http://100.64.0.9:8488", "inference_kind": "llama-worker",
        "available_models": ["bonsai-1.7b"], "inference_ready": True})
    reg = enrollment.build_registration(NODE, node_class="laptop")
    assert reg["inference_kind"] == "llama-worker"
    assert reg["inference_url"] == "http://100.64.0.9:8488"
    assert reg["available_models"] == ["bonsai-1.7b"] and reg["inference_ready"] is True


def test_real_worker_advert_reads_the_state_file(monkeypatch):
    _write_state(model="bonsai-8b")
    monkeypatch.setattr(mesh_worker.mesh_lend, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mesh_worker, "advertised",
                        lambda: {"inference_url": "http://100.64.0.9:8488",
                                 "inference_kind": "llama-worker",
                                 "available_models": ["bonsai-8b"], "inference_ready": True})
    assert enrollment._worker_advert()["available_models"] == ["bonsai-8b"]


# ── worker-on / worker-off: signed verbs, closed arguments ───────────────────


def _cmd(**over):
    cmd = {"id": "cmd_w", "tenant_id": "tnt", "node_id": NODE, "verb": "worker-on",
           "args": {}, "issued_by": "u1", "issued_at": int(time.time()),
           "expires_at": int(time.time()) + 600}
    cmd.update(over)
    cmd["sig"] = node_commands.sign(KEY, node_commands.canonical(cmd))
    return cmd


@pytest.mark.parametrize("args", [{}, {"parallel": "1"}, {"parallel": "4"},
                                  {"model": "bonsai-8b"},
                                  {"parallel": "2", "model": "bonsai-1.7b"}])
def test_worker_on_accepts_its_closed_args(args):
    assert node_commands.verify(_cmd(args=args), KEY, NODE, seen=[]) == ""


@pytest.mark.parametrize("args", [{"parallel": "5"}, {"parallel": "0"}, {"parallel": 2},
                                  {"parallel": " 2"}, {"model": "qwen-72b"},
                                  {"model": "bonsai-27b"}, {"port": "9999"},
                                  {"parallel": "2", "host": "0.0.0.0"}])
def test_worker_on_refuses_anything_else(args):
    assert node_commands.verify(_cmd(args=args), KEY, NODE, seen=[]) != ""


def test_worker_off_takes_no_argument():
    assert node_commands.verify(_cmd(verb="worker-off"), KEY, NODE, seen=[]) == ""
    assert node_commands.verify(_cmd(verb="worker-off", args={"x": "1"}), KEY, NODE,
                                seen=[]) != ""


def test_worker_verbs_run_the_cli_code(monkeypatch):
    seen = {}

    def fake_on(ns, say=None, **kw):
        seen["on"] = (ns.worker_action, ns.parallel, ns.model, ns.port)
        say("worker serving")
        return 0

    def fake_off(ns, say=None, **kw):
        seen["off"] = ns.worker_action
        return 0

    monkeypatch.setattr(mesh_worker, "cmd_on", fake_on)
    monkeypatch.setattr(mesh_worker, "cmd_off", fake_off)
    res = node_commands.run_commands([_cmd(args={"parallel": "3", "model": "bonsai-4b"})],
                                     NODE, KEY)
    assert res[0]["ok"] is True
    assert seen["on"] == ("on", 3, "bonsai-4b", mesh_worker.DEFAULT_PORT)
    res = node_commands.run_commands([_cmd(id="cmd_off", verb="worker-off")], NODE, KEY)
    assert res[0]["ok"] is True and seen["off"] == "off"


def test_device_tuples_match_the_worker_module():
    assert node_commands.WORKER_MODELS == mesh_worker.WORKER_MODELS
    assert node_commands.WORKER_PARALLEL == tuple(str(n) for n in
                                                  range(1, mesh_worker.MAX_PARALLEL + 1))


def test_cli_parses_mesh_worker():
    from adk import cli

    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert '"worker"' in src and "cmd_worker" in src
    p = argparse.ArgumentParser()
    mesh_worker.add_arguments(p)
    ns = p.parse_args(["on", "--parallel", "2", "--model", "bonsai-8b"])
    assert (ns.worker_action, ns.parallel, ns.model) == ("on", 2, "bonsai-8b")
    with pytest.raises(SystemExit):
        p.parse_args(["on", "--model", "llama-70b"])
