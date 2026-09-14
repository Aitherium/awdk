"""Idle-only lending — both directions, over the REAL functions.

A flag whose decision cannot be tested in both directions is decorative. These
drive ``adk.mesh_provider``'s own eligibility functions rather than a
reimplementation, because a test that reimplements the thing it tests proves
nothing about it.

The asymmetry between "may start" and "must withdraw" is the load-bearing part
and has its own arms: the cooldown governs STARTING and never STOPPING, so a
naive ``not eligible()`` would hold a busy box in the pool for the rest of its
cooldown — precisely the promise "idle-only" makes, broken by the obvious
implementation.
"""

from __future__ import annotations

import pytest
from adk.mesh_provider import (
    idle_lending_config,
    idle_lending_eligible,
    idle_lending_serve_hint,
    idle_lending_should_withdraw,
)

NOW = 1_000_000.0
COOLDOWN_S = 360 * 60


@pytest.fixture
def off() -> dict:
    return idle_lending_config({"enabled": False})


@pytest.fixture
def on() -> dict:
    return idle_lending_config(
        {"enabled": True, "min_idle_minutes": 30.0, "cooldown_minutes": 360.0}
    )


# ── DEFAULT-OFF ──────────────────────────────────────────────────────────────

def test_unset_is_off(monkeypatch):
    """Unset is OFF. Every other toggle in this family defaults ON via
    ``!== false``; copying that idiom here would make lending somebody's GPU the
    shipped default (the BCG009 shape)."""
    monkeypatch.delenv("AITHER_IDLE_LENDING", raising=False)
    assert idle_lending_config()["enabled"] is False


def test_unparseable_flag_is_off(monkeypatch):
    monkeypatch.setenv("AITHER_IDLE_LENDING", "maybe")
    assert idle_lending_config()["enabled"] is False


def test_disabled_never_lends_however_idle(off):
    assert idle_lending_eligible(off, 10_000, last_started_at=0, now=NOW) is False


def test_malformed_threshold_falls_back_not_to_zero(monkeypatch):
    """A malformed threshold must not become 0.0, i.e. 'always idle enough'."""
    monkeypatch.setenv("AITHER_IDLE_LENDING_MIN_IDLE_MINUTES", "soon")
    assert idle_lending_config()["min_idle_minutes"] == 30.0


# ── IDLE ARM, BOTH DIRECTIONS ────────────────────────────────────────────────

def test_enabled_but_not_idle_does_not_lend(on):
    assert idle_lending_eligible(on, 5, last_started_at=0, now=NOW) is False


def test_enabled_just_short_of_threshold_does_not_lend(on):
    assert idle_lending_eligible(on, 29.9, last_started_at=0, now=NOW) is False


def test_enabled_and_idle_past_threshold_and_cooldown_lends(on):
    assert idle_lending_eligible(on, 45, last_started_at=0, now=NOW) is True


# ── The cooldown is real: it stops endpoint flap on a twitchy idle signal. ───

def test_inside_the_cooldown_does_not_lend(on):
    assert idle_lending_eligible(on, 45, last_started_at=NOW - 60, now=NOW) is False


def test_past_the_cooldown_lends(on):
    assert (
        idle_lending_eligible(on, 45, last_started_at=NOW - COOLDOWN_S - 1, now=NOW)
        is True
    )


# ── "Could not measure" is not "idle". ──────────────────────────────────────

@pytest.mark.parametrize("reading", ["unknown", None, -1, float("-1")])
def test_unmeasurable_idle_does_not_lend(on, reading):
    assert idle_lending_eligible(on, reading, last_started_at=0, now=NOW) is False


# ── WITHDRAWAL is not the negation of eligibility. ──────────────────────────

def test_busy_box_withdraws_immediately(on):
    assert idle_lending_should_withdraw(on, 1) is True


def test_idle_box_does_not_withdraw(on):
    assert idle_lending_should_withdraw(on, 45) is False


def test_flag_switched_off_withdraws(off):
    assert idle_lending_should_withdraw(off, 45) is True


@pytest.mark.parametrize("reading", ["unknown", None, -1])
def test_unmeasurable_idle_withdraws(on, reading):
    """If we do not know the box is idle, we do not keep serving on the
    assumption that it is."""
    assert idle_lending_should_withdraw(on, reading) is True


def test_cooldown_does_not_hold_a_busy_box_in_the_pool(on):
    """The arm that pins the asymmetry.

    A symmetric ``not eligible()`` would answer "withdraw" here — the box IS
    idle and merely inside its start cooldown — and would answer "do not
    withdraw" for a busy box that had just started. Both are backwards.
    """
    assert idle_lending_eligible(on, 45, last_started_at=NOW - 60, now=NOW) is False
    assert idle_lending_should_withdraw(on, 45) is False
    assert idle_lending_should_withdraw(on, 1) is True


# ── The emitted serve command. ──────────────────────────────────────────────

def test_serve_hint_binds_loopback_positively():
    """A POSITIVE bind, not merely the absence of 0.0.0.0: a child spawned with
    no --host inherits its runtime's 0.0.0.0 default."""
    hint = idle_lending_serve_hint("gemma4-12b")
    assert "--host 127.0.0.1" in hint
    assert "0.0.0.0" not in hint


def test_serve_hint_survives_an_empty_model():
    assert "<model>" in idle_lending_serve_hint("")
