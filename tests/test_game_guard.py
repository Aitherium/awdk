"""adk.game_guard: while the owner plays, the worker stops and the lend only starves.

Pins: what counts as a game (Steam game, Game Mode, Big Picture, a busy GPU twice; on
Windows a fullscreen game), that a yield STOPS the worker but never the lend server (a
vanishing lend tier hangs the V4.1 coordinator), that the lend drops to idle priority
by a reversible means, and that both come back only GRACE_S seconds after the game.
No process is touched: every side effect goes through a recording seam.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adk import game_guard as gg
from adk import mesh_lend, mesh_worker


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(gg, "state_path", lambda: tmp_path / "game_guard.json")
    monkeypatch.setenv("AITHER_LEND_DIR", str(tmp_path / "lend"))
    monkeypatch.setenv("AITHER_WORKER_DIR", str(tmp_path / "worker"))
    yield tmp_path


def _proc(tmp_path: Path, procs: dict) -> str:
    root = tmp_path / "proc"
    for pid, argv in procs.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")
    root.mkdir(exist_ok=True)
    return str(root)


def _sys(tmp_path: Path, busy: int) -> str:
    d = tmp_path / "sys" / "class" / "drm" / "card0" / "device"
    d.mkdir(parents=True)
    (d / "gpu_busy_percent").write_text(f"{busy}\n")
    return str(tmp_path / "sys")


def _no_x(*a, **k):
    return SimpleNamespace(returncode=1, stdout="", stderr="")


# ── detection ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("argv,reason", [
    (["/home/deck/.steam/root/ubuntu12_32/reaper", "SteamLaunch", "AppId=1245620", "--",
      "/x/eldenring.exe"], "steam game AppId=1245620"),
    (["/usr/bin/gamescope", "-w", "1280"], "gamescope session (Game Mode)"),
    (["/home/deck/.steam/root/ubuntu12_32/steam", "-gamepadui", "-steamos3"],
     "steam big picture"),
])
def test_linux_names_the_game(tmp_path, argv, reason):
    v = gg.detect("linux", proc=_proc(tmp_path, {100: argv}), sys_root=_sys(tmp_path, 5),
                  run=_no_x)
    assert v["gaming"] is True and v["reason"] == reason


def test_linux_desktop_steam_and_appid_0_are_not_games(tmp_path):
    proc = _proc(tmp_path, {1: ["/home/deck/.steam/root/ubuntu12_32/steam", "-silent"],
                            2: ["reaper", "SteamLaunch", "AppId=0"],
                            3: ["/usr/bin/konsole"]})
    v = gg.detect("linux", proc=proc, sys_root=_sys(tmp_path, 5), run=_no_x)
    assert v["gaming"] is False and v["reason"] == ""


def test_a_busy_gpu_counts_only_on_the_second_hot_tick(tmp_path):
    proc, sys_root = _proc(tmp_path, {}), _sys(tmp_path, 97)
    first = gg.detect("linux", proc=proc, sys_root=sys_root, run=_no_x, prev_gpu_hot=False)
    assert first["gaming"] is False and first["gpu_hot"] is True
    second = gg.detect("linux", proc=proc, sys_root=sys_root, run=_no_x, prev_gpu_hot=True)
    assert second["gaming"] is True and second["reason"] == "GPU busy 97%"


def test_big_picture_window_in_desktop_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")

    def run(argv, **k):
        if "-root" in argv:
            return SimpleNamespace(
                returncode=0, stdout="_NET_ACTIVE_WINDOW(WINDOW): window id # 0x4a00003\n")
        return SimpleNamespace(
            returncode=0, stdout='_NET_WM_NAME(UTF8_STRING) = "Steam Big Picture Mode"\n')

    v = gg.detect("linux", proc=_proc(tmp_path, {}), sys_root=_sys(tmp_path, 0), run=run)
    assert v["gaming"] is True and v["reason"] == "steam big picture (window)"


@pytest.mark.parametrize("fg,busy,gaming", [
    ({"fullscreen": True, "exe": r"D:\SteamLibrary\steamapps\common\Elden Ring\eldenring.exe",
      "class": "ELDEN RING"}, 0, True),
    ({"fullscreen": True, "exe": r"C:\Program Files\Google\Chrome\chrome.exe",
      "class": "Chrome_WidgetWin_1"}, 5, False),
    ({"fullscreen": True, "exe": r"C:\Games\Indie\game.exe", "class": "UnityWndClass"},
     88, True),
    ({"fullscreen": True, "exe": r"C:\Windows\explorer.exe", "class": "Progman"}, 99, False),
    ({"fullscreen": False, "exe": r"D:\SteamLibrary\steamapps\common\X\x.exe",
      "class": "X"}, 99, False),
])
def test_windows_fullscreen_game(fg, busy, gaming):
    v = gg.detect("windows", foreground=lambda: fg,
                  run=lambda argv, **k: SimpleNamespace(returncode=0, stdout=f"{busy}\n"))
    assert v["gaming"] is gaming, v


# ── the yield ────────────────────────────────────────────────────────────────


class _Actions:
    def __init__(self):
        self.calls = []

    def pause_worker(self):
        self.calls.append("pause_worker")
        return {"model": "bonsai-8b", "parallel": 2, "port": 8488, "persist": "systemd"}

    def resume_worker(self, saved):
        self.calls.append(("resume_worker", saved["model"], saved["parallel"]))
        return {"resumed": "systemd"}

    def lower_lend(self):
        self.calls.append("lower_lend")
        return {"pid": 4242, "how": ["cgroup"], "unit": "awdk-mesh-lend.service"}

    def restore_lend(self, saved):
        self.calls.append(("restore_lend", saved["pid"]))
        return {"restored": "cgroup"}


def _guard(tmp_path, verdicts, acts):
    clock = {"t": 1000.0}
    it = iter(verdicts)
    saved = {}
    g = gg.GameGuard(detector=lambda **k: next(it), actions=acts, grace_s=60,
                     clock=lambda: clock["t"], save=lambda st: saved.update(st),
                     load=lambda: {})
    return g, clock, saved


def test_a_game_pauses_the_worker_and_starves_the_lend_once(tmp_path):
    acts = _Actions()
    game = {"gaming": True, "reason": "gamescope session (Game Mode)"}
    g, clock, saved = _guard(tmp_path, [game, game, game], acts)
    s1 = g.tick()
    clock["t"] += 10
    g.tick()
    clock["t"] += 10
    g.tick()
    assert acts.calls == ["pause_worker", "lower_lend"]
    assert s1["gaming"] and s1["yielding"] and s1["worker_paused"]
    assert s1["lend_priority"] == "idle (cgroup)"
    assert saved["yielding"] is True


def test_restore_waits_grace_after_the_game_ends(tmp_path):
    acts = _Actions()
    game, idle = {"gaming": True, "reason": "x"}, {"gaming": False, "reason": ""}
    g, clock, _ = _guard(tmp_path, [game, idle, idle, idle], acts)
    g.tick()
    clock["t"] += 10
    s = g.tick()  # 10 s after the game: still yielding
    assert s["yielding"] and not s["gaming"]
    clock["t"] += 49
    assert g.tick()["yielding"]  # 59 s
    clock["t"] += 2
    s = g.tick()  # 61 s
    assert not s["yielding"] and s["lend_priority"] == "normal"
    assert acts.calls[2:] == [("restore_lend", 4242), ("resume_worker", "bonsai-8b", 2)]


def test_a_restarted_beat_resumes_the_yield_from_disk(tmp_path):
    acts = _Actions()
    gg._save_state({"gaming": True, "yielding": True, "last_game_at": 1000.0,
                    "worker_paused": {"model": "bonsai-1.7b", "parallel": 1, "port": 8488},
                    "lend_lowered": {"pid": 7, "how": ["cgroup"]}})
    g = gg.GameGuard(detector=lambda **k: {"gaming": False}, actions=acts, grace_s=60,
                     clock=lambda: 1100.0)
    assert g.tick()["yielding"] is False
    assert ("restore_lend", 7) in acts.calls
    assert json.loads(gg.state_path().read_text())["yielding"] is False


# ── the real actions, against recording seams ────────────────────────────────


def _lend_state(pid):
    mesh_lend.write_state({"pid": pid, "port": 50052, "ip": "100.64.0.42",
                           "bind": "127.0.0.1", "lend_mb": 14336, "exe": LEND_EXE})


LEND_EXE = "/home/deck/.aither/lend/rpc-server"


def _img(pid):
    return LEND_EXE


def test_lower_lend_never_stops_it_and_uses_cgroup_weights(tmp_path, monkeypatch):
    _lend_state(4242)
    monkeypatch.setattr(mesh_lend, "_pid_alive", lambda pid: True)
    proc = _proc(tmp_path, {})
    (Path(proc) / "4242").mkdir()
    (Path(proc) / "4242" / "cgroup").write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.kde.konsole-1.scope\n")
    calls = []

    def run(argv, **k):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gg, "_may_renice_back", lambda prev: False)
    monkeypatch.setattr(gg.os, "kill", lambda *a: pytest.fail("the lend must never be signalled"),
                        raising=False)
    out = gg.Actions(run=run, proc=proc, host="linux", image=_img).lower_lend()
    # moved into a scope of its own (not the whole Konsole), then starved there
    assert calls[0][:3] == ["busctl", "--user", "call"] and "awdk-mesh-lend-4242.scope" in calls[0]
    assert calls[1] == ["systemctl", "--user", "set-property", "--runtime",
                        "awdk-mesh-lend-4242.scope", "CPUWeight=1", "IOWeight=1"]
    assert out["how"] == ["cgroup"] and "RLIMIT_NICE" in out["nice"]
    assert not any("stop" in a or "kill" in " ".join(a) for a in calls)
    back = gg.Actions(run=run, proc=proc, host="linux", image=_img).restore_lend(out)
    assert calls[-1][-2:] == ["CPUWeight=100", "IOWeight=100"] and back["restored"] == "cgroup"


def test_lower_lend_renices_every_thread_when_it_can_undo_it(tmp_path, monkeypatch):
    _lend_state(4242)
    monkeypatch.setattr(mesh_lend, "_pid_alive", lambda pid: True)
    proc = _proc(tmp_path, {})
    for tid in (4242, 4243, 4244):
        (Path(proc) / "4242" / "task" / str(tid)).mkdir(parents=True)
    (Path(proc) / "4242" / "cgroup").write_text("0::/user.slice/user@1000.service/app.slice/"
                                                "awdk-mesh-lend.service\n")
    niced, sched = [], []
    monkeypatch.setattr(gg, "_may_renice_back", lambda prev: True)
    monkeypatch.setattr(gg.os, "getpriority", lambda which, who: 0, raising=False)
    monkeypatch.setattr(gg.os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(gg.os, "setpriority", lambda w, tid, n: niced.append((tid, n)),
                        raising=False)
    monkeypatch.setattr(gg.os, "SCHED_IDLE", 5, raising=False)
    monkeypatch.setattr(gg.os, "SCHED_OTHER", 0, raising=False)
    monkeypatch.setattr(gg.os, "sched_param", lambda p: p, raising=False)
    monkeypatch.setattr(gg.os, "sched_setscheduler", lambda tid, pol, p: sched.append((tid, pol)),
                        raising=False)
    run = lambda argv, **k: SimpleNamespace(returncode=0, stdout="", stderr="")  # noqa: E731
    out = gg.Actions(run=run, proc=proc, host="linux", image=_img).lower_lend()
    assert out["unit"] == "awdk-mesh-lend.service"  # its own unit: no transient scope
    assert out["how"] == ["cgroup", "nice19+sched_idle"]
    assert niced == [(4242, 19), (4243, 19), (4244, 19)]
    assert sched == [(4242, 5), (4243, 5), (4244, 5)]
    gg.Actions(run=run, proc=proc, host="linux", image=_img).restore_lend(out)
    assert niced[-3:] == [(4242, 0), (4243, 0), (4244, 0)] and sched[-1] == (4244, 0)


def test_windows_lend_goes_to_the_idle_priority_class(monkeypatch):
    _lend_state(4242)
    monkeypatch.setattr(mesh_lend, "_pid_alive", lambda pid: True)
    set_to = []
    monkeypatch.setattr(gg, "_win_priority", lambda pid: gg.NORMAL_PRIORITY_CLASS)
    monkeypatch.setattr(gg, "_win_set_priority", lambda pid, c: set_to.append(c) or True)
    img = lambda pid: r"C:\Users\a\.aither\lend\rpc-server.exe"  # noqa: E731
    out = gg.Actions(host="windows", image=img).lower_lend()
    assert out["how"] == "windows-idle-class" and set_to == [gg.IDLE_PRIORITY_CLASS]
    gg.Actions(host="windows", image=img).restore_lend(out)
    assert set_to[-1] == gg.NORMAL_PRIORITY_CLASS


def test_pause_worker_stops_its_unit_through_systemd(monkeypatch):
    monkeypatch.setattr(mesh_worker, "running_state", lambda *a, **k: {
        "pid": 77, "model": "bonsai-8b", "parallel": 2, "port": 8488, "persist": "systemd"})
    calls = []
    run = lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0, stdout="")  # noqa: E731
    saved = gg.Actions(run=run, host="linux", image=lambda pid: "/x/llama-server").pause_worker()
    assert calls == [["systemctl", "--user", "stop", mesh_worker.UNIT_NAME]]
    assert saved["model"] == "bonsai-8b" and saved["parallel"] == 2


def test_status_reports_gaming_and_yielding():
    s = gg.status({"gaming": True, "reason": "steam game AppId=1", "yielding": True,
                   "since": 5, "worker_paused": {"model": "bonsai-8b"},
                   "lend_lowered": {"pid": 1, "how": "windows-idle-class"}})
    assert s["gaming"] and s["yielding"] and s["worker_paused"]
    assert s["lend_priority"] == "idle (windows-idle-class)"


def test_the_guard_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AITHER_GAME_GUARD", "off")
    assert gg.enabled() is False


def test_a_reused_pid_is_never_stopped_or_starved(monkeypatch, tmp_path):
    """A state file's pid can now belong to another program (the lend died, the pid
    was reused): the guard leaves THAT process alone."""
    _lend_state(4242)
    monkeypatch.setattr(mesh_lend, "_pid_alive", lambda pid: True)
    calls = []
    run = lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0, stdout="")  # noqa: E731
    acts = gg.Actions(run=run, proc=str(tmp_path), host="linux",
                      image=lambda pid: "/usr/bin/firefox")
    assert acts.lower_lend() is None and calls == []
    monkeypatch.setattr(mesh_worker, "running_state", lambda *a, **k: {
        "pid": 4242, "model": "bonsai-8b", "persist": "none",
        "argv": ["/x/llama-server", "-m", "w.gguf"]})
    monkeypatch.setattr(gg.os, "kill", lambda *a: pytest.fail("killed a stranger"),
                        raising=False)
    saved = acts.pause_worker()
    assert "no longer the worker" in saved["error"] and calls == []
    assert gg.pid_is(4242, r"C:\x\rpc-server.exe", image=lambda p: r"C:\X\RPC-SERVER.EXE")
    assert not gg.pid_is(4242, "", image=lambda p: "/x/rpc-server")
