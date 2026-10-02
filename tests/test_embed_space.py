"""adk.embeddings follows env AITHER_EMBED_SPACE; GraphMemory never mixes two spaces.

awdk ships without the AitherOS ``lib`` package, so the space mapping is restated in
``adk/embeddings.py``. The nomic space (env unset) must be exactly the pre-switch pins.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

_ENV = (
    "AITHER_EMBED_SPACE", "AITHER_EMBED_MODEL", "AITHER_EMBEDDINGS_URL", "AITHER_API_KEY",
    "AITHER_GATEWAY_EMBEDDINGS_URL", "AITHER_MICROSCHEDULER_URL", "AITHER_GRAPH_EMBEDDER",
)


@pytest.fixture
def emb(monkeypatch):
    """``emb(space="", **env)`` -> adk.embeddings reloaded under that env."""
    import adk.embeddings as mod

    for k in _ENV:
        monkeypatch.delenv(k, raising=False)

    def _enter(space="", **env):
        if space:
            monkeypatch.setenv("AITHER_EMBED_SPACE", space)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(mod)

    def _leave():
        for k in _ENV:
            monkeypatch.delenv(k, raising=False)
        return importlib.reload(mod)

    _enter.leave = _leave
    yield _enter
    _leave()


def _fake_httpx(monkeypatch, dim, seen):
    import httpx

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, n):
            self._n = n

        def json(self):
            # both wire shapes: OpenAI /v1/embeddings and Ollama /api/embeddings
            return {"data": [{"embedding": [0.1] * dim} for _ in range(self._n)],
                    "embedding": [0.1] * dim}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            seen.append((url, json))
            return _Resp(len(json.get("input") or [0]))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_nomic_space_is_unchanged(emb, monkeypatch):
    mod = emb()
    assert mod.EMBED_SPACE == "nomic"
    assert (mod.CANONICAL_MODEL, mod.CANONICAL_DIM, mod.CANONICAL_MAX_CHARS) == (
        "nomic-embed-text", 768, 0)
    assert mod._VLLM_EMBED_PORT == 8209
    monkeypatch.setattr(mod.os.path, "exists", lambda p: p == "/run/.containerenv")
    assert mod._local_vllm_host(8209) == "aither-vllm-embeddings:8209"

    seen = []
    _fake_httpx(monkeypatch, 768, seen)
    monkeypatch.setenv("AITHER_EMBEDDINGS_URL", "https://embed.test:8209")
    prov = mod.AdkEmbeddings()
    vecs, dim = asyncio.run(prov.embed_texts(["x" * 5000]))
    assert dim == 768 and not prov.degraded
    assert seen[-1][0] == "https://embed.test:8209/v1/embeddings"
    assert seen[-1][1] == {"input": ["x" * 5000], "model": "nomic-embed-text"}


@pytest.mark.parametrize(
    "alias", ["aither-code-embed", "code-embed", "ce1024", "aither-code-embed-1024", " CE1024 "]
)
def test_code_embed_space(emb, alias, monkeypatch):
    mod = emb(alias)
    assert mod.EMBED_SPACE == "aither-code-embed"
    assert (mod.CANONICAL_MODEL, mod.CANONICAL_DIM, mod.CANONICAL_MAX_CHARS) == (
        "aither-code-embed", 1024, 1013)
    assert mod._VLLM_EMBED_PORT == 8229
    monkeypatch.setattr(mod.os.path, "exists", lambda p: p == "/run/.containerenv")
    assert mod._local_vllm_host(8229) == "aither-vllm-code-embed:8229"

    seen = []
    _fake_httpx(monkeypatch, 1024, seen)
    monkeypatch.setenv("AITHER_EMBEDDINGS_URL", "http://ce.test:8229")
    prov = mod.AdkEmbeddings()
    vecs, dim = asyncio.run(prov.embed_texts(["x" * 5000, "short"]))
    assert dim == 1024 and not prov.degraded and len(vecs) == 2
    assert seen[-1][0] == "http://ce.test:8229/v1/embeddings"
    assert seen[-1][1] == {"input": ["x" * 1013, "short"], "model": "aither-code-embed"}
    assert prov.describe()["canonical_dim"] == 1024
    assert prov.describe()["space"] == "aither-code-embed"


def test_code_embed_space_never_pins_or_returns_a_768d_answer(emb, monkeypatch):
    """Every reachable rung still serves nomic: the answer is "no backend", not 768-d."""
    mod = emb("aither-code-embed")
    seen = []
    _fake_httpx(monkeypatch, 768, seen)
    monkeypatch.setenv("AITHER_EMBEDDINGS_URL", "https://byo.test:7000")
    monkeypatch.setattr(mod.AdkEmbeddings, "_probe_sentence_transformers",
                        lambda self: pytest.fail("the 384-d CPU rung is not in this space"))
    prov = mod.AdkEmbeddings()
    vecs, dim = asyncio.run(prov.embed_texts(["hello", "world"]))
    assert vecs == [[], []] and dim == 1024
    assert prov.backend == "none" and prov.degraded and prov.describe()["url"] == ""
    assert asyncio.run(prov.embed_one("hello")) == []
    urls = [u for u, _ in seen]
    assert "https://byo.test:7000/v1/embeddings" in urls
    assert not any("/api/embeddings" in u for u in urls)  # the nomic Ollama rung
    assert not any("nomic" in str(body.get("model")) for _, body in seen)


def test_nomic_space_still_pins_a_degraded_answer_and_probes_ollama(emb, monkeypatch):
    """Pre-switch behaviour, kept: another width is pinned and returned as degraded."""
    mod = emb()
    seen = []
    _fake_httpx(monkeypatch, 384, seen)
    monkeypatch.setenv("AITHER_EMBEDDINGS_URL", "https://embed.test:8209")
    prov = mod.AdkEmbeddings()
    vecs, dim = asyncio.run(prov.embed_texts(["hello"]))
    assert dim == 384 and len(vecs[0]) == 384 and prov.degraded and prov.backend == "vllm"

    prov = mod.AdkEmbeddings()
    assert asyncio.run(prov._try_ollama("http://localhost:11434")) is True
    assert seen[-1] == ("http://localhost:11434/api/embeddings",
                        {"model": "nomic-embed-text", "prompt": "ping"})


@pytest.mark.parametrize("stale", [
    "http://aither-vllm-embeddings:8209", "https://10.0.0.5:8209/v1",
    "https://aither-vllm-dgx-embed:9000", "http://dgx.test:8121",
])
def test_code_embed_space_replaces_a_retired_explicit_url(emb, monkeypatch, stale):
    mod = emb("aither-code-embed")
    seen = []
    _fake_httpx(monkeypatch, 1024, seen)
    monkeypatch.setenv("AITHER_EMBEDDINGS_URL", stale)
    assert mod._is_retired_url(stale) and mod._explicit_url() == mod._CODE_EMBED_DEFAULT_URL
    prov = mod.AdkEmbeddings()
    _vecs, dim = asyncio.run(prov.embed_texts(["hello"]))
    assert dim == 1024 and not prov.degraded
    assert [u for u, _ in seen] == ["http://aither-vllm-code-embed:8229/v1/embeddings"] * 2

    emb.leave()
    monkeypatch.setenv("AITHER_EMBEDDINGS_URL", stale)
    assert not mod._is_retired_url(stale) and mod._explicit_url() == stale  # nomic: live


# awdk also ships alone; the platform module is beside it only in the monorepo.
_AITHEROS_ROOT = Path(__file__).resolve().parents[2] / "AitherOS"
_HAS_PLATFORM_LIB = (_AITHEROS_ROOT / "lib" / "core" / "EmbeddingSpace.py").is_file()


@pytest.mark.skipif(not _HAS_PLATFORM_LIB, reason="AitherOS lib not present beside awdk")
def test_retired_markers_match_the_platform_module(emb):
    """Restated here because awdk ships without lib; pinned when lib is importable."""
    mod = emb("aither-code-embed")
    root = _AITHEROS_ROOT
    sys.path.insert(0, str(root))
    try:
        import lib.core.EmbeddingSpace as EmbedSpace

        es = importlib.reload(EmbedSpace)
        assert mod._RETIRED_URL_MARKERS == es.RETIRED_EMBED_URL_MARKERS
        assert mod._CODE_EMBED_DEFAULT_URL == es._DEFAULT_URL
        assert (mod.CANONICAL_MODEL, mod.CANONICAL_DIM) == (
            es.PLATFORM_EMBED_MODEL, es.PLATFORM_EMBED_DIM)
    finally:
        sys.path.remove(str(root))
        emb.leave()
        if "lib.core.EmbeddingSpace" in sys.modules:
            importlib.reload(sys.modules["lib.core.EmbeddingSpace"])


def test_code_embed_space_backend_that_changes_width_is_not_returned(emb, monkeypatch):
    mod = emb("aither-code-embed")
    _fake_httpx(monkeypatch, 1024, [])
    monkeypatch.setenv("AITHER_EMBEDDINGS_URL", "http://ce.test:8229")
    prov = mod.AdkEmbeddings()
    assert asyncio.run(prov.embed_texts(["a"]))[1] == 1024
    _fake_httpx(monkeypatch, 768, [])  # the pinned endpoint now serves another model
    vecs, dim = asyncio.run(prov.embed_texts(["a", "b"]))
    assert vecs == [[], []] and dim == 1024


def test_explicit_model_override_still_wins_in_both_spaces(emb):
    assert emb("", AITHER_EMBED_MODEL="bge-m3").CANONICAL_MODEL == "bge-m3"
    mod = emb("aither-code-embed", AITHER_EMBED_MODEL="ce-v2")
    assert (mod.CANONICAL_MODEL, mod.CANONICAL_DIM) == ("ce-v2", 1024)
    # a leftover nomic id is stale config in the code-embed space, never sent
    mod = emb("aither-code-embed", AITHER_EMBED_MODEL="nomic-embed-text")
    assert mod.CANONICAL_MODEL == "aither-code-embed"


def test_unknown_space_raises_like_the_platform_module(emb, monkeypatch):
    import adk.embeddings as mod

    monkeypatch.setenv("AITHER_EMBED_SPACE", "bogus")
    with pytest.raises(ValueError, match="AITHER_EMBED_SPACE='bogus' is not one of"):
        mod._embed_space()
    with pytest.raises(ValueError, match="refusing to guess an embedding space"):
        importlib.reload(mod)
    emb.leave()
    assert (mod.EMBED_SPACE, mod.CANONICAL_DIM) == ("nomic", 768)


def test_code_embed_space_never_autodeploys_the_nomic_container(emb, monkeypatch):
    mod = emb("aither-code-embed")
    monkeypatch.setattr(mod, "_has_gpu", lambda: pytest.fail("must not probe"))
    assert asyncio.run(mod.AdkEmbeddings()._maybe_autodeploy()) is False


def test_module_does_not_import_the_platform_lib(emb):
    emb("aither-code-embed")
    import inspect

    import adk.embeddings as mod

    assert "from lib" not in inspect.getsource(mod)
    assert "import lib" not in inspect.getsource(mod)


# ── GraphMemory: an existing 768-d graph refuses 1024-d vectors ──────────────


def _graph(tmp_path, dim):
    from adk.graph_memory import GraphMemory

    async def _embedder(text):
        return [0.25] * dim

    return GraphMemory(agent_name="t", db_path=str(tmp_path / "g.db"), embedder=_embedder)


def _embedded(db):
    with sqlite3.connect(db) as conn:
        return [n // 4 for (n,) in conn.execute(
            "SELECT length(embedding) FROM nodes WHERE embedding IS NOT NULL")]


def test_768d_graph_refuses_1024d_vectors(tmp_path, caplog):
    db = str(tmp_path / "g.db")
    g = _graph(tmp_path, 768)
    asyncio.run(g.add_node("a", content="first fact about alpha"))
    assert _embedded(db) == [768]

    g2 = _graph(tmp_path, 1024)
    with caplog.at_level("ERROR", logger="adk.graph_memory"):
        asyncio.run(g2.add_node("b", content="second fact about beta"))
    assert _embedded(db) == [768]  # the node is stored, WITHOUT a vector
    assert g2.last_embedding_dim == 0
    assert any("REFUSING 1024-d" in r.getMessage() for r in caplog.records)


def _unpinned_768d_graph(tmp_path):
    db = str(tmp_path / "g.db")
    g = _graph(tmp_path, 768)
    asyncio.run(g.add_node("a", content="first fact about alpha"))
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM meta WHERE key = 'embed_dim'")
        conn.commit()
    return db


def _pinned(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT value FROM meta WHERE key = 'embed_dim'").fetchone()[0]


def test_unpinned_legacy_graph_pins_to_the_newcomer_in_the_nomic_space(emb, tmp_path):
    """Pre-switch behaviour, kept: with no pin row the first writer sets it."""
    emb()
    db = _unpinned_768d_graph(tmp_path)
    g2 = _graph(tmp_path, 1024)
    asyncio.run(g2.add_node("b", content="second fact about beta"))
    assert g2._embed_dim == 1024 and _pinned(db) == "1024"
    assert sorted(_embedded(db)) == [768, 1024]


def test_unpinned_legacy_graph_pins_to_the_stored_dim_in_the_code_embed_space(emb, tmp_path):
    """A graph whose vectors predate the meta pin must not be re-pinned by a newcomer."""
    emb()
    db = _unpinned_768d_graph(tmp_path)
    emb("aither-code-embed")
    g2 = _graph(tmp_path, 1024)
    asyncio.run(g2.add_node("b", content="second fact about beta"))
    assert _embedded(db) == [768]
    assert g2._embed_dim == 768 and _pinned(db) == "768"


def test_code_embed_space_graph_refuses_a_non_1024d_embedder(emb, tmp_path, caplog):
    emb("aither-code-embed")
    db = str(tmp_path / "g.db")
    g = _graph(tmp_path, 768)
    with caplog.at_level("ERROR", logger="adk.graph_memory"):
        asyncio.run(g.add_node("a", content="first fact about alpha"))
    assert _embedded(db) == [] and g._embed_dim is None  # an empty graph is not pinned
    assert any("REFUSING 768-d" in r.getMessage() for r in caplog.records)

    g2 = _graph(tmp_path, 1024)
    asyncio.run(g2.add_node("b", content="second fact about beta"))
    assert _embedded(db) == [1024]


def test_code_embed_space_legacy_graph_path_never_uses_ollama_nomic_or_the_hash(
    emb, tmp_path, monkeypatch
):
    from adk.graph_memory import GraphMemory

    emb("aither-code-embed")
    monkeypatch.setenv("AITHER_GRAPH_EMBEDDER", "legacy")
    seen = []
    _fake_httpx(monkeypatch, 768, seen)
    db = str(tmp_path / "g.db")
    g = GraphMemory(agent_name="t", db_path=db)
    assert asyncio.run(g._embed("some text")) == []
    assert seen == []

    emb.leave()  # nomic space: the legacy Ollama path is what it always was
    monkeypatch.setenv("AITHER_GRAPH_EMBEDDER", "legacy")
    g = GraphMemory(agent_name="t", db_path=str(tmp_path / "n.db"))
    assert len(asyncio.run(g._embed("some text"))) == 768
    assert seen and seen[-1][0].endswith("/api/embeddings")


def test_unknown_space_is_not_swallowed_by_the_graph(emb, tmp_path, monkeypatch):
    import adk.embeddings as mod
    from adk.graph_memory import _strict_space_dim

    assert _strict_space_dim() is None
    emb("aither-code-embed")
    assert _strict_space_dim() == 1024
    emb.leave()
    monkeypatch.setattr(mod, "EMBED_SPACE", "nomic")
    assert _strict_space_dim() is None
