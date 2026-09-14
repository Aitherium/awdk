"""Tests for adk/sandbox.py — AitherSandbox capability-based execution."""

import sys
import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.sandbox import (
    AitherSandbox,
    SandboxPolicy,
    SandboxResult,
    TaintedOutput,
    Capability,
    TOOL_CAPABILITY_MAP,
    create_sandbox,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sandbox():
    """Default sandbox with network + filesystem capabilities."""
    return AitherSandbox()


@pytest.fixture
def restricted_sandbox():
    """Sandbox with only filesystem capability."""
    return AitherSandbox(capabilities={"filesystem"})


@pytest.fixture
def full_sandbox():
    """Sandbox with all capabilities."""
    return AitherSandbox(capabilities={
        "network", "filesystem", "exec", "secrets", "gpu", "privileged"
    })


# ---------------------------------------------------------------------------
# Capability enum
# ---------------------------------------------------------------------------

class TestCapability:
    def test_capability_values(self):
        assert Capability.NETWORK == "network"
        assert Capability.FILESYSTEM == "filesystem"
        assert Capability.EXEC == "exec"
        assert Capability.SECRETS == "secrets"
        assert Capability.GPU == "gpu"
        assert Capability.PRIVILEGED == "privileged"

    def test_tool_capability_map(self):
        assert Capability.NETWORK in TOOL_CAPABILITY_MAP["web_search"]
        assert Capability.FILESYSTEM in TOOL_CAPABILITY_MAP["file_read"]
        assert Capability.EXEC in TOOL_CAPABILITY_MAP["shell_command"]
        assert Capability.SECRETS in TOOL_CAPABILITY_MAP["secret_get"]
        assert Capability.PRIVILEGED in TOOL_CAPABILITY_MAP["docker_exec"]


# ---------------------------------------------------------------------------
# SandboxPolicy
# ---------------------------------------------------------------------------

class TestSandboxPolicy:
    def test_default_policy(self):
        policy = SandboxPolicy()
        assert Capability.NETWORK in policy.allowed_capabilities
        assert Capability.FILESYSTEM in policy.allowed_capabilities
        assert policy.max_execution_seconds == 30.0
        assert policy.max_output_bytes == 1_048_576
        assert policy.audit_log is True

    def test_custom_policy(self):
        policy = SandboxPolicy(
            allowed_capabilities={Capability.EXEC},
            max_execution_seconds=10.0,
            deny_tools={"dangerous_tool"},
        )
        assert Capability.EXEC in policy.allowed_capabilities
        assert Capability.NETWORK not in policy.allowed_capabilities
        assert "dangerous_tool" in policy.deny_tools


# ---------------------------------------------------------------------------
# can_execute
# ---------------------------------------------------------------------------

class TestCanExecute:
    def test_allowed_tool(self, sandbox):
        assert sandbox.can_execute("web_search") is True
        assert sandbox.can_execute("file_read") is True

    def test_denied_tool_missing_capability(self, restricted_sandbox):
        # filesystem only, no network
        assert restricted_sandbox.can_execute("web_search") is False
        # filesystem is available
        assert restricted_sandbox.can_execute("file_read") is True

    def test_tool_in_deny_list(self, sandbox):
        sandbox._policy.deny_tools.add("blocked_tool")
        assert sandbox.can_execute("blocked_tool") is False

    def test_unknown_tool_allowed(self, sandbox):
        # Unknown tools have no required capabilities
        assert sandbox.can_execute("unknown_tool_xyz") is True

    def test_check_capabilities_returns_tuple(self, sandbox):
        allowed, required, missing = sandbox.check_capabilities("web_search")
        assert allowed is True
        assert Capability.NETWORK in required
        assert len(missing) == 0

    def test_check_capabilities_missing(self, restricted_sandbox):
        allowed, required, missing = restricted_sandbox.check_capabilities("web_search")
        assert allowed is False
        assert Capability.NETWORK in missing


# ---------------------------------------------------------------------------
# execute -- sync functions
# ---------------------------------------------------------------------------

class TestExecuteSync:
    @pytest.mark.asyncio
    async def test_execute_sync_function(self, sandbox):
        def my_tool(x: str) -> str:
            return f"result: {x}"

        result = await sandbox.execute("file_read", my_tool, {"x": "hello"})
        assert result.success is True
        assert result.output == "result: hello"
        assert result.execution_ms > 0

    @pytest.mark.asyncio
    async def test_execute_returns_json_for_non_string(self, sandbox):
        def dict_tool() -> dict:
            return {"key": "value", "num": 42}

        result = await sandbox.execute("file_read", dict_tool, {})
        assert result.success is True
        parsed = json.loads(result.output)
        assert parsed["key"] == "value"

    @pytest.mark.asyncio
    async def test_execute_blocked_by_capability(self, restricted_sandbox):
        def net_tool():
            return "should not run"

        result = await restricted_sandbox.execute("web_search", net_tool, {})
        assert result.success is False
        assert result.blocked is True
        assert "network" in result.blocked_reason.lower()

    @pytest.mark.asyncio
    async def test_execute_blocked_by_deny_list(self, sandbox):
        sandbox._policy.deny_tools.add("banned_tool")

        def my_tool():
            return "nope"

        result = await sandbox.execute("banned_tool", my_tool, {})
        assert result.success is False
        assert result.blocked is True
        assert "deny list" in result.blocked_reason.lower()


# ---------------------------------------------------------------------------
# execute -- async functions
# ---------------------------------------------------------------------------

class TestExecuteAsync:
    @pytest.mark.asyncio
    async def test_execute_async_function(self, sandbox):
        async def async_tool(query: str) -> str:
            return f"async result: {query}"

        result = await sandbox.execute(
            "web_search", async_tool, {"query": "test"}, is_async=True
        )
        assert result.success is True
        assert result.output == "async result: test"


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------

class TestTimeout:
    @pytest.mark.asyncio
    async def test_sync_timeout(self):
        policy = SandboxPolicy(max_execution_seconds=0.1)
        sandbox = AitherSandbox(policy=policy)

        def slow_tool():
            import time
            time.sleep(5)
            return "too slow"

        result = await sandbox.execute("file_read", slow_tool, {})
        assert result.success is False
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_async_timeout(self):
        policy = SandboxPolicy(max_execution_seconds=0.1)
        sandbox = AitherSandbox(policy=policy)

        async def slow_async():
            await asyncio.sleep(5)
            return "too slow"

        result = await sandbox.execute("web_search", slow_async, {}, is_async=True)
        assert result.success is False
        assert "timed out" in result.error.lower()


# ---------------------------------------------------------------------------
# Output capture and truncation
# ---------------------------------------------------------------------------

class TestOutputCapture:
    @pytest.mark.asyncio
    async def test_output_truncated(self):
        policy = SandboxPolicy(max_output_bytes=50)
        sandbox = AitherSandbox(policy=policy)

        def big_output():
            return "x" * 1000

        result = await sandbox.execute("file_read", big_output, {})
        assert result.success is True
        assert result.truncated is True
        # Truncation appends "\n[TRUNCATED]" (12 chars) after the 50-byte slice
        assert len(result.output) <= 65
        assert "[TRUNCATED]" in result.output

    @pytest.mark.asyncio
    async def test_output_not_truncated_when_small(self, sandbox):
        def small_output():
            return "small"

        result = await sandbox.execute("file_read", small_output, {})
        assert result.truncated is False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_function_exception_captured(self, sandbox):
        def crasher():
            raise ValueError("kaboom")

        result = await sandbox.execute("file_read", crasher, {})
        assert result.success is False
        assert "kaboom" in result.error

    @pytest.mark.asyncio
    async def test_async_function_exception(self, sandbox):
        async def async_crasher():
            raise RuntimeError("async boom")

        result = await sandbox.execute("web_search", async_crasher, {}, is_async=True)
        assert result.success is False
        assert "async boom" in result.error


# ---------------------------------------------------------------------------
# Taint tracking
# ---------------------------------------------------------------------------

class TestTaintTracking:
    @pytest.mark.asyncio
    async def test_web_search_is_tainted(self, sandbox):
        async def mock_search():
            return "results"

        result = await sandbox.execute("web_search", mock_search, {}, is_async=True)
        assert result.tainted is True
        assert result.taint_source == "web_search"

    @pytest.mark.asyncio
    async def test_file_read_not_tainted(self, sandbox):
        def mock_read():
            return "file contents"

        result = await sandbox.execute("file_read", mock_read, {})
        assert result.tainted is False

    @pytest.mark.asyncio
    async def test_shell_command_is_tainted(self, full_sandbox):
        def mock_shell():
            return "shell output"

        result = await full_sandbox.execute("shell_command", mock_shell, {})
        assert result.tainted is True
        assert result.taint_source == "shell_command"


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_audit_records_execution(self, sandbox):
        def tool_fn():
            return "ok"

        await sandbox.execute("file_read", tool_fn, {})
        trail = sandbox.audit_trail
        assert len(trail) == 1
        assert trail[0]["tool"] == "file_read"
        assert trail[0]["success"] is True
        assert trail[0]["blocked"] is False

    @pytest.mark.asyncio
    async def test_audit_records_blocks(self, restricted_sandbox):
        def tool_fn():
            return "should not run"

        await restricted_sandbox.execute("web_search", tool_fn, {})
        trail = restricted_sandbox.audit_trail
        assert len(trail) == 1
        assert trail[0]["blocked"] is True

    @pytest.mark.asyncio
    async def test_audit_disabled(self):
        policy = SandboxPolicy(audit_log=False)
        sandbox = AitherSandbox(policy=policy)

        def tool_fn():
            return "ok"

        await sandbox.execute("file_read", tool_fn, {})
        assert len(sandbox.audit_trail) == 0

    @pytest.mark.asyncio
    async def test_audit_trail_bounded(self, sandbox):
        def tool_fn():
            return "ok"

        for _ in range(1200):
            await sandbox.execute("file_read", tool_fn, {})
        # Should be bounded to ~500 after pruning
        assert len(sandbox.audit_trail) <= 1001


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    @pytest.mark.asyncio
    async def test_stats_counts(self, sandbox):
        def tool_fn():
            return "ok"

        await sandbox.execute("file_read", tool_fn, {})
        stats = sandbox.stats
        assert stats["total_executions"] == 1
        assert stats["total_blocks"] == 0

    @pytest.mark.asyncio
    async def test_stats_block_count(self, restricted_sandbox):
        def tool_fn():
            return "nope"

        await restricted_sandbox.execute("web_search", tool_fn, {})
        stats = restricted_sandbox.stats
        assert stats["total_blocks"] == 1

    def test_stats_capabilities_listed(self, sandbox):
        stats = sandbox.stats
        assert "network" in stats["allowed_capabilities"]
        assert "filesystem" in stats["allowed_capabilities"]


# ---------------------------------------------------------------------------
# Violation callback
# ---------------------------------------------------------------------------

class TestViolationCallback:
    @pytest.mark.asyncio
    async def test_on_violation_called(self):
        violations = []
        sandbox = AitherSandbox(
            capabilities={"filesystem"},
            on_violation=lambda tool, reason: violations.append((tool, reason)),
        )

        def tool_fn():
            return "nope"

        await sandbox.execute("web_search", tool_fn, {})
        assert len(violations) == 1
        assert violations[0][0] == "web_search"
        assert "network" in violations[0][1].lower()


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------

class TestExecuteSubprocess:
    @pytest.mark.asyncio
    async def test_subprocess_blocked_without_exec(self):
        sandbox = AitherSandbox(capabilities={"filesystem"})  # No exec!
        result = await sandbox.execute_subprocess(["echo", "hello"])
        assert result.success is False
        assert result.blocked is True

    @pytest.mark.asyncio
    async def test_subprocess_success(self, full_sandbox):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"hello\n", b""))
        mock_proc.returncode = 0
        mock_proc.kill = AsyncMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await full_sandbox.execute_subprocess(["echo", "hello"])
            assert result.success is True
            assert "hello" in result.output
            assert result.tainted is True

    @pytest.mark.asyncio
    async def test_subprocess_nonzero_exit(self, full_sandbox):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error msg"))
        mock_proc.returncode = 1
        mock_proc.kill = AsyncMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await full_sandbox.execute_subprocess(["false"])
            assert result.success is False
            assert "error msg" in result.error

    @pytest.mark.asyncio
    async def test_subprocess_timeout(self, full_sandbox):
        full_sandbox._policy.max_execution_seconds = 0.1

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.kill = MagicMock()  # kill() is sync on subprocess
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await full_sandbox.execute_subprocess(["sleep", "60"])
            assert result.success is False
            assert "timed out" in result.error.lower()
            mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_subprocess_exception(self, full_sandbox):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("not found")):
            result = await full_sandbox.execute_subprocess(["nonexistent_cmd"])
            assert result.success is False
            assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_subprocess_output_truncated(self, full_sandbox):
        full_sandbox._policy.max_output_bytes = 20

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"x" * 100, b""))
        mock_proc.returncode = 0
        mock_proc.kill = AsyncMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await full_sandbox.execute_subprocess(["big_output"])
            assert result.truncated is True
            assert "[TRUNCATED]" in result.output


# ---------------------------------------------------------------------------
# create_sandbox factory
# ---------------------------------------------------------------------------

class TestCreateSandbox:
    def test_default_factory(self):
        sandbox = create_sandbox()
        assert Capability.NETWORK in sandbox.policy.allowed_capabilities
        assert Capability.FILESYSTEM in sandbox.policy.allowed_capabilities

    def test_custom_capabilities(self):
        sandbox = create_sandbox(capabilities={"exec", "gpu"})
        assert Capability.EXEC in sandbox.policy.allowed_capabilities
        assert Capability.GPU in sandbox.policy.allowed_capabilities
        assert Capability.NETWORK not in sandbox.policy.allowed_capabilities

    def test_custom_timeout(self):
        sandbox = create_sandbox(timeout=60.0)
        assert sandbox.policy.max_execution_seconds == 60.0

    def test_deny_tools(self):
        sandbox = create_sandbox(deny_tools={"rm", "format"})
        assert "rm" in sandbox.policy.deny_tools
        assert "format" in sandbox.policy.deny_tools


# ---------------------------------------------------------------------------
# SandboxResult / TaintedOutput dataclasses
# ---------------------------------------------------------------------------

class TestDataclasses:
    def test_sandbox_result_defaults(self):
        r = SandboxResult(success=True)
        assert r.output == ""
        assert r.error == ""
        assert r.blocked is False
        assert r.tainted is False
        assert r.truncated is False

    def test_tainted_output_defaults(self):
        t = TaintedOutput(content="data")
        assert t.content == "data"
        assert t.tainted is False

    def test_capabilities_in_result(self):
        r = SandboxResult(
            success=True,
            capabilities_required={"network"},
            capabilities_granted={"network", "filesystem"},
        )
        assert "network" in r.capabilities_required
        assert "filesystem" in r.capabilities_granted
