"""The awgit -> graph-RAG bridge, with the rename case as the headline.

Built on awgit's REAL dataclasses (`NodeChange`, `LedgerEntry`) rather than
stubs, so a field rename upstream fails these tests instead of silently
producing an empty graph update.
"""

from __future__ import annotations

import pytest
from adk.graph_rag import awgit_memory as am

awgit = pytest.importorskip("awgit", reason="awgit is an adk dependency; skip if absent")


def _change(node_id, ctype="modified", *, old=None, new=None, symbol=None,
            path="a.py", renamed_from=None, moved_from=None):
    return awgit.NodeChange(
        node_id=node_id,
        change_type=ctype,
        old_body_sha=old,
        new_body_sha=new,
        symbol=symbol or node_id,
        path=path,
        renamed_from=renamed_from,
        moved_from=moved_from,
        semantic_note=None,
    )


def _entry(sha, changes, actor="dev", verified=True, summary="did a thing"):
    return awgit.LedgerEntry(
        ledger_ref="r", op_id=sha, actor=actor, verified_actor=actor,
        actor_verified=verified, git_sha=sha, git_parent_sha=None, ts=1,
        file_paths=["a.py"], node_changes=changes,
        change_types=[c.change_type for c in changes], summary=summary,
    )


class _Log:
    def __init__(self, entries):
        self._entries = entries

    def all_ops(self):
        return list(self._entries)


def test_stale_nodes_are_symbols_not_files():
    """A one-line edit invalidates one symbol, not its whole module."""
    log = _Log([_entry("s1", [_change("mod:func_a", old="x", new="y")])])
    assert am.stale_nodes(log) == {"mod:func_a"}


def test_a_pure_rename_does_not_force_re_embedding():
    """The body is identical, so the stored vector is already correct.

    Re-embedding here spends the expensive half of indexing to arrive at the
    value already on disk.
    """
    same = "sha-identical"
    log = _Log([_entry("s1", [
        _change("mod:new_name", "renamed", old=same, new=same, renamed_from="mod:old_name"),
    ])])
    assert am.stale_nodes(log) == set()


def test_a_rename_that_also_edits_the_body_is_stale():
    log = _Log([_entry("s1", [
        _change("mod:new", "renamed", old="a", new="b", renamed_from="mod:old"),
    ])])
    assert am.stale_nodes(log) == {"mod:new"}


def test_rename_chain_recovers_prior_identity():
    """The half a file-keyed indexer cannot reconstruct at all."""
    log = _Log([
        _entry("s1", [_change("b", "renamed", renamed_from="a")]),
        _entry("s2", [_change("c", "renamed", renamed_from="b")]),
    ])
    assert am.rename_chain(log, "c") == ["a", "b"]


def test_rename_chain_is_empty_for_a_node_that_never_moved():
    log = _Log([_entry("s1", [_change("a", old="x", new="y")])])
    assert am.rename_chain(log, "a") == []


def test_since_sha_returns_only_later_commits():
    log = _Log([
        _entry("s1", [_change("a", old="1", new="2")]),
        _entry("s2", [_change("b", old="1", new="2")]),
        _entry("s3", [_change("c", old="1", new="2")]),
    ])
    assert am.stale_nodes(log, since_sha="s1") == {"b", "c"}


def test_unknown_since_sha_reindexes_everything():
    """Answering 'nothing changed' would be a confident lie.

    An unknown sha means we cannot say what is new; claiming nothing is would
    leave the graph stale forever with no signal that it happened.
    """
    log = _Log([_entry("s1", [_change("a", old="1", new="2")])])
    assert am.stale_nodes(log, since_sha="never-seen") == {"a"}


def test_provenance_records_whether_the_actor_was_verified():
    """Provenance nobody can trust is decoration."""
    log = _Log([_entry("s1", [_change("a", old="1", new="2")], actor="mallory", verified=False)])
    prov = am.latest_provenance(log)["a"]
    assert prov.actor == "mallory"
    assert prov.verified is False


def test_apply_provenance_writes_onto_the_graph_node():
    from adk.graph_rag.graph_store import GraphNode, GraphStore

    store = GraphStore()
    store.add_node(GraphNode(id="a", content="def a(): ...", score=0.0, metadata={}))
    log = _Log([_entry("s1", [_change("a", old="1", new="2")], summary="tightened a()")])

    assert am.apply_provenance(store, log) == 1
    meta = store.get_node("a").metadata["awgit"]
    assert meta["summary"] == "tightened a()"
    assert meta["git_sha"] == "s1"


def test_apply_provenance_skips_nodes_the_graph_does_not_have():
    from adk.graph_rag.graph_store import GraphStore

    log = _Log([_entry("s1", [_change("absent", old="1", new="2")])])
    assert am.apply_provenance(GraphStore(), log) == 0


def test_everything_degrades_to_empty_without_awgit():
    """adk must keep working outside a git tree.

    A retriever that explodes because there is no oplog is worse than one with
    no change information.
    """
    assert am.stale_nodes(None) == set()
    assert am.rename_chain(None, "a") == []
    assert am.latest_provenance(None) == {}
    assert am.summarize(None)["available"] is False


def test_a_broken_oplog_is_absence_not_a_crash():
    class _Broken:
        def all_ops(self):
            raise RuntimeError("store corrupt")

    assert am.stale_nodes(_Broken()) == set()


def test_summarize_counts_identity_preserved_moves():
    log = _Log([_entry("s1", [
        _change("b", "renamed", old="1", new="2", renamed_from="a"),
        _change("c", old="1", new="2"),
    ])])
    s = am.summarize(log)
    assert s["commits"] == 1
    assert s["stale_nodes"] == 2
    assert s["identity_preserved"] == 1
