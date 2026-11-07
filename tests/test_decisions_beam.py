"""The attention beam's engine rules (adk/decisions/beam.py).

Each arm pins a defect measured on 2026-10-28: a fresh approval card at #29 of
132, eight zombie "needs you" cards pinned above it, and a count line that named
nothing. Fixture cases live in tests/fixtures/beam_cases.json; awsh's
src/beam.ts mirrors these rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adk.decisions import beam

FIXTURE = Path(__file__).with_name("fixtures") / "beam_cases.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _load()["cases"], ids=lambda c: c["name"])
def test_fixture_cases(case):
    now = _load()["now"]
    live = case.get("live")
    result = beam.build(case["cards"], now=now, live_session_ids=live)
    assert [c["id"] for c in result["fresh"]] == case["expect"]["fresh_ids"]
    assert result["open"] == case["expect"]["open"]
    assert result["oldest_age"] == pytest.approx(case["expect"]["oldest_age"], abs=1)


def test_unknown_liveness_never_hides_an_ask():
    # not measuring is not permission to hide (a missed card is the whole complaint)
    card = {"id": "d-z", "kind": "blocked", "urgency": "high", "created_at": 1_799_999_700,
            "options": [], "dedupe_key": "cc-waiting:sess-dead:1799999700",
            "source": {"session_id": "sess-dead"}}
    assert beam.build([card], now=1_800_000_000, live_session_ids=None)["open"] == 1


def test_the_session_rides_the_key_when_source_is_absent():
    card = {"id": "d-z", "kind": "blocked", "options": [],
            "dedupe_key": "cc-waiting:sess-live:1", "created_at": 1_799_999_700}
    assert beam.still_waiting(card, {"sess-live"}) is True
    assert beam.still_waiting(card, {"other"}) is False


def test_compress_names_asks_with_id_and_age_and_maps_clicks_back():
    now = 1_800_000_000
    cards = [{"id": "d-ts7e", "kind": "decision", "urgency": "high",
              "created_at": now - 600, "title": "Election Day is Tuesday",
              "options": [{"key": "post"}]}]
    segs = beam.compress(beam.build(cards, now=now), now=now, width=120)
    assert segs[0]["key"] == "decision:d-ts7e"
    assert segs[0]["plain"].startswith("d-ts7e 10m Election Day")
    assert segs[0]["hot"] is True
    assert segs[-1]["key"] == "decisions"
    assert segs[-1]["plain"] == "(+0, 1 open)"


def test_compress_nothing_waiting_renders_nothing():
    assert beam.compress(beam.build([], now=1_800_000_000), now=1_800_000_000, width=120) == []


def test_compress_fits_the_row_and_keeps_the_count():
    now = 1_800_000_000
    cards = [{"id": f"d-l{i}", "kind": "decision", "urgency": "normal",
              "created_at": now - i, "title": "x" * 300, "options": [{"key": "a"}]}
             for i in range(5)]
    segs = beam.compress(beam.build(cards, now=now), now=now, width=90)
    used = sum(len(s["plain"]) + (3 if i else 0) for i, s in enumerate(segs))
    assert used <= 90, f"row used {used} of 90"
    assert len(segs) <= beam.BEAM_MAX + 1
    assert segs[-1]["plain"].endswith("open)")


def test_a_narrow_row_keeps_a_count_instead_of_a_fragment():
    now = 1_800_000_000
    cards = [{"id": "d-a", "kind": "decision", "urgency": "normal",
              "created_at": now, "title": "t", "options": [{"key": "a"}]}]
    segs = beam.compress(beam.build(cards, now=now), now=now, width=24)
    assert segs == [{"key": "decisions", "text": "DECIDE 1 waiting",
                     "plain": "DECIDE 1 waiting", "hot": False}]


def test_hot_means_now():
    assert beam.is_hot({"kind": "decision", "urgency": "critical"})
    assert beam.is_hot({"kind": "decision", "urgency": "high"})
    assert beam.is_hot({"kind": "credential", "urgency": "normal"})
    assert beam.is_hot({"kind": "blocked", "urgency": "low"})
    assert not beam.is_hot({"kind": "decision", "urgency": "normal"})


def test_format_options_spells_the_choices():
    assert beam.format_options({"options": [{"key": "post", "label": "Post"}, {"key": "skip"}]}) \
        == "post (Post) / skip"
    assert "no options" in beam.format_options({"options": []})


def test_fmt_age_matches_the_surfaces():
    assert [beam.fmt_age(s) for s in (0, 42, 7 * 60, 3 * 3600, 2 * 86400)] == \
        ["0s", "42s", "7m", "3h", "2d"]
