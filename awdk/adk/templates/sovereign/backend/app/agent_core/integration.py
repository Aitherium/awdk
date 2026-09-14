"""High-level chat integration helpers for agent_core memory.

Reduces per-app boilerplate. Each app's chat endpoint just calls:

    recall_text = await build_recall_context(client, tenant_id, user_message)
    # ... append to system_extra ...
    # ... run LLM ...
    await record_chat_turn(
        client, tenant_id, conversation_id,
        user_message=user_message, assistant_response=response_text,
        context_chunks=context, role="user",
    )
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from .client import UnifiedMemoryClient

log = logging.getLogger("agent_core.integration")


async def build_recall_context(
    client: UnifiedMemoryClient | None,
    tenant_id: str,
    query: str,
    *,
    limit: int = 5,
    min_confidence: float = 0.35,
    header: str = "Relevant context from prior conversations:",
) -> str:
    """Return a system-extra block with high-confidence memories.

    Empty string on miss / failure (never raises into the chat path).
    """
    if client is None or not query:
        return ""
    try:
        results = await client.recall(
            tenant_id, query,
            limit=limit, min_confidence=min_confidence,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_core recall failed: %s", exc)
        return ""
    if not results:
        return ""
    lines = [f"\n\n{header}"]
    for r in results:
        lines.append(
            f"- ({r.tier.value}, conf={r.confidence:.2f}, src={r.source}) {r.content}"
        )
    return "\n".join(lines)


def _extract_doc_ids(chunks: Iterable[dict[str, Any]] | None) -> list[str]:
    if not chunks:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        if not isinstance(c, dict):
            continue
        did = c.get("doc_id") or (c.get("metadata") or {}).get("doc_id")
        if did and did not in seen:
            seen.add(did)
            out.append(str(did))
    return out


def _summarize_turn(user_msg: str, assistant_msg: str, *, max_chars: int = 600) -> str:
    u = (user_msg or "").strip().replace("\n", " ")
    a = (assistant_msg or "").strip().replace("\n", " ")
    if len(u) > 240:
        u = u[:237] + "..."
    if len(a) > 360:
        a = a[:357] + "..."
    summary = f"Q: {u}\nA: {a}"
    return summary[:max_chars]


async def record_chat_turn(
    client: UnifiedMemoryClient | None,
    tenant_id: str,
    conversation_id: str,
    *,
    user_message: str,
    assistant_response: str,
    context_chunks: Iterable[dict[str, Any]] | None = None,
    source: str = "conversation",
    confidence: float = 0.55,
    min_chars: int = 80,
) -> None:
    """Persist a Q->A turn into the local 5-tier store with doc citations.

    Silently skips short turns and never raises into the chat path.
    """
    if client is None:
        return
    if not assistant_response or len(assistant_response) < min_chars:
        return
    try:
        summary = _summarize_turn(user_message, assistant_response)
        doc_ids = _extract_doc_ids(context_chunks)
        await client.store_memory(
            tenant_id=tenant_id,
            content=summary,
            conversation_id=conversation_id,
            source=source,
            confidence=confidence,
            document_citations=doc_ids,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_core record_chat_turn failed: %s", exc)
