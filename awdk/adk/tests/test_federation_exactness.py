"""Federation must be EXACT, and must carry aggregates only.

The whole design rests on one claim: summing per-action `(count, sum_delta)`
across agents and dividing once yields exactly the bias a single model trained
on everyone's pooled transitions would have produced. If that is false, the
shared model is a plausible-looking average-of-averages and every argument for
sending aggregates instead of events collapses with it.

So it is not asserted here, it is computed both ways and compared.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from adk.worldmodel import WARM_MIN, BuiltinWorldModel  # noqa: E402

ALLOWED = ["kb_search", "kb_chat"]
DIM = 8


def _model(root, name="a"):
    return BuiltinWorldModel(name, root=root)


def _rec(m, action, before, after, ok=True):
    m.record(before, action, after, ok=ok)


def _vec(v):
    return [v] * DIM


def test_merged_bias_equals_a_single_model_trained_on_everything():
    """The load-bearing claim, computed both ways.

    Two agents observe the same action with different deltas and different
    counts. Federating their aggregates must equal training one model on the
    union — including the weighting, which is why the counts are deliberately
    unequal: an unweighted average of averages would agree when counts match and
    silently diverge when they do not.
    """
    with tempfile.TemporaryDirectory() as td:
        a, b, both = _model(td, "a"), _model(td, "b"), _model(td, "both")

        # Agent A: 3 observations moving +0.2; Agent B: 7 moving +0.9.
        obs = [(a, 3, 0.1, 0.3), (b, 7, 0.0, 0.9)]
        for model, n, lo, hi in obs:
            for _ in range(n):
                _rec(model, "kb_search", _vec(lo), _vec(hi))
                _rec(both, "kb_search", _vec(lo), _vec(hi))

        # Federated: sum the exported aggregates, divide once.
        total_count, total_sum = 0, [0.0] * DIM
        for model, _n, _lo, _hi in obs:
            stats = model.export_federation_stats(allowed_actions=ALLOWED)["kb_search"]
            total_count += stats["count"]
            for i in range(DIM):
                total_sum[i] += stats["sum_delta"][i]
        federated = [total_sum[i] / total_count for i in range(DIM)]

        # Ground truth: one model that saw every transition.
        both.bootstrap()
        both._fit_warm()
        direct = both._bias["kb_search"]

        assert total_count == 10
        assert federated == pytest.approx(direct, abs=1e-12)

        # And it is genuinely count-weighted, not a mean of the two means.
        naive = [(0.2 + 0.9) / 2] * DIM
        assert federated[0] != pytest.approx(naive[0], abs=1e-6)


def test_export_is_aggregates_only_and_names_no_event():
    """What leaves the machine is a population, not an occurrence."""
    with tempfile.TemporaryDirectory() as td:
        m = _model(td)
        for _ in range(4):
            _rec(m, "kb_search", _vec(0.1), _vec(0.4))

        stats = m.export_federation_stats(allowed_actions=ALLOWED)

        assert set(stats) == {"kb_search"}
        assert stats["kb_search"]["count"] == 4
        assert len(stats["kb_search"]["sum_delta"]) == DIM

        # No per-event structure. Checked as exact KEYS, not substrings:
        # `sum_delta` is an aggregate and legitimately contains "delta", and a
        # substring test would fail on the correct payload.
        assert set(stats["kb_search"]) == {"count", "sum_delta"}
        for leaked in ("state_before", "state_after", "delta", "ok", "transition"):
            assert leaked not in stats["kb_search"]

        # And the transition buffer itself is never reachable through the export.
        assert not isinstance(stats["kb_search"].get("sum_delta"), dict)


def test_export_fails_closed_without_a_whitelist():
    """No whitelist means no export — the same rule as the redaction boundary."""
    with tempfile.TemporaryDirectory() as td:
        m = _model(td)
        _rec(m, "kb_search", _vec(0.1), _vec(0.2))
        assert m.export_federation_stats() == {}
        assert m.export_federation_stats(allowed_actions=[]) == {}


def test_unwhitelisted_action_never_leaves():
    """record() accepts any string, so the action field is attacker-shaped."""
    with tempfile.TemporaryDirectory() as td:
        m = _model(td)
        _rec(m, "kb_search", _vec(0.1), _vec(0.2))
        _rec(m, "read_file:/clients/Acme/merger.docx", _vec(0.1), _vec(0.2))

        stats = m.export_federation_stats(allowed_actions=ALLOWED)

        assert set(stats) == {"kb_search"}
        assert "Acme" not in repr(stats)


def test_unbounded_states_are_dropped_not_averaged_in():
    """|sum_delta| <= count is an identity for states in [0,1].

    A value outside it came from an unclamped state, and averaging it in would
    move the FIRM's bias — one bad client distorting everyone's model.
    """
    with tempfile.TemporaryDirectory() as td:
        m = _model(td)
        _rec(m, "kb_chat", _vec(0.0), _vec(50.0))   # impossible if clamped
        _rec(m, "kb_search", _vec(0.2), _vec(0.4))  # fine

        stats = m.export_federation_stats(allowed_actions=ALLOWED)

        assert set(stats) == {"kb_search"}


def test_adopting_a_bias_never_overwrites_first_hand_experience():
    """Local evidence wins, or every agent converges on the mean.

    Federation is a PRIOR for tools this agent barely knows, not a correction
    for the ones it uses daily.
    """
    with tempfile.TemporaryDirectory() as td:
        m = _model(td)
        for _ in range(WARM_MIN + 1):
            _rec(m, "kb_search", _vec(0.1), _vec(0.2))
        m.bootstrap()
        m._fit_warm()
        own = list(m._bias["kb_search"])

        adopted = m.apply_federated_bias({
            "kb_search": _vec(0.99),   # experienced -> must be ignored
            "kb_chat": _vec(0.42),     # never seen  -> must be adopted
        })

        assert adopted == 1
        assert m._bias["kb_search"] == own
        assert m._bias["kb_chat"] == pytest.approx(_vec(0.42))


def test_adopt_rejects_malformed_bias_without_raising():
    """Telemetry must never break the turn it rides along with."""
    with tempfile.TemporaryDirectory() as td:
        m = _model(td)
        adopted = m.apply_federated_bias({
            "wrong_width": [0.1, 0.2],
            "not_a_list": "0.5",
            "bools": [True] * DIM,
            "good": _vec(0.3),
        })
        assert adopted == 1
        assert "good" in m._bias
        assert "wrong_width" not in m._bias


def test_full_cycle_pushes_aggregates_and_adopts_the_merge(monkeypatch):
    """End to end against a stand-in hub.

    Proves the two halves actually connect. Each half is tested above; this is
    the one that fails if the client sends the wrong shape or never applies what
    it pulled — the inert-feature case, where every unit passes and the feature
    does nothing.
    """
    from adk.sync import federation as fed

    sent = {}

    def fake_request(self, method, path, payload=None):
        if method == "POST":
            sent["path"] = path
            sent["body"] = payload
            return {"ok": True, "accepted_actions": len(payload["stats"])}
        return {"ok": True, "bias": {"kb_chat": _vec(0.42)},
                "actions": 1, "contributors": 3}

    monkeypatch.setattr(fed.FederationClient, "_request", fake_request)

    with tempfile.TemporaryDirectory() as td:
        m = _model(td)
        for _ in range(5):
            _rec(m, "kb_search", _vec(0.1), _vec(0.3))

        out = fed.FederationClient(bearer="Bearer x").sync(m, allowed_actions=ALLOWED)

        assert out["ok"]
        assert sent["path"] == "/brain/federation/contribute"
        # Only the whitelisted action, and only aggregates.
        assert list(sent["body"]["stats"]) == ["kb_search"]
        assert sorted(sent["body"]["stats"]["kb_search"]) == ["count", "sum_delta"]
        # Nothing resembling an event crossed the wire.
        for leaked in ("state_before", "state_after", "transitions"):
            assert leaked not in repr(sent["body"])
        # And the pulled bias was actually applied, not just received.
        assert out["adopted"]["adopted_actions"] == 1
        assert m._bias["kb_chat"] == pytest.approx(_vec(0.42))


def test_cycle_survives_a_hub_that_is_down(monkeypatch):
    """A hub outage must cost the agent nothing — and must not look like success."""
    from adk.sync import federation as fed

    monkeypatch.setattr(
        fed.FederationClient, "_request",
        lambda self, m, p, payload=None: {"ok": False, "error": "connection refused"})

    with tempfile.TemporaryDirectory() as td:
        m = _model(td)
        _rec(m, "kb_search", _vec(0.1), _vec(0.3))
        out = fed.FederationClient(bearer="Bearer x").sync(m, allowed_actions=ALLOWED)

    assert out["ok"] is False, "a failed sync must not report success"
    assert out["contributed"]["ok"] is False
