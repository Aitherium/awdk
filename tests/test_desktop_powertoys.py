import json

import pytest

from adk.desktop.powertoys import PowerToys, PowerToysError


@pytest.fixture
def pt(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps(
        {"run_elevated": False, "enabled": {"FancyZones": True, "Awake": False}}),
        encoding="utf-8")
    (tmp_path / "FancyZones").mkdir()
    (tmp_path / "FancyZones" / "settings.json").write_text(
        '{"name": "FancyZones"}', encoding="utf-8")
    return PowerToys(tmp_path)


def test_reads_modules_and_elevation(pt):
    assert pt.modules() == {"FancyZones": True, "Awake": False}
    assert pt.runs_elevated() is False


def test_set_enabled_round_trips_and_keeps_other_keys(pt):
    assert pt.set_enabled("Awake", True) is False
    data = json.loads(pt.general_path.read_text(encoding="utf-8"))
    assert data["enabled"]["Awake"] is True
    assert data["run_elevated"] is False
    assert not list(pt.root.glob(".aither-*"))  # no temp file left behind


def test_unknown_module_refused_and_file_untouched(pt):
    before = pt.general_path.read_text(encoding="utf-8")
    with pytest.raises(PowerToysError):
        pt.set_enabled("NotAModule", True)
    assert pt.general_path.read_text(encoding="utf-8") == before


def test_missing_install_is_loud(tmp_path):
    with pytest.raises(PowerToysError):
        PowerToys(tmp_path / "nope").modules()


def test_module_settings(pt):
    assert pt.module_settings("FancyZones")["name"] == "FancyZones"
