"""ADK test fixtures — ensure tests run in env isolation."""

import os
import tempfile

import pytest

# `import adk` autoinstalls the air-gap egress guard when ANY air_gap.yaml exists
# (~/.aither/air_gap.yaml is written by `adk home trust init`). On a host that ran
# it, the guard latched the HOST's config before a single fixture ran, and 5
# air-gap / home-serve tests failed on that machine only (measured 2026-09-30).
# Point the primary layer at a path that does not exist, BEFORE adk is imported;
# a test that wants a config sets AITHER_AIR_GAP_CONFIG / AITHER_DATA_DIR itself.
if not (os.environ.get("AITHER_AIR_GAP_CONFIG") or os.environ.get("AITHER_AIR_GAP")):
    os.environ["AITHER_AIR_GAP_CONFIG"] = os.path.join(
        tempfile.mkdtemp(prefix="adk-test-airgap-"), "air_gap.yaml")

from adk.config import load_saved_config as _real_load_saved_config  # noqa: E402

# Env vars that ADK classes auto-read from the environment.
# Tests must not inherit these from the developer's shell — OR from another test
# module. The second half of that sentence is what AITHER_OFFLINE is here for.
#
# test_world_model_pack / test_env_enroll / test_arc_world_pack each do
# `os.environ["AITHER_OFFLINE"] = "1"` at MODULE level so the packs they import
# come up in-process. pytest imports every test module during collection, so that
# assignment leaks into the whole session — including tests that never asked for
# it. `swarm_code` reads the var at CALL time and takes its sovereign A2A path
# instead of the HTTP one (adk/builtin_tools.py), so `@patch("httpx.post")` never
# fires and TestSwarmCode gets a fabricated "completed" dict. Those five tests
# passed alone and failed in the full suite, which is what blocked the public
# payload gate (and therefore every adk-v* release) after the world-model fix.
#
# Stripping it per-test is safe for the offline modules: they capture offline mode
# at IMPORT time (before this fixture runs), so their in-process engines stay
# in-process. Only call-time readers are affected — which is the bug. Same class
# as a known ContextVar leak; asserted by a test-order-independence check.
_ISOLATION_VARS = [
    "AITHER_API_KEY",
    "AITHER_MCP_KEY",
    "MCP_SERVICE_TOKEN",
    "AITHER_GATEWAY_URL",
    "AITHER_MCP_URL",
    "AITHERNET_RELAY_URL",
    "AITHER_INFERENCE_URL",
    "AITHER_OFFLINE",
    # `adk run --crystal` exports these and AitherAgent reads them on construction
    # (crystal_from_env): a developer shell with ADK_CRYSTAL_SCOPE set would bind
    # every test agent to the real ~/.aither/awm/memory.db.
    "ADK_CRYSTAL_SCOPE",
    "ADK_CRYSTAL_DB",
    "ADK_CRYSTAL_GRAPH_ROOT",
    "ADK_CRYSTAL_NO_EMBED",
]


def _isolated_load_saved_config(config_path=None):
    """Return empty dict for default config path (no env bleed), passthrough for explicit paths."""
    if config_path is None:
        return {}
    return _real_load_saved_config(config_path)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Strip AitherOS credentials from the environment for every test.

    Also patches load_saved_config so the default path (~/.aither/config.yaml)
    returns empty dict, preventing credential bleed from the dev machine.
    Tests that pass an explicit path still get the real function.

    The functionality suite runs as the unrestricted INTERNAL tier so that
    feature tests (fleet, channels, cron, swarm, auto-neurons) are not blocked
    by the open-core license gates. The dedicated licensing/moat tests override
    this via their own (module-level, later-running) fixtures to exercise the
    free COMMUNITY tier and fail-closed behavior.
    """
    for var in _ISOLATION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("adk.config.load_saved_config", _isolated_load_saved_config)

    # Isolate ALL on-disk state (GraphMemory writes to AITHER_DATA_DIR/{agent}.db,
    # NOT the per-test Memory db) so prior runs don't pollute a "fresh" agent's
    # graph — otherwise a memory-question recalls its own past asks and self-grounds.
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path / ".aither"))

    # An auto-keyed MCPServer / /mcp-workstation mint persists its bearer to a
    # file under ~/.aither. Without this every run of the suite writes into the
    # developer's real home (same class as 'tests wrote the owner's card store').
    monkeypatch.setenv("AITHER_MCP_KEY_FILE", str(tmp_path / ".aither" / "mcp-session-key"))
    # Non-Claude harness sessions write session-focus records (adk.harnesses.focus);
    # a test codex turn must never land in the owner's ~/.aither/focus.
    monkeypatch.setenv("AITHER_FOCUS_DIR", str(tmp_path / ".aither" / "focus"))
    # create_app mints the daemon's per-user local credential (adk.local_auth); a test
    # app must never write ~/.aither/daemon-token into the developer's real home.
    monkeypatch.setenv("AITHER_LOCAL_TOKEN_FILE", str(tmp_path / ".aither" / "daemon-token"))
    monkeypatch.delenv("AITHER_LOCAL_AUTH", raising=False)

    # Isolate the operator-blind companion vault: tests must NOT read the dev
    # machine's ~/.aither persona, which would swap every agent's identity into
    # the companion. Tests that exercise the companion patch this explicitly.
    monkeypatch.setattr("adk.private_companion.get_companion_vault",
                        lambda *a, **k: None, raising=False)
    try:
        import adk.private_companion as _pc
        _pc._vault = None
    except Exception:
        pass

    monkeypatch.setenv("AITHER_TENANT_SLUG", "aitherium")

    # `adk home serve` setdefaults AITHER_A2A_REQUIRE_TRUST=true in-process. A bare
    # delenv of an unset var records nothing to undo, so set-then-delete: the value
    # a serve test leaves behind is removed at teardown, not leaked into A2A tests.
    monkeypatch.setenv("AITHER_A2A_REQUIRE_TRUST", "")
    monkeypatch.delenv("AITHER_A2A_REQUIRE_TRUST")
    try:
        from adk.licensing import reset_license_manager
        reset_license_manager()
    except Exception:
        pass
