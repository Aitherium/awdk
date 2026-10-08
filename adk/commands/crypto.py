"""``adk crypto`` -- self-service crypto payment settings from the terminal.

    adk crypto platform-get                    # receiving wallet, facilitator, network + source
    adk crypto platform-set --pay-to <ADDR>    # set the platform receiving wallet (admin)
    adk crypto platform-set --facilitator-url https://... --network solana

Calls the platform-only gateway MCP tools ``crypto_settings_get`` /
``crypto_settings_set`` (ACTA ``/v1/admin/crypto/settings`` behind them), the same
surface Veil's admin page and agents use. The address is a PUBLIC Solana address;
this command never asks for, reads or prints a private key or seed phrase.
Gateway: ``--gateway`` > ``AITHER_SPEND_MCP_URL`` > the local ``http://127.0.0.1:8182/mcp``.

Linking YOUR OWN wallet to your account is done by signing in the app
(Settings > Wallet), so the key never leaves the wallet.

Exit codes: 0 ok; 1 refused (bad address, field pinned by the host env, not an
admin); 2 could not judge (gateway down, no bearer, malformed answer).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

from adk.commands.spend import DEFAULT_MCP_URL, SpendUnavailableError, _read_bearer, call_tool


def _gateway(args: Any) -> str:
    return (getattr(args, "gateway", "") or os.environ.get("AITHER_SPEND_MCP_URL", "")
            or DEFAULT_MCP_URL)


def _print_settings(data: Dict[str, Any]) -> None:
    print("x402 crypto payments: " + ("ON" if data.get("enabled") else "OFF"))
    for key in ("pay_to", "facilitator_url", "network"):
        field = data.get(key) or {}
        value = field.get("value") or "-"
        print("  {:<16} {}  ({})".format(key, value, field.get("source", "?")))


def cmd_crypto(args: Any) -> int:
    action = getattr(args, "crypto_action", None)
    if action not in ("platform-get", "platform-set"):
        print("usage: adk crypto {platform-get,platform-set}", file=sys.stderr)
        return 2
    if action == "platform-get":
        name, arguments = "crypto_settings_get", {}
    else:
        arguments = {"pay_to": args.pay_to or "", "facilitator_url": args.facilitator_url or "",
                     "network": args.network or ""}
        if not any(arguments.values()):
            print("nothing to set: pass --pay-to, --facilitator-url or --network",
                  file=sys.stderr)
            return 1
        name = "crypto_settings_set"
    try:
        data = call_tool(name, arguments, _gateway(args), _read_bearer(), client="adk-crypto")
    except SpendUnavailableError as exc:
        print("could not reach the platform: {}".format(exc), file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    if not isinstance(data, dict):
        print("malformed answer from the platform", file=sys.stderr)
        return 2
    if data.get("ok") is False or data.get("error"):
        print("refused: {}".format(json.dumps(data.get("error", data))[:400]), file=sys.stderr)
        return 1
    if not getattr(args, "json", False):
        _print_settings(data)
    return 0


def register_parser(sub: Any) -> None:
    p = sub.add_parser("crypto", help="Crypto payment settings (platform receiving wallet)")
    p.add_argument("--gateway", default="", help="MCP gateway URL")
    p.add_argument("--json", action="store_true", help="print the raw answer")
    wsub = p.add_subparsers(dest="crypto_action")
    wsub.add_parser("platform-get", help="Show the receiving wallet, facilitator, network")
    ps = wsub.add_parser("platform-set", help="Set them in the vault (platform admin)")
    ps.add_argument("--pay-to", default="", help="PUBLIC Solana address (never a key)")
    ps.add_argument("--facilitator-url", default="", help="x402 facilitator (https://)")
    ps.add_argument("--network", default="", help="solana | solana-devnet | base | ...")
