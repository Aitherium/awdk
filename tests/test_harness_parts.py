"""adk harness parts -- the composed harness as this machine has it."""
from __future__ import annotations

import json

import pytest

from adk import brick_family, harness_parts


def _index(tmp_path, harness=None, bricks=None):
    doc = {
        "schema": brick_family.SCHEMA,
        "count": 3,
        "bricks": bricks or [
            {"id": "awa", "install": "pip install awa"},
            {"id": "awb", "install": "npm i -g @scope/awb"},
            {"id": "awc", "install": "git clone https://example.invalid/awc"},
            {"id": "awd", "install": "pip install git+https://example.invalid/awd"},
        ],
    }
    if harness is not None:
        doc["harness"] = harness
    p = tmp_path / "family.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


HARNESS = {"name": "awtest", "tagline": "The test harness", "status": "partial",
           "today": "two commands", "planned": "one command",
           "composes": ["awa", "awb", "awc", "awd"]}


def test_shipped_index_names_the_umbrella_and_only_composes_family_bricks():
    block = harness_parts.harness()
    ids = set(brick_family.by_id())
    assert block["name"]
    assert block["today"] and block["planned"], "both halves must be stated"
    assert block["composes"] and set(block["composes"]) <= ids


def test_the_name_is_read_from_the_index_not_from_this_package(tmp_path):
    p = _index(tmp_path, dict(HARNESS, name="awrenamed"))
    rep = harness_parts.report(p, pip_probe=lambda d: None, command_probe=lambda c: None)
    assert rep["name"] == "awrenamed"
    assert harness_parts.render(rep).startswith("awrenamed -- ")
    # and nowhere in the module is a name baked in
    src = open(harness_parts.__file__, encoding="utf-8").read()
    assert harness_parts.harness()["name"] not in src


def test_installed_missing_and_unknown_are_three_different_answers(tmp_path):
    p = _index(tmp_path, HARNESS)
    seen = []

    def pip_probe(dist):
        seen.append(dist)
        return {"version": "1.2.3", "editable": True} if dist == "awa" else None

    rep = harness_parts.report(
        p, pip_probe=pip_probe,
        command_probe=lambda c: {"version": None, "editable": False})
    rows = {r["id"]: r for r in rep["parts"]}
    assert rows["awa"]["state"] == "installed" and rows["awa"]["version"] == "1.2.3"
    assert "editable" in rows["awa"]["note"]
    # on PATH but no version: installed, and the version is NOT invented
    assert rows["awb"]["state"] == "installed" and rows["awb"]["version"] is None
    assert "--version" in rows["awb"]["note"]
    # a git clone cannot be checked, and is not counted as installed
    assert rows["awc"]["state"] == "unknown"
    # a git+ pip line is probed under the brick's own name, and is missing here
    assert rows["awd"]["state"] == "missing" and "awd" in seen
    assert rows["awd"]["install"] == "pip install git+https://example.invalid/awd"
    assert rep["counts"] == {"installed": 2, "missing": 1, "unknown": 1}
    text = harness_parts.render(rep)
    assert "2 of 4 installed, 1 missing, 1 not checkable" in text
    assert "Today: two commands" in text and "Planned, not built: one command" in text


def test_a_missing_npm_command_is_missing_with_its_install_line(tmp_path):
    p = _index(tmp_path, dict(HARNESS, composes=["awb"]))
    rep = harness_parts.report(p, pip_probe=lambda d: None, command_probe=lambda c: None)
    row = rep["parts"][0]
    assert row["state"] == "missing" and row["install"] == "npm i -g @scope/awb"


def test_a_composed_id_absent_from_the_family_is_reported_not_dropped(tmp_path):
    p = _index(tmp_path, dict(HARNESS, composes=["awa", "ghost"]))
    rep = harness_parts.report(p, pip_probe=lambda d: {"version": "1", "editable": False},
                               command_probe=lambda c: None)
    assert [r["id"] for r in rep["parts"]] == ["awa", "ghost"]
    assert rep["parts"][1]["state"] == "unknown"


@pytest.mark.parametrize("block", [None, {}, {"name": "x"}, {"name": "x", "composes": []},
                                   {"composes": ["awa"]}])
def test_an_index_without_a_usable_harness_block_raises(tmp_path, block):
    p = _index(tmp_path, block)
    with pytest.raises(harness_parts.HarnessDataMissingError):
        harness_parts.harness(p)


def test_command_probe_never_guesses(tmp_path):
    assert harness_parts._command_probe("nope", which=lambda c: None) is None

    class Res:
        stdout = "awb 4.5.6\n"
        stderr = ""

    got = harness_parts._command_probe("awb", which=lambda c: "/bin/awb",
                                       run=lambda *a, **k: Res())
    assert got["version"] == "4.5.6"

    def boom(*a, **k):
        raise OSError("no")

    got = harness_parts._command_probe("awb", which=lambda c: "/bin/awb", run=boom)
    assert got["version"] is None


def test_exit_code_is_1_when_a_part_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(harness_parts, "report", lambda: {
        "name": "n", "tagline": "t", "status": "partial", "today": "a", "planned": "b",
        "parts": [{"id": "awa", "state": "missing", "version": None,
                   "install": "pip install awa", "note": None}],
        "counts": {"installed": 0, "missing": 1, "unknown": 0}})
    assert harness_parts.main([]) == 1
    assert "pip install awa" in capsys.readouterr().out
    assert harness_parts.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out)["counts"]["missing"] == 1
