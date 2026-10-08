"""adk crypto: reads/sets the platform receiving wallet through the gateway tools."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from adk.commands import crypto

ADDR = "11111111111111111111111111111111"  # the all-zero system address, never a payee


def _args(**kw):
    base = dict(gateway="http://127.0.0.1:8182/mcp", json=False, crypto_action="platform-get",
                pay_to="", facilitator_url="", network="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_get_calls_the_settings_tool(capsys):
    data = {"enabled": True, "pay_to": {"value": ADDR, "source": "vault"}}
    with patch.object(crypto, "call_tool", return_value=data) as call, \
         patch.object(crypto, "_read_bearer", return_value="b"):
        assert crypto.cmd_crypto(_args()) == 0
    assert call.call_args.args[0] == "crypto_settings_get"
    assert "ON" in capsys.readouterr().out


def test_set_sends_only_the_public_address():
    with patch.object(crypto, "call_tool", return_value={"enabled": True}) as call, \
         patch.object(crypto, "_read_bearer", return_value="b"):
        assert crypto.cmd_crypto(_args(crypto_action="platform-set", pay_to=ADDR)) == 0
    name, arguments = call.call_args.args[:2]
    assert name == "crypto_settings_set" and arguments["pay_to"] == ADDR


def test_set_with_nothing_is_refused():
    assert crypto.cmd_crypto(_args(crypto_action="platform-set")) == 1


def test_refusal_is_exit_1_and_unreachable_is_exit_2():
    with patch.object(crypto, "_read_bearer", return_value="b"):
        with patch.object(crypto, "call_tool", return_value={"ok": False, "error": "409"}):
            assert crypto.cmd_crypto(_args(crypto_action="platform-set", pay_to=ADDR)) == 1
        with patch.object(crypto, "call_tool",
                          side_effect=crypto.SpendUnavailableError("down")):
            assert crypto.cmd_crypto(_args()) == 2
