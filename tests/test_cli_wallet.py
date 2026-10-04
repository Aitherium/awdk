"""`adk wallet` / `adk balance` against a REAL local HTTP server speaking /v1/wallet.

The command used to call an ACTA host that answers 404 for everyone outside the
fleet ("Account not found"). It now reads the gateway wallet; this pins that it
sends the saved key, prints the wallet and the buy links, pages the ledger, and
reports a rejected key as a login problem.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from adk import cli

WALLET = {"user_id": "u1", "balance": 2597, "plan": "free", "bought": 2000, "earned": 40,
          "granted": 1000, "spent": 450, "other": 7, "credits_per_usd": 1000,
          "buy": {"card": "https://shop.example/credits", "x402_quote": "https://gw.example/q"}}
LEDGER = {"events": [{"at": "2026-10-04T00:00:02", "type": "x402_settle", "delta": 2000}],
          "next": None}


class _H(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *_a):
        return

    def do_GET(self):  # noqa: N802 — http.server API
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer good-key":
            self.send_response(401)
            self.end_headers()
            return
        body = LEDGER if self.path.startswith("/v1/wallet/ledger") else WALLET
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


@pytest.fixture()
def gateway():
    _H.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _run(monkeypatch, gateway, key, **kw):
    monkeypatch.setattr(cli, "load_saved_config", lambda: {"api_key": key})
    args = SimpleNamespace(gateway=gateway, ledger=kw.get("ledger", 0), json=kw.get("json", False))
    return cli.cmd_balance(args)


def test_prints_wallet_and_buy_links(monkeypatch, gateway, capsys):
    assert _run(monkeypatch, gateway, "good-key", ledger=5) == 0
    out = capsys.readouterr().out
    assert "2,597 tokens" in out and "Earned:" in out and "x402_settle" in out
    assert "https://shop.example/credits" in out and "https://gw.example/q" in out
    assert ("/v1/wallet", "Bearer good-key") in _H.seen
    assert any(p.startswith("/v1/wallet/ledger?limit=5") for p, _ in _H.seen)


def test_json_output(monkeypatch, gateway, capsys):
    assert _run(monkeypatch, gateway, "good-key", ledger=1, json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["wallet"]["balance"] == 2597 and data["ledger"]["events"][0]["delta"] == 2000


def test_rejected_key_is_a_login_problem(monkeypatch, gateway, capsys):
    assert _run(monkeypatch, gateway, "bad-key") == 1
    assert "adk login" in capsys.readouterr().out


def test_not_logged_in(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_saved_config", lambda: {})
    assert cli.cmd_balance(SimpleNamespace(gateway=None, ledger=0, json=False)) == 1
