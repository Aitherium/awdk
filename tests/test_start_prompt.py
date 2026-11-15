"""START.md: the one-prompt on-ramp must only name commands the CLI really has.

A paste-in prompt rots silently: a renamed flag turns a coding agent's first
five minutes into argparse errors, and nobody at the keyboard knows why. Every
`adk ...` line inside a fenced block of START.md is therefore parsed by the real
parser. The live half (the commands succeeding against a model and the gateway)
was run by hand on 2026-11-12; this pins the half that can drift in the repo.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

import adk
from adk import agent_prompt, cli

START = Path(adk.__file__).parent / "START.md"

#: Stand-ins for the placeholders the prompt asks the agent to fill in.
_PLACEHOLDERS = {"<name>": "my-agent", "<port>": "8467", "<model>": "some-model"}


def _fenced_lines(text: str) -> list[str]:
    lines: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", text, flags=re.S):
        lines += [ln.strip() for ln in block.splitlines() if ln.strip()]
    return lines


def _adk_commands() -> list[str]:
    return [ln for ln in _fenced_lines(START.read_text(encoding="utf-8")) if ln.startswith("adk ")]


def _fill(line: str) -> str:
    for placeholder, value in _PLACEHOLDERS.items():
        line = line.replace(placeholder, value)
    return line


def test_the_prompt_names_the_commands_of_the_proven_path():
    commands = _adk_commands()
    for expected in ("adk status", "adk mcp status", "adk init <name>",
                     "adk mcp setup --bake-token", "adk run -p <port> -m <model>"):
        assert expected in commands, expected


@pytest.mark.parametrize("line", _adk_commands())
def test_every_adk_command_in_the_prompt_parses(line):
    argv = shlex.split(_fill(line))[1:]
    assert "<" not in " ".join(argv), f"unfilled placeholder in {line!r}"
    if argv == ["--version"]:
        return  # handled by main() before the subcommand parser; covered below
    args = cli.get_parser().parse_args(argv)   # SystemExit(2) = the command does not exist
    assert args.command == argv[0]


def test_a_command_that_does_not_exist_would_be_caught():
    """The parametrized test above must be able to fail."""
    with pytest.raises(SystemExit):
        cli.get_parser().parse_args(["mcp", "setup", "--bake-tokens"])
    with pytest.raises(SystemExit):
        cli.get_parser().parse_args(["innit", "my-agent"])


def test_version_flag_exists_on_the_real_entry_point(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["adk", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip().startswith("adk ")


def test_the_prompt_never_tells_the_agent_to_sign_in_for_the_person():
    text = START.read_text(encoding="utf-8")
    assert "adk login" not in _fenced_lines(text)          # never a command to run
    assert "never run" in text.lower() and "adk login" in text
    assert "adk setup" not in _fenced_lines(text)          # can fight a running model for the GPU
    assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}|aither_(sk|pat)_[A-Za-z0-9]{8,}", text)


def test_the_prompt_ends_on_a_check_that_can_fail():
    text = START.read_text(encoding="utf-8")
    assert "mcp__aitheros__" in text and "tool_calls" in text
    assert "not a pass" in text


def test_agent_prompt_start_flag_prints_start_md(capsys):
    assert agent_prompt.cmd_agent_prompt(argparse.Namespace(raw=True, start=True)) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Skill: Create a first Aitherium agent")
    # the long reference prompt is still the default
    assert agent_prompt.cmd_agent_prompt(argparse.Namespace(raw=True, start=False)) == 0
    assert "AitherADK Agent Setup Prompt" in capsys.readouterr().out
    args = cli.get_parser().parse_args(["agent-prompt", "--start"])
    assert args.start is True
