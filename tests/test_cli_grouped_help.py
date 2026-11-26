"""Tests for grouped `adk --help`, `adk decide`/`awdecide` disambiguation, and `--self-test`."""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout

import pytest

from adk import cli as adk_cli


def test_grouped_help_renders_all_subcommands_and_disambiguation():
    parser = adk_cli.get_parser()
    help_text = parser.format_help()

    for heading, _cmds in adk_cli._CLI_HELP_GROUPS:
        assert f"{heading}:" in help_text, f"missing group heading {heading!r}"

    # Every registered subcommand must appear in the grouped help output.
    sub_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    for cmd_name in sub_action.choices:
        assert cmd_name in help_text, f"subcommand {cmd_name!r} omitted from grouped help"

    assert "adk decide" in help_text
    assert "awdecide" in help_text


def test_cli_self_test_and_selftest_flags(monkeypatch):
    for flag in ("--self-test", "--selftest"):
        monkeypatch.setattr(sys, "argv", ["adk", flag])
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                rc = adk_cli.main()
            except SystemExit as exc:
                rc = int(exc.code or 0)
        assert rc == 0
        out = buf.getvalue()
        assert "adk --self-test OK" in out


def test_flat_command_parsing_preserved():
    parser = adk_cli.get_parser()
    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"

    args2 = parser.parse_args(["decide"])
    assert args2.command == "decide"

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
