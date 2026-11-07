"""A remote "Open secure prompt on my PC" tap must focus THAT card, and say so honestly.

2026-11-01: the phone said "The secure prompt is open on your PC. Type the secret
there." for X402_SQUADS_MEMBER_PUBKEYS_SOLANA while the desk showed only the Inbox
list: the raise named no card, and a failed raise still reported surface "desk".
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from adk.decisions import notify
from adk.decisions import secure_prompt


def _card(cid: str = "d-abc123") -> SimpleNamespace:
    return SimpleNamespace(id=cid, kind="credential", secret_name="X_NAME")


def test_owner_tap_raises_the_inbox_on_the_named_card(monkeypatch):
    asked: list = []
    monkeypatch.setattr(notify, "desk_alive", lambda: True)
    monkeypatch.setattr(notify, "raise_desk_inbox",
                        lambda timeout=4.0, card_id=None: asked.append(card_id))
    shown = secure_prompt.open_credential_surface(_card(), owner_initiated=True)
    assert asked == ["d-abc123"]
    assert shown.surface == "desk"
    assert "d-abc123" in shown.detail


def test_failed_raise_is_not_reported_as_open(monkeypatch):
    monkeypatch.setattr(notify, "desk_alive", lambda: True)
    monkeypatch.setattr(notify, "raise_desk_inbox",
                        lambda timeout=4.0, card_id=None: "desk bridge unreachable: refused")
    shown = secure_prompt.open_credential_surface(_card(), owner_initiated=True)
    assert shown.surface != "desk"
    assert "could not be brought forward" in shown.detail


def test_raise_sends_the_card_and_reads_a_refusal(monkeypatch):
    sent: dict = {}

    class _Resp:
        status = 200

        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=0):
        sent["body"] = json.loads(req.data)
        return _Resp(b'{"ok": false, "error": "Do not disturb"}')

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("AITHER_NO_GUI_PROMPT", raising=False)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    error = notify.raise_desk_inbox(card_id="d-abc123")
    assert sent["body"] == {"pane": "inbox", "card": "d-abc123"}
    assert error and "Do not disturb" in error
