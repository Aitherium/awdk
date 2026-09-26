"""``adk run --crystal SCOPE`` binds the crystal on every agent the server builds.

Before: ``build_crystal`` was called by nothing in awdk (only a dev tool), no CLI flag
reached it, and a stored vector from another embedder (a different dimension) was
compared by cosine against the query vector, so recall silently ranked garbage.
"""
from __future__ import annotations

import argparse
import asyncio
import os

import pytest

from adk.crystal import Crystal, crystal_from_env

_CRYSTAL_VARS = ("ADK_CRYSTAL_SCOPE", "ADK_CRYSTAL_DB", "ADK_CRYSTAL_GRAPH_ROOT",
                 "ADK_CRYSTAL_NO_EMBED")


@pytest.fixture(autouse=True, scope="module")
def _no_crystal_env_leak():
    """After every test here (and every monkeypatch undo) the crystal env must be what it
    was before the module ran; a leak binds later AitherAgents to the real store."""
    before = {k: os.environ.get(k) for k in _CRYSTAL_VARS}
    yield
    after = {k: os.environ.get(k) for k in _CRYSTAL_VARS}
    assert after == before, f"crystal env leaked out of this module: {after}"


def _parser() -> argparse.ArgumentParser:
    from adk.cli import _register_commands

    p = argparse.ArgumentParser(prog="adk")
    _register_commands(p.add_subparsers(dest="command"))
    return p


def test_run_parser_accepts_crystal_flags():
    a = _parser().parse_args(["run", "--crystal", "acme:{agent}:proj", "--crystal-no-embed"])
    assert a.crystal == "acme:{agent}:proj"
    assert a.crystal_no_embed is True


def test_run_crystal_flag_exports_env_and_rejects_bad_scope(monkeypatch):
    from adk.cli import _export_crystal_env

    # _export_crystal_env writes os.environ directly. A bare delenv(raising=False) on an
    # ABSENT key records nothing, so pytest would never undo those writes and every later
    # AitherAgent would bind a crystal on the developer's real ~/.aither/awm/memory.db.
    # setenv first records the original (absent) state; delenv then clears it.
    for k in _CRYSTAL_VARS:
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    _export_crystal_env(argparse.Namespace(crystal="acme:{agent}:p", crystal_db="",
                                           crystal_graph="", crystal_no_embed=True))
    assert os.environ["ADK_CRYSTAL_SCOPE"] == "acme:{agent}:p"
    assert os.environ["ADK_CRYSTAL_NO_EMBED"] == "1"
    with pytest.raises(SystemExit):
        _export_crystal_env(argparse.Namespace(crystal="just-a-name"))


def test_crystal_from_env_binds_per_agent_scope(tmp_path, monkeypatch):
    monkeypatch.delenv("ADK_CRYSTAL_SCOPE", raising=False)
    assert crystal_from_env("bob") is None
    monkeypatch.setenv("ADK_CRYSTAL_SCOPE", "acme:{agent}:proj")
    monkeypatch.setenv("ADK_CRYSTAL_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("ADK_CRYSTAL_NO_EMBED", "1")
    c = crystal_from_env("bob")
    assert isinstance(c, Crystal) and c.scope == "acme:bob:proj" and c.embed is None


def test_agent_binds_crystal_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADK_CRYSTAL_SCOPE", "acme:{agent}:proj")
    monkeypatch.setenv("ADK_CRYSTAL_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("ADK_CRYSTAL_NO_EMBED", "1")
    from adk.agent import AitherAgent

    agent = AitherAgent(name="bob")
    assert isinstance(agent.crystal, Crystal)
    assert agent.crystal.scope == "acme:bob:proj"


def test_recall_falls_back_to_keywords_on_a_dimension_mismatch():
    class Store:
        def put(self, *a):
            pass

        def scan(self, limit):
            return [("k1", "the postgres replica lags behind", {"vec": [0.1] * 768}),
                    ("k2", "unrelated fact about widgets here", {"vec": [0.1] * 768})]

    async def small(texts):
        return [[1.0] * 384 for _ in texts]

    c = Crystal(scope="a:b:c", store=Store(), embed=small)
    got = asyncio.run(c.recall_facts("why does the postgres replica lag"))
    assert got[0].startswith("the postgres replica")

