"""`adk new flue` copies the Flue-on-Aitherium template and prints ITS commands.

`adk new` was written for Python templates: it printed `pip install -r
requirements.txt` and `python serve.py` for whatever it copied. For a Node
project those two lines are false, so a template may now carry `.adk-next-steps`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import adk
from adk import cli

TEMPLATES = Path(adk.__file__).parent / "templates"


def _new(template, directory):
    return cli.cmd_new(argparse.Namespace(template=template, directory=str(directory)))


def test_new_flue_copies_the_project_and_prints_node_commands(tmp_path, capsys):
    target = tmp_path / "my-agent"
    assert _new("flue", target) == 0
    out = capsys.readouterr().out

    for rel in ("package.json", "flue.config.ts", "tsconfig.json", ".env.example",
                "src/aitherium.ts", "src/agents/aither.ts", "README.md", "AGENTS.md"):
        assert (target / rel).is_file(), rel
    # the next-steps file is an instruction to `adk new`, not part of the project
    assert not (target / cli._TEMPLATE_NEXT_STEPS).exists()
    assert not (target / "node_modules").exists()

    assert "npm install" in out
    assert "npx flue run src/agents/aither.ts" in out
    assert "pip install" not in out and "serve.py" not in out


def test_every_printed_command_is_in_the_templates_own_readme():
    """The steps `adk new` prints and the README must not drift apart."""
    readme = (TEMPLATES / "flue" / "README.md").read_text(encoding="utf-8")
    steps = (TEMPLATES / "flue" / cli._TEMPLATE_NEXT_STEPS).read_text(encoding="utf-8")
    commands = [ln.strip() for ln in steps.splitlines() if ln.strip() and not ln.startswith("#")]
    assert commands, "the template names no commands"
    for command in commands:
        assert command in readme, command


def test_python_templates_keep_their_python_steps(tmp_path, capsys):
    assert _new("deep-research", tmp_path / "dr") == 0
    out = capsys.readouterr().out
    assert "pip install -r requirements.txt" in out and "python serve.py" in out


def test_unknown_template_lists_flue_as_available(tmp_path, capsys):
    assert _new("no-such-template", tmp_path / "x") == 1
    out = capsys.readouterr().out
    assert "flue" in out and "deep-research" in out


def test_refuses_a_non_empty_target(tmp_path, capsys):
    target = tmp_path / "taken"
    target.mkdir()
    (target / "keep.txt").write_text("mine", encoding="utf-8")
    assert _new("flue", target) == 1
    assert (target / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert not (target / "package.json").exists()
