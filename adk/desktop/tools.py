"""Agent-facing Windows desktop tools. JSON in, JSON out.

:data:`DESKTOP_TOOLS` is the ONE list of them, with each tool's action class:
``register_desktop_tools`` (an awdk agent), the builtin ``desktop`` category and
the harness daemon's ``/desk/tools`` route (the fleet's reach, through the MCP
gateway's mcp_desktop proxy) all read it, so a tool added there is everywhere.
Every tool is read-only unless its class is ``write``. A write tool that can
plan takes ``dry_run`` and defaults it to True; a write tool that only acts
(the Windows Sandbox detonate, run and install-test tools) has no dry run, so
the route refuses it until the caller sends ``confirm=true``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["desktop_list_windows", "desktop_arrange", "register_desktop_tools",
           "desktop_sigcheck", "desktop_ports", "desktop_disk_usage", "desktop_autoruns",
           "desktop_who_holds", "SYSINTERNALS_TOOLS", "desktop_processes",
           "desktop_services", "desktop_modules", "desktop_ping", "desktop_procdump",
           "desktop_file_locks", "desktop_workspaces", "desktop_workspace_launch",
           "desktop_installed_apps", "desktop_upgradable_apps",
           "desktop_sandbox_doctor", "desktop_sandbox_install", "desktop_sandbox_detonate",
           "desktop_sandbox_run", "desktop_sandbox_install_test", "desktop_sandbox_deps",
           "SANDBOX_TOOLS", "DESKTOP_TOOLS", "desktop_tool_table"]


def _backend() -> Any:
    """The backend the tools use; tests monkeypatch this."""
    from adk.desktop.windows import default_backend

    return default_backend()


def desktop_list_windows(query: str = "", taskbar_only: bool = True) -> str:
    """List real, visible top-level windows on this Windows desktop.

    Returns JSON {"ok": bool, "windows": [...]} on success and on failure.

    query: case-insensitive substring of the window title or process name; empty lists all.
    taskbar_only: true keeps only windows Explorer shows on the taskbar.
    """
    from adk.desktop.windows import DesktopError, list_windows

    try:
        rows = list_windows(backend=_backend(), taskbar_only=taskbar_only, query=query)
    except DesktopError as exc:
        return json.dumps({**exc.to_dict(), "windows": []})
    return json.dumps({"ok": True, "windows": [w.to_dict() for w in rows]})


def desktop_arrange(windows: list[str], mode: str = "side_by_side", monitor: int = -1,
                    gap: int = 8, dry_run: bool = True, layout: str = "direct",
                    activate: bool = False) -> str:
    """Arrange named windows side by side, stacked or in a grid (dry run by default).

    windows: window titles, title/process substrings, or hwnd:<n>, in placement order.
    mode: side_by_side, stacked or grid.
    monitor: monitor index; -1 uses the monitor holding the first window.
    gap: pixels between windows.
    dry_run: true (default) returns the plan and moves nothing; false performs the moves.
    layout: direct (default) or fancyzones:<layout-uuid> to apply a saved FancyZones layout
        to a whole monitor; that mode moves no named window, so windows must be [].
    activate: bring the first window to the foreground after moving.
    """
    from adk.desktop.windows import DesktopError, arrange

    layout = (layout or "direct").strip()
    if layout.lower().startswith("fancyzones:"):
        from adk.desktop import fancyzones

        if windows:
            return json.dumps({"ok": False, "reason": "bad_layout", "needs_admin_lane": False,
                               "error": "fancyzones:<uuid> applies a whole-monitor layout and "
                                        "places no named window; pass windows=[] or use "
                                        "layout='direct' to arrange specific windows"})
        # arrange() numbers monitors from 0; the FancyZones CLI numbers them from 1
        # ("Monitor 1" in get-monitors, "--monitor 1" in its own help).
        fz_monitor = monitor + 1 if monitor is not None and monitor >= 0 else -1
        try:
            return json.dumps(fancyzones.apply_layout(
                layout.split(":", 1)[1], monitor=fz_monitor, dry_run=dry_run))
        except fancyzones.PowerToysError as exc:
            return json.dumps({"ok": False, "reason": "fancyzones", "error": str(exc),
                               "needs_admin_lane": False})
    if layout.lower() != "direct":
        return json.dumps({"ok": False, "reason": "bad_layout", "needs_admin_lane": False,
                           "error": f"layout must be 'direct' or 'fancyzones:<uuid>', "
                                    f"got {layout!r}"})
    try:
        receipt = arrange(windows, mode=mode, monitor=monitor, gap=gap, dry_run=dry_run,
                          activate=activate, backend=_backend())
    except DesktopError as exc:
        return json.dumps(exc.to_dict())
    return json.dumps(receipt)


def register_desktop_tools(agent: Any) -> int:
    """Register the desktop tools on ``agent``; returns how many were registered."""
    for fn, action_class in DESKTOP_TOOLS:
        agent._tools.register(fn, action_class=action_class)
    return len(DESKTOP_TOOLS)


# -- Sysinternals: diagnostics as typed tools ----------------------------------

def _sysinternals_call(method: str, *args: Any) -> str:
    from adk.desktop.sysinternals import Sysinternals, SysinternalsError

    try:
        r = getattr(Sysinternals(), method)(*args)
    except SysinternalsError as exc:
        text = str(exc)
        reason = "not_installed" if "not found" in text else "bad_args"
        return json.dumps({"ok": False, "reason": reason, "error": text, "rows": []})
    except (TypeError, ValueError) as exc:
        return json.dumps({"ok": False, "reason": "bad_args", "error": str(exc), "rows": []})
    extra = getattr(r, "extra", None) or {}
    # "no such process" is an answer (zero rows), whatever exit code pslist chose.
    ok = not r.timed_out and (r.exit_code == 0 or bool(extra.get("not_found")))
    out: Dict[str, Any] = {"ok": ok, "tool": r.tool, "exit_code": r.exit_code,
                           "timed_out": r.timed_out, "rows": r.rows,
                           "error": "" if ok else (r.stderr or f"exit {r.exit_code}")}
    if extra:
        out["extra"] = extra
    return json.dumps(out)


def desktop_sigcheck(path: str) -> str:
    """Is this Windows binary signed, by whom, and which version (Sysinternals sigcheck).

    path: full path to an .exe/.dll/.sys file.
    """
    return _sysinternals_call("sigcheck", path)


def desktop_ports() -> str:
    """Which process owns each TCP/UDP endpoint on this Windows machine (Sysinternals tcpvcon)."""
    return _sysinternals_call("ports")


def desktop_disk_usage(path: str, depth: int = 0) -> str:
    """Size of a folder and its subfolders on this Windows machine (Sysinternals du).

    path: folder to measure.
    depth: subfolder levels to report; 0 = the folder total only.
    """
    return _sysinternals_call("disk_usage", path, depth)


def desktop_autoruns() -> str:
    """Everything that starts at boot/logon, with signatures and hashes (Sysinternals autorunsc).

    Full results need an elevated process; non-elevated runs list the current user's entries.
    """
    return _sysinternals_call("autoruns")


def desktop_who_holds(name: str) -> str:
    """Which process holds an open handle to a file or object (Sysinternals handle).

    name: part of the file path or object name. Needs an elevated process; times out
    after 30 s otherwise.
    """
    return _sysinternals_call("who_holds", name)


SYSINTERNALS_TOOLS = [desktop_sigcheck, desktop_ports, desktop_disk_usage,
                      desktop_autoruns, desktop_who_holds]


def desktop_processes(name: str = "") -> str:
    """Running processes on this Windows machine with pid, priority, threads, handles,
    private memory (KB), CPU time and elapsed time (Sysinternals pslist). Read-only.

    name: optional image-name prefix (e.g. "chrome") or a pid; empty lists all.
    """
    return _sysinternals_call("processes", name)


def desktop_services(name: str = "", state: str = "all") -> str:
    """Windows services and whether each is RUNNING or STOPPED, with type, exit codes and
    accepted controls (Sysinternals psservice query). Read-only: never starts or stops.

    name: one service's short name (e.g. "Spooler"); empty lists every service.
    state: active, inactive or all (default) when name is empty.
    """
    return _sysinternals_call("services", name, state)


def desktop_modules(process: str, unsigned_only: bool = False) -> str:
    """DLLs a running process has loaded, with base address, size and path
    (Sysinternals listdlls). Read-only.

    process: image name (partial accepted, e.g. "explorer") or pid. Required.
    unsigned_only: true lists only DLLs with no valid signature.
    """
    return _sysinternals_call("modules", process, unsigned_only)


def desktop_ping(target: str, count: int = 4) -> str:
    """ICMP ping a host, or measure TCP connect latency to host:port, from this Windows
    machine (Sysinternals psping). Read-only. Answers "can this PC reach X, how fast".

    target: hostname or IP, optionally :port for a TCP connect test (e.g. 127.0.0.1:8362).
    count: attempts, 1..20 (default 4).
    """
    return _sysinternals_call("ping", target, count)


def desktop_procdump(pid: int, full: bool = False, dump_dir: str = "",
                     dry_run: bool = True) -> str:
    """WRITE: capture one memory dump of a running process (Sysinternals procdump) for
    crash/hang analysis. Dry run by default: returns the command and writes nothing.

    pid: process id to dump (from desktop_processes); 0-4 are refused.
    full: true writes a full dump (-ma, can be GBs); false a MiniPlus dump (-mp).
    dump_dir: folder for the .dmp file; empty uses LOCALAPPDATA/Aither/dumps.
    dry_run: true (default) returns the plan only; false writes the dump.
    """
    from adk.desktop.sysinternals import Sysinternals, SysinternalsError

    try:
        si = Sysinternals()
        argv = si.dump_argv(pid, full, dump_dir or None)
        if dry_run:
            return json.dumps({"ok": True, "dry_run": True, "tool": "procdump",
                               "argv": argv})
        r = si.dump(pid, full, dump_dir or None)
    except SysinternalsError as exc:
        text = str(exc)
        return json.dumps({"ok": False, "reason": "not_installed" if "not found" in text
                           else "bad_args", "error": text, "rows": []})
    except (TypeError, ValueError, OSError) as exc:
        return json.dumps({"ok": False, "reason": "bad_args", "error": str(exc), "rows": []})
    ok = not r.timed_out and bool(r.rows) and all(row.get("complete") for row in r.rows)
    return json.dumps({"ok": ok, "dry_run": False, "tool": "procdump",
                       "exit_code": r.exit_code, "timed_out": r.timed_out, "rows": r.rows,
                       "error": "" if ok else (r.stderr or f"exit {r.exit_code}")})


# -- PowerToys: File Locksmith and Workspaces ------------------------------------

def desktop_file_locks(paths: List[str]) -> str:
    """Which processes hold files or folders open, so they cannot be deleted, moved or
    rebuilt (PowerToys File Locksmith). Read-only: never kills the holders.

    paths: absolute file or folder paths (1-32), e.g. ["C:/build/out.dll"].
    """
    from adk.desktop.locksmith import LocksmithError, who_locks

    try:
        r = who_locks(paths)
    except LocksmithError as exc:
        reason = "not_installed" if "not found" in str(exc) else "bad_args"
        return json.dumps({"ok": False, "reason": reason, "error": str(exc),
                           "processes": []})
    ok = not r.timed_out and not r.error
    return json.dumps({"ok": ok, "tool": "file_locksmith", "exit_code": r.exit_code,
                       "timed_out": r.timed_out, "processes": r.processes,
                       "error": r.error})


def desktop_workspaces() -> str:
    """Saved PowerToys Workspaces (named sets of apps and window positions) on this
    Windows desktop: id, name, app count and app names. Read-only.
    """
    from adk.desktop.powertoys import PowerToysError
    from adk.desktop.workspaces import list_workspaces

    try:
        rows = list_workspaces()
    except PowerToysError as exc:
        return json.dumps({"ok": False, "reason": "not_installed", "error": str(exc),
                           "workspaces": []})
    return json.dumps({"ok": True, "workspaces": rows})


def desktop_workspace_launch(workspace: str, dry_run: bool = True) -> str:
    """WRITE: open every app in a saved PowerToys Workspace and move each into its saved
    place. Dry run by default: returns the resolved workspace and command only.

    workspace: the workspace id or its exact name (from desktop_workspaces).
    dry_run: true (default) launches nothing; false launches the workspace.
    """
    from adk.desktop.powertoys import PowerToysError
    from adk.desktop.workspaces import launch_workspace

    try:
        return json.dumps(launch_workspace(workspace, dry_run=dry_run))
    except PowerToysError as exc:
        return json.dumps({"ok": False, "reason": "workspace", "error": str(exc)})


# -- winget: installed software (read-only) --------------------------------------

def _winget_failure(exc: Exception) -> str:
    reason = "not_installed" if "not found" in str(exc) else "bad_args"
    return json.dumps({"ok": False, "reason": reason, "error": str(exc), "rows": []})


def desktop_installed_apps(query: str = "", limit: int = 0) -> str:
    """Software installed on this Windows machine: name, package id, version, the newer
    version when one is available, and source (winget list). Read-only; installs nothing.

    query: optional name/id substring filter, e.g. "python".
    limit: at most this many rows (1-1000); 0 = all.
    """
    from adk.desktop import winget

    try:
        return json.dumps(winget.to_dict(winget.list_installed(query, limit)))
    except winget.WingetError as exc:
        return _winget_failure(exc)


def desktop_upgradable_apps(query: str = "") -> str:
    """Installed Windows software that has a newer version available (winget list
    --upgrade-available). Read-only; upgrades nothing.

    query: optional name/id substring filter.
    """
    from adk.desktop import winget

    try:
        return json.dumps(winget.to_dict(winget.list_upgradable(query)))
    except winget.WingetError as exc:
        return _winget_failure(exc)


# -- Windows Sandbox: throwaway-VM app tests, installers and detonation --------
#
# The upstream sandbox-mcp CLI (MIT) is driven as an external tool; see
# adk/desktop/sandbox.py for the locate, verify and safety rules. Every receipt
# says findings are heuristics and that report fields are untrusted DATA.

#: A receipt larger than this (characters of JSON) is returned as a bounded preview.
_MAX_SANDBOX_CHARS = 20000

#: Where upstream writes the scheduled tasks it plants (src/detonate.js v1.2.0).
_TASK_PATH_PREFIX = "c:\\windows\\system32\\tasks\\"


def _sandbox_error(exc: Exception) -> str:
    return json.dumps({"ok": False, "reason": getattr(exc, "reason", "bad_args"),
                       "error": str(exc), "findings_are": "heuristics, not a verdict",
                       "data": {}})


def _sandbox_count(value: Any) -> Optional[int]:
    """len() of one reported list, or None when the command did not report it."""
    return len(value) if isinstance(value, list) else None


def _sandbox_sha256(data: Any) -> Optional[str]:
    """The digest the CLI reported for the file it ran.

    Deliberately never read from ``report`` (report.json is guest evidence and can
    disagree with what was executed); only the CLI's own JSON is trusted for this.
    """
    if not isinstance(data, dict):
        return None
    for candidate in (data, data.get("result"), data.get("run")):
        if isinstance(candidate, dict):
            value = candidate.get("sha256")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


#: Registry value paths upstream's detonate findings() treats as autoruns.
_AUTORUN_VALUE = re.compile(r"\\(?:Run|RunOnce)\\|\\Winlogon\\|\\Image File Execution Options", re.IGNORECASE)
#: The capped summary keeps the error, but never more than this of it.
_MAX_SANDBOX_ERROR_CHARS = 2000


def _sandbox_counts(data: Any) -> Dict[str, Optional[int]]:
    """Per-category counts from the fields sandbox-mcp v1.2.0 ``src/detonate.js`` writes.

    A category whose source field is absent stays None (not reported), never 0.
    """
    counts: Dict[str, Optional[int]] = {
        "processes": None, "lookups": None, "connections": None,
        "services_added": None, "persistence_values": None,
        "scheduled_tasks": None, "findings": None,
    }
    if not isinstance(data, dict):
        return counts
    counts["processes"] = _sandbox_count(data.get("tree"))
    counts["lookups"] = _sandbox_count(data.get("lookups"))
    counts["connections"] = _sandbox_count(data.get("connections"))
    counts["findings"] = _sandbox_count(data.get("findings"))
    diff = data.get("diff")
    if isinstance(diff, dict):
        services = diff.get("services")
        if isinstance(services, list):
            counts["services_added"] = sum(
                1 for s in services if isinstance(s, dict) and s.get("change") == "added")
        values = diff.get("values")
        if isinstance(values, list):
            # Autorun values only (Run, RunOnce, Winlogon, IFEO), as upstream's
            # findings() filters them; environment-variable changes are not
            # persistence. A removed value is not something the file planted.
            counts["persistence_values"] = sum(
                1 for v in values
                if isinstance(v, dict) and v.get("change") != "removed"
                and isinstance(v.get("name"), str) and _AUTORUN_VALUE.search(v["name"]))
        files = diff.get("files")
        added = files.get("added") if isinstance(files, dict) else None
        if isinstance(added, list):
            counts["scheduled_tasks"] = sum(
                1 for f in added
                if isinstance(f, dict) and isinstance(f.get("path"), str)
                and f["path"].replace("/", "\\").lower().startswith(_TASK_PATH_PREFIX))
    return counts


def _sandbox_receipt(r: Any) -> str:
    payload: Dict[str, Any] = {
        "ok": bool(r.ok), "command": r.command, "exit_code": r.exit_code,
        "timed_out": r.timed_out, "data": r.data, "run_dir": r.run_dir,
        "report": r.report, "findings_are": "heuristics, not a verdict",
        "error": "" if r.ok else (r.stderr or r.parse_error or "exit %s" % r.exit_code),
    }
    text = json.dumps(payload)
    if len(text) <= _MAX_SANDBOX_CHARS:
        return text
    # The bulk is `data` (the whole run result) and `report` (which repeats it).
    # A client still needs the envelope to reach the evidence and to compare the
    # digest, so the summary keeps every scalar the uncapped shape carried -- the
    # run folder the CLI named, the exit code, the executed digest -- plus counts
    # taken from the full result BEFORE it is capped. Only the preview is capped.
    summary: Dict[str, Any] = {
        "ok": payload["ok"], "command": payload["command"],
        "exit_code": payload["exit_code"], "timed_out": payload["timed_out"],
        "run_dir": payload["run_dir"], "sha256": _sandbox_sha256(payload["data"]),
        "counts": _sandbox_counts(payload["data"]),
        "findings_are": "heuristics, not a verdict",
        "error": str(payload["error"] or "")[:_MAX_SANDBOX_ERROR_CHARS],
        "truncated": True, "preview": "",
    }
    # Keep the whole receipt at or under the cap: the summary is always whole, so
    # only the preview gives ground (re-encoding escapes quotes and backslashes).
    budget = _MAX_SANDBOX_CHARS
    while True:
        summary["preview"] = text[:budget]
        out = json.dumps(summary)
        if len(out) <= _MAX_SANDBOX_CHARS or budget == 0:
            return out
        budget = max(0, budget - (len(out) - _MAX_SANDBOX_CHARS))


def desktop_sandbox_doctor() -> str:
    """Is Windows Sandbox ready on this PC? Read-only readiness check (sandbox-mcp
    `sbx doctor`): Windows edition/build, the Containers-DisposableClientVM feature,
    virtualization and the wsb tool. Exit 0 = ready, 1 = not ready.

    Never enables a feature or elevates; a not-ready answer includes the exact
    administrator command and the one-time steps for the owner.
    """
    from adk.desktop.sandbox import OWNER_ONCE, Sandbox, SandboxError
    from adk.desktop.sandbox import ADMIN_FEATURE_COMMAND

    try:
        r = Sandbox().doctor()
    except SandboxError as exc:
        return _sandbox_error(exc)
    if r.ok:
        return _sandbox_receipt(r)
    out: Dict[str, Any] = {"ok": False, "ready": False, "command": r.command,
                           "exit_code": r.exit_code, "timed_out": r.timed_out,
                           "data": r.data, "admin_command": ADMIN_FEATURE_COMMAND,
                           "owner_once": OWNER_ONCE,
                           "findings_are": "heuristics, not a verdict",
                           "error": r.stderr or r.parse_error or "not ready"}
    return json.dumps(out)


def desktop_sandbox_install(version: str = "latest", dry_run: bool = True) -> str:
    """WRITE: download and install the sandbox-mcp `sbx` CLI so the other sandbox
    tools can run. Dry run by default: shows the URLs and destination only.

    Downloads the GitHub release ZIP for the requested version ("latest" asks
    the GitHub API), verifies its published SHA-256 BEFORE extracting, and unpacks to
    ~/.aither/tools/sandbox-mcp/<version>. It never enables Windows features,
    never elevates, and never edits PATH or MCP configuration.
    """
    from adk.desktop.sandbox import Sandbox, SandboxError

    try:
        out = Sandbox().install(version, dry_run=dry_run)
    except SandboxError as exc:
        return _sandbox_error(exc)
    if not dry_run:
        try:
            check = Sandbox().doctor()
        except SandboxError:
            check = None
        if check is not None:
            out["ready"] = bool(check.ok)
            if not check.ok:
                out["next_step"] = out.get("owner_once", "")
    return json.dumps(out)


def desktop_sandbox_detonate(target: str, args: str = "", network: str = "off",
                             duration: int = 0, allow_network: bool = False) -> str:
    """WRITE: run ONE untrusted file in a fresh, disposable Windows Sandbox and report what it
    did: processes with command lines, name lookups, connection attempts, autoruns,
    scheduled tasks, services and files it planted (sandbox-mcp `sbx detonate`).

    target: an existing file on this host (.exe/.msi/.ps1/.cmd/.bat/.js/.vbs/.hta).
    args: optional command-line arguments, one string as the file would receive them.
    network: off (default, no adapter), firewall (outbound blocked but attempts shown)
        or on. "on" is REFUSED unless allow_network=True.
    duration: seconds to watch, 1..3600; 0 uses the upstream default (60).
    allow_network: must be true to pass network="on" for untrusted code.

    Findings are heuristics, not a verdict; exit code 0 means it was observed, not safe.
    The run leaves report.md/report.json/audit.jsonl on the host and the machine is deleted.
    """
    from adk.desktop.sandbox import Sandbox, SandboxError

    try:
        r = Sandbox().detonate(target, args=args, network=network,
                               duration=duration or None, allow_network=allow_network)
    except SandboxError as exc:
        return _sandbox_error(exc)
    return _sandbox_receipt(r)


def desktop_sandbox_run(target: str, args: str = "", test_script: str = "",
                        network: str = "off", allow_network: bool = False) -> str:
    """WRITE: test an app on a brand-new Windows machine: boot, deploy, launch/run, observe,
    optionally run a PowerShell test script, collect evidence, destroy the sandbox,
    and return the report (sandbox-mcp `sbx run`).

    target: an existing .exe/.msix file or a build folder on this host.
    args: optional arguments for the app.
    test_script: optional existing .ps1 to run inside the guest after launch.
    network: off (default), firewall or on; "on" needs allow_network=True.
    allow_network: must be true to give the guest real internet access.
    """
    from adk.desktop.sandbox import Sandbox, SandboxError

    try:
        r = Sandbox().run_app(target, args=args, test_script=test_script,
                              network=network, allow_network=allow_network)
    except SandboxError as exc:
        return _sandbox_error(exc)
    return _sandbox_receipt(r)


def desktop_sandbox_install_test(installer: str, args: str = "",
                                 test_script: str = "") -> str:
    """WRITE: test an installer on a fresh Windows machine: snapshot, install silently,
    snapshot, optionally run a test script, uninstall, snapshot, and report every
    file, registry key, service, scheduled task and autorun it added and what the
    uninstaller left behind (sandbox-mcp `sbx install-test`).

    installer: an existing .msi/.exe/.ps1/.cmd on this host.
    args: optional installer arguments (e.g. "/S").
    test_script: optional existing .ps1 to run after install.

    Findings are heuristics, not a verdict; leftovers are evidence, not a malware score.
    """
    from adk.desktop.sandbox import Sandbox, SandboxError

    try:
        r = Sandbox().install_test(installer, args=args, test_script=test_script)
    except SandboxError as exc:
        return _sandbox_error(exc)
    return _sandbox_receipt(r)


def desktop_sandbox_deps(target: str) -> str:
    """Diagnose an app's runtime dependencies on the HOST without running or
    installing anything (sandbox-mcp `sbx deps`): PE architecture and imports,
    app-local VC++ DLLs, .NET runtime/configs, package dependencies and script
    interpreters. Read-only and offline.

    target: an existing .exe file or a build folder on this host.
    """
    from adk.desktop.sandbox import Sandbox, SandboxError

    try:
        r = Sandbox().deps(target)
    except SandboxError as exc:
        return _sandbox_error(exc)
    return _sandbox_receipt(r)


SANDBOX_TOOLS = [desktop_sandbox_doctor, desktop_sandbox_install,
                 desktop_sandbox_detonate, desktop_sandbox_run,
                 desktop_sandbox_install_test, desktop_sandbox_deps]

#: Sandbox tools that change this machine or execute untrusted code. ``install``
#: plans by default (``dry_run=True``); the rest have no safe planning mode, so
#: the route always requires ``confirm=true`` for them.
_SANDBOX_WRITE_TOOLS = frozenset({
    desktop_sandbox_install, desktop_sandbox_detonate, desktop_sandbox_run,
    desktop_sandbox_install_test,
})


#: (tool, action_class). "" = read-only; "write" = changes the machine and takes
#: dry_run (default True). The daemon's /desk/tools refuses a write call that would
#: act (dry_run false) unless the caller also sends confirm=true.
DESKTOP_TOOLS: List[Tuple[Callable[..., str], str]] = [
    (desktop_list_windows, ""),
    (desktop_arrange, "write"),
    *[(fn, "") for fn in SYSINTERNALS_TOOLS],
    (desktop_processes, ""),
    (desktop_services, ""),
    (desktop_modules, ""),
    (desktop_ping, ""),
    (desktop_procdump, "write"),
    (desktop_file_locks, ""),
    (desktop_workspaces, ""),
    (desktop_workspace_launch, "write"),
    (desktop_installed_apps, ""),
    (desktop_upgradable_apps, ""),
    # Sandbox tools that boot a VM, run untrusted code or write host artifacts are
    # write-class: the daemon refuses them unless the caller sends confirm=true.
    *[(fn, "write" if fn in _SANDBOX_WRITE_TOOLS else "") for fn in SANDBOX_TOOLS],
]


def desktop_tool_table() -> Dict[str, Tuple[Callable[..., str], str]]:
    """name -> (function, action_class) for every desktop tool."""
    return {fn.__name__: (fn, cls) for fn, cls in DESKTOP_TOOLS}
