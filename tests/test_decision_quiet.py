"""Quiet mode on the SOURCE tree (adk.decisions) -- the awask twin is generated.

Owner, 2026-09-23: decision cards kept popping up over full-screen games. The
contract: ``quiet = doNotDisturb OR (quietWhenFullscreen AND busy)``, and a held
card is HELD, never dropped. The window scenarios (withdrawn-then-shown, no
re-lift, unpin) run against the generated package in
``AitherOS/packages/awask/tests/test_quiet.py``; this file pins the non-Tk gates
where the code is written, so an edit here cannot pass on the twin alone.

No test depends on the real screen: ``AITHER_QUIET`` or a stubbed probe decides.
"""

from __future__ import annotations

import json
import sys

import pytest

from adk.decisions import quiet as quiet_mod
from adk.decisions.store import STATUS_OPEN, DecisionCard, DecisionOption, DecisionStore

SHIP = DecisionOption(key="ship", label="Ship it", consequence="live in ~2 min")
HOLD = DecisionOption(key="hold", label="Hold", consequence="blocks the release")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AITHER_DECISIONS_DIR", str(tmp_path / "decisions"))
    monkeypatch.setenv("DESK_CAST_FILE", str(tmp_path / "cast.json"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for name in ("AITHER_QUIET", "AITHER_DECISIONS_POPUP", "AITHER_DECISIONS_WEBHOOK",
                 "AITHER_DECISIONS_IGNORE_PRESENCE", "AITHER_DECISIONS_TOAST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(quiet_mod, "_busy", lambda: (False, ""))
    return tmp_path


def _store(tmp_path) -> DecisionStore:
    return DecisionStore(tmp_path / "decisions")


def test_rule_dnd_fullscreen_and_env_override(tmp_path, monkeypatch):
    cast = tmp_path / "cast.json"
    assert quiet_mod.is_quiet() == (False, "")
    monkeypatch.setattr(quiet_mod, "_busy", lambda: (True, "a full-screen game"))
    assert quiet_mod.is_quiet() == (True, "a full-screen game")
    cast.write_text(json.dumps({"input": {"quietWhenFullscreen": False}}), encoding="utf-8")
    assert quiet_mod.is_quiet() == (False, "")
    cast.write_text(json.dumps({"input": {"doNotDisturb": True}}), encoding="utf-8")
    assert quiet_mod.is_quiet() == (True, "Do not disturb is on")
    monkeypatch.setenv("AITHER_QUIET", "0")
    assert quiet_mod.is_quiet() == (False, "")


def test_probe_error_is_not_quiet(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(quiet_mod, "read_prefs", boom)
    assert quiet_mod.is_quiet() == (False, "")


def test_popup_and_toast_are_held_and_the_card_stays_open(tmp_path, monkeypatch):
    from adk.decisions import notify

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("AITHER_QUIET", "0")
    assert notify.popup_enabled() is True
    monkeypatch.setenv("AITHER_QUIET", "1")
    assert notify.popup_enabled() is False

    store = _store(tmp_path)
    card = store.create(DecisionCard(id="", title="Ship now or hold?",
                                     options=[SHIP, HOLD], default_key="hold"))
    spawned: list[str] = []
    monkeypatch.setattr(notify, "open_card_window", lambda cid: spawned.append(cid))
    result = notify.notify(card, store)
    assert spawned == []
    assert any("held while quiet" in s for s in result.skipped), result.skipped
    assert store.get(card.id).status == STATUS_OPEN

    sent: list[tuple] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(notify, "_linux_toast", lambda *a, **k: sent.append((a, k)))
    monkeypatch.setenv("AITHER_DECISIONS_TOAST", "1")
    assert "held while quiet" in (notify.native_toast("t", "b") or "")
    assert sent == []


def test_credential_prompt_is_held(tmp_path, monkeypatch):
    from adk.decisions import secure_prompt

    store = _store(tmp_path)
    card = store.create(DecisionCard(id="", kind="credential", title="Need the token",
                                     secret_name="DEPLOY_TOKEN",
                                     credential_format="api_key",
                                     credential_description="the deploy lane needs it"))
    monkeypatch.setenv("AITHER_QUIET", "1")
    launched, why = secure_prompt.launch_gui_prompt(card)
    assert launched is False and why.startswith("held while quiet: ")
    assert f"adk decide answer {card.id}" in why
    assert store.get(card.id).status == STATUS_OPEN


class _FakeWinDLL:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.user32 = self
        self.kernel32 = self

    def __getattr__(self, name: str):
        def call(*_a, **_k):
            self.calls.append(name)
            return 1 if name == "SetForegroundWindow" else 0
        return call


def test_focus_window_is_skipped_while_quiet(monkeypatch):
    from adk.decisions import winproc

    fake = _FakeWinDLL()
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    monkeypatch.setattr(winproc.ctypes, "windll", fake, raising=False)
    monkeypatch.setenv("AITHER_QUIET", "1")
    assert winproc.focus_window(1234) is False
    assert fake.calls == []
    monkeypatch.setenv("AITHER_QUIET", "0")
    assert winproc.focus_window(1234) is True
    assert "SetForegroundWindow" in fake.calls
