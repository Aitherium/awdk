"""The logon task starts the agent on a Windows that has no VBScript.

Measured 2026-11-16 on Windows build 26300 (clean Sandbox): no vbscript.dll, so
``wscript.exe //B //Nologo run-hidden.vbs aither-agent.cmd`` fails ("There is no script engine
for file extension .vbs"), the AitherAgent task records Last Result 1 at every logon, and the
agent never comes back after a restart. VBScript is an optional Windows feature and Microsoft is
turning it off by default. ``conhost.exe --headless cmd.exe /c <wrapper>`` was measured working
through Task Scheduler on the same machine (Last Result 0) and, like wscript, is GUI-subsystem,
so it allocates no console to steal the desktop's focus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adk import agent_daemon as d
from adk import llamacpp_setup as lcs

SHIM = Path(r"C:\Users\g\.aither\run-hidden.vbs")
WRAPPER = Path(r"C:\Users\g\.aither\aither-agent.cmd")


@pytest.fixture
def system_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    (tmp_path / "System32").mkdir()
    return tmp_path


def test_vbscript_is_detected_by_its_engine_dll(system_root):
    assert lcs.vbscript_available() is False
    (system_root / "System32" / "vbscript.dll").write_bytes(b"MZ")
    assert lcs.vbscript_available() is True


def test_with_vbscript_the_shim_is_used_as_before(system_root):
    (system_root / "System32" / "vbscript.dll").write_bytes(b"MZ")
    assert lcs.hidden_launch_command(SHIM, WRAPPER) == (
        f'wscript.exe //B //Nologo "{SHIM}" "{WRAPPER}"'
    )


def test_without_vbscript_the_wrapper_runs_headless_through_conhost(system_root):
    cmd = lcs.hidden_launch_command(SHIM, WRAPPER)
    assert cmd == f'conhost.exe --headless cmd.exe /d /c ""{WRAPPER}""'
    assert "wscript" not in cmd and "vbs" not in cmd


def test_the_scheduled_task_value_follows_it_and_still_names_the_wrapper(system_root):
    value = lcs.hidden_task_run(SHIM, WRAPPER)
    assert value.startswith("conhost.exe --headless")
    # adk's ownership check finds a task by the wrapper path in its command
    assert d._names_wrapper(value, str(WRAPPER))
    assert len(value) <= lcs._SCHTASKS_TR_MAX


def test_the_run_key_fallback_uses_the_same_command(system_root, monkeypatch, tmp_path):
    seen = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    import types

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=1,
        KEY_SET_VALUE=2,
        REG_SZ=3,
        OpenKey=lambda *a, **k: _Key(),
        SetValueEx=lambda key, name, _r, _t, value: seen.update(name=name, value=value),
    )
    monkeypatch.setitem(__import__("sys").modules, "winreg", fake)
    monkeypatch.setattr(d, "AITHER_HOME", tmp_path / "home")
    assert d._install_hkcu_run(str(WRAPPER)) is True
    assert seen["name"] == d.WINDOWS_TASK_NAME
    assert seen["value"].startswith("conhost.exe --headless")
