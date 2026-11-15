"""adk.lessons: a lesson needs evidence, must pass its replay check, and stays local.

The worked example throughout is the one that started this: a company uploads a
spreadsheet whose columns are not named the way the importer expects. The first
upload needs a person to say which column is which. That confirmation is the
lesson. The second file from the same company, columns in another order, must map
without asking; switching the lesson off must bring the question back.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import yaml

from adk import lessons
from adk.memory import Memory

REQUIRED = ["name", "certification", "expires"]
FIRST_SHEET = ["Emp Name", "Cert", "Exp Date", "Dept"]
SECOND_SHEET = ["Exp Date", "Location", "CERT", "emp name"]  # other order, case, extra
CONFIRMED = {"Emp Name": "name", "Cert": "certification", "Exp Date": "expires"}

ACME = lessons.Scope("tnt_acme", "default", "u_admin")
ACME_MEMBER = lessons.Scope("tnt_acme", "default", "u_member")
ACME_OTHER_WS = lessons.Scope("tnt_acme", "field-office", "u_admin")
OTHER = lessons.Scope("tnt_other", "default", "u_admin")


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return lessons.LessonStore(tmp_path / "lessons.db", clock=clock)


async def _teach_mapping(store, scope=ACME, *, admin=True, headers=FIRST_SHEET,
                         mapping=None, target="roster"):
    return await store.capture_confirmed_mapping(
        scope, target=target, headers=headers, mapping=mapping or CONFIRMED,
        required=REQUIRED, confirmed_by=scope.user_id, confirmed_by_admin=admin,
        source_turn="upload:first-roster")


# ── the worked example ──────────────────────────────────────────────────────


async def test_first_upload_has_to_ask(store):
    """No lesson yet: exact column names only, so a person must be asked."""
    mapping, missing, used = lessons.map_columns(FIRST_SHEET, REQUIRED)
    assert mapping == {} and missing == REQUIRED and used == []
    assert await store.find_mapping(
        ACME, target="roster", headers=FIRST_SHEET, required=REQUIRED) is None


async def test_confirmed_mapping_is_kept_with_a_before_and_after(store):
    result = await _teach_mapping(store)

    assert result.kept is True
    lesson = result.lesson
    assert lesson.kind == "memory" and lesson.status == "active"
    assert lesson.evidence["type"] == "confirmed_mapping"
    assert lesson.evidence["source_turn"] == "upload:first-roster"
    assert lesson.evidence["confirmed_by"] == "u_admin"
    # The replay check: the triggering sheet resolved 0 of 3 fields without the
    # lesson and 3 of 3 with it. That number is why the lesson exists.
    assert lesson.replay["before"] == 0.0
    assert lesson.replay["after"] == 1.0
    assert lesson.replay["passed"] is True
    assert lesson.replay["case"]["type"] == "sheet-columns"


async def test_second_differently_ordered_file_maps_without_asking(store):
    await _teach_mapping(store)

    mapping = await store.find_mapping(
        ACME, target="roster", headers=SECOND_SHEET, required=REQUIRED)

    assert mapping == {"name": "emp name", "certification": "CERT", "expires": "Exp Date"}
    lesson = (await store.list(ACME))[0]
    assert lesson.used_count == 1 and lesson.helped_count == 1
    assert lesson.last_helped > 0


async def test_turning_the_lesson_off_restores_asking(store):
    lesson = (await _teach_mapping(store)).lesson
    ask = dict(target="roster", headers=SECOND_SHEET, required=REQUIRED)
    assert await store.find_mapping(ACME, **ask) is not None

    await store.set_enabled(ACME, lesson.id, False, by="u_admin")
    assert await store.find_mapping(ACME, **ask) is None, "an OFF lesson must not be applied"

    await store.set_enabled(ACME, lesson.id, True, by="u_admin")
    assert await store.find_mapping(ACME, **ask) is not None


async def test_a_new_alias_extends_the_same_lesson(store):
    first = (await _teach_mapping(store)).lesson
    third_sheet = ["Employee", "Cert", "Exp Date"]
    assert await store.find_mapping(
        ACME, target="roster", headers=third_sheet, required=REQUIRED) is None

    again = await _teach_mapping(store, headers=third_sheet, mapping={"Employee": "name"})

    assert again.kept and again.lesson.id == first.id, "one lesson per situation, not a pile"
    assert again.lesson.version == 2
    assert set(again.lesson.data["aliases"]) == {"emp name", "cert", "exp date", "employee"}
    assert "'emp name' is name" in again.lesson.body and "'employee' is name" in again.lesson.body
    assert again.lesson.used_count == first.used_count
    assert len(await store.list(ACME)) == 1


async def test_a_members_new_alias_never_changes_the_lesson_in_use(store):
    """Ask mode: what an admin approved stays in use, untouched, while a member's
    addition waits beside it. Approving folds it in; one undo takes it back out."""
    approved = (await _teach_mapping(store)).lesson
    third_sheet = ["Employee", "Cert", "Exp Date"]
    ask_second = dict(target="roster", headers=SECOND_SHEET, required=REQUIRED)
    ask_third = dict(target="roster", headers=third_sheet, required=REQUIRED)

    result = await _teach_mapping(store, ACME_MEMBER, admin=False, headers=third_sheet,
                                  mapping={"Employee": "name"})

    assert result.kept and result.lesson.status == "proposed"
    assert result.lesson.id != approved.id
    assert result.lesson.view()["extends"] == approved.id
    still = await store.get(ACME, approved.id)
    assert still.status == "active" and still.version == approved.version
    assert set(still.data["aliases"]) == {"emp name", "cert", "exp date"}
    assert await store.find_mapping(ACME_MEMBER, **ask_second) is not None, "still in use"
    assert await store.find_mapping(ACME_MEMBER, **ask_third) is None, "not applied yet"
    assert (await store.page(ACME, all_users=True))["waiting"] == 1

    # A second member addition lands in the same waiting proposal, not a third lesson.
    await _teach_mapping(store, ACME_MEMBER, admin=False, headers=["Staff", "Cert", "Exp Date"],
                         mapping={"Staff": "name"})
    assert len(await store.list(ACME, all_users=True)) == 2

    merged = await store.approve(ACME, result.lesson.id, by="u_admin")
    assert merged.id == approved.id and merged.status == "active"
    assert {"employee", "staff"} <= set(merged.data["aliases"])
    assert "extends" not in merged.data
    assert len(await store.list(ACME, all_users=True)) == 1, "the proposal was folded in"
    assert await store.find_mapping(ACME_MEMBER, **ask_third) is not None

    undone = await store.undo(ACME, approved.id, "u_admin")
    assert set(undone.data["aliases"]) == {"emp name", "cert", "exp date"}


async def test_a_member_cannot_bring_back_a_lesson_an_admin_switched_off(store):
    await store.update_settings(ACME, "u_admin", mode="auto")
    lesson = (await _teach_mapping(store)).lesson
    await store.set_enabled(ACME, lesson.id, False, by="u_admin")

    result = await _teach_mapping(store, ACME_MEMBER, admin=False,
                                  headers=["Employee", "Cert", "Exp Date"],
                                  mapping={"Employee": "name"})

    assert result.kept is False and "switched this lesson off" in result.reason
    after = await store.get(ACME, lesson.id)
    assert after.status == "off" and "employee" not in after.data["aliases"]

    again = await _teach_mapping(store, headers=["Employee", "Cert", "Exp Date"],
                                 mapping={"Employee": "name"})
    assert again.kept and again.lesson.status == "active", "an admin's own teaching does"


# ── a wrong column is worse than a question ─────────────────────────────────


def _aliases(**pairs):
    return [lessons.Lesson(id="ls_t", kind="memory", title="t", body="b", tenant_id="tnt_acme",
                           data={"aliases": pairs})]


def test_a_confirmed_column_outranks_a_column_that_merely_has_the_fields_name():
    """The sheet calls the person 'Employee' and uses 'Name' for the certificate."""
    taught = _aliases(employee="name", name="certification")
    mapping, missing, used = lessons.map_columns(
        ["Employee", "Name", "Expires"], REQUIRED, taught)
    assert mapping == {"name": "Employee", "certification": "Name", "expires": "Expires"}
    assert missing == [] and used == ["ls_t"]
    assert len(set(mapping.values())) == len(mapping), "one column never fills two fields"


@pytest.mark.parametrize("headers, taught, still_missing", [
    # two headers that are the same name once case and punctuation are ignored
    (["Emp Name", "Cert", "Exp Date", "Exp. Date"],
     {"emp name": "name", "cert": "certification", "exp date": "expires"}, ["expires"]),
    # two columns of this sheet were both taught as the same field
    (["Emp Name", "Employee", "Cert", "Exp Date"],
     {"emp name": "name", "employee": "name", "cert": "certification",
      "exp date": "expires"}, ["name"]),
    # the taught column next to a free column with the field's own name
    (["Cert", "Certification", "Name", "Expires"], {"cert": "certification"},
     ["certification"]),
    # a column a person assigned to one field is not also used for the field it is
    # named after
    (["Name", "Expires"], {"name": "certification"}, ["name"]),
])
def test_an_ambiguous_sheet_is_a_question_not_a_guess(headers, taught, still_missing):
    mapping, missing, _used = lessons.map_columns(headers, REQUIRED, _aliases(**taught))
    assert missing == still_missing
    assert len(set(mapping.values())) == len(mapping)


def test_two_lessons_that_disagree_about_a_column_are_a_question():
    one = lessons.Lesson(id="ls_1", kind="memory", title="t", body="b", tenant_id="tnt_acme",
                         data={"aliases": {"code": "name"}})
    two = lessons.Lesson(id="ls_2", kind="memory", title="t", body="b", tenant_id="tnt_acme",
                         data={"aliases": {"code": "certification"}})
    mapping, missing, used = lessons.map_columns(["Code", "Expires"], REQUIRED, [one, two])
    assert mapping == {"expires": "Expires"} and used == []
    assert missing == ["name", "certification"]


def test_a_blank_header_or_a_field_listed_twice_changes_nothing():
    taught = _aliases(**{"emp name": "name", "": "certification", "  ": "expires"})
    mapping, missing, _used = lessons.map_columns(
        ["Emp Name", "", " -- ", "Expires"], ["name", "name", "certification", "expires"], taught)
    assert mapping == {"name": "Emp Name", "expires": "Expires"}
    assert missing == ["certification"], "a column with no name is never matched"


def test_headers_in_any_script_keep_their_own_names():
    """"Имя 1" and "Отдел 1" are different columns, and so are "Größe" and "Grüße"."""
    assert lessons.normalize_header(" Emp. Name ") == "emp name"
    assert lessons.normalize_header("Имя 1") == "имя 1"
    assert lessons.normalize_header("Имя 1") != lessons.normalize_header("Отдел 1")
    assert lessons.normalize_header("Größe") != lessons.normalize_header("Grüße")
    assert lessons.normalize_header("ＥＭＰ　ＮＡＭＥ") == "emp name", "full-width forms fold"
    assert lessons.normalize_header(" -- ") == ""
    # Marks belong to their letters: two Hindi words, two Thai words, and a German
    # pair that case-folding would merge.
    assert lessons.normalize_header("दिन") != lessons.normalize_header("दान")
    assert lessons.normalize_header("ชื่อ") != lessons.normalize_header("ช่อ")
    assert lessons.normalize_header("Maße") != lessons.normalize_header("Masse")
    assert lessons.normalize_header("NAME") == lessons.normalize_header("name")
    hindi = _aliases(**{lessons.normalize_header("दिन"): "expires"})
    assert lessons.map_columns(["दान"], ["expires"], hindi) == ({}, ["expires"], [])
    assert lessons.map_columns(["दिन"], ["expires"], hindi)[0] == {"expires": "दिन"}

    taught = _aliases(**{lessons.normalize_header("Имя 1"): "name"})
    mapping, missing, _used = lessons.map_columns(
        ["Отдел 1", "Certification", "Expires"], REQUIRED, taught)
    assert "name" in missing and "Отдел 1" not in mapping.values()
    mapping, missing, _used = lessons.map_columns(
        ["Отдел 1", "ИМЯ 1", "Certification", "Expires"], REQUIRED, taught)
    assert mapping["name"] == "ИМЯ 1" and missing == []


async def test_a_sheet_in_another_script_is_learned_and_reused(store):
    first = ["Сотрудник", "Сертификат", "Срок"]
    taught = await _teach_mapping(
        store, headers=first,
        mapping={"Сотрудник": "name", "Сертификат": "certification", "Срок": "expires"})
    assert taught.kept and taught.lesson.replay["after"] == 1.0
    assert await store.find_mapping(
        ACME, target="roster", headers=["срок", "Отдел", "СЕРТИФИКАТ", "сотрудник"],
        required=REQUIRED) == {"name": "сотрудник", "certification": "СЕРТИФИКАТ",
                               "expires": "срок"}
    assert "сотрудник" in await store.context_block(ACME, "таблицы roster сотрудник срок")


# ── rule 1: evidence ────────────────────────────────────────────────────────


@pytest.mark.parametrize("evidence_type, source_turn", [
    ("", "turn-1"), ("i_think_so", "turn-1"), ("user_correction", ""),
])
async def test_no_evidence_no_lesson(store, evidence_type, source_turn):
    with pytest.raises(lessons.NoEvidenceError):
        await store.capture(
            ACME, kind="memory", title="The office is in Hartford",
            body="Headquarters is in Hartford.", evidence_type=evidence_type,
            source_turn=source_turn)
    assert await store.list(ACME, all_users=True) == []


# ── rule 2: the replay check ────────────────────────────────────────────────


async def test_a_lesson_that_changes_nothing_is_not_kept(store):
    result = await store.capture(
        ACME, kind="memory", title="Sheets have columns", body="Sheets have columns.",
        evidence_type="user_correction", source_turn="turn-9",
        confirmed_by="u_admin", confirmed_by_admin=True,
        run=lambda lesson: 0.5)  # the same score with and without the lesson

    assert result.kept is False
    assert "did not improve" in result.reason
    assert await store.list(ACME, all_users=True) == []


async def test_a_sheet_with_the_expected_names_teaches_nothing(store):
    result = await store.capture_confirmed_mapping(
        ACME, target="roster", headers=["Name", "Certification", "Expires"],
        mapping={"Name": "name", "Certification": "certification", "Expires": "expires"},
        required=REQUIRED, confirmed_by="u_admin", confirmed_by_admin=True,
        source_turn="upload:clean")
    assert result.kept is False and "nothing to learn" in result.reason


async def test_replay_check_accepts_an_async_runner():
    lesson = lessons.Lesson(id="ls_x", kind="skill", title="t", body="b", tenant_id="tnt_acme")

    async def run(candidate):
        return 0.9 if candidate is not None else 0.2

    result = await lessons.replay_check(lesson, run)
    assert (result["before"], result["after"], result["passed"]) == (0.2, 0.9, True)


@pytest.mark.parametrize("broken", [float("nan"), float("inf")])
async def test_a_broken_grader_never_reads_as_a_perfect_score(store, broken):
    result = await store.capture(
        ACME, kind="memory", title="A fact", body="A fact that a broken grader scored.",
        evidence_type="user_correction", source_turn="turn-4",
        confirmed_by="u_admin", confirmed_by_admin=True,
        run=lambda lesson: broken if lesson is not None else 0.0)
    assert result.kept is False
    assert (result.lesson.replay["before"], result.lesson.replay["after"]) == (0.0, 0.0)


async def test_a_lesson_that_stops_helping_is_retired_by_consolidation(store):
    lesson = (await _teach_mapping(store)).lesson
    # Someone edits the lesson so it no longer covers the sheet it was learned from.
    await store.edit(ACME, lesson.id, "u_admin", data={"aliases": {}, "target": "roster"})

    report = await store.consolidate(ACME)

    assert report == {"ran": True, "checked": 1, "retired": 1, "failed": 0}
    after = await store.get(ACME, lesson.id)
    assert after.status == "retired"
    assert "stopped improving" in after.retired_reason
    assert await store.find_mapping(
        ACME, target="roster", headers=SECOND_SHEET, required=REQUIRED) is None


async def test_one_broken_replay_case_does_not_stop_the_other_checks(store, monkeypatch):
    await store.update_settings(ACME, "u_admin", mode="auto")
    good = (await _teach_mapping(store)).lesson
    broken = (await _teach_mapping(store, target="training-log")).lesson
    recheck = store.recheck

    async def recheck_with_one_broken_case(scope, lesson_id, run=None):
        if lesson_id == broken.id:
            raise RuntimeError("the grader for this case is down")
        return await recheck(scope, lesson_id, run)

    monkeypatch.setattr(store, "recheck", recheck_with_one_broken_case)
    report = await store.consolidate(ACME)

    assert report == {"ran": True, "checked": 1, "retired": 0, "failed": 1}
    assert (await store.get(ACME, broken.id)).status == "active", "not retired on an error"
    assert (await store.get(ACME, good.id)).replay["runs"] == 2


async def test_three_unhelpful_uses_in_a_row_retire_a_lesson(store):
    lesson = (await _teach_mapping(store)).lesson
    for _ in range(lessons.RETIRE_AFTER_UNHELPFUL - 1):
        await store.record_use(ACME, lesson.id, helped=False)
    assert (await store.get(ACME, lesson.id)).status == "active"
    await store.record_use(ACME, lesson.id, helped=True)  # a help resets the streak
    for _ in range(lessons.RETIRE_AFTER_UNHELPFUL):
        await store.record_use(ACME, lesson.id, helped=False)

    retired = await store.get(ACME, lesson.id)
    assert retired.status == "retired" and "did not help" in retired.retired_reason
    assert await store.active(ACME) == []


async def test_nightly_consolidation_runs_once_per_night(store, clock):
    await _teach_mapping(store)
    await store.update_settings(ACME, "u_admin", consolidate="nightly")

    assert (await store.consolidate(ACME))["ran"] is True
    clock.tick(3600)
    skipped = await store.consolidate(ACME)
    assert skipped["ran"] is False and skipped["next_due_in_s"] == 86400 - 3600
    clock.tick(86400)
    assert (await store.consolidate(ACME))["ran"] is True


async def test_a_clock_set_back_does_not_stop_the_nightly_check(store, clock):
    await _teach_mapping(store)
    await store.update_settings(ACME, "u_admin", consolidate="nightly")
    assert (await store.consolidate(ACME))["ran"] is True
    clock.tick(-30 * 86400)
    assert (await store.consolidate(ACME))["ran"] is True


async def test_a_recheck_never_writes_the_settings_back(store, monkeypatch):
    """An admin switches learning off while a re-check is running: it stays off."""
    await _teach_mapping(store)
    recheck = store.recheck

    async def recheck_while_the_admin_changes_a_setting(scope, lesson_id, run=None):
        await store.update_settings(ACME, "u_admin", mode="off", consolidate="weekly")
        return await recheck(scope, lesson_id, run)

    monkeypatch.setattr(store, "recheck", recheck_while_the_admin_changes_a_setting)
    assert (await store.consolidate(ACME))["checked"] == 1

    cfg = await store.settings(ACME)
    assert (cfg.mode, cfg.consolidate) == ("off", "weekly")
    assert (await store.page(ACME))["last_checked"] is not None


# ── the admin's controls ────────────────────────────────────────────────────


async def test_ask_mode_holds_an_unconfirmed_lesson_for_the_admin(store):
    result = await _teach_mapping(store, ACME_MEMBER, admin=False)

    assert result.kept and result.lesson.status == "proposed"
    assert await store.find_mapping(
        ACME_MEMBER, target="roster", headers=SECOND_SHEET, required=REQUIRED) is None
    page = await store.page(ACME, all_users=True)
    assert page["next_action"] == "1 lesson waiting for your OK"
    assert page["waiting"] == 1

    approved = await store.approve(ACME, result.lesson.id, by="u_admin")
    assert approved.status == "active" and approved.evidence["confirmed_by"] == "u_admin"
    assert await store.find_mapping(
        ACME_MEMBER, target="roster", headers=SECOND_SHEET, required=REQUIRED) is not None
    assert (await store.page(ACME))["next_action"] == "1 lesson in use. Nothing needs your OK."
    # The proposal became THE lesson for this situation: one record, under the id an
    # admin's own teaching would have used, and the waiting slot is empty again.
    assert approved.id != result.lesson.id and "extends" not in approved.data
    assert await store.get(ACME, result.lesson.id) is None
    assert [x.id for x in await store.list(ACME, all_users=True)] == [approved.id]
    again = await _teach_mapping(store, headers=["Employee", "Cert", "Exp Date"],
                                 mapping={"Employee": "name"})
    assert again.lesson.id == approved.id


async def test_what_a_member_proposed_never_rides_in_on_an_admins_own_teaching(store):
    """A member's unapproved (here: wrong) column must not go in use because an admin
    later confirmed something else for the same kind of sheet."""
    wrong = await _teach_mapping(store, ACME_MEMBER, admin=False,
                                 headers=["Employee", "Cert", "Exp Date"],
                                 mapping={"Employee": "certification"})
    assert wrong.lesson.status == "proposed"

    mine = await _teach_mapping(store)  # the admin, FIRST_SHEET

    assert mine.lesson.status == "active" and mine.lesson.id != wrong.lesson.id
    assert "employee" not in mine.lesson.data["aliases"]
    mapping, missing, _used = lessons.map_columns(
        ["Employee", "Emp Name", "Exp Date"], REQUIRED, await store.active(ACME))
    assert mapping == {"name": "Emp Name", "expires": "Exp Date"}
    assert missing == ["certification"], "the member's column is still only a proposal"
    waiting = await store.get(ACME, wrong.lesson.id)
    assert waiting.status == "proposed" and waiting.view()["extends"] == mine.lesson.id


async def test_a_waiting_lesson_is_approved_or_forgotten_never_switched(store):
    """On/off on a proposal would put it in use (or park it) without an approval."""
    waiting = (await _teach_mapping(store, ACME_MEMBER, admin=False)).lesson
    for on in (True, False):
        with pytest.raises(lessons.LessonError, match="approve it or forget it"):
            await store.set_enabled(ACME, waiting.id, on, by="u_admin")
    assert (await store.get(ACME, waiting.id)).status == "proposed"
    assert await store.forget(ACME, waiting.id, by="u_admin") is True
    assert await store.active(ACME) == []


async def test_a_proposal_cannot_be_aimed_at_another_lesson(store):
    """``extends`` is the store's own field: not settable by a capture or an edit."""
    victim = (await _teach_skill(store)).lesson
    forged = await store.capture(
        ACME, kind="skill", title="Another skill", body="Another body.",
        evidence_type="imported", source_turn="skill.md:another",
        data={"extends": victim.id, "steps": ["x"]})
    assert forged.lesson.status == "proposed" and "extends" not in forged.lesson.data

    edited = await store.edit(ACME, forged.lesson.id, "u_admin",
                              data={"extends": victim.id, "steps": ["y"]})
    assert "extends" not in edited.data
    approved = await store.approve(ACME, forged.lesson.id, by="u_admin")
    assert approved.id == forged.lesson.id, "approved where it is, not folded anywhere"
    untouched = await store.get(ACME, victim.id)
    assert untouched.title == "Run a certification audit" and untouched.version == 1

    # ...and a real proposal keeps its own target through an edit.
    await store.update_settings(ACME, "u_admin", mode="ask")
    lesson = (await _teach_mapping(store)).lesson
    addition = (await _teach_mapping(store, ACME_MEMBER, admin=False,
                                     headers=["Employee", "Cert", "Exp Date"],
                                     mapping={"Employee": "name"})).lesson
    kept = await store.edit(ACME, addition.id, "u_admin",
                            data={"aliases": {"employee": "name"}, "extends": victim.id})
    assert kept.data["extends"] == lesson.id


async def test_data_that_would_break_applying_the_lesson_is_refused(store):
    lesson = (await _teach_mapping(store)).lesson
    for bad in ({"aliases": "x"}, {"aliases": {"emp name": 3}}, {"aliases": ["emp name"]}):
        with pytest.raises(lessons.LessonError, match="aliases"):
            await store.edit(ACME, lesson.id, "u_admin", data=bad)
    with pytest.raises(lessons.LessonError, match="aliases"):
        await store.capture(ACME, kind="memory", title="t", body="b", data={"aliases": "x"},
                            evidence_type="user_correction", source_turn="turn-1")
    assert (await store.get(ACME, lesson.id)).version == 1
    # A record that is malformed anyway (written by other code) is skipped, not fatal.
    odd = lessons.Lesson(id="ls_odd", kind="memory", title="t", body="b",
                         tenant_id="tnt_acme", data={"aliases": "x"})
    assert lessons.map_columns(["Name"], ["name"], [odd]) == ({"name": "Name"}, [], [])


# ── a slow check never undoes what happened while it ran ────────────────────


async def test_a_recheck_cannot_undo_an_off_that_landed_while_it_ran(store):
    lesson = (await _teach_mapping(store)).lesson
    calls = []

    async def slow_run(candidate):
        calls.append(candidate is not None)
        if len(calls) == 1:  # the admin switches it off in the middle of the first run
            await store.set_enabled(ACME, lesson.id, False, by="u_admin")
        return 1.0 if candidate is not None else 0.0

    after = await store.recheck(ACME, lesson.id, run=slow_run)

    assert after.status == "off", "the admin's off survived the re-check"
    assert (await store.get(ACME, lesson.id)).status == "off"
    assert len(calls) == 4, "the stale result was thrown away and the check ran again"
    assert await store.find_mapping(
        ACME, target="roster", headers=SECOND_SHEET, required=REQUIRED) is None


async def test_nothing_read_before_a_forget_can_be_saved_after_it(store):
    lesson = (await _teach_mapping(store)).lesson

    async def forget_during_the_run(candidate):
        await store.forget(ACME, lesson.id, by="u_admin")
        return 1.0 if candidate is not None else 0.0

    assert await store.recheck(ACME, lesson.id, run=forget_during_the_run) is None
    assert await store.get(ACME, lesson.id) is None
    assert await store.list(ACME, all_users=True) == []

    stale = (await _teach_mapping(store)).lesson  # learned again: a new record
    copy = lessons.Lesson.from_dict(stale.to_dict())
    await store.forget(ACME, stale.id, by="u_admin")
    with pytest.raises(lessons._StaleWriteError):
        await store._save(copy)
    assert await store.get(ACME, stale.id) is None, "forgotten stays forgotten"
    assert await store.forgotten(ACME) == [stale.id]


async def test_counting_uses_never_loses_a_count_or_an_off(store):
    """Eight requests apply the lesson while an admin switches it off: every use is
    counted, the off stands, and nobody's request fails."""
    import asyncio

    lesson = (await _teach_mapping(store)).lesson
    ask = dict(target="roster", headers=SECOND_SHEET, required=REQUIRED)

    results = await asyncio.gather(
        *[store.find_mapping(ACME, **ask) for _ in range(4)],
        store.set_enabled(ACME, lesson.id, False, by="u_admin"),
        *[store.record_use(ACME, lesson.id, helped=True) for _ in range(4)],
    )

    assert not any(isinstance(r, Exception) for r in results)
    after = await store.get(ACME, lesson.id)
    assert after.status == "off", "a count never switches a lesson back on"
    applied = sum(1 for r in results[:4] if r is not None)
    assert after.used_count == 4 + applied and after.helped_count == after.used_count


async def test_counting_a_use_does_not_disturb_a_check_or_an_edit_under_way(store):
    lesson = (await _teach_mapping(store)).lesson
    calls = []

    async def run_while_the_lesson_is_being_used(candidate):
        calls.append(candidate is not None)
        await store.record_use(ACME, lesson.id, helped=True)
        return 1.0 if candidate is not None else 0.0

    after = await store.recheck(ACME, lesson.id, run=run_while_the_lesson_is_being_used)

    assert len(calls) == 2, "the check ran once: counting does not make it stale"
    assert after.used_count == 2 and after.helped_count == 2, "and no count was lost"
    assert after.rev == lesson.rev + 1, "one content write: the re-check itself"

    held = await store.get(ACME, lesson.id)  # an admin opens the lesson to edit it...
    await store.record_use(ACME, lesson.id, helped=False)  # ...it is used meanwhile...
    held.title = "Roster column names"
    saved = await store._save(held)  # ...and the edit still lands, with the count
    assert saved.title == "Roster column names" and saved.used_count == 3
    assert saved.unhelpful_streak == 1


async def test_a_use_is_not_counted_for_another_tenant(store):
    lesson = (await _teach_mapping(store)).lesson
    assert await store.record_use(OTHER, lesson.id, helped=False) is None
    assert await store.record_use(ACME, "not-a-lesson-id") is None
    assert (await store.get(ACME, lesson.id)).used_count == 0


async def test_a_write_transaction_really_is_one(store):
    """``transact`` holds the write lock from its first statement: a read-then-write
    inside it cannot interleave with another writer."""
    assert await store._mem.transact(lambda conn: conn.in_transaction) is True
    assert await store._mem._db(lambda conn: conn.in_transaction) is False

    def fail_after_writing(conn):
        lessons.LocalOnlyMemory.put(conn, "ls_rolled_back", "x", "fact", None, 0.0)
        raise RuntimeError("the second half of the transaction failed")

    with pytest.raises(RuntimeError):
        await store._mem.transact(fail_after_writing)
    assert await store._mem.get_entry("ls_rolled_back") is None, "all or nothing"


async def test_a_proposal_forgotten_during_its_approval_is_not_approved(store, monkeypatch):
    """Admin one clicks OK, admin two clicks Forget at the same moment: the forgotten
    text must not end up in the lesson that is in use."""
    lesson = (await _teach_mapping(store)).lesson
    addition = (await _teach_mapping(store, ACME_MEMBER, admin=False,
                                     headers=["Employee", "Cert", "Exp Date"],
                                     mapping={"Employee": "name"})).lesson
    mutable = store._mutable

    async def read_then_someone_forgets_it(scope, lesson_id):
        found = await mutable(scope, lesson_id)
        monkeypatch.setattr(store, "_mutable", mutable)
        await store.forget(ACME, addition.id, by="u_other_admin")
        return found

    monkeypatch.setattr(store, "_mutable", read_then_someone_forgets_it)
    with pytest.raises(lessons.LessonNotFoundError):
        await store.approve(ACME, addition.id, by="u_admin")

    in_use = await store.get(ACME, lesson.id)
    assert "employee" not in in_use.data["aliases"] and in_use.version == lesson.version
    assert await store.forgotten(ACME) == [addition.id]


async def test_an_approval_folds_in_what_the_proposal_says_now(store, monkeypatch):
    """A member adds to the proposal while the admin's OK is under way: the OK
    applies to the proposal as it now is, nothing a member was told is "kept" is lost."""
    lesson = (await _teach_mapping(store)).lesson
    first = (await _teach_mapping(store, ACME_MEMBER, admin=False,
                                  headers=["Employee", "Cert", "Exp Date"],
                                  mapping={"Employee": "name"})).lesson
    mutable = store._mutable

    async def read_then_a_member_adds_more(scope, lesson_id):
        found = await mutable(scope, lesson_id)
        monkeypatch.setattr(store, "_mutable", mutable)
        more = await _teach_mapping(store, ACME_MEMBER, admin=False,
                                    headers=["Staff", "Cert", "Exp Date"],
                                    mapping={"Staff": "name"})
        assert more.kept and more.lesson.id == first.id
        return found

    monkeypatch.setattr(store, "_mutable", read_then_a_member_adds_more)
    merged = await store.approve(ACME, first.id, by="u_admin")

    assert merged.id == lesson.id and {"employee", "staff"} <= set(merged.data["aliases"])
    assert await store.get(ACME, first.id) is None
    undone = await store.undo(ACME, lesson.id, "u_admin")
    assert set(undone.data["aliases"]) == {"emp name", "cert", "exp date"}


async def test_a_row_that_is_not_a_lesson_does_not_block_its_id(store):
    lesson = (await _teach_mapping(store)).lesson
    await store.forget(ACME, lesson.id, by="u_admin")
    await store._mem.remember(lesson.id, "written by some other code")  # same key
    assert await store.get(ACME, lesson.id) is None
    again = await _teach_mapping(store)
    assert again.kept and again.lesson.id == lesson.id and again.lesson.status == "active"
    assert not hasattr(store, "typed"), "no unscoped door to other tenants' lessons"


# ── better than what is in use, not merely better than nothing ──────────────


@pytest.mark.parametrize("mode", ["auto", "ask"])
async def test_a_change_that_makes_the_lesson_in_use_worse_is_not_kept(store, mode):
    """A member says 'Dept' is the name column of a sheet the lesson already maps
    in full. Against nothing that looks like progress (0 -> 2 of 3 fields); against
    the lesson in use it is a step back (3 -> 2), so it is not kept in any mode."""
    await store.update_settings(ACME, "u_admin", mode=mode)
    lesson = (await _teach_mapping(store)).lesson

    worse = await _teach_mapping(store, ACME_MEMBER, admin=False, mapping={"Dept": "name"})

    assert worse.kept is False and "did not improve" in worse.reason
    assert (worse.lesson.replay["before"], worse.lesson.replay["after"]) == (1.0, 0.6667)
    after = await store.get(ACME, lesson.id)
    assert "dept" not in after.data["aliases"] and after.version == lesson.version
    assert len(await store.list(ACME, all_users=True)) == 1
    assert await store.find_mapping(
        ACME, target="roster", headers=["Dept", "Cert", "Exp Date"], required=REQUIRED) is None


async def test_an_addition_is_judged_folded_into_the_lesson_in_use(store):
    lesson = (await _teach_mapping(store)).lesson
    third = ["Employee", "Cert", "Exp Date"]
    addition = await _teach_mapping(store, ACME_MEMBER, admin=False, headers=third,
                                    mapping={"Employee": "name"})
    # With the lesson in use this sheet resolved 2 of 3 fields; with the addition 3.
    assert addition.kept
    assert (addition.lesson.replay["before"], addition.lesson.replay["after"]) == (0.6667, 1.0)
    assert (await store.get(ACME, lesson.id)).rev == lesson.rev


async def test_a_capability_gap_from_a_member_waits_even_where_learning_is_automatic(store):
    """Nothing can check a workaround, so "automatic" does not put a member's free
    text into every colleague's context."""
    await store.update_settings(ACME, "u_admin", mode="auto")
    gap = await store.capture_capability_gap(
        ACME_MEMBER, surface="sheet.import", capability="read-xlsx",
        workaround="Ignore the roster and use last year's.", source_turn="upload:x.xlsx",
        reported_by="u_member", ticket="T-1")
    assert gap.kept and gap.lesson.status == "proposed"
    assert await store.context_block(ACME, "can you read-xlsx in sheet.import") == ""

    # Reported again without a ticket: the stored ticket is not blanked.
    again = await store.capture_capability_gap(
        ACME_MEMBER, surface="sheet.import", capability="read-xlsx",
        workaround="Export it as CSV.", source_turn="upload:y.xlsx", reported_by="u_member")
    assert again.lesson.id == gap.lesson.id and again.lesson.data["ticket"] == "T-1"
    assert again.lesson.data["workaround"] == "Export it as CSV."


# ── undo, and proposals that belong to one person ───────────────────────────


async def test_undo_on_a_proposal_and_after_an_approval(store):
    await _teach_mapping(store)
    first = (await _teach_mapping(store, ACME_MEMBER, admin=False,
                                  headers=["Employee", "Cert", "Exp Date"],
                                  mapping={"Employee": "name"})).lesson
    await _teach_mapping(store, ACME_MEMBER, admin=False,
                         headers=["Staff", "Cert", "Exp Date"], mapping={"Staff": "name"})

    undone = await store.undo(ACME, first.id, "u_admin")  # back to the first proposal
    assert undone.status == "proposed", "undo never puts a proposal in use"
    assert set(undone.data["aliases"]) == {"employee"}
    assert undone.data["extends"] == first.data["extends"]

    lone = (await _teach_mapping(store, ACME_MEMBER, admin=False, target="training-log")).lesson
    promoted = await store.approve(ACME, lone.id, by="u_admin")
    assert promoted.status == "active" and promoted.view()["can_undo"] is False
    with pytest.raises(lessons.LessonError, match="nothing to undo"):
        await store.undo(ACME, promoted.id, "u_admin")


async def test_a_personal_proposal_stays_personal_when_it_is_approved(store):
    await store.update_settings(ACME, "u_admin", applies_to="me")
    waiting = (await _teach_mapping(store, ACME_MEMBER, admin=False)).lesson
    assert waiting.user_id == "u_member" and waiting.status == "proposed"
    assert await store.get(ACME, waiting.id) is None, "not the admin's own"
    assert await store.get(ACME, waiting.id, all_users=True) is not None, "but theirs to OK"

    approved = await store.approve(ACME, waiting.id, by="u_admin")

    assert approved.user_id == "u_member" and approved.applies_to == "me"
    ask = dict(target="roster", headers=SECOND_SHEET, required=REQUIRED)
    assert await store.find_mapping(ACME_MEMBER, **ask) is not None
    assert await store.find_mapping(ACME, **ask) is None, "it is the member's lesson only"


async def test_learning_again_after_a_forget_clears_the_tombstone(store):
    lesson = (await _teach_mapping(store)).lesson
    await store.forget(ACME, lesson.id, by="u_admin")
    assert await store.forgotten(ACME) == [lesson.id]
    again = (await _teach_mapping(store)).lesson
    assert again.id == lesson.id and again.version == 1 and again.used_count == 0
    assert await store.forgotten(ACME) == []


async def test_two_admins_changing_different_settings_both_keep_their_change(store):
    import asyncio

    await asyncio.gather(
        store.update_settings(ACME, "u_admin", mode="auto"),
        store.update_settings(ACME, "u_other_admin", consolidate="weekly"),
        store.update_settings(ACME, "u_third_admin", applies_to="me"),
    )
    cfg = await store.settings(ACME)
    assert (cfg.mode, cfg.consolidate, cfg.applies_to) == ("auto", "weekly", "me")


async def test_a_lesson_is_a_typed_memory_record(store):
    """Stored on the existing typed memory: its role, tier and recall all apply."""
    from adk.typed_memory import Role, Tier, TypedMemory, role_of, tier_of

    typed = TypedMemory(store._mem)
    lesson = (await _teach_mapping(store)).lesson
    entry = await typed.get(lesson.id)
    assert role_of(entry) == Role.FACT and tier_of(entry) == Tier.PERMANENT
    assert entry.metadata["lesson"]["id"] == lesson.id
    hits = await typed.recall("roster spreadsheets")
    assert [h.id for h in hits] == [lesson.id]
    skill = (await _teach_skill(store)).lesson
    assert role_of(await typed.get(skill.id)) == Role.PROCEDURE


async def test_auto_mode_keeps_what_the_replay_check_proves(store):
    await store.update_settings(ACME, "u_admin", mode="auto")
    result = await _teach_mapping(store, ACME_MEMBER, admin=False)
    assert result.lesson.status == "active"


async def test_auto_mode_still_asks_when_nothing_was_proven(store):
    await store.update_settings(ACME, "u_admin", mode="auto")
    result = await store.capture(
        ACME, kind="memory", title="The yard is on Pine St", body="The yard is on Pine St.",
        evidence_type="user_correction", source_turn="turn-3")
    assert result.kept and result.lesson.status == "proposed"


async def test_off_mode_learns_nothing(store):
    await store.update_settings(ACME, "u_admin", mode="off")
    result = await _teach_mapping(store)
    assert result.kept is False and "off" in result.reason
    assert await store.list(ACME, all_users=True) == []
    assert (await store.page(ACME, agent_name="Deskbot"))["next_action"].startswith(
        "Learning is off.")


async def test_settings_reject_values_outside_the_choices(store):
    with pytest.raises(lessons.LessonError):
        await store.update_settings(ACME, "u_admin", mode="sometimes")
    with pytest.raises(lessons.LessonError):
        await store.update_settings(ACME, "u_admin", colour="blue")
    cfg = await store.settings(ACME)
    assert (cfg.mode, cfg.consolidate, cfg.applies_to) == ("ask", "task", "workspace")
    assert cfg.share_product_lessons is False


async def test_applies_to_me_keeps_a_lesson_personal(store):
    await store.update_settings(ACME, "u_admin", applies_to="me")
    lesson = (await _teach_mapping(store)).lesson
    ask = dict(target="roster", headers=SECOND_SHEET, required=REQUIRED)

    assert lesson.applies_to == "me"
    assert await store.find_mapping(ACME, **ask) is not None
    assert await store.find_mapping(ACME_MEMBER, **ask) is None, "not the member's lesson"
    assert await store.list(ACME_MEMBER) == []
    assert len(await store.list(ACME_MEMBER, all_users=True)) == 1, "an admin sees it"

    # The member's own lesson for the same sheet is a different record.
    theirs = (await _teach_mapping(store, ACME_MEMBER, mapping={"Emp Name": "name"})).lesson
    assert theirs.id != lesson.id and theirs.user_id == "u_member"
    assert set((await store.get(ACME, lesson.id)).data["aliases"]) == {"emp name", "cert",
                                                                       "exp date"}


async def test_a_personal_lesson_needs_a_person(store):
    """Learning set to "me" and nobody signed in: nothing is kept for everybody."""
    await store.update_settings(ACME, "u_admin", applies_to="me")
    nobody = lessons.Scope("tnt_acme", "default", "")
    result = await _teach_mapping(store, nobody)
    assert result.kept is False and "no signed-in person" in result.reason
    assert await store.list(ACME, all_users=True) == []


async def test_edit_then_undo(store):
    lesson = (await _teach_mapping(store)).lesson
    edited = await store.edit(ACME, lesson.id, "u_admin", title="Roster column names")
    assert edited.title == "Roster column names" and edited.view()["can_undo"] is True

    undone = await store.undo(ACME, lesson.id, "u_admin")
    assert undone.title == lesson.title and undone.view()["can_undo"] is False
    with pytest.raises(lessons.LessonError):
        await store.undo(ACME, lesson.id, "u_admin")


async def test_undo_puts_a_switched_off_lesson_back_on(store):
    lesson = (await _teach_mapping(store)).lesson
    await store.set_enabled(ACME, lesson.id, False, by="u_admin")
    assert (await store.undo(ACME, lesson.id, "u_admin")).status == "active"


async def test_forget_removes_the_text_and_leaves_a_content_free_tombstone(store, tmp_path):
    lesson = (await _teach_mapping(store)).lesson

    assert await store.forget(ACME, lesson.id, by="u_admin") is True

    assert await store.get(ACME, lesson.id) is None
    assert await store.list(ACME, all_users=True) == []
    assert await store.forgotten(ACME) == [lesson.id]
    assert await store.forgotten(OTHER) == []
    conn = sqlite3.connect(tmp_path / "lessons.db")  # blocking-ok: a test reads its own temp file
    rows = conn.execute("SELECT key, value, metadata FROM kv_store").fetchall()
    conn.close()
    assert rows, "the tombstone row is there"
    for _key, value, metadata in rows:
        assert "emp name" not in (value or "").lower()
        assert "emp name" not in (metadata or "").lower()
    assert await store.forget(ACME, lesson.id, by="u_admin") is False


async def test_the_list_is_capped_at_five_with_the_real_total(store):
    await store.update_settings(ACME, "u_admin", mode="auto")
    for i in range(7):
        await store.capture(
            ACME, kind="memory", title=f"Fact number {i}", body=f"Fact number {i} is so.",
            evidence_type="user_correction", source_turn=f"turn-{i}")

    page = await store.page(ACME)
    assert list(page)[0] == "next_action", "the next action comes first"
    assert page["next_action"] == "7 lessons waiting for your OK"
    assert len(page["items"]) == lessons.VISIBLE_CAP == 5
    assert page["total"] == 7 and page["more"] == 2
    rest = await store.page(ACME, offset=5)
    assert len(rest["items"]) == 2 and rest["more"] == 0
    row = page["items"][0]
    for key in ("what", "from", "when", "confirmed_by", "used", "last_helped", "status",
                "applies_to", "replay"):
        assert key in row


# ── rule 3: tenant-local ────────────────────────────────────────────────────


async def test_another_tenant_never_sees_or_uses_a_lesson(store):
    lesson = (await _teach_mapping(store)).lesson
    ask = dict(target="roster", headers=SECOND_SHEET, required=REQUIRED)

    assert await store.get(OTHER, lesson.id) is None
    assert await store.get(OTHER, lesson.id, all_users=True) is None
    assert await store.list(OTHER, all_users=True) == []
    assert await store.find_mapping(OTHER, **ask) is None
    assert (await store.page(OTHER, all_users=True))["total"] == 0
    assert await store.context_block(OTHER, "roster spreadsheets columns") == ""
    # "Not found", exactly as for an id that does not exist: the owner's own approve
    # and undo raise a plain LessonError here (nothing waiting, nothing to undo), so
    # only the not-found type proves the scope check is what refused.
    for call in (store.approve, store.undo):
        with pytest.raises(lessons.LessonNotFoundError):
            await call(OTHER, lesson.id, "u_admin")
    with pytest.raises(lessons.LessonNotFoundError):
        await store.set_enabled(OTHER, lesson.id, False, by="u_admin")
    with pytest.raises(lessons.LessonNotFoundError):
        await store.edit(OTHER, lesson.id, "u_admin", title="mine now")
    assert await store.record_use(OTHER, lesson.id, helped=False) is None
    assert await store.recheck(OTHER, lesson.id) is None
    assert await store.forget(OTHER, lesson.id, by="u_admin") is False
    untouched = await store.get(ACME, lesson.id)
    assert untouched.status == "active" and untouched.title == lesson.title
    assert untouched.used_count == 0


async def test_a_separator_inside_an_id_cannot_merge_two_workspaces(store):
    """("a", "b|c") and ("a|b", "c") are different workspaces with different lessons."""
    one, two = lessons.Scope("a", "b|c", "u"), lessons.Scope("a|b", "c", "u")
    first = (await _teach_mapping(store, one)).lesson
    second = (await _teach_mapping(store, two, mapping={"Emp Name": "name"})).lesson

    assert first.id != second.id
    assert set((await store.get(one, first.id)).data["aliases"]) == {"emp name", "cert",
                                                                    "exp date"}
    await store.update_settings(one, "u", mode="off")
    assert (await store.settings(two)).mode == "ask"
    await store.consolidate(one, force=True)
    assert await store.last_consolidated(two) == 0.0


async def test_another_workspace_of_the_same_tenant_is_separate(store):
    lesson = (await _teach_mapping(store)).lesson
    assert await store.get(ACME_OTHER_WS, lesson.id, all_users=True) is None
    assert await store.find_mapping(
        ACME_OTHER_WS, target="roster", headers=SECOND_SHEET, required=REQUIRED) is None


async def test_settings_are_per_workspace(store):
    await store.update_settings(ACME, "u_admin", mode="off")
    assert (await store.settings(OTHER)).mode == "ask"
    assert (await store.settings(ACME_OTHER_WS)).mode == "ask"


async def test_a_scope_needs_a_tenant():
    with pytest.raises(lessons.LessonError):
        lessons.Scope("")


async def test_the_store_never_opens_a_network_client(tmp_path, monkeypatch):
    """Spirit and fleet sync are ON in the environment; a lesson still stays local."""
    import httpx

    monkeypatch.setenv("AITHER_SPIRIT_BRIDGE", "true")
    monkeypatch.setenv("AITHER_SPIRIT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AITHER_FLEET_SYNC", "true")
    monkeypatch.setenv("AITHER_FLEET_MEMORY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AITHER_CLOUD_MODE", "cloud_first")
    opened = []

    class Refuse:
        def __init__(self, *args, **kwargs):
            opened.append(kwargs)
            raise AssertionError("a lesson store tried to open a network client")

    monkeypatch.setattr(httpx, "AsyncClient", Refuse)
    monkeypatch.setattr(httpx, "Client", Refuse)
    store = lessons.LessonStore(tmp_path / "local.db")

    lesson = (await _teach_mapping(store)).lesson
    await store.find_mapping(ACME, target="roster", headers=SECOND_SHEET, required=REQUIRED)
    await store.context_block(ACME, "roster spreadsheets columns")
    await store.edit(ACME, lesson.id, "u_admin", title="Roster columns")
    await store.forget(ACME, lesson.id, "u_admin")

    assert opened == []

    # Two layers, each enough alone: the switches are off, and the remote methods
    # are overridden, so forcing the switches back on still sends nothing.
    memory = store._mem
    assert memory._spirit_enabled is False and memory._fleet_enabled is False
    memory._spirit_enabled = True
    memory._fleet_enabled = True
    memory._fleet_url = "http://127.0.0.1:9"
    await _teach_mapping(store)
    await memory.search("emp name")
    # The inherited write and read paths too, for any code that uses the memory
    # directly: with a plain Memory these four calls open two clients each.
    await memory.remember("note", "a value written through the base class")
    assert await memory.recall("a key that is not there") is None
    await memory.fleet_push_all()
    assert opened == []


async def test_a_plain_memory_cannot_back_a_lesson_store(tmp_path):
    with pytest.raises(lessons.LessonError):
        lessons.LessonStore(memory=Memory(db_path=tmp_path / "plain.db"))


# ── context at the moment of use ────────────────────────────────────────────


async def test_context_block_carries_active_lessons_that_bear_on_the_question(store):
    lesson = (await _teach_mapping(store)).lesson

    block = await store.context_block(ACME, "which columns do roster spreadsheets use?")
    assert block.startswith("# LESSONS learned in this workspace")
    assert "'emp name' is name" in block
    assert await store.context_block(ACME, "what is the weather tomorrow") == ""

    await store.set_enabled(ACME, lesson.id, False, by="u_admin")
    assert await store.context_block(ACME, "which columns do roster spreadsheets use?") == ""


async def test_a_capability_gap_is_said_out_loud_not_failed_silently(store):
    question = "can you read-xlsx files in sheet.import"
    gap = dict(surface="sheet.import", capability="read-xlsx",
               workaround="Ask for the roster as a CSV export.",
               source_turn="upload:roster.xlsx")

    # A member hit it: the workaround would be put in every colleague's context, so
    # in ask mode it waits for the admin like any other lesson.
    result = await store.capture_capability_gap(ACME_MEMBER, reported_by="u_member", **gap)
    assert result.kept and result.lesson.kind == "tool_request"
    assert result.lesson.status == "proposed" and result.lesson.applies_to == "workspace"
    assert await store.context_block(ACME_MEMBER, question) == ""
    assert (await store.page(ACME, all_users=True))["next_action"] == \
        "1 lesson waiting for your OK"

    await store.approve(ACME, result.lesson.id, by="u_admin")
    block = await store.context_block(ACME_MEMBER, question)
    assert "cannot read-xlsx" in block and "CSV export" in block

    # An admin hitting a gap is the OK.
    own = await store.capture_capability_gap(
        ACME, reported_by="u_admin", reported_by_admin=True,
        **{**gap, "capability": "read-xls"})
    assert own.lesson.status == "active"

    with pytest.raises(lessons.LessonError):
        await store.capture_capability_gap(
            ACME, surface="Dana's Roster Folder", capability="read-xlsx",
            workaround="x", source_turn="t")


# ── SKILL.md ────────────────────────────────────────────────────────────────


async def _teach_skill(store, scope=ACME):
    return await store.capture(
        scope, kind="skill", title="Run a certification audit",
        body="Reconcile the roster against the tracking sheet and list what expires soon.",
        evidence_type="retry_success", source_turn="conv-42:turn-7",
        confirmed_by="u_admin", confirmed_by_admin=True, trigger="skill:cert-audit",
        data={"when_to_use": "Someone asks who is out of date on certifications.",
              "steps": ["Find the roster by its content, whatever it is called.",
                        "Match each person to their rows in the tracking sheet.",
                        "List certifications that expire within 60 days."],
              "inputs": ["the roster", "the tracking sheet"],
              "example": "Audit for March: 4 people expiring, 1 missing a record."},
        run=lambda lesson: 1.0 if lesson is not None else 0.0)


async def test_a_skill_exports_as_a_valid_skill_md(store):
    lesson = (await _teach_skill(store)).lesson

    name, text = lessons.to_skill_md(lesson)

    assert name == "run-a-certification-audit"
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == name, "the name must match the parent directory"
    assert 1 <= len(front["description"]) <= 1024
    assert front["description"].startswith("Run a certification audit. Use when someone asks")
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in front["metadata"].items())
    assert front["metadata"]["replay-before"] == "0.0"
    assert front["metadata"]["replay-after"] == "1.0"
    assert front["metadata"]["evidence"] == "retry_success"
    assert "1. Find the roster by its content, whatever it is called." in text
    assert "## Worked example" in text


async def test_skill_md_round_trips_into_another_workspace_as_a_proposal(store):
    _name, text = lessons.to_skill_md((await _teach_skill(store)).lesson)

    result = await lessons.import_skill_md(store, OTHER, text, imported_by="u_admin")

    assert result.kept
    imported = result.lesson
    assert imported.tenant_id == "tnt_other" and imported.kind == "skill"
    assert imported.status == "proposed", "an imported skill has no outcome here yet"
    assert imported.evidence["type"] == "imported"
    assert imported.title == "Run a certification audit"
    assert imported.data["steps"][1] == "Match each person to their rows in the tracking sheet."
    assert imported.data["inputs"] == ["the roster", "the tracking sheet"]
    # ...and stays a proposal even where learning is automatic.
    await store.update_settings(ACME_OTHER_WS, "u_admin", mode="auto")
    again = await lessons.import_skill_md(store, ACME_OTHER_WS, text, imported_by="u_admin")
    assert again.lesson.status == "proposed"


async def test_only_a_skill_exports(store):
    with pytest.raises(lessons.LessonError):
        lessons.to_skill_md((await _teach_mapping(store)).lesson)


@pytest.mark.parametrize("text, problem", [
    ("# no frontmatter", "frontmatter"),
    ("---\nname: Bad Name\ndescription: x\n---\n", "name"),
    ("---\nname: double--hyphen\ndescription: x\n---\n", "name"),
    ("---\nname: ok-name\n---\n", "description"),
    ("---\nname: ok-name\ndescription: " + "x" * 1025 + "\n---\n", "description"),
], ids=["no-frontmatter", "bad-name", "double-hyphen", "no-description", "long-description"])
def test_parse_skill_md_names_what_is_wrong(text, problem):
    with pytest.raises(lessons.LessonError, match=problem):
        lessons.parse_skill_md(text)


def test_parse_skill_md_reads_a_third_party_skill():
    text = ("---\nname: pdf-processing\ndescription: Extract text and tables from PDFs. "
            "Use when working with PDF files.\nlicense: Apache-2.0\n"
            "metadata:\n  author: example-org\n  version: \"1.0\"\n---\n\n"
            "# PDF processing\n\nUse pdfplumber for text.\n\n## Steps\n\n- open the file\n"
            "- extract the pages\n")
    parsed = lessons.parse_skill_md(text)
    assert parsed["name"] == "pdf-processing"
    assert parsed["title"] == "PDF processing"
    assert parsed["body"] == "Use pdfplumber for text."
    assert parsed["data"]["steps"] == ["open the file", "extract the pages"]


def test_skill_name_is_a_valid_slug():
    assert lessons.skill_name("  How WE run: a Cert. Audit!! ") == "how-we-run-a-cert-audit"
    assert lessons.skill_name("") == "unnamed-skill"
    assert len(lessons.skill_name("x" * 200)) == 64
    assert lessons.skill_name("a" * 63 + "-b") == "a" * 63, "no trailing hyphen after the cut"


def _alias_bomb(field: str) -> str:
    """Under 1 KB of YAML whose ``field`` would be ~10**9 characters as text."""
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 9):
        lines.append(f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]")
    other = "name: ok-name" if field == "description" else "description: fine"
    return "---\n" + "\n".join(lines) + f"\n{other}\n{field}: *a8\n---\n\n# Title\n\nBody.\n"


def _merge_key_bomb() -> str:
    """~700 chars of YAML that ``yaml.safe_load`` alone expands for minutes and
    gigabytes: every level merges the level below ten times over."""
    lines = ["a0: &a0 {k0: v, k1: v, k2: v, k3: v, k4: v, k5: v, k6: v, k7: v, k8: v, k9: v}"]
    for i in range(1, 10):
        lines.append(f"a{i}: &a{i} {{<<: [" + ", ".join([f"*a{i - 1}"] * 10) + "]}")
    return ("---\nname: ok-name\ndescription: fine\n" + "\n".join(lines)
            + "\n---\n\n# Title\n\nBody.\n")


@pytest.mark.timeout(20)
@pytest.mark.parametrize("text", [
    _alias_bomb("name"), _alias_bomb("description"), _merge_key_bomb(),
    "---\nname: ok-name\ndescription: &d fine\nmetadata: {x: *d}\n---\n",
    "---\nname: ok-name\ndescription: !!python/name:os.system fine\n---\n",
], ids=["alias-name", "alias-description", "merge-keys", "plain-anchor", "tag"])
def test_yaml_anchors_aliases_and_tags_are_refused_before_anything_is_built(text):
    import time

    assert len(text) < 1200
    started = time.perf_counter()
    with pytest.raises(lessons.LessonError, match="no anchors, aliases or tags"):
        lessons.parse_skill_md(text)
    assert time.perf_counter() - started < 1.0, "refused on the token stream, not after loading"


@pytest.mark.timeout(20)
@pytest.mark.parametrize("opening, closing", [("[", "]"), ("{a: ", "}")])
def test_deeply_nested_frontmatter_is_a_refusal_not_a_crash(opening, closing):
    text = ("---\nname: ok-name\ndescription: fine\nx: " + opening * 600 + "1"
            + closing * 600 + "\n---\n")
    with pytest.raises(lessons.LessonError, match="nested too deeply"):
        lessons.parse_skill_md(text)


@pytest.mark.timeout(20)
@pytest.mark.parametrize("text, problem", [
    ("---\nname: ok-name\ndescription: fine\n", "must start with a YAML frontmatter"),
    ("---" + "\n" * 99_990 + "x", "frontmatter is larger than"),  # 100k blank lines
    ("---\nname: ok-name\n" + "k: v\n" * 4000 + "---\n", "larger than"),
    ("---\nname: ok-name\ndescription: fine\n---\n" + "x" * 100_000, "larger than"),
    ("---\n- a\n- b\n---\n", "mapping"),
    ("---\nname: 12\ndescription: fine\n---\n", "name"),
    ("---\nname: ok-name\ndescription: [a, b]\n---\n", "description"),
    # errors the YAML loader raises that are not YAML errors
    ("---\nname: ok-name\ndescription: fine\nwhen: 2024-13-45\n---\n", "not valid YAML"),
    ("---\nname: ok-name\ndescription: fine\nn: " + "9" * 5000 + "\n---\n",
     "not valid YAML|description"),
    ('---\nname: ok-name\ndescription: "\\UFFFFFFFF"\n---\n', "not valid YAML"),
], ids=["no-closing-fence", "endless-frontmatter", "huge-frontmatter", "huge-file",
        "list-frontmatter", "numeric-name", "list-description", "impossible-date",
        "huge-number", "escape-outside-unicode"])
def test_hostile_skill_files_are_refused_quickly(text, problem):
    with pytest.raises(lessons.LessonError, match=problem):
        lessons.parse_skill_md(text)


def test_skill_metadata_keeps_plain_values_only():
    parsed = lessons.parse_skill_md(
        "---\nname: ok-name\ndescription: fine\nmetadata:\n  author: someone\n  n: 3\n"
        "  nested: {a: [1, 2]}\n---\n\nBody.\n")
    assert parsed["metadata"] == {"author": "someone", "n": "3"}


# ── the one outbound shape ──────────────────────────────────────────────────

TENANT_MARKERS = ("hartwell", "dana", "okafor", "roster_q3", "tnt_acme", "u_member",
                  "field-office", "certs@hartwell.example", "csv")


@pytest.fixture
def vocabulary(monkeypatch):
    """A private copy of the closed vocabulary, with this test product registered."""
    for name in ("PRODUCT_SURFACES", "PRODUCT_CAPABILITIES", "PRODUCT_AGENTS"):
        monkeypatch.setattr(lessons, name, set(getattr(lessons, name)))
    lessons.register_product_vocabulary(agents=("deskbot",))


async def _gap(store, **over):
    args = dict(surface="sheet.import", capability="read-xlsx",
                workaround="Ask Dana Okafor at Hartwell for roster_q3 as a CSV, "
                           "or mail certs@hartwell.example.",
                source_turn="upload:Hartwell roster_q3.xlsx", reported_by="u_member")
    args.update(over)
    return (await store.capture_capability_gap(ACME_MEMBER, **args)).lesson


async def _sharing(store):
    return await store.update_settings(ACME, "u_admin", share_product_lessons=True)


async def test_contribution_is_off_by_default(store, vocabulary):
    lesson = await _gap(store)
    with pytest.raises(lessons.LessonError, match="off"):
        lessons.product_lesson(lesson, await store.settings(ACME), agent="deskbot")


async def test_an_outbound_lesson_carries_no_tenant_text(store, vocabulary):
    lesson = await _gap(store)

    payload = lessons.product_lesson(lesson, await _sharing(store), agent="deskbot")

    assert payload == {
        "id": payload["id"], "lesson": "sheet.import: missing capability read-xlsx",
        "source": "product:deskbot", "kind": "tool_request", "class": "knowledge",
        "verified_by": "capability_gap", "review": "pending"}
    blob = json.dumps(payload).lower()
    for marker in TENANT_MARKERS:
        assert marker not in blob, f"tenant text {marker!r} left the workspace"


async def test_the_guard_refuses_any_row_that_is_not_the_vocabulary_row(store, vocabulary):
    """The test the boundary rests on. The guard does not search a row for known
    tenant words; it refuses every row that is not exactly the one the vocabulary
    builds. So text in another script, a single initial, or a sentence made only
    of ordinary words is refused the same way as a name."""
    lesson = await _gap(store)
    clean = lessons.product_lesson(lesson, await _sharing(store), agent="deskbot")
    lessons.assert_content_free(dict(clean), lesson)  # the clean row passes

    said = clean["lesson"]
    tampered = [
        {**clean, "lesson": f"{said} ({lesson.data['workaround']})"},
        {**clean, "lesson": f"{said} for Hartwell"},
        {**clean, "lesson": f"{said} seen at tnt_acme"},
        {**clean, "lesson": f"{said} спросите "
                            "Дану"},       # not ASCII
        {**clean, "lesson": f"{said} D"},                      # one character
        {**clean, "lesson": "send roster by email"},           # ordinary words only
        {**clean, "source": "product:hartwell-ops"},           # an unregistered signer
        {**clean, "source": "deskbot"},
        {**clean, "review": "pending, ask Dana"},
        {**clean, "id": "okafor"},
        {**clean, "note": {"detail": [lesson.evidence["source_turn"]]}},  # an extra field
        {k: v for k, v in clean.items() if k != "review"},
        [clean],
    ]
    for row in tampered:
        with pytest.raises(lessons.TenantTextLeakError):
            lessons.assert_content_free(row, lesson)


async def test_a_lesson_with_tenant_text_where_the_slugs_belong_is_refused(store, vocabulary):
    lesson = await _gap(store)
    cfg = await _sharing(store)
    for product in ({"surface": "sheet.import", "capability": "ask Dana Okafor"},
                    {"surface": "Hartwell", "capability": "read-xlsx"},
                    {"surface": ["sheet.import"], "capability": "read-xlsx"},
                    {}):
        lesson.data = {**lesson.data, "product": product}
        with pytest.raises(lessons.TenantTextLeakError):
            lessons.product_lesson(lesson, cfg, agent="deskbot")


async def test_every_outbound_lesson_goes_through_the_guard(store, vocabulary, monkeypatch):
    lesson = await _gap(store)
    cfg = await _sharing(store)
    seen = []

    def refuse(payload, checked):
        seen.append((payload, checked.id))
        raise lessons.TenantTextLeakError("refused by the guard")

    monkeypatch.setattr(lessons, "assert_content_free", refuse)
    with pytest.raises(lessons.TenantTextLeakError, match="refused by the guard"):
        lessons.product_lesson(lesson, cfg, agent="deskbot")
    assert len(seen) == 1 and seen[0][1] == lesson.id


async def test_only_a_tool_request_in_the_vocabulary_can_be_contributed(store, vocabulary):
    cfg = await _sharing(store)
    mapping_lesson = (await _teach_mapping(store)).lesson
    with pytest.raises(lessons.LessonError, match="only a tool request"):
        lessons.product_lesson(mapping_lesson, cfg, agent="deskbot")

    unknown = await _gap(store, capability="read-hartwell-format")
    with pytest.raises(lessons.TenantTextLeakError, match="vocabulary"):
        lessons.product_lesson(unknown, cfg, agent="deskbot")


async def test_an_unregistered_product_cannot_sign_a_row(store, vocabulary):
    lesson = await _gap(store)
    with pytest.raises(lessons.TenantTextLeakError, match="not a registered product"):
        lessons.product_lesson(lesson, await _sharing(store), agent="hartwell-ops")


def test_the_vocabulary_is_extended_by_code_with_slugs_only(vocabulary):
    lessons.register_product_vocabulary(surfaces=("roster.import",), capabilities=("read-ods",))
    assert "roster.import" in lessons.PRODUCT_SURFACES
    assert "read-ods" in lessons.PRODUCT_CAPABILITIES
    for bad in ("Dana's Folder", "", "has space", None, "UPPER"):
        with pytest.raises(lessons.LessonError):
            lessons.register_product_vocabulary(surfaces=(bad,))
        with pytest.raises(lessons.LessonError):
            lessons.register_product_vocabulary(agents=(bad,))
    assert "Dana's Folder" not in lessons.PRODUCT_SURFACES
    assert "Dana's Folder" not in lessons.PRODUCT_AGENTS
