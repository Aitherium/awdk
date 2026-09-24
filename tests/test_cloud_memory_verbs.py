"""`adk embed` / `adk kb` / `adk memory` reach the tenant platform.

Before: cli.py had no embed/kb/memory verb at all — only `adk ingest`, which writes
a LOCAL graph — so a customer had no CLI path to Genesis /embeddings/* or the
tenant memory graph.
"""
from __future__ import annotations

import sys

import pytest

import adk.cloud_memory as cm


def _run(monkeypatch, argv, responses):
    seen = []

    def fake_post(path, body):
        seen.append((path, body))
        return responses.get(path, (404, {"detail": "nope"}))

    monkeypatch.setenv("AITHER_API_KEY", "test-key")
    monkeypatch.setattr(cm, "_http_post", fake_post)
    monkeypatch.setattr(sys, "argv", ["adk", *argv])
    from adk import cli

    with pytest.raises(SystemExit) as ei:
        cli.main()
    return ei.value.code, seen


def test_embed_verb_posts_to_embeddings_embed(monkeypatch, capsys):
    code, seen = _run(monkeypatch, ["embed", "hello", "world", "--dim", "256"],
                      {"/embeddings/embed": (200, {"embedding": [0.1] * 256, "modality": "text"})})
    assert code == 0
    assert seen == [("/embeddings/embed", {"text": "hello world", "modality": "text", "dim": 256})]
    assert "dim=256" in capsys.readouterr().out


def test_kb_ingest_sends_no_tenant_id(monkeypatch, tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("our refund policy is 30 days", encoding="utf-8")
    code, seen = _run(monkeypatch, ["kb", "ingest", str(f)],
                      {"/external/ingest": (200, {"status": "ingested", "node_id": "n1",
                                                  "source": "notes.md"})})
    assert code == 0
    path, body = seen[0]
    # The SAME store `kb query` reads (/external/graph/query + ingested.jsonl);
    # the round trip against the real router is test_external_kb_memory_roundtrip.py.
    assert path == cm.KB_INGEST_ROUTE == "/external/ingest"
    assert body["source_name"] == "notes.md" and "refund" in body["content"]
    assert "tenant_id" not in body


def test_kb_query_uses_graph_query_and_memory_recall_uses_recall(monkeypatch, capsys):
    resp = {"/external/graph/query": (200, {"results": [{"snippet": "refunds: 30 days",
                                                         "type": "document"}]}),
            "/external/memory/recall": (200, {"memories": [{"content": "refunds: 30 days"}]})}
    code, seen = _run(monkeypatch, ["kb", "query", "refund", "policy"], resp)
    assert code == 0 and seen[0] == (cm.KB_QUERY_ROUTE, {"query": "refund policy", "max_results": 10})
    assert "refunds: 30 days" in capsys.readouterr().out
    code, seen = _run(monkeypatch, ["kb", "query", "refund", "--category", "spec"], resp)
    assert code == 0 and "(no matches)" in capsys.readouterr().out
    code, seen = _run(monkeypatch, ["memory", "recall", "refund"], resp)
    assert code == 0 and seen[0][0] == "/external/memory/recall"


def test_memory_remember(monkeypatch):
    code, seen = _run(monkeypatch, ["memory", "remember", "ship", "friday", "--category", "ops"],
                      {"/external/memory/remember": (200, {"status": "remembered", "memory_id": "m1"})})
    assert code == 0
    assert seen == [("/external/memory/remember", {"content": "ship friday", "category": "ops"})]


def test_server_error_exits_nonzero(monkeypatch, capsys):
    code, _ = _run(monkeypatch, ["memory", "recall", "x"],
                   {"/external/memory/recall": (403, {"detail": "tenant mismatch"})})
    assert code == 1
    assert "403" in capsys.readouterr().err


def test_missing_api_key_refuses_without_calling(monkeypatch):
    monkeypatch.delenv("AITHER_API_KEY", raising=False)
    called = []
    rc = cm.cmd_memory(type("A", (), {"memory_command": "recall", "query": ["x"],
                                      "category": "", "json": False})(),
                       poster=lambda p, b: called.append(p) or (200, {}))
    assert rc == 1 and called == []
