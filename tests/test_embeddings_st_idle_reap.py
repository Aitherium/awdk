"""The CPU-rung embedding child is shut down once it has been idle.

Measured 2026-09-29 on aitheros-mcpgateway: every embeddings backend was down, the
gateway fell to the CPU rung, spawned the one-worker torch/sentence-transformers
child for a handful of embeds (5.7 s CPU in total) and then kept that child for
3.5 h at 1.07 GB RSS -- 80% of the gateway's growth from 278 MB to 1.29 GB.

These tests fail on the code before the reaper (the pool is kept forever) and pass
with it. No real child process is spawned: ProcessPoolExecutor is swapped for a
thread-backed stand-in that records its shutdown.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import adk.embeddings as emb


class _FakeProcessPool(ThreadPoolExecutor):
    instances: list = []

    def __init__(self, max_workers=None, mp_context=None):  # noqa: ARG002 - mirrors the real API
        super().__init__(max_workers=max_workers)
        self.shut_down = False
        _FakeProcessPool.instances.append(self)

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shut_down = True
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


def _fake_encode(model_name, texts):  # noqa: ARG001 - mirrors _st_encode_in_child
    return [[0.1] * emb._DEGRADED_DIM for _ in texts]


@pytest.fixture
def cpu_provider(monkeypatch):
    _FakeProcessPool.instances = []
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _FakeProcessPool)
    monkeypatch.setattr(emb, "_st_encode_in_child", _fake_encode)
    monkeypatch.setattr(emb, "_ST_IDLE_S", 0.3, raising=False)
    p = emb.AdkEmbeddings()
    p._resolved = True
    p._backend = "cpu"
    p._dim = emb._DEGRADED_DIM
    p._degraded = True
    yield p
    for pool in _FakeProcessPool.instances:
        ThreadPoolExecutor.shutdown(pool, wait=True)


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_idle_cpu_worker_is_shut_down(cpu_provider):
    vecs, dim = asyncio.run(cpu_provider.embed_texts(["hello"]))
    assert dim == emb._DEGRADED_DIM and len(vecs[0]) == emb._DEGRADED_DIM
    assert len(_FakeProcessPool.instances) == 1
    pool = _FakeProcessPool.instances[0]
    assert _wait_for(lambda: cpu_provider._st_pool is None and pool.shut_down), (
        "the CPU embedding worker was kept after the idle window -- it holds the "
        "torch model (1.07 GB measured) for the life of the process")


def test_next_embed_after_reap_respawns(cpu_provider):
    asyncio.run(cpu_provider.embed_texts(["one"]))
    assert _wait_for(lambda: cpu_provider._st_pool is None)
    vecs, _ = asyncio.run(cpu_provider.embed_texts(["two"]))
    assert len(vecs) == 1 and len(_FakeProcessPool.instances) == 2


def test_busy_worker_is_not_reaped(cpu_provider, monkeypatch):
    monkeypatch.setattr(emb, "_ST_IDLE_S", 5.0, raising=False)
    asyncio.run(cpu_provider.embed_texts(["hello"]))
    time.sleep(0.5)
    assert cpu_provider._st_pool is not None, "reaped before the idle window elapsed"
    assert not _FakeProcessPool.instances[0].shut_down
    cpu_provider._st_idle_timer.cancel()
