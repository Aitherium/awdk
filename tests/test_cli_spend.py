"""``adk spend``: registered and dispatched, honest when unavailable, formats the contract.

Goes through the real argparse tree and ``adk.cli.main`` so it fails if the verb is
not registered or not dispatched. The fetch is a fake; nothing touches a network.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List

import pytest

from adk import cli
from adk.commands import spend

CONTRACT = {
    "window_hours": 24, "generated_at": "2026-10-04T12:00:00Z", "total_usd": 12.3456,
    "unpriced_requests": 2,
    "providers": [{"provider": "deepseek", "usd": 12.3456, "prompt_tokens": 1000000,
                   "completion_tokens": 50000, "requests": 40, "failed": 1,
                   "models": [{"model": "deepseek-v4-flash", "usd": 12.3456,
                               "prompt_tokens": 1000000, "completion_tokens": 50000,
                               "requests": 40}]}],
    "top_sources": [{"source": "vnext_pillars_teacher", "usd": 9.0, "requests": 30,
                     "tokens": 1200}],
    "balance": {"deepseek": {"available": True, "total_balance": "12.34", "currency": "USD",
                             "checked_at": "2026-10-04T12:00:00Z", "error": None}},
}


def _parse(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="adk")
    sub = parser.add_subparsers(dest="command")
    cli._register_commands(sub)
    return parser.parse_args(argv)


def test_spend_parses_defaults_and_flags() -> None:
    a = _parse(["spend"])
    assert a.command == "spend" and a.hours == 24 and a.json is False
    b = _parse(["spend", "--hours", "168", "--json"])
    assert b.hours == 168 and b.json is True


def test_report_lines_carry_every_section() -> None:
    text = "\n".join(spend.format_report(CONTRACT))
    assert "last 24h" in text
    assert "Total  $12.35" in text and "2 unpriced request(s) NOT in the total" in text
    assert "deepseek" in text and "(1 failed)" in text and "1,000,000 in / 50,000 out" in text
    assert "deepseek-v4-flash" in text
    assert "Top callers" in text and "vnext_pillars_teacher" in text and "$9.00" in text
    assert "DeepSeek balance  12.34 USD  (checked 12:00Z)" in text


def test_unavailable_balance_says_so() -> None:
    lines = spend.format_balances({"deepseek": {"available": False, "error": "HTTP 401"}})
    assert lines == ["  DeepSeek balance  unavailable: HTTP 401"]


def test_seven_day_label() -> None:
    assert spend.window_label(168) == "7d" and spend.window_label(24) == "24h"


def test_cmd_spend_json_passes_hours_through() -> None:
    seen: List[Any] = []
    out: List[str] = []

    def fake(hours: int, url: str, bearer: str) -> dict:
        seen.append((hours, url))
        return CONTRACT

    rc = spend.cmd_spend(_parse(["spend", "--hours", "720", "--json",
                                 "--gateway", "http://gw/mcp"]), fetch=fake, out=out.append)
    assert rc == 0 and seen == [(720, "http://gw/mcp")]
    assert json.loads(out[0])["total_usd"] == 12.3456


def test_unavailable_exits_2_and_prints_no_numbers(capsys: pytest.CaptureFixture) -> None:
    out: List[str] = []

    def down(*_a: Any) -> dict:
        raise spend.SpendUnavailableError("Unknown tool: cloud_spend")

    assert spend.cmd_spend(_parse(["spend"]), fetch=down, out=out.append) == 2
    assert out == []
    assert "spend unavailable: Unknown tool: cloud_spend" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [None, {}, {"error": "x"}, {"total_usd": 1.0},
                                 {"total_usd": "?", "providers": []}])
def test_off_contract_is_rejected(bad: Any) -> None:
    assert spend.validate(bad) is None


def test_tool_payload_unwraps_text_and_errors() -> None:
    ok = {"content": [{"type": "text", "text": json.dumps(CONTRACT)}]}
    assert spend.validate(spend.tool_payload(ok)) is not None
    err = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
    assert spend.tool_payload(err) == {"error": "boom"}


def test_main_dispatches_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["adk", "spend"])
    monkeypatch.setattr(cli, "_cached_parser", None, raising=False)

    def down(*_a: Any) -> dict:
        raise spend.SpendUnavailableError("gateway down")

    monkeypatch.setattr(spend, "fetch_spend", down)
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2
