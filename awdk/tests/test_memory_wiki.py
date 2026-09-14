"""Tests for adk.memory_wiki — LyraWiki-style self-managed semantic knowledge.

Offline by design: deterministic bag-of-words embedder injected, fake LLMs,
tmp-dir sqlite graphs, fake clocks for decay/prune windows.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

import pytest

from adk.graph_memory import GraphMemory
from adk.memory_wiki import (
    EDGE_CONSOLIDATED_FROM,
    WIKI_NODE_TYPE,
    MemoryWiki,
    _parse_json_block,
    _slugify,
)

_DIM = 32


def _bucket(word: str) -> int:
    return int(hashlib.md5(word.encode()).hexdigest(), 16) % _DIM


async def _embed(text: str) -> list[float]:
    """Deterministic offline bag-of-words embedder (stopword-ish filtered)."""
    vec = [0.0] * _DIM
    for w in re.findall(r"[a-z]{4,}", text.lower()):
        vec[_bucket(w)] += 1.0
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec] if norm else vec


@pytest.fixture
def graph(tmp_path):
    return GraphMemory(
        db_path=tmp_path / "wiki.db", agent_name="wikitest",
        embedder=_embed, auto_sync=False,
    )


HARBOUR = [
    ("Seagate tax rumor",
     "The harbour town of Seagate taxes fish traders heavily at the docks"),
    ("Seagate warehouse",
     "Fish traders in Seagate harbour keep salted stock in the dockside warehouse"),
    ("Seagate patrol",
     "Seagate harbour guards patrol the fish market and shake down traders"),
]


async def _seed(graph):
    ids = []
    for label, content in HARBOUR:
        node = await graph.add_node(
            label=label, node_type="fact", content=content, tier="persistent",
        )
        ids.append(node.id)
    lone = await graph.add_node(
        label="Volcano hermit", node_type="fact",
        content="A quiet recluse dwells beside the ashen crater rim",
        tier="persistent",
    )
    return ids, lone.id


def _str_llm(respond):
    """A (str)->str llm that REFUSES messages lists (exercises the fallback)."""
    def llm(prompt):
        if not isinstance(prompt, str):
            raise TypeError("str prompts only")
        return respond(prompt) if callable(respond) else respond
    return llm


def _article_rows(graph):
    with graph._connect() as conn:
        return conn.execute(
            "SELECT id, label, content, tier, metadata FROM nodes "
            "WHERE node_type = ?", (WIKI_NODE_TYPE,),
        ).fetchall()


def _node_row(graph, nid):
    with graph._connect() as conn:
        return conn.execute(
            "SELECT id, tier, metadata FROM nodes WHERE id = ?", (nid,)
        ).fetchone()


# ─── helpers under test ─────────────────────────────────────────────────────

def test_slugify():
    assert _slugify("Seagate Harbour!") == "seagate-harbour"
    assert _slugify("  ") == "untitled"


def test_parse_json_block_tolerant():
    assert _parse_json_block('noise {"title": "T", "markdown": "M"} trailing') == {
        "title": "T", "markdown": "M"}
    assert _parse_json_block("no json here") == {}
    assert _parse_json_block("") == {}


# ─── consolidate ────────────────────────────────────────────────────────────

async def test_consolidate_builds_article_edges_and_demotes(graph):
    ids, lone = await _seed(graph)

    def respond(prompt):
        return json.dumps({
            "title": "Seagate Harbour",
            "markdown": f"# Seagate Harbour\nFish traders are taxed and "
                        f"shaken down (src: {ids[0]}). See [[Trade Routes]].",
            "contradicted_sources": [],
        })

    wiki = MemoryWiki(graph)
    stats = await wiki.consolidate(_str_llm(respond))

    assert stats["articles_created"] == ["Seagate Harbour"]
    arts = _article_rows(graph)
    assert len(arts) == 1
    art_id, _label, content, tier, md_json = arts[0]
    md = json.loads(md_json)
    assert md["slug"] == "seagate-harbour"
    assert md["revision"] == 1
    assert set(md["sources"]) == set(ids)
    assert tier == "trace"
    # Deterministic citation footer cites every source id.
    for nid in ids:
        assert nid in content

    # CONSOLIDATED_FROM edges article -> each source.
    with graph._connect() as conn:
        targets = {r[0] for r in conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation = ?",
            (art_id, EDGE_CONSOLIDATED_FROM),
        ).fetchall()}
    assert targets == set(ids)

    # Sources demoted to the fast-decay tier + marked consolidated.
    for nid in ids:
        row = _node_row(graph, nid)
        assert row[1] == "session"
        assert json.loads(row[2])["consolidated_into"] == art_id

    # The unrelated singleton stays raw and untouched.
    row = _node_row(graph, lone)
    assert row[1] == "persistent"
    assert "consolidated_into" not in json.loads(row[2])

    # Revision is governance-ledgered.
    ledger, _tombs = graph._governance_stores()
    assert ledger is not None
    kinds = [(m.mutation_type.value, m.node_id) for m in ledger.all()]
    assert ("store", art_id) in kinds


async def test_consolidate_accepts_messages_llm(graph):
    ids, _lone = await _seed(graph)
    seen = []

    def messages_llm(messages):
        assert isinstance(messages, list) and messages[0]["role"] == "system"
        seen.append(messages)
        return json.dumps({"title": "Seagate Harbour",
                           "markdown": "# Seagate Harbour\nDock politics.",
                           "contradicted_sources": []})

    stats = await MemoryWiki(graph).consolidate(messages_llm)
    assert stats["articles_created"] == ["Seagate Harbour"]
    assert seen


async def test_consolidate_marks_contradicted_sources_superseded(graph):
    ids, _lone = await _seed(graph)

    def respond(prompt):
        found = re.findall(r"id=([0-9a-f]{12})", prompt)
        return json.dumps({
            "title": "Seagate Harbour",
            "markdown": "# Seagate Harbour\nThe tax rumor was wrong.",
            "contradicted_sources": found[:1],
        })

    await MemoryWiki(graph).consolidate(_str_llm(respond))
    art_id = _article_rows(graph)[0][0]
    md = json.loads(_node_row(graph, ids[0])[2])
    assert md["superseded_by"] == art_id
    assert md["stale"] is True
    with graph._connect() as conn:
        sup = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ? AND target_id = ? "
            "AND relation = 'supersedes'", (art_id, ids[0]),
        ).fetchone()[0]
    assert sup == 1


async def test_consolidate_updates_existing_article_with_new_evidence(graph):
    ids, _lone = await _seed(graph)
    base = _str_llm(json.dumps({
        "title": "Seagate Harbour",
        "markdown": "# Seagate Harbour\nTaxed fish traders.",
        "contradicted_sources": [],
    }))
    wiki = MemoryWiki(graph)
    await wiki.consolidate(base)

    new = await graph.add_node(
        label="Seagate Harbour",  # slug-matches the article
        node_type="fact",
        content="New evidence: seagate harbour fish traders now bribe the guards",
        tier="persistent",
    )
    calls = []

    def respond(prompt):
        calls.append(prompt)
        assert "CURRENT ARTICLE" in prompt  # update path, not a fresh draft
        return json.dumps({
            "title": "Seagate Harbour",
            "markdown": "# Seagate Harbour\nTaxed AND bribing now.",
            "contradicted_sources": [],
        })

    stats = await wiki.consolidate(_str_llm(respond))
    assert stats["articles_updated"] == ["Seagate Harbour"]
    arts = _article_rows(graph)
    assert len(arts) == 1
    md = json.loads(arts[0][4])
    assert md["revision"] == 2
    assert new.id in md["sources"]
    # the new evidence was consumed
    assert json.loads(_node_row(graph, new.id)[2])["consolidated_into"] == arts[0][0]


async def test_consolidate_llm_silence_never_demotes(graph):
    ids, _lone = await _seed(graph)
    stats = await MemoryWiki(graph).consolidate(_str_llm(""))
    assert stats["articles_created"] == []
    assert stats["skipped"] >= 1
    assert _article_rows(graph) == []
    for nid in ids:
        assert _node_row(graph, nid)[1] == "persistent"  # untouched


async def test_consolidate_plain_text_reply_becomes_article(graph):
    """Repair tolerance: a non-JSON llm reply IS the article markdown."""
    await _seed(graph)
    stats = await MemoryWiki(graph).consolidate(
        _str_llm("Seagate's harbour lives off the fish trade."))
    assert len(stats["articles_created"]) == 1
    arts = _article_rows(graph)
    assert "fish trade" in arts[0][2]


async def test_consolidate_since_filters_old_nodes(graph):
    ids, _lone = await _seed(graph)
    stats = await MemoryWiki(graph).consolidate(
        _str_llm("irrelevant"), since=time.time() + 3600)
    assert stats["examined"] == 0


# ─── recall (article-first) ─────────────────────────────────────────────────

async def test_recall_article_first_and_reinforces(graph):
    ids, _lone = await _seed(graph)
    wiki = MemoryWiki(graph)
    await wiki.consolidate(_str_llm(json.dumps({
        "title": "Seagate Harbour",
        "markdown": "# Seagate Harbour\nFish traders, taxes, patrols, harbour.",
        "contradicted_sources": [],
    })))
    res = await wiki.recall("seagate harbour fish traders", limit=5)
    assert res, "recall returned nothing"
    assert res[0].node_type == WIKI_NODE_TYPE
    # reinforce-on-recall feeds the prune relevance
    art = await graph.get_node(res[0].id)
    assert int(art.metadata.get("reinforcement_count", 0)) >= 1


# ─── prune: tombstone → hard delete ─────────────────────────────────────────

async def test_prune_tombstones_then_hard_deletes(graph):
    ids, lone = await _seed(graph)
    wiki = MemoryWiki(graph)
    await wiki.consolidate(_str_llm(json.dumps({
        "title": "Seagate Harbour",
        "markdown": "# Seagate Harbour\nFish trade dossier.",
        "contradicted_sources": [],
    })))
    art_id = _article_rows(graph)[0][0]

    # A second article whose consolidated source stays LIVE — must survive.
    keeper = await wiki._write_article(
        "Volcano Hermit",
        "# Volcano Hermit\nA recluse beside the crater rim, hates visitors.",
        [lone],
    )

    # Simulate the sweep having collected the demoted raw copies — one source
    # survives as a STALE husk (doesn't count as a live link, but proves the
    # tombstone snapshot captures edge connectivity).
    for nid in ids[1:]:
        await graph.remove_node(nid)
    wiki._merge_node_metadata(ids[0], {"stale": True})

    far = time.time() + 400 * 86400  # trace freshness ~0.046 < 0.05 floor
    stats = await wiki.prune(now=far)
    assert stats["tombstoned"] == 1
    assert art_id in stats["tombstones"]
    assert stats["hard_deleted"] == 0  # fresh tombstone survives its window

    # The node row (incl. its embedding) is GONE from the graph.
    assert await graph.get_node(art_id) is None
    with graph._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM nodes WHERE id = ?",
                         (art_id,)).fetchone()[0]
        e = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (art_id, art_id)).fetchone()[0]
    assert n == 0 and e == 0

    # ...but REVERSIBLE inside the window: the tombstone snapshot recovers.
    tomb_id = stats["tombstones"][art_id]
    _ledger, tombs = graph._governance_stores()
    snap = tombs.recover(tomb_id)
    assert snap and "Fish trade dossier" in snap["content"]
    assert snap.get("edges"), "edge connectivity snapshot missing"

    # The live-linked article survived.
    assert await graph.get_node(keeper.id) is not None

    # Past the retention window → HARD delete: the snapshot itself is purged.
    stats2 = await wiki.prune(now=far + 15 * 86400)
    assert stats2["hard_deleted"] == 1
    _ledger2, tombs2 = graph._governance_stores()
    assert tombs2.recover(tomb_id) is None  # irrecoverable — true deletion


async def test_prune_dry_run_touches_nothing(graph):
    ids, _lone = await _seed(graph)
    wiki = MemoryWiki(graph)
    await wiki.consolidate(_str_llm(json.dumps({
        "title": "Seagate Harbour", "markdown": "# S\nDossier.",
        "contradicted_sources": []})))
    art_id = _article_rows(graph)[0][0]
    for nid in ids:
        await graph.remove_node(nid)
    stats = await wiki.prune(now=time.time() + 400 * 86400, dry_run=True)
    assert art_id in stats["would_prune"]
    assert stats["tombstoned"] == 0
    assert await graph.get_node(art_id) is not None


async def test_prune_reinforced_article_survives(graph):
    ids, _lone = await _seed(graph)
    wiki = MemoryWiki(graph)
    await wiki.consolidate(_str_llm(json.dumps({
        "title": "Seagate Harbour",
        "markdown": "# Seagate Harbour\nFish traders harbour dossier.",
        "contradicted_sources": []})))
    art_id = _article_rows(graph)[0][0]
    for nid in ids:
        await graph.remove_node(nid)
    far = time.time() + 400 * 86400
    # A recall near the prune horizon reinforces the article → it survives.
    graph._reinforce_nodes([art_id], now=far - 3600)
    stats = await wiki.prune(now=far)
    assert stats["tombstoned"] == 0
    assert await graph.get_node(art_id) is not None


# ─── lint ────────────────────────────────────────────────────────────────────

async def test_lint_findings(graph):
    ids, _lone = await _seed(graph)
    wiki = MemoryWiki(graph)
    await wiki._write_article(
        "Lonely Page",
        "See [[Nowhere Land]] for the missing region and its many rumours.",
        [],
    )
    await wiki._write_article("Tiny", "stub", [])
    # a live node contradicted by an article
    art = await wiki._write_article(
        "Corrections", "# Corrections\nThe tax rumor was overstated, per audit.",
        [], contradicted=[],
    )
    wiki._merge_node_metadata(ids[0], {"superseded_by": art.id, "stale": True})

    findings = await wiki.lint(now=time.time() + 40 * 86400)
    kinds = {f["kind"] for f in findings}
    assert "orphan_link" in kinds
    assert "empty_article" in kinds
    assert "stale_article" in kinds
    assert "contradiction" in kinds
    orphan = next(f for f in findings if f["kind"] == "orphan_link")
    assert orphan["link"] == "Nowhere Land"


async def test_lint_optional_llm_pass(graph):
    wiki = MemoryWiki(graph)
    await wiki._write_article(
        "Solo", "A perfectly reasonable article about nothing much at all.", [])

    def audit_llm(prompt):
        if not isinstance(prompt, str):
            raise TypeError
        return '[{"kind": "gap", "article": "solo", "message": "No sources cited"}]'

    findings = await wiki.lint(llm=audit_llm)
    assert any(f["kind"] == "gap" and f["message"] == "No sources cited"
               for f in findings)


async def test_lint_wikilinks_between_articles_resolve(graph):
    wiki = MemoryWiki(graph)
    await wiki._write_article(
        "Trade Routes", "The coastal shipping lanes carry salted fish north.", [])
    await wiki._write_article(
        "Seagate Harbour", "The port at the heart of [[Trade Routes]] commerce.", [])
    findings = await wiki.lint()
    assert not any(f["kind"] == "orphan_link" for f in findings)


# ─── default-off: zero behaviour change ──────────────────────────────────────

async def test_default_off_zero_behavior_change(graph):
    await _seed(graph)
    with graph._connect() as conn:
        before_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        before_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    MemoryWiki(graph)  # constructing the wiki performs zero writes
    with graph._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before_nodes
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == before_edges
    assert _article_rows(graph) == []
    # plain graph search is unaffected by the wiki module's existence
    res = await graph.search("seagate fish traders", limit=5)
    assert res
