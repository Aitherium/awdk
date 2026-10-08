"""``adk connectors`` -- the CLI door to the connect-once plane (connector slice S5).

Argument parsing through the real ``adk`` parser, the deep link ``connect`` builds,
and each verb against a fake transport: nothing here reaches a network, and no
verb ever prints a token.
"""

from __future__ import annotations

import argparse
import json

import pytest

from adk import connectors as cn
from adk import connectors_cli as cli

BEARER = "login-bearer-123"


@pytest.fixture
def signed_in(monkeypatch):
    for name in ("AITHER_API_KEY", "AITHER_IDENTITY_BEARER", "AITHER_SESSION_BEARER",
                 "AITHER_PORTAL_API_URL", "AITHER_CONNECT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cn, "_saved_bearer", lambda: BEARER)
    return monkeypatch


def _parser():
    ap = argparse.ArgumentParser(prog="adk")
    cli.register_parser(ap.add_subparsers(dest="command"))
    return ap


class Fake:
    def __init__(self, status=200, data=None):
        self.status, self.data = status, data
        self.calls: list = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.status, self.data


ROWS = {"workspace_id": "w1", "can_manage": True, "connectors": [
    {"connector": "github", "status": "connected", "account": "octo", "available": True},
    {"connector": "gmail", "status": "not_connected", "account": "", "available": False},
]}


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parser_registers_every_verb_and_validates_ids():
    ap = _parser()
    a = ap.parse_args(["connectors", "grant", "github", "--agent", "demiurge", "--cap", "git"])
    assert (a.connectors_command, a.connector, a.agent, a.cap, a.revoke) == (
        "grant", "github", "demiurge", "git", False)
    assert ap.parse_args(["connectors", "connect", "GitHub"]).connector == "github"
    with pytest.raises(SystemExit):
        ap.parse_args(["connectors", "connect", "../admin"])
    with pytest.raises(SystemExit):
        ap.parse_args(["connectors", "grant", "github", "--agent", "x", "--cap", "admin"])
    with pytest.raises(SystemExit):
        ap.parse_args(["connectors", "grant", "github", "--agent", "a b", "--cap", "read"])


def test_the_real_adk_parser_has_the_door():
    import sys

    from adk import cli as adk_cli

    argv = sys.argv
    try:
        sys.argv = ["adk", "connectors", "connect", "github", "--no-open"]
        with pytest.raises(SystemExit) as done:
            adk_cli.main()
    finally:
        sys.argv = argv
    assert done.value.code == 0


# ── connect: the deep link ───────────────────────────────────────────────────

def test_connect_url_is_the_customer_connections_window(signed_in):
    assert cli.connect_url() == "https://aitherium.com/?app=connections"
    assert cli.connect_url("github") == "https://aitherium.com/?app=connections&connect=github"
    signed_in.setenv("AITHER_CONNECT_URL", "https://example.test/conn")
    assert cli.connect_url("gmail") == "https://example.test/conn?connect=gmail"


def test_connect_opens_the_link_and_never_asks_the_server(signed_in):
    opened, lines = [], []
    args = _parser().parse_args(["connectors", "connect", "github"])
    assert cli.cmd_connect(args, opener=opened.append, out=lines.append) == 0
    assert opened == ["https://aitherium.com/?app=connections&connect=github"]
    args = _parser().parse_args(["connectors", "connect", "github", "--no-open"])
    opened.clear()
    assert cli.cmd_connect(args, opener=opened.append, out=lines.append) == 0
    assert opened == []


# ── list / status ────────────────────────────────────────────────────────────

def test_list_reads_me_connectors_with_the_saved_login(signed_in):
    fake, lines = Fake(200, ROWS), []
    args = _parser().parse_args(["connectors", "list"])
    assert cli.cmd_list(args, transport=fake, out=lines.append) == 0
    method, url, headers, _ = fake.calls[0]
    assert (method, url) == ("GET", "https://api.aitherium.com/api/me/connectors")
    assert headers["Authorization"] == f"Bearer {BEARER}"
    text = "\n".join(lines)
    assert "github" in text and "octo" in text and "not set up yet" in text


def test_status_exit_codes(signed_in):
    lines: list = []
    p = _parser()
    assert cli.cmd_status(p.parse_args(["connectors", "status", "github"]),
                          transport=Fake(200, ROWS), out=lines.append) == 0
    assert cli.cmd_status(p.parse_args(["connectors", "status", "gmail"]),
                          transport=Fake(200, ROWS), out=lines.append) == 1
    assert "adk connectors connect gmail" in lines[-1]
    assert cli.cmd_status(p.parse_args(["connectors", "status", "slack"]),
                          transport=Fake(200, ROWS), out=lines.append) == 1
    assert cli.cmd_status(p.parse_args(["connectors", "status", "github"]),
                          transport=Fake(0, None), out=lines.append) == 2


def test_not_signed_in_asks_nothing(signed_in):
    signed_in.setattr(cn, "_saved_bearer", lambda: "")
    fake, lines = Fake(200, ROWS), []
    assert cli.cmd_list(_parser().parse_args(["connectors", "list"]),
                        transport=fake, out=lines.append) == 2
    assert fake.calls == [] and "adk login" in lines[0]


# ── grant ────────────────────────────────────────────────────────────────────

def test_grant_puts_the_grant(signed_in):
    fake, lines = Fake(200, {"ok": True}), []
    args = _parser().parse_args(["connectors", "grant", "github", "--agent", "demiurge",
                                 "--cap", "git"])
    assert cli.cmd_grant(args, transport=fake, out=lines.append) == 0
    method, url, _, body = fake.calls[0]
    assert (method, url) == ("PUT", "https://api.aitherium.com/api/me/connectors/github/grants")
    assert body == {"agent": "demiurge", "capability": "git", "granted": True}
    assert "granted" in lines[-1]


def test_grant_on_a_server_without_grants_says_so_and_claims_nothing(signed_in):
    lines: list = []
    args = _parser().parse_args(["connectors", "grant", "github", "--agent", "iris",
                                 "--cap", "read", "--revoke"])
    assert cli.cmd_grant(args, transport=Fake(404, {"detail": "Not Found"}),
                         out=lines.append) == 1
    assert "not available" in lines[0] and "Nothing was changed" in lines[0]
    assert not any("revoked" in line for line in lines)


def test_no_verb_prints_no_token(signed_in):
    rows = json.loads(json.dumps(ROWS))
    rows["connectors"][0]["token"] = "gho_SHOULD-NEVER-PRINT"
    lines: list = []
    for argv in (["connectors", "list"], ["connectors", "list", "--json"],
                 ["connectors", "status", "github", "--json"]):
        cli.cmd_status(_parser().parse_args(argv), transport=Fake(200, rows),
                       out=lines.append)
    assert lines and "gho_SHOULD-NEVER-PRINT" not in "\n".join(lines)


# ── no verb persists a token outside the session ────────────────────────────

def test_there_is_no_gh_auth_verb():
    """``gh auth login`` + ``gh auth setup-git`` would write the connector token
    into gh's store and the global ~/.gitconfig, where it outlives the session,
    the grant and any revocation, and reaches every later child."""
    with pytest.raises(SystemExit):
        _parser().parse_args(["connectors", "gh-auth"])
    assert not hasattr(cli, "cmd_gh_auth")
    assert cli.cmd_connectors(argparse.Namespace(connectors_command="gh-auth")) == 2


def test_login_points_at_the_connect_step(monkeypatch, capsys, tmp_path):
    """A device-flow sign-in ends with the next step: connect an account."""
    from adk import cli as adk_cli

    monkeypatch.setattr(adk_cli, "_device_flow_login",
                        lambda url: {"access_token": "tok", "user": {"username": "u"}})
    monkeypatch.setattr(adk_cli, "save_saved_config", lambda update: None)
    monkeypatch.setattr(adk_cli, "_save_account_license", lambda result: "")
    monkeypatch.setattr(adk_cli, "_persist_workspace_endpoints", lambda url: None)
    monkeypatch.setattr(adk_cli, "_persist_shell_auth", lambda *a, **k: None)
    import adk.account_license as al

    monkeypatch.setattr(al, "sync_account_license", lambda *a, **k: {})
    args = argparse.Namespace(portal_url="https://idp.example", api_key=None, email=None,
                              no_sync=True)
    assert adk_cli.cmd_login(args) == 0
    out = capsys.readouterr().out
    assert adk_cli.LOGIN_CONNECT_HINT in out
    assert "adk connectors connect github" in adk_cli.LOGIN_CONNECT_HINT
