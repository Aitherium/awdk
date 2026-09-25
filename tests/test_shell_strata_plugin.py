"""/strata is a registered shell builtin that round-trips through adk.strata."""

from __future__ import annotations

import asyncio

import pytest
from adk.shell.plugins import PluginRegistry, SlashCommand


@pytest.fixture()
def strata_cmd(tmp_path, monkeypatch):
    import adk.strata as strata_mod

    monkeypatch.delenv("AITHER_STRATA_URL", raising=False)
    local_only = strata_mod.Strata(
        backends=[strata_mod.LocalBackend(base_dir=tmp_path)], data_dir=tmp_path
    )
    monkeypatch.setattr(strata_mod, "_strata_instance", local_only)
    reg = PluginRegistry([])
    reg.load_all()
    cmd = reg.get("strata")
    assert isinstance(cmd, SlashCommand), "/strata is not registered"
    return cmd


def _run(cmd, *args):
    return asyncio.run(cmd.run(list(args), {}))


def test_put_ls_cat_rm_roundtrip(strata_cmd):
    assert "Stored" in _run(strata_cmd, "put", "notes/a.txt", "hello", "world")
    assert "notes/a.txt" in _run(strata_cmd, "ls", "notes/")
    assert _run(strata_cmd, "cat", "notes/a.txt") == "hello world"
    assert _run(strata_cmd, "exists", "notes/a.txt") == "yes"
    assert "Deleted" in _run(strata_cmd, "rm", "notes/a.txt")
    assert _run(strata_cmd, "exists", "notes/a.txt") == "no"
    assert "Not found" in _run(strata_cmd, "cat", "notes/a.txt")


def test_backends_and_usage(strata_cmd):
    assert '"local"' in _run(strata_cmd, "backends")
    assert "Usage" in _run(strata_cmd, "cat")
    assert "Unknown subcommand" in _run(strata_cmd, "frobnicate")
