"""A daemon notices newer installed code and restarts onto it, but only when idle.

Measured 2026-10-02: the awsh harness daemon ran a day from a deleted snapshot;
/agents and /workforce raised ModuleNotFoundError behind a green /health.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from adk import self_update as su


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AITHER_DAEMON_AUTO_UPDATE", raising=False)
    su._STATE.update(checked_at=None, installed_root=None, error="", pending=False, relaunch=None)


def _probe(root):
    return lambda: (Path(root), "")


def _watcher(root, idle=True):
    calls = {"relaunch": 0, "stop": 0}

    def relaunch(name):
        calls["relaunch"] += 1
        return {"helper_pid": 1}

    def stop():
        calls["stop"] += 1

    w = su.UpdateWatcher("t", lambda: idle, probe=_probe(root), relaunch=relaunch, stop=stop,
                         validate=lambda r, n: (True, "validated"),
                         preflight_fn=lambda n: (True, "preflight ok"))
    return w, calls


def test_same_install_is_current():
    w, calls = _watcher(su.RUNNING_ROOT)
    assert w.tick() == "current"
    assert su.status()["update_available"] is False
    assert calls == {"relaunch": 0, "stop": 0}


def test_newer_install_while_idle_relaunches_then_stops(tmp_path):
    w, calls = _watcher(tmp_path / "adk-new" / "awdk")
    assert w.tick() == "restarting"
    st = su.status()
    assert st["update_available"] is True and st["installed_at"].endswith("awdk")
    assert calls == {"relaunch": 1, "stop": 1}


def test_a_busy_daemon_is_never_restarted_only_reported(tmp_path):
    w, calls = _watcher(tmp_path / "adk-new" / "awdk", idle=False)
    assert w.tick() == "busy"
    assert su.status()["restart_pending"] is True
    assert calls == {"relaunch": 0, "stop": 0}


def test_an_idle_check_that_raises_counts_as_busy(tmp_path):
    w, calls = _watcher(tmp_path / "x")
    w.is_idle = lambda: 1 / 0
    assert w.tick() == "busy" and calls["stop"] == 0


def test_opt_out_reports_but_does_not_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DAEMON_AUTO_UPDATE", "0")
    w, calls = _watcher(tmp_path / "x")
    assert w.tick() == "report-only"
    assert su.status()["auto_restart"] is False and calls["stop"] == 0


def test_running_code_deleted_counts_as_needing_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "RUNNING_ROOT", tmp_path / "gone")
    w, calls = _watcher(tmp_path / "gone")  # installed == running, but the files are gone
    assert w.tick() == "restarting"
    assert su.status()["running_code_missing"] is True


def test_a_failed_probe_is_reported_not_raised():
    st = su.check(lambda: (None, "probe failed: boom"))
    assert st["check_error"] == "probe failed: boom" and st["update_available"] is False


def test_installed_root_finds_this_package_in_a_fresh_interpreter():
    root, err = su.installed_root()
    assert err == "" and root is not None and (root / "adk").is_dir()


def test_register_records_the_code_root_and_live_roots_skip_dead_pids(tmp_path):
    path = su.register("unit")
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["pid"] == os.getpid() and rec["code_root"] == str(su.RUNNING_ROOT)
    dead = su.run_dir() / "dead.json"
    done = subprocess.run([sys.executable, "-c", "import os;print(os.getpid())"], capture_output=True, text=True)
    dead.write_text(json.dumps({"pid": int(done.stdout), "code_root": str(tmp_path / "old")}), encoding="utf-8")
    roots = [str(r) for r in su.live_code_roots()]
    assert str(su.RUNNING_ROOT) in roots and str(tmp_path / "old") not in roots


def test_relaunch_argv_keeps_the_interpreter_flags(monkeypatch):
    monkeypatch.setattr(sys, "orig_argv", ["python", "-P", "-m", "adk.harnesses.daemon", "--port", "1"], raising=False)
    assert su.relaunch_argv() == [sys.executable, "-P", "-m", "adk.harnesses.daemon", "--port", "1"]


def test_the_helper_waits_for_the_old_pid_then_starts_the_command(tmp_path):
    marker = tmp_path / "started.txt"
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    spec = {
        "pid": old.pid,
        "argv": [sys.executable, "-c", f"open(r'{marker}', 'w').write('up')"],
        "cwd": str(tmp_path), "log": str(tmp_path / "relaunch.log"), "root": str(su.RUNNING_ROOT),
    }
    helper = subprocess.Popen([sys.executable, "-c", su._HELPER, json.dumps(spec)])
    time.sleep(0.8)
    assert not marker.exists(), "relaunched before the old process exited"
    old.wait(timeout=10)
    helper.wait(timeout=30)
    deadline = time.time() + 15
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert marker.read_text() == "up"


# ── the validation gates (2026-10-03) ─────────────────────────────────────────

def _root(tmp_path, name, verdict=None):
    r = tmp_path / name / "awdk"
    (r / "adk").mkdir(parents=True)
    if verdict is not None:
        (r / su.MARKER).write_text(json.dumps({"validated": verdict}), encoding="utf-8")
    return r


def test_unvalidated_code_is_refused_and_never_restarted_onto(tmp_path):
    root = _root(tmp_path, "adk-dev")
    calls = []
    w = su.UpdateWatcher("t", lambda: True, probe=_probe(root), relaunch=lambda n: calls.append(n),
                         stop=lambda: calls.append("stop"), preflight_fn=lambda n: (True, ""))
    assert w.tick() == "refused"
    st = su.status()
    assert st["adoptable"] is False and "not validated" in st["refused_because"]
    assert calls == []


def test_code_whose_tests_failed_is_refused(tmp_path):
    root = _root(tmp_path, "adk-red", {"ok": False, "summary": "2 failed, 9 passed"})
    ok, why = su.validation_of(root)
    assert not ok and "2 failed" in why


def test_validated_code_is_adopted(tmp_path):
    root = _root(tmp_path, "adk-green", {"ok": True, "commit": "abcdef1234567", "summary": "11 passed"})
    assert su.validation_of(root)[0] is True


def test_opt_in_adopts_unvalidated(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DAEMON_ADOPT_UNVALIDATED", "1")
    assert su.validation_of(_root(tmp_path, "adk-dev"))[0] is True


def test_a_root_that_was_rolled_back_is_refused_even_if_validated(tmp_path):
    root = _root(tmp_path, "adk-bad", {"ok": True})
    su.run_dir().mkdir(parents=True, exist_ok=True)
    (su.run_dir() / "t.rollback.json").write_text(json.dumps({"refused": [str(root)]}), encoding="utf-8")
    ok, why = su.validation_of(root, "t")
    assert not ok and "rolled back" in why


def test_a_failing_preflight_blocks_the_restart(tmp_path):
    calls = []
    w = su.UpdateWatcher("t", lambda: True, probe=_probe(tmp_path / "new"),
                         relaunch=lambda n: calls.append(n), stop=lambda: calls.append("stop"),
                         validate=lambda r, n: (True, ""),
                         preflight_fn=lambda n: (False, "preflight failed: ImportError"))
    assert w.tick() == "refused"
    assert "ImportError" in su.status()["refused_because"] and calls == []


def test_preflight_runs_the_daemon_code_in_a_fresh_interpreter(monkeypatch):
    monkeypatch.setitem(su.PREFLIGHTS, "t-ok", "import adk.self_update")
    monkeypatch.setitem(su.PREFLIGHTS, "t-bad", "raise SystemExit('boom')")
    assert su.preflight("t-ok") == (True, "preflight ok")
    ok, why = su.preflight("t-bad")
    assert not ok and "boom" in why


def test_the_helper_rolls_back_when_the_new_code_is_not_healthy(tmp_path):
    rolled = tmp_path / "rolled-back.txt"
    run = tmp_path / "run"
    spec = {
        "pid": 0, "cwd": str(tmp_path), "log": str(tmp_path / "relaunch.log"),
        "root": str(su.RUNNING_ROOT), "name": "t", "run_dir": str(run), "python": sys.executable,
        "argv": [sys.executable, "-c", "import time; time.sleep(30)"],          # never healthy
        "rollback_argv": [sys.executable, "-c", f"open(r'{rolled}', 'w').write('old code')"],
        "health_url": "http://127.0.0.1:9/health", "health_timeout": 3,
        "previous_root": "", "installed_root": str(tmp_path / "adk-bad" / "awdk"),
        "reinstall": False, "flags": 0,
    }
    helper = subprocess.run([sys.executable, "-c", su._HELPER, json.dumps(spec)], timeout=90)
    assert helper.returncode == 0
    deadline = time.time() + 15
    while not rolled.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert rolled.read_text() == "old code"
    rec = json.loads((run / "t.rollback.json").read_text(encoding="utf-8"))
    assert rec["refused"] == [str(tmp_path / "adk-bad" / "awdk")]


def test_health_reports_whether_the_running_code_was_validated():
    assert "running_validated" in su.status() and "adoptable" in su.status()
