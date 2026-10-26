"""awsupervise: the loop/stall supervisor's own self-test, run under pytest.

The self-test builds synthetic transcripts in a temp tree and drives every detector
(a-f), its negative cases, the L1 -> L2 -> L3 ladder, dedupe, the hourly caps, the
Opus 5.5 recommendation and dry-run purity. Relay and card runs go through a fake
runner and subprocess is fenced off, so no real notification can leave the test.
"""
from __future__ import annotations

from adk.harnesses import awsupervise


def test_self_test_passes():
    assert awsupervise._self_test(quiet=True) == 0


def test_router_failure_classes():
    cfg = awsupervise.CONFIG
    assert awsupervise.router_failure({"upstream_status": 401}, cfg) == "401"
    assert awsupervise.router_failure({"upstream_status": 503}, cfg) == "503"
    timeout = {"upstream_status": 0, "error": "read timed out"}
    refused = {"upstream_status": 0, "error": "ConnectionRefusedError"}
    assert awsupervise.router_failure(timeout, cfg) == "timeout"
    assert awsupervise.router_failure(refused, cfg) == "transport"
    assert awsupervise.router_failure({"upstream_status": 200, "error": ""}, cfg) == ""
    assert awsupervise.router_failure({"upstream_status": 400, "error": ""}, cfg) == ""


def test_cheap_tier_includes_the_opus_alias():
    cfg = awsupervise.CONFIG
    assert awsupervise.is_cheap("deepseek-v4-flash[1m]", cfg)
    assert awsupervise.is_cheap("opus", cfg)  # this tier map points `opus` at DeepSeek
    assert not awsupervise.is_cheap("claude-opus-5-5", cfg)


def test_redaction():
    assert "ghp_" not in awsupervise.redact("token ghp_" + "a" * 30)
    assert awsupervise.redact("plain text stays") == "plain text stays"
