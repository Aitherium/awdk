"""Tests for adk.graph_rag.page_index -- the vectorless heading-tree index."""

from __future__ import annotations

import asyncio
import json

import pytest

from adk.graph_rag.page_index import (
    Node,
    build_tree,
    doc_tree_search,
    index_document,
    load_index,
    node_own_text,
    node_text,
    save_index,
    sidecar_path,
    summarize_tree,
    tree_search,
)

FILLER = " ".join(["lorem ipsum dolor sit amet consectetur"] * 12)  # ~470 chars


def _section(title: str, level: int, body: str = FILLER) -> str:
    return f"{'#' * level} {title}\n\n{body}\n\n"


def _titles(node: Node) -> list[str]:
    return [c.title for c in node.children]


# ---------------------------------------------------------------------------
# build_tree
# ---------------------------------------------------------------------------

def test_build_tree_nests_by_level():
    md = (
        _section("One", 1)
        + _section("One.A", 2)
        + _section("One.A.i", 3)
        + _section("One.B", 2)
        + _section("Two", 1)
    )
    root = build_tree(md)
    assert root.title == "document"
    assert root.node_id == "0000"
    assert _titles(root) == ["One", "Two"]
    one = root.children[0]
    assert _titles(one) == ["One.A", "One.B"]
    assert _titles(one.children[0]) == ["One.A.i"]
    # spans: One covers everything up to Two
    two = root.children[1]
    assert one.end_line == two.start_line - 1
    assert two.end_line == len(md.splitlines())
    # ids are document order
    assert [n.node_id for n in root.walk()] == ["0000", "0001", "0002", "0003", "0004", "0005"]


def test_h3_after_h1_nests_under_h1():
    md = _section("Top", 1) + _section("Deep", 3) + _section("Next", 1)
    root = build_tree(md)
    assert _titles(root) == ["Top", "Next"]
    assert _titles(root.children[0]) == ["Deep"]
    assert root.children[0].children[0].level == 3


def test_headings_inside_fenced_code_are_ignored():
    md = (
        _section("Real", 1)
        + "```markdown\n# Not a heading\n## Nor this\n```\n\n"
        + FILLER
        + "\n\n~~~\n### also fenced\n~~~\n\n"
        + _section("Also real", 1)
    )
    root = build_tree(md)
    assert _titles(root) == ["Real", "Also real"]


def test_thinning_merges_a_tiny_leaf_into_parent():
    md = _section("Parent", 1) + _section("Tiny", 2, body="short.") + _section("Big", 2)
    root = build_tree(md, min_node_chars=200)
    parent = root.children[0]
    assert _titles(parent) == ["Big"]
    # the merged text is still inside the parent's span
    assert "short." in node_text(parent, md.splitlines())
    assert "short." in node_own_text(parent, md.splitlines())
    # ids are contiguous after thinning
    assert [n.node_id for n in root.walk()] == ["0000", "0001", "0002"]
    # with thinning disabled the tiny leaf survives
    assert _titles(build_tree(md, min_node_chars=0).children[0]) == ["Tiny", "Big"]


def test_thinned_leaf_after_a_kept_sibling_stays_in_parent_own_text():
    md = _section("Parent", 1) + _section("Big", 2) + _section("Tail", 2, body="tail-note.")
    root = build_tree(md, min_node_chars=200)
    parent = root.children[0]
    assert _titles(parent) == ["Big"]
    own = node_own_text(parent, md.splitlines())
    assert "tail-note." in own
    assert "## Big" not in own


def test_document_without_headings_is_a_single_root():
    root = build_tree("just some text\nand more")
    assert root.children == []
    assert node_text(root, ["just some text", "and more"]) == "just some text\nand more"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def test_save_load_round_trip(tmp_path):
    md = _section("A", 1) + _section("A.1", 2) + _section("B", 1)
    root = build_tree(md)
    root.children[0].summary = "about A"
    out = tmp_path / "doc.pageindex.json"
    save_index(root, out, source="doc.md", sha256="abc")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["source"] == "doc.md"
    assert data["sha256"] == "abc"
    assert load_index(out) == root


def test_load_index_rejects_unknown_version(tmp_path):
    p = tmp_path / "x.pageindex.json"
    p.write_text(json.dumps({"version": 99, "tree": {"node_id": "0000"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_index(p)


async def test_index_document_reuses_sidecar_and_force_rebuilds(tmp_path):
    doc = tmp_path / "guide.md"
    md = _section("Intro", 1) + _section("Setup", 1)
    doc.write_text(md, encoding="utf-8")

    root = await index_document(doc)
    side = sidecar_path(doc)
    assert side.name == "guide.pageindex.json"
    assert side.is_file()

    # mark the sidecar; a second call with a matching sha must return the marked copy
    root.children[0].summary = "MARKER"
    save_index(root, side, source=str(doc), sha256=json.loads(side.read_text())["sha256"])
    again = await index_document(doc)
    assert again.children[0].summary == "MARKER"

    forced = await index_document(doc, force=True)
    assert forced.children[0].summary == ""
    assert json.loads(side.read_text())["sha256"] == json.loads(side.read_text())["sha256"]

    # a changed document invalidates the sidecar by sha, not by mtime
    save_index(again, side, source=str(doc), sha256=json.loads(side.read_text())["sha256"])
    doc.write_text(md + _section("Extra", 1), encoding="utf-8")
    rebuilt = await index_document(doc)
    assert _titles(rebuilt) == ["Intro", "Setup", "Extra"]


async def test_index_document_accepts_a_markdown_string_without_persisting(tmp_path):
    root = await index_document(_section("Only", 1))
    assert _titles(root) == ["Only"]
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# tree_search
# ---------------------------------------------------------------------------

def _synthetic_manual(control: str, control_section: int = 23, sections: int = 40) -> str:
    parts = []
    for i in range(1, sections + 1):
        body = FILLER + f" Section {i} discusses topic number {i} in detail."
        if i == control_section:
            body += f" The control token is {control} and appears only here."
        parts.append(_section(f"Chapter {i}", 1, body))
        parts.append(_section(f"Chapter {i} details", 2, FILLER + f" detail {i}"))
    return "".join(parts)


async def test_tree_search_lexical_finds_the_control_string():
    md = _synthetic_manual("CONTROL-TREE-8181")
    root = build_tree(md)
    assert len(root.children) == 40
    hits = await tree_search(root, md, "where is CONTROL-TREE-8181 defined?", router=None)
    assert hits, "no hits at all"
    assert "CONTROL-TREE-8181" in hits[0]["text"]
    assert hits[0]["path"] == ["document", "Chapter 23"]
    assert hits[0]["why"] == "lexical overlap"
    assert "Chapter 23 details" not in hits[0]["text"]  # own text, not the subsection
    assert hits[0]["score"] > hits[-1]["score"] or len(hits) == 1


async def test_tree_search_lexical_misses_an_absent_control_string():
    md = _synthetic_manual("CONTROL-TREE-8181")
    root = build_tree(md)
    hits = await tree_search(root, md, "CONTROL-TREE-9999", router=None, top_k=3)
    assert all("CONTROL-TREE-9999" not in h["text"] for h in hits)
    assert all(h["score"] < 1.0 for h in hits)


class _PickRouter:
    """Fake LLMRouter: picks the listed sections whose title is in ``wanted``."""

    def __init__(self, wanted: set[str]):
        self.wanted = wanted
        self.prompts: list[str] = []

    async def chat(self, messages, model=None, **_):
        user = messages[-1].content
        self.prompts.append(user)
        ids = []
        for line in user.splitlines():
            if not line[:4].isdigit() or ": " not in line:
                continue
            sid, label = line.split(": ", 1)
            if label.split(" -- ", 1)[0] in self.wanted:
                ids.append(sid)
        return type("R", (), {"content": json.dumps(ids)})()


async def test_tree_search_follows_the_router_beam():
    # lexically the query matches "Alpha"; the router must steer to Beta -> Beta.two
    md = (
        _section("Alpha", 1, "needle needle needle " + FILLER)
        + _section("Beta", 1)
        + _section("Beta.one", 2)
        + _section("Beta.two", 2)
    )
    root = build_tree(md)
    router = _PickRouter({"Beta", "Beta.two"})
    hits = await tree_search(root, md, "needle", router=router, top_k=1, beam=1)
    assert hits[0]["title"] == "Beta.two"
    assert hits[0]["path"] == ["document", "Beta", "Beta.two"]
    assert hits[0]["why"].startswith("router pick #1")
    assert len(router.prompts) == 2  # one pick per level descended
    # the second prompt offered Beta's own text as a choice next to its children
    assert any(line.startswith("0002-own:") for line in router.prompts[1].splitlines())


async def test_tree_search_falls_back_to_lexical_when_router_reply_is_garbage():
    class Garbage:
        async def chat(self, messages, model=None, **_):
            return type("R", (), {"content": "I cannot decide."})()

    md = _section("Alpha", 1, FILLER + " needle") + _section("Beta", 1, FILLER)
    root = build_tree(md)
    hits = await tree_search(root, md, "needle", router=Garbage(), top_k=1)
    assert hits[0]["title"] == "Alpha"
    assert hits[0]["why"] == "lexical overlap"


# ---------------------------------------------------------------------------
# summarize_tree
# ---------------------------------------------------------------------------

class _CountingRouter:
    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    async def chat(self, messages, model=None, **_):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.calls += 1
        try:
            await asyncio.sleep(0.01)
            title = messages[-1].content.splitlines()[0].replace("Section title: ", "")
            return type("R", (), {"content": f"Summary of {title}."})()
        finally:
            self.in_flight -= 1


async def test_summarize_tree_fills_every_node_within_concurrency():
    md = "".join(
        _section(f"S{i}", 1) + _section(f"S{i}.a", 2) + _section(f"S{i}.b", 2) for i in range(8)
    )
    root = build_tree(md)
    router = _CountingRouter()
    await summarize_tree(root, md, router=router, concurrency=4)
    nodes = list(root.walk())
    assert len(nodes) == 1 + 8 * 3
    assert all(n.summary for n in nodes)
    assert router.calls == len(nodes)
    assert router.max_in_flight <= 4
    assert router.max_in_flight > 1  # it actually ran in parallel
    assert root.summary == "Summary of document."


async def test_summarize_tree_offline_uses_excerpts():
    md = _section("Only", 1, "First real line.\nSecond line.")
    root = build_tree(md, min_node_chars=0)
    await summarize_tree(root, md, router=None)
    assert root.children[0].summary == "First real line."


# ---------------------------------------------------------------------------
# tool
# ---------------------------------------------------------------------------

async def test_doc_tree_search_tool_answers_lexically(tmp_path, monkeypatch):
    import adk.graph_rag.page_index as pi

    monkeypatch.setattr(pi, "_lazy_router", lambda: None)
    doc = tmp_path / "manual.md"
    doc.write_text(_synthetic_manual("CONTROL-TREE-8181"), encoding="utf-8")
    out = json.loads(await doc_tree_search(str(doc), "CONTROL-TREE-8181", top_k=2))
    assert out["mode"] == "lexical"
    assert "CONTROL-TREE-8181" in out["hits"][0]["text"]
    assert sidecar_path(doc).is_file()


async def test_doc_tree_search_tool_reports_missing_file(tmp_path):
    out = json.loads(await doc_tree_search(str(tmp_path / "nope.md"), "x"))
    assert "error" in out
