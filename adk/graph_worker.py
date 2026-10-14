"""The code graph in its own process.

awgraph's index load and its keyword scorer are pure-Python work over ~495k chunks.
Run inside the adk daemon they held the GIL for whole turns (py-spy, 2026-10-14): every
other memory stage timed out and /health stopped answering, so the watchdog killed a
working daemon. This module is that work moved into a child process the daemon owns:

* ``python -m adk.graph_worker <root>`` loads the index once, prints one ready line
  (``{"ready": true|false, "reason": ...}``), then answers one JSON line per query
  (``{"id", "q", "k"}`` -> ``{"id", "hits": [...]}`` or ``{"id", "error": ...}``).
* :class:`GraphWorkerClient` starts it, waits for ready, and runs queries with a
  timeout. A reader thread routes answers by id, so a query the caller gave up on
  never blocks the next one.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Optional

logger = logging.getLogger("adk.graph_worker")

#: Windows: no console window (a console-less parent's child opens a terminal tab) and
#: below-normal priority (the graph must never compete with the owner's foreground).
_WIN_FLAGS = 0x08000000 | 0x00004000  # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS


def _hit(chunk: Any, root: str) -> dict:
    path = getattr(chunk, "source_path", "") or ""
    try:
        path = os.path.relpath(path, root)
    except ValueError:  # another drive on Windows: the absolute path is the answer
        path = os.path.abspath(path) if path else path
    return {"name": getattr(chunk, "name", ""), "path": path,
            "line": getattr(chunk, "start_line", 0),
            "signature": getattr(chunk, "signature", "") or ""}


def serve(root: str, stdin: Any = None, stdout: Any = None) -> int:
    """The worker loop. Returns the process exit code."""
    import asyncio

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def say(obj: dict) -> None:
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()

    try:
        from awgraph.cli import _open_graph  # type: ignore[import-not-found]

        loop = asyncio.new_event_loop()
        graph, ok = loop.run_until_complete(_open_graph(root, build=False))
    except Exception as exc:  # noqa: BLE001 -- reported on the ready line
        say({"ready": False, "reason": f"{type(exc).__name__}: {exc}"})
        return 1
    if not ok:
        say({"ready": False, "reason": f"no-index for {root} (run `awgraph index`)"})
        return 1
    say({"ready": True, "reason": ""})
    for line in stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        try:
            chunks = loop.run_until_complete(
                graph.hybrid_query(str(req.get("q") or ""), max_results=int(req.get("k") or 5)))
            say({"id": rid, "hits": [_hit(c, root) for c in chunks]})
        except Exception as exc:  # noqa: BLE001 -- one failed query never ends the worker
            say({"id": rid, "error": f"{type(exc).__name__}: {exc}"})
    return 0


def _log_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".aither", "logs", "graph-worker.log")


def _log_file() -> Any:
    try:
        os.makedirs(os.path.dirname(_log_path()), exist_ok=True)
        return open(_log_path(), "a", encoding="utf-8")  # noqa: SIM115 -- the child owns it
    except OSError as exc:
        logger.debug("graph worker log unavailable: %s", exc)
        return None


def _last_log_line() -> str:
    try:
        with open(_log_path(), encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh.readlines()[-20:] if ln.strip()]
        return lines[-1][:300] if lines else "(no output)"
    except OSError:
        return "(no log)"


class GraphWorkerClient:
    """One worker process for one repository root."""

    def __init__(self, root: str, argv: Optional[list] = None):
        self.root = os.path.abspath(root)
        self.argv = argv or [sys.executable, "-m", "adk.graph_worker", self.root]
        self.reason = ""
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._ok = False
        self._next = 0
        self._waiting: dict[int, tuple[threading.Event, dict]] = {}

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, ready_timeout: float = 900.0) -> bool:
        """Start the worker and wait for its ready line. True when it can answer."""
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = _WIN_FLAGS
        # The child imports THIS adk (the daemon runs `python -P` from a snapshot that
        # is not on the child's default path) and logs to a file a failure can be read
        # from: on 2026-10-14 the first live start died silently with stderr discarded.
        # cwd too: `python -m` puts the working directory FIRST on sys.path, and the
        # daemon's cwd held an older adk tree without this module ("No module named
        # adk.graph_worker", measured live), which shadowed PYTHONPATH.
        env = dict(os.environ)
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = os.pathsep.join(
            [here] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
        self._log = _log_file()
        self._proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log or subprocess.DEVNULL, text=True, encoding="utf-8",
            bufsize=1, env=env, cwd=here, **kw)
        threading.Thread(target=self._read, name="graph-worker-reader", daemon=True).start()
        if not self._ready.wait(ready_timeout):
            self.reason = f"graph worker not ready after {ready_timeout:.0f}s"
            self.close()
            return False
        return self._ok

    def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "ready" in msg:
                self._ok, self.reason = bool(msg["ready"]), str(msg.get("reason") or "")
                self._ready.set()
                continue
            with self._lock:
                slot = self._waiting.pop(msg.get("id"), None)
            if slot is not None:
                slot[1].update(msg)
                slot[0].set()
        if not self._ready.is_set():
            self.reason = self.reason or ("graph worker exited before it was ready: "
                                          + _last_log_line())
            self._ready.set()

    def query(self, text: str, k: int, timeout: float) -> list[dict]:
        """Hits for ``text``; raises TimeoutError past ``timeout`` and RuntimeError when
        the worker is gone or answered an error."""
        if not self.alive or not self._ok:
            raise RuntimeError(self.reason or "graph worker not running")
        done, box = threading.Event(), {}
        with self._lock:
            self._next += 1
            rid = self._next
            self._waiting[rid] = (done, box)
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.write(json.dumps({"id": rid, "q": text, "k": k}) + "\n")
            self._proc.stdin.flush()
        if not done.wait(timeout):
            with self._lock:
                self._waiting.pop(rid, None)
            raise TimeoutError(f"graph query over {timeout:.1f}s")
        if "error" in box:
            raise RuntimeError(box["error"])
        return list(box.get("hits") or [])

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError as exc:
                logger.debug("graph worker kill failed: %s", exc)


if __name__ == "__main__":  # pragma: no cover -- exercised as a subprocess
    sys.exit(serve(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()))
