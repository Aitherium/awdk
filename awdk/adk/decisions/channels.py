"""Deliver decision cards to the owner wherever they are — DM, not channel.

The point of a card is that it follows you. At a desk that is the popup window; away
from the desk it is a direct message. This module is the bridge between the card
store and ``adk.channels``, which already implements Discord/Telegram/Slack/webhook
adapters — none of that is reimplemented here.

**This is a DM bridge, not a chat-channel bot.** The owner sets up a bot with their
own token, binds it to their own user id, and the bot DMs them. There is no shared
channel, no server, and no other participant. That is a deliberate design choice
and it is also the entire security model:

    a card answer STEERS A CODING AGENT WITH FILESYSTEM ACCESS

so the question "who may answer" is an authorization decision, and it is
fail-closed at every path per `.claude/rules/security-review-patterns.md`:

* no config for a platform          -> deny
* no ``owner_user_id`` recorded     -> deny (an unbound bot answers nobody)
* sender id != owner id             -> deny
* message not in a DM               -> deny, even from the owner

The last one matters more than it looks. If the bot is ever added to a server, a
message in a public channel must not be able to answer a card even when the owner
typed it — otherwise anyone who can spoof a display name, or who simply replies
first in a busy channel, is steering the agent. Binding to DM keeps the trusted
surface exactly one conversation wide.

Adding a substrate is a config entry plus an adapter that already exists; the card
rendering, reply grammar and authorization are shared. Telegram and email reuse
this bridge unchanged — Discord is just the one that ships as the free example.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from adk.decisions.render import human_age, render_markdown
from adk.decisions.store import (
    CLOSED_STATUSES,
    DecisionCard,
    DecisionError,
    DecisionStore,
    decisions_dir,
)

#: Platforms this bridge knows how to talk to. Each maps to an adapter that
#: already exists in ``adk.channels``; nothing here is platform-specific except
#: the message length limit, which that module already owns.
SUPPORTED = ("discord", "telegram", "slack")

#: A card id anywhere in the reply text.
_CARD_RE = re.compile(r"\b(d-[23456789abcdefghjkmnpqrstuvwxyz]{4,12})\b", re.IGNORECASE)

#: Words that mean "show me what is waiting" rather than "answer something".
_LIST_WORDS = {"cards", "decisions", "pending", "waiting", "list", "?"}


class ChannelConfigError(RuntimeError):
    """The channel configuration is missing or unusable."""


def config_path() -> Path:
    return decisions_dir() / "channels.json"


@dataclass
class ChannelConfig:
    """One substrate the owner has bound to their cards.

    ``token_env`` holds the NAME of an environment variable, never a token. A bot
    token in a config file is a credential on disk that syncs, backs up and ends up
    in a support bundle; the same rule the rest of this platform follows
    (`.claude/rules/secret-safety.md`).
    """

    platform: str
    enabled: bool = False
    token_env: str = ""
    owner_user_id: str = ""
    #: Where to send. For Discord this is the owner's user id (a DM is opened to
    #: it); the adapter's channel_id for an already-open DM is cached at runtime.
    deliver_to: str = ""
    #: Cards below this urgency are not sent to this substrate. Away from the desk
    #: the owner wants the ones that matter, not all of them.
    min_urgency: str = "normal"
    #: DM-only. Flipping this to False is a deliberate, documented downgrade.
    require_direct_message: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, platform: str, raw: dict[str, Any]) -> "ChannelConfig":
        return cls(
            platform=platform,
            enabled=bool(raw.get("enabled")),
            token_env=str(raw.get("token_env") or ""),
            owner_user_id=str(raw.get("owner_user_id") or ""),
            deliver_to=str(raw.get("deliver_to") or ""),
            min_urgency=str(raw.get("min_urgency") or "normal"),
            require_direct_message=bool(raw.get("require_direct_message", True)),
        )

    def token(self) -> str:
        """The live token, read from the environment at use time."""
        return os.getenv(self.token_env, "").strip() if self.token_env else ""


_URGENCY_RANK = {"low": 0, "normal": 1, "high": 2, "critical": 3}


def load_config() -> dict[str, ChannelConfig]:
    target = config_path()
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A malformed config must not silently mean "no channels" — that would
        # look identical to "nothing configured" and the owner would never learn
        # their cards stopped being delivered.
        raise ChannelConfigError(f"{target} is not readable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ChannelConfigError(f"{target} must contain a JSON object")

    # A malformed ENTRY is raised on rather than skipped. Dropping it
    # silently would leave a caller unable to tell "nothing configured" from
    # "your Discord block is broken and your cards are going nowhere" — and the
    # second one looks exactly like the first from every surface.
    bad = [name for name, body in raw.items() if not isinstance(body, dict)]
    if bad:
        raise ChannelConfigError(
            f"{target}: these entries are not objects and were NOT loaded: {', '.join(bad)}"
        )
    unknown = [name for name in raw if name not in SUPPORTED]
    if unknown:
        raise ChannelConfigError(
            f"{target}: unsupported platform(s) {', '.join(unknown)} "
            f"— known: {', '.join(SUPPORTED)}"
        )
    return {name: ChannelConfig.from_dict(name, body) for name, body in raw.items()}


def save_config(configs: dict[str, ChannelConfig]) -> Path:
    target = config_path()
    payload = {name: cfg.to_dict() for name, cfg in configs.items()}
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError as exc:
        # Not fatal: the file holds no secret by design (tokens live in env vars,
        # see ChannelConfig.token_env), so a permissive mode is not a credential
        # exposure. Reported rather than swallowed so a filesystem that rejects
        # every permission change is visible instead of assumed.
        import sys as _sys

        _sys.stderr.write(f"[decision-channels] could not restrict {target}: {exc}\n")
    return target


# ── authorization ───────────────────────────────────────────────────────────────


@dataclass
class AuthDecision:
    """Why a message was or was not allowed to answer a card."""

    allowed: bool
    reason: str


def authorize(
    cfg: Optional[ChannelConfig],
    *,
    user_id: str,
    is_direct_message: bool,
) -> AuthDecision:
    """Fail-closed: every non-happy path denies.

    Kept as a pure function with no I/O precisely so it can be exhaustively
    tested — an authorization gate whose denial paths are never exercised is how
    a fail-open default survives review.
    """
    if cfg is None:
        return AuthDecision(False, "no configuration for this platform")
    if not cfg.enabled:
        return AuthDecision(False, f"{cfg.platform} is configured but disabled")
    if not cfg.owner_user_id:
        return AuthDecision(False, "no owner_user_id bound — an unbound bot answers nobody")
    if not user_id:
        return AuthDecision(False, "message carried no sender id")
    if user_id != cfg.owner_user_id:
        return AuthDecision(False, "sender is not the bound owner")
    if cfg.require_direct_message and not is_direct_message:
        return AuthDecision(False, "not a direct message — DM-only is enforced")
    return AuthDecision(True, "bound owner in a direct message")


# ── rendering ───────────────────────────────────────────────────────────────────


def render_for_chat(card: DecisionCard) -> str:
    """A card as a chat message — answerable from a phone without scrolling."""
    lines = [f"**{card.title}**"]
    if card.summary:
        lines += ["", card.summary]
    if card.facts:
        lines += [""] + [f"• {f}" for f in card.facts[:5]]
    if card.options:
        lines += ["", "Reply with a number:"]
        for index, opt in enumerate(card.options, start=1):
            star = " ⭐" if opt.recommended else ""
            tail = f" — {opt.consequence}" if opt.consequence else ""
            lines.append(f"`{index}` **{opt.label}**{star}{tail}")
    left = card.seconds_left
    if card.default_key and left is not None:
        lines += ["", f"_No answer → `{card.default_key}` in {human_age(left)}._"]
    elif card.default_key:
        lines += ["", f"_No answer → `{card.default_key}` (no deadline)._"]
    lines.append(f"\n`{card.id}`")
    return "\n".join(lines)


# ── the bridge ──────────────────────────────────────────────────────────────────


class DecisionChannelBridge:
    """Turns chat messages into card answers, and cards into chat messages.

    Wire it to any adapter in ``adk.channels`` by passing :meth:`on_message` as
    that adapter's ``on_message`` handler — the signature matches deliberately.
    """

    def __init__(
        self,
        store: Optional[DecisionStore] = None,
        configs: Optional[dict[str, ChannelConfig]] = None,
    ) -> None:
        self.store = store or DecisionStore()
        self._configs = configs
        self._lock = threading.RLock()
        #: platform -> the last card id sent, so a bare "2" is unambiguous.
        self._last_sent: dict[str, str] = {}

    @property
    def configs(self) -> dict[str, ChannelConfig]:
        if self._configs is None:
            self._configs = load_config()
        return self._configs

    def should_deliver(self, card: DecisionCard, cfg: ChannelConfig) -> bool:
        if not cfg.enabled or not card.is_open:
            return False
        if not card.options:
            # Nothing to reply with. Same rule as the popup: a message you cannot
            # act on is an interruption, not a decision surface.
            #
            # EXCEPT credential cards (kind="credential"): their ONLY door is
            # the masked prompt (secure_prompt.py, DC008) — no popup renders
            # them, the deck reply box is not allowed to, and without a DM the
            # owner never learns the ask exists. Measured 2026-08-27: three
            # secure-input cards (LAMBDA/HETZNER/GOOGLE) sat undelivered for
            # hours with 4912 deliveries for option cards around them. The DM
            # for a credential card carries the answer instruction; the value
            # itself still never travels through any channel.
            if card.kind != "credential":
                return False
        floor = _URGENCY_RANK.get(cfg.min_urgency, 1)
        rank = _URGENCY_RANK.get(card.urgency, 1)
        if rank < floor:
            return False

        # PRESENCE, the Slack rule: the phone stays quiet while you are at the
        # desktop, because the popup already has you. A card that arrives on a
        # screen you are looking at AND buzzes in your pocket trains you to
        # ignore the pocket.
        #
        # CRITICAL is never suppressed. Presence decides whether a REDUNDANT
        # channel fires, never whether an urgent card reaches somebody.
        #
        # And it fails toward DELIVERING: unmeasurable presence (no API,
        # non-Windows, permission error) counts as away. Suppressing on
        # evidence nobody gathered is the failure cards exist to prevent.
        if rank < _URGENCY_RANK.get("critical", 99) and card.kind != "credential":
            # Credential cards bypass the presence suppression: "popup is
            # enough" is FALSE for them (no popup renders them — the masked
            # prompt is the only door), so suppressing the DM makes the ask
            # invisible. Fail toward delivering, per this module's own rule.
            #
            # The suppression holds ONLY when a popup will actually show:
            # "popup is enough" is a statement about a window that exists.
            # With the popup plane disabled (AITHER_DECISIONS_POPUP=0 — the
            # owner's background-first preference, 2026-08-29), an at-desk
            # card must reach the DM instead of vanishing into a suppressed
            # silence — the exact "the feature is just off" shape.
            try:
                from adk.decisions.notify import popup_enabled
                from adk.decisions.presence import is_at_desk, presence_note
                if is_at_desk() and popup_enabled():
                    # Logged, not silent: a notification that did not happen is
                    # otherwise indistinguishable from one that failed.
                    print(f"[decisions] {card.id}: {presence_note()}")
                    return False
            except Exception as exc:  # noqa: BLE001 - best-effort, but LOUD
                # Never silent. A swallowed presence error means every card
                # quietly reverts to always-DM, or worse never-DM, and nobody
                # can tell which — the exact "the feature is just off" shape.
                print(f"[decisions] presence check failed "
                      f"({type(exc).__name__}: {exc}) — delivering anyway")
        return True

    def note_sent(self, platform: str, card_id: str) -> None:
        with self._lock:
            self._last_sent[platform] = card_id

    # ── inbound ───────────────────────────────────────────────────────────────

    def resolve_target(self, platform: str, text: str) -> tuple[Optional[DecisionCard], str]:
        """Work out which card a reply refers to. Returns (card, why-not)."""
        match = _CARD_RE.search(text or "")
        if match:
            try:
                card = self.store.get(match.group(1).lower())
            except DecisionError as exc:
                return None, str(exc)
            if card is None:
                return None, f"no card {match.group(1)}"
            return card, ""

        open_cards = self.store.list()
        if not open_cards:
            return None, "nothing is waiting"

        last = self._last_sent.get(platform)
        if last:
            for card in open_cards:
                if card.id == last:
                    return card, ""
        if len(open_cards) == 1:
            return open_cards[0], ""
        # Ambiguous on purpose: guessing which of several cards a bare "2" means
        # could apply an answer to the wrong decision, and a card answered wrongly
        # is worse than one answered late.
        listing = ", ".join(c.id for c in open_cards[:6])
        return None, f"{len(open_cards)} cards are open — name one: {listing}"

    @staticmethod
    def interpret(card: DecisionCard, text: str) -> tuple[str, str]:
        """Map reply text to an option key. Returns (key, why-not)."""
        cleaned = _CARD_RE.sub("", text or "").strip().lower()
        if not cleaned:
            return "", "no answer in that message"
        token = cleaned.split()[0].strip(".,:;!)")
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(card.options):
                return card.options[index - 1].key, ""
            return "", f"{index} is not one of {len(card.options)} options"
        for opt in card.options:
            if opt.key.lower() == token:
                return opt.key, ""
        for opt in card.options:
            if opt.label.lower().startswith(token) and len(token) >= 3:
                return opt.key, ""
        valid = ", ".join(f"{i}={o.key}" for i, o in enumerate(card.options, start=1))
        return "", f"could not read '{token}' as an option — try {valid}"

    def dm_handler(self):
        """An ``adk.channels.MessageHandler`` that asserts DM-only semantics.

        ``adk.channels`` calls handlers with exactly four positional arguments,
        so the DM fact cannot travel in that signature — it has to come from the
        wiring. This factory is the ONLY supported way to produce a 4-arg handler,
        and its name is the assertion: calling it says "the adapter I am wiring
        this to can see nothing but direct messages".

        The alternative — defaulting ``is_direct_message`` to True on
        :meth:`on_message` — was the first shape and it was wrong. A default that
        grants trust is invisible at the call site, so wiring up a server-capable
        adapter would silently hand every channel member the ability to steer a
        coding agent, and nothing in the diff would look like a security change.
        With no default, that mistake is a TypeError at wiring time instead of a
        fail-open gate in production.
        """

        async def handler(platform: str, channel_id: str, user_id: str, text: str):
            return await self.on_message(
                platform, channel_id, user_id, text, is_direct_message=True,
            )

        return handler

    async def on_message(
        self,
        platform: str,
        channel_id: str,
        user_id: str,
        text: str,
        *,
        is_direct_message: bool,
    ) -> Optional[str]:
        """Handle one inbound message.

        ``is_direct_message`` is REQUIRED and has no default on purpose — see
        :meth:`dm_handler`. An adapter that can see server channels must compute
        it and pass it; passing False denies rather than degrading quietly.
        """
        cfg = self.configs.get(platform)
        verdict = authorize(cfg, user_id=user_id, is_direct_message=is_direct_message)
        if not verdict.allowed:
            # Deliberately terse to the sender and specific in the log: telling an
            # unauthorized party which id WOULD be accepted is an information leak.
            return "Not authorized."

        body = (text or "").strip()
        if not body:
            return None
        if body.lower().strip("/ ") in _LIST_WORDS:
            return self.summary_message()

        card, problem = self.resolve_target(platform, body)
        if card is None:
            return problem
        if card.status in CLOSED_STATUSES:
            return f"{card.id} is already {card.status} ({card.answer or 'no answer'})."

        choice, why_not = self.interpret(card, body)
        if not choice:
            return f"{why_not}\n\n{render_for_chat(card)}"

        try:
            # deliver=False: this method delivers explicitly below, and
            # delivering twice drops the SAME instruction into the session's
            # mailbox twice — the agent reads it as two separate asks.
            answered = self.store.answer(card.id, choice, via=platform, deliver=False)
        except DecisionError as exc:
            return str(exc)

        delivered = self.store.deliver_answer(answered)
        opt = answered.option(answered.answer or "")
        confirmation = f"✅ {answered.id} → **{opt.label if opt else answered.answer}**"
        if not delivered:
            # Say so. "Recorded" and "reached the agent" are different facts and
            # only the second one unblocks anything.
            confirmation += "\n_(recorded — this card names no session to steer)_"
        remaining = len(self.store.list())
        if remaining:
            confirmation += f"\n\n{remaining} still waiting."
        return confirmation

    def summary_message(self) -> str:
        cards = self.store.list()
        if not cards:
            return "Nothing waiting. 🎉"
        lines = [f"**{len(cards)} waiting**", ""]
        for card in cards[:8]:
            lines.append(f"`{card.id}` · {human_age(card.age_seconds)} · {card.title}")
        lines += ["", "Send a card id to see it, or `<id> <number>` to answer."]
        return "\n".join(lines)

    def card_message(self, card_id: str) -> str:
        card = self.store.get(card_id)
        return render_for_chat(card) if card else f"no card {card_id}"

    def markdown(self, card_id: str) -> str:
        card = self.store.get(card_id)
        return render_markdown(card) if card else f"no card {card_id}"


# ── self-test ───────────────────────────────────────────────────────────────────


def _self_test() -> int:
    """Exercise every DENIAL path, because that is where fail-open hides."""
    import tempfile

    from adk.decisions.store import DecisionOption, DecisionSource

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name} {detail}")
            failures.append(name)

    good = ChannelConfig(
        platform="discord", enabled=True, token_env="X", owner_user_id="owner-1",
    )

    check("denies with no config", not authorize(None, user_id="owner-1",
                                                 is_direct_message=True).allowed)
    check("denies when disabled",
          not authorize(ChannelConfig(platform="discord", enabled=False,
                                      owner_user_id="owner-1"),
                        user_id="owner-1", is_direct_message=True).allowed)
    check("denies when unbound",
          not authorize(ChannelConfig(platform="discord", enabled=True, owner_user_id=""),
                        user_id="owner-1", is_direct_message=True).allowed)
    check("denies an empty sender",
          not authorize(good, user_id="", is_direct_message=True).allowed)
    check("denies a stranger",
          not authorize(good, user_id="someone-else", is_direct_message=True).allowed)
    check("denies the owner OUTSIDE a dm",
          not authorize(good, user_id="owner-1", is_direct_message=False).allowed)
    check("allows the bound owner in a dm",
          authorize(good, user_id="owner-1", is_direct_message=True).allowed)

    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["AITHER_DECISIONS_DIR"] = str(Path(tmp) / "cards")
        os.environ["AITHER_STEER_DIR"] = str(Path(tmp) / "steer")
        store = DecisionStore(Path(tmp) / "cards")
        bridge = DecisionChannelBridge(store, {"discord": good})

        card = store.create(DecisionCard(
            id="", title="Ship it?", kind="decision", default_key="no",
            options=[
                DecisionOption(key="yes", label="Ship", consequence="goes live"),
                DecisionOption(key="no", label="Hold", recommended=True),
            ],
            source=DecisionSource(session_id="chat-test"),
        ))

        async def say(text: str, *, user: str = "owner-1", dm: bool = True) -> Optional[str]:
            return await bridge.on_message("discord", "dm-1", user, text,
                                           is_direct_message=dm)

        check("a stranger cannot answer",
              asyncio.run(say("1", user="intruder")) == "Not authorized.")
        check("the owner cannot answer from a server channel",
              asyncio.run(say("1", dm=False)) == "Not authorized.")
        check("card still open after both denials",
              (store.get(card.id) or card).is_open)

        check("renders a card for chat", "Ship it?" in render_for_chat(card))
        check("lists what is waiting", card.id in bridge.summary_message())

        bad = asyncio.run(say("banana"))
        check("an unreadable answer explains itself", "could not read" in (bad or ""))
        check("card still open after a bad answer", (store.get(card.id) or card).is_open)

        ok = asyncio.run(say(f"{card.id} 1"))
        check("answers by number", "✅" in (ok or ""))
        refreshed = store.get(card.id)
        check("recorded the right option", refreshed is not None and refreshed.answer == "yes")
        box = Path(tmp) / "steer" / "chat-test"
        # Mailbox files are suffixed by kind now (-answer / -steer), so this
        # asserts the ANSWER arrived rather than "some file appeared" — and the
        # glob is deliberately `*-answer*` so a DOUBLE delivery fails here. Two
        # copies of one instruction reads to the agent as two separate asks, and
        # the un-suffixed version of this check could not see it: the second
        # write lands as `-answer-1.md`, which `*-answer.md` does not match.
        delivered = list(box.glob("*-answer*.md")) if box.exists() else []
        check("steered the session exactly once", len(delivered) == 1,
              f"found {len(delivered)}")

        again = asyncio.run(say(f"{card.id} 2"))
        check("refuses to re-answer", "already" in (again or "").lower())

        # Ambiguity must not resolve to a guess.
        for _ in range(2):
            store.create(DecisionCard(
                id="", title="another", kind="decision", default_key="a",
                options=[DecisionOption(key="a", label="A")],
            ))
        bridge._last_sent.clear()  # noqa: SLF001 - simulating a fresh process
        ambiguous = asyncio.run(say("1"))
        check("refuses to guess between open cards", "name one" in (ambiguous or ""))

        # The unsafe default must stay gone. If someone reintroduces
        # `is_direct_message: bool = True`, wiring a server-capable adapter would
        # silently grant DM trust to every channel member — invisible in a diff.
        # Calling without it must be a TypeError, not a permissive default.
        try:
            asyncio.run(bridge.on_message("discord", "c", "owner-1", "1"))
            check("on_message refuses to assume DM", False, "it defaulted instead of raising")
        except TypeError:
            check("on_message refuses to assume DM", True)

        # ...and the only 4-arg handler must be the explicitly-named DM one.
        handler = bridge.dm_handler()
        import inspect

        check("dm_handler matches the MessageHandler signature",
              len(inspect.signature(handler).parameters) == 4)
        store.create(DecisionCard(
            id="", title="via handler", kind="decision", default_key="a",
            options=[DecisionOption(key="a", label="A")],
            source=DecisionSource(session_id="chat-test"),
        ))
        only = [c for c in store.list() if c.title == "via handler"][0]
        via = asyncio.run(handler("discord", "dm-1", "owner-1", f"{only.id} 1"))
        check("dm_handler answers a card", "✅" in (via or ""))
        check("dm_handler still refuses a stranger",
              asyncio.run(handler("discord", "dm-1", "nope", "1")) == "Not authorized.")

    print()
    if failures:
        print(f"SELF-TEST FAILED — {len(failures)}: {', '.join(failures)}")
        return 1
    print("channel bridge self-test passed — every denial path denies")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
