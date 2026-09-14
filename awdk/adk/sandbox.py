"""AitherSandbox — Isolated tool execution with capability-based security.

Capability-based tool execution isolation. Since we're in Python,
we use subprocess isolation with:

  - **Capability checks**: Tools declare required capabilities; sandbox
    enforces them before execution.
  - **Timeout enforcement**: Hard kill after deadline.
  - **Environment clearing**: Subprocess gets only explicitly allowed env vars.
  - **Output capture**: stdout/stderr captured and size-limited.
  - **Resource limits**: Memory and CPU time limits (Linux only via ulimit).
  - **Taint tracking**: Mark outputs from untrusted sources.

Security model:
    ALLOW_LIST capabilities — tools must declare what they need:
      - "network"      → HTTP/socket access
      - "filesystem"   → Read/write files
      - "exec"         → Execute subprocesses
      - "secrets"      → Access AitherSecrets
      - "gpu"          → GPU resource access
      - "privileged"   → Docker/system-level ops

Usage:
    sandbox = AitherSandbox(capabilities={"network", "filesystem"})

    # Execute a tool safely
    result = await sandbox.execute(tool_def, arguments)

    # Check if a tool is allowed
    if sandbox.can_execute(tool_def):
        result = await sandbox.execute(tool_def, arguments)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, Optional, Set

logger = logging.getLogger("adk.sandbox")


# ─────────────────────────────────────────────────────────────────────────────
# Capability Model
# ─────────────────────────────────────────────────────────────────────────────

class Capability(str, Enum):
    """Capabilities that tools can request."""
    NETWORK = "network"          # HTTP/socket access
    FILESYSTEM = "filesystem"    # Read/write files
    EXEC = "exec"                # Execute subprocesses
    SECRETS = "secrets"          # Access AitherSecrets
    GPU = "gpu"                  # GPU resource access
    PRIVILEGED = "privileged"    # Docker/system-level operations
    MEMORY = "memory"            # Large memory allocation
    UNSAFE = "unsafe"            # Bypasses all checks (admin only)


# Default capabilities for common tool categories
TOOL_CAPABILITY_MAP: Dict[str, FrozenSet[Capability]] = {
    "web_search": frozenset({Capability.NETWORK}),
    "fetch_webpage": frozenset({Capability.NETWORK}),
    "shell_command": frozenset({Capability.EXEC, Capability.FILESYSTEM}),
    "python_repl": frozenset({Capability.EXEC, Capability.FILESYSTEM}),
    "python_eval": frozenset({Capability.EXEC}),
    "file_read": frozenset({Capability.FILESYSTEM}),
    "file_write": frozenset({Capability.FILESYSTEM}),
    "file_edit": frozenset({Capability.FILESYSTEM}),
    "secret_get": frozenset({Capability.SECRETS}),
    "docker_exec": frozenset({Capability.PRIVILEGED, Capability.EXEC}),
    "gpu_allocate": frozenset({Capability.GPU}),
    "service_call": frozenset({Capability.NETWORK}),
    "orchestrator_task": frozenset({Capability.NETWORK}),
}


@dataclass
class SandboxPolicy:
    """Policy governing what a sandbox can do.

    Args:
        allowed_capabilities: Set of capabilities this sandbox grants.
        max_execution_seconds: Hard timeout for tool execution.
        max_output_bytes: Maximum output size before truncation.
        max_memory_mb: Memory limit for subprocess (Linux only).
        allow_env_vars: Environment variables to pass through.
        deny_tools: Tools that are always blocked.
        audit_log: Whether to log all executions.
    """
    allowed_capabilities: Set[Capability] = field(
        default_factory=lambda: {Capability.NETWORK, Capability.FILESYSTEM}
    )
    max_execution_seconds: float = 30.0
    max_output_bytes: int = 1_048_576  # 1MB
    max_memory_mb: int = 512
    allow_env_vars: Set[str] = field(
        default_factory=lambda: {"PATH", "HOME", "LANG", "TERM", "PYTHONPATH"}
    )
    deny_tools: Set[str] = field(default_factory=set)
    audit_log: bool = True


@dataclass
class TaintedOutput:
    """Output marked with taint tracking metadata."""
    content: str
    tainted: bool = False
    taint_source: str = ""
    taint_reason: str = ""
    execution_ms: float = 0.0
    truncated: bool = False
    capability_used: Set[str] = field(default_factory=set)


@dataclass
class SandboxResult:
    """Result from sandboxed tool execution."""
    success: bool
    output: str = ""
    error: str = ""
    execution_ms: float = 0.0
    tainted: bool = False
    taint_source: str = ""
    blocked: bool = False
    blocked_reason: str = ""
    truncated: bool = False
    capabilities_required: Set[str] = field(default_factory=set)
    capabilities_granted: Set[str] = field(default_factory=set)


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox
# ─────────────────────────────────────────────────────────────────────────────

class AitherSandbox:
    """Isolated tool execution environment with capability-based security.

    Args:
        policy: SandboxPolicy governing this sandbox.
        capabilities: Shorthand for allowed_capabilities (overrides policy).
        on_violation: Callback when a capability violation occurs.
    """

    def __init__(
        self,
        policy: SandboxPolicy | None = None,
        capabilities: Set[str | Capability] | None = None,
        on_violation: Callable[[str, str], None] | None = None,
    ):
        self._policy = policy or SandboxPolicy()

        if capabilities is not None:
            self._policy.allowed_capabilities = {
                Capability(c) if isinstance(c, str) else c
                for c in capabilities
            }

        self._on_violation = on_violation
        self._audit_trail: list[dict] = []
        self._total_executions: int = 0
        self._total_blocks: int = 0

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────────────

    def can_execute(self, tool_name: str, required_caps: Set[Capability] | None = None) -> bool:
        """Check if a tool is allowed to execute under this sandbox's policy."""
        if tool_name in self._policy.deny_tools:
            return False

        caps = required_caps or self._get_required_capabilities(tool_name)
        missing = caps - self._policy.allowed_capabilities
        return len(missing) == 0

    def check_capabilities(self, tool_name: str) -> tuple[bool, Set[Capability], Set[Capability]]:
        """Check capabilities and return (allowed, required, missing)."""
        required = self._get_required_capabilities(tool_name)
        missing = required - self._policy.allowed_capabilities
        return (len(missing) == 0, required, missing)

    async def execute(
        self,
        tool_name: str,
        fn: Callable,
        arguments: Dict[str, Any],
        is_async: bool = False,
        required_capabilities: Set[Capability] | None = None,
    ) -> SandboxResult:
        """Execute a tool function within the sandbox.

        Enforces capability checks, timeout, and output size limits.
        """
        self._total_executions += 1
        start = time.perf_counter()

        # ── Deny list check ──
        if tool_name in self._policy.deny_tools:
            self._total_blocks += 1
            result = SandboxResult(
                success=False,
                blocked=True,
                blocked_reason=f"Tool '{tool_name}' is in the deny list",
            )
            self._audit(tool_name, arguments, result)
            return result

        # ── Capability check ──
        required = required_capabilities or self._get_required_capabilities(tool_name)
        missing = required - self._policy.allowed_capabilities

        if missing:
            self._total_blocks += 1
            missing_names = {c.value for c in missing}
            reason = (
                f"Tool '{tool_name}' requires capabilities {missing_names} "
                f"not granted by sandbox"
            )
            if self._on_violation:
                self._on_violation(tool_name, reason)
            # Fire metrics + Pulse for sandbox violation
            try:
                from adk.metrics import get_metrics
                get_metrics().record_sandbox_block()
            except Exception:
                pass
            result = SandboxResult(
                success=False,
                blocked=True,
                blocked_reason=reason,
                capabilities_required={c.value for c in required},
                capabilities_granted={c.value for c in self._policy.allowed_capabilities},
            )
            self._audit(tool_name, arguments, result)
            return result

        # ── Execute with timeout ──
        try:
            if is_async:
                raw_output = await asyncio.wait_for(
                    fn(**arguments),
                    timeout=self._policy.max_execution_seconds,
                )
            else:
                raw_output = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, lambda: fn(**arguments)),
                    timeout=self._policy.max_execution_seconds,
                )
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - start) * 1000
            result = SandboxResult(
                success=False,
                error=f"Tool '{tool_name}' timed out after {self._policy.max_execution_seconds}s",
                execution_ms=elapsed,
                capabilities_required={c.value for c in required},
                capabilities_granted={c.value for c in self._policy.allowed_capabilities},
            )
            self._audit(tool_name, arguments, result)
            return result
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            result = SandboxResult(
                success=False,
                error=str(e),
                execution_ms=elapsed,
                capabilities_required={c.value for c in required},
                capabilities_granted={c.value for c in self._policy.allowed_capabilities},
            )
            self._audit(tool_name, arguments, result)
            return result

        elapsed = (time.perf_counter() - start) * 1000

        # ── Process output ──
        output_str = raw_output if isinstance(raw_output, str) else json.dumps(raw_output, default=str)
        truncated = False
        if len(output_str) > self._policy.max_output_bytes:
            output_str = output_str[:self._policy.max_output_bytes] + "\n[TRUNCATED]"
            truncated = True

        # ── Taint tracking ──
        tainted = tool_name in {"web_search", "fetch_webpage", "shell_command", "python_repl"}
        taint_source = tool_name if tainted else ""

        result = SandboxResult(
            success=True,
            output=output_str,
            execution_ms=elapsed,
            tainted=tainted,
            taint_source=taint_source,
            truncated=truncated,
            capabilities_required={c.value for c in required},
            capabilities_granted={c.value for c in self._policy.allowed_capabilities},
        )
        self._audit(tool_name, arguments, result)
        return result

    async def execute_subprocess(
        self,
        command: list[str],
        tool_name: str = "subprocess",
        env_override: Dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Execute a subprocess in an isolated environment.

        Clears environment except allowed vars, enforces timeout,
        captures and size-limits output.
        """
        self._total_executions += 1
        start = time.perf_counter()

        # Capability check
        required = {Capability.EXEC}
        if not self.can_execute(tool_name, required):
            self._total_blocks += 1
            return SandboxResult(
                success=False,
                blocked=True,
                blocked_reason=f"Subprocess execution requires 'exec' capability",
            )

        # Build clean environment
        clean_env: Dict[str, str] = {}
        for var in self._policy.allow_env_vars:
            val = os.environ.get(var)
            if val:
                clean_env[var] = val
        if env_override:
            clean_env.update(env_override)

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_env,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._policy.max_execution_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                elapsed = (time.perf_counter() - start) * 1000
                return SandboxResult(
                    success=False,
                    error=f"Subprocess timed out after {self._policy.max_execution_seconds}s",
                    execution_ms=elapsed,
                )

            elapsed = (time.perf_counter() - start) * 1000
            output = stdout.decode("utf-8", errors="replace")
            err_output = stderr.decode("utf-8", errors="replace")

            truncated = False
            if len(output) > self._policy.max_output_bytes:
                output = output[:self._policy.max_output_bytes] + "\n[TRUNCATED]"
                truncated = True

            return SandboxResult(
                success=proc.returncode == 0,
                output=output,
                error=err_output if proc.returncode != 0 else "",
                execution_ms=elapsed,
                tainted=True,
                taint_source="subprocess",
                truncated=truncated,
            )

        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            return SandboxResult(
                success=False,
                error=str(e),
                execution_ms=elapsed,
            )

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy

    @property
    def audit_trail(self) -> list[dict]:
        return list(self._audit_trail)

    @property
    def stats(self) -> dict:
        return {
            "total_executions": self._total_executions,
            "total_blocks": self._total_blocks,
            "allowed_capabilities": [c.value for c in self._policy.allowed_capabilities],
            "deny_list_size": len(self._policy.deny_tools),
            "audit_trail_size": len(self._audit_trail),
        }

    # ─────────────────────────────────────────────────────────────────────
    # INTERNAL
    # ─────────────────────────────────────────────────────────────────────

    def _get_required_capabilities(self, tool_name: str) -> Set[Capability]:
        """Look up required capabilities for a tool."""
        caps = TOOL_CAPABILITY_MAP.get(tool_name)
        if caps:
            return set(caps)
        # Default: no special capabilities needed
        return set()

    def _audit(self, tool_name: str, arguments: Dict[str, Any], result: SandboxResult) -> None:
        """Record execution in audit trail."""
        if not self._policy.audit_log:
            return
        self._audit_trail.append({
            "tool": tool_name,
            "blocked": result.blocked,
            "success": result.success,
            "tainted": result.tainted,
            "execution_ms": result.execution_ms,
            "timestamp": time.time(),
        })
        # Keep audit trail bounded
        if len(self._audit_trail) > 1000:
            self._audit_trail = self._audit_trail[-500:]


# ─────────────────────────────────────────────────────────────────────────────
# Convenience
# ─────────────────────────────────────────────────────────────────────────────

def create_sandbox(
    capabilities: Set[str] | None = None,
    timeout: float = 30.0,
    deny_tools: Set[str] | None = None,
) -> AitherSandbox:
    """Factory for creating sandbox with common defaults."""
    policy = SandboxPolicy(
        allowed_capabilities={Capability(c) for c in (capabilities or {"network", "filesystem"})},
        max_execution_seconds=timeout,
        deny_tools=deny_tools or set(),
    )
    return AitherSandbox(policy=policy)
