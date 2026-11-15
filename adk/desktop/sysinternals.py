"""Sysinternals as typed agent tools: one runner, parsed rows, never raw console.

Plan: docs/plans/2026-11-09-powertoys-desktop-engine.md (Sysinternals section).

Every run passes ``-accepteula -nobanner``, has a hard timeout (``handle`` run
non-elevated was measured hanging past 120 s on 2026-11-09), decodes the
console code page with replacement, and returns rows as dicts. Stdlib only,
Python 3.10 compatible.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_DIRS = ("C:/Sysinternals", "C:/Program Files/Sysinternals")

# tcpvcon -c prints no header row (measured 2026-11-09).
TCPVCON_COLUMNS = ("protocol", "process", "pid", "state", "local", "remote")

# pslist's default view: a name (may contain spaces) then seven fixed columns;
# "CPU Time" and "Elapsed Time" are one value each (measured 2026-11-09).
PSLIST_COLUMNS = ("pid", "pri", "thd", "hnd", "priv_kb", "cpu_time", "elapsed_time")
PSSERVICE_STATES = ("active", "inactive", "all")
#: A psping target: host or IPv4/IPv6 literal, optional :port. Never a flag.
_PSPING_TARGET = re.compile(r"^(?!-)[A-Za-z0-9._\[\]:-]{1,255}$")
#: A pslist/listdlls target: an image name or a pid. Never a flag.
_PROCESS_FILTER = re.compile(r"^(?!-)[A-Za-z0-9 ._()+-]{1,128}$")
_SERVICE_NAME = re.compile(r"^(?!-)[A-Za-z0-9 ._$@{}-]{1,256}$")
_MS = re.compile(r"(\d+(?:\.\d+)?)ms")


class SysinternalsError(RuntimeError):
    pass


@dataclass
class RunResult:
    tool: str
    argv: List[str]
    exit_code: Optional[int]
    rows: List[Dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    stderr: str = ""
    #: Tool-specific facts beside the rows (psping's statistics, listdlls' denials).
    extra: Dict[str, Any] = field(default_factory=dict)


Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[bytes]"]


#: A parser turns a tool's text into ``(rows, extra)``.
Parser = Callable[[str], Tuple[List[Dict[str, Any]], Dict[str, Any]]]


def _default_runner(argv: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[bytes]":
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console tab from a daemon
    return subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False,
                          creationflags=flags)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


#: The shared runner seam for every typed Windows tool in adk.desktop (winget,
#: FileLocksmith): no console window, hard timeout, bytes out. Tests pass a fake.
default_runner = _default_runner
decode_output = _decode


def parse_csv(text: str, columns: Optional[Sequence[str]] = None) -> List[Dict[str, str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows = list(reader)
    header = list(columns) if columns else rows.pop(0)
    return [dict(zip(header, r)) for r in rows]


def check_process_filter(value: str) -> str:
    """An image name or pid safe to hand a Sysinternals tool as its target."""
    v = (value or "").strip()
    if not _PROCESS_FILTER.match(v):
        raise SysinternalsError(f"not a process name or pid: {value!r}")
    return v


def parse_pslist(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """pslist's default table -> rows; a "was not found" line -> no rows, ``not_found``."""
    rows: List[Dict[str, Any]] = []
    in_table = False
    for line in text.splitlines():
        if not line.strip():
            continue
        if "was not found" in line:
            return [], {"not_found": line.strip()}
        if line.startswith("Name") and " Pid " in line:
            in_table = True
            continue
        if not in_table:
            continue
        parts = line.rsplit(None, len(PSLIST_COLUMNS))
        if len(parts) != len(PSLIST_COLUMNS) + 1 or not parts[1].isdigit():
            continue
        row: Dict[str, Any] = {"name": parts[0].strip()}
        row.update(zip(PSLIST_COLUMNS, parts[1:]))
        rows.append(row)
    return rows, {}


def parse_psservice(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """psservice query blocks -> one row per service (``state`` is the word: RUNNING)."""
    rows: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("SERVICE_NAME:"):
            cur = {"service_name": line.split(":", 1)[1].strip()}
            rows.append(cur)
            continue
        if cur is None or not line.strip():
            continue
        if line.startswith("DISPLAY_NAME:"):
            cur["display_name"] = line.split(":", 1)[1].strip()
        elif not raw.startswith(("\t", " ")):
            cur.setdefault("description", line.strip())
        elif line.strip().startswith("("):
            cur["controls"] = line.strip().strip("()")
        elif ":" in line:
            key, _, value = line.strip().partition(":")
            k = re.sub(r"\s+", "_", key.strip()).lower()
            v = value.strip()
            if k == "state":
                code, _, word = v.partition(" ")
                cur["state_code"], cur["state"] = code, word.strip()
            elif k in ("win32_exit_code", "service_exit_code"):
                cur[k] = v.split(" ", 1)[0]
            elif k in ("type", "group"):
                cur[k] = v
    return rows, {}


def parse_listdlls(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """listdlls per-process blocks -> one row per loaded module."""
    rows: List[Dict[str, Any]] = []
    process, pid, cmdline = "", "", ""
    denied: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("---"):
            continue
        m = re.match(r"^(.+?) pid: (\d+)$", line)
        if m:
            process, pid, cmdline = m.group(1), m.group(2), ""
            continue
        if line.startswith("Command line:"):
            cmdline = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Error opening"):
            denied.append(line[len("Error opening"):].strip().rstrip(":"))
            continue
        if line.startswith("0x") and process:
            parts = line.split(None, 2)
            if len(parts) == 3:
                rows.append({"process": process, "pid": pid, "base": parts[0],
                             "size": parts[1], "path": parts[2], "command_line": cmdline})
    extra: Dict[str, Any] = {}
    if denied:
        extra["access_denied"] = denied
    return rows, extra


def parse_psping(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """psping ICMP/TCP attempts -> one row per attempt, statistics in ``extra``."""
    rows: List[Dict[str, Any]] = []
    extra: Dict[str, Any] = {}
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith(("Reply from", "Connecting to")):
            ms = _MS.search(line)
            if ms and line.endswith("ms"):
                rows.append({"ok": True, "ms": float(ms.group(1)), "error": ""})
            else:
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                rows.append({"ok": False, "ms": None, "error": nxt or line})
        elif line.startswith(("Request timed out", "Destination host unreachable")):
            rows.append({"ok": False, "ms": None, "error": line})
        elif line.startswith("Sent ="):
            for part in line.split(","):
                k, _, v = part.partition("=")
                v = v.strip().split(" ", 1)[0]
                if v.isdigit():
                    extra[k.strip().lower()] = int(v)
        elif line.startswith("Minimum ="):
            for part in line.split(","):
                k, _, v = part.partition("=")
                ms = _MS.search(v)
                if ms:
                    extra[k.strip().lower() + "_ms"] = float(ms.group(1))
        elif line.startswith(("Invalid destination", "Host not found")):
            extra.setdefault("error", line)
    return rows, extra


def parse_procdump(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """procdump's progress lines -> one row per dump written."""
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        m = re.search(r"Dump (\d+) initiated: (.+)$", line.strip())
        if m:
            rows.append({"dump": int(m.group(1)), "path": m.group(2).strip(),
                         "complete": False})
            continue
        m = re.search(r"Dump (\d+) complete: (.+)$", line.strip())
        if m:
            for r in rows:
                if r["dump"] == int(m.group(1)):
                    r["complete"], r["detail"] = True, m.group(2).strip()
    return rows, {}


def default_dump_dir() -> Path:
    """Where procdump writes unless told otherwise: %LOCALAPPDATA%/Aither/dumps."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Aither" / "dumps"


class Sysinternals:
    def __init__(self, home: Optional[Path] = None, runner: Optional[Runner] = None,
                 timeout: float = 60.0) -> None:
        self.home = Path(home) if home else self._find_home()
        self.runner = runner or _default_runner
        self.timeout = timeout

    @staticmethod
    def _find_home() -> Optional[Path]:
        for d in DEFAULT_DIRS:
            if Path(d, "sigcheck64.exe").is_file() or Path(d, "sigcheck.exe").is_file():
                return Path(d)
        found = shutil.which("sigcheck64") or shutil.which("sigcheck")
        return Path(found).parent if found else None

    def exe(self, tool: str) -> Path:
        if self.home is None:
            raise SysinternalsError("Sysinternals not found (looked in C:/Sysinternals and PATH)")
        for name in (f"{tool}64.exe", f"{tool}.exe"):
            p = self.home / name
            if p.is_file():
                return p
        raise SysinternalsError(f"{tool} not found in {self.home}")

    def argv(self, tool: str, args: Sequence[str]) -> List[str]:
        """The exact command line ``run`` would execute (what a dry run reports)."""
        return [str(self.exe(tool)), "-accepteula", "-nobanner", *args]

    def run(self, tool: str, args: Sequence[str], columns: Optional[Sequence[str]] = None,
            timeout: Optional[float] = None, parser: Optional[Parser] = None) -> RunResult:
        argv = self.argv(tool, args)
        try:
            proc = self.runner(argv, timeout or self.timeout)
        except subprocess.TimeoutExpired:
            return RunResult(tool, argv, None, timed_out=True,
                             stderr=f"timed out after {timeout or self.timeout}s "
                                    "(often: needs elevation)")
        out = _decode(proc.stdout or b"")
        if parser is not None:
            rows, extra = parser(out)
        else:
            rows, extra = parse_csv(out, columns), {}
        return RunResult(tool, argv, proc.returncode, rows,
                         stderr=_decode(proc.stderr or b"").strip(), extra=extra)

    # -- typed tools: one agent question each ------------------------------
    def sigcheck(self, path: str) -> RunResult:
        """Is this binary signed, by whom, which version."""
        return self.run("sigcheck", ["-c", path])

    def ports(self) -> RunResult:
        """Which process owns which TCP/UDP endpoint (numeric addresses)."""
        return self.run("tcpvcon", ["-a", "-c", "-n"], columns=TCPVCON_COLUMNS)

    def disk_usage(self, path: str, depth: int = 0) -> RunResult:
        """Where did the disk go."""
        return self.run("du", ["-c", "-l", str(depth), path])

    def autoruns(self) -> RunResult:
        """What starts at boot/logon, with hashes and signatures (full view needs admin)."""
        return self.run("autorunsc", ["-a", "*", "-c", "-h", "-s"], timeout=300)

    def who_holds(self, name: str) -> RunResult:
        """Which process holds a file/handle matching ``name`` (needs admin)."""
        return self.run("handle", ["-v", name], timeout=30)

    def processes(self, name: str = "") -> RunResult:
        """Running processes: pid, priority, threads, handles, private KB, CPU and age."""
        args = [check_process_filter(name)] if (name or "").strip() else []
        return self.run("pslist", args, timeout=30, parser=parse_pslist)

    def services(self, name: str = "", state: str = "all") -> RunResult:
        """Windows services and their state (psservice query; never start/stop)."""
        st = (state or "all").strip().lower()
        if st not in PSSERVICE_STATES:
            raise SysinternalsError(f"state must be one of {PSSERVICE_STATES}, got {state!r}")
        n = (name or "").strip()
        if n and not _SERVICE_NAME.match(n):
            raise SysinternalsError(f"not a service name: {name!r}")
        args = ["query", n] if n else ["query", "-s", st]
        return self.run("psservice", args, timeout=60, parser=parse_psservice)

    def modules(self, process: str, unsigned_only: bool = False) -> RunResult:
        """DLLs one process has loaded (listdlls).

        A target is REQUIRED: listdlls with none, or with ``-d``, walks every process
        and was measured hanging past 120 s non-elevated on 2026-11-09.
        """
        target = check_process_filter(process)
        args = (["-u"] if unsigned_only else []) + [target]
        return self.run("listdlls", args, timeout=60, parser=parse_listdlls)

    def ping(self, target: str, count: int = 4) -> RunResult:
        """ICMP ping, or TCP connect latency when ``target`` is host:port (psping).

        ``-t`` (ping until stopped) is never passed; ``count`` is clamped to 1..20.
        """
        t = (target or "").strip()
        if not _PSPING_TARGET.match(t):
            raise SysinternalsError(f"not a host or host:port: {target!r}")
        n = max(1, min(int(count), 20))
        return self.run("psping", ["-n", str(n), "-w", "0", t], timeout=10 + 3 * n,
                        parser=parse_psping)

    def dump_argv(self, pid: int, full: bool = False,
                  dump_dir: Optional[Path] = None) -> List[str]:
        """The procdump command line for ONE immediate dump of ``pid`` (no triggers)."""
        p = int(pid)
        if p <= 4:
            raise SysinternalsError(f"refusing to dump pid {p} (System/Idle)")
        folder = Path(dump_dir) if dump_dir else default_dump_dir()
        return self.argv("procdump", ["-ma" if full else "-mp", str(p), str(folder)])

    def dump(self, pid: int, full: bool = False, dump_dir: Optional[Path] = None) -> RunResult:
        """WRITE: one immediate dump of ``pid`` into ``dump_dir`` (procdump -mp, or -ma).

        Never passes a trigger (-e/-c/-m/-t), install (-i) or uninstall (-u) flag.
        """
        argv = self.dump_argv(pid, full, dump_dir)
        Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        return self.run("procdump", argv[3:], timeout=180, parser=parse_procdump)
