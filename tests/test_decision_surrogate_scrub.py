"""A card carrying a lone UTF-16 surrogate must not 500 the decision list.

Regression (measured 2026-09-27): one answered card whose ``answer_note`` held
half of an emoji made ``GET /decisions?status=answered`` raise
``PydanticSerializationError`` for every caller; Genesis surfaced it as a 503 on
``/api/v1/decisions`` and the owner's decision surface went dark.
"""

import json

import pytest
from fastapi.testclient import TestClient

LONE = "\udc9d"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", "tok")
    from adk.harnesses.daemon import create_app

    c = TestClient(create_app())
    c.headers = {"Authorization": "Bearer tok"}
    return c


def _plant_card(tmp_path, status="answered"):
    """Write the card the way the bug left it on disk: escaped surrogate JSON."""
    d = tmp_path / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    card = {
        "id": "d-2n4x", "title": "t", "kind": "decision", "status": status,
        "options": [{"key": "a", "label": "A"}], "default_key": "a",
        "created_at": 1.0, "answer": "a",
        "answer_note": f"terminal reply {LONE} [Image #1]",
    }
    (d / "d-2n4x.json").write_text(json.dumps(card), encoding="utf-8")


def test_scrub_surrogates_replaces_only_lone_halves():
    from adk.decisions.store import scrub_surrogates

    out = scrub_surrogates({"a": [f"x{LONE}y", "ok ✨"], "n": 3})
    assert out["a"][0] == "x?y"
    assert out["a"][1] == "ok ✨"
    assert out["n"] == 3
    json.dumps(out, ensure_ascii=False).encode("utf-8")  # must not raise


def test_list_answered_with_surrogate_card_is_200(client, tmp_path):
    _plant_card(tmp_path)
    r = client.get("/decisions?status=answered")
    assert r.status_code == 200, r.text
    cards = r.json()["decisions"]
    assert len(cards) == 1
    assert LONE not in cards[0]["answer_note"]


def test_write_path_never_persists_a_surrogate(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    from adk.decisions.store import DecisionCard, DecisionOption, get_store

    store = get_store()
    card = DecisionCard(id="d-5bky", title=f"t{LONE}", default_key="a",
                        options=[DecisionOption(key="a", label="A")])
    store.create(card)
    raw = (store.path / "d-5bky.json").read_text(encoding="utf-8")
    assert "\\udc9d" not in raw.lower()
