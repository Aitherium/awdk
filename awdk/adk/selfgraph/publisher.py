"""Offline-first publishing of provenance updates to the platform graph plane.

The Publisher maintains a LOCAL durable spool for GraphUpdate records and drains
them opportunistically to the platform. If the platform is unreachable, records
are retained and retried with exponential backoff. No update is ever lost, and
no network failure ever blocks or crashes the calling agent.

Key design:
  - Write to local SQLite spool first (zero latency, zero risk of loss)
  - Drain on a background task or on-demand
  - 4xx from the server = TERMINAL (invariant violation, not retryable)
  - 5xx / timeout / connection error = TRANSIENT (retry with backoff)
  - No auth token = return ok=False with clear error, leave entries PENDING
  - Auth/config come from adk.config; tokens from ~/.aither/auth.json via Config.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .._tls import tls_verify
from ..config import Config
from .schema import GraphUpdate, check_invariants
from .spool import Spool

logger = logging.getLogger("adk.selfgraph.publisher")


@dataclass
class PublishOutcome:
    """Result of a publish operation (single update or drain batch)."""

    ok: bool
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    unreachable: bool = False
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-friendly dict."""
        return asdict(self)


def resolve_base_url(config: Config | None = None) -> str:
    """Resolve the graph plane base URL.

    Resolution order:
      1. AITHER_SELFGRAPH_URL env var (explicit override)
      2. AITHER_GRAPH_URL env var (fallback)
      3. Config's gateway_url (off-box external gateway)
      4. http://127.0.0.1:8154 (on-box default)

    Uses 127.0.0.1 and NOT 'localhost' — the IPv6 path (::1) hangs for ~2s per
    connection on the primary dev host. The doubled-lookup of localhost can
    stall when WSL2 DNS or systemd-resolved is congested.
    """
    # Explicit override
    explicit = os.getenv("AITHER_SELFGRAPH_URL", "").strip()
    if explicit:
        return explicit

    # Secondary override
    secondary = os.getenv("AITHER_GRAPH_URL", "").strip()
    if secondary:
        return secondary

    # Config default (off-box gateway)
    if config is None:
        config = Config.from_env()
    if config.gateway_url:
        return config.gateway_url

    # On-box default
    return "http://127.0.0.1:8154"


class Publisher:
    """Async publisher for provenance updates.

    Handles HTTP transport and error recovery for draining the local spool to
    the platform's graph API. Publishes are idempotent and offline-safe: a failed
    publish is logged, the update remains in the spool, and is retried with
    exponential backoff. Never raises exceptions into the calling agent.

    Usage:
        publisher = Publisher()
        outcome = await publisher.drain()
        await publisher.aclose()
    """

    def __init__(
        self,
        base_url: str = "",
        token: str | None = None,
        spool: Spool | None = None,
        timeout: float = 15.0,
        config: Config | None = None,
    ) -> None:
        """Initialize Publisher.

        Args:
            base_url: Graph plane URL. If empty, auto-resolves via resolve_base_url.
            token: Bearer token. ``None`` (the default) means "resolve from
                Config"; an EXPLICIT empty string means "no token, publish
                nothing". Those are different intents and the distinction is not
                cosmetic: with a plain ``str = ""`` default there was no way to
                construct an unauthenticated publisher, and a caller passing
                ``token=""`` silently borrowed whatever credential the ambient
                Config happened to hold.
            spool: Spool instance. If None, creates one at default ~/.aither/selfgraph/spool.db.
            timeout: HTTP timeout in seconds.
            config: Config instance for resolving defaults. If None, Config.from_env().
        """
        self.config = config or Config.from_env()
        self.base_url = base_url or resolve_base_url(self.config)
        if token is None:
            self.token = self.config.aither_api_key or self.config.get_api_key()
        else:
            self.token = token
        self.spool = spool or Spool()
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init httpx client with generous timeouts."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self.timeout / 2,
                    read=self.timeout,
                    write=self.timeout / 2,
                    pool=self.timeout,
                ),
                verify=tls_verify(),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def publish_update(self, update: GraphUpdate) -> PublishOutcome:
        """Publish a single update directly (bypassing spool).

        This is for callers that want immediate confirmation. If the update
        fails, it is NOT added to the spool — the caller gets the error and
        may retry themselves.

        Args:
            update: GraphUpdate to publish.

        Returns:
            PublishOutcome with sent=1 on success, or failed=1 + error detail.
        """
        # Validate locally first
        problems = check_invariants(update)
        if problems:
            return PublishOutcome(
                ok=False,
                failed=1,
                errors=problems,
            )

        # Check auth
        if not self.token:
            return PublishOutcome(
                ok=False,
                failed=1,
                errors=["not authenticated (no token in Config or AITHER_API_KEY)"],
            )

        # POST to /api/graph/provenance/publish
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/api/graph/provenance/publish",
                json=update.to_dict(),
                headers={"Authorization": f"Bearer {self.token}"},
            )

            if response.status_code == 200 or response.status_code == 201:
                return PublishOutcome(ok=True, sent=1)

            # Server rejected the content (invariant violation)
            if 400 <= response.status_code < 500:
                error_detail = response.text[:500]
                return PublishOutcome(
                    ok=False,
                    failed=1,
                    errors=[f"HTTP {response.status_code}: {error_detail}"],
                )

            # Server error or overloaded
            error_detail = response.text[:500]
            return PublishOutcome(
                ok=False,
                failed=1,
                unreachable=True,
                errors=[f"HTTP {response.status_code}: {error_detail}"],
            )

        except (httpx.RequestError, asyncio.TimeoutError) as e:
            return PublishOutcome(
                ok=False,
                failed=1,
                unreachable=True,
                errors=[f"connection error: {str(e)[:200]}"],
            )

        except Exception as e:
            # Catch all other exceptions so publish_update() never raises
            logger.exception("Unexpected error in publish_update()")
            return PublishOutcome(
                ok=False,
                failed=1,
                unreachable=True,
                errors=[f"unexpected error: {str(e)[:200]}"],
            )

    async def drain(self, limit: int = 100) -> PublishOutcome:
        """Drain pending spool entries.

        Fetches up to `limit` pending entries, POSTs each to /api/graph/provenance/publish,
        and marks them sent/failed accordingly.

        A 4xx (invariant violation) is marked TERMINAL (never retried).
        A 5xx / timeout / connection error is marked TRANSIENT (retried with backoff).

        Args:
            limit: Max number of entries to drain per call.

        Returns:
            PublishOutcome with sent/failed/skipped counts and any errors.
        """
        outcome = PublishOutcome(ok=True)

        # No auth = leave entries pending and report clearly
        if not self.token:
            stats = self.spool.stats()
            if stats.get("pending", 0) > 0:
                outcome.ok = False
                outcome.skipped = stats["pending"]
                outcome.errors.append(
                    "not authenticated (no token) — spool entries remain pending"
                )
            return outcome

        rows = self.spool.pending(limit)
        if not rows:
            return outcome

        client = await self._get_client()

        for row in rows:
            try:
                update = row.to_update()
                if update is None:
                    logger.error(f"Failed to deserialize update rowid {row.rowid}")
                    self.spool.mark_sent([row.rowid])
                    outcome.failed += 1
                    outcome.errors.append(f"deserialization error: rowid {row.rowid}")
                    continue

                response = await client.post(
                    f"{self.base_url}/api/graph/provenance/publish",
                    json=update.to_dict(),
                    headers={"Authorization": f"Bearer {self.token}"},
                )

                if response.status_code == 200 or response.status_code == 201:
                    self.spool.mark_sent([row.rowid])
                    outcome.sent += 1

                # Server rejected the content (invariant violation) — terminal
                elif 400 <= response.status_code < 500:
                    error_detail = response.text[:200]
                    logger.warning(
                        f"terminal error publishing entry {row.rowid} "
                        f"(HTTP {response.status_code}): {error_detail}"
                    )
                    # Mark as sent anyway (no point retrying invariant violations)
                    self.spool.mark_sent([row.rowid])
                    outcome.failed += 1
                    outcome.errors.append(
                        f"terminal error (HTTP {response.status_code}): {error_detail}"
                    )

                # Server error or overloaded (transient)
                else:
                    error_detail = response.text[:200]
                    logger.debug(
                        f"transient error publishing entry {row.rowid} "
                        f"(HTTP {response.status_code}): will retry"
                    )
                    outcome.unreachable = True
                    outcome.errors.append(
                        f"transient error (HTTP {response.status_code}): {error_detail}"
                    )

            except (httpx.RequestError, asyncio.TimeoutError) as e:
                # Network error (transient) — leave in spool for retry
                logger.debug(f"connection error publishing entry {row.rowid}: {e}")
                outcome.unreachable = True
                outcome.errors.append(f"connection error: {str(e)[:150]}")

            except Exception as e:
                # Unexpected error; leave in spool for retry
                logger.exception(f"Unexpected error publishing entry {row.rowid}")
                self.spool.mark_failed(row.rowid, str(e))
                outcome.errors.append(f"unexpected error: {str(e)[:150]}")

        # Final status
        outcome.ok = outcome.failed == 0 and not outcome.unreachable
        if outcome.sent > 0 and outcome.failed == 0:
            logger.info(f"Drained {outcome.sent} provenance updates")
        if outcome.unreachable:
            logger.warning(
                f"Platform unreachable ({outcome.sent} sent, {outcome.failed} failed, "
                f"will retry)"
            )

        return outcome

    async def ground(
        self, claim_id: str = "", statement: str = "", triple: Dict[str, str] | None = None
    ) -> dict:
        """POST to /api/graph/provenance/ground (grounding/citation endpoint).

        Grounds a claim by connecting it to external sources/evidence.

        Args:
            claim_id: ID of the claim to ground.
            statement: Raw statement text.
            triple: {'subject': '...', 'predicate': '...', 'object': '...'}.

        Returns:
            Server response dict, or empty dict on error.
        """
        if not self.token:
            logger.warning("ground() called without auth token")
            return {}

        try:
            client = await self._get_client()
            payload = {}
            if claim_id:
                payload["claim_id"] = claim_id
            if statement:
                payload["statement"] = statement
            if triple:
                payload["triple"] = triple

            response = await client.post(
                f"{self.base_url}/api/graph/provenance/ground",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
            )

            if response.status_code == 200:
                return response.json()
            logger.warning(f"ground() returned HTTP {response.status_code}")
            return {}

        except Exception as e:
            logger.warning(f"ground() error: {e}")
            return {}

    async def context(
        self,
        task: str = "",
        hops: int = 2,
        token_budget: int = 4000,
        seed_ids: List[str] | None = None,
    ) -> dict:
        """POST to /api/graph/provenance/context (retrieve grounded context).

        Fetches context for a task or query from the provenance graph.

        Args:
            task: Task description or query.
            hops: Traversal depth.
            token_budget: Max tokens in response.
            seed_ids: Optional starting node IDs.

        Returns:
            Server response dict with context, or empty dict on error.
        """
        if not self.token:
            logger.warning("context() called without auth token")
            return {}

        try:
            client = await self._get_client()
            payload = {
                "task": task,
                "hops": hops,
                "token_budget": token_budget,
            }
            if seed_ids:
                payload["seed_ids"] = seed_ids

            response = await client.post(
                f"{self.base_url}/api/graph/provenance/context",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
            )

            if response.status_code == 200:
                return response.json()
            logger.warning(f"context() returned HTTP {response.status_code}")
            return {}

        except Exception as e:
            logger.warning(f"context() error: {e}")
            return {}

    async def leaves(self, limit: int = 50) -> list[dict]:
        """GET /api/graph/provenance/leaves (fetch leaf nodes).

        Returns the most recent or highest-entropy nodes in the graph.

        Args:
            limit: Max number of leaves to return.

        Returns:
            List of leaf node dicts, or empty list on error.
        """
        if not self.token:
            logger.warning("leaves() called without auth token")
            return []

        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/api/graph/provenance/leaves?limit={limit}",
                headers={"Authorization": f"Bearer {self.token}"},
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("leaves", []) if isinstance(data, dict) else data

            logger.warning(f"leaves() returned HTTP {response.status_code}")
            return []

        except Exception as e:
            logger.warning(f"leaves() error: {e}")
            return []

    async def stats(self) -> dict:
        """GET /api/graph/provenance/stats (graph metrics).

        Returns:
            Server response dict with stats, or empty dict on error.
        """
        if not self.token:
            logger.warning("stats() called without auth token")
            return {}

        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/api/graph/provenance/stats",
                headers={"Authorization": f"Bearer {self.token}"},
            )

            if response.status_code == 200:
                return response.json()
            logger.warning(f"stats() returned HTTP {response.status_code}")
            return {}

        except Exception as e:
            logger.warning(f"stats() error: {e}")
            return {}

    async def health(self) -> bool:
        """Simple liveness check."""
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/api/graph/provenance/stats",
                timeout=self.timeout / 2,
            )
            return response.status_code == 200
        except Exception:
            return False


# ==============================================================================
# Sync wrapper for CLI
# ==============================================================================


def drain_sync(limit: int = 100, config: Config | None = None) -> PublishOutcome:
    """Blocking convenience wrapper for CLI (runs asyncio.run).

    Drains pending spool entries and blocks until done. Safe to call even if
    an event loop is already running (checks and handles gracefully).

    Args:
        limit: Max entries to drain per batch.
        config: Config instance. If None, Config.from_env().

    Returns:
        PublishOutcome with results.
    """
    config = config or Config.from_env()
    publisher = Publisher(config=config)

    try:
        # Try to get running loop; if one exists, we can't use asyncio.run
        loop = asyncio.get_running_loop()
        logger.warning(
            "drain_sync() called from inside an async context; "
            "use await drain() instead"
        )
        return PublishOutcome(ok=False, errors=["already in async context"])
    except RuntimeError:
        # No running loop; safe to use asyncio.run
        pass

    try:
        return asyncio.run(publisher.drain(limit=limit))
    finally:
        # Clean up the publisher's HTTP client
        try:
            asyncio.run(publisher.aclose())
        except RuntimeError:
            pass
