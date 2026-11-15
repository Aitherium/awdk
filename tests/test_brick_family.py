"""adk.brick_family -- the vendored family index the three adk family lists now read."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from adk import brick_family, bricks, integrations

MONOREPO_COPY = Path(__file__).resolve().parents[2] / "AitherOS" / "config" / "family.json"


def test_vendored_index_loads_and_is_not_empty():
    doc = brick_family.load()
    assert doc["schema"] == brick_family.SCHEMA
    assert doc["count"] == len(doc["bricks"]) > 0
    assert "awdk" in brick_family.by_id()


@pytest.mark.skipif(not MONOREPO_COPY.exists(), reason="monorepo copy absent (installed wheel)")
def test_vendored_copy_is_byte_identical_to_the_monorepo_index():
    # EC018 asserts this from the registry side; this pins it from the package side.
    assert brick_family.DATA_PATH.read_bytes() == MONOREPO_COPY.read_bytes()


def test_missing_index_raises_never_returns_an_empty_family(tmp_path):
    with pytest.raises(brick_family.FamilyDataMissingError):
        brick_family.load(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": brick_family.SCHEMA, "bricks": []}), encoding="utf-8")
    with pytest.raises(brick_family.FamilyDataMissingError):
        brick_family.load(bad)


def test_bricks_family_is_derived_from_the_index():
    ids = {b["id"] for b in brick_family.bricks()}
    assert bricks.FAMILY, "an empty family would read as 'nothing installed'"
    assert all(bid in ids for bid, _dist, _src in bricks.FAMILY)
    assert list(bricks.FAMILY) == sorted(bricks.FAMILY)


def test_integrations_read_names_and_descriptions_from_the_index():
    fam = brick_family.by_id()
    rows = integrations._family()
    assert {r[0] for r in rows} == set(integrations._UNLOCKS)
    for name, module, description, _unlocks in rows:
        assert module == (fam[name].get("module") or name)
        assert description == fam[name]["tagline"]


def test_integrations_refuse_a_brick_the_index_does_not_register(monkeypatch):
    monkeypatch.setitem(integrations._UNLOCKS, "awnot-a-brick", ["x"])
    with pytest.raises(LookupError):
        integrations._family()
