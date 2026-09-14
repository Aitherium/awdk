"""AitherRelay client — make an adk agent a first-class chat participant.

The agent JOINS AitherRelay (the production chat/DM hub, e.g.
``https://relay.aitherium.com/api/relay/v1``) as itself, then answers direct
messages **on its own inference** — no SSH, and independent of the platform's
Genesis-persona reply loop. This is what lets a human DM *any* fleet agent
(OptiPlex / DGX / 5090 / a managed twin) and get a real reply.

Flow (all against the live AitherRelay REST API):
    1. ``POST /agent/join``            — register this agent's nick (Bearer key)
    2. poll ``GET /dms/partners`` + ``GET /dms/{partner}`` for new inbound human DMs
    3. for each new DM: ``agent.chat(text)`` → ``POST /dms {to_nick, content}``

Auth: a Bearer token that AitherRelay accepts — a TRUSTED credential for a
fleet agent (ACTA ``aither_sk_*`` / a service identity), or an ``aither_ext_*``
Agent-Lounge key (note: lounge/external agents are quarantined to agent-only
channels and cannot DM humans — use a trusted key for human-facing agents).

Usage::

    from adk.relay_client import RelayClient
    client = RelayClient(base_url=..., token=..., nick="optiplex-agent", agent=my_agent)
    await client.run()          # join + serve DMs forever

TLS: talks to internal-CA HTTPS endpoints; by default it trusts the AitherNet
CA bundle (``tls_verify()``) rather than the system store — it never disables
verification. Pass ``verify=`` to override.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional, Union

import httpx

from ._tls import tls_verify

logger = logging.getLogger("adk.relay_client")

_DEFAULT_POLL_S = 4.0


class RelayClient:
    """Join AitherRelay as an agent and answer DMs on the agent's own inference."""

    def __init__(
        self,
        base_url: str,
        token: str,
        nick: str,
        agent: Any,
        *,
        channel: str = "#agents",
        agent_service: str = "adk",
        poll_interval: float = _DEFAULT_POLL_S,
        verify: Union[bool, str, None] = None,
        on_notification: Optional[Callable[[dict], None]] = None,
    ):
        # base_url points at the relay API root, e.g.
        # https://relay.aitherium.com/api/relay/v1  (or http://localhost:8205/v1)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.nick = nick
        self.agent = agent
        self.channel = channel if channel.startswith("#") else f"#{channel}"
        self.agent_service = agent_service
        self.poll_interval = poll_interval
        # TLS: default to the AitherNet CA bundle (internal-CA endpoints fail the
        # system trust store) — never disable verification. `tls_verify()` honours
        # AITHER_TLS_VERIFY / AITHER_CA_BUNDLE; an explicit `verify=` overrides it.
        self.verify: Union[bool, str] = tls_verify() if verify is None else verify
        # Highest message id we've already handled per partner — so we reply once.
        self._seen: dict[str, str] = {}
        # Notification callback hook (type=="mention" will call this; others log at INFO)
        self.on_notification = on_notification
        # Track seen notification ids to avoid duplicates
        self._seen_notif_ids: set[str] = set()
        # Track last failed notification poll to log warning only once per N failures
        self._notif_poll_failure_count = 0
        self._running = False

    # ── HTTP helpers ────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def _get(self, client: httpx.AsyncClient, path: str) -> Optional[Any]:
        r = await client.get(f"{self.base_url}{path}", headers=self._headers())
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            return None
        return r.json()

    # ── Lifecycle ───────────────────────────────────────────────────────
    async def join(self, client: httpx.AsyncClient) -> bool:
        """Register this agent's nick with the relay. Returns True on success."""
        r = await client.post(
            f"{self.base_url}/agent/join",
            headers=self._headers(),
            params={"channel": self.channel},
            json={"nick": self.nick, "agent_service": self.agent_service},
        )
        if r.status_code == 200:
            logger.info("relay: joined as %s on %s", self.nick, self.channel)
            return True
        # NICK↔IDENTITY PARITY (2026-07-31): the relay is the single authority on
        # an authenticated nick and 403s a requested nick that does not match the
        # token's identity. Our nick is operator-CONFIGURED, so any drift between
        # the config and the key made every join fail — and the old code just
        # logged a warning and returned False, leaving the agent silently absent
        # from the channel while the process looked healthy. AitherShell already
        # solves this by omitting the nick when authenticated; do the same on
        # retry, and ADOPT the identity nick so later logs name the real actor.
        if r.status_code == 403 and self.nick:
            retry = await client.post(
                f"{self.base_url}/agent/join",
                headers=self._headers(),
                params={"channel": self.channel},
                json={"agent_service": self.agent_service},
            )
            if retry.status_code == 200:
                resolved = ""
                try:
                    resolved = (retry.json() or {}).get("nick") or ""
                except ValueError:
                    resolved = ""
                logger.warning(
                    "relay: configured nick %r rejected (does not match the "
                    "authenticated identity); joined as %r instead — align the "
                    "config with the key to silence this",
                    self.nick, resolved or "<identity nick>",
                )
                if resolved:
                    self.nick = resolved
                return True
        logger.warning("relay: join failed %s: %s", r.status_code, r.text[:200])
        return False

    @staticmethod
    def _rows(payload: Any) -> list[dict]:
        if isinstance(payload, dict):
            payload = payload.get("messages", payload.get("partners", []))
        return [m for m in (payload or []) if isinstance(m, dict)]

    async def _reply_to(self, client: httpx.AsyncClient, partner: str, text: str) -> None:
        """Run the agent's turn and DM the reply back to `partner`."""
        try:
            resp = await self.agent.chat(text)
            content = getattr(resp, "content", None) or str(resp)
        except Exception as exc:  # noqa: BLE001 — a bad turn must not kill the loop
            logger.error("relay: agent turn failed for %s: %s", partner, exc)
            content = f"[agent error: {exc}]"
        await client.post(
            f"{self.base_url}/dms",
            headers=self._headers(),
            json={"to_nick": partner, "content": content},
        )
        logger.info("relay: replied to %s (%d chars)", partner, len(content))

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        """One pass: answer any new inbound human DMs. Returns #replies sent."""
        partners = self._rows(await self._get(client, "/dms/partners"))
        replies = 0
        for p in partners:
            partner = p.get("nick") or p.get("partner") or ""
            if not partner or partner == self.nick:
                continue
            thread = self._rows(await self._get(client, f"/dms/{partner}"))
            if not thread:
                continue
            last = thread[-1]
            last_id = str(last.get("id") or last.get("message_id") or "")
            from_nick = (last.get("from_nick") or last.get("from") or "").lower()
            # Only reply to a NEW message FROM the human (not our own, not already seen).
            if not last_id or self._seen.get(partner) == last_id:
                continue
            self._seen[partner] = last_id
            if from_nick and from_nick != self.nick.lower():
                await self._reply_to(client, partner, str(last.get("content", "")))
                replies += 1
        return replies

    async def poll_notifications(self, client: httpx.AsyncClient) -> list[dict]:
        """Poll stored notifications for this agent. Returns list of new notifications."""
        # GET /v1/notifications/{nick}?unread_only=true
        resp = await self._get(client, f"/notifications/{self.nick}?unread_only=true")
        if resp is None:
            self._notif_poll_failure_count += 1
            # Log warning once per 10 failures (~40s apart at 4s poll interval)
            if self._notif_poll_failure_count % 10 == 1:
                logger.warning(
                    "relay: notifications poll got no response (401/unreachable) — "
                    "token/identity mismatch?"
                )
            return []

        self._notif_poll_failure_count = 0
        notifs = resp.get("notifications", [])
        new_notifs = []
        ids_to_read = []

        for notif in notifs:
            notif_id = notif.get("id", "")
            if not notif_id or notif_id in self._seen_notif_ids:
                continue
            self._seen_notif_ids.add(notif_id)
            ids_to_read.append(notif_id)
            new_notifs.append(notif)

            # Handle notification types
            notif_type = notif.get("type", "")
            if notif_type == "mention":
                # For mentions, call the callback if provided; log at INFO otherwise
                if self.on_notification:
                    try:
                        self.on_notification(notif)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "relay: notification callback failed: %s", exc
                        )
                else:
                    logger.info(
                        "relay: notification: %s from %s in %s",
                        notif_type,
                        notif.get("from_nick", "?"),
                        notif.get("channel", "?"),
                    )
            elif notif_type in ("dm", "reaction", "thread_reply", "report", "system"):
                # Log other known types at INFO
                logger.info(
                    "relay: notification: %s from %s in %s",
                    notif_type,
                    notif.get("from_nick", "?"),
                    notif.get("channel", "?"),
                )
            else:
                # Log unknown types at DEBUG
                logger.debug(
                    "relay: notification: unknown type=%s from %s in %s",
                    notif_type,
                    notif.get("from_nick", "?"),
                    notif.get("channel", "?"),
                )

        # Mark retrieved notifications as read
        if ids_to_read:
            await client.post(
                f"{self.base_url}/notifications/{self.nick}/read",
                headers=self._headers(),
                json={"ids": ids_to_read},
            )
            logger.debug("relay: marked %d notifications as read", len(ids_to_read))

        return new_notifs

    async def run(self) -> None:
        """Join, then serve DMs forever (poll → reply loop)."""
        self._running = True
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30, verify=self.verify
        ) as client:
            if not await self.join(client):
                raise RuntimeError("relay join failed — check the token / nick / channel")
            # Prime _seen so we don't reply to the whole backlog on startup.
            for p in self._rows(await self._get(client, "/dms/partners")):
                partner = p.get("nick") or ""
                thread = self._rows(await self._get(client, f"/dms/{partner}")) if partner else []
                if thread:
                    self._seen[partner] = str(thread[-1].get("id") or thread[-1].get("message_id") or "")
            while self._running:
                try:
                    await self.poll_once(client)
                    await self.poll_notifications(client)
                except httpx.HTTPError as exc:
                    logger.warning("relay: poll error (continuing): %s", exc)
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
