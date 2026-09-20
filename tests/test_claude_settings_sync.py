"""The merge and the redaction for the coding-agent settings sync.

This module shipped with no tests. The two operations it performs are the two that
can do harm -- sending a credential somewhere, and accepting one -- and both are
pure functions, so they are tested here with no network and no settings file.
"""
from __future__ import annotations

from adk.sync.claude_settings import merge, redact


def test_a_top_level_credential_may_not_arrive():
    out = merge({}, {"apiKeyHelper": "/bin/evil", "env": {"X": "y"}})
    assert "apiKeyHelper" not in out
    assert "env" not in out


def test_a_nested_credential_may_not_arrive_either():
    """The defect: the sub-key denylist ran on the way out and never on the way in."""
    local = {"sandbox": {"enabled": True}}
    remote = {"sandbox": {"credentials": {"envVars": [{"name": "PLANTED"}]},
                          "network": {"allow": ["example.com"]}}}
    out = merge(local, remote)
    assert "credentials" not in out["sandbox"]
    # Refusing the credential block must not cost the rest of the key.
    assert out["sandbox"]["enabled"] is True
    assert out["sandbox"]["network"] == {"allow": ["example.com"]}


def test_a_local_nested_credential_survives_a_pull():
    local = {"sandbox": {"credentials": {"envVars": [{"name": "MINE"}]}}}
    out = merge(local, {"sandbox": {"enabled": True, "credentials": {"envVars": []}}})
    assert out["sandbox"]["credentials"] == {"envVars": [{"name": "MINE"}]}
    assert out["sandbox"]["enabled"] is True


def test_no_credential_leaves_nested_or_top_level():
    # Asserts the property, not the mechanism: whether or not `sandbox` is in the
    # synced set, nothing under it that holds a credential may be in the snapshot.
    red = redact({"sandbox": {"enabled": True, "credentials": {"envVars": []}},
                  "env": {"TOKEN": "x"}, "apiKeyHelper": "/bin/print-token",
                  "permissions": {"allow": ["Bash(git *)"]}})
    assert "credentials" not in red.get("sandbox", {})
    assert "env" not in red
    assert "apiKeyHelper" not in red
    # A control, so an empty snapshot cannot pass this test by sending nothing.
    assert red.get("permissions") == {"allow": ["Bash(git *)"]}


def test_arrays_union_and_a_deny_is_one_way():
    local = {"permissions": {"allow": ["Bash(a *)"], "deny": ["Bash(rm -rf *)"]}}
    remote = {"permissions": {"allow": ["Bash(b *)"], "deny": []}}
    out = merge(local, remote)
    assert sorted(out["permissions"]["allow"]) == ["Bash(a *)", "Bash(b *)"]
    assert out["permissions"]["deny"] == ["Bash(rm -rf *)"]
    assert merge(local, remote, prune_denies=True)["permissions"]["deny"] == []


def test_merge_never_mutates_its_inputs():
    local = {"sandbox": {"enabled": True}}
    remote = {"sandbox": {"credentials": {"x": 1}}}
    merge(local, remote)
    assert local == {"sandbox": {"enabled": True}}
    assert remote == {"sandbox": {"credentials": {"x": 1}}}
