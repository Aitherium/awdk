"""The world-model federation boundary must REJECT, not merely intend to.

A world model can be federated across colleagues who must NOT share knowledge —
consultants at one firm serving DIFFERENT clients — but only while what crosses
carries no client content: whitelisted tool names and bounded numbers, nothing
else. These tests pin that property at the export boundary.

Each test names the shape it guards against, because every one is reachable from
the CURRENT recording path: ``BuiltinWorldModel.record()`` accepts any non-empty
string as an action and never bounds-checks the state vectors it stores. The
export is therefore the first place the guarantee is actually made, and a
redactor with no failing test is a redactor nobody has watched refuse anything.

Tool and client names here are deliberately generic: this package publishes to
PyPI, so a test naming a real customer would disclose them to every installer.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from adk.worldmodel import BuiltinWorldModel  # noqa: E402

ALLOWED = ["kb_search", "kb_chat"]


@pytest.fixture()
def wm():
    with tempfile.TemporaryDirectory() as td:
        yield BuiltinWorldModel("test-agent", root=td)


def _rec(model, action, before=None, after=None, ok=True):
    """Write a transition through the REAL record() path."""
    dim = model._state_dim
    before = before if before is not None else [0.1] * dim
    after = after if after is not None else [0.2] * dim
    model.record(before, action, after, ok=ok)


def test_no_whitelist_exports_nothing(wm):
    """Fail closed: without an explicit whitelist nothing may leave the machine.

    The tempting default is "export everything the caller recorded", which is
    exactly how an unreviewed action name reaches a shared hub.
    """
    _rec(wm, "kb_search")
    assert wm.export_redacted_transitions() == []
    assert wm.export_redacted_transitions(allowed_actions=[]) == []


def test_unwhitelisted_action_is_dropped(wm):
    """record() takes ANY non-empty string, so the action field is attacker-shaped.

    An action name derived from a filename or a client name is the realistic
    leak here — a plain string that looks like ordinary telemetry.
    """
    _rec(wm, "kb_search")
    _rec(wm, "read_file:/clients/Acme/2026-merger-terms.docx")
    _rec(wm, "search Northwind Hospital retrofit bid")

    out = wm.export_redacted_transitions(allowed_actions=ALLOWED)

    assert len(out) == 1
    assert out[0]["action"] == "kb_search"
    blob = repr(out)
    for leak in ("Acme", "merger", "Northwind", "/clients/"):
        assert leak not in blob


def test_out_of_bounds_state_is_dropped(wm):
    """observe() clamps to [0,1]; a hand-built state is under no such obligation.

    Unbounded floats are a covert channel: arbitrary precision in a numeric
    field can carry arbitrary information off the machine.
    """
    dim = wm._state_dim
    _rec(wm, "kb_search", before=[0.0] * dim, after=[1.0] * dim)
    _rec(wm, "kb_chat", before=[0.0] * dim, after=[42.0] * dim)
    _rec(wm, "kb_chat", before=[-3.5] * dim, after=[0.5] * dim)

    out = wm.export_redacted_transitions(allowed_actions=ALLOWED)

    assert len(out) == 1
    assert out[0]["action"] == "kb_search"
    for row in out:
        for vec in (row["state_before"], row["state_after"]):
            assert all(0.0 <= v <= 1.0 for v in vec)


def test_export_carries_no_unexpected_keys(wm):
    """A future writer adding a key to the transition dict must not widen the export.

    This is the regression that turns a redactor into a passthrough silently:
    the redaction still 'runs', and the new field rides along.
    """
    _rec(wm, "kb_search")
    wm._transitions[0]["prompt"] = "Acme merger due diligence, see /clients/Acme"
    wm._transitions[0]["workspace_id"] = "ws_acme"

    out = wm.export_redacted_transitions(allowed_actions=ALLOWED)

    assert len(out) == 1
    assert set(out[0]) == {"action", "state_before", "state_after", "delta", "ok"}
    assert "Acme" not in repr(out)


def test_happy_path_shape_and_delta(wm):
    """The safe case must actually survive — a filter that drops everything is inert.

    A redactor is only proven by a positive assertion; "returns nothing" passes
    trivially when the feature is broken.
    """
    dim = wm._state_dim
    _rec(wm, "kb_search", before=[0.25] * dim, after=[0.75] * dim, ok=True)

    out = wm.export_redacted_transitions(allowed_actions=ALLOWED)

    assert len(out) == 1
    row = out[0]
    assert row["action"] == "kb_search"
    assert row["ok"] is True
    assert row["delta"] == pytest.approx([0.5] * dim)


def test_limit_returns_most_recent(wm):
    for _ in range(5):
        _rec(wm, "kb_search")
    assert len(wm.export_redacted_transitions(allowed_actions=ALLOWED, limit=2)) == 2


def test_export_never_raises_into_the_agent_loop(wm):
    """A telemetry export must never break the turn it rides along with."""
    _rec(wm, "kb_search")
    wm._transitions.append({"action": "kb_search", "state_before": "not-a-vector"})
    wm._transitions.append({})

    out = wm.export_redacted_transitions(allowed_actions=ALLOWED)

    assert len(out) == 1
    assert out[0]["action"] == "kb_search"
