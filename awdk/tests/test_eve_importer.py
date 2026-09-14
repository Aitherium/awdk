"""Eve → AitherADK pack importer — end-to-end, no network.

Regression coverage for three defects found after the first build reported the
importer "verified end-to-end":

  1. model-as-string crash — eve authors write `model: "anthropic/claude-sonnet-5"`
     (a plain string), but the importer assumed a dict and raised AttributeError.
  2. tools silently dropped — the copier globbed `agent/tools` but eve's real
     layout is `agent/agent/tools`, so no .ts tool was ever copied.
  3. skills silently stubbed — same wrong directory; the real skill body was
     replaced by an empty stub, so an "imported" pack looked complete but inert.

The one assertion that matters: the imported pack is discoverable by the REAL
pack_discovery machinery, not merely a directory that exists on disk.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from adk.importers.eve import COMPILED_AGENT_MANIFEST_VERSION, import_eve_agent


def _make_eve_agent(root: Path) -> Path:
    """Build a minimal eve agent dir in eve's real `agent/` layout."""
    agent = root / "weather-agent"
    (agent / "agent" / "skills").mkdir(parents=True)
    (agent / "agent" / "tools").mkdir(parents=True)
    (agent / "agent" / "instructions.md").write_text(
        "You are a concise weather assistant.", encoding="utf-8"
    )
    (agent / "agent" / "skills" / "get-weather.md").write_text(
        "---\ndescription: Fetch weather before a forecast answer.\n---\n"
        "REAL-SKILL-BODY-MARKER",
        encoding="utf-8",
    )
    (agent / "agent" / "tools" / "get_weather.ts").write_text(
        "export default defineTool({ /* REAL-TOOL-MARKER */ });", encoding="utf-8"
    )
    manifest = {
        "version": COMPILED_AGENT_MANIFEST_VERSION,
        "kind": "eve-agent-compiled-manifest",
        # model as a STRING — the authored form that used to crash the importer.
        "config": {
            "name": "Weather Agent",
            "model": "anthropic/claude-sonnet-5",
            "description": "mock weather",
        },
        "instructions": {"text": "You are a concise weather assistant."},
        "skills": [
            {
                "name": "get-weather",
                "description": "Fetch weather",
                "path": "agent/skills/get-weather.md",
            }
        ],
        "tools": [{"name": "get_weather", "path": "agent/tools/get_weather.ts"}],
    }
    (agent / ".compiled-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return agent


def test_import_string_model_does_not_crash(tmp_path):
    agent = _make_eve_agent(tmp_path)
    res = import_eve_agent(str(agent), tmp_path / "packs")
    assert res["pack_id"].endswith("-eve-import")


def test_skill_copied_verbatim_not_stubbed(tmp_path):
    agent = _make_eve_agent(tmp_path)
    res = import_eve_agent(str(agent), tmp_path / "packs")
    skill = (Path(res["pack_dir"]) / "skills" / "get-weather.md").read_text(
        encoding="utf-8"
    )
    assert "REAL-SKILL-BODY-MARKER" in skill, "skill degraded to a stub"


def test_typescript_tool_copied_verbatim(tmp_path):
    agent = _make_eve_agent(tmp_path)
    res = import_eve_agent(str(agent), tmp_path / "packs")
    tool = Path(res["pack_dir"]) / "tools" / "node" / "get_weather.ts"
    assert tool.is_file(), "TS tool was silently dropped"
    assert "REAL-TOOL-MARKER" in tool.read_text(encoding="utf-8")


def test_imported_pack_is_discoverable_by_real_discovery(tmp_path, monkeypatch):
    agent = _make_eve_agent(tmp_path)
    packs = tmp_path / "packs"
    res = import_eve_agent(str(agent), packs)

    monkeypatch.setenv("AITHER_PACKS_DIR", str(packs))
    import adk.pack_discovery as pd

    importlib.reload(pd)
    try:
        ids = [p.get("id") or p.get("name") for p in pd.list_available_packs()]
        assert res["pack_id"] in ids, (
            f"imported pack {res['pack_id']} not found by real discovery: {ids}"
        )
    finally:
        importlib.reload(pd)  # reset module-level packs-dir caching for other tests


def test_unknown_manifest_version_is_rejected(tmp_path):
    agent = _make_eve_agent(tmp_path)
    manifest_path = agent / ".compiled-manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["version"] = COMPILED_AGENT_MANIFEST_VERSION + 999
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        import_eve_agent(str(agent), tmp_path / "packs")
