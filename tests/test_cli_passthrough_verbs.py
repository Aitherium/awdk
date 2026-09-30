"""Pass-through verbs accept a LEADING flag (`adk lookout --help`).

A subparser registered with ``add_help=False`` + ``nargs=REMAINDER`` hands its
args to the verb's own parser -- but REMAINDER only captures after a
positional, so ``adk lookout --help`` died as ``unrecognized arguments`` (rc 2)
unless ``main()`` dispatched the verb before argparse. Measured 2026-09-30:
bricks, lookout, mobile and link were all missing from that pre-dispatch.

The verb list is DERIVED from the parser, so a future add_help=False verb that
nobody adds to ``_PASSTHROUGH_VERBS`` fails here.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from adk import cli

AWDK = Path(__file__).resolve().parents[1]


def _passthrough_subparsers() -> "list[str]":
    parser = argparse.ArgumentParser(prog="adk")
    sub = parser.add_subparsers(dest="command")
    cli._register_commands(sub)
    return sorted(name for name, p in sub.choices.items() if not p.add_help)


def test_every_add_help_false_verb_is_predispatched():
    verbs = _passthrough_subparsers()
    assert verbs, "found no add_help=False subparser -- the probe is broken"
    missing = [v for v in verbs if v not in cli._PASSTHROUGH_VERBS]
    assert not missing, (f"{missing} are registered with add_help=False but are not in "
                         "cli._PASSTHROUGH_VERBS: `adk <verb> --help` will exit 2")


@pytest.mark.parametrize("verb", sorted(cli._PASSTHROUGH_VERBS))
def test_leading_help_reaches_the_verb(verb):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(AWDK) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-m", "adk.cli", verb, "--help"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=180)
    assert "unrecognized arguments" not in r.stderr, r.stderr[-400:]
    assert r.returncode == 0, (r.returncode, r.stderr[-400:])
    assert not r.stdout.startswith("usage: adk [-h]"), "the TOP parser answered"
