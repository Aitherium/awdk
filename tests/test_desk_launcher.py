"""How a credential ask starts awdesk: ``notify.desk_launcher`` and its spawn flags.

Owner, 2026-10-28: credential intake goes through awdesk, never a one-off Python
window, so an ask that finds the desk down starts it the way the machine starts it at
logon. These pin WHICH command that is on each platform, and that the Windows logon
shim runs where its console children borrow a hidden console instead of each opening
a window. Every path is a tmp dir; nothing here runs a launcher.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adk.decisions import notify


@pytest.fixture(autouse=True)
def _no_installed_desk(monkeypatch):
    # The machine running the tests may have a real Desk.exe; the resolver must
    # only ever see the tmp homes below.
    import adk.desk_release as release

    monkeypatch.setattr(release, "installed_desk_windows", lambda: None)
    monkeypatch.setattr(release, "find_desk_exe", lambda installed: "")


def _startup(tmp_path: Path) -> Path:
    folder = (tmp_path / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu"
              / "Programs" / "Startup")
    folder.mkdir(parents=True)
    return folder


def test_windows_logon_shim_runs_under_cscript(tmp_path):
    shim = _startup(tmp_path) / "aither-awdesk.vbs"
    shim.write_text("' shim\n", encoding="utf-8")
    env = {"APPDATA": str(tmp_path / "AppData" / "Roaming"), "SystemRoot": r"C:\Windows"}

    found = notify.desk_launcher(env=env, home=tmp_path, platform="win32")

    assert found is not None
    # cscript, never wscript: the shim Execs netstat, and under the windowless
    # wscript that child got its own console window (measured 2026-10-28).
    assert found.argv[0].replace("\\", "/").lower().endswith("system32/cscript.exe")
    assert found.argv[1:] == ["//B", "//Nologo", str(shim)]
    assert "aither-awdesk.vbs" in found.how


def test_launcher_spawn_lends_a_hidden_console():
    create_no_window, detached_process = 0x08000000, 0x00000008
    assert notify._LAUNCHER_FLAGS & create_no_window  # noqa: SLF001
    # DETACHED_PROCESS would leave cscript no console to lend netstat.
    assert not notify._LAUNCHER_FLAGS & detached_process  # noqa: SLF001


def test_explicit_command_wins_over_the_shim(tmp_path):
    (_startup(tmp_path) / "aither-awdesk.vbs").write_text("' shim\n", encoding="utf-8")
    env = {"APPDATA": str(tmp_path / "AppData" / "Roaming"),
           notify.DESK_START_ENV: r'"C:\Program Files\Desk\Desk.exe" --background'}

    found = notify.desk_launcher(env=env, home=tmp_path, platform="win32")

    assert found is not None
    assert found.argv == [r"C:\Program Files\Desk\Desk.exe", "--background"]


def test_windows_with_nothing_installed_is_none(tmp_path):
    env = {"APPDATA": str(tmp_path / "AppData" / "Roaming")}
    assert notify.desk_launcher(env=env, home=tmp_path, platform="win32") is None


def test_linux_autostart_entry_drops_field_codes(tmp_path):
    entry = tmp_path / ".config" / "autostart" / "aither-desk.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text('[Desktop Entry]\nExec="/opt/Desk/Desk.AppImage" --background %U\n',
                     encoding="utf-8")

    found = notify.desk_launcher(env={}, home=tmp_path, platform="linux")

    assert found is not None
    assert found.argv == ["/opt/Desk/Desk.AppImage", "--background"]


def test_macos_launch_agent_is_kickstarted(tmp_path, monkeypatch):
    agent = tmp_path / "Library" / "LaunchAgents" / "com.aitherium.desk.plist"
    agent.parent.mkdir(parents=True)
    agent.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

    found = notify.desk_launcher(env={}, home=tmp_path, platform="darwin")

    assert found is not None
    assert found.argv == ["launchctl", "kickstart", "gui/501/com.aitherium.desk"]
