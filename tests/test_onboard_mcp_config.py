"""`adk onboard` must write MCP configs that can actually start.

Until 2026-09-27 onboarding wrote ``npx -y aither-mcp-server`` — a package that
was never published to npm (E404) — and printed ``api_key[:16]``. These tests
run the real onboarding flow against a temp HOME/CWD and read back every file
it wrote.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adk import cli, mcp_entries  # noqa: E402

FAKE_KEY = "aither_sk_live_" + "Q" * 24 + "WXYZ"

#: npm/PyPI names a generated entry may launch. `awnode` is on PyPI.
PUBLISHED = mcp_entries.KNOWN_COMMANDS


def _walk_entries(doc: dict):
    for key in ("mcpServers", "servers"):
        for name, entry in (doc.get(key) or {}).items():
            yield name, entry


def _assert_entry_is_real(name: str, entry: dict) -> None:
    if "url" in entry:
        assert entry["url"] == mcp_entries.HOSTED_MCP_URL, (name, entry)
        assert entry.get("type") == "http", (name, entry)
        auth = entry.get("headers", {}).get("Authorization", "")
        assert auth.startswith("Bearer ${") and "AITHER_API_KEY" in auth, (name, entry)
        return
    cmd = entry.get("command")
    assert cmd in PUBLISHED, f"{name}: command {cmd!r} is not a published package"
    assert cmd != "npx", f"{name}: npx of an unverified package"
    assert "aither-mcp-server" not in json.dumps(entry)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    (home / ".cursor").mkdir(parents=True)
    (proj / ".vscode").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("AITHER_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    return home, proj


def _run(api_key: str = FAKE_KEY) -> int:
    args = argparse.Namespace(agent=None, tenant=None, quick=False, webgpu=False,
                              discord=False, api_key=api_key)
    return cli.cmd_onboard(args)


def test_onboard_writes_only_real_endpoints(sandbox, capsys):
    home, proj = sandbox
    _run()
    written = {
        "claude-code": proj / ".mcp.json",
        "cursor": home / ".cursor" / "mcp.json",
        "vscode": proj / ".vscode" / "mcp.json",
    }
    for client, path in written.items():
        assert path.exists(), f"{client}: {path} not written"
        text = path.read_text(encoding="utf-8")
        assert FAKE_KEY not in text, f"{client}: raw API key written to {path}"
        entries = dict(_walk_entries(json.loads(text)))
        assert "aitheros" in entries, client
        for name, entry in entries.items():
            _assert_entry_is_real(name, entry)


def test_onboard_never_prints_key_material(sandbox, capsys):
    _run()
    out = capsys.readouterr().out
    assert FAKE_KEY[:16] not in out
    assert "Q" * 6 not in out
    assert "****WXYZ" in out


def test_onboard_repairs_a_broken_legacy_entry(sandbox):
    _home, proj = sandbox
    legacy = {"mcpServers": {
        "aitheros": {"command": "npx", "args": ["-y", "aither-mcp-server"]},
        "mine": {"command": "my-own-server"},
    }}
    (proj / ".mcp.json").write_text(json.dumps(legacy), encoding="utf-8")
    _run()
    doc = json.loads((proj / ".mcp.json").read_text(encoding="utf-8"))
    _assert_entry_is_real("aitheros", doc["mcpServers"]["aitheros"])
    assert doc["mcpServers"]["mine"] == {"command": "my-own-server"}  # user entry untouched


def test_hosted_entry_env_ref_per_client():
    assert "${AITHER_API_KEY}" in json.dumps(mcp_entries.hosted_entry("claude-code"))
    assert "${env:AITHER_API_KEY}" in json.dumps(mcp_entries.hosted_entry("cursor"))


def test_no_unpublished_package_name_in_awdk_source():
    """The dead package name must not come back anywhere in the shipped tree."""
    root = Path(__file__).resolve().parents[1] / "adk"
    hits = [
        str(p) for p in root.rglob("*")
        if p.is_file() and p.suffix in {".py", ".md", ".json", ".yaml", ".yml", ".txt"}
        and p.name != "mcp_entries.py"
        and "aither-mcp-server" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not hits, hits
