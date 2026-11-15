"""Typed Windows tools: File Locksmith, PowerToys Workspaces, winget and the new
Sysinternals wrappers. Every run goes through a FAKE runner or spawner fed with
output captured on the owner's machine on 2026-11-09; nothing is killed, launched
or dumped.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from adk.desktop import locksmith, winget, workspaces
from adk.desktop import tools as dt
from adk.desktop.powertoys import PowerToysError
from adk.desktop.sysinternals import (
    Sysinternals,
    SysinternalsError,
    parse_listdlls,
    parse_procdump,
    parse_pslist,
    parse_psping,
    parse_psservice,
)


def fake(stdout=b"", code=0, raise_timeout=False, seen=None, stderr=b""):
    def run(argv, timeout):
        if seen is not None:
            seen.append((list(argv), timeout))
        if raise_timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, code, stdout, stderr)
    return run


# -- File Locksmith ----------------------------------------------------------------

LOCKED = (b'{"processes":[{"pid":30580,"name":"python.exe","user":"wzns","files":'
          b'["C:\\\\Users\\\\wzns\\\\AppData\\\\Local\\\\Temp\\\\locked.txt"]}]}\r\n')


def test_locksmith_parses_holders_and_never_passes_kill():
    seen = []
    r = locksmith.who_locks(["C:/x/locked.txt"], runner=fake(LOCKED, seen=seen),
                            exe="FileLocksmithCLI.exe")
    assert r.processes[0]["pid"] == 30580 and r.processes[0]["name"] == "python.exe"
    assert seen[0][0] == ["FileLocksmithCLI.exe", "--json", "C:/x/locked.txt"]
    assert "--kill" not in seen[0][0] and seen[0][1] == 30.0


@pytest.mark.parametrize("paths", [[], ["--kill"], ["relative/file.txt"], ["-x"],
                                   ["C:/a"] * (locksmith.MAX_PATHS + 1)])
def test_locksmith_refuses_bad_path_lists(paths):
    with pytest.raises(locksmith.LocksmithError):
        locksmith.who_locks(paths, runner=fake(b"{}"), exe="x.exe")


def test_locksmith_empty_and_timeout_and_garbage():
    r = locksmith.who_locks(["C:/a"], runner=fake(b'{"processes":[]}'), exe="x.exe")
    assert r.processes == [] and r.error == ""
    r = locksmith.who_locks(["C:/a"], runner=fake(raise_timeout=True), exe="x.exe")
    assert r.timed_out and r.exit_code is None
    r = locksmith.who_locks(["C:/a"], runner=fake(b"not json"), exe="x.exe")
    assert "unparseable" in r.error


def test_file_locks_tool_is_json_when_missing(monkeypatch):
    monkeypatch.setattr(locksmith, "program_path", lambda name: None)
    out = json.loads(dt.desktop_file_locks(["C:/a"]))
    assert out["ok"] is False and out["reason"] == "not_installed"


# -- PowerToys Workspaces ----------------------------------------------------------

WS = {"workspaces": [
    {"id": "{5777753B-1F6F-478D-B758-7CDBEEF0CB72}", "name": "Workspace 1",
     "creation-time": 1773680677, "last-launched-time": 0,
     "move-existing-windows": False,
     "monitor-configuration": [{"id": "GSM5B9A"}, {"id": "GSMC0C8"}],
     "applications": [{"application": "Code", "title": "x"},
                      {"application": "Windows Terminal"}]},
    {"id": "{11111111-2222-3333-4444-555555555555}", "name": "Dup", "applications": []},
    {"id": "{66666666-2222-3333-4444-555555555555}", "name": "dup", "applications": []},
]}


@pytest.fixture
def ptroot(tmp_path):
    (tmp_path / "Workspaces").mkdir()
    (tmp_path / "Workspaces" / "workspaces.json").write_text(json.dumps(WS),
                                                             encoding="utf-8")
    return tmp_path


def test_workspaces_summarise(ptroot):
    rows = workspaces.list_workspaces(ptroot)
    assert rows[0]["name"] == "Workspace 1" and rows[0]["apps"] == 2
    assert rows[0]["app_names"] == ["Code", "Windows Terminal"] and rows[0]["monitors"] == 2


def test_workspace_resolution(ptroot):
    rows = workspaces.list_workspaces(ptroot)
    assert workspaces.resolve_workspace(rows, "workspace 1")["name"] == "Workspace 1"
    assert workspaces.resolve_workspace(
        rows, "5777753b-1f6f-478d-b758-7cdbeef0cb72")["name"] == "Workspace 1"
    with pytest.raises(PowerToysError, match="pass the id"):
        workspaces.resolve_workspace(rows, "DUP")
    with pytest.raises(PowerToysError, match="no workspace"):
        workspaces.resolve_workspace(rows, "nope")


def test_workspace_launch_dry_run_spawns_nothing(ptroot):
    spawned = []
    out = workspaces.launch_workspace("Workspace 1", root=ptroot, exe="L.exe",
                                      spawn=lambda argv: spawned.append(argv) or 1)
    assert out["dry_run"] is True and spawned == []
    assert out["argv"] == ["L.exe", "{5777753B-1F6F-478D-B758-7CDBEEF0CB72}"]


def test_workspace_launch_live_uses_the_spawner(ptroot):
    spawned = []
    out = workspaces.launch_workspace("Workspace 1", dry_run=False, root=ptroot,
                                      exe="L.exe",
                                      spawn=lambda argv: spawned.append(argv) or 4242)
    assert out["pid"] == 4242 and spawned == [out["argv"]]


def test_workspaces_missing_file_is_loud(tmp_path):
    with pytest.raises(PowerToysError):
        workspaces.list_workspaces(tmp_path)


# -- winget ------------------------------------------------------------------------

UPGRADES = (
    "Name               Id                      Version Available Source\r\n"
    "-------------------------------------------------------------------\r\n"
    "Git                Git.Git                 2.52.0  2.56.0.4  winget\r\n"
    "Proton Mail Bridge Proton.ProtonMailBridge 3.22.0  3.27.1    winget\r\n"
    "<additional entries truncated due to result limit>\r\n"
    "3 upgrades available.\r\n").encode("utf-8")

# A CJK name takes two display cells per character; winget pads by cells.
WIDE = ("Name              Id                 Version Available Source\r\n"
        "--------------------------------------------------------------\r\n"
        "AI*Shoujo/AI*\u5c11\u5973  ARP\\Steam App 125 Unknown\r\n"
        "7-Zip 26.02       7zip.7zip          26.02   26.06     winget\r\n").encode("utf-8")


def test_winget_parses_upgrades_and_ignores_footer():
    seen = []
    r = winget.list_upgradable(runner=fake(UPGRADES, seen=seen), exe="winget.exe")
    assert [row["id"] for row in r.rows] == ["Git.Git", "Proton.ProtonMailBridge"]
    assert r.rows[1]["name"] == "Proton Mail Bridge" and r.rows[0]["available"] == "2.56.0.4"
    assert r.truncated is True
    argv = seen[0][0]
    assert argv[:2] == ["winget.exe", "list"] and "--upgrade-available" in argv
    assert "--disable-interactivity" in argv
    assert not {"install", "upgrade", "uninstall"} & set(argv)


def test_winget_slices_by_display_cells_not_characters():
    rows, _ = winget.parse_table(WIDE.decode("utf-8"))
    assert rows[0]["name"] == "AI*Shoujo/AI*\u5c11\u5973"
    assert rows[0]["id"] == "ARP\\Steam App 125" and rows[0]["version"] == "Unknown"
    assert rows[1] == {"name": "7-Zip 26.02", "id": "7zip.7zip", "version": "26.02",
                       "available": "26.06", "source": "winget"}


def test_winget_no_packages_exit_is_an_empty_answer():
    r = winget.list_upgradable(runner=fake(b"No installed package found.\r\n",
                                           code=0x8A150014), exe="w.exe")
    assert r.rows == [] and r.error == ""
    assert winget.to_dict(r)["ok"] is True


def test_winget_query_and_limit_are_bounded():
    seen = []
    winget.list_installed("python", 5000, runner=fake(b"", seen=seen), exe="w.exe")
    argv = seen[0][0]
    assert argv[argv.index("--query") + 1] == "python"
    assert argv[argv.index("--count") + 1] == "1000"
    with pytest.raises(winget.WingetError):
        winget.list_installed("--force", runner=fake(), exe="w.exe")


def test_winget_timeout_is_reported():
    r = winget.list_installed(runner=fake(raise_timeout=True), exe="w.exe")
    assert r.timed_out and winget.to_dict(r)["ok"] is False


# -- Sysinternals: pslist, psservice, listdlls, psping, procdump --------------------

PSLIST = """Process information for DESKTOP-EG38V5F:

Name                Pid Pri Thd  Hnd   Priv        CPU Time    Elapsed Time
Idle                  0   0  96    0     60   108:52:48.640    33:59:21.577
Secure System       432   8   0    0    176     0:00:00.000    33:59:24.953
"""

PSSERVICE = """SERVICE_NAME: Spooler
DISPLAY_NAME: Print Spooler
This service spools print jobs.
\tGROUP\t\t  : SpoolerGroup
\tTYPE\t\t  : 110 WIN32_OWN_PROCESS INTERACTIVE_PROCESS
\tSTATE\t\t  : 4  RUNNING
\t\t\t       (STOPPABLE,NOT_PAUSABLE,IGNORES_SHUTDOWN)
\tWIN32_EXIT_CODE\t  : 0  (0x0)
\tSERVICE_EXIT_CODE : 0  (0x0)

SERVICE_NAME: ADPSvc
DISPLAY_NAME: Aggregated Data Platform Service
\tSTATE\t\t  : 1  STOPPED
\tWIN32_EXIT_CODE\t  : 1077 (0x435)
"""

LISTDLLS = """------------------------------------------------------------------------------
explorer.exe pid: 12804
Command line: C:\\WINDOWS\\Explorer.EXE

Base                Size      Path
0x000000005c1a0000  0x344000  C:\\WINDOWS\\Explorer.EXE
0x0000000072da0000  0x268000  C:\\WINDOWS\\SYSTEM32\\ntdll.dll
Error opening System(4):
Access is denied.
"""

PSPING_OK = """TCP connect to 127.0.0.1:8362:
3 iterations (warmup 0) ping test:
Connecting to 127.0.0.1:8362: from 127.0.0.1:61559: 0.43ms
Connecting to 127.0.0.1:8362: from 127.0.0.1:61609: 0.54ms

TCP connect statistics for 127.0.0.1:8362:
  Sent = 2, Received = 2, Lost = 0 (0% loss),
  Minimum = 0.39ms, Maximum = 0.54ms, Average = 0.46ms
"""

PSPING_REFUSED = """TCP connect to 127.0.0.1:1:
Connecting to 127.0.0.1:1: from 0.0.0.0:24384:
The remote computer refused the network connection.

  Sent = 1, Received = 0, Lost = 1 (100% loss),
"""


@pytest.fixture
def home(tmp_path):
    for t in ("pslist64", "PsService64", "Listdlls64", "psping64", "procdump64"):
        (tmp_path / f"{t}.exe").write_bytes(b"")
    return tmp_path


def test_pslist_keeps_spaced_names():
    rows, extra = parse_pslist(PSLIST)
    assert rows[1]["name"] == "Secure System" and rows[1]["pid"] == "432"
    assert rows[0]["cpu_time"] == "108:52:48.640" and extra == {}
    rows, extra = parse_pslist("process x was not found on HOST\r\n")
    assert rows == [] and "not_found" in extra


def test_psservice_blocks():
    rows, _ = parse_psservice(PSSERVICE)
    assert rows[0]["service_name"] == "Spooler" and rows[0]["state"] == "RUNNING"
    assert rows[0]["description"] == "This service spools print jobs."
    assert rows[0]["controls"].startswith("STOPPABLE") and rows[0]["group"] == "SpoolerGroup"
    assert rows[1]["state"] == "STOPPED" and rows[1]["win32_exit_code"] == "1077"


def test_listdlls_rows_and_denials():
    rows, extra = parse_listdlls(LISTDLLS)
    assert [r["path"] for r in rows][1].endswith("ntdll.dll")
    assert rows[0]["process"] == "explorer.exe" and rows[0]["pid"] == "12804"
    assert extra["access_denied"] == ["System(4)"]


def test_psping_attempts_and_stats():
    rows, extra = parse_psping(PSPING_OK)
    assert [r["ms"] for r in rows] == [0.43, 0.54] and all(r["ok"] for r in rows)
    assert extra["sent"] == 2 and extra["lost"] == 0 and extra["average_ms"] == 0.46
    rows, extra = parse_psping(PSPING_REFUSED)
    assert rows == [{"ok": False, "ms": None,
                     "error": "The remote computer refused the network connection."}]
    assert extra["lost"] == 1


def test_procdump_progress():
    rows, _ = parse_procdump("[10:00:00] Dump 1 initiated: C:\\d\\x.dmp\n"
                             "[10:00:01] Dump 1 complete: 5 MB written in 0.4 seconds\n")
    assert rows == [{"dump": 1, "path": "C:\\d\\x.dmp", "complete": True,
                     "detail": "5 MB written in 0.4 seconds"}]


def test_new_wrappers_pass_safe_argv(home):
    seen = []
    si = Sysinternals(home, fake(b"", seen=seen))
    si.processes("chrome")
    si.services()
    si.services("Spooler")
    si.modules("explorer", unsigned_only=True)
    si.ping("127.0.0.1:8362", count=99)
    argvs = [a for a, _ in seen]
    assert argvs[0][-1] == "chrome"
    assert argvs[1][-3:] == ["query", "-s", "all"] and argvs[2][-2:] == ["query", "Spooler"]
    assert argvs[3][-2:] == ["-u", "explorer"]
    assert argvs[4][3:] == ["-n", "20", "-w", "0", "127.0.0.1:8362"]
    assert "-t" not in argvs[4]
    assert all(t <= 70 for _, t in seen)


@pytest.mark.parametrize("call", [
    lambda si: si.processes("-d"),
    lambda si: si.modules(""),
    lambda si: si.modules("-d ntdll"),
    lambda si: si.ping("-t"),
    lambda si: si.ping("a b"),
    lambda si: si.services(state="weird"),
    lambda si: si.services(name="-x"),
    lambda si: si.dump_argv(4),
])
def test_wrappers_refuse_flags_and_bad_targets(home, call):
    seen = []
    with pytest.raises(SysinternalsError):
        call(Sysinternals(home, fake(seen=seen)))
    assert seen == []


def test_procdump_argv_is_one_immediate_dump(home, tmp_path):
    argv = Sysinternals(home, fake()).dump_argv(1234, full=False, dump_dir=tmp_path / "d")
    assert argv[1:3] == ["-accepteula", "-nobanner"]
    assert argv[3:] == ["-mp", "1234", str(tmp_path / "d")]
    full = Sysinternals(home, fake()).dump_argv(1234, full=True, dump_dir=tmp_path)
    assert full[3] == "-ma"


def test_procdump_tool_dry_run_writes_nothing(monkeypatch, home, tmp_path):
    from adk.desktop import sysinternals as si_mod

    ran = []
    monkeypatch.setattr(si_mod, "Sysinternals",
                        lambda: Sysinternals(home, fake(seen=ran)))
    out = json.loads(dt.desktop_procdump(1234, dump_dir=str(tmp_path / "dumps")))
    assert out["ok"] is True and out["dry_run"] is True and ran == []
    assert not (tmp_path / "dumps").exists()


def test_processes_tool_not_found_is_ok(monkeypatch, home):
    from adk.desktop import sysinternals as si_mod

    monkeypatch.setattr(si_mod, "Sysinternals", lambda: Sysinternals(
        home, fake(b"process zz was not found on HOST\r\n", code=1)))
    out = json.loads(dt.desktop_processes("zz"))
    assert out["ok"] is True and out["rows"] == [] and "not_found" in out["extra"]


def test_bad_target_is_bad_args_not_not_installed(monkeypatch, home):
    from adk.desktop import sysinternals as si_mod

    monkeypatch.setattr(si_mod, "Sysinternals", lambda: Sysinternals(home, fake()))
    out = json.loads(dt.desktop_ping("-t"))
    assert out["ok"] is False and out["reason"] == "bad_args"
