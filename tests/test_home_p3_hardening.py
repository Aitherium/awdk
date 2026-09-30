"""Hearth P3 hardening: serve autostart, receipt head anchoring.

(a) ``adk home serve --install/--uninstall`` registers the serve at logon through
    ``agent_daemon.install_user_autostart`` -- a pythonw no-window launcher on Windows
    (never run-hidden.vbs), a systemd --user unit on Linux.
(b) ``adk home receipts --anchor`` signs the chain head; ``verify`` then reports a
    truncated tail as tampered (1).
"""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from adk import agent_daemon, receipts

# --------------------------------------------------------------------------- #
# (a) named user autostart
# --------------------------------------------------------------------------- #


@pytest.fixture
def daemon_home(tmp_path, monkeypatch):
    home = tmp_path / ".aither"
    monkeypatch.setattr(agent_daemon, "AITHER_HOME", home)
    monkeypatch.setattr(agent_daemon, "LOG_DIR", home / "logs")
    monkeypatch.setattr(agent_daemon, "systemd_user_dir", lambda: tmp_path / "systemd")
    monkeypatch.setattr(agent_daemon, "launchd_agents_dir", lambda: tmp_path / "agents")
    return tmp_path


class _Runs:
    """Records every subprocess.run call; answers with a fixed return code."""

    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=self.rc, stdout="", stderr="denied")


def test_systemd_install_writes_unit_and_enables(daemon_home, monkeypatch):
    runs = _Runs()
    monkeypatch.setattr(agent_daemon.subprocess, "run", runs)
    argv = ["/usr/bin/python3", "-m", "adk.home", "serve", "--channels", "local"]
    where = agent_daemon.install_user_autostart(
        "aither-hearth", argv, description="Hearth", env={"AITHER_AGENT_HOME": "/h"},
        platform="linux")
    assert where == "systemd:aither-hearth"
    unit = (daemon_home / "systemd" / "aither-hearth.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/python3 -m adk.home serve --channels local" in unit
    assert 'Environment="AITHER_AGENT_HOME=/h"' in unit
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    assert ["systemctl", "--user", "enable", "--now", "aither-hearth.service"] in runs.calls


def test_systemd_enable_failure_is_reported(daemon_home, monkeypatch):
    monkeypatch.setattr(agent_daemon.subprocess, "run", _Runs(rc=1))
    assert agent_daemon.install_user_autostart("aither-hearth", ["x"], platform="linux") is None


def test_systemd_uninstall_removes_unit(daemon_home, monkeypatch):
    runs = _Runs()
    monkeypatch.setattr(agent_daemon.subprocess, "run", runs)
    agent_daemon.install_user_autostart("aither-hearth", ["x"], platform="linux")
    assert agent_daemon.remove_user_autostart("aither-hearth", platform="linux") is True
    assert not (daemon_home / "systemd" / "aither-hearth.service").exists()
    assert ["systemctl", "--user", "disable", "--now", "aither-hearth.service"] in runs.calls
    # idempotent: nothing installed is success
    assert agent_daemon.remove_user_autostart("aither-hearth", platform="linux") is True


def test_windows_install_uses_pythonw_launcher_not_vbs(daemon_home, monkeypatch):
    pyw = daemon_home / "pythonw.exe"
    pyw.write_bytes(b"")
    monkeypatch.setattr(agent_daemon, "pythonw_executable", lambda python=None: pyw)
    runs = _Runs()
    monkeypatch.setattr(agent_daemon.subprocess, "run", runs)
    argv = ["C:/py/python.exe", "-m", "adk.home", "serve"]
    where = agent_daemon.install_user_autostart("aither-hearth", argv, platform="win32")
    assert where == "windows-task:aither-hearth"
    create = next(c for c in runs.calls if c[:2] == ["schtasks", "/create"])
    tr = create[create.index("/tr") + 1]
    assert str(pyw) in tr and tr.endswith('-launch.pyw"')
    assert "wscript" not in tr.lower() and ".vbs" not in tr.lower()
    assert create[create.index("/sc") + 1] == "onlogon"
    launcher = agent_daemon.launcher_path("aither-hearth").read_text(encoding="utf-8")
    assert "CREATE_NO_WINDOW" in launcher
    assert repr(argv) in launcher


def test_windows_without_pythonw_refuses_rather_than_open_a_console(daemon_home,
                                                                    monkeypatch):
    monkeypatch.setattr(agent_daemon, "pythonw_executable", lambda python=None: None)
    runs = _Runs()
    monkeypatch.setattr(agent_daemon.subprocess, "run", runs)
    assert agent_daemon.install_user_autostart("aither-hearth", ["x"],
                                               platform="win32") is None
    assert runs.calls == []


def test_windows_schtasks_denied_falls_back_to_hkcu_run(daemon_home, monkeypatch):
    pyw = daemon_home / "pythonw.exe"
    pyw.write_bytes(b"")
    monkeypatch.setattr(agent_daemon, "pythonw_executable", lambda python=None: pyw)
    monkeypatch.setattr(agent_daemon.subprocess, "run", _Runs(rc=1))
    seen = {}
    monkeypatch.setattr(agent_daemon, "_set_hkcu_run_value",
                        lambda name, value: seen.update({name: value}) or True)
    where = agent_daemon.install_user_autostart("aither-hearth", ["x"], platform="win32")
    assert where == "hkcu-run:aither-hearth"
    assert "pythonw.exe" in seen["aither-hearth"] and ".vbs" not in seen["aither-hearth"]


def test_windows_uninstall_deletes_task_run_value_and_launcher(daemon_home, monkeypatch):
    pyw = daemon_home / "pythonw.exe"
    pyw.write_bytes(b"")
    monkeypatch.setattr(agent_daemon, "pythonw_executable", lambda python=None: pyw)
    runs = _Runs()
    monkeypatch.setattr(agent_daemon.subprocess, "run", runs)
    monkeypatch.setattr(agent_daemon, "_del_hkcu_run_value", lambda name: True)
    agent_daemon.install_user_autostart("aither-hearth", ["x"], platform="win32")
    assert agent_daemon.launcher_path("aither-hearth").exists()
    assert agent_daemon.remove_user_autostart("aither-hearth", platform="win32") is True
    assert ["schtasks", "/delete", "/tn", "aither-hearth", "/f"] in runs.calls
    assert not agent_daemon.launcher_path("aither-hearth").exists()


def test_launcher_runs_the_command_logs_it_and_keeps_its_exit_code(daemon_home):
    log = daemon_home / "logs" / "t.log"
    argv = [sys.executable, "-c", "print('hearth-up'); raise SystemExit(3)"]
    launcher = agent_daemon.write_pythonw_launcher("t", argv, log, {"HEARTH_T": "1"})
    rc = subprocess.run([sys.executable, str(launcher)], timeout=60).returncode
    assert rc == 3
    assert "hearth-up" in log.read_text(encoding="utf-8")


def test_autostart_name_is_validated():
    with pytest.raises(ValueError):
        agent_daemon.install_user_autostart("bad name; rm -rf", ["x"], platform="linux")


def _home_main(argv):
    from adk.home import cli

    return cli.main(argv)


def test_cli_serve_install_forwards_serve_options_not_one_shot_ones(monkeypatch):
    from adk.home import cli

    got = {}

    def _install(name, argv, **kw):
        got.update(name=name, argv=argv, **kw)
        return "systemd:" + name

    monkeypatch.setattr(agent_daemon, "install_user_autostart", _install)
    monkeypatch.setenv("AITHER_AGENT_HOME", "/tmp/hh")
    for var in ("AITHER_RELAY_TOKEN", "AITHER_RECEIPTS_PATH", "HEARTH_LOCAL_PORT",
                "AITHER_RELAY_URL"):
        monkeypatch.delenv(var, raising=False)
    rc = _home_main(["serve", "--install", "--pair", "--channels", "local,relay",
                     "--nick", "hearth", "--no-local", "--poll", "9"])
    assert rc == 0
    assert got["name"] == cli.SERVE_AUTOSTART
    argv = got["argv"]
    assert argv[:4] == [sys.executable, "-m", "adk.home", "serve"]
    assert argv[4:] == ["--channels", "local,relay", "--nick", "hearth", "--poll", "9.0",
                        "--no-local"]
    assert "--install" not in argv and "--pair" not in argv
    assert got["env"] == {"AITHER_AGENT_HOME": "/tmp/hh"}


def test_cli_serve_install_failure_exits_1(monkeypatch):
    monkeypatch.setattr(agent_daemon, "install_user_autostart", lambda *a, **k: None)
    assert _home_main(["serve", "--install"]) == 1


def test_cli_serve_uninstall(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_daemon, "remove_user_autostart",
                        lambda name, **k: calls.append(name) or True)
    assert _home_main(["serve", "--uninstall"]) == 0
    assert calls == ["aither-hearth"]


def test_cli_install_and_uninstall_are_exclusive():
    with pytest.raises(SystemExit):
        _home_main(["serve", "--install", "--uninstall"])


# --------------------------------------------------------------------------- #
# (b) receipt head anchoring
# --------------------------------------------------------------------------- #

@pytest.fixture
def rhome(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    agent_home = tmp_path / "agent-home"
    monkeypatch.setattr(receipts, "_home_dir", lambda: agent_home)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    for var in (receipts.PATH_ENV, receipts.KEY_ENV, receipts.PUBKEY_ENV):
        monkeypatch.delenv(var, raising=False)
    return agent_home


def _log(rhome, rows=4):
    log = rhome / "actions.jsonl"
    for i in range(rows):
        receipts.append("tool", f"t{i}", {"i": i}, {"ok": True}, approval="auto", path=log)
    return log


def _lines(log):
    return log.read_bytes().split(b"\n")[:-1]


def test_anchor_written_beside_log_and_verifies(rhome):
    log = _log(rhome)
    anc = receipts.anchor(log)
    path = rhome / "receipts.anchor"
    assert path.is_file() and receipts.anchor_path_for(log) == path
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["seq"] == 3 and stored["signed"] is True and stored == anc
    code, reason = receipts.check(log)
    assert code == 0 and "anchored at seq 3" in reason


def test_rows_after_the_anchor_still_verify(rhome):
    log = _log(rhome)
    receipts.anchor(log)
    receipts.append("tool", "later", {}, {}, path=log)
    assert receipts.verify(log) == 0


def test_truncated_tail_is_tampered_with_anchor(rhome):
    log = _log(rhome)
    # Without an anchor a clean truncation is invisible (the stated limit)...
    log.write_bytes(b"\n".join(_lines(log)[:-1]) + b"\n")
    assert receipts.verify(log) == 0
    # ...with one, it is caught.
    log2 = _log(rhome.parent / "b", rows=4)
    receipts.anchor(log2)
    log2.write_bytes(b"\n".join(_lines(log2)[:-2]) + b"\n")
    code, reason = receipts.check(log2)
    assert code == 1 and "tail truncated" in reason


def test_truncate_then_reappend_diverges_from_anchor(rhome):
    log = _log(rhome)
    receipts.anchor(log)
    log.write_bytes(b"\n".join(_lines(log)[:-1]) + b"\n")
    receipts.append("tool", "replacement", {"x": 1}, {}, path=log)  # same seq 3, new row
    code, reason = receipts.check(log)
    assert code == 1 and "anchored head" in reason


def test_emptied_or_deleted_log_is_tampered_when_anchored(rhome):
    log = _log(rhome)
    receipts.anchor(log)
    log.write_bytes(b"")
    assert receipts.check(log)[0] == 1
    log.unlink()
    code, reason = receipts.check(log)
    assert code == 1 and "removed" in reason


def test_edited_anchor_is_tampered(rhome):
    log = _log(rhome)
    receipts.anchor(log)
    path = receipts.anchor_path_for(log)
    data = json.loads(path.read_text(encoding="utf-8"))
    # Lower the anchored seq to hide a truncation: the hash no longer matches.
    data["seq"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    assert receipts.verify(log) == 1
    # Re-point seq AND sha to the real line 2: the signature no longer matches.
    data["sha256"] = __import__("hashlib").sha256(_lines(log)[1]).hexdigest()
    path.write_text(json.dumps(data), encoding="utf-8")
    code, reason = receipts.check(log)
    assert code == 1 and "bad signature" in reason
    path.write_text("{not json", encoding="utf-8")
    assert receipts.verify(log) == 1


def test_anchor_refuses_a_tampered_or_missing_log(rhome):
    with pytest.raises(receipts.AnchorError) as missing:
        receipts.anchor(rhome / "actions.jsonl")
    assert missing.value.code == 2
    log = _log(rhome)
    receipts.anchor(log)
    log.write_bytes(b"\n".join(_lines(log)[:-1]) + b"\n")
    with pytest.raises(receipts.AnchorError) as bad:
        receipts.anchor(log)          # re-anchoring must not bless the truncation
    assert bad.value.code == 1


def test_unsigned_anchor_cannot_judge(rhome, monkeypatch):
    log = _log(rhome)
    monkeypatch.setattr(receipts, "_signing_key", lambda key_path=None, create=True: None)
    anc = receipts.anchor(log)
    assert anc["signed"] is False and anc["sig"] == ""
    code, reason = receipts.check(log)
    assert code == 2 and "unsigned" in reason


def test_cli_receipts_anchor_then_verify_truncation(rhome, monkeypatch, capsys):
    log = _log(rhome)
    monkeypatch.setenv("AITHER_RECEIPTS_PATH", str(log))
    assert _home_main(["receipts", "--anchor"]) == 0
    assert "anchored seq 3" in capsys.readouterr().out
    assert _home_main(["receipts", "--verify"]) == 0
    log.write_bytes(b"\n".join(_lines(log)[:-1]) + b"\n")
    assert _home_main(["receipts", "--verify"]) == 1
    assert "tail truncated" in capsys.readouterr().out
    assert _home_main(["receipts", "--anchor"]) == 1
