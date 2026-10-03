"""adk kvholder without numpy: the CLI still builds, plan works, serve says what is missing."""

from __future__ import annotations

import subprocess
import sys

BLOCK = "import sys; sys.modules['numpy'] = None\n"


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", BLOCK + code], capture_output=True, text=True, encoding="utf-8"
    )


def test_cli_works_without_numpy():
    r = _run(
        "import argparse, adk.kvholder as k\n"
        "ap = argparse.ArgumentParser(); k.register(ap.add_subparsers(dest='command'))\n"
        "assert k.run(ap.parse_args(['kvholder', 'plan', '--free-gb', '8'])) == 0\n"
        "assert k.run(ap.parse_args(['kvholder', 'serve', '--max-mb', '8'])) == 2\n"
    )
    assert r.returncode == 0, r.stderr


def test_adk_parser_builds_without_numpy():
    """The `adk` CLI registers kvholder when it builds its parser: that must not need numpy."""
    r = _run(
        "import argparse\n"
        "from adk.kvholder import register\n"
        "ap = argparse.ArgumentParser(); register(ap.add_subparsers(dest='command'))\n"
        "print('ok')\n"
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


def test_cli_constants_match_the_modules():
    """kvholder_cli repeats a few constants so `adk` never imports numpy to build its parser."""
    from adk import kvholder, kvholder_cli, kvholder_net

    assert kvholder_cli.DEFAULT_PORT == kvholder.DEFAULT_PORT
    assert kvholder_cli.DEFAULT_WS_PORT == kvholder_net.DEFAULT_WS_PORT
    assert set(kvholder_cli.PROFILE_NAMES) == set(kvholder.PROFILES)
    assert set(kvholder_cli.KV_NAMES) == set(kvholder.KV_TYPES)


def test_building_the_kvholder_parser_attempts_no_numpy_import():
    r = _run(
        "import builtins\n"
        "seen = []\n"
        "real = builtins.__import__\n"
        "def spy(name, *a, **k):\n"
        "    if name.split('.')[0] == 'numpy': seen.append(name)\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = spy\n"
        "import argparse\n"
        "from adk.kvholder_cli import register\n"
        "ap = argparse.ArgumentParser(); register(ap.add_subparsers(dest='command'))\n"
        "assert not seen, seen\n"
        "print('ok')\n"
    )
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr
