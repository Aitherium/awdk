"""A credential ask reaches the owner's DM and says WHICH secret and WHY.

Measured 2026-10-14: channels.json pins Discord at min_urgency=high (the owner's
choice, so option cards stop pinging him), and credential cards are raised at the
default "normal" -- so every secret ask was blocked from the DM, and the one that
did render said nothing about the secret. The floor stays for every other kind.
"""
from __future__ import annotations

import pytest

from adk.decisions import channels as ch
from adk.decisions.store import DecisionCard, DecisionOption, DecisionSource


@pytest.fixture(autouse=True)
def _away(monkeypatch):
    # presence must not decide these tests: the owner is "away", the popup is off
    import adk.decisions.notify as notify
    import adk.decisions.presence as presence

    monkeypatch.setattr(presence, "is_at_desk", lambda: False)
    monkeypatch.setattr(notify, "popup_enabled", lambda: False)


def _discord_high() -> ch.ChannelConfig:
    return ch.ChannelConfig.from_dict("discord", {
        "enabled": True, "owner_user_id": "123", "min_urgency": "high",
    })


def _credential(urgency: str = "normal") -> DecisionCard:
    return DecisionCard(
        id="d-nwh4", title="Proton Bridge password", kind="credential",
        urgency=urgency, options=[],
        secret_name="PROTON_BRIDGE_PASSWORD", credential_format="password",
        credential_scope="platform",
        credential_description="SMTP relay re-login after a Bridge restart",
        source=DecisionSource(session_id="s-1", agent="mail-doctor"),
    )


def test_normal_credential_card_is_delivered_past_a_high_floor():
    bridge = ch.DecisionChannelBridge(configs={"discord": _discord_high()})
    assert bridge.should_deliver(_credential("normal"), _discord_high()) is True


def test_normal_option_card_is_still_blocked_by_the_high_floor():
    card = DecisionCard(id="d-abcd", title="Ship it?", urgency="normal",
                        options=[DecisionOption(key="y", label="Yes"),
                                 DecisionOption(key="n", label="No")],
                        default_key="n")
    bridge = ch.DecisionChannelBridge(configs={"discord": _discord_high()})
    assert bridge.should_deliver(card, _discord_high()) is False


def test_render_names_the_secret_scope_and_reason_and_links_the_prompt(monkeypatch):
    monkeypatch.delenv(ch.DECIDE_BASE_ENV, raising=False)
    text = ch.render_for_chat(_credential())
    assert "SECRET NEEDED: PROTON_BRIDGE_PASSWORD (platform)" in text
    assert "SMTP relay re-login after a Bridge restart" in text
    assert "Opens on your PC: https://app.aitherium.com/decide?id=d-nwh4" in text
    assert "mail-doctor" in text
    assert "d-nwh4" in text


def test_render_never_carries_a_value_field():
    card = _credential()
    # a value can only reach the card if a caller put one in a free-text field;
    # the chat render must not echo any of them
    card.answer_note = "hunter2-value"
    card.summary = ""
    text = ch.render_for_chat(card)
    assert "hunter2-value" not in text
    for field in ("value:", "value=", "password:", "answer_note"):
        assert field not in text.lower()


# ── the daemon's /decisions/{id}/prompt: the remote surfaces' one button ─────────


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("AITHER_STEER_DIR", str(tmp_path / "steer"))
    monkeypatch.setenv("AITHER_HARNESS_TOKEN", "test-token-prompt")
    from adk.decisions.store import get_store
    from adk.harnesses.daemon import create_app

    client = TestClient(create_app())
    client.headers = {"Authorization": "Bearer test-token-prompt"}
    return client, get_store()


def test_prompt_route_launches_once_and_carries_no_value(daemon, monkeypatch):
    client, store = daemon
    store.create(_credential())
    launched = []
    import adk.decisions.secure_prompt as sp

    monkeypatch.setattr(sp, "launch_gui_prompt",
                        lambda card: (launched.append(card.id) or True, "masked prompt launched"))
    r = client.post("/decisions/d-nwh4/prompt", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["launched"] is True and body["secret_name"] == "PROTON_BRIDGE_PASSWORD"
    assert set(body) == {"id", "launched", "detail", "secret_name"}
    # a second tap from another surface does not open a second window
    again = client.post("/decisions/d-nwh4/prompt", json={})
    assert again.json()["launched"] is True and launched == ["d-nwh4"]


def test_prompt_route_under_pytest_never_opens_a_window(daemon):
    client, store = daemon
    store.create(_credential())
    body = client.post("/decisions/d-nwh4/prompt", json={}).json()
    assert body["launched"] is False and "suppressed under test" in body["detail"]


def test_prompt_route_refuses_other_kinds_and_unknown_cards(daemon):
    client, store = daemon
    store.create(DecisionCard(id="d-abcd", title="Ship it?",
                              options=[DecisionOption(key="y", label="Yes")], default_key="y"))
    assert client.post("/decisions/d-abcd/prompt", json={}).status_code == 409
    assert client.post("/decisions/d-zzzz/prompt", json={}).status_code == 404
    assert client.post("/decisions/d-nwh4/prompt", json={},
                       headers={"Authorization": "Bearer wrong"}).status_code in (401, 403)


def test_non_credential_render_is_unchanged():
    card = DecisionCard(id="d-abcd", title="Ship it?", urgency="high",
                        options=[DecisionOption(key="y", label="Yes")], default_key="y")
    text = ch.render_for_chat(card)
    assert text.startswith("**Ship it?**") and "SECRET NEEDED" not in text
