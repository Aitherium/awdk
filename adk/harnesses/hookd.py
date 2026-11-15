"""hookd: run Claude Code command hooks in long-lived workers, reached over `type: http`.

Every Windows process creation leaks kernel pool until reboot (microsoft/WSL#41296, open;
measured here 2026-10-19: ~2.5 File + ~24 FMfn per spawn). A command hook costs bash +
the interpreter + conhost per call, and one Bash tool call fired ~55 processes. An http
hook spawns nothing: Claude Code POSTs the event JSON here, a prefork worker runs each
Python hook in-process with that JSON on stdin, and the results merge into one response
with command-hook semantics (exit 2 blocks, JSON stdout is passed through, plain stdout
becomes context on UserPromptSubmit). One exception: an exit 2 that SAYS NOTHING outside
PreToolUse is a failed hook, not a decision. It is logged and does not block. Measured
2026-10-26: the folded awsettings push hook exited 2 silently while the settings hub
answered 503, and every PostToolUse in every session showed "blocked by hook".

Stdlib only, runnable straight from the tree (`adk` imports from a deploy snapshot):

    pythonw awdk/adk/harnesses/hookd.py serve [--port 8369] [--workers 6]
    python  awdk/adk/harnesses/hookd.py health
    python  awdk/adk/harnesses/hookd.py ensure      # start detached when not answering
    python  awdk/adk/harnesses/hookd.py --self-test

settings.json entry (one per event group, hooks listed as `h=` in order):

    {"type": "http", "timeout": 30,
     "url": "http://127.0.0.1:8369/run?h=.claude/hooks/a.py&h=.claude/hooks/b.py|--stop",
     "headers": {"X-Hookd": "1", "X-Project-Dir": "$CLAUDE_PROJECT_DIR"},
     "allowedEnvVars": ["CLAUDE_PROJECT_DIR"]}

`h=<path>|<arg>|<arg>`: a relative path resolves against X-Project-Dir. Only files under
`allowed_roots()` run. `h=~<path>|...` marks a hook folded from a command that ended in
`|| true`: any exit it makes is reported as 0, as the shell did. The required X-Hookd
header forces a CORS preflight, which this server never answers, so a web page cannot
drive it.
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import multiprocessing as mp
import os
import queue
import re
import runpy
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("AITHER_HOOKD_PORT", "8369"))
HOME = Path.home()
STATE_DIR = HOME / ".aither" / "hookd"
ROOTS_FILE = STATE_DIR / "roots.txt"  # one extra allowed directory per line
MODULES_FILE = STATE_DIR / "modules.txt"  # top-level packages `ep:`/`-m:` may run
MAX_JOBS = 500  # a worker is recycled after this many jobs (module state, memory)
#: A worker is also recycled after a job that left it holding more than this much private
#: memory (GB; env AITHER_HOOKD_MAX_WORKER_GB, 0 disables). Measured 2026-10-31: three idle
#: workers held 5.4 + 5.1 + 3.2 GB (the pool is ~1.3 GB at rest) - a hook imported something
#: heavy once and the worker kept it for its next 500 jobs, while a CC5 load next to it was
#: killed at 96% host commit. A fresh worker costs ~40 MB and ~0.3 s.
MAX_WORKER_GB = float(os.environ.get("AITHER_HOOKD_MAX_WORKER_GB", "0.75") or 0)
#: Spec prefix for a hook folded from `<cmd> || true`. Dropping the suffix without keeping
#: its promise turned a fail-soft hook into one that blocks: `awsettings --quiet push
#: --debounce || true` folded to a hook that exits 2, silently, whenever the hub is down.
SOFT = "~"


def allowed_roots(project_dir: str) -> list:
    """~/.claude/hooks, <project>/.claude/hooks (the nearest ancestor that has one, see
    `_find_up`), and each line of ROOTS_FILE."""
    roots = [HOME / ".claude" / "hooks"]
    if project_dir:
        start = Path(project_dir)
        roots.append(next((d / ".claude" / "hooks" for d in (start, *start.resolve().parents)
                           if (d / ".claude" / "hooks").is_dir()), start / ".claude" / "hooks"))
    try:
        roots += [Path(x.strip()) for x in ROOTS_FILE.read_text(encoding="utf-8").splitlines()
                  if x.strip() and not x.startswith("#")]
    except OSError as exc:
        _log("roots file unreadable, defaults only: %s" % exc)
    return roots
DEFAULT_TIMEOUT = 30.0
CREATE_NO_WINDOW = 0x08000000

#: Network guard, installed in every worker (`_install_net_guard`). A hook's TCP connect
#: waits at most NET_CONNECT_S; on every event except UNCAPPED_EVENTS each socket read waits
#: at most NET_READ_S, whatever timeout the hook asked for. An endpoint that refused, timed
#: out a connect or timed out a read is written to DOWN_FILE for DOWN_TTL_S, and every hook
#: in every worker that dials it in that window fails at once with ConnectionRefusedError
#: (an OSError; urllib wraps it in URLError) instead of waiting out its own timeout. When
#: the services a hook calls are down, a 0 ms hook stays 0 ms and a network hook costs one
#: refused connect per minute, not one timeout per prompt.
NET_CONNECT_S = float(os.environ.get("AITHER_HOOKD_CONNECT_S", "2.0"))
NET_READ_S = float(os.environ.get("AITHER_HOOKD_NET_S", "4.0"))
DOWN_TTL_S = float(os.environ.get("AITHER_HOOKD_DOWN_TTL_S", "60"))
DOWN_FILE = Path(os.environ.get("AITHER_HOOKD_DOWN_FILE") or STATE_DIR / "endpoints-down.json")
#: Stop hooks carry LLM-sized budgets by design (a summary call can take 20 s); their reads
#: keep the hook's own timeout. Connect cap and down-marker still apply to them.
UNCAPPED_EVENTS = ("Stop", "SubagentStop")
#: How long a worker may take to start before it is killed and retried. A worker is put in
#: the idle queue only after it says it is ready, so its start-up never counts against a hook.
SPAWN_READY_S = 60.0


# ---------------------------------------------------------------- network guard

_NET: dict = {"event": "", "peers": {}, "installed": False}


def _down_load() -> dict:
    """`host:port` -> epoch until which it is skipped; expired entries dropped."""
    try:
        data = json.loads(DOWN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > now} \
        if isinstance(data, dict) else {}


def endpoint_down(host: str, port: int) -> float:
    """The epoch until which `host:port` is marked unreachable, or 0.0."""
    return float(_down_load().get("%s:%s" % (host, port), 0.0))


def mark_down(host: str, port: int, why: object) -> None:
    """Skip `host:port` in every worker for DOWN_TTL_S. Atomic replace; a lost race only
    drops one mark, which the next failure writes again."""
    if DOWN_TTL_S <= 0:
        return
    key = "%s:%s" % (host, port)
    data = _down_load()
    fresh = key not in data
    data[key] = time.time() + DOWN_TTL_S
    tmp = DOWN_FILE.with_name("%s.%d.tmp" % (DOWN_FILE.name, os.getpid()))
    try:
        DOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, DOWN_FILE)
    except OSError as exc:
        _log("down-marker write failed for %s: %s" % (key, exc))
        return
    if fresh:
        _log("endpoint %s unreachable (%s): hooks skip it for %.0fs" % (key, why, DOWN_TTL_S))


def _read_cap(asked):
    """The read timeout a hook's socket gets: its own, capped unless the event is exempt."""
    if _NET["event"] in UNCAPPED_EVENTS or NET_READ_S <= 0:
        return asked
    return NET_READ_S if asked is None else min(asked, NET_READ_S)


def _install_net_guard() -> None:
    """Wrap `socket.create_connection` (urllib, http.client and httpx's sync transport all
    dial through it) and the recv methods of plain and TLS sockets. Worker-only: it changes
    process-wide socket behaviour."""
    import ssl

    if _NET["installed"]:
        return
    _NET["installed"] = True
    orig_connect = socket.create_connection
    sentinel = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT", object())

    def create_connection(address, timeout=sentinel, source_address=None, *a, **kw):
        host, port = address[0], address[1]
        until = endpoint_down(host, port)
        if until:
            raise ConnectionRefusedError(
                111, "hookd: %s:%s marked unreachable for %.0fs more; skipped"
                % (host, port, until - time.time()))
        asked = socket.getdefaulttimeout() if timeout is sentinel else timeout
        connect_t = NET_CONNECT_S if asked is None else min(asked, NET_CONNECT_S)
        try:
            sock = orig_connect(address, connect_t, source_address, *a, **kw)
        except OSError as exc:
            mark_down(host, port, "connect: %s" % (exc.__class__.__name__,))
            raise
        sock.settimeout(_read_cap(asked))
        _NET["peers"][sock.fileno()] = (host, port)
        return sock

    def wrap(cls, name: str) -> None:
        orig = getattr(cls, name)

        def recv(self, *a, **kw):
            try:
                return orig(self, *a, **kw)
            except socket.timeout:
                try:
                    peer = _NET["peers"].get(self.fileno())
                except OSError:
                    peer = None
                if peer:
                    mark_down(peer[0], peer[1], "read timed out")
                raise

        recv.__name__ = name
        setattr(cls, name, recv)

    socket.create_connection = create_connection
    for cls in (socket.socket, ssl.SSLSocket):
        for name in ("recv", "recv_into"):
            wrap(cls, name)


# ---------------------------------------------------------------- worker side


def _run_one(job: dict) -> dict:
    """Execute one hook script as __main__ with the event JSON on stdin."""
    t0 = time.monotonic()
    saved_env = dict(os.environ)
    saved_cwd = os.getcwd()
    saved = (sys.stdin, sys.stdout, sys.stderr, list(sys.argv), list(sys.path))
    out_b, err_b = io.BytesIO(), io.BytesIO()
    out = io.TextIOWrapper(out_b, encoding="utf-8", errors="replace", write_through=True)
    err = io.TextIOWrapper(err_b, encoding="utf-8", errors="replace", write_through=True)
    code = 0
    _NET["event"] = job.get("event") or ""
    try:
        os.environ.update(job.get("env") or {})
        if job.get("cwd") and os.path.isdir(job["cwd"]):
            os.chdir(job["cwd"])
        sys.stdin = io.TextIOWrapper(io.BytesIO(job["stdin"]), encoding="utf-8")
        sys.stdout, sys.stderr = out, err
        sys.argv = [job["path"], *job.get("args", [])]
        target = job["path"]
        if target.startswith("ep:"):  # a console script: sys.exit(<module>:<func>())
            mod, _, fn = target[3:].partition(":")
            sys.argv[0] = mod.split(".")[0]
            rc = getattr(importlib.import_module(mod), fn)()
            code = rc if isinstance(rc, int) else 0
        elif target.startswith("-m:"):
            sys.argv[0] = target[3:]
            runpy.run_module(target[3:], run_name="__main__", alter_sys=True)
        else:
            sys.path.insert(0, os.path.dirname(target))
            runpy.run_path(target, run_name="__main__")
    except SystemExit as exc:
        c = exc.code
        if c is None:
            code = 0
        elif isinstance(c, int):
            code = c
        else:
            err.write(str(c))
            code = 1
    except BaseException:  # a hook's own crash is that hook's non-blocking error
        err.write(traceback.format_exc())
        code = 1
    finally:
        for f in (out, err):
            try:
                f.flush()
            except ValueError:
                continue  # the hook closed its own stream; what it wrote is in the buffer
        sys.stdin, sys.stdout, sys.stderr, sys.argv, sys.path[:] = (
            saved[0], saved[1], saved[2], saved[3], saved[4])
        os.environ.clear()
        os.environ.update(saved_env)
        _NET["peers"].clear()
        _NET["event"] = ""
        try:
            os.chdir(saved_cwd)
        except OSError as exc:
            _log("cwd restore failed: %s" % exc)
    return {"code": code, "out": out_b.getvalue().decode("utf-8", "replace"),
            "err": err_b.getvalue().decode("utf-8", "replace"),
            "ms": int((time.monotonic() - t0) * 1000)}


_JOB_TIMERS: list = []


class _TrackedTimer(threading.Timer):
    """A hook's watchdog Timer, cancelled when its job ends instead of firing later."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        _JOB_TIMERS.append(self)


def _contain_exit() -> None:
    """Hooks arm `Timer(cap, lambda: os._exit(0))` as a hang guard. In a one-shot process
    that is harmless; in a long-lived worker it fires after the job and kills the worker
    (measured: record-owner-reply.py, prompt-recall.py). os._exit from the job's own
    thread ends that job; from any other thread it is ignored (the pool's timeout guards
    hangs), and every Timer a job armed is cancelled when the job returns."""
    main = threading.main_thread()

    def _exit(code: int = 0) -> None:
        if threading.current_thread() is main:
            raise SystemExit(code)

    os._exit = _exit
    threading.Timer = _TrackedTimer


def _worker_main(conn) -> None:
    _contain_exit()
    _install_net_guard()
    conn.send({"ready": os.getpid()})  # only now does the pool hand this worker a job
    while True:
        try:
            job = conn.recv()
        except (EOFError, OSError):
            return
        if job is None:
            return
        res = _run_one(job)
        while _JOB_TIMERS:
            _JOB_TIMERS.pop().cancel()
        conn.send(res)


# ---------------------------------------------------------------- pool side


def _private_gb(pid: int):
    """A process's private memory (commit charge) in GB, or None when it cannot be read."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t),
                            ("PrivateUsage", ctypes.c_size_t)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED | VM_READ
            if not h:
                return None
            try:
                c = PMC()
                c.cb = ctypes.sizeof(PMC)
                gpmi = ctypes.WinDLL("psapi").GetProcessMemoryInfo
                gpmi.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
                return c.PrivateUsage / 2**30 if gpmi(h, ctypes.byref(c), c.cb) else None
            finally:
                k32.CloseHandle(h)
        with open("/proc/%d/statm" % pid, encoding="ascii") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**30
    except (OSError, ValueError, AttributeError):
        return None


class _Worker:
    def __init__(self, ctx) -> None:
        self.parent, child = ctx.Pipe()
        self.proc = ctx.Process(target=_worker_main, args=(child,), daemon=True)
        self.proc.start()
        child.close()
        self.jobs = 0
        self.last = ""  # the job before a death is the suspect, not the one that found it


class Pool:
    """Prefork workers; a hung or dead worker is replaced, never waited on twice.

    Four properties keep a slow hook from starving the rest (measured 2026-11-09: after a
    burst of worker deaths, every hook, including ones that do no I/O at all, returned
    124 for seven hours, and then the daemon stopped answering, until it was restarted):

    * a request's `timeout` is ONE deadline covering the wait for a worker AND the run, so
      a request always returns within it. Before, `idle.get()` waited forever once every
      worker was busy or lost, and the timeout clock only started after that.
    * replacements start on the refill thread and join the idle queue only after they say
      they are ready. Before, the request thread spawned the replacement inline, and a
      spawn that raised lost that worker slot for good. A worker that has not finished
      starting never gets a job, so its start-up is never charged to a hook.
    * while a request waits and every worker is busy, up to `burst` extra workers start;
      they retire once nothing is waiting.
    * workers carry the network guard, so a hook dialling a dead service gives its worker
      back in at most a few seconds instead of holding it for the hook's whole budget.
    """

    def __init__(self, size: int, burst: "int | None" = None) -> None:
        self.ctx = mp.get_context("spawn")
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        if os.name == "nt" and pyw.exists():
            self.ctx.set_executable(str(pyw))  # a console python would open a window
        self.size = size
        self.max_size = size + (max(2, size // 2) if burst is None else max(0, burst))
        self.idle: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.live = 0  # started or starting, not yet retired or lost
        self.waiting = 0
        self.spawned = 0
        self._wake = threading.Event()
        with self.lock:
            self.live = size
        starting = [self._start() for _ in range(size)]
        for w in starting:
            self._admit(w)
        threading.Thread(target=self._refill, name="hookd-refill", daemon=True).start()

    # -- worker lifecycle (refill thread, plus the initial fill)

    def _start(self) -> "_Worker | None":
        try:
            w = _Worker(self.ctx)
        except Exception as exc:  # noqa: BLE001 -- a failed spawn must not lose the slot
            _log("worker spawn failed: %s" % exc)
            return None
        self.spawned += 1
        return w

    def _admit(self, w: "_Worker | None") -> bool:
        """Wait for `w` to report ready, then make it idle. False (slot freed) otherwise."""
        try:
            ready = w is not None and w.parent.poll(SPAWN_READY_S) and "ready" in (
                w.parent.recv() or {})
        except (EOFError, OSError):
            ready = False
        if ready:
            self.idle.put(w)
            return True
        if w is not None:
            _log("worker %s not ready after %.0fs (exit %s); retrying"
                 % (w.proc.pid, SPAWN_READY_S, w.proc.exitcode))
            self._kill(w)
        self._lost()
        return False

    def _need(self) -> bool:
        with self.lock:
            want = self.live < self.size or (
                self.waiting > 0 and self.idle.empty() and self.live < self.max_size)
            if want:
                self.live += 1
            return want

    def _refill(self) -> None:
        backoff = 0.5
        while True:
            self._wake.wait(1.0)
            self._wake.clear()
            while self._need():
                if self._admit(self._start()):
                    backoff = 0.5
                else:
                    time.sleep(backoff)  # a host that cannot spawn: do not spin
                    backoff = min(backoff * 2, 30.0)
                    break

    def _lost(self) -> None:
        with self.lock:
            self.live -= 1
        self._wake.set()

    @staticmethod
    def _kill(w: "_Worker") -> None:
        try:
            w.proc.kill()
        except OSError as exc:
            _log("worker %s kill failed (already gone?): %s" % (w.proc.pid, exc))

    def _retire(self, w: "_Worker") -> None:
        try:
            w.parent.send(None)  # the worker returns; the refill thread replaces it
        except OSError:
            self._kill(w)
        self._lost()

    def _release(self, w: "_Worker") -> None:
        with self.lock:
            surplus = self.live > self.size and self.waiting == 0
        if surplus:
            self._retire(w)  # a burst worker, no longer needed
        else:
            self.idle.put(w)

    def _acquire(self, deadline: float) -> "_Worker | None":
        with self.lock:
            self.waiting += 1
        try:
            while True:
                try:
                    w = self.idle.get_nowait()
                except queue.Empty:
                    self._wake.set()  # every worker busy: the refill thread may burst
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    try:
                        w = self.idle.get(timeout=left)
                    except queue.Empty:
                        return None
                if w.proc.is_alive():
                    return w
                _log("worker %d dead (exit %s) after %s" % (w.proc.pid, w.proc.exitcode, w.last))
                self._lost()
        finally:
            with self.lock:
                self.waiting -= 1

    def run(self, job: dict, timeout: float) -> dict:
        t0 = time.monotonic()
        deadline = t0 + timeout
        w = self._acquire(deadline)
        if w is None:
            with self.lock:
                busy = "%d live, %d waiting" % (self.live, self.waiting)
            return {"code": 124, "out": "", "ms": int(timeout * 1000),
                    "err": "hookd: no free worker within %.0fs (%s)" % (timeout, busy)}
        w.last = os.path.basename(job["path"])
        try:
            w.parent.send(job)
            if w.parent.poll(max(0.0, deadline - time.monotonic())):
                res = w.parent.recv()
                w.jobs += 1
                gb = _private_gb(w.proc.pid) if MAX_WORKER_GB > 0 else None
                if w.jobs >= MAX_JOBS or (gb is not None and gb > MAX_WORKER_GB):
                    if gb is not None and gb > MAX_WORKER_GB:
                        _log("worker %d recycled holding %.2f GB after %s (cap %.2f)"
                             % (w.proc.pid, gb, w.last, MAX_WORKER_GB))
                    self._retire(w)
                else:
                    self._release(w)
                return res
            reason = "timed out after %.0fs" % timeout
        except (EOFError, OSError, BrokenPipeError) as exc:
            reason = "worker died: %s" % exc
        self._kill(w)
        self._lost()
        return {"code": 124, "out": "", "err": "hookd: %s" % reason,
                "ms": int((time.monotonic() - t0) * 1000)}


# ---------------------------------------------------------------- semantics


def allowed_modules() -> set:
    try:
        return {x.strip() for x in MODULES_FILE.read_text(encoding="utf-8").splitlines()
                if x.strip() and not x.startswith("#")}
    except OSError:
        return set()


def parse_specs(query: str, project_dir: str) -> list:
    """`h=path|arg|arg` entries -> [(target, [args], soft)], refusing anything not allowlisted.

    A target is a script path, `ep:<module>:<func>` (a console script's entry point) or
    `-m:<module>`; a module's top-level package must be listed in MODULES_FILE. `soft` is
    the SOFT prefix: the hook was folded from `<cmd> || true`."""
    specs = []
    for raw in urllib.parse.parse_qs(query, keep_blank_values=False).get("h", []):
        soft = raw.startswith(SOFT)
        parts = (raw[len(SOFT):] if soft else raw).split("|")
        if parts[0].startswith(("ep:", "-m:")):
            pkg = parts[0][3:].split(":")[0].split(".")[0]
            if pkg not in allowed_modules():
                raise PermissionError("module not allowlisted: %s" % pkg)
            specs.append((parts[0], parts[1:], soft))
            continue
        p = Path(parts[0].replace("$HOME", str(HOME)))
        if not p.is_absolute():
            p = _find_up(Path(project_dir or "."), p)
        p = p.resolve()
        if not any(_under(p, r) for r in allowed_roots(project_dir)):
            raise PermissionError("hook outside allowed roots: %s" % p)
        specs.append((str(p), parts[1:], soft))
    return specs


_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(?=/|$)")


def _native(path: str) -> str:
    """`/c/x` -> `C:/x` on Windows. Git Bash hands Claude Code its cwd in that form, and
    `Path("/c/x")` there is `C:\\c\\x`: no `.claude/hooks` above it, so every hook 403s."""
    if os.name == "nt":
        return _MSYS_DRIVE.sub(lambda m: m.group(1).upper() + ":", path)
    return path


def project_candidates(header: str, cwd: str) -> list:
    """X-Project-Dir first, then the payload cwd; uninterpolated `$...` and blanks dropped."""
    out = []
    for d in (header, cwd):
        d = _native(d or "")
        if d and not d.startswith("$") and d not in out:
            out.append(d)
    return out


def _find_up(start: Path, rel: Path) -> Path:
    """`rel` under `start` or its nearest ancestor that has it. Claude Code sets
    CLAUDE_PROJECT_DIR to the session's CURRENT directory, so a session that moved into a
    subdirectory would otherwise miss every `.claude/hooks/...` hook of the repo above it."""
    for d in (start, *start.resolve().parents):
        if (d / rel).is_file():
            return d / rel
    return start / rel


def _under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def silent_exit2(event: str, r: dict) -> bool:
    """Exit 2 with nothing on stdout or stderr, on an event where 2 would BLOCK.

    That is a hook that failed (an offline sync, a crash path with no message), not a
    hook that decided anything: there is no reason to show, so the block would read
    "blocked by hook" on every tool call. PreToolUse stays fail-closed: there exit 2 is
    a deny, and a gate that cannot say why still denies."""
    return (r.get("code") == 2 and event != "PreToolUse"
            and not str(r.get("out") or "").strip() and not str(r.get("err") or "").strip())


def soften(r: dict, soft: bool) -> dict:
    """A hook folded from `<cmd> || true` succeeds whatever it exits; keep the real code."""
    if soft and r.get("code") not in (0, None):
        return dict(r, code=0, soft_code=r["code"])
    return r


def merge(event: str, results: list) -> dict:
    """Fold command-hook results into one http-hook response (same JSON schema)."""
    resp: dict = {}
    hso: dict = {}
    ctx, reasons, sysmsg = [], [], []
    perm_rank = {"allow": 1, "ask": 2, "deny": 3}
    perm, perm_reason = None, None

    def set_perm(d: str, why: str) -> None:
        nonlocal perm, perm_reason
        if perm is None or perm_rank.get(d, 0) > perm_rank.get(perm, 0):
            perm, perm_reason = d, why

    for r in results:
        out, err, code = r["out"].strip(), r["err"].strip(), r["code"]
        if silent_exit2(event, r):
            continue  # a failed hook, not a decision: non-blocking (the handler logs it)
        if code == 2:
            why = err or out or "blocked by hook"
            if event == "PreToolUse":
                set_perm("deny", why)
            else:
                reasons.append(why)
            continue
        if code != 0:
            continue  # non-blocking error: the command semantics show it to nobody
        data = None
        if out.startswith("{"):
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        if data is None:
            if out and event in ("UserPromptSubmit", "SessionStart"):
                ctx.append(out)
            continue
        if data.get("decision") == "block":
            reasons.append(data.get("reason") or "blocked by hook")
        if data.get("continue") is False:
            resp["continue"] = False
            if data.get("stopReason"):
                resp["stopReason"] = data["stopReason"]
        if data.get("systemMessage"):
            sysmsg.append(data["systemMessage"])
        if data.get("suppressOutput"):
            resp["suppressOutput"] = True
        h = data.get("hookSpecificOutput") or {}
        if h.get("additionalContext"):
            ctx.append(h["additionalContext"])
        if h.get("permissionDecision"):
            set_perm(h["permissionDecision"], h.get("permissionDecisionReason") or "")
        for k, v in h.items():
            if k not in ("additionalContext", "permissionDecision",
                         "permissionDecisionReason", "hookEventName"):
                hso[k] = v
    if reasons:
        resp["decision"] = "block"
        resp["reason"] = "\n\n".join(reasons)
    if sysmsg:
        resp["systemMessage"] = "\n".join(sysmsg)
    if ctx:
        hso["additionalContext"] = "\n\n".join(ctx)
    if perm:
        hso["permissionDecision"] = perm
        hso["permissionDecisionReason"] = perm_reason or ""
    if hso:
        hso["hookEventName"] = event
        resp["hookSpecificOutput"] = hso
    return resp


# ---------------------------------------------------------------- http


class _Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.time()
        self.requests = 0
        self.hooks = 0
        self.errors = 0


def _log(line: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "hookd.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line + "\n")
    except OSError as exc:  # a full or locked disk must not fail the hook it describes
        if sys.__stderr__:
            sys.__stderr__.write("hookd log: %s\n" % exc)


def make_handler(pool: Pool, stats: _Stats):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # silence the default stderr access log
            return

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path.startswith("/health"):
                with stats.lock:
                    self._send(200, {"ok": True, "pid": os.getpid(),
                                     "uptime_s": int(time.time() - stats.started),
                                     "requests": stats.requests, "hooks": stats.hooks,
                                     "errors": stats.errors, "workers_spawned": pool.spawned})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/run" or self.headers.get("X-Hookd") != "1":
                self._send(403, {"error": "forbidden"})
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                event_json = json.loads(body or b"{}")
            except ValueError:
                event_json = {}
            event = event_json.get("hook_event_name", "")
            candidates = project_candidates(self.headers.get("X-Project-Dir") or "",
                                            event_json.get("cwd") or "")
            project, specs, refused = (candidates or [""])[0], None, None
            # The header is the session's CURRENT dir; when that refuses (a cd out of the
            # repo), the payload cwd gets its own chance before the request is a 403.
            for cand in candidates or [""]:
                try:
                    specs = parse_specs(url.query, cand)
                    project = cand
                    break
                except PermissionError as exc:
                    refused = refused or exc
            if specs is None:
                self._send(403, {"error": str(refused)})
                return
            env = {"CLAUDE_PROJECT_DIR": project}
            # The payload is authoritative: a header interpolates Claude's env, which a
            # nested `claude -p` inherits from its parent session (measured 2026-10-20).
            sid = event_json.get("session_id") or ""
            if sid:
                env["CLAUDE_CODE_SESSION_ID"] = sid
            timeout = float(urllib.parse.parse_qs(url.query).get("t", [DEFAULT_TIMEOUT])[0])
            results: list = [None] * len(specs)

            def go(i: int, path: str, args: list, soft: bool) -> None:
                try:
                    res = pool.run({"path": path, "args": args, "stdin": body,
                                    "cwd": project, "env": env, "event": event}, timeout)
                except Exception as exc:  # noqa: BLE001 -- the request must still answer
                    res = {"code": 1, "out": "", "err": "hookd: pool error: %r" % exc, "ms": 0}
                results[i] = soften(res, soft)

            threads = [threading.Thread(target=go, args=(i, p, a, s), daemon=True)
                       for i, (p, a, s) in enumerate(specs)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            resp = merge(event, results)
            if urllib.parse.parse_qs(url.query).get("raw") == ["1"]:
                resp["_raw"] = results  # per-hook code/out/err for the equivalence check
            with stats.lock:
                stats.requests += 1
                stats.hooks += len(specs)
                stats.errors += sum(1 for r in results if r["code"] not in (0, 2)
                                    or r.get("soft_code") or silent_exit2(event, r))
            _log("%s %s %s" % (event, sid[:8], " ".join(
                "%s=%d/%dms" % (Path(p).name, r.get("soft_code", r["code"]), r["ms"])
                for (p, _, _), r in zip(specs, results))))
            for (p, _, _), r in zip(specs, results):
                if r["code"] not in (0, 2):
                    _log("  %s exit %d: %s" % (Path(p).name, r["code"], r["err"][-600:]))
                elif r.get("soft_code"):
                    _log("  %s exit %d reported as 0 (folded from `|| true`): %s"
                         % (Path(p).name, r["soft_code"], r["err"][-300:]))
                elif silent_exit2(event, r):
                    _log("  %s exit 2 with no reason on %s: a failed hook, not a block"
                         % (Path(p).name, event))
            self._send(200, resp)

    return H


# A pool sized for one agent starves when many share a host: the daemon keeps hookd
# alive with `ensure(PORT, 6)`, so six workers served twelve subagents and every
# queued Bash hook waited out its full 600 s budget (measured 2026-10-26: ~165
# cancellations per agent, 11 min per turn, 7 h for zero lanes to finish).
# serve() enforces the floor so every caller gets it, whatever --workers says.
_MIN_WORKERS = 24


class _ExclusiveServer(ThreadingHTTPServer):
    """A listener no second hookd can share.

    ``HTTPServer`` sets SO_REUSEADDR, which on Windows lets ANOTHER process bind the
    same port while this one listens. Measured 2026-11-08: two hookd daemons (one from
    a deploy snapshot, one from the worktree, started 13 s apart by two ``ensure``
    callers) both listened on :8369, each with its 24-worker pool: 48 spawn workers,
    2.6 GB. Exclusive use makes the second bind FAIL, so a duplicate exits instead.
    """

    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if os.name == "nt" and excl is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
        super().server_bind()


def _bind(port: int) -> "_ExclusiveServer | None":
    """The bound, listening server, or None when another process owns the port."""
    try:
        return _ExclusiveServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    except OSError as exc:
        _log("port %d is already owned (%s): not starting a second hookd" % (port, exc))
        return None


def serve(port: int, workers: int) -> int:
    workers = max(workers, _MIN_WORKERS)
    # Bind BEFORE the pool starts. Spawning 24 workers takes seconds on a loaded host;
    # with the port still free in that window, a second `ensure` saw nothing there and
    # started another daemon. Bound first, the port is held (connections wait in the
    # backlog) and a late `ensure` sees "bound, not answering yet" and spawns nothing.
    srv = _bind(port)
    if srv is None:
        return 1
    pool = Pool(workers)
    stats = _Stats()
    srv.RequestHandlerClass = make_handler(pool, stats)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "hookd.pid").write_text(str(os.getpid()))
    _log("serving on 127.0.0.1:%d with %d workers (pid %d)" % (port, workers, os.getpid()))
    srv.serve_forever()
    return 0


def health(port: int, timeout: float = 2.0):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def _port_bound(port: int, timeout: float = 0.5) -> bool:
    """True when something holds the port, whether or not it answers /health."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class _EnsureLock:
    """Exclusive, bounded lock so concurrent SessionStarts cannot herd.

    MEASURED 2026-10-26: twelve hookd daemons accumulated on :8369. Every session
    runs `ensure`; health() timed out at 2s while the daemon was saturated, and
    Windows SO_REUSEADDR let each new daemon bind the SAME port anyway. Requests
    then landed on shadow listeners and UserPromptSubmit hooks died at 70s
    ("output discarded"), which is what throttled every agent on this box.
    """

    def __init__(self, path: Path, wait: float = 10.0):
        self.path = path
        self.wait = wait
        self.handle = None

    def __enter__(self) -> "_EnsureLock":
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.path, "a+")
            deadline = time.monotonic() + self.wait
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        break  # proceed unlocked rather than fail the session
                    time.sleep(0.2)
        except OSError as exc:
            _log("lock: %s" % exc)
        return self

    def __exit__(self, *_exc) -> bool:
        try:
            if self.handle:
                if os.name == "nt":
                    import msvcrt
                    try:
                        self.handle.seek(0)
                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError as exc:
                        _log("unlock: %s" % exc)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()
        except OSError as exc:
            _log("lock close: %s" % exc)
        return False


def ensure(port: int, workers: int) -> int:
    """Start hookd detached and console-less unless it already answers. 0 up, 1 failed.

    A BOUND PORT THAT DOES NOT ANSWER /health IS A WEDGED DAEMON, NOT A MISSING ONE.
    Spawning another cannot help (it cannot own the socket) and on Windows it
    creates a shadow listener that steals requests. So: bound + no health = report
    failure and leave it alone; the restart is a deliberate act.
    """
    if health(port):
        return 0
    if _port_bound(port):
        _log("port %d is bound but /health did not answer; refusing to spawn a duplicate "
             "daemon (2026-10-26: 12 accumulated this way)" % port)
        return 1
    with _EnsureLock(STATE_DIR / "hookd.ensure.lock"):
        if health(port):
            return 0
        if _port_bound(port):
            return 1
        import subprocess

        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        argv = [str(pyw if pyw.exists() else exe), os.path.abspath(__file__), "serve",
                "--port", str(port), "--workers", str(workers)]
        flags = CREATE_NO_WINDOW | 0x00000008 | 0x00000200 if os.name == "nt" else 0
        subprocess.Popen(argv, creationflags=flags, close_fds=True,  # console-ok: flags set
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(0.25)
            if health(port, 0.5):
                return 0
    return 1


# ---------------------------------------------------------------- convert

# Events whose http hooks were proven live (block honoured, context delivered). SessionStart
# accepts only command hooks; the rest stay commands until each is proven.
FOLD_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
BACKUP_SUFFIX = ".hookd-bak"
_PY = re.compile(r"(^|[\\/])python[w0-9.]*(\.exe)?$", re.I)
_SHELL_PY = re.compile(r"""^bash -c '(?:cd "\$CLAUDE_PROJECT_DIR" && )?python[0-9.]*(?: -[SI])* """
                       r"""((?:\$CLAUDE_PROJECT_DIR/)?\.claude/hooks/[\w.\-]+\.py)"""
                       r"""((?: --?[\w\-]+)*)'$""")


def _console_scripts() -> dict:
    """console-script name -> `ep:<module>:<func>`, for allowlisted packages only."""
    from importlib.metadata import entry_points

    try:
        eps = entry_points(group="console_scripts")
    except TypeError:  # 3.9: entry_points() takes no group
        eps = entry_points().get("console_scripts", [])
    mods = allowed_modules()
    return {ep.name: "ep:" + ep.value for ep in eps
            if ep.value.split(":")[0].split(".")[0] in mods}


def hook_spec(h: dict, project_dir: str, scripts: dict):
    """The `h=` spec that runs command hook `h` in-process, or None when it must stay a command
    (a shell construct, pwsh, async, or a target hookd would refuse)."""
    if not isinstance(h, dict) or h.get("type") != "command" or h.get("async"):
        return None
    cmd, args = str(h.get("command", "")), [str(a) for a in h.get("args") or []]
    target, rest = None, []
    m = _SHELL_PY.match(cmd)
    if m and not args:
        target, rest = m.group(1).replace("$CLAUDE_PROJECT_DIR/", ""), m.group(2).split()
    elif args and _PY.search(cmd):
        args = [a for a in args if a not in ("-S", "-I", "-u")]
        if args[:1] == ["-m"] and len(args) > 1:
            if args[1].split(".")[0] not in allowed_modules():
                return None
            return "|".join(["-m:" + args[1], *args[2:]])
        if not args or not args[0].endswith(".py"):
            return None
        target, rest = args[0].replace("${CLAUDE_PROJECT_DIR}/", ""), args[1:]
    else:
        bare = cmd.removesuffix(" || true")
        word = bare.split()
        if word and word[0] in scripts and not any(c in bare for c in "|&;<>$`"):
            spec = "|".join([scripts[word[0]], *word[1:], *args])
            return SOFT + spec if bare != cmd else spec  # keep the `|| true` promise
        return None
    try:
        parse_specs(urllib.parse.urlencode({"h": target}), project_dir)
    except PermissionError:
        return None
    return "|".join([target.replace("\\", "/"), *rest])


def convert_hooks(hooks: dict, project_dir: str, port: int = PORT):
    """Fold every in-process-able command hook into one http hook per (event, matcher).

    Returns (new_hooks, folded_count). Pure; hooks that must stay commands keep their group."""
    scripts = _console_scripts()
    out, n = {}, 0
    for event, groups in hooks.items():
        if event not in FOLD_EVENTS or not isinstance(groups, list):
            out[event] = groups
            continue
        folded: dict = {}
        new_groups = []
        for g in groups:
            keep = []
            key = "" if g.get("matcher") in (None, "", "*") else g["matcher"]
            for h in g.get("hooks", []):
                spec = hook_spec(h, project_dir, scripts)
                if spec is None:
                    keep.append(h)
                    continue
                f = folded.setdefault(key, {"specs": [], "t": 0})
                f["specs"].append(spec)
                f["t"] = max(f["t"], int(h.get("timeout", 60)))
                n += 1
            if keep:
                new_groups.append({**g, "hooks": keep})
        for key, f in folded.items():
            q = urllib.parse.urlencode([("t", f["t"])] + [("h", s) for s in f["specs"]])
            grp = {"hooks": [{"type": "http", "url": "http://127.0.0.1:%d/run?%s" % (port, q),
                              "timeout": f["t"] + 10,
                              "headers": {"X-Hookd": "1", "X-Project-Dir": "$CLAUDE_PROJECT_DIR"},
                              "allowedEnvVars": ["CLAUDE_PROJECT_DIR"]}]}
            if key:
                grp["matcher"] = key
            new_groups.append(grp)
        out[event] = new_groups
    return out, n


def default_settings_paths(cwd: str = "") -> list:
    paths = [HOME / ".claude" / "settings.json"]
    proj = Path(cwd or os.getcwd()) / ".claude" / "settings.json"
    if proj.is_file() and proj.resolve() != paths[0].resolve():
        paths.append(proj)
    return [p for p in paths if p.is_file()]


def convert_file(path: Path, write: bool) -> int:
    """Convert one settings file (backup first, kept from the FIRST conversion). Returns folded."""
    data = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent.parent
    project = str(root) if path.parent.name == ".claude" and root != HOME else ""
    data["hooks"], n = convert_hooks(data.get("hooks") or {}, project)
    if write and n:
        bak = path.with_name(path.name + BACKUP_SUFFIX)
        if not bak.exists():
            bak.write_bytes(path.read_bytes())
        tmp = path.with_name(path.name + ".hookd-tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    return n


def revert_file(path: Path) -> bool:
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if not bak.is_file():
        return False
    os.replace(bak, path)
    return True


# ---------------------------------------------------------------- self-test


def _self_test_pool_and_net(check, d: Path, hook: Path) -> None:
    """A busy pool answers inside the deadline; a dead endpoint is skipped, not waited on."""
    sleeper = d / "sleep.py"
    sleeper.write_text("import time\ntime.sleep(8)\n", encoding="utf-8")
    trivial = {"path": str(hook), "args": ["A"], "stdin": b'{"x": "ok"}'}
    # 1. every worker busy, no burst: the request returns 124 at its deadline, not never
    tight = Pool(1, burst=0)
    threading.Thread(target=tight.run, args=({"path": str(sleeper), "stdin": b"{}"}, 20),
                     daemon=True).start()
    time.sleep(0.5)
    t0 = time.monotonic()
    res = tight.run(trivial, 1.0)
    check("a starved request returns 124 inside its own deadline",
          res["code"] == 124 and "no free worker" in res["err"]
          and time.monotonic() - t0 < 2.5)
    # 2. with burst room, a trivial hook is served while a slow one holds the only worker
    roomy = Pool(1, burst=1)
    threading.Thread(target=roomy.run, args=({"path": str(sleeper), "stdin": b"{}"}, 20),
                     daemon=True).start()
    time.sleep(0.5)
    t0 = time.monotonic()
    res = roomy.run(trivial, 15.0)
    check("a burst worker serves a trivial hook while a slow hook holds the pool",
          res["code"] == 0 and time.monotonic() - t0 < 7.0)
    # 3. network guard, in this process: refused endpoint is marked, then skipped at once
    g = globals()
    saved = (g["DOWN_FILE"], g["NET_READ_S"], socket.create_connection)
    g["DOWN_FILE"], g["NET_READ_S"] = d / "down.json", 0.5
    _install_net_guard()
    try:
        _NET["event"] = "PostToolUse"
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # nothing listens there now
        try:
            socket.create_connection(("127.0.0.1", port), timeout=30)
        except OSError as exc:
            check("guard re-raises the real connect failure", "hookd" not in str(exc))
        check("a refused endpoint is marked down", endpoint_down("127.0.0.1", port) > 0)
        t0 = time.monotonic()
        try:
            socket.create_connection(("127.0.0.1", port), timeout=30)
            check("a marked endpoint is skipped", False)
        except ConnectionRefusedError as exc:
            check("a marked endpoint is skipped at once", "hookd:" in str(exc)
                  and time.monotonic() - t0 < 0.2)
        # 4. an endpoint that accepts and never answers: the read cap ends it, then marks it
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        hang_port = srv.getsockname()[1]
        t0 = time.monotonic()
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/" % hang_port, timeout=30)
            check("a silent endpoint times out", False)
        except OSError:
            check("a silent endpoint ends at the read cap, not the hook's 30 s",
                  time.monotonic() - t0 < 3.0)
        check("a read timeout marks the endpoint down", endpoint_down("127.0.0.1", hang_port) > 0)
        srv.close()
        check("read cap applies off Stop, not on Stop", _read_cap(30) == 0.5
              and _read_cap(None) == 0.5)
        _NET["event"] = "Stop"
        check("Stop keeps the hook's own read timeout", _read_cap(30) == 30)
    finally:
        _NET["event"] = ""
        g["DOWN_FILE"], g["NET_READ_S"] = saved[0], saved[1]


def _self_test() -> int:
    import tempfile

    fails = []
    tmp_down = tempfile.mkdtemp(prefix="hookd-selftest-")
    # spawned self-test workers read this at import: they must never mark the real file
    os.environ["AITHER_HOOKD_DOWN_FILE"] = os.path.join(tmp_down, "down.json")

    def check(name: str, cond: bool) -> None:
        if not cond:
            fails.append(name)

    r = lambda code, out="", err="": {"code": code, "out": out, "err": err, "ms": 0}  # noqa: E731
    m = merge("PreToolUse", [r(0), r(2, err="no raw git")])
    check("exit2 denies PreToolUse",
          m.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
          and "no raw git" in m["hookSpecificOutput"]["permissionDecisionReason"])
    m = merge("Stop", [r(2, err="open items"), r(0, json.dumps({"decision": "block",
                                                                  "reason": "brief"}))])
    check("Stop blocks merge reasons", m.get("decision") == "block"
          and "open items" in m["reason"] and "brief" in m["reason"])
    m = merge("UserPromptSubmit", [r(0, "ctx one"), r(0, json.dumps(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                "additionalContext": "ctx two"}}))])
    check("UPS plain stdout becomes context",
          m.get("hookSpecificOutput", {}).get("additionalContext") == "ctx one\n\nctx two")
    check("PostToolUse plain stdout dropped", merge("PostToolUse", [r(0, "noise")]) == {})
    check("non-blocking error dropped", merge("Stop", [r(1, err="boom")]) == {})
    check("silent exit 2 on PostToolUse is a failed hook, not a block",
          merge("PostToolUse", [r(0), r(2), r(0)]) == {})
    check("silent exit 2 on Stop / UserPromptSubmit does not block",
          merge("Stop", [r(2)]) == {} and merge("UserPromptSubmit", [r(2, out="  ")]) == {})
    m = merge("PreToolUse", [r(2)])
    check("silent exit 2 on PreToolUse still denies (a gate fails closed)",
          m.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")
    m = merge("PostToolUse", [r(2, err="lint failed")])
    check("exit 2 WITH a reason on PostToolUse still blocks",
          m.get("decision") == "block" and m.get("reason") == "lint failed")
    check("a SOFT (`|| true`) hook reports any exit as 0 and keeps the real code",
          soften(r(2), True)["code"] == 0 and soften(r(2), True)["soft_code"] == 2
          and soften(r(2), False)["code"] == 2 and "soft_code" not in soften(r(0), True))
    m = merge("PreToolUse", [r(0, json.dumps({"hookSpecificOutput": {
        "permissionDecision": "allow"}})), r(0, json.dumps({"hookSpecificOutput": {
            "permissionDecision": "ask", "permissionDecisionReason": "why"}}))])
    check("ask outranks allow", m["hookSpecificOutput"]["permissionDecision"] == "ask")
    try:
        parse_specs("h=C:/Windows/System32/evil.py", "")
        check("outside root refused", False)
    except PermissionError as exc:
        check("outside root refused", "outside allowed roots" in str(exc))
    with tempfile.TemporaryDirectory() as d:
        hook = Path(d) / "h.py"
        hook.write_text("import sys, json, os\nd = json.load(sys.stdin)\n"
                        "print(d['x'], os.environ.get('CLAUDE_PROJECT_DIR'), sys.argv[1])\n"
                        "sys.exit(2 if d['x'] == 'block' else 0)\n", encoding="utf-8")
        res = _run_one({"path": str(hook), "args": ["A"], "stdin": b'{"x": "hi"}',
                        "cwd": d, "env": {"CLAUDE_PROJECT_DIR": "P"}})
        check("in-process run reads stdin/env/argv", res["code"] == 0
              and res["out"].strip() == "hi P A")
        check("env restored", os.environ.get("CLAUDE_PROJECT_DIR") != "P"
              or "CLAUDE_PROJECT_DIR" in os.environ)
        res = _run_one({"path": str(hook), "args": ["A"], "stdin": b'{"x": "block"}'})
        check("exit code carried", res["code"] == 2)
        wd = Path(d) / "wd.py"
        wd.write_text("import os, threading\nt = threading.Timer(0.3, lambda: os._exit(0))\n"
                      "t.daemon = True\nt.start()\nprint('armed')\n", encoding="utf-8")
        pool = Pool(1)
        first = pool.run({"path": str(wd), "stdin": b"{}"}, 10)
        time.sleep(0.8)  # past the watchdog: it must have been cancelled, not fired
        second = pool.run({"path": str(hook), "args": ["A"], "stdin": b'{"x": "ok"}'}, 10)
        check("watchdog Timer does not kill the worker", first["code"] == 0
              and second["code"] == 0 and pool.spawned == 1)
        # a hook that leaves its worker holding more than the cap: that worker is recycled
        fat = Path(d) / "fat.py"
        fat.write_text("import sys\nsys.modules['__hookd_ballast'] = bytearray(%d)\n"
                       % int((MAX_WORKER_GB + 0.3) * 2**30), encoding="utf-8")
        before = pool.spawned
        pool.run({"path": str(fat), "stdin": b"{}"}, 30)
        light = pool.run({"path": str(hook), "args": ["A"], "stdin": b'{"x": "ok"}'}, 10)
        check("a worker left over the memory cap is recycled",
              MAX_WORKER_GB <= 0 or (pool.spawned == before + 1 and light["code"] == 0))
        _self_test_pool_and_net(check, Path(d), hook)
    with tempfile.TemporaryDirectory() as d:
        hooks_dir = Path(d) / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "g.py").write_text("", encoding="utf-8")
        sh = "bash -c 'cd \"$CLAUDE_PROJECT_DIR\" && python -S .claude/hooks/g.py --stop'"
        hooks = {"Stop": [{"hooks": [{"type": "command", "command": sh, "timeout": 9}]},
                          {"matcher": "*", "hooks": [
                              {"type": "command", "command": "python",
                               "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/g.py"]},
                              {"type": "command", "command": "pwsh -File x.ps1"}]}],
                 "SessionStart": [{"hooks": [{"type": "command", "command": sh}]}],
                 "PreToolUse": [{"matcher": "Bash", "hooks": [
                     {"type": "command", "command": "python", "args": ["C:/evil/x.py"]},
                     {"type": "command", "command": "python", "args": ["a.py"], "async": True}]}]}
        out, n = convert_hooks(hooks, d)
        urls = [h["url"] for g in out["Stop"] for h in g["hooks"] if h["type"] == "http"]
        check("convert folds shell + exec forms into one http hook", n == 2 and len(urls) == 1
              and "g.py%7C--stop" in urls[0] and "t=60" in urls[0])
        check("convert keeps pwsh, SessionStart, outside-root and async as commands",
              out["SessionStart"] == hooks["SessionStart"] and out["PreToolUse"] == hooks[
                  "PreToolUse"] and any(h.get("command") == "pwsh -File x.ps1"
                                        for g in out["Stop"] for h in g["hooks"]))
        sub = Path(d) / "pkg" / "deep"
        sub.mkdir(parents=True)
        specs = parse_specs("h=.claude/hooks/g.py", str(sub))
        check("relative hook found from a subdirectory",
              specs and Path(specs[0][0]) == (hooks_dir / "g.py").resolve())
        scripts = {"awsettings": "ep:awsettings.cli:main"}
        soft = hook_spec({"type": "command",
                          "command": "awsettings --quiet push --debounce || true"}, d, scripts)
        plain = hook_spec({"type": "command",
                           "command": "awsettings --quiet --never-fail push --debounce"},
                          d, scripts)
        check("convert keeps `|| true` as the SOFT marker, and only there",
              soft == SOFT + "ep:awsettings.cli:main|--quiet|push|--debounce"
              and plain == "ep:awsettings.cli:main|--quiet|--never-fail|push|--debounce")
        specs = parse_specs("h=" + SOFT + ".claude/hooks/g.py|--x", str(sub))
        check("a SOFT spec parses with soft=True and its args intact",
              specs and specs[0][1] == ["--x"] and specs[0][2] is True)
        cands = project_candidates("$CLAUDE_PROJECT_DIR", str(sub))
        check("uninterpolated header falls to the payload cwd", cands == [str(sub)])
        if os.name == "nt":
            check("msys /c/ path is native", _native("/c/x/y") == "C:/x/y"
                  and _native("/cx/y") == "/cx/y")
    for f in fails:
        print("FAIL", f)
    print("self-test: %d checks failed" % len(fails) if fails else "self-test: ok")
    return 1 if fails else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hookd")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("serve", "ensure"):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=PORT)
        p.add_argument("--workers", type=int, default=6)
    p = sub.add_parser("health")
    p.add_argument("--port", type=int, default=PORT)
    for name, why in (("convert", "fold command hooks into hookd http hooks"),
                      ("revert", "restore the settings saved by the first convert")):
        p = sub.add_parser(name, help=why)
        p.add_argument("settings", nargs="*", help="default: user + ./.claude settings.json")
        if name == "convert":
            p.add_argument("--write", action="store_true", help="apply (default: dry run)")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.cmd == "serve":
        return serve(a.port, a.workers)
    if a.cmd == "ensure":
        return ensure(a.port, a.workers)
    if a.cmd == "health":
        h = health(a.port)
        print(json.dumps(h) if h else "hookd: not answering on :%d" % a.port)
        return 0 if h else 1
    if a.cmd in ("convert", "revert"):
        paths = [Path(x) for x in a.settings] or default_settings_paths()
        if not paths:
            print("hookd: no settings.json found")
            return 2
        for path in paths:
            if a.cmd == "revert":
                print("%s: %s" % (path, "restored" if revert_file(path) else "no backup"))
                continue
            n = convert_file(path, a.write)
            print("%s: %d hook(s) %s" % (path, n, "folded" if a.write else "foldable (dry run)"))
        if a.cmd == "convert" and a.write and ensure(PORT, 6):
            print("hookd: converted but NOT answering on :%d -- those hooks are skipped" % PORT)
            return 1
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
