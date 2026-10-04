"""A card answer reaches the raising session exactly ONCE (owner, 2026-10-04).

When a live tier (harness PTY, /chat/steer, console typing) already put the
answer into the session, the mailbox copy must be archived to ``delivered/`` so
the prompt/tool-use drains do not inject it a second time. When no live tier
landed, the mailbox file stays pending -- that is the delivery path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from adk.decisions import steerback
from adk.decisions.store import DecisionCard, DecisionOption, DecisionSource, DecisionStore


def _raise(store: DecisionStore) -> DecisionCard:
    card = DecisionCard(
        id="",
        title="Ship it?",
        options=[DecisionOption(key="1", label="Yes"), DecisionOption(key="2", label="No")],
        default_key="2",
        source=DecisionSource(session_id="sess-once"),
    )
    return store.create(card)


@pytest.mark.parametrize("landed", [True, False])
def test_answer_lands_once(tmp_path: Path, monkeypatch, landed: bool):
    steer = tmp_path / "steer"
    monkeypatch.setenv("AITHER_STEER_DIR", str(steer))
    monkeypatch.setattr(steerback, "deliver", lambda card, text: (landed, "test tier"))
    store = DecisionStore(tmp_path / "decisions")
    card = _raise(store)
    store.answer(card.id, "1", via="test")
    box = steer / "sess-once"
    pending = sorted(p.name for p in box.glob("*.md"))
    archived = sorted(p.name for p in (box / "delivered").glob("*.md"))
    if landed:
        assert pending == [], "a live delivery must not also leave a pending mailbox file"
        assert len(archived) == 1
    else:
        assert len(pending) == 1, "with no live tier the mailbox IS the delivery"
        assert archived == []
