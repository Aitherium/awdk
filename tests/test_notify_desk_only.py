"""A live awdesk must not stop the card window: the owner wants cards IN FRONT (2026-11-07).

This is the SOURCE half of the rule. The behaviour first shipped as a hand edit in the
GENERATED ``notify.py`` of the standalone ``awask`` package -- a file whose own header says a
hand edit is reverted by the next regeneration -- so the rule lives here now, where the
generator reads it, and the generated copy is pinned to this by AWK001.
"""

from __future__ import annotations

from adk.decisions import notify as n
from adk.decisions.store import DecisionCard, DecisionOption


def _card() -> DecisionCard:
    return DecisionCard(id="d-test", title="Pick one", kind="decision",
                        options=[DecisionOption(key="a", label="A"),
                                 DecisionOption(key="b", label="B")],
                        default_key="a")


def _wire(monkeypatch, tmp_path, *, desk: bool, desk_only: bool):
    monkeypatch.setattr(n.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(n, "desk_alive", lambda *a, **k: desk)
    monkeypatch.setattr(n, "popup_enabled", lambda: True)
    monkeypatch.setattr(n, "native_toast", lambda *a, **k: "off in test")
    monkeypatch.setattr(n, "_webhook_post", lambda *a, **k: "off in test")
    opened = []
    monkeypatch.setattr(n, "open_card_window", lambda cid: opened.append(cid) or None)
    if desk_only:
        (tmp_path / ".aither" / "decisions").mkdir(parents=True)
        (tmp_path / ".aither" / "decisions" / ".desk-only").write_text("", encoding="utf-8")
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "store"))
    return opened


def test_live_desk_still_opens_the_window(monkeypatch, tmp_path):
    opened = _wire(monkeypatch, tmp_path, desk=True, desk_only=False)
    res = n.notify(_card())
    assert "awdesk" in res.delivered and "card window" in res.delivered
    assert opened == ["d-test"]


def test_desk_only_file_restores_one_surface(monkeypatch, tmp_path):
    opened = _wire(monkeypatch, tmp_path, desk=True, desk_only=True)
    res = n.notify(_card())
    assert res.delivered == ["awdesk"] and opened == []
    assert any("desk-only" in s for s in res.skipped)


def test_no_desk_opens_the_window(monkeypatch, tmp_path):
    opened = _wire(monkeypatch, tmp_path, desk=False, desk_only=False)
    res = n.notify(_card())
    assert "card window" in res.delivered and opened == ["d-test"]


# ── the fairy claim: the desk's in-front surface (owner, 2026-11-08) ──────────
#
# awdesk adds ``"attention": "fairy"`` to its heartbeat ONLY while the fairy is
# visible and running its nudge/escalation engine. A live claim means the card is
# already in front of the owner, so the Tk window is not spawned beside it. Every
# failure (stale, absent, unreadable) must fall TOWARD the window.

_FAIRY_SKIP = "card window (the desk fairy carries it: nudges and escalation)"


def _heartbeat(tmp_path, body: str, *, age_seconds: float = 0.0):
    """Write ``<store>/.desk-alive`` with ``body``, aged by ``age_seconds``."""
    import os

    beat = tmp_path / "store" / n.DESK_ALIVE_FILENAME
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(body, encoding="utf-8")
    if age_seconds:
        stamp = beat.stat().st_mtime - age_seconds
        os.utime(beat, (stamp, stamp))
    return beat


def _wire_real_beat(monkeypatch, tmp_path, *, desk_up_override=None):
    """Like ``_wire`` but desk_alive() reads the REAL heartbeat file, unless overridden."""
    monkeypatch.setattr(n.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(n, "popup_enabled", lambda: True)
    monkeypatch.setattr(n, "native_toast", lambda *a, **k: "off in test")
    monkeypatch.setattr(n, "_webhook_post", lambda *a, **k: "off in test")
    monkeypatch.setattr(n, "_relay_post", lambda *a, **k: "off in test")
    if desk_up_override is not None:
        monkeypatch.setattr(n, "desk_alive", lambda *a, **k: desk_up_override)
    opened = []
    monkeypatch.setattr(n, "open_card_window", lambda cid: opened.append(cid) or None)
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "store"))
    return opened


_FAIRY_BEAT = '{"pid": 4242, "at": "2026-11-08T00:00:00.000Z", "attention": "fairy"}'
_PLAIN_BEAT = '{"pid": 4242, "at": "2026-11-08T00:00:00.000Z"}'


def test_fresh_fairy_claim_carries_the_card_and_spawns_no_window(monkeypatch, tmp_path):
    opened = _wire_real_beat(monkeypatch, tmp_path)
    _heartbeat(tmp_path, _FAIRY_BEAT)
    res = n.notify(_card())
    assert opened == []
    assert res.delivered == ["awdesk", "awdesk fairy"]
    assert _FAIRY_SKIP in res.skipped


def test_stale_fairy_claim_falls_back_to_the_window(monkeypatch, tmp_path):
    opened = _wire_real_beat(monkeypatch, tmp_path)
    _heartbeat(tmp_path, _FAIRY_BEAT, age_seconds=n.DESK_ALIVE_STALE_SECONDS + 5)
    res = n.notify(_card())
    assert opened == ["d-test"] and "card window" in res.delivered
    assert "awdesk fairy" not in res.delivered


def test_stale_claim_is_judged_on_its_own_even_if_the_desk_reads_alive(monkeypatch, tmp_path):
    """The claim carries its own staleness rule: a stale file never suppresses."""
    opened = _wire_real_beat(monkeypatch, tmp_path, desk_up_override=True)
    _heartbeat(tmp_path, _FAIRY_BEAT, age_seconds=n.DESK_ALIVE_STALE_SECONDS + 5)
    res = n.notify(_card())
    assert opened == ["d-test"]
    assert "awdesk" in res.delivered and "awdesk fairy" not in res.delivered


def test_live_desk_without_a_claim_still_opens_the_window(monkeypatch, tmp_path):
    opened = _wire_real_beat(monkeypatch, tmp_path)
    _heartbeat(tmp_path, _PLAIN_BEAT)
    res = n.notify(_card())
    assert opened == ["d-test"]
    assert res.delivered == ["awdesk", "card window"]


def test_unreadable_heartbeat_json_falls_back_to_the_window(monkeypatch, tmp_path):
    opened = _wire_real_beat(monkeypatch, tmp_path)
    _heartbeat(tmp_path, '{"pid": 4242, "attention": "fairy"')   # truncated mid-write
    res = n.notify(_card())
    assert opened == ["d-test"] and "awdesk fairy" not in res.delivered


def test_desk_only_still_wins_over_the_fairy_claim(monkeypatch, tmp_path):
    opened = _wire_real_beat(monkeypatch, tmp_path)
    (tmp_path / ".aither" / "decisions").mkdir(parents=True)
    (tmp_path / ".aither" / "decisions" / ".desk-only").write_text("", encoding="utf-8")
    _heartbeat(tmp_path, _FAIRY_BEAT)
    res = n.notify(_card())
    assert opened == []
    assert any("desk-only" in s for s in res.skipped)
    assert _FAIRY_SKIP not in res.skipped


def test_optionless_card_with_a_fairy_claim_spawns_nothing(monkeypatch, tmp_path):
    opened = _wire_real_beat(monkeypatch, tmp_path)
    _heartbeat(tmp_path, _FAIRY_BEAT)
    card = DecisionCard(id="d-bare", title="waiting for input", kind="decision")
    res = n.notify(card)
    assert opened == [] and "awdesk fairy" in res.delivered


def test_desk_attention_claim_reads_only_a_fresh_string_claim(monkeypatch, tmp_path):
    store = tmp_path / "store"
    assert n.desk_attention_claim(store) == ""                      # absent
    _heartbeat(tmp_path, _FAIRY_BEAT)
    assert n.desk_attention_claim(store) == "fairy"                 # fresh
    _heartbeat(tmp_path, "﻿" + _FAIRY_BEAT)
    assert n.desk_attention_claim(store) == "fairy"                 # a BOM is not a reason
    _heartbeat(tmp_path, _PLAIN_BEAT)
    assert n.desk_attention_claim(store) == ""                      # no claim
    for body in ("not json", '["fairy"]', '{"attention": true}', '{"attention": null}', ""):
        _heartbeat(tmp_path, body)
        assert n.desk_attention_claim(store) == "", body
    _heartbeat(tmp_path, _FAIRY_BEAT, age_seconds=n.DESK_ALIVE_STALE_SECONDS + 1)
    assert n.desk_attention_claim(store) == ""                      # stale
    _heartbeat(tmp_path, '{"attention": "fairy", "pad": "' + "x" * (70 * 1024) + '"}')
    assert n.desk_attention_claim(store) == ""                      # not a heartbeat


def test_desk_attention_claim_defaults_to_the_store_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "store"))
    _heartbeat(tmp_path, _FAIRY_BEAT)
    assert n.desk_attention_claim() == "fairy"


def test_a_test_run_never_opens_a_card_window(monkeypatch):
    # 2026-11-08: fixture cards from a test run popped real windows on the owner's screen.
    import subprocess as sp
    from adk.decisions import notify as n
    spawned = []
    monkeypatch.setattr(sp, "Popen", lambda *a, **k: spawned.append(a))
    assert "under test" in (n.open_card_window("d-test") or "")
    assert spawned == []
