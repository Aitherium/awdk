"""A device agent joins ITS company's room, and the relay -- not the device -- names it.

`curl ... install-bonsai.sh | sh -s -- --with-adk` puts an agent on an employee's
machine, signed in to that employee's company. Before this, `adk relay join`
defaulted to `#agents` -- the platform's channel -- and `adk up` joined nothing, so
the agent never showed up where the employee's colleagues are.

The room is `#<workspace slug>-room` and the slug is the relay's record, so the
device ASKS (`GET /agent/home-room`, answered from the bearer). These tests pin:
the answer is used verbatim, nothing is guessed when there is no answer, a login
with no company tenant behaves exactly as before, and an explicit --channel wins.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk import cli, relay_client  # noqa: E402

BASE = "https://relay.test/api/relay/v1"
GARG = {"username": "cy", "tenant_id": "tnt_garg", "api_key": "saved-key"}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── relay_client.home_room: the channel is the relay's answer ────────────────

def test_home_room_returns_the_room_the_relay_names_and_sends_only_the_bearer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["query"] = request.url.query
        return httpx.Response(200, json={"channel": "#acme-corp-room", "workspace": "acme-corp",
                                         "tenant_id": "tnt_garg", "reason": ""})

    assert relay_client.home_room(BASE, "tok", client=_client(handler)) == ("#acme-corp-room", "")
    assert seen["url"] == f"{BASE}/agent/home-room"
    assert seen["auth"] == "Bearer tok"
    # no tenant, workspace or slug travels in the request: the bearer is the identity
    assert seen["query"] == b""


@pytest.mark.parametrize("response,needle", [
    (httpx.Response(200, json={"channel": "", "reason": "no company room exists for tenant tnt_garg yet"}),
     "no company room exists"),
    (httpx.Response(200, json={"channel": ""}), "named no company room"),
    (httpx.Response(200, json={"channel": "garg-room"}), "named no company room"),   # not a channel
    (httpx.Response(200, json=["#garg-room"]), "unreadable"),
    (httpx.Response(200, text="<html>"), "unreadable"),
    (httpx.Response(404, json={"detail": "Not Found"}), "does not serve company rooms"),
    (httpx.Response(401, json={"detail": "nope"}), "adk login"),
    (httpx.Response(502, text="bad gateway"), "502"),
])
def test_home_room_never_invents_a_channel(response, needle):
    channel, reason = relay_client.home_room(BASE, "tok", client=_client(lambda r: response))
    assert channel == ""
    assert needle in reason


def test_home_room_survives_an_unreachable_relay():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    channel, reason = relay_client.home_room(BASE, "tok", client=_client(handler))
    assert channel == "" and "ConnectError" in reason


# ── which channel `adk relay join` enters ────────────────────────────────────

def _lookup(answer):
    calls = []

    def lookup(base, token):
        calls.append((base, token))
        return answer

    lookup.calls = calls
    return lookup


def test_company_login_joins_the_room_the_relay_names():
    lookup = _lookup(("#acme-corp-room", ""))
    assert cli._relay_join_channel("", GARG, BASE, "tok", lookup) == ("#acme-corp-room", "")
    assert lookup.calls == [(BASE, "tok")]


def test_the_room_is_never_derived_from_the_tenant_id():
    """`tnt_garg` looks like it means `#garg-room`. It is the relay's call: when the
    relay names no room the device falls back and says why -- it does not guess."""
    lookup = _lookup(("", "no company room exists for tenant tnt_garg yet"))
    channel, note = cli._relay_join_channel("", GARG, BASE, "tok", lookup)
    assert channel == "#agents"
    assert "garg" not in channel
    assert "no company room exists" in note


@pytest.mark.parametrize("saved", [
    {},                                             # not signed in to a company
    {"username": "sam"},                            # a personal login
    {"username": "sam", "tenant_id": ""},
    {"username": "david", "tenant_id": "platform"},  # the platform's own agents
    {"username": "david", "tenant_id": " Platform "},
])
def test_login_without_a_company_tenant_is_unchanged(saved):
    lookup = _lookup(("#should-never-be-asked", ""))
    assert cli._relay_join_channel("", saved, BASE, "tok", lookup) == ("#agents", "")
    assert lookup.calls == []                       # the relay is not even asked


def test_explicit_channel_always_wins():
    lookup = _lookup(("#acme-corp-room", ""))
    assert cli._relay_join_channel("#agents", GARG, BASE, "tok", lookup) == ("#agents", "")
    assert cli._relay_join_channel("ops", GARG, BASE, "tok", lookup) == ("#ops", "")
    assert lookup.calls == []


# ── `adk relay join` end to end (the relay and the agent are fakes) ──────────

class _FakeRelayClient:
    made: list = []

    def __init__(self, **kw):
        self.kw = kw
        _FakeRelayClient.made.append(self)

    async def run(self):
        return None


def _join_args(**kw):
    base = dict(relay_command="join", nick="", url=BASE, local=False, channel="", token="tok")
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def relay_join(monkeypatch):
    import adk

    _FakeRelayClient.made = []
    monkeypatch.setattr(relay_client, "RelayClient", _FakeRelayClient)
    monkeypatch.setattr(adk, "AitherAgent", lambda nick, system_prompt="": object())
    monkeypatch.setattr(cli, "_session_bearer_token", lambda: "")

    def _configure(saved, answer=("", "unset")):
        monkeypatch.setattr(cli, "load_saved_config", lambda: dict(saved))
        lookup = _lookup(answer)
        monkeypatch.setattr(relay_client, "home_room", lambda base, token: lookup(base, token))
        return lookup

    return _configure


def test_relay_join_with_no_channel_joins_the_company_room(relay_join, capsys):
    lookup = relay_join(GARG, ("#acme-corp-room", ""))
    assert cli.cmd_relay(_join_args()) == 0
    assert _FakeRelayClient.made[0].kw["channel"] == "#acme-corp-room"
    assert lookup.calls == [(BASE, "tok")]
    assert "#acme-corp-room" in capsys.readouterr().out


def test_relay_join_with_no_tenant_still_joins_agents(relay_join):
    lookup = relay_join({"username": "sam"}, ("#acme-corp-room", ""))
    assert cli.cmd_relay(_join_args()) == 0
    assert _FakeRelayClient.made[0].kw["channel"] == "#agents"
    assert lookup.calls == []


def test_relay_join_says_why_when_the_company_room_is_not_used(relay_join, capsys):
    relay_join(GARG, ("", "no company room exists for tenant tnt_garg yet"))
    assert cli.cmd_relay(_join_args()) == 0
    assert _FakeRelayClient.made[0].kw["channel"] == "#agents"
    out = capsys.readouterr().out
    assert "no company room exists for tenant tnt_garg yet" in out


def test_relay_join_parser_default_is_no_channel():
    """The argparse default used to be '#agents', which would mask the company room."""
    import adk.cli as c

    monkey = {}

    def _capture(args):
        monkey["channel"] = args.channel
        return 0

    orig = c.cmd_relay
    c.cmd_relay = _capture
    try:
        argv = sys.argv
        sys.argv = ["adk", "relay", "join"]
        try:
            c.main()
        except SystemExit:
            pass
        finally:
            sys.argv = argv
    finally:
        c.cmd_relay = orig
    assert monkey.get("channel") == ""


# ── `adk up` ─────────────────────────────────────────────────────────────────

class _FakeDaemon:
    LOG_DIR = Path("logs")

    def __init__(self):
        self.spawned: list = []

    def spawn_detached(self, argv, log_path, env=None):
        self.spawned.append((list(argv), log_path))
        return 5150


def test_up_joins_the_company_room_and_names_it():
    daemon = _FakeDaemon()
    joined = []

    def join(base, token, nick, channel):
        joined.append((base, token, nick, channel))
        return True, "cy+aither"            # the nick the relay actually granted

    st = cli._up_join_company_room(daemon, GARG, "aither", "tok", BASE,
                                   lookup=_lookup(("#acme-corp-room", "")), join=join)
    assert st["room"] == "#acme-corp-room" and st["relay_pid"] == 5150
    assert joined == [(BASE, "tok", "aither", "#acme-corp-room")]
    argv, log = daemon.spawned[0]
    assert argv[argv.index("--channel") + 1] == "#acme-corp-room"
    assert argv[argv.index("--nick") + 1] == "cy+aither"
    assert log.name == "relay.log"
    line = cli._room_line(st)
    assert "#acme-corp-room" in line and "cy+aither" in line and "--no-room" in line


def test_up_without_a_company_tenant_joins_nothing_and_says_nothing():
    daemon = _FakeDaemon()
    lookup = _lookup(("#acme-corp-room", ""))
    st = cli._up_join_company_room(daemon, {"username": "sam"}, "aither", "tok", BASE,
                                   lookup=lookup, join=lambda *a: (True, "x"))
    assert st == {"room": None, "room_note": "", "relay_pid": None}
    assert lookup.calls == [] and daemon.spawned == []
    assert cli._room_line(st) == ""


def test_up_says_why_when_the_relay_names_no_room():
    daemon = _FakeDaemon()
    st = cli._up_join_company_room(
        daemon, GARG, "aither", "tok", BASE,
        lookup=_lookup(("", "no company room exists for tenant tnt_garg yet")),
        join=lambda *a: (True, "x"))
    assert st["room"] is None and daemon.spawned == []
    assert "no company room exists for tenant tnt_garg yet" in cli._room_line(st)


def test_up_does_not_claim_a_room_the_relay_refused():
    """A detached child refused in a log nobody reads is not 'joined'."""
    daemon = _FakeDaemon()
    st = cli._up_join_company_room(daemon, GARG, "aither", "tok", BASE,
                                   lookup=_lookup(("#acme-corp-room", "")),
                                   join=lambda *a: (False, "aither"))
    assert st["room"] is None and st["relay_pid"] is None and daemon.spawned == []
    line = cli._room_line(st)
    assert "not joined" in line and "#acme-corp-room" in line


def test_up_survives_a_join_that_raises():
    def join(*a):
        raise httpx.ConnectTimeout("")

    st = cli._up_join_company_room(_FakeDaemon(), GARG, "aither", "tok", BASE,
                                   lookup=_lookup(("#acme-corp-room", "")), join=join)
    assert st["room"] is None and "ConnectTimeout" in st["room_note"]


def test_up_without_a_credential_says_so():
    st = cli._up_join_company_room(_FakeDaemon(), GARG, "aither", "", BASE,
                                   lookup=_lookup(("#acme-corp-room", "")))
    assert st["room"] is None and "adk login" in st["room_note"]


def test_no_room_survives_the_logon_rerun():
    assert "--no-room" in cli._autostart_up_argv("aither", 8080, "", False, no_room=True)
    assert "--no-room" not in cli._autostart_up_argv("aither", 8080, "", False)


def test_up_parser_has_the_opt_out():
    captured = {}

    def _capture(args):
        captured["no_room"] = args.no_room
        return 0

    orig, argv = cli.cmd_up, sys.argv
    cli.cmd_up = _capture
    sys.argv = ["adk", "up", "--no-room", "--dry-run"]
    try:
        try:
            cli.main()
        except SystemExit:
            pass
    finally:
        cli.cmd_up, sys.argv = orig, argv
    assert captured.get("no_room") is True
