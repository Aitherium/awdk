"""``adk home serve --channels`` and ``adk home channels``: which channels run, and
what the owner sees about them.

Transports are faked at the CLI's seams (``_build_transport``, the relay client
and the serve agent), so nothing here opens a socket.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from adk import approval, receipts  # noqa: E402
from adk.home import cli as home_cli  # noqa: E402
from adk.home import config as hc  # noqa: E402
from adk.home import hearth, serve  # noqa: E402

EMAIL_ENV = ("HEARTH_IMAP_HOST", "HEARTH_IMAP_USER", "HEARTH_MAIL_PASSWORD",
             "HEARTH_MAIL_AUTHSERV_ID")
ALL_ENV = sorted({n for spec in home_cli.CHANNEL_SPECS.values() for g in spec.env for n in g})


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "agent-home"
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    monkeypatch.delenv("AITHER_RECEIPTS_PATH", raising=False)
    monkeypatch.delenv(receipts.KEY_ENV, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: root)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    monkeypatch.setattr(approval, "_STORE", approval.ApprovalStore(tmp_path / "paused.json"))
    for n in ALL_ENV:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr("adk.config.load_saved_config", lambda *a, **k: {})
    monkeypatch.setattr(home_cli, "_keychain_has", lambda cls, fn: False)
    return root


class FakeTransport:
    def __init__(self, name, fail_with=None, gate=None, peer=None):
        self.name = name
        self.fail_with = fail_with
        self.gate, self.peer = gate, peer
        self.started = self.stopped = False

    async def start(self, core):
        if self.gate is not None:           # concurrency proof: wait for the peer
            self.peer.set()
            await asyncio.wait_for(self.gate.wait(), 2)
        if self.fail_with is not None:
            raise self.fail_with
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send(self, user_id, text):
        return True


# ── selection ────────────────────────────────────────────────────────────────

def test_default_is_relay_when_token_plus_every_configured_channel(home, monkeypatch):
    assert home_cli._select_channels("") == ([], False)
    monkeypatch.setenv("AITHER_RELAY_TOKEN", "relay-tok")
    assert home_cli._select_channels("") == (["relay"], False)
    monkeypatch.setenv("HEARTH_TELEGRAM_TOKEN", "123:abc")
    for n in EMAIL_ENV:
        monkeypatch.setenv(n, "x")
    assert home_cli._select_channels("") == (["relay", "telegram", "email"], False)
    monkeypatch.delenv("AITHER_RELAY_TOKEN")
    assert home_cli._select_channels("") == (["telegram", "email"], False)


@pytest.mark.parametrize("alias", ["TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN"])
def test_a_generic_bot_token_never_joins_the_default_selection(home, monkeypatch, alias):
    """Plain `adk home serve` must not start polling some other bot on the box."""
    monkeypatch.setenv("AITHER_RELAY_TOKEN", "relay-tok")
    monkeypatch.setenv(alias, "123:abc")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-x")
    assert home_cli._select_channels("") == (["relay"], False)
    # ...but it still satisfies an explicit request.
    channel = alias.split("_")[0].lower()
    assert home_cli._channel_status(channel)["configured"] is True


def test_a_default_channel_that_fails_to_build_is_skipped_not_fatal(home, monkeypatch, capsys):
    monkeypatch.setenv("AITHER_RELAY_TOKEN", "relay-tok")
    monkeypatch.setenv("HEARTH_TELEGRAM_TOKEN", "123456:SECRETSECRET")

    class LicenseError(Exception):
        pass

    def boom(channel, cls):
        raise LicenseError("Starter tier required for telegram")

    monkeypatch.setattr(home_cli, "_build_transport", boom)
    names, built, rc = home_cli._prepare_channels(SimpleNamespace(channels=""))
    assert (names, built, rc) == (["relay"], {}, home_cli.EXIT_OK)
    err = capsys.readouterr().err
    assert "telegram" in err and "skipped" in err and "SECRETSECRET" not in err
    # Asked for by name, the same failure is still an error.
    names, built, rc = home_cli._prepare_channels(SimpleNamespace(channels="relay,telegram"))
    assert rc == home_cli.EXIT_LICENSE


def test_explicit_list_is_deduped_and_unknown_is_refused(home):
    assert home_cli._select_channels(" Telegram,relay,telegram ,") == (
        ["telegram", "relay"], True)
    with pytest.raises(hc.HomeError, match="unknown channel 'fax'"):
        home_cli._select_channels("relay,fax")


def test_keychain_password_satisfies_email(home, monkeypatch):
    monkeypatch.setenv("HEARTH_IMAP_HOST", "imap.test")
    monkeypatch.setenv("HEARTH_IMAP_USER", "me@test")
    monkeypatch.setenv("HEARTH_MAIL_AUTHSERV_ID", "mx.test")
    assert home_cli._channel_status("email")["missing_env"] == ["HEARTH_MAIL_PASSWORD"]
    monkeypatch.setattr(home_cli, "_keychain_has", lambda cls, fn: True)
    assert home_cli._channel_status("email")["configured"] is True


# ── serve: errors before anything starts ─────────────────────────────────────

@pytest.mark.parametrize("channel,var", [
    ("telegram", "HEARTH_TELEGRAM_TOKEN"),
    ("email", "HEARTH_IMAP_HOST"),
    ("whatsapp", "HEARTH_WA_TOKEN"),
    ("sms", "HEARTH_TWILIO_AUTH_TOKEN"),
    ("relay", "AITHER_RELAY_TOKEN"),
])
def test_explicit_channel_without_credentials_names_the_env_var(home, capsys, channel, var):
    hc.init_home(name="hearth-test")
    assert home_cli.main(["serve", "--channels", channel]) == home_cli.EXIT_SETUP
    err = capsys.readouterr().err
    assert var in err and channel in err


def test_no_channel_configured_at_all_is_a_setup_error(home, capsys):
    hc.init_home(name="hearth-test")
    # Without --no-local the loopback channel alone would serve.
    assert home_cli.main(["serve", "--no-local"]) == home_cli.EXIT_SETUP
    assert "AITHER_RELAY_TOKEN" in capsys.readouterr().err


def test_explicit_channel_whose_module_is_missing_is_unavailable(home, monkeypatch, capsys):
    hc.init_home(name="hearth-test")
    specs = dict(home_cli.CHANNEL_SPECS)
    specs["whatsapp"] = specs["whatsapp"]._replace(module="no_such_transport_module")
    monkeypatch.setattr(home_cli, "CHANNEL_SPECS", specs)
    assert home_cli.main(["serve", "--channels", "whatsapp"]) == home_cli.EXIT_SETUP
    assert "unavailable" in capsys.readouterr().err
    # ...and in the default selection it is skipped, not fatal.
    monkeypatch.setenv("AITHER_RELAY_TOKEN", "relay-tok")
    for n in home_cli._WA:
        monkeypatch.setenv(n, "x")
    names, built, rc = home_cli._prepare_channels(SimpleNamespace(channels=""))
    assert (names, rc) == (["relay"], home_cli.EXIT_OK)
    assert "whatsapp" in capsys.readouterr().err


def test_attach_error_is_scrubbed_of_the_token(home, monkeypatch, capsys):
    hc.init_home(name="hearth-test")
    monkeypatch.setenv("HEARTH_TELEGRAM_TOKEN", "123456:SECRETSECRET")

    def boom(channel, cls):
        raise RuntimeError("GET https://api.telegram.org/bot123456:SECRETSECRET/getMe failed")

    monkeypatch.setattr(home_cli, "_build_transport", boom)
    assert home_cli.main(["serve", "--channels", "telegram"]) == home_cli.EXIT_SETUP
    err = capsys.readouterr().err
    assert "SECRETSECRET" not in err and "cannot attach" in err


# ── serve: one core, concurrent start, pairing code once ─────────────────────

def test_serve_runs_every_channel_on_one_core(home, monkeypatch, capsys):
    hc.init_home(name="hearth-test")
    monkeypatch.setenv("AITHER_RELAY_TOKEN", "relay-tok")
    monkeypatch.setenv("HEARTH_TELEGRAM_TOKEN", "tg-tok")
    for n in EMAIL_ENV:
        monkeypatch.setenv(n, "x")
    monkeypatch.setattr(serve, "build_serve_agent", lambda *a, **k: SimpleNamespace(
        name="hearth-test", _tools=SimpleNamespace(list_tools=lambda: [])))
    monkeypatch.setattr(serve, "tool_names", lambda agent: ["remind_me"])
    monkeypatch.setattr(hearth.HearthCore, "_wrap_execute", lambda self: None)
    monkeypatch.setattr(home_cli, "_build_transport",
                        lambda channel, cls: FakeTransport(channel))
    seen = {}

    async def fake_run(core, tick):
        seen["core"] = core
        return home_cli.EXIT_OK

    monkeypatch.setattr(home_cli, "_run_hearth", fake_run)
    assert home_cli.main(["serve", "--pair"]) == home_cli.EXIT_OK
    core = seen["core"]
    # The loopback local channel joins the default selection last.
    assert list(core.transports) == ["relay", "telegram", "email", "local"]
    assert isinstance(core.transports["relay"], serve.OwnerRelayClient)
    assert core.transports["relay"].core is core
    out = capsys.readouterr().out
    assert out.count(core.pair_code) == 1 and "PAIRING CODE" in out
    assert "channels: relay, telegram, email, local" in out


def test_serve_without_relay_builds_a_bare_core(home, monkeypatch, capsys):
    hc.init_home(name="hearth-test")
    monkeypatch.setenv("AITHER_RELAY_TOKEN", "relay-tok")
    monkeypatch.setenv("HEARTH_TELEGRAM_TOKEN", "tg-tok")
    hearth.OwnerRegistry(hearth.owner_path(home)).bind("telegram", "123456789")
    monkeypatch.setattr(serve, "build_serve_agent", lambda *a, **k: SimpleNamespace(
        name="hearth-test"))
    monkeypatch.setattr(serve, "tool_names", lambda agent: [])
    monkeypatch.setattr(hearth.HearthCore, "_wrap_execute", lambda self: None)
    monkeypatch.setattr(home_cli, "_build_transport",
                        lambda channel, cls: FakeTransport(channel))
    seen = {}

    async def fake_run(core, tick):
        seen["core"] = core
        return home_cli.EXIT_OK

    monkeypatch.setattr(home_cli, "_run_hearth", fake_run)
    assert home_cli.main(["serve", "--channels", "telegram"]) == home_cli.EXIT_OK
    assert list(seen["core"].transports) == ["telegram"]
    out = capsys.readouterr().out
    assert "123456789" not in out and "12***89" in out


def test_run_hearth_starts_concurrently_and_drops_a_failed_channel(monkeypatch, capsys):
    monkeypatch.setenv("HEARTH_DISCORD_TOKEN", "discord-SECRET-value")
    a_ready, b_ready = asyncio.Event(), asyncio.Event()
    # Each start waits for the OTHER to have begun: a sequential start would time out.
    a = FakeTransport("telegram", gate=b_ready, peer=a_ready)
    b = FakeTransport("email", gate=a_ready, peer=b_ready)
    c = FakeTransport("discord", fail_with=RuntimeError("login refused for "
                                                        "discord-SECRET-value"))
    ticks = []

    async def fire_due():
        ticks.append(1)
        core._running = False

    async def stop():
        for t in core.transports.values():
            await t.stop()

    core = SimpleNamespace(transports={"telegram": a, "email": b, "discord": c},
                           fire_due=fire_due, stop=stop, _running=False)
    assert asyncio.run(home_cli._run_hearth(core, 0)) == home_cli.EXIT_OK
    assert a.started and b.started and ticks == [1]
    assert list(core.transports) == ["telegram", "email"] and c.stopped
    cap = capsys.readouterr()
    assert "telegram  up" in cap.out and "email     up" in cap.out
    assert "discord   FAILED" in cap.err and "SECRET" not in cap.err


def test_run_hearth_with_nothing_started_fails():
    core = SimpleNamespace(transports={"x": FakeTransport("x", fail_with=OSError("down"))},
                           fire_due=None, stop=None, _running=False)
    assert asyncio.run(home_cli._run_hearth(core, 0)) == home_cli.EXIT_FAIL


# ── adk home channels ────────────────────────────────────────────────────────

def test_channels_lists_every_channel_with_masked_owner(home, monkeypatch, capsys):
    monkeypatch.setenv("AITHER_RELAY_TOKEN", "relay-tok")
    reg = hearth.OwnerRegistry(hearth.owner_path(home))
    reg.bind("relay", "david-the-owner")
    reg.bind("telegram", "123456789")
    reg.set_preferred("telegram")
    assert home_cli.main(["channels"]) == home_cli.EXIT_OK
    out = capsys.readouterr().out
    for ch in home_cli.CHANNELS:
        assert ch in out
    assert "david-the-owner" not in out and "123456789" not in out
    assert "da***er" in out and "12***89" in out
    assert home_cli.main(["channels", "--json"]) == home_cli.EXIT_OK
    rows = {r["channel"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["relay"]["configured"] and rows["relay"]["owner"] == "da***er"
    assert rows["telegram"]["preferred"] and not rows["relay"]["preferred"]
    assert rows["telegram"]["missing_env"] == ["HEARTH_TELEGRAM_TOKEN"]
    assert "123456789" not in json.dumps(rows)


def test_mask_id_never_shows_a_short_id():
    assert home_cli.mask_id("") == "" and home_cli.mask_id("12345") == "***"
    assert home_cli.mask_id("123456789") == "12***89"
