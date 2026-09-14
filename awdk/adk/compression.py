"""adk context compression — headroom pre-send hook for standalone / BYO agents.

The AitherOS fleet compresses every LLM call at its gateway chokepoint. This is the
adk-local equivalent so a standalone or BYO adk agent gets the SAME token savings
against its own backend (Ollama / OpenAI / Anthropic / vLLM), by shrinking bulky
context (verbose tool output, RAG, file dumps) just before it is sent.

adk is a separate open-core package and cannot import ``AitherOS/lib`` — this mirrors
``lib/core/CompressionClient`` as a small self-contained client. It talks to the same
**headroom sidecar** (POST ``/compress``); there is NO in-process dependency on
headroom itself.

Contract (identical to the fleet hook):
  * DEFAULT OFF. Enable with ``AITHER_HEADROOM_ENABLED=true``.
  * GRACEFUL NO-OP. Disabled, below threshold, sidecar unreachable/slow, or any
    malformed/inflating/mismatched response -> the original messages are returned
    UNCHANGED. This must never raise into the LLM call.
  * Only string content is compressed; image / structured (list) content and message
    order/count are preserved exactly (mismatch -> no-op).

Env: ``AITHER_HEADROOM_ENABLED`` (default false), ``AITHER_HEADROOM_URL``
(default ``http://aither-headroom:8787``; a standalone agent typically points this at
its own sidecar, e.g. ``http://127.0.0.1:8788``).
"""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

logger = logging.getLogger("adk.compression")

_TRUTHY = {"1", "true", "yes", "on"}
_DEFAULT_URL = "http://aither-headroom:8787"
_MIN_CHARS = 800
_TIMEOUT_S = 2.0


def enabled() -> bool:
    """Live switch (default OFF). env only — adk has no baked config file."""
    return (os.environ.get("AITHER_HEADROOM_ENABLED") or "").strip().lower() in _TRUTHY


def _base_url() -> str:
    return (os.environ.get("AITHER_HEADROOM_URL") or _DEFAULT_URL).rstrip("/")


def _content_chars(messages: list) -> int:
    total = 0
    for m in messages:
        c = getattr(m, "content", None)
        if isinstance(c, str):
            total += len(c)
    return total


async def maybe_compress(messages: list, model: str | None = None) -> list:
    """Return ``messages`` with string content compressed via the headroom sidecar,
    or the ORIGINAL list unchanged on any failure. Never raises.

    Args:
        messages: list of adk ``Message`` dataclasses.
        model: tokenizer/model target (optional).
    """
    if not enabled() or not messages:
        return messages
    if _content_chars(messages) < _MIN_CHARS:
        return messages
    try:
        import httpx

        payload: dict[str, Any] = {
            "messages": [
                {"role": getattr(m, "role", "user"),
                 "content": m.content if isinstance(getattr(m, "content", None), str) else ""}
                for m in messages
            ],
        }
        if model:
            payload["model"] = model
        async with httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT_S, connect=1.0)) as c:
            r = await c.post(f"{_base_url()}/compress", json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # never break the LLM path
        logger.debug("[headroom] adk compress skipped: %s", e)
        return messages

    compressed = data.get("messages") if isinstance(data, dict) else None
    # Preserve order/count exactly; a shape mismatch means we can't safely map back.
    if not isinstance(compressed, list) or len(compressed) != len(messages):
        return messages

    out = list(messages)
    changed_total = 0
    for i, (orig, comp) in enumerate(zip(messages, compressed)):
        oc = getattr(orig, "content", None)
        nc = comp.get("content") if isinstance(comp, dict) else None
        # Only swap when the original was a plain string that actually got smaller.
        if isinstance(oc, str) and isinstance(nc, str) and 0 < len(nc) < len(oc):
            try:
                out[i] = dataclasses.replace(orig, content=nc)
                changed_total += len(oc) - len(nc)
            except Exception:  # noqa: BLE001 — not a dataclass / frozen; leave as-is
                return messages
    if changed_total <= 0:
        return messages
    return out
