"""Write a completed task's context into the tenant knowledge pool. Opt-in.

WHY THIS IS OPT-IN AND STAYS OPT-IN.

Same doctrine as ``adk.learning_report``: an agent's task text and its answer
are the most sensitive things it holds, and on a customer's machine they may
contain their data. This module does nothing at all unless a pool ingest
endpoint has been configured AND the switch is on. Unset means off; there is
no "helpful" default, and no default host.

WHY IT EXISTS.

The platform's context substrate accumulates what its agents produce: the
search deep lane writes every completed research back into the knowledge pool
(Nexus vector store) so the next query can cite it. An SDK agent produced work
that reached no pool of any kind -- its completed tasks died in the request
window. This lets an operator point their agent at the same pool, so the
substrate accumulates across the whole fleet of agents, not just the platform's
own.

WHAT IT SENDS.

Sent: the task, the answer text, the agent name, a success flag, and the
tenant id (the agent's OWN tenant -- the platform refuses a payload tenant
that differs from the caller's, so a misconfigured client cannot write into
someone else's pool). Not sent: tool arguments, memory contents, credentials,
or anything from the environment. If you would not paste it into a support
ticket, it does not belong in the pool either.

THE ENDPOINT CONTRACT.

The platform's ``POST /external/ingest`` (Genesis) with an
``Authorization: Bearer <token>`` and ``X-Tenant-ID: <tenant>`` header. The
token is a device-flow minted credential (``aither_ext_*``); the tenant in
the payload MUST equal the caller's own tenant or the route 403s.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

#: Switch. Unset or anything other than a truthy value means DO NOT SEND.
ENABLE_ENV = "AITHER_POOL_WRITE_THROUGH"

#: Where to POST. No default host: a module that guesses an endpoint can send
#: a customer's text somewhere they never chose.
URL_ENV = "AITHER_POOL_INGEST_URL"

#: The Bearer credential (device-flow minted, aither_ext_* / PAT).
TOKEN_ENV = "AITHER_POOL_INGEST_TOKEN"

#: The tenant this agent belongs to. Sent as X-Tenant-ID AND in the payload;
#: the platform's tenant-scope guard refuses a payload tenant that differs
#: from the caller's own, so a wrong value fails closed (403), never cross-
#: tenant.
TENANT_ENV = "AITHER_TENANT_ID"

#: A quality floor mirrors the platform's own: a failed or poor outcome is
#: not something to imitate, so it is not worth pooling.
MIN_QUALITY = 0.7

#: Cap the pooled content. A task + answer beyond this is truncated, not
#: dropped: the pool is a context store, and the tail of a long result is
#: often where the conclusion lives.
MAX_CONTENT = 12000

_TRUTHY = {"1", "true", "yes", "on"}

#: Module-level alias so a test can capture the request without touching the
#: shared urllib module.
_urlopen = urllib.request.urlopen


def write_through_enabled() -> bool:
    """True only when an operator has explicitly switched the write-through on."""
    raw = os.getenv(ENABLE_ENV, "").strip().lower()
    return raw in _TRUTHY


def _pool_ingest_payload(
    task: str,
    answer: str,
    *,
    agent_name: str,
    tenant_id: str,
    success: bool,
    quality: float,
) -> dict[str, Any]:
    """Build the /external/ingest payload for a completed task."""
    content = f"TASK: {task}\n\nRESULT: {answer}"[:MAX_CONTENT]
    return {
        "content": content,
        "content_type": "notes",
        "source_name": f"sdk_agent:{agent_name}",
        "tenant_id": tenant_id,
        "metadata": {
            "kind": "sdk_agent_task",
            "agent": agent_name,
            "success": success,
            "quality": round(quality, 3),
        },
    }


def report_task_to_pool(
    task: str,
    answer: str,
    *,
    agent_name: str,
    system_prompt: str = "",
    success: bool = True,
    quality: float = 1.0,
) -> None:
    """Offer this completed task to the tenant knowledge pool. OPT-IN.

    Does nothing unless ``AITHER_POOL_WRITE_THROUGH`` is on AND
    ``AITHER_POOL_INGEST_URL`` (+ token, tenant) are set. Never raises:
    pooling must not be able to fail the work it reports on.

    The system prompt is deliberately NOT sent (it may hold the operator's
    instructions and secrets); it is accepted only to keep the signature
    parallel to the learning report's, and exists to be documented as not
    sent.
    """
    if not write_through_enabled():
        return
    url = os.getenv(URL_ENV, "").strip()
    token = os.getenv(TOKEN_ENV, "").strip()
    tenant = os.getenv(TENANT_ENV, "").strip()
    if not url or not token or not tenant:
        logger.debug(
            "pool write-through on but incomplete (%s, %s, %s) — not sending",
            "url" if url else "URL",
            "token" if token else "TOKEN",
            "tenant" if tenant else "TENANT",
        )
        return
    if quality < MIN_QUALITY or not success:
        logger.debug(
            "pool write-through skipped: quality=%s success=%s below floor",
            quality, success,
        )
        return
    payload = _pool_ingest_payload(
        task, answer, agent_name=agent_name, tenant_id=tenant,
        success=success, quality=quality,
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Tenant-ID": tenant,
        },
        method="POST",
    )
    try:
        with _urlopen(req, timeout=15.0) as resp:
            if resp.status != 200:
                logger.debug("pool write-through returned %s", resp.status)
            else:
                logger.info(
                    "Pooled task outcome for agent %s (tenant %s)", agent_name, tenant)
    except (urllib.error.URLError, OSError) as exc:
        logger.debug("pool write-through failed: %s", exc)
