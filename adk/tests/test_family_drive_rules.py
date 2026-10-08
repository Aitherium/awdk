"""Copy rules for the family drive, the pure parts.

Pins: the nearest folder rule wins, a file's own rule wins over every folder, no rule is
the drive's default; placement params carry only copies / ec / kinds of device / no-kids /
pin / an opaque rule id; a file is stored anew only when copies and erasure switch; a
guardian's newer browser edit is adopted, an older one is not.
"""
from __future__ import annotations

import pytest

from adk import family_drive_rules as rules
from adk import family_vault as fv


def _index():
    return {"files": {"Photos/a.jpg": {}, "Photos/Kids/b.jpg": {},
                      "Photos/Kids/deep/c.jpg": {},
                      "Photos/own.jpg": {"rule": {"rule_id": "f" * 16, "redundancy": {"copies": 1}}},
                      "Docs/d.pdf": {}},
            "folders": {"Photos": {"rule_id": "1" * 16, "redundancy": {"copies": 3}},
                        "Photos/Kids": {"rule_id": "2" * 16, "redundancy": {"erasure": [4, 2]}}}}


def test_nearest_folder_wins_and_a_file_rule_wins_over_all():
    ix = _index()
    assert rules.effective(ix, "Photos/a.jpg")[1] == "Photos"
    assert rules.effective(ix, "Photos/Kids/b.jpg")[1] == "Photos/Kids"
    assert rules.effective(ix, "Photos/Kids/deep/c.jpg")[1] == "Photos/Kids"
    assert rules.effective(ix, "Photos/own.jpg")[0]["rule_id"] == "f" * 16
    assert rules.effective(ix, "Docs/d.pdf") == (rules.default_rule(), "default")
    assert rules.folder_effective(ix, "Photos/Kids/deep")[1] == "Photos/Kids"
    assert rules.folder_effective(ix, "Photos")[1] == "Photos"
    ix["folders"][""] = {"rule_id": "0" * 16, "redundancy": {"copies": 1}}
    assert rules.effective(ix, "Docs/d.pdf")[1] == ""
    assert sorted(rules.covered(ix, "1" * 16)) == ["Photos/a.jpg"]


def test_ancestors_and_paths():
    assert rules.ancestors("a/b/c.txt") == ["a/b", "a", ""]
    assert rules.ancestors("c.txt") == [""]
    assert rules.clean_folder("/") == "" and rules.clean_folder("a\\b/") == "a/b"
    with pytest.raises(fv.VaultError):
        rules.clean_folder("../x")


def test_parsing_devices_and_erasure():
    assert rules.parse_devices("computers") == ["computer"]
    assert rules.parse_devices("computers,laptops") == ["computer", "laptop"]
    assert rules.parse_devices("any") == [] and rules.parse_devices("computers,laptops,phones") == []
    assert rules.parse_devices("tablets") == ["phone"]
    with pytest.raises(fv.VaultError):
        rules.parse_devices("Photos")
    assert rules.parse_erasure("4+2") == [4, 2]
    for bad in ("4", "1+1", "17+1", "8+9", "x+y"):
        with pytest.raises(fv.VaultError):
            rules.parse_erasure(bad)


def test_placement_params_carry_no_name():
    rule = {"rule_id": "a" * 16, "redundancy": {"copies": 2}, "devices": ["computer"],
            "no_kids": True, "pin": "opti"}
    assert rules.placement_params(rule) == {"replicas": "2", "tier": "warm",
                                            "devices": "computer", "no_kids": "true",
                                            "pin": "opti", "rule": "a" * 16}
    er = {"rule_id": "b" * 16, "redundancy": {"erasure": [2, 1]}, "devices": [], "pin": "x"}
    assert rules.placement_params(er) == {"replicas": "2", "tier": "warm", "ec": "2+1",
                                          "rule": "b" * 16}  # a pin never rides with shards
    assert "computers only" in rules.describe(rule) and "never on kids' devices" in rules.describe(rule)
    assert rules.describe(rules.default_rule()).startswith("the drive's default")


def test_a_file_is_stored_anew_only_when_copies_and_erasure_switch():
    copies = {"rule_id": "a" * 16, "redundancy": {"copies": 3}}
    ec = {"rule_id": "b" * 16, "redundancy": {"erasure": [4, 2]}}
    assert rules.fits({"redundancy": "copies", "replicas": 2}, copies)  # the household re-copies
    assert not rules.fits({"redundancy": "ec 4+2"}, copies)
    assert rules.fits({"redundancy": "ec 4+2"}, ec)
    assert not rules.fits({"redundancy": "ec 2+1"}, ec)
    assert not rules.fits({}, ec)  # an entry from before B10 is copies


def test_a_newer_browser_edit_is_adopted_and_an_older_one_is_not():
    ix = _index()
    ix["folders"]["Photos"]["server_version"] = 3
    served = {"rules": [{"rule_id": "1" * 16, "version": 4, "redundancy": {"copies": 2},
                         "devices": ["computer"], "no_kids": True, "pin": ""},
                        {"rule_id": "f" * 16, "version": 0, "redundancy": {"copies": 3}}]}
    assert rules.adopt_server_changes(ix, served) == ["1" * 16]
    assert ix["folders"]["Photos"]["devices"] == ["computer"]
    assert ix["folders"]["Photos"]["server_version"] == 4
    assert ix["files"]["Photos/own.jpg"]["rule"]["redundancy"] == {"copies": 1}
    assert rules.adopt_server_changes(ix, served) == []  # once
