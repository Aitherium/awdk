"""adk mesh lend -- lend this machine's spare RAM to a pooled model over the mesh.

One command, no manual steps, on a signed-in machine that is on the mesh:

    adk mesh lend                 # 50% of free RAM (always leaving >= 4 GB free)
    adk mesh lend --gb 24         # an explicit amount
    adk mesh lend --status        # what is lent, is the server alive
    adk mesh lend --stop          # stop lending and remove the logon hook

What it does, in order:

1. Refuses unless ``adk.mesh.overlay_status`` says this device is joined to the mesh.
2. Picks the ggml RPC server build for this OS/arch, downloads it into
   ``~/.aither/lend/`` and checks every file against the sha256 pinned below.
   A mismatch deletes the file and stops: nothing unverified ever runs.
3. Starts it detached (it outlives this terminal and the agent process that ran
   this command) bound to the tailnet address, or to 127.0.0.1 when tailscale runs
   in userspace mode (tailscaled forwards tailnet connections to loopback there).
   On x86_64/arm64 Linux the fast build is tried first and the baseline build is
   used when the fast one dies on an illegal instruction.
4. Makes it come back after a reboot: a logon Scheduled Task (or, without the
   rights for one, a per-user Run entry) on Windows; a ``systemd --user`` unit on
   Linux when systemd is there.
5. Writes ``~/.aither/lend/state.json`` and announces the offer -- tailnet address,
   port and the MEASURED lend size -- to the pool registry
   (``POST {registry}/pools/{pool}/backends``, the route ``adk grid join
   --register`` uses). The size is announced because a static RPC server reports
   free == total memory, so the coordinator cannot learn the real number from it.

The RPC server is unauthenticated, so it is only ever bound to a tailnet address
or loopback, never a LAN or public interface.

Exit codes: 0 lending (and announced, or no registry configured); 1 lending but
the registry refused or could not be reached; 2 could not lend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("adk.mesh_lend")

#: The published builds. Changing a byte of any of them means a new release tag
#: and new pins here; the download is verified against these and nothing else.
RELEASE = "v41-lend-rpc-20261022"
RELEASE_BASE = ("https://github.com/Aitherium/aitherkvcache/releases/download/"
                + RELEASE + "/")

DEFAULT_PORT = 50052
RESERVE_MB = 4096          # always left free for the owner of the machine
MIN_LEND_MB = 1024         # below this the offer is not worth a pool slot
DEFAULT_POOL_MODEL = "deepseek-v4.1-flash-1017"
TASK_NAME = "AwdkMeshLend"
RUN_VALUE = "AwdkMeshLend"
UNIT_NAME = "awdk-mesh-lend.service"
FIREWALL_RULE = "awdk mesh lend"
UA = "awdk-mesh-lend/1 (+https://aitherium.com)"

#: Windows: a child that never opens a console window and is not tied to ours.
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
#: Exit status of a process killed by an illegal instruction.
_SIGILL_CODES = frozenset({-4, 132, 0xC000001D, -1073741795})


class LendError(RuntimeError):
    """A refusal with a message meant for the person at the terminal."""


@dataclass(frozen=True)
class Asset:
    name: str        # release asset name
    size: int
    sha256: str
    save_as: str     # file name in the install directory


@dataclass(frozen=True)
class Variant:
    key: str
    files: tuple[Asset, ...]
    exe: str         # save_as of the executable

    @property
    def exe_sha256(self) -> str:
        return next(a.sha256 for a in self.files if a.save_as == self.exe)


#: Every published asset of RELEASE: name -> (size, sha256). Nothing outside this table is
#: ever downloaded, and a test pins that every variant file is in it and vice versa.
PINS: dict[str, tuple[int, str]] = {
    "ggml-rpc-server-windows-x64-v3.exe":
        (2313216, "4ed5b4b32bc7ad1bb469ae1fa9eef8b3e88fed1cb5d252913b69ac812462a656"),
    "ggml-rpc-server-windows-x64-v2.exe":
        (1821184, "bbf2c83a6f876d721ece1b0c7248e3a49befac2b495332b692989268464584a5"),
    "libc++.dll":
        (2002944, "9779d03d9bdb4bed763a5b18d0aa5b9002ce5dad6d0adc2d496e783a3f336e3f"),
    "libunwind.dll":
        (185856, "5de310284e361c8d10a92e0a18f8b17d2d5a0de1d091d9e3c0b92d86f9f6c7c2"),
    "libwinpthread-1.dll":
        (280576, "f50ee7828d41ce02eb8bf3ec1d59dedfd66126a753782ae2d7489a56d84f7816"),
    "ggml-rpc-server-linux-x86_64-znver2":
        (2383304, "3c196b71a3cefb738320801afc567874344b12c8dc4f86d219c068b0c95b4daf"),
    "ggml-rpc-server-linux-x86_64-v2":
        (2052280, "0fa1510b89c71412e82e53e9ec85c43dfa20cc04ab26d83b56c1940dec4b64a9"),
    "ggml-rpc-server-linux-arm64-v82":
        (1868104, "bf2605dc6f9488f2186cb227ee7ccba712308e1b04b399a6c03d8699d4aa551d"),
    "ggml-rpc-server-linux-arm64-v8a":
        (1847888, "413c18aa23ae280e2e897daad93f5ba4a0c2726fb24388fdb06b5c01cf42b0f6"),
}


def _asset(name: str, save_as: str) -> Asset:
    size, sha = PINS[name]
    return Asset(name, size, sha, save_as)


_WIN_DLLS = tuple(_asset(n, n) for n in ("libc++.dll", "libunwind.dll", "libwinpthread-1.dll"))


def _one(key: str, name: str, save_as: str) -> Variant:
    return Variant(key, (_asset(name, save_as),), save_as)


#: (os, arch) -> builds, fastest first. The next one is tried when the first fails its
#: ``-h`` probe or dies on an illegal instruction.
VARIANTS: dict[tuple[str, str], tuple[Variant, ...]] = {
    ("windows", "x86_64"): (
        Variant("windows-x64-v3",
                (_asset("ggml-rpc-server-windows-x64-v3.exe", "ggml-rpc-server-v3.exe"),)
                + _WIN_DLLS, "ggml-rpc-server-v3.exe"),
        Variant("windows-x64-v2",
                (_asset("ggml-rpc-server-windows-x64-v2.exe", "ggml-rpc-server-v2.exe"),)
                + _WIN_DLLS, "ggml-rpc-server-v2.exe"),
    ),
    ("linux", "x86_64"): (
        _one("linux-x86_64-znver2", "ggml-rpc-server-linux-x86_64-znver2",
             "ggml-rpc-server-znver2"),
        _one("linux-x86_64-v2", "ggml-rpc-server-linux-x86_64-v2",
             "ggml-rpc-server-x86-64-v2"),
    ),
    ("linux", "aarch64"): (
        _one("linux-arm64-v8.2", "ggml-rpc-server-linux-arm64-v82",
             "ggml-rpc-server-arm64-v82"),
        _one("linux-arm64-v8a", "ggml-rpc-server-linux-arm64-v8a",
             "ggml-rpc-server-arm64-v8a"),
    ),
}

_ARCH = {"amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64",
         "arm64": "aarch64", "aarch64": "aarch64"}


# ── pure pieces ─────────────────────────────────────────────────────────────


def lend_dir() -> Path:
    return Path(os.environ.get("AITHER_LEND_DIR") or Path.home() / ".aither" / "lend")


def state_path() -> Path:
    return lend_dir() / "state.json"


def select_variants(system: Optional[str] = None,
                    machine: Optional[str] = None) -> tuple[Variant, ...]:
    """The builds for this OS/arch, fastest first. Raises LendError when unsupported."""
    osname = (system or platform.system()).strip().lower()
    arch = _ARCH.get((machine or platform.machine()).strip().lower(), "")
    found = VARIANTS.get((osname, arch))
    if not found:
        raise LendError(f"no RPC server build for {osname or '?'}/{machine or '?'}; "
                        "supported: Windows x64, Linux x86_64, Linux arm64")
    return found


def compute_lend_mb(free_mb: int, gb: Optional[float] = None) -> int:
    """How much to lend. Default: half the free RAM, never leaving < RESERVE_MB free.

    An explicit ``gb`` is honoured exactly or refused -- it is never silently cut.
    """
    if free_mb <= 0:
        raise LendError("could not measure free RAM; refusing to lend an unmeasured amount")
    room = free_mb - RESERVE_MB
    if gb is not None:
        want = int(float(gb) * 1024)
        if want <= 0:
            raise LendError("--gb must be positive")
        if want > room:
            raise LendError(f"--gb {gb} would leave {free_mb - want} MB free; "
                            f"at most {max(0, room) // 1024} GB can be lent "
                            f"(free {free_mb} MB, {RESERVE_MB} MB kept)")
        lend = want
    else:
        lend = min(free_mb // 2, room)
    if lend < MIN_LEND_MB:
        raise LendError(f"only {max(0, lend)} MB could be lent (free {free_mb} MB, "
                        f"{RESERVE_MB} MB kept); the minimum offer is {MIN_LEND_MB} MB")
    return int(lend)


def bind_host(overlay: dict) -> str:
    """Where the server listens: the tailnet address, or loopback in userspace mode."""
    if overlay.get("tun") is False:
        return "127.0.0.1"
    ip = str(overlay.get("tailnet_ip") or "")
    if not ip:
        raise LendError("no tailnet address to bind to")
    return ip


def server_argv(exe: Path | str, host: str, port: int, threads: int) -> list[str]:
    """The same flags the platform's own Windows tier is started with."""
    return [str(exe), "-H", host, "-p", str(int(port)), "-d", "CPU", "-t", str(int(threads))]


def default_threads() -> int:
    return max(1, (os.cpu_count() or 2) // 2)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_ok(path: Path, asset: Asset) -> bool:
    try:
        return path.stat().st_size == asset.size and sha256_file(path) == asset.sha256
    except OSError:
        return False


# ── download ────────────────────────────────────────────────────────────────


def _http_get(url: str, dest: Path, timeout: float = 60.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)


def fetch_variant(variant: Variant, dest_dir: Path,
                  fetch: Callable[[str, Path], None] = _http_get) -> Path:
    """Download every file of ``variant`` into ``dest_dir``, verified. Returns the exe.

    A file already present with the pinned digest is kept. A download whose size or
    sha256 is not the pin is deleted and the whole lend stops (fail closed).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    for asset in variant.files:
        final = dest_dir / asset.save_as
        if file_ok(final, asset):
            continue
        part = dest_dir / (asset.save_as + ".part")
        url = RELEASE_BASE + urllib.request.quote(asset.name)
        try:
            fetch(url, part)
        except Exception as exc:  # noqa: BLE001 - any transport failure is a refusal
            part.unlink(missing_ok=True)
            raise LendError(f"download of {asset.name} failed: {exc}") from exc
        if not file_ok(part, asset):
            got = sha256_file(part) if part.exists() else "missing"
            part.unlink(missing_ok=True)
            raise LendError(f"sha256 mismatch for {asset.name}: expected {asset.sha256}, "
                            f"got {got}; refusing to run it")
        os.replace(part, final)
        if os.name != "nt":
            final.chmod(0o755)
    return dest_dir / variant.exe


# ── measurement ─────────────────────────────────────────────────────────────


def free_ram_mb() -> int:
    """Available physical memory in MB, measured by the OS (0 when unmeasurable)."""
    if os.name == "nt":
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return int(st.ullAvailPhys // (1024 * 1024))
        return 0
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError) as exc:
        logger.debug("mesh lend: best-effort step failed: %s", exc)
    return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))  # type: ignore[attr-defined]
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _listening(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── process + persistence ───────────────────────────────────────────────────


def _run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    """A short helper child: never a console window on Windows."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL,
                          creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)


def spawn_detached(argv: list[str], log_path: Path,
                   popen: Callable[..., Any] = subprocess.Popen,
                   windows: Optional[bool] = None) -> Any:
    """Start ``argv`` so it outlives this process and holds none of its pipes.

    The agent's /admin/cli/exec runs this command with captured output; a child that
    inherited those pipes would keep that call open until the server exited.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log,
                              "stderr": subprocess.STDOUT, "close_fds": True,
                              "cwd": str(Path(argv[0]).parent)}
    if (os.name == "nt") if windows is None else windows:
        kwargs["creationflags"] = CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        return popen(argv, **kwargs)
    finally:
        log.close()


def _relaunch_argv(args: argparse.Namespace) -> list[str]:
    """What the logon hook runs: this same command, windowless, so it re-verifies the
    binary, re-checks the mesh and re-announces after every sign-in."""
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = str(pyw if os.name == "nt" and pyw.exists() else py)
    out = [exe, "-m", "adk.cli", "mesh", "lend", "--port", str(args.port)]
    if args.gb is not None:
        out += ["--gb", str(args.gb)]
    for flag in ("pool", "registry_url", "coordinator"):
        val = getattr(args, flag, "") or ""
        if val:
            out += ["--" + flag.replace("_", "-"), val]
    return out


def _cmdline(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv)


def _unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT_NAME


def install_persistence(args: argparse.Namespace, server: list[str],
                        run: Callable[..., Any] = _run) -> str:
    """Bring the lend back after a reboot. Returns how: schtasks|run-key|systemd|none."""
    if os.name == "nt":
        relaunch = _cmdline(_relaunch_argv(args))
        try:
            r = run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
                     "/TN", TASK_NAME, "/TR", relaunch])
            if r.returncode == 0:
                return "schtasks"
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mesh lend: best-effort step failed: %s", exc)
        try:  # a logon task needs rights a standard user may lack; the Run key does not
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, relaunch)
            return "run-key"
        except OSError:
            return "none"
    if not shutil.which("systemctl"):
        return "none"
    unit_dir = _unit_path().parent
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / UNIT_NAME).write_text(
            "[Unit]\nDescription=awdk mesh lend (ggml RPC server)\n"
            "After=network-online.target\n\n"
            f"[Service]\nExecStart={_cmdline(server)}\nRestart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        r = run(["systemctl", "--user", "daemon-reload"])
        if r.returncode != 0:
            return "none"
        r = run(["systemctl", "--user", "enable", UNIT_NAME])
        if r.returncode != 0:
            return "none"
    except (OSError, subprocess.SubprocessError):
        return "none"
    try:  # survive logout; best effort, a desktop session usually has it already
        run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("mesh lend: best-effort step failed: %s", exc)
    return "systemd"


def remove_persistence(run: Callable[..., Any] = _run) -> list[str]:
    removed: list[str] = []
    if os.name == "nt":
        try:
            if run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME]).returncode == 0:
                removed.append("schtasks")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mesh lend: best-effort step failed: %s", exc)
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
            removed.append("run-key")
        except OSError as exc:
            logger.debug("mesh lend: best-effort step failed: %s", exc)
        try:
            run(["netsh", "advfirewall", "firewall", "delete", "rule",
                 f"name={FIREWALL_RULE}"])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mesh lend: best-effort step failed: %s", exc)
        return removed
    if shutil.which("systemctl"):
        try:
            run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mesh lend: best-effort step failed: %s", exc)
    unit = _unit_path()
    if unit.exists():
        unit.unlink()
        removed.append("systemd")
    return removed


def _allow_firewall(exe: Path, port: int, run: Callable[..., Any] = _run) -> bool:
    """Windows: allow inbound to the server from the tailnet range only. Needs admin;
    without it the server still runs and the status says the rule is missing."""
    if os.name != "nt":
        return True
    try:
        run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"])
        r = run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}",
                 "dir=in", "action=allow", f"program={exe}", "protocol=TCP",
                 f"localport={int(port)}", "remoteip=100.64.0.0/10"])
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe_binary(exe: Path,
                 run: Optional[Callable[..., Any]] = None) -> tuple[bool, str]:
    """Does ``exe -h`` run on this CPU and print the RPC server's usage?"""
    try:
        r = (run or _run)([str(exe), "-h"], timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"-h probe could not run ({exc.__class__.__name__})"
    if r.returncode in _SIGILL_CODES:
        return False, "illegal instruction (CPU lacks this build's ISA level)"
    if "--device" not in (r.stdout or "") + (r.stderr or ""):
        return False, f"-h probe exited {r.returncode} without the usage text"
    return True, ""


def start_server(variants: tuple[Variant, ...], dest: Path, host: str, port: int,
                 threads: int, *, fetch: Callable[[str, Path], None] = _http_get,
                 popen: Callable[..., Any] = subprocess.Popen,
                 settle_s: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep,
                 probe: Optional[Callable[[Path], tuple[bool, str]]] = None,
                 ) -> tuple[Variant, Any, list[str]]:
    """Download + start the fastest build that runs here. Returns (variant, proc, argv).

    A build is skipped for the next one when its ``-h`` probe fails or it dies on an
    illegal instruction at start (a CPU without the build's ISA level).
    """
    last = ""
    check = probe or probe_binary
    for variant in variants:
        exe = fetch_variant(variant, dest, fetch=fetch)
        ok, why = check(exe)
        if not ok:
            last = f"{variant.key}: {why}"
            continue
        argv = server_argv(exe, host, port, threads)
        proc = spawn_detached(argv, dest / "rpc-server.log", popen=popen)
        sleep(settle_s)
        code = proc.poll()
        if code is None:
            return variant, proc, argv
        if code in _SIGILL_CODES:
            last = f"{variant.key} needs CPU features this machine lacks"
            continue
        raise LendError(f"{variant.key} exited with {code} at start; see "
                        f"{dest / 'rpc-server.log'}")
    raise LendError(f"no build runs on this CPU ({last})")


# ── announce ────────────────────────────────────────────────────────────────


def announce(state: dict, *, pool: str, registry_url: str, coordinator: str,
             send: Optional[Callable[..., tuple[int, str]]] = None,
             bearer: Optional[str] = None) -> tuple[int, str]:
    """Record the offer in the pool registry. Returns (exit code, one-line verdict).

    Uses the registry route ``adk grid join --register`` uses, with an rpc-cpu
    backend whose budget is the measured lend size and whose address is the
    tailnet address (what the coordinator dials). The registry re-validates it and
    the coordinator's adoption gate re-measures it; this records, it adopts nothing.
    """
    if not registry_url or not pool:
        return 0, ("not announced: no pool registry configured (--registry-url/--pool or "
                   "ADK_POOL_REGISTRY_URL/ADK_LEND_POOL)")
    from adk import pool_role

    spec = pool_role.resolve_model(DEFAULT_POOL_MODEL)
    rtt = None
    if coordinator:
        host, _, port = coordinator.rpartition(":")
        if host and port.isdigit():
            rtt = pool_role.measure_rtt_ms(host, int(port))
    lend_gib = round(state["lend_mb"] / 1024, 2)
    per_block = (spec.block_gib * spec.rpc_cpu_rss_factor) if spec else 1.0
    caps = pool_role.NodeCaps(os=platform.system().lower(), ram_total_gib=0.0,
                              ram_available_gib=lend_gib, physical_cores=os.cpu_count() or 1,
                              logical_cores=os.cpu_count() or 1,
                              rtt_ms=None if rtt is None else round(rtt, 3))
    backend = pool_role.Backend(
        role="rpc-cpu", blocks=int(lend_gib // per_block), budget_gib=lend_gib,
        command=server_argv(state.get("exe") or "ggml-rpc-server", state["ip"],
                            state["port"], state.get("threads") or 1),
        note=f"adk mesh lend: {state['lend_mb']} MB measured free RAM")
    admission = ("SPLIT_ELIGIBLE" if rtt is not None and rtt <= pool_role.MAX_SPLIT_RTT_MS
                 else "REPLICA_ONLY")
    plan = pool_role.RolePlan(
        model=DEFAULT_POOL_MODEL, spec_measured=bool(spec and spec.measured),
        spec_source=spec.source if spec else "", admission=admission,
        reasons=[] if rtt is not None else ["coordinator RTT not measured"],
        backends=[backend], tuning=[], caps=caps)
    code = pool_role.register_plan(plan, pool, registry_url,
                                   node_id=socket.gethostname(), bearer=bearer, send=send)
    if code == 0:
        return 0, f"announced to pool {pool}: {state['ip']}:{state['port']} {state['lend_mb']} MB"
    return 1, f"lending, but the pool registry did not record it (exit {code}; reason above)"


# ── state ───────────────────────────────────────────────────────────────────


def read_state() -> Optional[dict]:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def write_state(state: dict) -> Path:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def make_state(pid: int, port: int, ip: str, bind: str, lend_mb: int, variant: Variant,
               exe: Path, threads: int, persist: str) -> dict:
    return {"pid": int(pid), "port": int(port), "ip": ip, "bind": bind,
            "lend_mb": int(lend_mb), "binary_sha": variant.exe_sha256,
            "variant": variant.key, "exe": str(exe), "threads": int(threads),
            "persist": persist, "release": RELEASE, "started_at": int(time.time())}


# ── commands ────────────────────────────────────────────────────────────────


def _overlay() -> dict:
    from adk.mesh import overlay_status

    return overlay_status()


def cmd_status(args: argparse.Namespace) -> int:
    st = read_state()
    if not st:
        out: dict[str, Any] = {"lending": False}
    else:
        alive = _pid_alive(int(st.get("pid") or 0))
        out = dict(st, lending=alive,
                   listening=alive and _listening(st.get("bind") or st.get("ip") or "",
                                                  int(st.get("port") or 0)))
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2, sort_keys=True))
    elif not out["lending"]:
        print("not lending" + (" (state file present, server not running)" if st else ""))
    else:
        print(f"lending {out['lend_mb']} MB on {out['ip']}:{out['port']} "
              f"(pid {out['pid']}, {out['variant']}, "
              f"{'listening' if out['listening'] else 'NOT listening'}, "
              f"after reboot: {out.get('persist')})")
    return 0 if (not st or out["lending"]) else 1


def cmd_stop(args: argparse.Namespace, run: Callable[..., Any] = _run) -> int:
    st = read_state() or {}
    pid = int(st.get("pid") or 0)
    stopped = False
    if pid and _pid_alive(pid):
        try:
            if os.name == "nt":
                run(["taskkill", "/PID", str(pid), "/T", "/F"])
            else:
                os.kill(pid, signal.SIGTERM)
            stopped = True
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"could not stop pid {pid}: {exc}", file=sys.stderr)
            return 2
    removed = remove_persistence(run=run)
    state_path().unlink(missing_ok=True)
    print(f"stopped{' pid ' + str(pid) if stopped else ' (nothing running)'}; "
          f"removed: {', '.join(removed) or 'no logon hook'}")
    return 0


def cmd_lend(args: argparse.Namespace, *, overlay: Optional[dict] = None,
             fetch: Callable[[str, Path], None] = _http_get,
             popen: Callable[..., Any] = subprocess.Popen,
             run: Callable[..., Any] = _run,
             free_mb: Optional[int] = None,
             send: Optional[Callable[..., tuple[int, str]]] = None,
             sleep: Callable[[float], None] = time.sleep) -> int:
    if getattr(args, "status", False):
        return cmd_status(args)
    if getattr(args, "stop", False):
        return cmd_stop(args, run=run)
    ov = overlay if overlay is not None else _overlay()
    if not ov.get("joined"):
        print(f"not on the mesh: {ov.get('detail') or ov.get('code') or 'unknown'}. "
              "Run: adk mesh join", file=sys.stderr)
        return 2
    try:
        variants = select_variants()
        host = bind_host(ov)
        ip = str(ov.get("tailnet_ip") or "")
        prior = read_state()
        if prior and _pid_alive(int(prior.get("pid") or 0)) and prior.get("port") == args.port:
            print(f"already lending {prior['lend_mb']} MB on {prior['ip']}:{prior['port']} "
                  f"(pid {prior['pid']})")
            state = prior
        else:
            lend_mb = compute_lend_mb(free_ram_mb() if free_mb is None else free_mb, args.gb)
            threads = int(getattr(args, "threads", 0) or default_threads())
            variant, proc, argv = start_server(variants, lend_dir(), host, args.port,
                                               threads, fetch=fetch, popen=popen, sleep=sleep,
                                               probe=lambda exe: probe_binary(exe, run=run))
            exe = Path(argv[0])
            persist = install_persistence(args, argv, run=run)
            if persist == "systemd":
                # hand the running server to systemd so there is one owner of it
                try:
                    proc.terminate()
                    run(["systemctl", "--user", "start", UNIT_NAME])
                    r = run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                             UNIT_NAME])
                    pid = int((r.stdout or "0").strip() or 0)
                except (OSError, subprocess.SubprocessError, ValueError):
                    pid = 0
                if pid <= 0:
                    raise LendError("systemd did not start the server; see "
                                    f"journalctl --user -u {UNIT_NAME}")
            else:
                pid = int(proc.pid)
            if not _allow_firewall(exe, args.port, run=run):
                print("note: no inbound firewall rule (needs an elevated shell once); "
                      "the coordinator may not reach this machine", file=sys.stderr)
            state = make_state(pid, args.port, ip, host, lend_mb, variant, exe, threads,
                               persist)
            write_state(state)
            print(f"lending {lend_mb} MB on {ip}:{args.port} (pid {pid}, {variant.key}, "
                  f"bound {host}, after reboot: {persist})")
    except LendError as exc:
        print(f"could not lend: {exc}", file=sys.stderr)
        return 2
    code, verdict = announce(
        state,
        pool=getattr(args, "pool", "") or os.environ.get("ADK_LEND_POOL", ""),
        registry_url=(getattr(args, "registry_url", "")
                      or os.environ.get("ADK_POOL_REGISTRY_URL", "")),
        coordinator=(getattr(args, "coordinator", "")
                     or os.environ.get("ADK_POOL_COORDINATOR", "")),
        send=send)
    print(verdict, file=sys.stderr if code else sys.stdout)
    return code


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gb", type=float, default=None,
                   help=f"GB to lend (default: half the free RAM, keeping "
                        f"{RESERVE_MB // 1024} GB free)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"RPC server port (default {DEFAULT_PORT})")
    p.add_argument("--threads", type=int, default=0,
                   help="CPU threads for the server (default: half the logical cores)")
    p.add_argument("--status", action="store_true", help="show what is lent and stop there")
    p.add_argument("--stop", action="store_true",
                   help="stop lending and remove the logon task / user unit")
    p.add_argument("--json", action="store_true", help="--status as JSON")
    p.add_argument("--pool", default="", help="pool id to announce to (default $ADK_LEND_POOL)")
    p.add_argument("--registry-url", default="",
                   help="pool registry base URL (default $ADK_POOL_REGISTRY_URL)")
    p.add_argument("--coordinator", default="",
                   help="coordinator host:port, for the RTT the registry gates on "
                        "(default $ADK_POOL_COORDINATOR)")
