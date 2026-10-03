"""`adk models use <embedder>` saves the space; adk.embeddings and the faculty embedder follow it.

Until 2026-10-02 the saved ``embed_space`` / ``embeddings_url`` keys were written and never
read, so a customer who ran ``adk models use aither-code-embed`` stayed in the 768-d nomic
space unless they also set an env var by hand.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

_ENV = ("AITHER_EMBED_SPACE", "AITHER_EMBED_MODEL", "AITHER_EMBEDDINGS_URL")


@pytest.fixture
def load(monkeypatch):
    """``load(saved, **env)`` -> (adk.embeddings, adk.faculties.embeddings) reloaded."""
    import adk.config as cfg
    import adk.embeddings as emb
    import adk.faculties.embeddings as fac

    def _load(saved=None, **env):
        for k in _ENV:
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(cfg, "load_saved_config", lambda *a, **k: dict(saved or {}))
        return importlib.reload(emb), importlib.reload(fac)

    yield _load
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cfg, "load_saved_config", lambda *a, **k: {})
    importlib.reload(emb)
    importlib.reload(fac)


SAVED = {"embed_space": "aither-code-embed", "embeddings_model": "aither-code-embed",
         "embeddings_url": "http://127.0.0.1:8229/v1"}


def test_nothing_saved_is_the_old_nomic_space(load):
    emb, fac = load()
    assert emb.EMBED_SPACE == "nomic" and emb.CANONICAL_DIM == 768
    assert fac._EMBEDDING_DIM == 768 and fac._SERVED_MODEL == "nomic-embed-text"
    assert fac._FEATURE_HASH_DIM == 768


def test_the_saved_space_and_url_are_followed(load):
    emb, fac = load(SAVED)
    assert emb.EMBED_SPACE == "aither-code-embed" and emb.CANONICAL_DIM == 1024
    assert emb._explicit_url() == "http://127.0.0.1:8229", "the saved /v1 base is normalised"
    assert fac._EMBEDDING_DIM == 1024 and fac._SERVED_MODEL == "aither-code-embed"
    assert fac._FEATURE_HASH_DIM == 384, "a hash must never pass as a 1024-d vector"


def test_the_env_still_wins_over_the_saved_choice(load):
    emb, _ = load(SAVED, AITHER_EMBED_SPACE="nomic", AITHER_EMBEDDINGS_URL="http://x:9")
    assert emb.EMBED_SPACE == "nomic" and emb._explicit_url() == "http://x:9"


def test_an_unknown_saved_space_falls_back_but_an_env_typo_still_refuses(load):
    emb, _ = load({"embed_space": "some-future-embedder"})
    assert emb.EMBED_SPACE == "nomic", "a newer catalogue id must not crash every write"
    with pytest.raises(ValueError):
        load(AITHER_EMBED_SPACE="nomic-v2")


def test_the_faculty_drops_wrong_width_vectors_in_the_code_embed_space(load, monkeypatch):
    _, fac = load(SAVED)
    p = fac.EmbeddingProvider()
    tried = []

    async def ollama(texts):
        tried.append("ollama")
        return [[0.1] * 768 for _ in texts]          # a leftover nomic model answered

    async def elysium(texts):
        tried.append("elysium")
        return [[0.2] * 1024 for _ in texts]

    monkeypatch.setattr(p, "_embed_ollama", ollama)
    monkeypatch.setattr(p, "_embed_elysium", elysium)
    monkeypatch.setattr(p, "_try_load_sentence_transformers",
                        lambda: pytest.fail("the local nomic model must not load here"))
    out = asyncio.run(p.embed_batch(["a", "b"]))
    assert tried == ["ollama", "elysium"] and all(len(v) == 1024 for v in out)
