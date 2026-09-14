"""Provider-agnostic async batch runner (OQ16 lever 4).

One entry point — :func:`run_batch` — for fire-a-pile-of-requests-cheaply work
(nightly self-review, pattern-mining, embeddings: anything NOT on a human-waiting
path). Where the provider exposes a real async batch API (Anthropic Message
Batches, OpenAI ``/v1/batches`` — ~50% off), it submits → polls → fetches. Where
it does not, or the batch never finishes, it transparently falls back to running
the same requests concurrently through ``provider.chat()`` — same results, no
discount, correct everywhere. A single failed item becomes an error sentinel
rather than sinking the whole batch.

    from adk.llm.batch_runner import run_batch
    from adk.llm.base import BatchRequest, Message
    reqs = [BatchRequest(custom_id=str(i), messages=[Message("user", q)]) for i, q in enumerate(qs)]
    results = await run_batch(provider, reqs)   # list[LLMResponse], input order
"""

from __future__ import annotations

import asyncio
import logging

from .base import BatchRequest, LLMResponse

logger = logging.getLogger("adk.llm.batch_runner")

# Terminal poll statuses, normalized across providers (Anthropic uses "ended",
# OpenAI "completed"/"failed"/"expired"/"cancelled"). Kept generous on purpose.
_SUCCESS_TERMINAL = frozenset({"completed", "ended", "done", "success"})
_FAILURE_TERMINAL = frozenset(
    {"failed", "cancelled", "canceled", "expired", "errored", "error"}
)


class _BatchUnfinishedError(Exception):
    """Native batch did not complete (failed/timed out) — triggers fallback."""


def _sentinel() -> LLMResponse:
    """An item that could not be produced — never raised, always returned."""
    return LLMResponse(content="", finish_reason="error")


async def run_batch(
    provider,
    requests: list[BatchRequest],
    *,
    poll_interval: float = 5.0,
    timeout: float = 86400.0,
    max_concurrency: int = 8,
) -> list[LLMResponse]:
    """Run ``requests`` through ``provider``, batched if supported, else concurrent.

    Returns one :class:`LLMResponse` per request, in INPUT order. Always returns —
    a missing native batch API, a failed/timed-out batch, or a single bad item all
    degrade gracefully (fallback or error sentinel), never an exception.
    """
    if not requests:
        return []
    if getattr(provider.capabilities(), "batch", False):
        try:
            return await _run_native(
                provider, requests, poll_interval=poll_interval, timeout=timeout
            )
        except NotImplementedError:
            logger.info(
                "Provider advertised batch but the native path is unavailable "
                "(local engine?) — falling back to concurrent chat()."
            )
        except _BatchUnfinishedError as exc:
            logger.warning("%s — falling back to concurrent chat().", exc)
        except Exception as exc:  # noqa: BLE001 — any native failure degrades, never sinks
            logger.warning(
                "Native batch failed (%s) — falling back to concurrent chat().", exc
            )
    return await _run_fallback(provider, requests, max_concurrency=max_concurrency)


async def _run_native(
    provider,
    requests: list[BatchRequest],
    *,
    poll_interval: float,
    timeout: float,
) -> list[LLMResponse]:
    batch_id = await provider.submit_batch(requests)
    if not batch_id:
        raise RuntimeError("submit_batch returned no batch id")

    waited = 0.0
    delay = max(0.01, min(poll_interval, 5.0))
    while True:
        status = (await provider.poll_batch(batch_id) or "").strip().lower()
        if status in _SUCCESS_TERMINAL:
            break
        if status in _FAILURE_TERMINAL:
            raise _BatchUnfinishedError(f"batch {batch_id} ended in status {status!r}")
        if waited >= timeout:
            raise _BatchUnfinishedError(
                f"batch {batch_id} did not finish within {timeout:.0f}s (last status {status!r})"
            )
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 30.0)  # gentle capped backoff

    results = await provider.fetch_batch_results(batch_id)
    # Providers return results sorted by custom_id and drop the id from the
    # response object; re-align by sorting requests the same way, then restore
    # the caller's original ordering. Missing/failed items become sentinels.
    ordered = sorted(requests, key=lambda r: r.custom_id)
    mapped = {req.custom_id: res for req, res in zip(ordered, results)}
    return [mapped.get(r.custom_id, _sentinel()) for r in requests]


async def _run_fallback(
    provider,
    requests: list[BatchRequest],
    *,
    max_concurrency: int,
) -> list[LLMResponse]:
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _one(req: BatchRequest) -> LLMResponse:
        async with sem:
            try:
                return await provider.chat(
                    req.messages,
                    model=req.model,
                    temperature=req.temperature,
                    max_tokens=req.max_tokens,
                    tools=req.tools,
                    cache=req.cache,
                )
            except Exception as exc:  # noqa: BLE001 — one failure must not kill the batch
                logger.warning("Fallback chat failed for %s: %s", req.custom_id, exc)
                return _sentinel()

    # gather preserves input order.
    return await asyncio.gather(*[_one(r) for r in requests])
