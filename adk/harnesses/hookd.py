"""hookd: run Claude Code command hooks in long-lived workers, reached over `type: http`.

Every Windows process creation leaks kernel pool until reboot (microsoft/WSL#41296, open;
measured here 2026-10-19: ~2.5 File + ~24 FMfn per spawn). A command hook costs bash +
the interpreter + conhost per call, and one Bash tool call fired ~55 processes. An http
hook spawns nothing: Claude Code POSTs the event JSON here, a prefork worker runs each
Python hook in-process with that JSON on stdin, and the results merge into one response
with command-hook semantics (exit 2 blocks, JSON stdout is passed through, plain stdout
becomes context on UserPromptSubmit).

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
`allowed_roots()` run. The required X-Hookd header forces a CORS preflight, which this server
never answers, so a web page cannot drive it.
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import multiprocessing as mp
import os
import queue
import runpy
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


def allowed_roots(project_dir: str) -> list:
    """~/.claude/hooks, <project>/.claude/hooks, and each line of ROOTS_FILE."""
    roots = [HOME / ".claude" / "hooks"]
    if project_dir:
        roots.append(Path(project_dir) / ".claude" / "hooks")
    try:
        roots += [Path(x.strip()) for x in ROOTS_FILE.read_text(encoding="utf-8").splitlines()
                  if x.strip() and not x.startswith("#")]
    except OSError as exc:
        _log("roots file unreadable, defaults only: %s" % exc)
    return roots
DEFAULT_TIMEOUT = 30.0
CREATE_NO_WINDOW = 0x08000000


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


class _Worker:
    def __init__(self, ctx) -> None:
        self.parent, child = ctx.Pipe()
        self.proc = ctx.Process(target=_worker_main, args=(child,), daemon=True)
        self.proc.start()
        child.close()
        self.jobs = 0
        self.last = ""  # the job before a death is the suspect, not the one that found it


class Pool:
    """Prefork workers; a hung or dead worker is replaced, never waited on twice."""

    def __init__(self, size: int) -> None:
        self.ctx = mp.get_context("spawn")
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        if os.name == "nt" and pyw.exists():
            self.ctx.set_executable(str(pyw))  # a console python would open a window
        self.idle: queue.Queue = queue.Queue()
        self.spawned = 0
        for _ in range(size):
            self._add()

    def _add(self) -> None:
        self.idle.put(_Worker(self.ctx))
        self.spawned += 1

    def run(self, job: dict, timeout: float) -> dict:
        w = self.idle.get()
        while not w.proc.is_alive():  # died after its previous job returned
            _log("worker %d dead (exit %s) after %s" % (w.proc.pid, w.proc.exitcode, w.last))
            self._add()
            w = self.idle.get()
        w.last = os.path.basename(job["path"])
        try:
            w.parent.send(job)
            if w.parent.poll(timeout):
                res = w.parent.recv()
                w.jobs += 1
                if w.jobs >= MAX_JOBS:
                    w.parent.send(None)  # the worker returns; a fresh one replaces it
                    self._add()
                else:
                    self.idle.put(w)
                return res
            reason = "timed out after %.0fs" % timeout
        except (EOFError, OSError, BrokenPipeError) as exc:
            reason = "worker died: %s" % exc
        try:
            w.proc.kill()
        except OSError as exc:
            _log("worker %s kill failed (already gone?): %s" % (w.proc.pid, exc))
        self._add()
        return {"code": 124, "out": "", "err": "hookd: %s" % reason, "ms": int(timeout * 1000)}


# ---------------------------------------------------------------- semantics


def allowed_modules() -> set:
    try:
        return {x.strip() for x in MODULES_FILE.read_text(encoding="utf-8").splitlines()
                if x.strip() and not x.startswith("#")}
    except OSError:
        return set()


def parse_specs(query: str, project_dir: str) -> list:
    """`h=path|arg|arg` entries -> [(target, [args])], refusing anything not allowlisted.

    A target is a script path, `ep:<module>:<func>` (a console script's entry point) or
    `-m:<module>`; a module's top-level package must be listed in MODULES_FILE."""
    specs = []
    for raw in urllib.parse.parse_qs(query, keep_blank_values=False).get("h", []):
        parts = raw.split("|")
        if parts[0].startswith(("ep:", "-m:")):
            pkg = parts[0][3:].split(":")[0].split(".")[0]
            if pkg not in allowed_modules():
                raise PermissionError("module not allowlisted: %s" % pkg)
            specs.append((parts[0], parts[1:]))
            continue
        p = Path(parts[0].replace("$HOME", str(HOME)))
        if not p.is_absolute():
            p = Path(project_dir or ".") / p
        p = p.resolve()
        if not any(_under(p, r) for r in allowed_roots(project_dir)):
            raise PermissionError("hook outside allowed roots: %s" % p)
        specs.append((str(p), parts[1:]))
    return specs


def _under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root.resolve())
        return True
    except ValueError:
        return False


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
            project = self.headers.get("X-Project-Dir") or event_json.get("cwd") or ""
            if project.startswith("$"):  # header left uninterpolated
                project = event_json.get("cwd") or ""
            env = {"CLAUDE_PROJECT_DIR": project}
            # The payload is authoritative: a header interpolates Claude's env, which a
            # nested `claude -p` inherits from its parent session (measured 2026-10-20).
            sid = event_json.get("session_id") or ""
            if sid:
                env["CLAUDE_CODE_SESSION_ID"] = sid
            try:
                specs = parse_specs(url.query, project)
            except PermissionError as exc:
                self._send(403, {"error": str(exc)})
                return
            timeout = float(urllib.parse.parse_qs(url.query).get("t", [DEFAULT_TIMEOUT])[0])
            results: list = [None] * len(specs)

            def go(i: int, path: str, args: list) -> None:
                results[i] = pool.run({"path": path, "args": args, "stdin": body,
                                       "cwd": project, "env": env}, timeout)

            threads = [threading.Thread(target=go, args=(i, p, a), daemon=True)
                       for i, (p, a) in enumerate(specs)]
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
                stats.errors += sum(1 for r in results if r["code"] not in (0, 2))
            _log("%s %s %s" % (event, sid[:8], " ".join(
                "%s=%d/%dms" % (Path(p).name, r["code"], r["ms"])
                for (p, _), r in zip(specs, results))))
            for (p, _), r in zip(specs, results):
                if r["code"] not in (0, 2):
                    _log("  %s exit %d: %s" % (Path(p).name, r["code"], r["err"][-600:]))
            self._send(200, resp)

    return H


def serve(port: int, workers: int) -> int:
    pool = Pool(workers)
    stats = _Stats()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(pool, stats))
    srv.daemon_threads = True
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


def ensure(port: int, workers: int) -> int:
    """Start hookd detached and console-less unless it already answers. 0 up, 1 failed."""
    if health(port):
        return 0
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


# ---------------------------------------------------------------- self-test


def _self_test() -> int:
    import tempfile

    fails = []

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
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
