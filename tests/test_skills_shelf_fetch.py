"""`adk skills install` gets the public shelf itself, and a kit agent can use the result.

Measured 2026-11-12: ``adk skills install local-research --target project`` failed
until the tester cloned Aitherium/awskills by hand and set ``AWSKILLS_DIR``; the
file then landed in ``.claude/skills``, a directory the kit agent's
``skill_search`` / ``skill_load`` did not scan.
"""
from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path

import pytest

from adk import addons, pack_activation

SKILL = "---\nname: local-research\ndescription: Hand exploration to the local fleet.\n---\n\n# Steps\n1. ask\n"


def _shelf_tarball(extra=()) -> bytes:
    buf = io.BytesIO()
    members = [("awskills-main/skills/local-research.md", SKILL.encode()),
               ("awskills-main/skills/README.md", b"# shelf\n"),
               ("awskills-main/README.md", b"top\n"),
               ("awskills-main/skills/local-research/SKILL.md", b"nested copy\n"),
               *extra]
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture()
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(project)
    monkeypatch.delenv("AWSKILLS_DIR", raising=False)
    monkeypatch.delenv("AWSKILLS_NO_FETCH", raising=False)
    monkeypatch.delenv("AITHER_SKILL_DIRS", raising=False)
    # No checkout beside the kit or above cwd: the outside user's machine.
    monkeypatch.setattr(addons, "_local_skills_shelf", lambda: (
        (home / ".aither" / "awskills" / "skills")
        if (home / ".aither" / "awskills" / "skills").is_dir() else None))
    monkeypatch.setattr("adk.addon_lock.lock_path", lambda: home / ".aither" / "addons.lock.json",
                        raising=False)
    fetched = []
    monkeypatch.setattr(addons, "_download_shelf",
                        lambda url: fetched.append(url) or _shelf_tarball())
    return home, project, fetched


def test_install_fetches_the_shelf_when_none_is_on_disk(box):
    home, project, fetched = box
    res = addons.install_skill("local-research", target="project", cwd=project)
    assert res.ok, res.detail
    assert fetched == [addons.AWSKILLS_TARBALL]
    dest = project / ".claude" / "skills" / "local-research" / "SKILL.md"
    assert dest.read_text(encoding="utf-8") == SKILL
    shelf = home / ".aither" / "awskills" / "skills"
    assert sorted(p.name for p in shelf.iterdir()) == ["README.md", "local-research.md"]
    marker = json.loads((home / ".aither" / "awskills" / addons.SHELF_MARKER).read_text())
    assert marker["skills"] == 2 and marker["source"] == addons.AWSKILLS_TARBALL


def test_the_install_output_says_who_can_use_it(box):
    _home, project, _fetched = box
    res = addons.install_skill("local-research", target="project", cwd=project)
    assert "Claude Code in this project" in res.next
    assert 'skill_load("local-research")' in res.next
    assert "kit agents only" in addons.skill_usage_note("x", "adk")


def test_a_kit_agent_finds_and_loads_a_skill_installed_to_any_target(box):
    _home, project, _fetched = box
    from adk import builtin_tools

    for target in ("project", "claude", "adk"):
        res = addons.install_skill("local-research", target=target, cwd=project,
                                   replace=True)
        if not res.ok and "exists" in res.detail:
            continue
        assert res.ok, (target, res.detail)
        dest = addons.skill_targets(project)[target]
        assert dest in pack_activation._skill_search_dirs(), target
        hits = json.loads(builtin_tools._skill_search_sync("local research fleet", 5))
        assert "local-research" in [h["name"] for h in hits["results"]], target
        assert "# Steps" in builtin_tools._skill_load_sync("local-research"), target
        addons.remove_skill("local-research")


def test_a_fetched_shelf_older_than_a_day_is_refreshed_and_a_checkout_never_is(box):
    home, project, fetched = box
    assert addons.find_skills_shelf() is not None and len(fetched) == 1
    assert addons.find_skills_shelf() is not None and len(fetched) == 1, "fresh: no refetch"
    marker = home / ".aither" / "awskills" / addons.SHELF_MARKER
    marker.write_text(json.dumps({"fetched_at": time.time() - 3 * 86400}), encoding="utf-8")
    shelf = addons.find_skills_shelf()
    assert len(fetched) == 2
    assert addons.shelf_age_s(shelf) < 60
    assert "fetched 0h ago" in addons._shelf_source(shelf)

    marker.unlink()  # an owner-managed shelf (no marker): never refetched, no age
    assert addons.shelf_age_s(shelf) is None
    addons.find_skills_shelf()
    assert len(fetched) == 2


def test_a_failed_fetch_says_exactly_what_is_needed(box, monkeypatch):
    _home, project, _fetched = box

    def offline(url):
        raise OSError("no route to host")

    monkeypatch.setattr(addons, "_download_shelf", offline)
    res = addons.install_skill("local-research", target="project", cwd=project)
    assert not res.ok
    assert "no route to host" in res.detail
    assert "AWSKILLS_DIR" in res.detail and "github.com/Aitherium/awskills" in res.detail

    monkeypatch.setenv("AWSKILLS_NO_FETCH", "1")
    assert addons.fetch_skills_shelf()[1].startswith("fetching is disabled")


def test_the_archive_cannot_write_outside_the_shelf(box, monkeypatch):
    home, _project, _fetched = box
    evil = [("awskills-main/skills/../../../escape.md", b"x"),
            ("awskills-main/skills/bad name!.md", b"x"),
            ("awskills-main/skills/big.md", b"x" * (addons._SHELF_MAX_FILE + 1))]
    monkeypatch.setattr(addons, "_download_shelf", lambda url: _shelf_tarball(evil))
    shelf, why = addons.fetch_skills_shelf()
    assert why == "" and shelf is not None
    assert sorted(p.name for p in shelf.iterdir()) == ["README.md", "local-research.md"]
    assert not list(home.rglob("escape.md"))
