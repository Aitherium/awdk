"""The code-graph worker keeps itself small: it idles out, defers under host memory
pressure or beside another full load, skips vectors it cannot use, and the client
starts it again on the next query (2026-11-08: a 10.8 GB worker sat idle for hours
while the host ran at 93-96% commit)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import threading
import time

import pytest

from adk import graph_worker as gw


class _Graph:
    chunks: dict = {}

    async def hybrid_query(self, text, max_results=5):
        return []


def _opener(seen):
    async def _open(root, hydrate):
        seen.append(hydrate)
        return _Graph(), True
    return lambda root, hydrate: _open(root, hydrate)


class _Lock:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


def _lines(buf):
    return [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]


def test_worker_idles_out_with_a_reason_and_releases_nothing_it_does_not_hold():
    r, w = os.pipe()
    stdin = os.fdopen(r, "r", encoding="utf-8")
    out, exits = io.StringIO(), []

    def _exit(code):
        exits.append(code)
        os.close(w)  # ends the stdin loop, as the real os._exit would end the process

    rc = gw.serve("root", stdin, out, opener=_opener([]), idle_s=0.2, exit_fn=_exit,
                  admission=lambda root: ("", None), stamp=lambda root: None)
    msgs = _lines(out)
    assert rc == 0 and exits == [0]
    assert msgs[0] == {"ready": True, "reason": ""}
    assert msgs[-1]["idle_exit"] is True and msgs[-1]["reason"].startswith("idle")


def test_idle_watch_never_fires_mid_query():
    now = [0.0]
    fired = []
    watch = gw.IdleWatch(10.0, lambda: fired.append(1), clock=lambda: now[0])
    watch.begin()
    now[0] = 100.0
    assert not watch.due()  # a long query is not idleness
    watch.end()
    assert not watch.due()
    now[0] = 111.0
    assert watch.due()
    assert not gw.IdleWatch(0.0, lambda: None).due()  # 0 disables


def test_idle_default_comes_from_env(monkeypatch):
    monkeypatch.setenv(gw.IDLE_ENV, "42")
    assert gw.idle_seconds() == 42.0
    monkeypatch.delenv(gw.IDLE_ENV)
    assert gw.idle_seconds() == gw.DEFAULT_IDLE_S == 600.0


def test_worker_defers_without_loading_when_admission_says_no():
    out, seen = io.StringIO(), []
    why = "deferred: host commit 94% >= 85% (AWGRAPH_ADMIT_COMMIT_PCT)"
    rc = gw.serve("root", io.StringIO(""), out, opener=_opener(seen),
                  admission=lambda root: (why, None))
    assert rc == gw.EXIT_DEFERRED and seen == []
    assert _lines(out) == [{"ready": False, "reason": why}]


def test_worker_releases_the_load_lock_once_loaded_and_skips_vectors_without_a_lane(
        monkeypatch):
    lock, seen, out = _Lock(), [], io.StringIO()
    monkeypatch.setattr(gw, "_lane_available", lambda: False)
    rc = gw.serve("root", io.StringIO(""), out, opener=_opener(seen), idle_s=0,
                  admission=lambda root: ("", lock), stamp=lambda root: None)
    assert rc == 0 and lock.released == 1 and seen == [False]


def test_worker_exits_after_answering_once_the_index_was_rebuilt():
    stamps = iter([(1.0, 10), (2.0, 11)])
    out, exits = io.StringIO(), []
    stdin = io.StringIO(json.dumps({"id": 1, "q": "x", "k": 1}) + "\n"
                        + json.dumps({"id": 2, "q": "y", "k": 1}) + "\n")
    rc = gw.serve("root", stdin, out, opener=_opener([]), idle_s=0,
                  exit_fn=exits.append, admission=lambda root: ("", None),
                  stamp=lambda root: next(stamps))
    msgs = _lines(out)
    assert rc == 0 and exits == [0]
    assert [m.get("id") for m in msgs if "hits" in m] == [1]  # answered, then left
    assert msgs[-1] == {"idle_exit": True, "reason": "index rebuilt"}


@pytest.mark.skipif(importlib.util.find_spec("awgraph") is None
                    or importlib.util.find_spec("awgraph._admit") is None,
                    reason="awgraph with admission not installed")
def test_real_admission_defers_beside_another_full_load(tmp_path, monkeypatch):
    from awgraph import _admit

    monkeypatch.setenv("AWGRAPH_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("AWGRAPH_ADMIT_COMMIT_PCT", "0")
    # awgraph's own commit check, not the host-wide gate (that would judge the live host)
    monkeypatch.setenv("AWGRAPH_HOST_BUDGET", str(tmp_path / "none.py"))
    monkeypatch.setattr(_admit, "_HOST_BUDGET", {})
    root = str(tmp_path / "repo")
    held = _admit.FullLoadLock(root, "index")
    assert held.acquire() == ""
    try:
        why, lock = gw._admission(root)
        assert lock is None and why.startswith("deferred: busy")
    finally:
        held.release()
    why, lock = gw._admission(root)
    assert why == "" and lock is not None
    lock.release()
    monkeypatch.setenv("AWGRAPH_ADMIT_COMMIT_PCT", "0.001")
    why, lock = gw._admission(root)
    assert lock is None and "host commit" in why


_FAKE_WORKER = r'''
import json, os, sys
print(json.dumps({"ready": True, "reason": ""}), flush=True)
line = sys.stdin.readline()
req = json.loads(line)
print(json.dumps({"id": req["id"], "hits": [{"name": "pid%d" % os.getpid()}]}), flush=True)
print(json.dumps({"idle_exit": True, "reason": "idle 1s"}), flush=True)
'''


def test_client_starts_a_fresh_worker_on_the_next_query_after_an_idle_exit(tmp_path):
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_WORKER, encoding="utf-8")
    client = gw.GraphWorkerClient(str(tmp_path), argv=[sys.executable, str(script)])
    assert client.start(ready_timeout=120), client.reason
    first = client.query("q", 1, timeout=30)[0]["name"]
    deadline = time.monotonic() + 30
    while client.alive and time.monotonic() < deadline:
        time.sleep(0.05)
    assert client.idled and client.idle_reason == "idle 1s"
    second = client.query("q", 1, timeout=30)[0]["name"]
    assert second != first  # a new process answered
    client.close()


def test_concurrent_queries_after_an_idle_exit_start_one_worker(tmp_path):
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_WORKER, encoding="utf-8")
    client = gw.GraphWorkerClient(str(tmp_path), argv=[sys.executable, str(script)])
    starts = []
    real_start = client.start

    def _counted(ready_timeout=900.0):
        starts.append(1)
        return real_start(ready_timeout)

    client.start = _counted  # type: ignore[method-assign]
    assert client.start(ready_timeout=120), client.reason
    client.query("q", 1, timeout=30)
    deadline = time.monotonic() + 30
    while client.alive and time.monotonic() < deadline:
        time.sleep(0.05)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        _safe(client.query, "q", 1, 5))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(starts) == 2  # the first start and ONE restart
    client.close()


def _safe(fn, *a):
    try:
        return fn(*a)
    except (RuntimeError, TimeoutError) as exc:
        return str(exc)


def test_worker_caps_blas_threads_unless_the_caller_chose():
    env: dict = {}
    gw._limit_blas_threads(env)
    assert env["OPENBLAS_NUM_THREADS"] == gw.BLAS_THREADS == "2"
    env = {"OPENBLAS_NUM_THREADS": "8"}
    gw._limit_blas_threads(env)
    assert env["OPENBLAS_NUM_THREADS"] == "8"


_SLOW_WORKER = r'''
import json, sys, time
if len(sys.argv) > 1 and sys.argv[1] == "slow":
    time.sleep(3)
print(json.dumps({"ready": True, "reason": ""}), flush=True)
sys.stdin.readline()
print(json.dumps({"idle_exit": True, "reason": "idle 1s"}), flush=True)
'''


def test_a_query_during_the_reload_says_still_loading(tmp_path):
    """The daemon's /v1/codegraph route turns "still loading" into 503 loading, so a
    host-shared `awgraph query` waits instead of loading a second copy itself."""
    script = tmp_path / "slow_worker.py"
    script.write_text(_SLOW_WORKER, encoding="utf-8")
    client = gw.GraphWorkerClient(str(tmp_path), argv=[sys.executable, str(script)])
    assert client.start(ready_timeout=120), client.reason
    with pytest.raises((RuntimeError, TimeoutError)):
        client.query("q", 1, timeout=0.5)  # the fake never answers; it idles out
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and (client.alive or (
            client._restarting is not None and client._restarting.is_alive())):
        time.sleep(0.05)
    client.argv = [sys.executable, str(script), "slow"]
    with pytest.raises(TimeoutError, match="still loading"):
        client.query("q", 1, timeout=0.5)
    client.close()


_SILENT_EXIT = r'''
import json, sys
print(json.dumps({"ready": True, "reason": ""}), flush=True)
sys.stdin.readline()
'''


def test_a_worker_that_dies_mid_query_fails_the_waiter_at_once(tmp_path):
    """Review 2026-11-08: a waiting query sat out its whole timeout after a crash."""
    script = tmp_path / "silent.py"
    script.write_text(_SILENT_EXIT, encoding="utf-8")
    client = gw.GraphWorkerClient(str(tmp_path), argv=[sys.executable, str(script)])
    assert client.start(ready_timeout=120), client.reason
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="exited"):
        client.query("q", 1, timeout=30)
    assert time.monotonic() - t0 < 10
    client.close()


def test_idle_exit_refuses_a_query_that_arrives_as_it_fires():
    now = [0.0]
    watch = gw.IdleWatch(10.0, lambda: None, clock=lambda: now[0])
    assert watch.begin() is True
    watch.end()
    now[0] = 11.0
    assert watch.claim() is True
    assert watch.begin() is False  # the request is answered with an error, not dropped


class _DeadPipe:
    def write(self, _):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        return None


class _LiveProc:
    stdin = _DeadPipe()

    def poll(self):
        return None


def test_writing_to_a_worker_that_just_exited_raises_runtime_error():
    client = gw.GraphWorkerClient("root")
    client._proc, client._ok = _LiveProc(), True  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="gone"):
        client.query("q", 1, timeout=1)
    assert client._waiting == {}


def test_a_restart_closes_the_previous_log_handle(tmp_path, monkeypatch):
    logs = []

    def _log():
        fh = open(tmp_path / ("log%d" % len(logs)), "a", encoding="utf-8")
        logs.append(fh)
        return fh

    monkeypatch.setattr(gw, "_log_file", _log)
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_WORKER, encoding="utf-8")
    client = gw.GraphWorkerClient(str(tmp_path), argv=[sys.executable, str(script)])
    assert client.start(ready_timeout=120), client.reason
    client.close()
    assert client.start(ready_timeout=120), client.reason
    assert logs[0].closed and not logs[1].closed
    client.close()
    logs[1].close()


class _Ticket:
    def __init__(self):
        self.started, self.released = [], 0

    def start(self, *a, **kw):
        self.started.append(kw)
        return self

    def release(self):
        self.released += 1


def test_the_host_budget_ticket_is_held_while_resident_and_freed_on_exit():
    """One host admission ledger: the worker holds its ticket while it is loaded and
    gives it back when it exits; the per-root load lock is freed as soon as it loaded."""
    lock, ticket = _Lock(), _Ticket()
    out = io.StringIO()
    stdin = io.StringIO(json.dumps({"id": 1, "q": "x", "k": 1}) + "\n")
    rc = gw.serve("root", stdin, out, opener=_opener([]), idle_s=0,
                  admission=lambda root: ("", gw._Admitted(lock, ticket)),
                  stamp=lambda root: None)
    assert rc == 0
    assert ticket.started and ticket.started[0].get("self_mode") is True
    assert lock.released == 1 and ticket.released == 1


def test_a_deferred_host_budget_reaches_the_ready_line():
    out = io.StringIO()
    why = "deferred: host budget: 1 waiter(s) ahead -- graph worker load not started"
    rc = gw.serve("root", io.StringIO(""), out, opener=_opener([]),
                  admission=lambda root: (why, None))
    assert rc == gw.EXIT_DEFERRED and _lines(out)[0]["reason"].startswith("deferred:")
