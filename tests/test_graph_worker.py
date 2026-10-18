"""The code graph in a child process (adk.graph_worker).

In-process, awgraph's pure-Python load and scorer held the daemon's GIL for whole turns
(py-spy, 2026-10-14): every memory stage timed out and the watchdog killed the daemon.
"""

import asyncio
import io
import json
import os
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


def test_the_child_imports_this_adk_even_from_a_cwd_holding_another(tmp_path, monkeypatch):
    """Live 2026-10-14: the daemon's cwd held an older adk without graph_worker, and
    `python -m` puts cwd first on sys.path -> "No module named adk.graph_worker"."""
    shadow = tmp_path / "adk"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("")
    monkeypatch.chdir(tmp_path)
    probe = ("import json, adk.graph_worker as g; "
             "print(json.dumps({'ready': True, 'reason': g.__file__}), flush=True)")
    c = GraphWorkerClient(".", argv=[sys.executable, "-c", probe])
    try:
        assert c.start(ready_timeout=30), c.reason
    finally:
        c.close()


class _Chunk:
    def __init__(self, cid, name, path, line=1, kind="function"):
        self.id, self.name, self.source_path = cid, name, path
        self.start_line, self.chunk_type, self.signature = line, kind, f"def {name}()"


class _FakeGraph:
    """A loaded awgraph stand-in whose hybrid search is slow and counted."""

    def __init__(self, chunks):
        self.chunks = {c.id: c for c in chunks}
        self.hybrid_calls = []

    async def hybrid_query(self, query, max_results=10):
        self.hybrid_calls.append(query)
        return [self.chunks["fuzzy"]]


def _serve_fake(monkeypatch, graph, queries):
    import types

    async def _open_graph(root, build):
        return graph, True

    monkeypatch.setitem(sys.modules, "awgraph", types.ModuleType("awgraph"))
    monkeypatch.setitem(sys.modules, "awgraph.cli",
                        types.SimpleNamespace(_open_graph=_open_graph))
    stdin = io.StringIO("".join(json.dumps({"id": i, "q": q, "k": 5}) + "\n"
                                for i, q in enumerate(queries)))
    out = io.StringIO()
    assert serve("/repo", stdin=stdin, stdout=out) == 0
    lines = [json.loads(ln) for ln in out.getvalue().splitlines()]
    assert lines[0]["ready"] is True
    return lines[1:]


def _graph():
    return _FakeGraph([
        _Chunk("fuzzy", "_is_near_duplicate", "/repo/lib/eager.py", 1582),
        _Chunk("doc", "stream_react", "/repo/docs/x.md", 5, kind="section"),
        _Chunk("test", "test_stream_react", "/repo/tests/test_agent.py", 9),
        _Chunk("tdef", "Fake.stream_react", "/repo/tests/test_agent.py", 3, kind="method"),
        _Chunk("def", "AitherAgent.stream_react", "/repo/adk/agent.py", 3318, kind="method"),
    ])


def test_an_exact_symbol_is_answered_from_the_name_map_without_hybrid_search(monkeypatch):
    """Live 2026-10-14: hybrid_query took 2.5-9.5 s for one identifier on a 496k-chunk
    index and ranked a markdown heading above the definition."""
    graph = _graph()
    graph.hybrid_calls.clear()
    got = _serve_fake(monkeypatch, graph, ["stream_react", "AitherAgent.stream_react",
                                           "STREAM_REACT"])
    assert [h["name"] for h in got[0]["hits"]] == ["AitherAgent.stream_react",
                                                   "Fake.stream_react"]
    assert got[0]["hits"][0]["line"] == 3318
    assert [h["name"] for h in got[1]["hits"]] == ["AitherAgent.stream_react"]
    assert got[2]["hits"][0]["name"] == "AitherAgent.stream_react"
    assert "stream_react" not in graph.hybrid_calls
    assert "AitherAgent.stream_react" not in graph.hybrid_calls


def test_prose_and_unknown_symbols_run_a_hybrid_search_warmed_before_ready(monkeypatch):
    """The first hybrid query paid 9.5 s of one-time setup vs 0.6 s warm (2026-10-14);
    the worker pays it once before it says ready."""
    graph = _graph()
    got = _serve_fake(monkeypatch, graph, ["split_near_question",
                                           "where is stream_react defined"])
    assert got[0]["hits"][0]["name"] == "_is_near_duplicate"
    assert got[1]["hits"][0]["name"] == "_is_near_duplicate"
    assert graph.hybrid_calls[1:] == ["split_near_question",
                                      "where is stream_react defined"]
    assert len(graph.hybrid_calls) == 3  # one warm-up, before any request


def test_concept_words_ambiguous_names_and_mirror_copies_are_not_misranked(monkeypatch):
    """Review 2026-10-14 on the live index: "auth" returned same-named FILES, "get_secret"
    (47 definitions) an alphabetical slice led by .DEPLOYMENT copies, and the real
    AitherAgent class ranked last behind type symbols."""
    chunks = [
        _Chunk("fuzzy", "_is_near_duplicate", "/repo/lib/eager.py", 1582),
        _Chunk("authmod", "auth", "/repo/.DEPLOYMENT/standalone/auth.py", 1, kind="module"),
        _Chunk("authfn", "auth", "/repo/lib/auth.py", 10),
        _Chunk("mirror", "get_service_url", "/repo/.DEPLOYMENT/templates/AitherPorts.py", 29),
        _Chunk("bk", "get_service_url", "/repo/x.deploy-backup/AitherPorts.py", 29),
        _Chunk("real", "get_service_url", "/repo/lib/core/AitherPorts.py", 772),
        _Chunk("tsym", "AitherAgent", "/repo/awdesk/a.ts", 3, kind="symbol"),
        _Chunk("cls", "AitherAgent", "/repo/awdk/adk/agent.py", 764, kind="class"),
    ] + [_Chunk(f"m{i}", "run_all", f"/repo/pkg{i}/m.py", i) for i in range(9)]
    graph = _FakeGraph(chunks)
    got = _serve_fake(monkeypatch, graph, ["auth", "get_service_url", "AitherAgent",
                                           "run_all"])
    assert graph.hybrid_calls[1:] == ["auth", "run_all"], "concept word + ambiguous name"
    assert got[1]["hits"][0]["line"] == 772, "the code before its mirror/backup copies"
    assert got[2]["hits"][0]["line"] == 764, "the class before a same-named type symbol"


def test_a_worker_exits_when_its_daemon_is_gone():
    """Live 2026-10-14: each deploy restart left the old worker loading for a dead
    parent; it hogged the disk and the new daemon's worker missed its ready timeout."""
    import subprocess as sp
    import time

    from adk.graph_worker import _pid_alive, watch_parent

    gone = sp.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(gone.pid) is False
    exited = []
    watch_parent(gone.pid, interval=0.05, exit_fn=exited.append)
    deadline = time.monotonic() + 3
    while not exited and time.monotonic() < deadline:
        time.sleep(0.05)
    assert exited == [0]


def test_a_failed_worker_start_is_retried_after_the_cooldown(monkeypatch):
    """One worker that missed its ready timeout disabled the code graph until the
    daemon's next restart."""
    monkeypatch.setenv("ADK_AWGRAPH_WORKER", "1")
    monkeypatch.setattr(crystal_mod, "WORKER_RETRY_S", -1.0)
    starts = []

    class _Client:
        def __init__(self, root, argv=None):
            self.reason = "graph worker not ready after 900s"

        def start(self):
            starts.append(1)
            return len(starts) > 1  # the first start fails, the retry works

        def close(self):
            pass

    import adk.graph_worker as gw

    monkeypatch.setattr(gw, "GraphWorkerClient", _Client)
    idx = crystal_mod.AwgraphIndex(os.getcwd())
    assert asyncio.run(idx._open()) is False
    assert asyncio.run(idx._open()) is True, "retried after the cooldown"
    assert len(starts) == 2
