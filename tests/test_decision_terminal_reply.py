"""Free text never answers a card (d-9vph, 2026-11-16).

The owner typed an unrelated angry message in a Claude Code tab; the
UserPromptSubmit hook closed card d-9vph as its DEFAULT ("hold") before any
screen had shown it. A terminal reply answers a card only when it NAMES an
option -- by key or label -- and a miss leaves the card OPEN.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "awdk"))

from adk.decisions.store import (  # noqa: E402
    DecisionCard,
    DecisionError,
    DecisionOption,
    DecisionSource,
    DecisionStore,
    reply_choice,
)

#: The exact note text from d-9vph.
D9VPH_NOTE = (
    "dude hwya re fairies sitll not fuckign getting my attnetion with thsi shti!??!?!?! "
    "i keep fuckign askin for tha tot be fixed ther eis no fckign decision card"
    "!!!!!!!!!!!!!!!"
)

OPTIONS = [
    DecisionOption(key="send-the-redline-to-roger", label="Send the redline to Roger"),
    DecisionOption(key="sign-the-original-as-is", label="Sign the original as is"),
    DecisionOption(key="hold", label="Hold"),
]


def _hook():
    path = ROOT / ".claude" / "hooks" / "record-owner-reply.py"
    spec = importlib.util.spec_from_file_location("record_owner_reply", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def store(tmp_path):
    return DecisionStore(tmp_path / "decisions")


def _raise(store, session="S1"):
    card = DecisionCard(
        id="d-9vph", title="Roger NDA: send him the redline?", kind="approve",
        options=list(OPTIONS), default_key="Hold",
        source=DecisionSource(session_id=session),
        created_at=time.time() - 60,
    )
    return store.create(card)


def test_d9vph_note_names_no_option():
    assert reply_choice(D9VPH_NOTE, OPTIONS, "d-9vph") is None


def test_hook_leaves_card_open_for_the_d9vph_message(store, tmp_path):
    card = _raise(store)
    out = _hook().record({"prompt": D9VPH_NOTE, "session_id": "S1"},
                         tmp_path / "replies.jsonl", store)
    assert out["answered"] == []
    assert store.get(card.id).status == "open"


def test_store_refuses_a_terminal_reply_that_names_nothing(store):
    card = _raise(store)
    with pytest.raises(DecisionError):
        store.answer(card.id, "hold", note=f"terminal reply: {D9VPH_NOTE}",
                     via="terminal", deliver=False)
    assert store.get(card.id).status == "open"


@pytest.mark.parametrize("reply, want", [
    ("hold", "hold"),
    ("Hold.", "hold"),
    ("send the redline to roger", "send-the-redline-to-roger"),
    ("d-9vph sign the original as is", "sign-the-original-as-is"),
    ("yes do it", None),
    ("sure go ahead", None),
    ("later", None),
])
def test_reply_choice_rule(reply, want):
    assert reply_choice(reply, OPTIONS, "d-9vph") == want


def test_a_reply_naming_an_option_answers_and_keeps_the_words(store, tmp_path):
    card = _raise(store)
    out = _hook().record({"prompt": "hold", "session_id": "S1"},
                         tmp_path / "replies.jsonl", store)
    assert out["answered"] == [card.id]
    done = store.get(card.id)
    assert done.status == "answered" and done.answer == "hold"
    assert done.answered_via == "terminal"
