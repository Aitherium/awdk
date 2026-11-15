"""Decision cards carry generated UI (A2UI v0.9): ``adk.decisions.card_ui`` + the store.

Pins: a ``ui`` survives the store round-trip; a credential card never serves one;
a Button whose event names no option is refused (store, ``set_ui`` and the daemon
422); a long label compiles to one full-width Button carrying the WHOLE label;
``card_hash`` moves when the card is revised and ``set_ui`` compare-and-sets on it.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-for-testing-only"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", TOKEN)
    # The daemon raise below reaches notify(): never put a real card window on
    # the owner's desktop from a test.
    monkeypatch.setenv("AITHER_DECISIONS_POPUP", "0")
    monkeypatch.setenv("AITHER_NO_GUI_PROMPT", "1")
    import adk.decisions.store as store_module

    monkeypatch.setattr(store_module, "_STORE", None)
    return store_module.get_store()


def _card(label: str = "Ship it", **kw):
    from adk.decisions.store import DecisionCard, DecisionOption

    return DecisionCard(
        id="", title=kw.pop("title", "Ship the build?"), summary="tests are green",
        kind="decision", default_key="hold", facts=["12 tests pass", "no lint"],
        options=[DecisionOption(key="ship", label=label, recommended=True),
                 DecisionOption(key="hold", label="Hold it")], **kw)


def _buttons(ui):
    comps = ui["messages"][1]["updateComponents"]["components"]
    return [c for c in comps if c["component"] == "Button"], comps


def test_ui_survives_the_store_round_trip(store):
    from adk.decisions import card_ui
    from adk.decisions.store import DecisionCard

    card = _card()
    card.ui = card_ui.compile_default(card)
    created = store.create(card)
    again = store.get(created.id)
    assert again.ui == card.ui
    assert DecisionCard.from_dict(again.to_dict()).ui == card.ui
    assert card_ui.validate_bindings(again) == []
    assert card_ui.is_current(again)


def test_credential_card_strips_ui_at_the_boundary_and_refuses_it_on_write(store):
    from adk.decisions.store import DecisionCard, DecisionError

    cred = DecisionCard(id="d-cred", title="Need key", kind="credential",
                        secret_name="X_API_KEY", credential_format="api_key",
                        credential_description="for X")
    cred.ui = {"a2ui": "v0.9", "messages": []}
    assert cred.to_dict()["ui"] is not None          # persistence is verbatim
    assert cred.to_dict_safe()["ui"] is None          # the boundary never serves it
    with pytest.raises(DecisionError, match="must not carry ui"):
        store.create(cred)


def test_unknown_event_is_refused_everywhere(store):
    from adk.decisions import card_ui
    from adk.decisions.store import DecisionError

    card = _card()
    ui = card_ui.compile_default(card)
    bad = copy.deepcopy(ui)
    buttons, _ = _buttons(bad)
    buttons[0]["action"]["event"]["name"] = "answer:delete_everything"
    card.ui = bad
    problems = card_ui.validate_bindings(card)
    assert problems and "delete_everything" in problems[0]
    with pytest.raises(DecisionError, match="does not bind"):
        store.create(card)
    # Not an answer event at all, and an answer carrying a payload.
    buttons[0]["action"]["event"] = {"name": "lights_on"}
    assert card_ui.validate_bindings(card)
    buttons[0]["action"]["event"] = {"name": "answer:ship", "context": {"x": 1}}
    assert card_ui.validate_bindings(card)


def test_subset_cap_and_url_are_refused():
    from adk.decisions import card_ui

    card = _card()
    ui = card_ui.compile_default(card)
    comps = ui["messages"][1]["updateComponents"]["components"]
    for mutate, needle in [
        (lambda c: c.append({"id": "f", "component": "TextField", "label": "x"}),
         "TextField"),
        (lambda c: c[1].__setitem__("text", "see https://evil.example/x"), "URL"),
        (lambda c: c.extend({"id": f"x{i}", "component": "Text", "text": "."}
                            for i in range(30)), "max 30"),
    ]:
        trial = copy.deepcopy(comps)
        mutate(trial)
        card.ui = {**ui, "messages": [ui["messages"][0], {
            "version": "v0.9",
            "updateComponents": {"surfaceId": ui["messages"][0]["createSurface"]["surfaceId"],
                                 "components": trial}}]}
        assert any(needle in p for p in card_ui.validate_bindings(card)), needle
    card.ui = {**ui, "messages": [{"version": "v0.9", "createSurface": {
        "surfaceId": "s", "catalogId": "https://example.com/other.json"}}]}
    assert any("Basic catalog" in p for p in card_ui.validate_bindings(card))


def test_long_label_compiles_to_one_full_width_wrapping_button():
    from adk.decisions import card_ui

    label = ("Allow the one-off rental of a GPU for the nightly evaluation run, "
             "capped at $40.00")
    assert len(label) >= 80
    ui = card_ui.compile_default(_card(label=label))
    buttons, comps = _buttons(ui)
    root = next(c for c in comps if c["id"] == "root")
    ship = next(b for b in buttons if b["action"]["event"]["name"] == "answer:ship")
    assert ship["text"] == label                         # never cut
    assert ship["id"] in root["children"]                # alone on its line, not in a Row
    assert root["component"] == "Column" and root["align"] == "stretch"
    assert ship["variant"] == "primary"
    assert [b["action"]["event"]["name"] for b in buttons] == ["answer:ship", "answer:hold"]
    assert f"[ship] {label}" in card_ui.to_text(ui)


def test_card_hash_changes_on_revise_and_set_ui_compare_and_sets(store):
    from adk.decisions import card_ui
    from adk.decisions.store import StaleCardUIError

    created = store.create(_card())
    before = card_ui.card_hash(created)
    ui = card_ui.compile_default(created)
    revised = store.revise(created.id, title="Ship the build NOW?")
    after = card_ui.card_hash(revised)
    assert after != before
    with pytest.raises(StaleCardUIError):
        store.set_ui(created.id, ui, before)
    assert store.get(created.id).ui is None
    fresh = card_ui.compile_default(revised)
    out = store.set_ui(created.id, fresh, after)
    assert out.ui["card_hash"] == after and card_ui.is_current(store.get(created.id))


def test_daemon_raise_carries_ui_and_422s_a_bad_one(store):
    from adk.decisions import card_ui
    from adk.harnesses.daemon import create_app

    client = TestClient(create_app())
    client.headers = {"Authorization": f"Bearer {TOKEN}"}
    body = {"title": "Ship the build?", "kind": "decision", "default": "hold",
            "options": [{"key": "ship", "label": "Ship it"},
                        {"key": "hold", "label": "Hold it"}],
            "recipe_vars": {"approved_monthly_usd": "12.00"}}
    body["ui"] = card_ui.compile_default(body)
    r = client.post("/decisions", json=body)
    assert r.status_code == 200, r.text
    stored = store.get(r.json()["id"])
    assert stored.ui == body["ui"]
    assert stored.recipe_vars == {"approved_monthly_usd": "12.00"}
    bad = copy.deepcopy(body)
    bad["dedupe_key"] = "card-ui:bad"
    _buttons(bad["ui"])[0][0]["action"]["event"]["name"] = "answer:nope"
    r = client.post("/decisions", json=bad)
    assert r.status_code == 422 and "nope" in r.json()["detail"]
