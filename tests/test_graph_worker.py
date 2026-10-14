"""The code graph in a child process (adk.graph_worker).

In-process, awgraph's pure-Python load and scorer held the daemon's GIL for whole turns
(py-spy, 2026-10-14): every memory stage timed out and the watchdog killed the daemon.
"""

import asyncio
import io
import json
import sys
import textwrap
import time

import pytest

from adk import crystal as crystal_mod
from adk.graph_worker import GraphWorkerClient, serve

FAKE = textwrap.dedent('''
    import json, sys, time
    print(json.dumps({"ready": True, "reason": ""}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        if req["q"] == "slow":
            time.sleep(3)
        if req["q"] == "boom":
            print(json.dumps({"id": req["id"], "error": "ValueError: boom"}), flush=True)
            continue
        print(json.dumps({"id": req["id"], "hits": [
            {"name": "f", "path": "a.py", "line": 3, "signature": "def f()"}]}), flush=True)
''')


def _client():
    c = GraphWorkerClient(".", argv=[sys.executable, "-c", FAKE])
    assert c.start(ready_timeout=30)
    return c


def test_queries_run_in_the_child_and_a_slow_one_never_blocks_the_next():
    c = _client()
    try:
        assert c.query("f", 5, timeout=10)[0]["path"] == "a.py"
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            c.query("slow", 5, timeout=0.3)
        assert time.monotonic() - t0 < 1.0
        assert c.query("f", 5, timeout=10)[0]["name"] == "f"
        with pytest.raises(RuntimeError):
            c.query("boom", 5, timeout=10)
    finally:
        c.close()
    assert not c.alive


def test_a_worker_that_cannot_open_says_why():
    c = GraphWorkerClient(".", argv=[sys.executable, "-c",
                                     "import json;print(json.dumps({'ready': False, "
                                     "'reason': 'no-index for x'}), flush=True)"])
    assert c.start(ready_timeout=30) is False
    assert "no-index" in c.reason


def test_serve_reports_a_missing_awgraph_on_the_ready_line(monkeypatch):
    monkeypatch.setitem(sys.modules, "awgraph", None)
    monkeypatch.setitem(sys.modules, "awgraph.cli", None)
    out = io.StringIO()
    assert serve(".", stdin=io.StringIO(""), stdout=out) == 1
    assert json.loads(out.getvalue())["ready"] is False


def test_the_index_uses_the_worker_and_never_loads_the_graph_in_process(monkeypatch):
    monkeypatch.setenv("ADK_AWGRAPH_WORKER", "1")
    monkeypatch.setattr(crystal_mod, "_worker_argv",
                        lambda root: [sys.executable, "-c", FAKE])
    monkeypatch.setitem(sys.modules, "awgraph.cli", None)  # in-process load would fail
    idx = crystal_mod.AwgraphIndex(".")
    try:
        hits = asyncio.run(idx.query("f", 5))
        assert hits and hits[0]["path"] == "a.py"
    finally:
        if idx._graph is not None:
            idx._graph.close()


def test_a_code_turn_waits_for_the_out_of_process_graph():
    """Out of process a slow graph blocks nothing else, so a code turn waits for it."""
    assert crystal_mod.CODE_GRAPH_WAIT_S > crystal_mod.GRAPH_WAIT_S

    class _SlowGraph:
        async def query(self, text, k):
            await asyncio.sleep(crystal_mod.GRAPH_WAIT_S + 0.5)
            return [{"name": "f", "path": "a.py", "line": 1, "signature": "def f()"}]

    c = crystal_mod.Crystal(scope="aitherium:t:t", graph=_SlowGraph())
    quick = asyncio.run(c.recall_block("fix f in a.py"))
    waited = asyncio.run(c.recall_block("fix f in a.py",
                                        graph_wait=crystal_mod.CODE_GRAPH_WAIT_S))
    assert "a.py" not in quick and "a.py" in waited
