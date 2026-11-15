"""Both MCP Registry entries are bumped together, never one of them.

The root `server.json` (io.github auth) and `packaging/mcp-registry/server.json`
(com.aitherium domain auth) both carry the package version twice. Before this
split only the root one was bumped, so a release could leave the entry that is
actually published to the public registry advertising an older version.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sync_versions", Path(__file__).resolve().parent.parent / "packaging" / "sync_versions.py"
)
sync_versions = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync_versions)

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ("server.json", "packaging/mcp-registry/server.json")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A COPY of both entries + README; the real files are never rewritten."""
    for rel in (*ENTRIES, "README.md"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, target)
    monkeypatch.setattr(
        sync_versions, "__file__", str(tmp_path / "packaging" / "sync_versions.py")
    )
    return tmp_path


def _versions(root: Path) -> list[tuple[str, str]]:
    out = []
    for rel in ENTRIES:
        doc = json.loads((root / rel).read_text(encoding="utf-8"))
        out.append((doc["version"], doc["packages"][0]["version"]))
    return out


def test_bumps_both_entries(tree):
    assert sync_versions.sync_server_json("9.9.9", check=False) is True
    assert _versions(tree) == [("9.9.9", "9.9.9"), ("9.9.9", "9.9.9")]


def test_check_names_both_entries_when_stale(tree, capsys):
    assert sync_versions.sync_server_json("9.9.9", check=True) is False
    out = capsys.readouterr().out
    for rel in ENTRIES:
        assert f"{rel}: " in out


def test_missing_ownership_marker_fails_even_when_versions_match(tree):
    readme = tree / "README.md"
    readme.write_text("# awdk\n", encoding="utf-8")
    version = json.loads((tree / "server.json").read_text(encoding="utf-8"))["version"]
    assert sync_versions.sync_server_json(version, check=True) is False
