"""Tests for agent pack manifest + supervisor."""
import asyncio
import tempfile
from pathlib import Path

import pytest
import yaml

from adk.agent_pack import (
    AGENT_FRAMEWORKS,
    AGENT_PROTOCOLS,
    AgentHandle,
    AgentPackManifest,
    RuntimeConfig,
    Supervisor,
    load_agent_pack,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_pack_dir():
    """Create a temporary directory for test manifests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _write_manifest(pack_dir: Path, data: dict) -> Path:
    """Helper to write a manifest YAML file."""
    manifest_path = pack_dir / "agent.yaml"
    manifest_path.write_text(yaml.dump(data), encoding="utf-8")
    return manifest_path


# ============================================================================
# Test: Parse valid manifest for each framework
# ============================================================================


@pytest.mark.parametrize(
    "framework",
    ["nooa", "deer-flow", "hermes", "openclaw", "native", "custom"],
)
def test_load_agent_pack_all_frameworks(temp_pack_dir, framework):
    """Parse a valid manifest for each framework (fail-closed validation)."""
    manifest_data = {
        "id": f"test_{framework}",
        "name": f"Test {framework}",
        "version": "1.0.0",
        "framework": framework,
        "runtime": {
            "type": "python",
            "cmd": "agent:main",
        },
        "entrypoint": "agent:main",
        "protocol": "acp",
        "skills": ["skill1", "skill2"],
        "entitlements": ["ai_advanced"],
        "min_tier": "builder",
        "identity": {"avatar": "default"},
        "secrets": {
            "api_key": "vault:secrets/api_key",
            "token": "vault:secrets/token",
        },
    }
    _write_manifest(temp_pack_dir, manifest_data)

    # Should parse successfully
    manifest = load_agent_pack(temp_pack_dir)
    assert manifest.id == f"test_{framework}"
    assert manifest.framework == framework
    assert manifest.protocol == "acp"
    assert manifest.skills == ["skill1", "skill2"]
    assert manifest.entitlements == ["ai_advanced"]
    assert manifest.min_tier == "builder"


# ============================================================================
# Test: Parse valid manifest for each protocol
# ============================================================================


@pytest.mark.parametrize(
    "protocol",
    ["acp", "a2a", "mcp", "openai", "langgraph_rest", "http"],
)
def test_load_agent_pack_all_protocols(temp_pack_dir, protocol):
    """Parse a valid manifest for each protocol."""
    manifest_data = {
        "id": f"test_{protocol}",
        "name": f"Test {protocol}",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "agent:main",
        },
        "entrypoint": "agent:main",
        "protocol": protocol,
    }
    _write_manifest(temp_pack_dir, manifest_data)

    manifest = load_agent_pack(temp_pack_dir)
    assert manifest.protocol == protocol


# ============================================================================
# Test: Bad framework raises ValidationError
# ============================================================================


def test_load_agent_pack_bad_framework(temp_pack_dir):
    """Invalid framework should raise ValidationError."""
    manifest_data = {
        "id": "test_bad",
        "name": "Test Bad",
        "framework": "not_a_framework",
        "runtime": {
            "type": "python",
            "cmd": "agent:main",
        },
        "entrypoint": "agent:main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    with pytest.raises(ValueError, match="Manifest validation failed"):
        load_agent_pack(temp_pack_dir)


# ============================================================================
# Test: Bad protocol raises ValidationError
# ============================================================================


def test_load_agent_pack_bad_protocol(temp_pack_dir):
    """Invalid protocol should raise ValidationError."""
    manifest_data = {
        "id": "test_bad",
        "name": "Test Bad",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "agent:main",
        },
        "entrypoint": "agent:main",
        "protocol": "not_a_protocol",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    with pytest.raises(ValueError, match="Manifest validation failed"):
        load_agent_pack(temp_pack_dir)


# ============================================================================
# Test: Missing runtime fields raises ValidationError
# ============================================================================


def test_load_agent_pack_missing_runtime_cmd(temp_pack_dir):
    """Python runtime without cmd should raise ValidationError."""
    manifest_data = {
        "id": "test_bad",
        "name": "Test Bad",
        "framework": "native",
        "runtime": {
            "type": "python",
            # Missing cmd
        },
        "entrypoint": "agent:main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    with pytest.raises(ValueError, match="Manifest validation failed"):
        load_agent_pack(temp_pack_dir)


def test_load_agent_pack_missing_runtime_image(temp_pack_dir):
    """Docker runtime without image should raise ValidationError."""
    manifest_data = {
        "id": "test_bad",
        "name": "Test Bad",
        "framework": "native",
        "runtime": {
            "type": "docker",
            # Missing image
        },
        "entrypoint": "agent:main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    with pytest.raises(ValueError, match="Manifest validation failed"):
        load_agent_pack(temp_pack_dir)


# ============================================================================
# Test: Missing required manifest fields
# ============================================================================


def test_load_agent_pack_missing_framework(temp_pack_dir):
    """Missing framework should raise ValidationError."""
    manifest_data = {
        "id": "test_bad",
        "name": "Test Bad",
        # Missing framework
        "runtime": {
            "type": "python",
            "cmd": "agent:main",
        },
        "entrypoint": "agent:main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    with pytest.raises(ValueError):
        load_agent_pack(temp_pack_dir)


def test_load_agent_pack_missing_protocol(temp_pack_dir):
    """Missing protocol should raise ValidationError."""
    manifest_data = {
        "id": "test_bad",
        "name": "Test Bad",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "agent:main",
        },
        "entrypoint": "agent:main",
        # Missing protocol
    }
    _write_manifest(temp_pack_dir, manifest_data)

    with pytest.raises(ValueError):
        load_agent_pack(temp_pack_dir)


# ============================================================================
# Test: Vault secrets NOT resolved inline
# ============================================================================


def test_secrets_not_resolved_inline(temp_pack_dir):
    """Vault: prefixes in secrets should NOT be resolved (left as-is)."""
    manifest_data = {
        "id": "test_secrets",
        "name": "Test Secrets",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "agent:main",
        },
        "entrypoint": "agent:main",
        "protocol": "acp",
        "secrets": {
            "api_key": "vault:secrets/api_key",
            "token": "vault:internal/token",
            "plain_secret": "hardcoded_value",
        },
    }
    _write_manifest(temp_pack_dir, manifest_data)

    manifest = load_agent_pack(temp_pack_dir)
    # Secrets should be stored as-is, vault: NOT resolved
    assert manifest.secrets["api_key"] == "vault:secrets/api_key"
    assert manifest.secrets["token"] == "vault:internal/token"
    assert manifest.secrets["plain_secret"] == "hardcoded_value"


# ============================================================================
# Test: Supervisor.spawn with python echo process
# ============================================================================


async def test_supervisor_spawn_echo_process(temp_pack_dir):
    """Supervisor spawns a FAKE echo process and handle reports correct state."""
    # Create a manifest that runs a simple python echo
    manifest_data = {
        "id": "test_echo",
        "name": "Test Echo",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "python -c",
            "args": ["import sys; sys.stdout.write('hello'); sys.exit(0)"],
        },
        "entrypoint": "echo",
        "protocol": "http",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    manifest = load_agent_pack(temp_pack_dir)
    supervisor = Supervisor()

    # Spawn the process
    handle = await supervisor.spawn(manifest)

    # Assert handle reports correct protocol
    assert handle.protocol == "http"

    # Assert process is running (or ran to completion)
    # Since we're using a simple python -c that exits, we wait for it
    try:
        await asyncio.wait_for(handle.process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        # If it times out, the process should still be running
        assert handle.is_running()
        await handle.terminate()

    # Assert the handle is properly configured
    assert handle.manifest.id == "test_echo"
    assert isinstance(handle, AgentHandle)


# ============================================================================
# Test: Supervisor.spawn with python runtime
# ============================================================================


async def test_supervisor_spawn_python_runtime(temp_pack_dir):
    """Supervisor spawns a python-runtime agent."""
    # Simple python command that runs for a bit
    manifest_data = {
        "id": "test_python",
        "name": "Test Python Runtime",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "python -c",
            "args": ["import time; time.sleep(10)"],  # Sleep so we can check is_running
        },
        "entrypoint": "main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    manifest = load_agent_pack(temp_pack_dir)
    supervisor = Supervisor()

    handle = await supervisor.spawn(manifest)

    # Process should be running shortly after spawn
    await asyncio.sleep(0.1)
    assert handle.is_running()
    assert handle.protocol == "acp"

    # Clean up
    await handle.terminate(timeout_secs=2.0)


# ============================================================================
# Test: Supervisor.spawn fails on missing executable
# ============================================================================


async def test_supervisor_spawn_python_nonexistent_module(temp_pack_dir):
    """Supervisor.spawn handles python processes that fail at runtime."""
    manifest_data = {
        "id": "test_missing",
        "name": "Test Missing",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "python -c",
            "args": ["import nonexistent_module"],
        },
        "entrypoint": "main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    manifest = load_agent_pack(temp_pack_dir)
    supervisor = Supervisor()

    # Spawn should succeed (process starts), but process will fail
    handle = await supervisor.spawn(manifest)
    assert handle is not None

    # Wait a bit for process to complete
    try:
        await asyncio.wait_for(handle.process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        await handle.terminate()

    # Process should have exited (not running)
    assert not handle.is_running() or handle.process.returncode is not None


# ============================================================================
# Test: Supervisor.terminate_all
# ============================================================================


async def test_supervisor_terminate_all(temp_pack_dir):
    """Supervisor.terminate_all stops all running agents."""
    manifest_data = {
        "id": "test_terminate",
        "name": "Test Terminate",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "python -c",
            "args": ["import time; time.sleep(30)"],  # Long sleep
        },
        "entrypoint": "main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    manifest = load_agent_pack(temp_pack_dir)
    supervisor = Supervisor()

    # Spawn two agents
    handle1 = await supervisor.spawn(manifest)
    manifest.id = "test_terminate_2"  # Change ID for second agent
    handle2 = await supervisor.spawn(manifest)

    await asyncio.sleep(0.2)
    assert handle1.is_running()
    assert handle2.is_running()

    # Terminate all
    await supervisor.terminate_all(timeout_secs=2.0)

    await asyncio.sleep(0.2)
    assert not handle1.is_running()
    assert not handle2.is_running()


# ============================================================================
# Test: to_toolpack_dict bridging
# ============================================================================


def test_to_toolpack_dict_bridging(temp_pack_dir):
    """AgentPackManifest.to_toolpack_dict() bridges to ToolPackManifest shape."""
    manifest_data = {
        "id": "test_bridge",
        "name": "Test Bridge",
        "version": "1.2.3",
        "framework": "native",
        "runtime": {
            "type": "python",
            "cmd": "agent:main",
        },
        "entrypoint": "agent:main",
        "protocol": "acp",
        "skills": ["skill1", "skill2"],
        "entitlements": ["ai_advanced"],
        "min_tier": "builder",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    manifest = load_agent_pack(temp_pack_dir)
    toolpack_dict = manifest.to_toolpack_dict()

    # Assert bridge fields
    assert toolpack_dict["id"] == "test_bridge"
    assert toolpack_dict["name"] == "Test Bridge"
    assert toolpack_dict["version"] == "1.2.3"
    assert toolpack_dict["category"] == "agent_packs"
    assert toolpack_dict["skills"] == ["skill1", "skill2"]
    assert toolpack_dict["mcp_tools"] == ["skill1", "skill2"]
    assert toolpack_dict["entitlements"] == ["ai_advanced"]
    assert toolpack_dict["min_tier"] == "builder"
    assert "agent" in toolpack_dict["tags"]
    assert "native" in toolpack_dict["tags"]
    assert "acp" in toolpack_dict["tags"]


# ============================================================================
# Test: RuntimeConfig validation
# ============================================================================


def test_runtime_config_docker_requires_image():
    """RuntimeConfig docker type requires image."""
    with pytest.raises(ValueError):
        RuntimeConfig(type="docker", cmd="start.sh")


def test_runtime_config_python_requires_cmd():
    """RuntimeConfig python type requires cmd."""
    with pytest.raises(ValueError):
        RuntimeConfig(type="python")


def test_runtime_config_node_requires_cmd():
    """RuntimeConfig node type requires cmd."""
    with pytest.raises(ValueError):
        RuntimeConfig(type="node")


def test_runtime_config_valid_docker():
    """RuntimeConfig docker with image is valid."""
    rc = RuntimeConfig(type="docker", image="my/agent:latest")
    assert rc.type == "docker"
    assert rc.image == "my/agent:latest"


def test_runtime_config_valid_python():
    """RuntimeConfig python with cmd is valid."""
    rc = RuntimeConfig(type="python", cmd="agent:main")
    assert rc.type == "python"
    assert rc.cmd == "agent:main"


def test_runtime_config_valid_node():
    """RuntimeConfig node with cmd is valid."""
    rc = RuntimeConfig(type="node", cmd="agent.js")
    assert rc.type == "node"
    assert rc.cmd == "agent.js"


# ============================================================================
# Test: Manifest file discovery
# ============================================================================


def test_load_agent_pack_discovers_agent_yaml(temp_pack_dir):
    """load_agent_pack discovers agent.yaml in directory."""
    manifest_data = {
        "id": "test_discovery",
        "name": "Test Discovery",
        "framework": "native",
        "runtime": {"type": "python", "cmd": "agent:main"},
        "entrypoint": "agent:main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    # Should find agent.yaml in directory
    manifest = load_agent_pack(temp_pack_dir)
    assert manifest.id == "test_discovery"


def test_load_agent_pack_rejects_nonexistent_path():
    """load_agent_pack raises FileNotFoundError for missing manifest."""
    with pytest.raises(FileNotFoundError):
        load_agent_pack(Path("/nonexistent/path"))


def test_load_agent_pack_invalid_yaml(temp_pack_dir):
    """load_agent_pack raises ValueError for unparseable YAML."""
    manifest_path = temp_pack_dir / "agent.yaml"
    manifest_path.write_text("invalid: yaml: content: [", encoding="utf-8")

    with pytest.raises(ValueError, match="Failed to parse"):
        load_agent_pack(temp_pack_dir)


# ============================================================================
# Test: Manifest defaults
# ============================================================================


def test_manifest_defaults(temp_pack_dir):
    """AgentPackManifest applies sensible defaults."""
    manifest_data = {
        "id": "test_defaults",
        "name": "Test Defaults",
        "framework": "native",
        "runtime": {"type": "python", "cmd": "agent:main"},
        "entrypoint": "agent:main",
        "protocol": "acp",
    }
    _write_manifest(temp_pack_dir, manifest_data)

    manifest = load_agent_pack(temp_pack_dir)
    assert manifest.version == "0.0.0"
    assert manifest.model_endpoint == "http://localhost:8150"
    assert manifest.mcp is None
    assert manifest.skills == []
    assert manifest.entitlements == []
    assert manifest.min_tier == ""
    assert manifest.identity == {}
    assert manifest.secrets == {}
