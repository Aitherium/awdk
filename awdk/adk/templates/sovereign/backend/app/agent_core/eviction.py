"""Targeted, citation-aware cache eviction.

Replaces the legacy "wipe every cached conversation for the tenant"
pattern in document-delete handlers. We evict only the conversations
whose memories actually cited the deleted document, then leave the
memory rows in place with `auto_recall_blocked=1`. The memory_citations
rows flip to `status='stale'` so learning signal survives.

Designed as a one-shot helper — pass the host app's chat-cache dict
plus the agent_core UnifiedMemoryClient instance.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, MutableMapping

log = logging.getLogger("agent_core.eviction")


async def evict_cached_conversations_referencing(
    *,
    tenant_id: str,
    document_id: str,
    memory_client: Any | None,
    cache: MutableMapping[tuple[str, str], Any] | None,
    fallback_clear_all_on_no_memory: bool = True,
) -> dict[str, Any]:
    """Evict only cache rows whose conversations cite the deleted document.

    Returns a small report dict for logging/admin endpoints.

    Behaviour:
    - If `memory_client` is provided and can identify citing conversations,
      only those (tenant_id, conv_id) keys are removed from `cache`.
    - If no citing conversations are found AND
      `fallback_clear_all_on_no_memory=True`, we degrade to the legacy
      tenant-wide clear (matches the original behaviour, just gated).
    - `memory_client.block_recall_for_document` is called so future recall
      stops returning content from the deleted doc.
    """
    report: dict[str, Any] = {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "cache_keys_removed": 0,
        "memories_blocked": 0,
        "fallback_used": False,
    }

    citing_convs: list[str] = []
    if memory_client is not None:
        try:
            citing_convs = await memory_client.conversations_citing_document(
                tenant_id, document_id,
            )
        except Exception as e:
            log.debug("conversations_citing_document failed: %s", e)

        try:
            report["memories_blocked"] = await memory_client.block_recall_for_document(
                tenant_id, document_id,
            )
        except Exception as e:
            log.debug("block_recall_for_document failed: %s", e)

    if cache is not None:
        if citing_convs:
            keys_to_remove = [
                k for k in list(cache.keys())
                if isinstance(k, tuple) and len(k) == 2
                and k[0] == tenant_id and k[1] in citing_convs
            ]
            for k in keys_to_remove:
                cache.pop(k, None)
            report["cache_keys_removed"] = len(keys_to_remove)
        elif fallback_clear_all_on_no_memory:
            keys_to_remove = [
                k for k in list(cache.keys())
                if isinstance(k, tuple) and len(k) == 2 and k[0] == tenant_id
            ]
            for k in keys_to_remove:
                cache.pop(k, None)
            report["cache_keys_removed"] = len(keys_to_remove)
            report["fallback_used"] = True

    return report


def tombstone_document(doc: Any) -> bool:
    """Mark an ORM document as tombstoned instead of hard-deleting it.

    Works for any model that has `deleted_at` or `tombstoned` attributes —
    if neither exists, returns False so the caller can fall back to delete.
    """
    from datetime import datetime

    touched = False
    if hasattr(doc, "deleted_at"):
        try:
            doc.deleted_at = datetime.utcnow()
            touched = True
        except Exception:
            pass
    if hasattr(doc, "tombstoned"):
        try:
            doc.tombstoned = True
            touched = True
        except Exception:
            pass
    return touched
