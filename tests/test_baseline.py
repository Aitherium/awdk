"""``adk baseline``: the ready-to-code table, ``--json`` rows, ``--sync`` exit codes.

Every case goes through the real argparse tree and ``adk.cli.main`` so a missing
subparser or a missing dispatch branch fails HERE, not in a hand-written call to
the module. Nothing touches a network or a real install: ``shutil.which`` and
``subprocess.run`` are replaced with a fixture.

The last test pins that the generated ``adk doctor`` path was not touched --
``adk baseline`` is a NEW verb precisely because ``doctor`` belongs to the
generator (``gen_aw_doctor.py``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from adk import cli

VERSION_TEXT = {
    "git": "git version 2.52.0.windows.1",
    "pwsh": "PowerShell 7.4.6",
    "code": "1.95.0",
    "gh": "gh version 2.62.0 (2024-11-14)",
    "node": "v22.11.0",
    "python": "Python 3.12.7",
    "adk": "adk 3.8.40",
    "aither": "aither 2.4.0",
    "aitherdesk": "aitherdesk 1.3.0",
}


def _install_fake(monkeypatch, present: set[str], versions: dict[str, str] | None = None):
    """Make ``which`` answer only for *present* and ``run`` echo a version."""
    table = dict(VERSION_TEXT if versions is None else versions)

    def which(cmd, *a, **k):
        return f"/usr/bin/{cmd}" if cmd in present else None

    def run(argv, *a, **k):
        name = str(argv[0]).rsplit("/", 1)[-1]
        return subprocess.CompletedProcess(argv, 0, table.get(name, ""), "")

    monkeypatch.setattr("shutil.which", which)
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(cli, "_check_for_updates", lambda: None, raising=False)


def _main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["adk", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return int(exc.value.code or 0)


def test_missing_git_exits_1_and_reports_a_missing_row(monkeypatch, capsys):
    present = set(VERSION_TEXT) - {"git"}
    _install_fake(monkeypatch, present)

    code = _main(monkeypatch, ["baseline", "--json"])

    assert code == 1
    rows = json.loads(capsys.readouterr().out)
    row = next(r for r in rows if r["component"] == "git")
    assert row["version"] is None
    assert row["expected"] == ">=2.40"
    assert row["state"] == "missing"


def test_all_present_exits_0(monkeypatch, capsys):
    _install_fake(monkeypatch, set(VERSION_TEXT))

    assert _main(monkeypatch, ["baseline", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert all(r["state"] == "present" for r in rows)


def test_json_rows_carry_the_five_contract_keys(monkeypatch, capsys):
    _install_fake(monkeypatch, set(VERSION_TEXT))

    _main(monkeypatch, ["baseline", "--json"])

    rows = json.loads(capsys.readouterr().out)
    assert isinstance(rows, list) and rows
    for row in rows:
        assert {"component", "version", "expected", "state", "fix"} <= set(row)


def test_sync_without_a_record_exits_2(monkeypatch, capsys, tmp_path):
    _install_fake(monkeypatch, set(VERSION_TEXT))
    monkeypatch.setenv("AITHER_HOME", str(tmp_path / "aither"))

    code = _main(monkeypatch, ["baseline", "--sync"])

    assert code == 2
    assert "baseline.json" in capsys.readouterr().out


@pytest.mark.parametrize("components", [
    [{"name": "git", "version": "2.52.0"}],          # foreign key: `name`, not `id`
    [None, "git", {"no_name": True}],                # non-dicts / nameless dicts
])
def test_sync_all_unusable_components_exits_2(
    monkeypatch, capsys, tmp_path, components
):
    """A non-empty list where nothing is usable is NO record: 2, never 0.

    Regression: the loop skipped every entry, leaving ``checked`` empty and
    control falling through to the success branch -- exit 0 on silence for a
    malformed or foreign-format record.
    """
    home = tmp_path / "aither"
    home.mkdir()
    (home / "baseline.json").write_text(json.dumps({
        "schema": "aither.baseline/v1",
        "components": components,
    }), encoding="utf-8")
    monkeypatch.setenv("AITHER_HOME", str(home))
    _install_fake(monkeypatch, set(VERSION_TEXT))

    code = _main(monkeypatch, ["baseline", "--sync"])

    assert code == 2, "an unusable record must be could-not-judge, not a match"


def test_sync_one_usable_component_among_junk_is_still_judged(
    monkeypatch, capsys, tmp_path
):
    """The guard must fire only when NOTHING is usable, not before a real row."""
    home = tmp_path / "aither"
    home.mkdir()
    (home / "baseline.json").write_text(json.dumps({
        "schema": "aither.baseline/v1",
        "components": [
            None,
            {"id": "git", "command": "git", "version": "2.52.0",
             "expected": ">=2.40", "state": "present"},
        ],
    }), encoding="utf-8")
    monkeypatch.setenv("AITHER_HOME", str(home))
    _install_fake(monkeypatch, set(VERSION_TEXT))

    assert _main(monkeypatch, ["baseline", "--sync"]) == 0


def test_sync_reports_drift_exit_1(monkeypatch, capsys, tmp_path):
    home = tmp_path / "aither"
    home.mkdir()
    (home / "baseline.json").write_text(json.dumps({
        "schema": "aither.baseline/v1",
        "components": [{"id": "git", "command": "git", "version": "2.52.0",
                        "expected": ">=2.40", "state": "present"}],
    }), encoding="utf-8")
    monkeypatch.setenv("AITHER_HOME", str(home))
    _install_fake(monkeypatch, set(VERSION_TEXT),
                  dict(VERSION_TEXT, git="git version 2.30.0"))

    code = _main(monkeypatch, ["baseline", "--sync", "--json"])

    assert code == 1
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["state"] == "outdated"


def test_sync_on_a_match_exits_0(monkeypatch, capsys, tmp_path):
    home = tmp_path / "aither"
    home.mkdir()
    (home / "baseline.json").write_text(json.dumps({
        "schema": "aither.baseline/v1",
        "components": [{"id": "git", "command": "git", "version": "2.52.0",
                        "expected": ">=2.40", "state": "present"}],
    }), encoding="utf-8")
    monkeypatch.setenv("AITHER_HOME", str(home))
    _install_fake(monkeypatch, set(VERSION_TEXT))

    assert _main(monkeypatch, ["baseline", "--sync"]) == 0


def test_generated_doctor_files_are_untouched():
    """`adk baseline` is its own verb and never reaches into the doctor path.

    ``adk doctor`` is generated; a baseline that imported or edited it would be
    reverted on the next regeneration. The invariant is pinned inside this
    package only: ``baseline.py`` imports no doctor module, and ``cli.py``
    dispatches ``baseline`` to ``adk.baseline``.
    """
    root = Path(__file__).resolve().parents[1]

    baseline_text = (root / "adk" / "baseline.py").read_text(encoding="utf-8")
    assert "from adk.doctor" not in baseline_text
    assert "from ._doctor" not in baseline_text
    assert "import _doctor" not in baseline_text

    cli_text = (root / "adk" / "cli.py").read_text(encoding="utf-8")
    assert 'elif args.command == "baseline":' in cli_text
    assert "from adk.baseline import main as _baseline_main" in cli_text
