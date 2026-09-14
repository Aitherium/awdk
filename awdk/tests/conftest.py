"""ADK test fixtures — ensure tests run in env isolation."""

import os

import pytest

from adk.config import load_saved_config as _real_load_saved_config

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
    try:
        from adk.licensing import reset_license_manager
        reset_license_manager()
    except Exception:
        pass
