"""awrise's `why` reaches every surface that reads /wakes, and the wake-failed
card's `fix` answer has a real consumer.

2026-11-08: the desk's wakes panel showed 21 broken wakes with an exit code
each. awrise now stamps `last_why`/`last_cause`/`held_until` on the job and
`why`/`cause`/`output_tail` on the bad ledger row; these pin that the one reader
(adk.wakes) carries them, and that answering `fix` spawns
`awrise investigate --name <job> --dispatch` detached -- the name re-validated
at apply time, nothing else from the card reaching the argv.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adk import wakes
from adk.decisions import card_recipes


@pytest.fixture(autouse=True)
def _no_popups(monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_POPUP", "0")
    monkeypatch.setenv("AITHER_NO_GUI_PROMPT", "1")


def _store(home: Path) -> None:
    job = {
        "run": "py publish.py", "every": "5m", "interval_s": 300.0, "enabled": True,
        "last_state": "skipped_held", "last_reason": "held:no llama-server until x",
        "consecutive_failures": 3,
        "last_why": "publish: no llama-server on spark.local:8118",
        "last_cause": "dependency-down", "why_streak": 3,
        "why_since": "2026-11-08T18:00:00+00:00",
        "held_until": "2026-11-08T19:00:00+00:00", "held_on": "publish: no llama-server",
    }
    (home / "jobs.json").write_text(json.dumps({"schema": 2, "jobs": {"moe": job}}), "utf-8")
    ledger = home / "ledger"
    ledger.mkdir()
    row = {
        "ts": "2026-11-08T18:10:00+00:00", "job": "moe", "event": "finished",
        "state": "failure", "reason": "exit_2", "wake_id": "w-aaaaaaaa", "exit_code": 2,
        "why": "publish: no llama-server on spark.local:8118", "cause": "dependency-down",
        "output_tail": "publish: no llama-server on spark.local:8118",
    }
    (ledger / "2026-11-08.jsonl").write_text(json.dumps(row) + "\n", "utf-8")


def test_snapshot_and_get_wake_carry_the_why(tmp_path):
    _store(tmp_path)
    snap = wakes.snapshot(tmp_path)
    moe = snap["wakes"][0]
    assert moe["last_why"].startswith("publish: no llama-server")
    assert moe["last_cause"] == "dependency-down"
    assert moe["why_streak"] == 3 and moe["held_until"] and moe["held_on"]
    one = wakes.get_wake("moe", tmp_path)
    assert one is not None
    newest = one["recent"][0]
    assert newest["why"].startswith("publish:") and newest["cause"] == "dependency-down"
    assert "no llama-server" in newest["output_tail"]


def test_an_older_store_without_why_reads_as_none(tmp_path):
    job = {"run": "x", "every": "5m", "interval_s": 300.0}
    (tmp_path / "jobs.json").write_text(json.dumps({"schema": 2, "jobs": {"a": job}}), "utf-8")
    moe = wakes.snapshot(tmp_path)["wakes"][0]
    assert moe["last_why"] is None and moe["held_until"] is None and moe["error"] is None


def test_wake_failed_offers_fix_with_a_real_consumer():
    recipe = card_recipes.get_recipe("wake-failed")
    assert [o["key"] for o in recipe["options"]] == ["disable", "keep", "run_now", "fix"]
    assert recipe["steerback"]["fix"] == ("awrise", "investigate", "--name", "{job}", "--dispatch")
    assert card_recipes.spawns_on("wake-failed", "fix")
    assert recipe["default_key"] == "keep" and recipe["steerback"]["keep"] is None
    assert not card_recipes.check_all()


def _card(answer: str, job: str = "moe"):
    card = card_recipes.build_card(
        "wake-failed", {"job": "moe", "first_failure_ts": "2026-11-08T18:00:00Z", "n": "5"}
    )
    card.answer = answer
    card.recipe_vars["job"] = job
    return card


def test_answering_fix_spawns_investigate_detached(monkeypatch, tmp_path):
    binary = tmp_path / "awrise.exe"
    binary.write_text("", "utf-8")
    monkeypatch.setenv(card_recipes.BIN_ENV, str(binary))
    spawned: list[list[str]] = []

    class _Proc:
        pid = 4242

    def fake_detached(argv):
        spawned.append(argv)
        return _Proc()

    monkeypatch.setattr(card_recipes, "_detached", fake_detached)
    applied, why = card_recipes.apply_answer(_card("fix"))
    assert applied, why
    assert spawned == [[str(binary.resolve()), "investigate", "--name", "moe", "--dispatch"]]
    assert "pid 4242" in why


def test_fix_refuses_a_tampered_job_name(monkeypatch, tmp_path):
    binary = tmp_path / "awrise.exe"
    binary.write_text("", "utf-8")
    monkeypatch.setenv(card_recipes.BIN_ENV, str(binary))
    monkeypatch.setattr(card_recipes, "_detached", lambda argv: pytest.fail(f"spawned {argv}"))
    applied, why = card_recipes.apply_answer(_card("fix", job="moe; rm -rf /"))
    assert not applied and "invalid at apply time" in why
