"""One code-graph index per root for tools AND crystal; a turn's trace shows only its own
degrades.

Measured 2026-10-14 on the live :9001 daemon: two graph workers (2-2.6 GB each) served
one root, because builtin_tools keyed the cache on abspath and crystal on
normcase(abspath); and every turn's trace read "awgraph:degraded:cold" from a startup
warm-up, long after both workers were serving.
"""

from __future__ import annotations

import asyncio
import os

from adk import builtin_tools
from adk import crystal as crystal_mod
from adk.crystal import Crystal


def test_tools_and_crystal_share_one_index_for_a_root(monkeypatch, tmp_path):
    monkeypatch.setattr(crystal_mod, "_GRAPH_CACHE", {}, raising=False)
    monkeypatch.setattr(builtin_tools, "_LOCAL_GRAPHS", {}, raising=False)
    monkeypatch.setattr(crystal_mod.AwgraphIndex, "probe", lambda self: True)
    root = str(tmp_path)
    monkeypatch.setenv("ADK_CRYSTAL_GRAPH_ROOT", root)
    first = crystal_mod.shared_graph(root)
    assert builtin_tools._local_code_graph() is first
    assert len(crystal_mod._GRAPH_CACHE) == 1
    assert first.root == os.path.abspath(root), "paths in hits keep their real case"


def test_tools_first_then_crystal_still_one_index(monkeypatch, tmp_path):
    monkeypatch.setattr(crystal_mod, "_GRAPH_CACHE", {}, raising=False)
    monkeypatch.setattr(builtin_tools, "_LOCAL_GRAPHS", {}, raising=False)
    monkeypatch.setattr(crystal_mod.AwgraphIndex, "probe", lambda self: True)
    root = str(tmp_path)
    monkeypatch.setenv("ADK_CRYSTAL_GRAPH_ROOT", root)
    tools_idx = builtin_tools._local_code_graph()
    assert crystal_mod.shared_graph(root) is tools_idx


def test_turn_degraded_holds_only_this_recall(monkeypatch):
    c = Crystal(scope="aitherium:t:p", store=None, graph=None, embed=None)
    assert "awm:unbound" in c.telemetry["degraded"]
    c._degrade("awgraph:degraded:cold")
    asyncio.run(c.recall_block("what is in the news today", graph=False))
    assert "awgraph:degraded:cold" in c.telemetry["degraded"], "history is kept"
    assert "awgraph:degraded:cold" not in c.turn_degraded, "a turn reports only its own"
