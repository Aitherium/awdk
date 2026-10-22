"""`adk mesh lend`: lend spare RAM to the pooled model as a verified ggml RPC server.

Hermetic: the download, the process start, schtasks/systemctl and the registry are
all fakes. Nothing touches the network, the scheduler or a real process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from types import SimpleNamespace

import pytest

from adk import mesh_lend as ml

GOOD = b"rpc-server bytes"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()


def _variant(key: str = "fake", exe: str = "rpc-fake") -> ml.Variant:
    return ml.Variant(key, (ml.Asset("asset-" + key, len(GOOD), GOOD_SHA, exe),), exe)


def _fetch(payload: bytes = GOOD):
    calls = []

    def fetch(url, dest):
        calls.append(url)
        dest.write_bytes(payload)
    fetch.calls = calls
    return fetch


class _Proc:
    def __init__(self, code=None, pid=4242):
        self._code, self.pid, self.terminated = code, pid, False

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True


def _popen(*codes):
    seen = []
    it = iter(codes)

    def popen(argv, **kw):
        seen.append((argv, kw))
        return _Proc(next(it))
    popen.seen = seen
    return popen


def _run_ok(cmd, **kw):
    # `systemctl --user show -p MainPID --value` answers the pid alone: a Linux runner
    # with systemctl takes the systemd hand-over path in cmd_lend, and int() of the
    # --help text below made it LendError (rc 2). Everything else gets the help text
    # the binary probe looks for.
    if list(cmd[:2]) == ["systemctl", "--user"] and "show" in cmd:
        return SimpleNamespace(returncode=0, stdout="4242\n", stderr="")
    return SimpleNamespace(returncode=0, stdout="4242\n  -d, --device <dev1,dev2,...>", stderr="")


def _args(**kw):
    base = dict(gb=None, port=50052, threads=4, status=False, stop=False, json=False,
                pool="", registry_url="", coordinator="")
    base.update(kw)
    return argparse.Namespace(**base)


ON_MESH = {"joined": True, "code": "ok", "tailnet_ip": "100.64.0.9", "tun": True}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_LEND_DIR", str(tmp_path / "lend"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for k in ("ADK_LEND_POOL", "ADK_POOL_REGISTRY_URL", "ADK_POOL_COORDINATOR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ml, "_allow_firewall", lambda *a, **k: True)


# ── binary selection ────────────────────────────────────────────────────────


@pytest.mark.parametrize("system,machine,first", [
    ("Windows", "AMD64", "windows-x64-v3"),
    ("Linux", "x86_64", "linux-x86_64-znver2"),
    ("Linux", "aarch64", "linux-arm64-v8.2"),
    ("linux", "arm64", "linux-arm64-v8.2"),
])
def test_select_variant_per_platform(system, machine, first):
    assert ml.select_variants(system, machine)[0].key == first


def test_linux_x86_has_v2_fallback_after_znver2():
    keys = [v.key for v in ml.select_variants("Linux", "x86_64")]
    assert keys == ["linux-x86_64-znver2", "linux-x86_64-v2"]


def test_windows_ships_runtime_dlls_next_to_exe():
    v = ml.select_variants("Windows", "AMD64")[0]
    names = {a.save_as for a in v.files}
    assert {"ggml-rpc-server-v3.exe", "libc++.dll", "libunwind.dll",
            "libwinpthread-1.dll"} <= names


def test_unsupported_platform_refuses():
    with pytest.raises(ml.LendError):
        ml.select_variants("Darwin", "arm64")


def test_windows_has_v2_fallback_after_v3():
    keys = [v.key for v in ml.select_variants("Windows", "AMD64")]
    assert keys == ["windows-x64-v3", "windows-x64-v2"]


def test_pins_table_covers_every_asset_and_nothing_else():
    used = set()
    for variants in ml.VARIANTS.values():
        for v in variants:
            assert v.exe in {a.save_as for a in v.files}
            for a in v.files:
                assert ml.PINS[a.name] == (a.size, a.sha256)
                used.add(a.name)
    assert used == set(ml.PINS)
    for size, sha in ml.PINS.values():
        assert size > 0 and len(sha) == 64 and int(sha, 16) >= 0


# ── download verification ───────────────────────────────────────────────────


def test_sha_mismatch_refuses_and_leaves_nothing(tmp_path):
    with pytest.raises(ml.LendError, match="sha256 mismatch"):
        ml.fetch_variant(_variant(), tmp_path, fetch=_fetch(b"tampered bytes!!"))
    assert not list(tmp_path.iterdir())


def test_verified_download_installs_and_is_reused(tmp_path):
    f = _fetch()
    exe = ml.fetch_variant(_variant(), tmp_path, fetch=f)
    assert exe.read_bytes() == GOOD
    ml.fetch_variant(_variant(), tmp_path, fetch=f)
    assert len(f.calls) == 1  # already verified on disk: no second download
    assert f.calls[0].startswith(ml.RELEASE_BASE)


def test_tampered_file_on_disk_is_replaced(tmp_path):
    (tmp_path / "rpc-fake").write_bytes(b"x" * len(GOOD))
    f = _fetch()
    ml.fetch_variant(_variant(), tmp_path, fetch=f)
    assert len(f.calls) == 1 and (tmp_path / "rpc-fake").read_bytes() == GOOD


# ── lend size ───────────────────────────────────────────────────────────────


def test_default_is_half_of_free():
    assert ml.compute_lend_mb(32768) == 16384


def test_default_always_leaves_4gb():
    assert ml.compute_lend_mb(7000) == 7000 - ml.RESERVE_MB


def test_explicit_gb_honoured():
    assert ml.compute_lend_mb(65536, gb=24) == 24 * 1024


def test_explicit_gb_over_reserve_refused_not_cut():
    with pytest.raises(ml.LendError):
        ml.compute_lend_mb(16384, gb=14)


@pytest.mark.parametrize("free", [0, 4096, 5000])
def test_too_little_ram_refuses(free):
    with pytest.raises(ml.LendError):
        ml.compute_lend_mb(free)


# ── bind ────────────────────────────────────────────────────────────────────


def test_binds_tailnet_ip_or_loopback_in_userspace():
    assert ml.bind_host(ON_MESH) == "100.64.0.9"
    assert ml.bind_host(dict(ON_MESH, tun=False)) == "127.0.0.1"


# ── spawn ───────────────────────────────────────────────────────────────────


def test_windows_child_is_windowless_and_detached(tmp_path):
    p = _popen(None)
    ml.spawn_detached([str(tmp_path / "x.exe")], tmp_path / "log", popen=p, windows=True)
    kw = p.seen[0][1]
    assert kw["creationflags"] & ml.CREATE_NO_WINDOW
    assert kw["creationflags"] & ml.DETACHED_PROCESS
    assert kw["stdin"] is not None and kw["close_fds"] is True
    assert kw["stdout"] is not None  # a log file, never the caller's pipe


def test_sigill_falls_back_to_next_build(tmp_path):
    v1, v2 = _variant("fast", "rpc-fast"), _variant("base", "rpc-base")
    p = _popen(-4, None)
    v, _proc, argv = ml.start_server((v1, v2), tmp_path, "100.64.0.9", 50052, 4,
                                     fetch=_fetch(), popen=p, sleep=lambda s: None,
                                     probe=lambda e: (True, ""))
    assert v.key == "base" and argv[0].endswith("rpc-base")
    assert argv[1:] == ["-H", "100.64.0.9", "-p", "50052", "-d", "CPU", "-t", "4"]


def test_other_early_exit_is_an_error(tmp_path):
    with pytest.raises(ml.LendError, match="exited with 1"):
        ml.start_server((_variant(),), tmp_path, "100.64.0.9", 50052, 4,
                        fetch=_fetch(), popen=_popen(1), sleep=lambda s: None,
                        probe=lambda e: (True, ""))


# ── the command ─────────────────────────────────────────────────────────────


def _lend(monkeypatch, overlay=ON_MESH, **kw):
    monkeypatch.setattr(ml, "select_variants", lambda *a: (_variant(),))
    return ml.cmd_lend(_args(**kw), overlay=overlay, fetch=_fetch(), popen=_popen(None),
                       run=_run_ok, free_mb=32768, sleep=lambda s: None)


def test_lend_writes_state_file(monkeypatch):
    assert _lend(monkeypatch) == 0
    st = json.loads(ml.state_path().read_text(encoding="utf-8"))
    assert st["pid"] == 4242 and st["port"] == 50052 and st["ip"] == "100.64.0.9"
    assert st["lend_mb"] == 16384 and st["binary_sha"] == GOOD_SHA
    assert st["persist"] in ("schtasks", "systemd", "run-key", "none")


def test_lend_refuses_off_mesh(monkeypatch):
    rc = _lend(monkeypatch, overlay={"joined": False, "code": "not_running"})
    assert rc == 2 and not ml.state_path().exists()


def test_stop_removes_state(monkeypatch):
    _lend(monkeypatch)
    monkeypatch.setattr(ml, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(ml, "remove_persistence", lambda run: ["schtasks"])
    assert ml.cmd_stop(_args(stop=True), run=_run_ok) == 0
    assert not ml.state_path().exists()


def test_announce_sends_tailnet_ip_port_and_lend(monkeypatch):
    sent = {}

    def send(url, body, headers):
        sent.update(url=url, body=body)
        return 200, json.dumps({"registered": ["100.64.0.9:50052"], "refused": []})

    monkeypatch.setattr("adk.pool_role.measure_rtt_ms", lambda h, p: 3.0)
    st = {"ip": "100.64.0.9", "port": 50052, "lend_mb": 16384, "exe": "rpc", "threads": 4}
    code, _ = ml.announce(st, pool="v41", registry_url="https://reg.example",
                          coordinator="100.64.0.1:8001", send=send, bearer="t")
    assert code == 0 and sent["url"] == "https://reg.example/pools/v41/backends"
    b = sent["body"]["plan"]["backends"][0]
    assert b["role"] == "rpc-cpu" and b["budget_gib"] == 16.0
    assert b["command"][b["command"].index("-H") + 1] == "100.64.0.9"
    assert sent["body"]["plan"]["admission"] == "SPLIT_ELIGIBLE"


def test_announce_without_registry_is_reported_not_failed():
    code, msg = ml.announce({"ip": "1", "port": 1, "lend_mb": 1}, pool="",
                            registry_url="", coordinator="")
    assert code == 0 and "not announced" in msg


# ── remote trigger ──────────────────────────────────────────────────────────


def test_lend_is_in_the_admin_cli_manifest():
    from adk.cli import build_command_manifest

    mesh = next(c for c in build_command_manifest() if c["name"] == "mesh")
    lend = next(s for s in mesh["subcommands"] if s["name"] == "lend")
    flags = {f for a in lend["args"] for f in a["flags"]}
    assert {"--gb", "--port", "--stop", "--status"} <= flags


def test_failed_probe_falls_back_v3_to_v2(tmp_path):
    v3, v2 = _variant("windows-x64-v3", "rpc-v3.exe"), _variant("windows-x64-v2", "rpc-v2.exe")
    p = _popen(None)

    def probe(exe):
        return (False, "illegal instruction") if exe.name == "rpc-v3.exe" else (True, "")
    v, _proc, argv = ml.start_server((v3, v2), tmp_path, "100.64.0.9", 50052, 4,
                                     fetch=_fetch(), popen=p, sleep=lambda s: None, probe=probe)
    assert v.key == "windows-x64-v2" and len(p.seen) == 1  # v3 never started


def test_windows_illegal_instruction_exit_falls_back(tmp_path):
    v3, v2 = _variant("windows-x64-v3", "rpc-v3.exe"), _variant("windows-x64-v2", "rpc-v2.exe")
    v, _proc, _argv = ml.start_server((v3, v2), tmp_path, "100.64.0.9", 50052, 4,
                                      fetch=_fetch(), popen=_popen(0xC000001D, None),
                                      sleep=lambda s: None, probe=lambda e: (True, ""))
    assert v.key == "windows-x64-v2"


@pytest.mark.parametrize("rc,out,ok", [
    (0, "  -d, --device <dev1,dev2,...>  comma-separated list of devices", True),
    (0xC000001D, "", False),
    (1, "error while loading", False),
])
def test_probe_binary_judges_usage_and_sigill(tmp_path, rc, out, ok):
    def run(cmd, **kw):
        assert cmd[1:] == ["-h"]
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")
    assert ml.probe_binary(tmp_path / "x.exe", run=run)[0] is ok


def test_no_build_runs_refuses(tmp_path):
    with pytest.raises(ml.LendError, match="no build runs on this CPU"):
        ml.start_server((_variant("a", "a"), _variant("b", "b")), tmp_path, "100.64.0.9",
                        50052, 4, fetch=_fetch(), popen=_popen(), sleep=lambda s: None,
                        probe=lambda e: (False, ml._ISA_REASON))


def test_a_launch_failure_is_not_called_a_cpu_mismatch(tmp_path):
    """Owner's laptop 2026-10-22: "no build runs on this CPU (windows-x64-v2: -h probe
    could not run (OSError))" for exes that run on the desktop."""
    with pytest.raises(ml.LendError) as ei:
        ml.start_server((_variant("a", "a"), _variant("b", "b")), tmp_path, "100.64.0.9",
                        50052, 4, fetch=_fetch(), popen=_popen(), sleep=lambda s: None,
                        probe=lambda e: (False, "could not launch x: OSError [WinError 225]"))
    assert "CPU" not in str(ei.value) and "WinError 225" in str(ei.value)
    assert "no build could start here" in str(ei.value)


def _oserror(winerror, text):
    exc = OSError(22, text)
    exc.winerror = winerror
    return exc


@pytest.mark.parametrize("winerror,needle", [
    (225, "Defender blocked it"),
    (4551, "Smart App Control"),
    (2, "file not found"),
    (126, "DLL it needs is missing"),
    (5, "access denied"),
])
def test_probe_launch_errors_say_exactly_why(tmp_path, winerror, needle):
    exe = tmp_path / "ggml-rpc-server-v2.exe"

    def run(cmd, **kw):
        raise _oserror(winerror, "The operation did not complete")
    ok, why = ml.probe_binary(exe, run=run)
    assert ok is False
    assert f"WinError {winerror}" in why and needle in why and str(exe) in why
    assert "CPU" not in why and why != ml._ISA_REASON
    if winerror in (225, 4551):
        assert "owner" in why  # actionable, names who can allow it


def test_probe_runs_in_the_exe_folder_and_reads_dll_not_found(tmp_path):
    exe = tmp_path / "lend" / "ggml-rpc-server-v3.exe"
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        return SimpleNamespace(returncode=0xC0000135, stdout="", stderr="")
    ok, why = ml.probe_binary(exe, run=run)
    assert seen["cwd"] == str(exe.parent)
    assert ok is False and "STATUS_DLL_NOT_FOUND" in why and "C0000135" in why


def test_a_dll_quarantined_after_download_is_reported_not_probed(tmp_path, monkeypatch):
    v = ml.Variant("windows-x64-v2", (ml.Asset("asset-a", len(GOOD), GOOD_SHA, "rpc.exe"),
                                      ml.Asset("asset-b", len(GOOD), GOOD_SHA, "libc++.dll")),
                   "rpc.exe")

    def fetch_then_quarantine(url, dest):
        dest.write_bytes(GOOD)

    real_fetch_variant = ml.fetch_variant

    def fv(variant, dest_dir, fetch):
        exe = real_fetch_variant(variant, dest_dir, fetch=fetch)
        (dest_dir / "libc++.dll").unlink()  # Defender took it
        return exe
    monkeypatch.setattr(ml, "fetch_variant", fv)
    probed = []
    with pytest.raises(ml.LendError, match=r"libc\+\+\.dll missing"):
        ml.start_server((v,), tmp_path, "100.64.0.9", 50052, 4, fetch=fetch_then_quarantine,
                        popen=_popen(), sleep=lambda s: None,
                        probe=lambda e: probed.append(e) or (True, ""))
    assert probed == []


def test_timeout_is_described():
    import subprocess
    why = ml.describe_launch_error(subprocess.TimeoutExpired(["x"], 20), "x.exe")
    assert "timed out after 20 s" in why
