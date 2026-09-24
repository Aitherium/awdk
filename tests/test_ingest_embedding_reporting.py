"""adk ingest reports embeddings truthfully and honours --skip-embeddings.

Before: ``skip_embeddings`` was accepted and never read, ``chunks_embedded``
was bumped per chunk BEFORE anything was embedded, and ``embedding_degraded``
was never assigned.
"""

from __future__ import annotations

import pytest

import adk.embeddings as emb
from adk.ingest import ingest_files


class _FakeProvider:
    def __init__(self, degraded: bool) -> None:
        self.degraded = degraded
        self.calls = 0

    async def embed_one(self, text: str):
        self.calls += 1
        return [0.1] * 8


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AITHER_GRAPH_EMBEDDER", raising=False)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("alpha " * 50)
    (src / "b.md").write_text("beta " * 50)
    return src


def _install(monkeypatch, provider):
    monkeypatch.setattr(emb, "get_provider", lambda: provider)
    monkeypatch.setattr(emb, "get_default_embedder", lambda: provider.embed_one)


@pytest.mark.asyncio
async def test_skip_embeddings_stores_without_vectors(corpus, monkeypatch):
    provider = _FakeProvider(degraded=True)
    _install(monkeypatch, provider)
    result = await ingest_files(corpus, skip_embeddings=True, agent_name="skip")
    assert result.chunks_created == 2
    assert not result.errors
    assert provider.calls == 0, "skip_embeddings must not call the embedder"
    assert result.chunks_embedded == 0
    assert result.embedding_degraded is False


@pytest.mark.asyncio
async def test_chunks_embedded_counts_real_vectors_and_reports_degraded(corpus, monkeypatch):
    provider = _FakeProvider(degraded=True)
    _install(monkeypatch, provider)
    result = await ingest_files(corpus, agent_name="embed")
    assert result.chunks_created == 2
    assert result.chunks_embedded == 2
    assert result.embedding_degraded is True


@pytest.mark.asyncio
async def test_empty_vectors_are_not_counted(corpus, monkeypatch):
    class _Empty(_FakeProvider):
        async def embed_one(self, text: str):
            return []

    _install(monkeypatch, _Empty(degraded=False))
    result = await ingest_files(corpus, agent_name="empty")
    assert result.chunks_created == 2
    assert result.chunks_embedded == 0
