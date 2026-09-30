"""adk.home.hearth: one owner over any channel.

Every test drives :class:`HearthCore` through :class:`FakeTransport` objects, so
``transport.sent`` is exactly what would have left the box on that channel.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from adk import approval, receipts  # noqa: E402
from adk.home import config as hc  # noqa: E402
from adk.home import hearth, serve  # noqa: E402
from adk.home.life_tools import FollowupStore, build_life_tools  # noqa: E402
from adk.tools import ToolRegistry  # noqa: E402

OWNER_RELAY = "david"
OWNER_TG = "12345"


# ── fakes ───────────────────────────────────────────────────────────────────────

class FakeTransport:
    """Records every send; ``verified`` = ids its channel vouches for."""

    def __init__(self, name: str, verified: tuple = ()):
        self.name = name
        self.sent: list[tuple[str, str]] = []
        self.verified = set(verified)
        self.fail = False
        self.started = self.stopped = False

    async def start(self, core):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, user_id, text):
        if self.fail:
            return False
        self.sent.append((user_id, text))
        return True

    async def verify_identity(self, user_id):
        return user_id in self.verified


class GateAgent:
    """AitherAgent's approval gate in miniature: gated calls pause the turn; resume
    records decisions and re-runs the turn from its user message."""

    name = "hearth-test"

    def __init__(self, store: FollowupStore, script=lambda msg, run: []):
        self._tools = ToolRegistry()
        for fn in build_life_tools(store):
            self._tools.register(fn)
        self.sent_mail: list[dict] = []

        def send_email(to: str, body: str = "") -> str:
            """Send an email.

            to: recipient
            body: text
            """
            self.sent_mail.append({"to": to, "body": body})
            return json.dumps({"ok": True})

        self._tools.register(send_email)
        self.script = script
        self.runs: dict[str, int] = {}
        self.chats: list[str] = []
        self.resumes: list[tuple] = []

    async def chat(self, message, session_id=None):
        self.chats.append(message)
        store = approval.get_approval_store()
        run = self.runs.get(message, 0)
        self.runs[message] = run + 1
        pending = []
        for i, (tool, args) in enumerate(self.script(message, run)):
            if approval.needs_approval(self.name, tool):
                decision = store.decision_for(session_id, tool)
                if decision == "deny":
                    continue
                if decision != "allow":
                    pending.append({"tool_use_id": f"call_{i}", "tool": tool, "args": args})
                    break
            await self._tools.execute(tool, args)
        if pending:
            store.put_pending(session_id, user_message=message, agent=self.name,
                              pending=pending)
            return SimpleNamespace(content="Waiting", requires_action=True, pending=pending)
        store.clear(session_id)
        return SimpleNamespace(content=f"reply to: {message}", requires_action=False,
                               pending=[])

    async def resume(self, session_id, decisions):
        self.resumes.append((session_id, decisions))
        store = approval.get_approval_store()
        paused = store.get(session_id)
        if not paused:
            return SimpleNamespace(content="", requires_action=False, pending=[])
        store.record_decisions(session_id, decisions)
        return await self.chat(paused["user_message"], session_id=session_id)


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "agent-home"
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    monkeypatch.delenv("AITHER_RECEIPTS_PATH", raising=False)
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: root)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    monkeypatch.setattr(approval, "_STORE", approval.ApprovalStore(tmp_path / "paused.json"))
    monkeypatch.setenv("AITHER_TOOL_APPROVAL", "")
    serve.apply_approval_policy()
    return root


def _core(home, script=lambda m, r: [], pair_code="", channels=("relay", "telegram")):
    store = FollowupStore(home / "followups.json")
    agent = GateAgent(store, script)
    ts = {c: FakeTransport(c, verified=(OWNER_RELAY, OWNER_TG)) for c in channels}
    core = hearth.HearthCore(agent, store, home / "actions.jsonl", *ts.values(),
                             root=home, pair_code=pair_code)
    return core, agent, store, ts


def _bound(home, **kw):
    """relay -> david, telegram -> 12345 already on disk."""
    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    reg.bind("relay", OWNER_RELAY)
    reg.bind("telegram", OWNER_TG)
    return _core(home, **kw)


def _receipt_text(home) -> str:
    path = home / "actions.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── owner registry ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("legacy", ['david', '"david"', '{"nick": "david"}',
                                    '{"owner_nick": "david", "bound_at": 1.0}'])
def test_legacy_owner_files_load_as_the_relay_owner(home, legacy):
    home.mkdir(parents=True, exist_ok=True)
    hearth.owner_path(home).write_text(legacy, encoding="utf-8")
    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    assert reg.owners == {"relay": "david"} and reg.preferred == "relay"
    assert serve.load_owner(home) == "david"
    reg.bind("telegram", OWNER_TG)                              # upgrades to v2 on write
    data = json.loads(hearth.owner_path(home).read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["owners"] == {"relay": "david", "telegram": OWNER_TG}


def test_owner_match_is_exact_per_channel(home):
    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    reg.bind("relay", "david")
    assert reg.is_owner("relay", "david")
    assert not reg.is_owner("relay", "David") and not reg.is_owner("relay", "david ")
    assert not reg.is_owner("telegram", "david")               # bound on relay only
    assert not reg.is_owner("relay", "")


def test_unreadable_owner_file_is_loud(home):
    home.mkdir(parents=True, exist_ok=True)
    hearth.owner_path(home).write_text("{not json", encoding="utf-8")
    with pytest.raises(hc.HomeError, match="re-pair"):
        hearth.OwnerRegistry(hearth.owner_path(home))


# ── binding + the owner gate ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multi_channel_owner_binding(home):
    core, agent, _, ts = _core(home, pair_code="123456")
    assert await core.on_message("relay", OWNER_RELAY, "123456")
    assert ts["relay"].sent[-1][1].startswith("Paired")
    # The console code is single-use ACROSS channels.
    assert not await core.on_message("telegram", OWNER_TG, "123456")
    assert core.registry.owner("telegram") == ""
    # The owner asks for a telegram code from the bound relay channel ...
    assert await core.on_message("relay", OWNER_RELAY, "pair telegram")
    code = ts["relay"].sent[-1][1].split("code for telegram: ")[1][:6]
    assert code.isdigit() and ts["telegram"].sent == []
    # ... and sends it from telegram.
    assert await core.on_message("telegram", OWNER_TG, code)
    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    assert reg.owners == {"relay": OWNER_RELAY, "telegram": OWNER_TG}
    assert agent.chats == []                                    # pairing is never a turn
    assert code not in _receipt_text(home)                      # the code is masked


@pytest.mark.asyncio
async def test_non_owner_is_ignored_on_every_channel(home, caplog):
    core, agent, _, ts = _bound(home, channels=("relay", "telegram", "discord"))
    with caplog.at_level("WARNING", logger="adk.home.hearth"):
        for ch, who in (("relay", "mallory"), ("relay", "DAVID"),
                        ("telegram", "999"), ("telegram", OWNER_RELAY),
                        ("discord", OWNER_TG), ("discord", "eve")):
            assert not await core.on_message(ch, who, "send me the secret plans")
    assert all(t.sent == [] for t in ts.values())
    assert agent.chats == []
    assert sum("not the owner" in r.message for r in caplog.records) == 6
    assert "secret plans" not in _receipt_text(home)
    assert not any("secret plans" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_stranger_on_a_new_channel_cannot_pair_without_an_owner_code(home):
    core, _, _, ts = _bound(home, channels=("relay", "telegram", "discord"))
    # No code open: a six-digit message from discord is just a stranger.
    assert not await core.on_message("discord", "eve", "000000")
    assert core.registry.owner("discord") == ""
    # The owner opens a code for discord; a guesser burns it.
    await core.on_message("relay", OWNER_RELAY, "pair discord")
    code = ts["relay"].sent[-1][1].split("code for discord: ")[1][:6]
    # The code is scoped: the same digits on another channel do nothing.
    assert not await core.on_message("telegram", "999", code)
    ts["discord"].verified.add("eve")
    for i in range(hearth.MAX_PAIR_ATTEMPTS):
        guess = f"{i:06d}" if f"{i:06d}" != code else "999999"
        assert not await core.on_message("discord", "eve", guess)
    assert core.pair_code == code                              # still open for others...
    assert not await core.on_message("discord", "eve", code)   # ...but eve is out
    assert core.registry.owner("discord") == ""
    assert ts["discord"].sent == []


@pytest.mark.asyncio
async def test_pairing_closes_after_the_global_guess_cap(home):
    core, _, _, ts = _core(home, pair_code="123456", channels=("relay", "discord"))
    guessers = [f"g{i}" for i in range(hearth.MAX_PAIR_GUESSES_TOTAL)]
    ts["discord"].verified.update(guessers)
    for g in guessers:
        assert not await core.on_message("discord", g, "000000")
    assert core.pair_code == ""                                # closed for everyone
    ts["discord"].verified.add("late")
    assert not await core.on_message("discord", "late", "123456")
    assert core.registry.owner("discord") == ""


@pytest.mark.asyncio
async def test_chatter_and_unverified_guesses_do_not_burn_pairing(home):
    """Spam, newsletters and unauthenticated senders must not close pairing."""
    core, _, _, ts = _core(home, pair_code="123456", channels=("relay", "email"))
    for i in range(hearth.MAX_PAIR_GUESSES_TOTAL * 2):
        assert not await core.on_message("email", f"spam{i}@x.test", "Buy now! 50% off")
        assert not await core.on_message("email", f"forged{i}@x.test", f"{i:06d}")
    assert core._pair_attempts == 0 and core.pair_code == "123456"
    ts["email"].verified.add("me@home.test")
    assert await core.on_message("email", "me@home.test", "123456")
    assert core.registry.owner("email") == "me@home.test"


@pytest.mark.asyncio
async def test_pairing_needs_the_channel_to_vouch_for_the_sender(home):
    core, _, _, ts = _core(home, pair_code="123456")
    assert await core.on_message("relay", "guest42", "123456")  # right code, unverified
    assert core.registry.owner("relay") == ""
    assert "could not be verified" in ts["relay"].sent[-1][1]
    ts["relay"].verified.add("guest42")
    assert await core.on_message("relay", "guest42", "123456")
    assert core.registry.owner("relay") == "guest42"


@pytest.mark.asyncio
async def test_an_owner_issued_code_expires(home, monkeypatch):
    core, _, _, ts = _bound(home, channels=("relay", "telegram", "discord"))
    await core.on_message("relay", OWNER_RELAY, "pair discord")
    code = ts["relay"].sent[-1][1].split("code for discord: ")[1][:6]
    core._pair["expires"] = 1.0                                 # long past
    assert not await core.on_message("discord", "me", code)
    assert core.registry.owner("discord") == ""


@pytest.mark.asyncio
async def test_pair_request_for_an_unattached_channel(home):
    core, _, _, ts = _bound(home)
    await core.on_message("relay", OWNER_RELAY, "pair whatsapp")
    assert "not listening on `whatsapp`" in ts["relay"].sent[-1][1]
    assert core.pair_code == ""


# ── replies and follow-ups ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reply_returns_on_the_same_channel(home):
    core, agent, _, ts = _bound(home)
    await core.on_message("telegram", OWNER_TG, "hello")
    assert ts["telegram"].sent == [(OWNER_TG, "reply to: hello")]
    assert ts["relay"].sent == []
    await core.on_message("relay", OWNER_RELAY, "and you")
    assert ts["relay"].sent == [(OWNER_RELAY, "reply to: and you")]
    assert len(ts["telegram"].sent) == 1
    assert agent.chats == ["hello", "and you"]


@pytest.mark.asyncio
async def test_followup_goes_to_the_preferred_channel_then_falls_back(home):
    core, _, store, ts = _bound(home)
    await core.on_message("telegram", OWNER_TG, "hi")           # telegram is now preferred
    assert hearth.OwnerRegistry(hearth.owner_path(home)).preferred == "telegram"
    store.add("remind", "call the dentist", 0.0)
    assert await core.fire_due(now=10.0) == 1
    assert ts["telegram"].sent[-1][1] == "Reminder: call the dentist"   # verbatim, no model turn
    assert core.agent.chats == ["hi"]
    assert len(ts["telegram"].sent) == 2 and ts["relay"].sent == []

    ts["telegram"].fail = True                                  # telegram is down
    store.add("remind", "water the plants", 0.0)
    assert await core.fire_due(now=20.0) == 1
    assert len(ts["relay"].sent) == 1 and ts["relay"].sent[0][0] == OWNER_RELAY
    assert await core.fire_due(now=30.0) == 0                   # at most once
    assert [r["status"] for r in store.rows()] == ["done", "done"]

    ts["relay"].fail = True                                     # everything is down
    store.add("remind", "lost", 0.0)
    assert await core.fire_due(now=40.0) == 1
    assert await core.fire_due(now=50.0) == 0                   # claimed, never retried
    rows = receipts.tail(50, path=home / "actions.jsonl")
    assert [r["approval"] for r in rows if r["kind"] == "dm_out"][-2:] == ["failed",
                                                                           "failed"]


@pytest.mark.asyncio
async def test_no_bound_owner_claims_nothing(home):
    core, _, store, _ = _core(home)
    store.add("remind", "x", 0.0)
    assert await core.fire_due(now=10.0) == 0
    assert store.rows()[0]["status"] == "pending"


# ── approval across channels ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_card_is_answered_only_on_the_channel_it_was_sent_to(home):
    core, agent, _, ts = _bound(home, script=lambda m, r: [("send_email", {"to": "a@x"})])
    await core.on_message("telegram", OWNER_TG, "email a")
    card = ts["telegram"].sent[-1][1]
    nonce = core.awaiting["nonce"]
    assert f"yes {nonce}" in card and core.awaiting["channel"] == "telegram"

    # The right nonce from the owner's OTHER channel is refused and counts as a miss.
    await core.on_message("relay", OWNER_RELAY, f"yes {nonce}")
    assert agent.resumes == [] and agent.sent_mail == []
    assert "Answer on telegram" in ts["relay"].sent[-1][1]
    assert core.awaiting and core.awaiting["misses"] == 1

    await core.on_message("telegram", OWNER_TG, f"yes {nonce}")
    assert agent.sent_mail == [{"to": "a@x", "body": ""}]
    assert ts["telegram"].sent[-1][1] == "reply to: email a"
    assert core.awaiting is None


@pytest.mark.asyncio
async def test_cross_channel_answers_burn_the_card(home):
    core, agent, _, ts = _bound(home, script=lambda m, r: [("send_email", {"to": "a@x"})])
    await core.on_message("telegram", OWNER_TG, "email a")
    nonce = core.awaiting["nonce"]
    for _ in range(hearth.MAX_NONCE_MISSES):
        await core.on_message("relay", OWNER_RELAY, f"yes {nonce}")
    assert core.awaiting is None
    await core.on_message("telegram", OWNER_TG, f"yes {nonce}")
    assert agent.resumes == [] and agent.sent_mail == []


@pytest.mark.asyncio
async def test_a_followup_card_lands_where_it_was_delivered(home):
    core, agent, store, ts = _bound(
        home, script=lambda m, r: [("send_email", {"to": "a@x"})])
    await core.on_message("relay", OWNER_RELAY, "hi")           # relay preferred
    ts["relay"].fail = True
    store.add("follow_up", "email a", 0.0)
    await core.fire_due(now=10.0)
    assert core.awaiting and core.awaiting["channel"] == "telegram"
    nonce = core.awaiting["nonce"]
    await core.on_message("telegram", OWNER_TG, f"yes {nonce}")
    assert agent.sent_mail == [{"to": "a@x", "body": ""}]


# ── receipts ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_calls_and_messages_are_receipted_per_channel(home):
    core, _, store, _ = _bound(
        home, script=lambda m, r: [("remind_me", {"when": "in 5 minutes", "text": "x"})])
    await core.on_message("telegram", OWNER_TG, "remind me")
    rows = receipts.tail(10, path=home / "actions.jsonl")
    assert [(r["kind"], r["name"]) for r in rows] == [("tool", "remind_me"),
                                                      ("dm_out", OWNER_TG)]
    assert rows[0]["approval"] == "auto" and rows[1]["approval"] == "sent"
    assert len(store.rows()) == 1
    assert receipts.verify(home / "actions.jsonl") == 0


# ── the adapter bridge ────────────────────────────────────────────────────────

class FakeAdapter:
    """Duck-typed adk.channels.ChannelAdapter."""

    platform = "telegram"

    def __init__(self):
        self.on_message = None
        self.sent: list[tuple[str, str]] = []
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False

    async def send(self, channel_id, message):
        self.sent.append((channel_id, message))


@pytest.mark.asyncio
async def test_channel_adapter_transport_bridges_a_platform_adapter(home):
    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    reg.bind("telegram", OWNER_TG)
    store = FollowupStore(home / "followups.json")
    adapter = FakeAdapter()
    tg = hearth.ChannelAdapterTransport(adapter)
    core = hearth.HearthCore(GateAgent(store), store, home / "actions.jsonl", tg, root=home)
    await core.start()
    assert adapter.started and tg.name == "telegram"
    # A group chat is dropped even from the owner: the group would read the reply.
    assert await adapter.on_message("telegram", "-100777", OWNER_TG, "hello") is None
    assert adapter.sent == []
    await adapter.on_message("telegram", OWNER_TG, OWNER_TG, "hello")
    assert adapter.sent == [(OWNER_TG, "reply to: hello")]
    await core.stop()
    assert not adapter.started


# ── the relay client is a transport over the core ───────────────────────────────

def test_owner_relay_client_is_a_hearth_transport(home):
    store = FollowupStore(home / "followups.json")
    rc = serve.OwnerRelayClient("https://relay.test/api/relay/v1", "tok", "home",
                                GateAgent(store), owner_nick=OWNER_RELAY, root=home,
                                store=store, receipts_file=home / "actions.jsonl",
                                verify=False)
    assert isinstance(rc, hearth.Transport)
    assert rc.core.transports == {"relay": rc}
    assert rc.core.is_owner("relay", OWNER_RELAY) and rc.owner_nick == OWNER_RELAY
    tg = FakeTransport("telegram")
    rc.core.add_transport(tg)
    with pytest.raises(ValueError):
        rc.core.add_transport(FakeTransport("telegram"))


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_cli_serve_channels_need_their_token_from_the_env(home, monkeypatch, capsys):
    from adk.home import cli as home_cli

    hc.init_home(name="hearth-test")
    monkeypatch.setenv("AITHER_RELAY_TOKEN", "t")
    for names in hearth.CHANNEL_ENV.values():
        for n in names:
            monkeypatch.delenv(n, raising=False)
    hearth.OwnerRegistry(hearth.owner_path(home)).bind("telegram", OWNER_TG)
    assert home_cli.main(["serve", "--channels", "telegram"]) == home_cli.EXIT_SETUP
    assert "HEARTH_TELEGRAM_TOKEN" in capsys.readouterr().err
    assert home_cli.main(["serve", "--channels", "carrierpigeon"]) == home_cli.EXIT_SETUP
    assert "unknown channel" in capsys.readouterr().err


# ── one turn at a time ───────────────────────────────────────────────────────

class SlowAgent(GateAgent):
    """A GateAgent whose turns take a while and record the concurrency they saw."""

    def __init__(self, store, script):
        super().__init__(store, script)
        self.store = store
        self.inflight = self.peak = 0
        self.flags: list[tuple[str, bool, bool]] = []

    async def chat(self, message, session_id=None):
        import asyncio

        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            before = self.store.unattended
            await asyncio.sleep(0.02)
            after = self.store.unattended
            self.flags.append((message, before, after))
            return await super().chat(message, session_id=session_id)
        finally:
            self.inflight -= 1


@pytest.mark.asyncio
async def test_turns_never_overlap_across_channels_and_the_tick(home):
    """A slow resume, a due follow-up and two owner messages on different channels
    run one at a time, and the follow-up's turn stays unattended throughout."""
    import asyncio

    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    reg.bind("relay", OWNER_RELAY)
    reg.bind("telegram", OWNER_TG)
    store = FollowupStore(home / "followups.json")
    agent = SlowAgent(store, lambda m, r: [("send_email", {"to": "a@x"})]
                      if m == "email a" else [])
    ts = {c: FakeTransport(c, verified=(OWNER_RELAY, OWNER_TG)) for c in ("relay", "telegram")}
    core = hearth.HearthCore(agent, store, home / "actions.jsonl", *ts.values(), root=home)
    await core.on_message("telegram", OWNER_TG, "email a")
    nonce = core.awaiting["nonce"]
    store.add("follow_up", "check the oven", 0.0)
    agent.flags.clear()
    agent.peak = 0
    await asyncio.gather(
        core.on_message("telegram", OWNER_TG, f"yes {nonce}"),   # slow resume
        core.fire_due(now=10.0),                                  # unattended turn
        core.on_message("relay", OWNER_RELAY, "hello"),
        core.on_message("telegram", OWNER_TG, "and you"),
    )
    assert agent.peak == 1
    followup = [f for f in agent.flags if f[0].startswith("[follow-up due")]
    assert followup and all(before and after for _, before, after in followup)
    attended = [f for f in agent.flags if not f[0].startswith("[follow-up due")]
    assert attended and not any(before or after for _, before, after in attended)
    assert store.unattended is False
