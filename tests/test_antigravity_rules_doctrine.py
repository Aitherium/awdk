"""Tests verifying Aither Antigravity execution-doctrine rules & emitter parity.

Ensures the permanent operating rules ("Execute implied fixes — zero
permission-begging", "Check prior decisions before asking", "Hard stop ONLY on
real B1/B2/B3") stay present across:
  * adk.antigravity_setup._default_rules_markdown()  (aitheros plugin emitter)
  * adk.antigravity_setup._aither_persona_rules_markdown() + setup_aither_persona()
  * awdk/plugins/aither-persona/{plugin.json, rules/AGENTS.md}
  * awdk/plugins/antigravity/rules/AGENTS.md & adk/harnesses/antigravity_mod/rules/AGENTS.md
"""

from __future__ import annotations

import json
from pathlib import Path

from adk.antigravity_setup import (
    DEFAULT_PERSONA_PLUGIN_NAME,
    DEFAULT_PLUGIN_NAME,
    _aither_persona_manifest,
    _aither_persona_rules_markdown,
    _default_rules_markdown,
    setup_aither_persona,
    setup_antigravity,
)

AWDK_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DOCTRINE_PHRASES = (
    "zero permission-begging",
    "Check prior decisions before asking",
    "Hard stop ONLY on real B1/B2/B3",
    "~/.gemini/antigravity/brain/",
    "~/.claude/projects/",
)


def test_default_rules_markdown_contains_execution_doctrine():
    md = _default_rules_markdown()
    low = md.lower()
    for phrase in REQUIRED_DOCTRINE_PHRASES:
        assert phrase.lower() in low, f"Missing {phrase!r} in _default_rules_markdown()"
    assert "execute implied fixes" in low


def test_aither_persona_rules_and_repo_mirror_are_in_sync():
    md = _aither_persona_rules_markdown()
    low = md.lower()
    for phrase in ("A-I-T-H-E-R", "Realist, not a sycophant", *REQUIRED_DOCTRINE_PHRASES):
        assert phrase.lower() in low, f"Missing {phrase!r} in _aither_persona_rules_markdown()"

    mirror_dir = AWDK_ROOT / "plugins" / "aither-persona"
    mirror_md = (mirror_dir / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    assert mirror_md == md

    mirror_manifest = json.loads((mirror_dir / "plugin.json").read_text(encoding="utf-8"))
    assert mirror_manifest == _aither_persona_manifest()


def test_portable_antigravity_plugin_mirrors_stay_in_sync():
    standalone = (
        AWDK_ROOT / "plugins" / "antigravity" / "rules" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    bundled = (
        AWDK_ROOT / "adk" / "harnesses" / "antigravity_mod" / "rules" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert standalone == bundled
    low = standalone.lower()
    for phrase in REQUIRED_DOCTRINE_PHRASES:
        assert phrase.lower() in low, f"Missing {phrase!r} in portable antigravity rules"


def test_setup_aither_persona_and_auto_sync_in_setup_antigravity(tmp_path: Path):
    cfg = tmp_path / "cfg"

    # Dry-run writes nothing.
    dry = setup_aither_persona(cfg, dry_run=True)
    assert dry["ok"] is True and dry["dry_run"] is True
    assert not (cfg / "plugins" / DEFAULT_PERSONA_PLUGIN_NAME).exists()

    # Clean setup_antigravity leaves aither-persona absent unless requested.
    res_plain = setup_antigravity(cfg, include_bundled_skills=False)
    assert res_plain["ok"] is True
    assert res_plain["persona_synced"] is False
    aitheros_rules = (cfg / "plugins" / DEFAULT_PLUGIN_NAME / "rules" / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    assert "Execute implied fixes" in aitheros_rules

    # Explicit include_persona=True installs aither-persona.
    res_with = setup_antigravity(cfg, include_bundled_skills=False, include_persona=True)
    assert res_with["ok"] is True
    assert res_with["persona_synced"] is True
    persona_rules_path = cfg / "plugins" / DEFAULT_PERSONA_PLUGIN_NAME / "rules" / "AGENTS.md"
    assert persona_rules_path.read_text(encoding="utf-8") == _aither_persona_rules_markdown()

    # Once installed, re-running setup_antigravity auto-refreshes stale persona rules.
    persona_rules_path.write_text("# stale\n", encoding="utf-8")
    res_refresh = setup_antigravity(cfg, include_bundled_skills=False)
    assert res_refresh["persona_synced"] is True
    assert persona_rules_path.read_text(encoding="utf-8") == _aither_persona_rules_markdown()
