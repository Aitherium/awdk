"""The Bonsai installers stop ONLY the llama-server they started.

``install-bonsai.ps1`` ran ``Get-Process llama-server | Stop-Process -Force`` on every
install, binary refresh and uninstall: it killed every llama-server on the machine --
the owner's own model, another app's, a dev box's fleet. Measured 2026-10-01 on the
build host: a first-timer install would have killed an unrelated llama-server holding
:8080 and then reported success from THAT server's /health.

These tests drive the REAL functions extracted from the shipped installers (so a fix
that never reaches the installer cannot pass) against two fake servers: one running
out of the installer's ``bin`` directory, one elsewhere with the same process name.
Only the first may die. The fakes carry a per-run UNIQUE process name that is
substituted into the extracted functions, so even a regressed stop-by-name kills only
this test's fakes and never a real llama-server on the machine running the tests (a
mutation run of the first draft, which used the real name, did exactly that). They
skip when the installers are not in this checkout (the standalone awdk repo) or the
shell they need is missing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PUBLIC = Path(__file__).resolve().parents[2] / "AitherOS" / "apps" / "AitherVeil" / "public"
PS1 = PUBLIC / "install-bonsai.ps1"
SH = PUBLIC / "install-bonsai.sh"

needs_installers = pytest.mark.skipif(not (PS1.is_file() and SH.is_file()),
                                      reason="installers live in the monorepo only")


def _function(text: str, header: str) -> str:
    """The function starting with ``header`` at column 0, through its column-0 ``}``."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(header)), None)
    if start is None:
        pytest.fail(f"installer no longer defines {header!r} at column 0")
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == "}"), None)
    if end is None:
        pytest.fail(f"{header!r} has no closing brace at column 0")
    return "\n".join(lines[start:end + 1]) + "\n"


def _unique_name() -> str:
    return f"llama-server-t{os.getpid()}x{int(time.time() * 1000) % 100000}"


def _wait_dead(proc: subprocess.Popen, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return False


# ── static: no stop-by-name survives anywhere ────────────────────────────────

@needs_installers
def test_ps1_never_stops_llama_server_by_name():
    text = PS1.read_text(encoding="utf-8-sig")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"Get-Process\s+llama-server[^\n]*\|\s*Stop-Process", code)
    assert "Stop-OwnServer" in code and "-PassThru" in code and "$PidFile" in code


@needs_installers
def test_sh_never_pkills_by_bare_name():
    text = SH.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for m in re.finditer(r"pkill[^\n]*", code):
        assert "$BIN/llama-server" in m.group(0), m.group(0)
    assert "killall" not in code
    assert "SERVER_PID=$!" in code and '"$PIDFILE"' in code


@needs_installers
def test_both_installers_refuse_a_port_they_do_not_own():
    assert "Get-PortOwner $Port" in PS1.read_text(encoding="utf-8-sig")
    assert 'port_busy "$PORT"' in SH.read_text(encoding="utf-8")


# ── live: two fake servers, only ours dies ───────────────────────────────────

@needs_installers
@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                    reason="Windows PowerShell installer")
def test_ps1_stop_kills_only_the_server_under_its_bin(tmp_path):
    ping = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE"
    if not ping.is_file():
        pytest.skip("no ping.exe to stand in for llama-server")
    name = _unique_name()
    text = PS1.read_text(encoding="utf-8-sig")
    funcs = (_function(text, "function Get-OwnServerIds")
             + _function(text, "function Stop-OwnServer"))
    assert "Name='llama-server.exe'" in funcs
    funcs = funcs.replace("llama-server", name)
    ours_dir, theirs_dir = tmp_path / "bonsai" / "bin", tmp_path / "theirs"
    ours_dir.mkdir(parents=True)
    theirs_dir.mkdir()
    shutil.copy(ping, ours_dir / f"{name}.exe")
    shutil.copy(ping, theirs_dir / f"{name}.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ours = subprocess.Popen([str(ours_dir / f"{name}.exe"), "-n", "120", "127.0.0.1"],
                            stdout=subprocess.DEVNULL, creationflags=flags)
    theirs = subprocess.Popen([str(theirs_dir / f"{name}.exe"), "-n", "120", "127.0.0.1"],
                              stdout=subprocess.DEVNULL, creationflags=flags)
    try:
        pid_file = tmp_path / "bonsai" / "server.pid"
        # A recorded pid that names THEIR process must not be honoured either.
        pid_file.write_text(f"{theirs.pid} 8080\n", encoding="utf-8")
        script = tmp_path / "stop.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\nSet-StrictMode -Version Latest\n"
            f"$BinDir = '{ours_dir}'\n$PidFile = '{pid_file}'\n"
            "function Say ($m) { Write-Host \"  $m\" }\n"
            + funcs + "Stop-OwnServer\n", encoding="utf-8-sig")
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-File", str(script)], capture_output=True, text=True,
                             timeout=120, creationflags=flags)
        assert out.returncode == 0, out.stdout + out.stderr
        assert _wait_dead(ours), "the server under bin\\ survived"
        assert theirs.poll() is None, "a llama-server the installer did not start was killed"
        assert not pid_file.exists()
        assert f"pid {ours.pid}" in out.stdout
    finally:
        for p in (ours, theirs):
            if p.poll() is None:
                p.kill()


@needs_installers
@pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh")
                    or not shutil.which("pkill"), reason="POSIX installer")
def test_sh_stop_kills_only_the_server_under_its_bin(tmp_path):
    name = _unique_name()
    text = SH.read_text(encoding="utf-8")
    funcs = _function(text, "_is_ours()") + _function(text, "stop_server()")
    assert "$BIN/llama-server" in funcs
    funcs = funcs.replace("llama-server", name)
    root = tmp_path / "bonsai"
    ours_bin, theirs_bin = root / "bin", tmp_path / "theirs"
    for d in (ours_bin, theirs_bin):
        d.mkdir(parents=True)
        # A loop, not `exec sleep`: exec would drop the path from the cmdline.
        (d / name).write_text("#!/bin/sh\nwhile :; do sleep 1; done\n", encoding="utf-8")
    ours = subprocess.Popen(["sh", str(ours_bin / name)])
    theirs = subprocess.Popen(["sh", str(theirs_bin / name)])
    try:
        time.sleep(0.3)
        pid_file = root / "server.pid"
        pid_file.write_text(f"{theirs.pid} 8080\n", encoding="utf-8")
        script = f'BIN="{ours_bin}"\nPIDFILE="{pid_file}"\n' + funcs + "stop_server\n"
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert _wait_dead(ours), "the server under bin/ survived"
        assert theirs.poll() is None, "a llama-server the installer did not start was killed"
        assert not pid_file.exists()
    finally:
        for p in (ours, theirs):
            if p.poll() is None:
                p.kill()


# ── -Uninstall must get past the legacy scheduled task that is not there ─────

@needs_installers
def test_ps1_has_no_native_stderr_redirect_outside_a_try():
    """Under ``$ErrorActionPreference = 'Stop'`` Windows PowerShell 5.1 makes a native
    command's redirected stderr a terminating error. Measured 2026-10-01: ``-Uninstall``
    died on ``schtasks /Delete ... 2>$null`` (no such task = every current install),
    after removing the Startup shortcut and before stopping the server or its files."""
    text = PS1.read_text(encoding="utf-8-sig")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"^\s*schtasks\b[^\n]*2>", code, re.M)
    assert "Remove-LegacyTask" in code.split("if ($Uninstall)", 1)[1].split("exit 0", 1)[0]


@needs_installers
@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                    reason="Windows PowerShell installer")
def test_ps1_legacy_task_removal_survives_a_missing_task(tmp_path):
    text = PS1.read_text(encoding="utf-8-sig")
    fn = _function(text, "function Remove-LegacyTask")
    script = tmp_path / "legacy.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\nSet-StrictMode -Version Latest\n"
        f"$TaskName = 'Aitherium Bonsai test {os.getpid()} no such task'\n"
        + fn + "Remove-LegacyTask\nWrite-Host 'REACHED'\nexit 0\n", encoding="utf-8-sig")
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(script)], capture_output=True, text=True, timeout=120,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0 and "REACHED" in out.stdout, out.stdout + out.stderr


@needs_installers
@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                    reason="Windows PowerShell installer")
def test_ps1_stop_returns_only_once_the_files_can_be_deleted(tmp_path):
    """``-Uninstall`` deletes the install right after stopping the server. Measured
    2026-10-01: Stop-Process had returned, the server still held its DLLs, and the
    delete died on access-denied with every file left in place."""
    ping = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE"
    if not ping.is_file():
        pytest.skip("no ping.exe to stand in for llama-server")
    name = _unique_name()
    text = PS1.read_text(encoding="utf-8-sig")
    funcs = (_function(text, "function Get-OwnServerIds")
             + _function(text, "function Stop-OwnServer")).replace("llama-server", name)
    # The stand-in exits too fast to hold its files the way a server with a mapped model
    # does, so the wait itself is asserted; the run below proves the sequence still works.
    assert "Wait-Process -Id $id" in funcs
    root = tmp_path / "bonsai"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(ping, bin_dir / f"{name}.exe")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ours = subprocess.Popen([str(bin_dir / f"{name}.exe"), "-n", "120", "127.0.0.1"],
                            stdout=subprocess.DEVNULL, creationflags=flags)
    try:
        script = tmp_path / "uninstall.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\nSet-StrictMode -Version Latest\n"
            f"$BinDir = '{bin_dir}'\n$PidFile = '{root / 'server.pid'}'\n$Root = '{root}'\n"
            "function Say ($m) { Write-Host \"  $m\" }\n" + funcs
            + "Stop-OwnServer\nRemove-Item $Root -Recurse -Force\nWrite-Host 'GONE'\n",
            encoding="utf-8-sig")
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-File", str(script)], capture_output=True, text=True,
                             timeout=120, creationflags=flags)
        assert out.returncode == 0 and "GONE" in out.stdout, out.stdout + out.stderr
        assert not root.exists()
    finally:
        if ours.poll() is None:
            ours.kill()


@needs_installers
@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                    reason="Windows PowerShell installer")
def test_ps1_uninstall_outlasts_a_briefly_locked_file(tmp_path):
    """Measured 2026-10-01: the stopped server's ggml-base.dll stayed locked for a moment
    after the process had exited, and a single Remove-Item left the install half-deleted."""
    text = PS1.read_text(encoding="utf-8-sig")
    fn = _function(text, "function Remove-InstallDir")
    assert "Remove-InstallDir" in text.split("if ($Uninstall)", 1)[1].split("exit 0", 1)[0]
    root = tmp_path / "bonsai"
    (root / "bin").mkdir(parents=True)
    locked = root / "bin" / "ggml-base.dll"
    locked.write_bytes(b"x")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Another process keeps the file open (no delete sharing) for two seconds.
    holder = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         f"$f = [IO.File]::Open('{locked}', 'Open', 'Read', 'None'); 'held'; "
         "Start-Sleep -Seconds 2; $f.Close()"],
        stdout=subprocess.PIPE, text=True, creationflags=flags)
    try:
        assert holder.stdout.readline().strip() == "held"
        script = tmp_path / "rm.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\nSet-StrictMode -Version Latest\n"
            f"$Root = '{root}'\n" + fn + "Remove-InstallDir\nWrite-Host 'GONE'\n",
            encoding="utf-8-sig")
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-File", str(script)], capture_output=True, text=True,
                             timeout=120, creationflags=flags)
        assert out.returncode == 0 and "GONE" in out.stdout, out.stdout + out.stderr
        assert not root.exists()
    finally:
        if holder.poll() is None:
            holder.kill()
