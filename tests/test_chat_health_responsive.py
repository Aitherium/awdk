"""/health must answer DURING a /chat turn, and say how many turns are in flight.

Measured 2026-09-27 on the :9001 daemon: ~20 s into the first /chat after a launch
the embeddings resolver fell through to the CPU sentence-transformers rung and ran
``import sentence_transformers`` + the model load + ``encode`` ON the event loop
(54 s in the live log, 77-82 s reproduced in-process). /health stopped answering,
the watchdog's ``curl -m 8`` timed out and the daemon was killed mid-turn.

The fake ``sentence_transformers`` below blocks for BLOCK_S inside the model load.
On the old code that block runs on the loop, so /health waits it out and both tests
fail; on the fixed code the CPU rung runs in an executor (a child process in
production, a thread pool here) and /health answers in milliseconds.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from adk import embeddings as emb
from adk.agent import AgentResponse
from adk.inflight import InflightTracker

BLOCK_S = 1.5
#: /health must answer well inside this while the turn is blocked in the CPU rung.
HEALTH_BUDGET_S = 0.5


class _SlowModel:
    """Stands in for SentenceTransformer: a blocking load, a trivial encode."""

    def __init__(self, name: str) -> None:
        time.sleep(BLOCK_S)
        self.name = name

    def encode(self, texts, show_progress_bar=False):  # noqa: ARG002 — mirrors the real API
        class _Arr(list):
            def tolist(self):
                return list(self)
        return _Arr([[0.1] * emb._DEGRADED_DIM for _ in texts])


def _slow_encode_in_worker(model_name, texts):
    """The fixed code's worker entry point, pointed at the slow fake model."""
    return _SlowModel(model_name).encode(texts).tolist()


@pytest.fixture
def cpu_rung(monkeypatch):
    """An AdkEmbeddings provider that can only resolve to the (slow) CPU rung."""
    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = _SlowModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setenv("AITHER_EMBED_MICROSCHEDULER", "0")
    monkeypatch.delenv("AITHER_EMBEDDINGS_URL", raising=False)
    monkeypatch.delenv("AITHER_API_KEY", raising=False)
    monkeypatch.delenv("AITHER_GATEWAY_EMBEDDINGS_URL", raising=False)

    async def _no(*_a, **_k):
        return False

    for rung in ("_try_local_vllm", "_try_ollama", "_try_openai_endpoint"):
        monkeypatch.setattr(emb.AdkEmbeddings, rung, _no)
    # The fixed code's seams (absent on the old code, hence raising=False): run the
    # worker in a thread pool instead of spawning a real child that imports torch.
    pool = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(emb.AdkEmbeddings, "_st_executor", lambda self: pool, raising=False)
    monkeypatch.setattr(emb, "_st_encode_in_child", _slow_encode_in_worker, raising=False)
    monkeypatch.setattr(emb, "_provider", None)
    yield emb.AdkEmbeddings()
    pool.shutdown(wait=True)


async def _max_loop_lag(coro) -> tuple[float, object]:
    """Run ``coro`` while a 20 ms ticker measures the worst event-loop stall."""
    worst = 0.0
    stop = asyncio.Event()

    async def _tick() -> None:
        nonlocal worst
        while not stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(0.02)
            worst = max(worst, time.perf_counter() - t0 - 0.02)

    ticker = asyncio.create_task(_tick())
    await asyncio.sleep(0.05)
    try:
        result = await coro
    finally:
        stop.set()
        await ticker
    return worst, result


def test_cpu_rung_never_blocks_the_loop(cpu_rung):
    lag, (vecs, dim) = asyncio.run(_max_loop_lag(cpu_rung.embed_texts(["reply ok"])))
    assert cpu_rung.backend == "cpu"
    assert dim == emb._DEGRADED_DIM and len(vecs[0]) == emb._DEGRADED_DIM
    assert lag < HEALTH_BUDGET_S, (
        f"event loop stalled {lag:.2f}s during a CPU-rung embed — the model load ran "
        "on the loop (the 2026-09-27 /health outage)")


def test_probe_does_not_import_sentence_transformers(monkeypatch):
    """The rung probe answers 'installed?' from the import path, never by importing."""
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
    emb.AdkEmbeddings()._probe_sentence_transformers()
    assert "sentence_transformers" not in sys.modules, "the probe imported the package"


class _EmbeddingAgent:
    """A stub agent whose turn walks the real embeddings path (as graph memory does)."""

    def __init__(self, provider) -> None:
        self.name = "health-probe"
        self._provider = provider
        self.llm = types.SimpleNamespace(provider_name="stub")

    async def chat(self, message, session_id=None, **_kw):
        await self._provider.embed_texts([message])
        return AgentResponse(content="ok", session_id=session_id or "s", model="stub")


def test_health_answers_during_chat_and_reports_inflight(cpu_rung):
    from adk.server import create_app

    app = create_app(agent=_EmbeddingAgent(cpu_rung), identity="health-probe")

    async def _scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            idle = (await client.get("/health")).json()
            chat = asyncio.create_task(client.post("/chat", json={"message": "reply ok"}))
            # Timed from BEFORE the pause: on a blocked loop the pause itself cannot
            # end until the block does, so timing only the GET would hide the stall.
            t0 = time.perf_counter()
            await asyncio.sleep(0.2)  # the turn is now inside the blocking model load
            busy_resp = await client.get("/health")
            elapsed = time.perf_counter() - t0 - 0.2
            chat_resp = await chat
            after = (await client.get("/health")).json()
        return idle, busy_resp, elapsed, chat_resp, after

    idle, busy_resp, elapsed, chat_resp, after = asyncio.run(_scenario())
    assert chat_resp.status_code == 200 and chat_resp.json()["response"] == "ok"
    assert busy_resp.status_code == 200
    assert elapsed < HEALTH_BUDGET_S, (
        f"/health took {elapsed:.2f}s during a /chat — the watchdog's curl -m 8 kills "
        "the daemon when this grows")
    assert idle["chat"]["inflight"] == 0
    assert busy_resp.json()["chat"]["inflight"] == 1
    assert after["chat"]["inflight"] == 0
    assert after["chat"]["total_started"] == 1


def test_inflight_tracker_releases_on_error():
    """A turn that raises must not pin inflight above zero (it would disarm the watchdog)."""
    from adk.inflight import InflightChatMiddleware

    tracker = InflightTracker()

    async def _boom(scope, receive, send):
        raise RuntimeError("turn failed")

    mw = InflightChatMiddleware(_boom, tracker)

    async def _run():
        with pytest.raises(RuntimeError):
            await mw({"type": "http", "method": "POST", "path": "/chat"}, None, None)

    asyncio.run(_run())
    assert tracker.count == 0 and tracker.total_started == 1


def test_inflight_ignores_non_turn_routes():
    from adk.inflight import InflightChatMiddleware

    tracker = InflightTracker()
    seen = []

    async def _app(scope, receive, send):
        seen.append(tracker.count)

    mw = InflightChatMiddleware(_app, tracker)
    for method, path in (("GET", "/chat"), ("POST", "/chat/steer"), ("GET", "/health")):
        asyncio.run(mw({"type": "http", "method": method, "path": path}, None, None))
    assert seen == [0, 0, 0] and tracker.total_started == 0
