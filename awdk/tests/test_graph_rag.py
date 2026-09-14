"""Tests for the graph-RAG service-pack: parsers, builder, retriever, governance."""

import hashlib
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from adk.graph_rag import GraphStore, ingest_corpus, open_retriever
from adk.graph_rag.bounded_retriever import CorpusGraphRetriever
from adk.graph_rag.config import NodeType
from adk.graph_rag.governance import (
    ConflictDetector,
    MutationLedger,
    MutationType,
    StableNodeID,
    TombstoneStore,
)
from adk.graph_rag.graph_builder import build_graph
from adk.graph_rag.corpus_loader import load_corpus
from adk.graph_rag.parsers.markdown_parser import parse_markdown
from adk.graph_rag.parsers.sql_parser import parse_sql
from adk.graph_rag.parsers.typescript_parser import parse_typescript
from adk.graph_retriever import GraphEdge, GraphNode

_DIM = 48


async def _embed(text: str) -> list[float]:
    """Deterministic hashed embedding (mirrors the runtime offline fallback)."""
    vec = [0.0] * _DIM
    for tok in (text.lower().split() or [""]):
        h = hashlib.sha256(tok.encode()).digest()
        for i in range(0, len(h), 4):
            idx = struct.unpack("<I", h[i:i + 4])[0] % _DIM
            vec[idx] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _write_corpus(root: Path) -> None:
    (root / "agents" / "priya").mkdir(parents=True)
    (root / "agents" / "nidhi").mkdir(parents=True)
    (root / "standards").mkdir(parents=True)
    (root / "migrations").mkdir(parents=True)
    (root / "agents" / "priya" / "rule.md").write_text(
        "# Leave Rules\nPriya approves leave. See [approval](../../standards/approval.md).\n"
        "## Escalation\nEscalate big requests to the CEO.\n", encoding="utf-8")
    (root / "agents" / "priya" / "handoff.md").write_text(
        "# Handoff\nFinance goes to [nidhi](../nidhi/rule.md).\n", encoding="utf-8")
    (root / "agents" / "nidhi" / "rule.md").write_text(
        "# Finance Rules\nNidhi handles invoices and payroll.\n", encoding="utf-8")
    (root / "standards" / "approval.md").write_text(
        "# Approval\nActions flow through global_pending_approvals.\n", encoding="utf-8")
    (root / "migrations" / "001_init.sql").write_text(
        "CREATE TABLE global_pending_approvals (id serial primary key, agent_id text);\n"
        "ALTER TABLE global_pending_approvals ENABLE ROW LEVEL SECURITY;\n", encoding="utf-8")
    (root / "migrations" / "002_outbox.sql").write_text(
        "CREATE TABLE global_outbox (id serial primary key, "
        "approval_id integer REFERENCES global_pending_approvals(id));\n", encoding="utf-8")
    (root / "functions" if False else root / "fn").mkdir(parents=True)  # noqa: SIM115
    (root / "fn" / "server.ts").write_text(
        "import { x } from './util.ts';\nexport async function handle(req) { return x(req); }\n",
        encoding="utf-8")


# ── parsers ────────────────────────────────────────────────────────────────

def test_markdown_parser_emits_sections_agent_and_links():
    md = ("# Leave Rules\nApprove leave. See [approval](../../standards/approval.md).\n"
          "## Escalation\nEscalate to CEO.\n")
    res = parse_markdown("agents/priya/rule.md", md)
    types = {n.type for n in res.nodes}
    assert NodeType.FILE in types
    assert NodeType.SECTION in types
    assert NodeType.AGENT in types  # path under agents/<name>/
    # the relative link resolved to a references edge to the standards file
    assert any(e.relation == "references" and e.dst_key == "file:standards/approval.md"
               for e in res.edges)
    # heading nesting: escalation is contained by leave-rules
    assert any(e.relation == "contains"
               and e.dst_key == "section:agents/priya/rule.md#escalation"
               for e in res.edges)


def test_sql_parser_tables_columns_fk_rls():
    sql = ("CREATE TABLE global_outbox (id serial primary key, "
           "approval_id integer REFERENCES global_pending_approvals(id));\n"
           "ALTER TABLE global_outbox ENABLE ROW LEVEL SECURITY;\n")
    res = parse_sql("migrations/002.sql", sql)
    tables = {n.key for n in res.nodes if n.type == NodeType.TABLE}
    assert "table:global_outbox" in tables
    cols = {n.key for n in res.nodes if n.type == NodeType.COLUMN}
    assert "column:global_outbox.id" in cols
    assert "column:global_outbox.approval_id" in cols
    assert any(e.relation == "depends_on" and e.dst_key == "table:global_pending_approvals"
               for e in res.edges)
    outbox = next(n for n in res.nodes if n.key == "table:global_outbox")
    assert outbox.metadata.get("rls") is True


def test_typescript_parser_functions_and_relative_imports():
    ts = "import { x } from './util.ts';\nexport async function handle(req) {}\n"
    res = parse_typescript("fn/server.ts", ts)
    assert any(n.type == NodeType.FUNCTION and n.title == "handle" for n in res.nodes)
    assert any(e.relation == "depends_on" and e.dst_key == "file:fn/util.ts" for e in res.edges)


# ── governance ───────────────────────────────────────────────────────────────

def test_conflict_detector_classifies_update_vs_contradiction():
    det = ConflictDetector(similarity_threshold=0.85)
    assert det.detect("n", {"content": "same"}, {"content": "same"}) is None
    # polarity flip at high textual similarity → contradiction
    v = det.detect("n", {"content": "Priya approves leave"},
                   {"content": "Priya does not approve leave"},
                   before_embedding=[1.0, 0.0], after_embedding=[0.99, 0.01])
    assert v is not None and v.kind == "contradiction"
    # genuine rewording, no negation/number change, high sim → update
    v2 = det.detect("n", {"content": "alpha beta"}, {"content": "alpha beta gamma"},
                    before_embedding=[1.0, 0.0], after_embedding=[1.0, 0.0])
    assert v2 is not None and v2.kind == "update"


def test_ledger_records_and_indexes_by_node(tmp_path):
    led = MutationLedger(tmp_path / "l.jsonl")
    led.record(MutationType.STORE, node_id="a", reason="new")
    led.record(MutationType.SUPERSEDE, node_id="a", reason="changed")
    led.record(MutationType.STORE, node_id="b")
    assert [m.mutation_type for m in led.by_node("a")] == [MutationType.STORE, MutationType.SUPERSEDE]
    # durable: a fresh ledger over the same file sees the history
    assert MutationLedger(tmp_path / "l.jsonl").stats()["total"] == 3


def test_tombstone_recover_roundtrip(tmp_path):
    store = TombstoneStore(tmp_path / "t.jsonl")
    tid = store.entomb({"id": "x", "content": "gone"}, reason="removed")
    assert store.recover(tid) == {"id": "x", "content": "gone"}
    assert store.recover("missing") is None


def test_stable_ids_persist_and_reuse(tmp_path):
    sid = StableNodeID(tmp_path / "ids.json")
    a = sid.id_for("section:foo#bar")
    sid.mark_seen("section:foo#bar", "v1")
    sid.save()
    sid2 = StableNodeID(tmp_path / "ids.json")
    assert sid2.id_for("section:foo#bar") == a  # same id across runs
    assert sid2.content_hash("section:foo#bar") != ""  # remembers content


# ── retriever ────────────────────────────────────────────────────────────────

async def _store_with(nodes: list[tuple[str, str, str]]) -> GraphStore:
    """nodes: list of (id, namespace, text)."""
    store = GraphStore()
    for nid, ns, text in nodes:
        emb = await _embed(text)
        store.add_node(GraphNode(id=nid, content=text, score=0.0,
                                 metadata={"namespace": ns, "type": "section",
                                           "title": nid, "natural_key": nid, "embedding": emb}))
    return store


async def test_retriever_floor_drops_offtopic_and_filters_namespace():
    store = await _store_with([
        ("approval", "codebase", "approval workflow gateway pending approvals"),
        ("leave", "business", "leave balance vacation holiday entitlement"),
    ])
    store.add_edge(GraphEdge(src="approval", dst="leave", relation="related"))
    r = CorpusGraphRetriever(store, _embed, floor=0.05)
    sg = await r.subgraph_search("approval workflow gateway", namespace="codebase",
                                 k_seeds=3, k_hops=0, limit=5)
    ids = {n.id for n in sg.nodes}
    assert "approval" in ids       # on-topic, right namespace
    assert "leave" not in ids      # other namespace filtered out


async def test_retriever_bfs_expands_and_is_deterministic():
    store = await _store_with([
        ("a", "codebase", "approval workflow gateway"),
        ("b", "codebase", "totally unrelated zzz quux"),
    ])
    store.add_edge(GraphEdge(src="a", dst="b", relation="contains"))
    r = CorpusGraphRetriever(store, _embed, floor=0.05)
    sg1 = await r.subgraph_search("approval workflow", k_seeds=1, k_hops=1, limit=5)
    ids = {n.id for n in sg1.nodes}
    assert ids == {"a", "b"}                       # b reached via 1-hop expansion
    assert any(e.relation == "contains" for e in sg1.edges)
    # deterministic rendering
    r2 = CorpusGraphRetriever(store, _embed, floor=0.05)
    sg2 = await r2.subgraph_search("approval workflow", k_seeds=1, k_hops=1, limit=5)
    assert sg1.to_context() == sg2.to_context()


# ── end-to-end ingest + governance over re-ingestion ─────────────────────────

async def test_ingest_corpus_roundtrip_and_governed_reingest(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_corpus(corpus)
    graph = tmp_path / "index" / "g.graph.json"

    stats = await ingest_corpus(str(corpus), str(graph), embedder=_embed, namespace="codebase")
    assert stats["files"] >= 7
    assert stats["nodes"] > 10
    assert stats["ledger"]["by_type"]["store"] == stats["nodes"]
    assert graph.exists()

    # retriever loads from disk and answers
    r = open_retriever(str(graph), embedder=_embed)
    sg = await r.subgraph_search("approval global_pending_approvals", namespace="codebase",
                                 k_seeds=4, k_hops=1, limit=12)
    assert sg.nodes
    block = sg.to_context()
    assert "# Facts" in block

    # edit a rule and remove a section → supersede(+conflict) and forget(+tombstone)
    (corpus / "agents" / "priya" / "rule.md").write_text(
        "# Leave Rules\nPriya does NOT approve leave; only the CEO can.\n", encoding="utf-8")
    stats2 = await ingest_corpus(str(corpus), str(graph), embedder=_embed, namespace="codebase")
    by_type = stats2["ledger"]["by_type"]
    assert by_type.get("supersede", 0) >= 1
    assert by_type.get("forget", 0) >= 1            # the removed "Escalation" section
    assert any(c["kind"] == "contradiction" for c in stats2["conflicts"])


async def test_namespaces_do_not_cross_tombstone(tmp_path):
    """Ingesting a second namespace must not forget the first's nodes."""
    c1 = tmp_path / "c1"
    c1.mkdir()
    (c1 / "a.md").write_text("# A\nalpha content here\n", encoding="utf-8")
    c2 = tmp_path / "c2"
    c2.mkdir()
    (c2 / "b.md").write_text("# B\nbeta content here\n", encoding="utf-8")
    graph = tmp_path / "g.json"

    await ingest_corpus(str(c1), str(graph), embedder=_embed, namespace="codebase")
    s2 = await ingest_corpus(str(c2), str(graph), embedder=_embed, namespace="company-facts")
    # second run must not have tombstoned the first namespace's nodes
    assert s2["ledger"]["by_type"].get("forget", 0) == 0
    store = GraphStore.load(str(graph))
    assert len(store.nodes_in_namespace("codebase")) >= 2
    assert len(store.nodes_in_namespace("company-facts")) >= 2
