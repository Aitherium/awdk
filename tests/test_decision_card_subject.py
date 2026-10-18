"""DecisionCard.subject: the universe object a card concerns, end to end in awdk.

The subject is where a surface DRAWS a card; it is unrelated to the dedupe
"same subject" prefix, so these tests also pin that dedupe is unaffected.
"""

from __future__ import annotations

import json

import pytest

from adk.decisions import cli as decisions_cli
from adk.decisions.card_recipes import build_card
from adk.decisions.store import DecisionCard, DecisionStore, normalize_subject


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AWASK_GROUPING", "0")
    return DecisionStore(tmp_path / "decisions")


@pytest.mark.parametrize("good", [
    "platform", "svc:Secrets", "secret:STRIPE_API_KEY", "repo:Aitherium/AitherOS",
    "gpu:node-1/0", "session:3f1e-aa.b", "routine:pr_review_retry", "agent:demiurge@x",
])
def test_normalize_subject_accepts_the_id_grammar(good):
    assert normalize_subject(good) == good
    assert normalize_subject(f"  {good}  ") == good


@pytest.mark.parametrize("bad", [
    "", "Svc:Secrets", "svc:", "svc:has space", "nokind", "platform:", "svc:a;b",
    "svc:" + "x" * 161, None, 42, ["svc:Secrets"],
])
def test_normalize_subject_rejects_everything_else(bad):
    assert normalize_subject(bad) == ""


def test_subject_round_trips_through_the_store(store):
    card = store.create(DecisionCard(id="", title="Secrets is down", kind="info",
                                     subject="svc:Secrets"))
    assert card.subject == "svc:Secrets"
    again = store.get(card.id)
    assert again is not None and again.subject == "svc:Secrets"
    assert DecisionCard.from_dict(card.to_dict()).subject == "svc:Secrets"
    raw = json.loads((store.path / f"{card.id}.json").read_text(encoding="utf-8"))
    assert raw["subject"] == "svc:Secrets"


def test_invalid_subject_is_stored_as_empty(store):
    card = store.create(DecisionCard(id="", title="x", kind="info", subject="Not An Id"))
    assert card.subject == ""
    assert store.get(card.id).subject == ""
    # A hand-edited file cannot smuggle an off-grammar subject past from_dict.
    assert DecisionCard.from_dict({"id": "d-abcd", "subject": "../etc"}).subject == ""


def test_card_without_subject_reads_as_empty():
    assert DecisionCard.from_dict({"id": "d-abcd", "title": "old card"}).subject == ""


def test_credential_defaults_subject_to_its_secret(store):
    card = store.create(DecisionCard(
        id="", title="Enter key", kind="credential", secret_name="STRIPE_API_KEY",
        credential_format="api_key", credential_scope="platform",
        credential_description="billing"))
    assert card.subject == "secret:STRIPE_API_KEY"


def test_credential_keeps_an_explicit_subject(store):
    card = store.create(DecisionCard(
        id="", title="Enter key", kind="credential", secret_name="STRIPE_API_KEY",
        credential_format="api_key", credential_scope="platform",
        credential_description="billing", subject="svc:Commerce"))
    assert card.subject == "svc:Commerce"


def test_subject_plays_no_part_in_dedupe(store):
    first = store.create(DecisionCard(id="", title="a", kind="info",
                                      dedupe_key="probe:one", subject="svc:A"))
    second = store.create(DecisionCard(id="", title="b", kind="info",
                                       dedupe_key="probe:one", subject="svc:B"))
    assert second.id == first.id  # same key = same card, whatever the subject
    third = store.create(DecisionCard(id="", title="c", kind="info",
                                      dedupe_key="probe:two", subject="svc:A"))
    assert third.id != first.id  # same subject, different key = a new card


def test_wake_failed_recipe_sets_a_routine_subject():
    card = build_card("wake-failed", {"job": "nightly-sync", "n": "3",
                                      "first_failure_ts": "2026-10-15T00:00:00+00:00"})
    assert card.subject == "routine:nightly-sync"


def test_cli_subject_flag_sets_it(store, monkeypatch, capsys):
    class _Quiet:
        def describe(self):
            return "notify stubbed"

    monkeypatch.setattr(decisions_cli, "notify", lambda card, store=None: _Quiet())
    monkeypatch.setattr(decisions_cli, "DecisionStore", lambda *a, **k: store)
    rc = decisions_cli.main(["ask", "Secrets unreachable", "--kind", "info",
                             "--subject", "svc:Secrets", "--json", "--quiet"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["card"]["subject"] == "svc:Secrets"
    assert store.get(out["id"]).subject == "svc:Secrets"


def test_cli_without_subject_flag_leaves_it_empty(store, monkeypatch, capsys):
    class _Quiet:
        def describe(self):
            return "notify stubbed"

    monkeypatch.setattr(decisions_cli, "notify", lambda card, store=None: _Quiet())
    monkeypatch.setattr(decisions_cli, "DecisionStore", lambda *a, **k: store)
    rc = decisions_cli.main(["ask", "plain", "--kind", "info", "--json", "--quiet"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["card"]["subject"] == ""


def test_daemon_raise_passes_subject_through(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", "test-token-for-testing-only")
    monkeypatch.setenv("AWASK_GROUPING", "0")
    from adk.harnesses.daemon import create_app

    client = TestClient(create_app())
    client.headers = {"Authorization": "Bearer test-token-for-testing-only"}
    resp = client.post("/decisions", json={"title": "Secrets down", "kind": "info",
                                           "subject": "svc:Secrets"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    card = body.get("card") or body
    assert card["subject"] == "svc:Secrets"
