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
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

import httpx

from ._tls import tls_verify

logger = logging.getLogger("adk.relay_client")

_DEFAULT_POLL_S = 4.0

#: awrelay's wire format: a human line, then the structured body in a fenced
#: block. Parsed here rather than imported -- adk ships standalone, and a hard
#: dependency on the awrelay package would make this module unimportable for
#: every customer agent that has only adk.
_FENCE_RE = re.compile(r"```awrelay\s*\n(?P<body>.*?)\n```", re.S)
#: Newest N channel rows read per pass. A channel this agent is mentioned in
#: rarely moves faster than this between polls, and a bigger window only costs
#: the relay work for rows the seen-set drops.
_CHANNEL_WINDOW = 30


def _envelope_payload(content: str) -> dict[str, Any]:
    """The structured half of an awrelay message, or {} for ordinary chat."""
    m = _FENCE_RE.search(content or "")
    if not m:
        return {}
    try:
        data = json.loads(m.group("body"))
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def addressed_to_me(content: str, nick: str) -> bool:
    """True when a CHANNEL message names this agent.

    Same rule the Claude-session side uses (`awrelay.inbox.addressed_to`), so a
    session that writes `--to lyra` and an agent that answers agree on what
    "addressed" means: the envelope's ``payload.to`` carries the nick or its
    session suffix, or the text @-mentions it. Everything else is broadcast --
    an agent that replies to broadcast turns a shared channel into noise.
    """
    if not nick:
        return False
    low = nick.lower()
    suffix = low.split("+", 1)[1] if "+" in low else ""
    payload = (_envelope_payload(content).get("payload") or {})
    to = payload.get("to") if isinstance(payload, dict) else None
    if isinstance(to, str):
        to = [to]
    for t in (to or []):
        t = str(t).strip().lower()
        if t == low or (suffix and (t == suffix or (len(t) >= 6 and suffix.startswith(t)))):
            return True
    return ("@" + low) in (content or "").lower()


def strip_envelope(content: str) -> str:
    """The human-readable half, without the fence and the kind prefix."""
    m = _FENCE_RE.search(content or "")
    text = (content or "")[: m.start()] if m else (content or "")
    return re.sub(r"^\[(finding|alert|request|steer|ack)\]\s*", "", text.strip())


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
        doors_url: str = "",
        humanity_path: str = "",
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
        # Channel messages already answered, so one mention gets one reply.
        self._seen_channel_ids: set[str] = set()
        self._channel_primed = False
        # Door passes per channel: (attestation, expires_at).
        self._door_cache: dict[str, tuple[str, float]] = {}
        # Whether this process has registered its nick on the channel yet.
        self._joined = False
        #: Where the doors plane lives. Genesis issues the passes and publishes
        #: no host port, so the local MCP gateway is the only reachable door.
        self.doors_url = (doors_url or "http://127.0.0.1:8182").rstrip("/")
        self.humanity_path = Path(humanity_path) if humanity_path else (
            Path.home() / ".aither" / "humanity-attestation")
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

    async def _ensure_joined(self, client: httpx.AsyncClient) -> None:
        """Join once per process; a repeat join is a no-op the relay accepts."""
        if self._joined:
            return
        try:
            self._joined = await self.join(client)
        except Exception as exc:  # noqa: BLE001 - the post below reports the real verdict
            logger.warning("relay: join attempt failed (%s); posting anyway", exc)

    async def _door_attestation(self, client: httpx.AsyncClient,
                                channel: str) -> Optional[str]:
        """A door pass for `channel`, from cache or freshly presented.

        `#agents` is an agent-only channel: the relay answers 403 with a remedy
        naming /doors/knock and /doors/present, and a client that cannot follow
        it simply never posts. Measured 2026-09-20 while wiring the agent-side
        reply: the loop selected the message, ran its turn, posted the answer
        into a 403 and reported success -- a reply that existed nowhere.

        None when there is nothing to present or the doors plane is unreachable:
        the caller then posts WITHOUT the header and lets the relay be the
        authority. A client must never decide it is exempt.
        """
        cached = self._door_cache.get(channel)
        if cached and cached[1] > time.time() + 5:
            return cached[0]
        try:
            evidence = self.humanity_path.read_text(encoding="utf-8").strip()
        except OSError:
            evidence = ""
        if not evidence:
            return None
        try:
            resp = await client.post(
                f"{self.doors_url}/doors/present",
                json={"door": f"channel:{channel}", "attestation": evidence},
                headers=self._headers(), timeout=20.0)
        except Exception as exc:  # noqa: BLE001 - an unreachable door is not a verdict
            logger.warning("relay: doors plane unreachable (%s); posting without a pass", exc)
            return None
        if resp.status_code != 200:
            logger.warning("relay: door %s refused admission (HTTP %s): %s",
                           channel, resp.status_code, resp.text[:160])
            return None
        data = resp.json()
        token = data.get("attestation")
        if not token:
            return None
        expires = float(data.get("expires_at")
                        or (time.time() + float(data.get("ttl_s", 300))))
        self._door_cache[channel] = (token, expires)
        return token

    async def _post_channel(self, client: httpx.AsyncClient, text: str,
                            to_nick: str, correlation_id: str = "") -> None:
        """Post into the channel, ADDRESSED back at the sender, in awrelay's wire
        format -- so the reply reaches that session in-turn (its PostToolUse drain
        selects addressed rows) instead of waiting for its owner to type."""
        body = json.dumps({
            "kind": "ack", "sender": self.nick,
            "payload": {"to": [to_nick]},
            "correlation_id": correlation_id or "",
            "sent_at": "",
        }, ensure_ascii=False, sort_keys=True)
        content = f"[ack] {text}\n```awrelay\n{body}\n```"
        url = f"{self.base_url}/channels/{self.channel.lstrip('#')}/messages"
        payload = {"channel": self.channel, "nick": self.nick,
                   "content": content, "agent": True}

        async def _send(extra: Optional[dict] = None):
            headers = dict(self._headers())
            headers.update(extra or {})
            return await client.post(url, headers=headers, json=payload)

        # MEMBERSHIP FIRST. `#agents` refuses a non-member with the same 403 a
        # door uses ("agent-only channel. You don't have permission"), so a loop
        # driven without `run()` -- a probe, a one-shot answer, a caller that
        # polls itself -- posted into a refusal and logged success. `run()`
        # happens to join first; nothing else did.
        await self._ensure_joined(client)
        pass_token = await self._door_attestation(client, self.channel)
        resp = await _send({"X-Door-Attestation": pass_token} if pass_token else None)
        if resp.status_code == 403:
            # MEMBERSHIP IS SERVER STATE AND IT CAN VANISH UNDER US. The relay lost its
            # roster when its container restarted (2026-09-20, an HA pair cycling), and
            # this loop kept posting into "#agents is a agent-only channel. You don't
            # have permission" forever, because `_joined` is per-PROCESS and was still
            # True. A 403 is the one signal that says "join again", so it does.
            self._joined = False
            await self._ensure_joined(client)
            resp = await _send({"X-Door-Attestation": pass_token} if pass_token else None)
        if resp.status_code == 403 and not pass_token:
            # The 403 is how a door announces itself; knock once, then retry.
            pass_token = await self._door_attestation(client, self.channel)
            if pass_token:
                resp = await _send({"X-Door-Attestation": pass_token})
        if resp.status_code not in (200, 201):
            # LOUD. A silent failure here is the worst shape this loop can take:
            # the agent believes it answered, the peer waits forever, and nothing
            # anywhere is red.
            logger.error("relay: POST %s -> %s: %s -- the answer to %s was NOT posted",
                         url, resp.status_code, resp.text[:200], to_nick)
            return
        logger.info("relay: answered %s in %s (%d chars)", to_nick, self.channel, len(text))

    async def poll_channel_once(self, client: httpx.AsyncClient) -> int:
        """One pass over the channel: answer messages ADDRESSED to this agent.

        This is the other half of in-turn coordination. A Claude session can
        already address an agent (`awrelay send --to <nick>`) and hear an answer
        inside its own turn; until now nothing on the agent side answered, so
        every exchange still needed a human to relay it. Broadcast rows are left
        alone on purpose -- an agent that replies to everything is noise, and
        noise is what makes people mute the channel the coordination lives in.
        """
        rows = self._rows(await self._get(
            client, f"/channels/{self.channel.lstrip('#')}/messages"
            f"?limit={_CHANNEL_WINDOW}"))
        # First pass after start only marks what is already there. Otherwise a
        # restart re-answers the whole window -- the same mention, N times, from
        # an agent that looks like it is stuck.
        # Primed even on an EMPTY window: priming only on a non-empty read means
        # an agent that joins a quiet channel spends its priming pass on the first
        # real message and never answers it.
        if not self._channel_primed:
            self._channel_primed = True
            for r in rows:
                rid = str(r.get("id") or r.get("message_id") or "")
                if rid:
                    self._seen_channel_ids.add(rid)
            return 0
        replies = 0
        for r in rows:
            rid = str(r.get("id") or r.get("message_id") or "")
            sender = str(r.get("nick") or r.get("from_nick") or "")
            content = str(r.get("content") or "")
            if not rid or rid in self._seen_channel_ids:
                continue
            self._seen_channel_ids.add(rid)
            if not sender or sender.lower() == self.nick.lower():
                continue
            if not addressed_to_me(content, self.nick):
                continue
            text = strip_envelope(content)
            # WHO AND WHERE, not just what. Measured 2026-09-20 on the first live
            # answer: asked "what channel are you on?", the agent replied "No channel
            # on my end -- I'm just here on your Windows machine", because the turn
            # carried the words and none of the situation. A reply that does not know
            # it is in a shared channel reads as a different agent than the one people
            # addressed.
            framed = f"[relay {self.channel}] {sender} says: {text}"
            try:
                resp = await self.agent.chat(framed)
                answer = getattr(resp, "content", None) or str(resp)
            except Exception as exc:  # noqa: BLE001 - a bad turn must not kill the loop
                logger.error("relay: agent turn failed for %s: %s", sender, exc)
                answer = f"[agent error: {exc}]"
            corr = str(_envelope_payload(content).get("correlation_id") or "")
            await self._post_channel(client, answer, sender, corr)
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
            self._joined = True
            # Prime _seen so we don't reply to the whole backlog on startup.
            for p in self._rows(await self._get(client, "/dms/partners")):
                partner = p.get("nick") or ""
                thread = self._rows(await self._get(client, f"/dms/{partner}")) if partner else []
                if thread:
                    self._seen[partner] = str(thread[-1].get("id") or thread[-1].get("message_id") or "")
            while self._running:
                try:
                    await self.poll_once(client)
                    await self.poll_channel_once(client)
                    await self.poll_notifications(client)
                except httpx.HTTPError as exc:
                    logger.warning("relay: poll error (continuing): %s", exc)
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
