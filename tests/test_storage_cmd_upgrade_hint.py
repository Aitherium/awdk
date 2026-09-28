"""`adk storage files ...` on a node whose awstorage predates the file index prints an
upgrade hint and exits 2 -- never a confusing argparse error from an old CLI."""

from __future__ import annotations

import sys
import types

from adk import storage_cmd


def _fake_awstorage(monkeypatch, version: str, calls: list):
    pkg = types.ModuleType("awstorage")
    pkg.__version__ = version
    cli = types.ModuleType("awstorage.cli")
    cli.main = lambda argv: calls.append(argv) or 0
    monkeypatch.setitem(sys.modules, "awstorage", pkg)
    monkeypatch.setitem(sys.modules, "awstorage.cli", cli)


def test_old_awstorage_gets_an_upgrade_hint(monkeypatch, capsys):
    calls: list = []
    _fake_awstorage(monkeypatch, "0.1.0", calls)
    assert storage_cmd.main(["files", "find", "x"]) == 2
    assert "awstorage>=0.3.0" in capsys.readouterr().err and calls == []
    assert storage_cmd.main(["scan", "/"]) == 0 and calls == [["scan", "/"]]


def test_new_awstorage_passes_through(monkeypatch):
    calls: list = []
    _fake_awstorage(monkeypatch, "0.3.0", calls)
    assert storage_cmd.main(["files", "find", "x"]) == 0
    assert calls == [["files", "find", "x"]]
