"""awsh ``/hearth teach``: Aither Classroom from the shell, through the gateway.

Pins: each teach verb is exactly one ``classroom_*`` tool call with no identity
argument, through the same gateway seam ``/hearth cloud`` uses; the writes
(assign, announce, draft) send NOTHING without ``--yes`` and carry ``confirm=true``
with it; bad usage sends nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from adk.home import gateway_hearth as gh
from adk.shell.plugins.builtins import hearth as hearth_plugin

CLASSES = {"classes": [{"class_id": "c1", "name": "Room 4", "grade_level": "3-5",
                        "student_count": 22, "consent": {"consent_recorded": True}}]}


class Gateway:
    def __init__(self, answers: Dict[str, Dict[str, Any]]):
        self.answers = answers
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, arguments))
        return self.answers.get(name, {})


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """The gateway stub behind gateway_hearth.call_gateway; no local serve running."""
    from adk.home import config as hc

    monkeypatch.setenv(hc.HOME_ENV, str(tmp_path / "agent-home"))
    stub = Gateway({"classroom_list_classes": CLASSES})
    monkeypatch.setattr(gh, "call_gateway", stub)
    return stub


def test_teach_classes_calls_the_classroom_list_tool_through_the_gateway(gw):
    out = hearth_plugin.hearth_command(["teach", "classes"])
    assert gw.calls == [("classroom_list_classes", {})]
    assert "c1" in out and "Room 4" in out


@pytest.mark.parametrize("args, tool, arguments", [
    (["roster", "c1"], "classroom_roster", {"class_id": "c1"}),
    (["queue", "c1"], "classroom_review_queue", {"class_id": "c1"}),
    (["hard", "c1"], "classroom_hard_now", {"class_id": "c1", "days": 14}),
    (["hard", "c1", "30"], "classroom_hard_now", {"class_id": "c1", "days": 30}),
    (["insight", "c1"], "classroom_insight", {"class_id": "c1"}),
    (["insight", "c1", "m7"], "classroom_insight",
     {"class_id": "c1", "student_member_id": "m7"}),
])
def test_each_read_verb_is_one_tool_call_with_no_identity(gw, args, tool, arguments):
    hearth_plugin.hearth_command(["teach", *args])
    assert gw.calls == [(tool, arguments)]


@pytest.mark.parametrize("args", [
    ["assign", "c1", "L9"],
    ["assign", "c1", "--skills", "frac,dec", "--to", "m7", "--", "Fractions"],
    ["announce", "c1", "--", "No", "class", "Friday"],
    ["draft", "c1", "--minutes", "30", "--", "fractions"],
])
def test_a_write_without_confirmation_sends_nothing(gw, args):
    out = hearth_plugin.hearth_command(["teach", *args])
    assert gw.calls == []
    assert out.startswith("NOT sent") and "--yes" in out


@pytest.mark.parametrize("args, tool, arguments", [
    (["assign", "c1", "L9", "--yes"], "classroom_assign",
     {"class_id": "c1", "lesson_id": "L9", "confirm": True}),
    (["assign", "c1", "--skills", "frac,dec", "--to", "m7", "--due", "2026-10-09", "--yes",
      "--", "Fractions"], "classroom_assign",
     {"class_id": "c1", "skill_ids": ["frac", "dec"], "student_member_id": "m7",
      "due_at": "2026-10-09", "title": "Fractions", "confirm": True}),
    (["announce", "c1", "--yes", "--", "No", "class", "Friday"], "classroom_announce",
     {"class_id": "c1", "text": "No class Friday", "confirm": True}),
    (["draft", "c1", "--grade", "3-5", "--yes", "--", "fractions"], "classroom_draft_lesson",
     {"class_id": "c1", "topic": "fractions", "grade_level": "3-5", "confirm": True}),
])
def test_a_confirmed_write_is_one_call_with_confirm(gw, args, tool, arguments):
    hearth_plugin.hearth_command(["teach", *args])
    assert gw.calls == [(tool, arguments)]


def test_the_preview_names_the_command_that_sends_it(gw):
    out = hearth_plugin.hearth_command(["teach", "announce", "c1", "--", "hi", "all"])
    assert "/hearth teach announce c1 --yes -- hi all" in out
    assert '"text": "hi all"' in out


def test_bad_usage_sends_nothing(gw):
    for args in (["roster"], ["hard", "c1", "x"], ["assign", "c1"], ["announce", "c1"],
                 ["announce", "c1", "--yes"], ["draft", "c1", "--minutes", "x", "--", "t"],
                 ["assign", "c1", "L9", "--bogus"], ["nope"]):
        assert hearth_plugin.hearth_command(["teach", *args]).startswith("hearth teach:")
    assert gw.calls == []


def test_errors_and_server_refusals_are_named(gw):
    gw.answers["classroom_roster"] = {"error": "classroom_forbidden: not your class"}
    assert hearth_plugin.hearth_command(["teach", "roster", "c9"]) == \
        "hearth teach: classroom_forbidden: not your class"
    gw.answers["classroom_assign"] = {"status": "confirm_required", "hint": "nothing was sent."}
    out = hearth_plugin.hearth_command(["teach", "assign", "c1", "L9", "--yes"])
    assert out.startswith("hearth teach: classroom_assign wrote nothing")


def test_teach_help_lists_the_verbs(gw):
    out = hearth_plugin.hearth_command(["teach"])
    assert "classes" in out and "announce" in out and gw.calls == []
