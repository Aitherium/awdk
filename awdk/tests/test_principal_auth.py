"""Tests for principal authority and authorization (G23 / principal-authority).

Tests cover:
- Principal dataclass creation and immutability
- AuthContext authorization enforcement
- ToolRegistry execute with auth=None (backward compatible)
- ToolRegistry execute with auth (forbidden cases)
- Channel-specific auth helpers (signing, verification)
"""

import sys
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Import directly from modules to avoid the adk/__init__.py import chain issue
import adk.auth as auth_module
import adk.channel_auth as channels_auth_module
import adk.tools as tools_module

Principal = auth_module.Principal
AuthContext = auth_module.AuthContext
PrincipalResolver = auth_module.PrincipalResolver
DenyAllResolver = auth_module.DenyAllResolver

verify_telegram_secret = channels_auth_module.verify_telegram_secret
is_ceo = channels_auth_module.is_ceo
sign_approval = channels_auth_module.sign_approval
verify_approval = channels_auth_module.verify_approval

ToolRegistry = tools_module.ToolRegistry


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------


class TestPrincipal:
    def test_principal_creation(self):
        """Create a principal with all fields."""
        p = Principal(
            subject_id="user_123",
            principal_class="customer",
            role="viewer",
            clearance=1,
            allowed_action_types=frozenset(["read"]),
            channel="telegram",
            verified=True,
        )
        assert p.subject_id == "user_123"
        assert p.principal_class == "customer"
        assert p.role == "viewer"
        assert p.clearance == 1
        assert p.allowed_action_types == frozenset(["read"])
        assert p.channel == "telegram"
        assert p.verified is True

    def test_principal_frozen(self):
        """Principal is immutable (frozen dataclass)."""
        p = Principal(subject_id="user_1", principal_class="customer")
        with pytest.raises(AttributeError):
            p.subject_id = "user_2"

    def test_principal_defaults(self):
        """Principal has sensible defaults."""
        p = Principal(subject_id="user_1", principal_class="agent")
        assert p.role == ""
        assert p.clearance == 0
        assert p.allowed_action_types == frozenset()
        assert p.channel == ""
        assert p.verified is False


# ---------------------------------------------------------------------------
# AuthContext
# ---------------------------------------------------------------------------


class TestAuthContext:
    def test_unverified_no_clearance_allowed(self):
        """Unverified principal can do actions with zero clearance and no class."""
        p = Principal(
            subject_id="user_1",
            principal_class="customer",
            verified=False,
        )
        ctx = AuthContext(p)
        assert ctx.can("read_public", required_clearance=0, action_class="") is True

    def test_unverified_with_clearance_denied(self):
        """Unverified principal denied if action requires clearance."""
        p = Principal(
            subject_id="user_1",
            principal_class="customer",
            verified=False,
        )
        ctx = AuthContext(p)
        assert ctx.can("admin_task", required_clearance=1) is False

    def test_unverified_with_action_class_denied(self):
        """Unverified principal denied if action has a class restriction."""
        p = Principal(
            subject_id="user_1",
            principal_class="customer",
            verified=False,
        )
        ctx = AuthContext(p)
        assert ctx.can("write_data", action_class="write") is False

    def test_verified_clearance_passes(self):
        """Verified principal with sufficient clearance passes."""
        p = Principal(
            subject_id="user_1",
            principal_class="customer",
            clearance=5,
            verified=True,
        )
        ctx = AuthContext(p)
        assert ctx.can("task", required_clearance=3) is True

    def test_verified_clearance_fails(self):
        """Verified principal with insufficient clearance denied."""
        p = Principal(
            subject_id="user_1",
            principal_class="customer",
            clearance=2,
            verified=True,
        )
        ctx = AuthContext(p)
        assert ctx.can("admin_task", required_clearance=5) is False

    def test_verified_action_class_in_set(self):
        """Verified principal with action class in allowed set passes."""
        p = Principal(
            subject_id="user_1",
            principal_class="customer",
            allowed_action_types=frozenset(["read", "write"]),
            verified=True,
        )
        ctx = AuthContext(p)
        assert ctx.can("write_data", action_class="write") is True
        assert ctx.can("read_data", action_class="read") is True

    def test_verified_action_class_not_in_set(self):
        """Verified principal with action class not in set denied."""
        p = Principal(
            subject_id="user_1",
            principal_class="customer",
            allowed_action_types=frozenset(["read"]),
            verified=True,
        )
        ctx = AuthContext(p)
        assert ctx.can("delete_data", action_class="delete") is False

    def test_verified_action_class_wildcard(self):
        """Verified principal with wildcard in action types allowed."""
        p = Principal(
            subject_id="user_1",
            principal_class="customer",
            allowed_action_types=frozenset(["*"]),
            verified=True,
        )
        ctx = AuthContext(p)
        assert ctx.can("delete_data", action_class="delete") is True
        assert ctx.can("admin_task", action_class="admin") is True

    def test_anonymous_principal(self):
        """AuthContext.anonymous() creates unverified anon principal."""
        ctx = AuthContext.anonymous()
        assert ctx.principal.subject_id == "anon"
        assert ctx.principal.verified is False
        assert ctx.can("public_action", required_clearance=0, action_class="") is True
        assert ctx.can("restricted", required_clearance=1) is False

    def test_system_principal(self):
        """AuthContext.system() creates fully trusted principal."""
        ctx = AuthContext.system()
        assert ctx.principal.subject_id == "system"
        assert ctx.principal.verified is True
        assert ctx.principal.clearance == 999
        assert ctx.can("anything", required_clearance=999, action_class="admin") is True


# ---------------------------------------------------------------------------
# PrincipalResolver
# ---------------------------------------------------------------------------


class TestPrincipalResolver:
    @pytest.mark.asyncio
    async def test_deny_all_resolver_returns_unverified(self):
        """DenyAllResolver always returns unverified principal."""
        resolver = DenyAllResolver()
        principal = await resolver.resolve(
            channel="telegram",
            raw_identity={"user_id": "123", "username": "bob"},
        )
        assert principal.verified is False
        assert principal.subject_id == "123"
        assert principal.channel == "telegram"

    @pytest.mark.asyncio
    async def test_deny_all_resolver_unknown_user(self):
        """DenyAllResolver handles missing user_id gracefully."""
        resolver = DenyAllResolver()
        principal = await resolver.resolve(
            channel="slack",
            raw_identity={"username": "alice"},
        )
        assert principal.verified is False
        assert principal.subject_id == "unknown"


# ---------------------------------------------------------------------------
# ToolRegistry with auth=None (backward compatible)
# ---------------------------------------------------------------------------


class TestToolRegistryBackwardCompat:
    @pytest.mark.asyncio
    async def test_execute_no_auth_runs_tool(self):
        """With auth=None, tool executes normally (backward compatible)."""
        registry = ToolRegistry()

        def simple_tool(x: int) -> str:
            """Add 10 to x."""
            return str(x + 10)

        registry.register(simple_tool)
        result = await registry.execute("simple_tool", {"x": 5}, auth=None)
        # Tools that return strings are returned as-is, not JSON-encoded
        assert result == "15"

    @pytest.mark.asyncio
    async def test_execute_no_auth_tool_error(self):
        """With auth=None, tool exception propagates normally."""
        registry = ToolRegistry()

        def failing_tool():
            """Raises an error."""
            raise ValueError("Tool broke")

        registry.register(failing_tool)
        result = await registry.execute("failing_tool", {}, auth=None)
        data = json.loads(result)
        assert "error" in data
        assert "Tool broke" in data["error"]

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_no_auth(self):
        """With auth=None, unknown tool returns error."""
        registry = ToolRegistry()
        result = await registry.execute("nonexistent", {}, auth=None)
        data = json.loads(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# ToolRegistry with auth (authorization enforcement)
# ---------------------------------------------------------------------------


class TestToolRegistryAuth:
    @pytest.mark.asyncio
    async def test_execute_auth_denied(self):
        """Tool with clearance requirement denied to unverified principal."""
        registry = ToolRegistry()

        tool_called = False
        def secure_tool():
            nonlocal tool_called
            tool_called = True
            return "success"

        registry.register(secure_tool, required_clearance=5)

        # Unverified principal, tool requires clearance 5 → denied
        ctx = AuthContext.anonymous()
        result = await registry.execute("secure_tool", {}, auth=ctx)
        data = json.loads(result)

        assert data["error"] == "forbidden"
        assert data["tool"] == "secure_tool"
        assert tool_called is False  # Tool was NOT called

    @pytest.mark.asyncio
    async def test_execute_auth_allowed(self):
        """Tool with clearance requirement allowed to verified principal."""
        registry = ToolRegistry()

        def secure_tool() -> str:
            """Secure operation."""
            return "success"

        registry.register(secure_tool, required_clearance=3)

        # Verified principal with clearance 10
        principal = Principal(
            subject_id="admin",
            principal_class="employee",
            clearance=10,
            verified=True,
        )
        ctx = AuthContext(principal)
        result = await registry.execute("secure_tool", {}, auth=ctx)

        assert result == "success"

    @pytest.mark.asyncio
    async def test_execute_auth_insufficient_clearance(self):
        """Tool denied when clearance too low."""
        registry = ToolRegistry()

        tool_called = False
        def admin_tool():
            nonlocal tool_called
            tool_called = True
            return "secret"

        registry.register(admin_tool, required_clearance=10)

        principal = Principal(
            subject_id="user",
            principal_class="customer",
            clearance=3,  # Not enough
            verified=True,
        )
        ctx = AuthContext(principal)
        result = await registry.execute("admin_tool", {}, auth=ctx)
        data = json.loads(result)

        assert data["error"] == "forbidden"
        assert tool_called is False

    @pytest.mark.asyncio
    async def test_execute_action_class_allowed(self):
        """Tool with action class allowed to principal with matching class."""
        registry = ToolRegistry()

        def write_tool() -> str:
            """Write operation."""
            return "written"

        registry.register(write_tool, action_class="write")

        principal = Principal(
            subject_id="editor",
            principal_class="employee",
            allowed_action_types=frozenset(["read", "write"]),
            verified=True,
        )
        ctx = AuthContext(principal)
        result = await registry.execute("write_tool", {}, auth=ctx)

        assert result == "written"

    @pytest.mark.asyncio
    async def test_execute_action_class_denied(self):
        """Tool denied when action class not in allowed set."""
        registry = ToolRegistry()

        tool_called = False
        def delete_tool():
            nonlocal tool_called
            tool_called = True
            return "deleted"

        registry.register(delete_tool, action_class="delete")

        principal = Principal(
            subject_id="viewer",
            principal_class="customer",
            allowed_action_types=frozenset(["read"]),
            verified=True,
        )
        ctx = AuthContext(principal)
        result = await registry.execute("delete_tool", {}, auth=ctx)
        data = json.loads(result)

        assert data["error"] == "forbidden"
        assert tool_called is False

    @pytest.mark.asyncio
    async def test_execute_action_class_wildcard(self):
        """Tool allowed when principal has wildcard action class."""
        registry = ToolRegistry()

        def delete_tool() -> str:
            """Delete operation."""
            return "deleted"

        registry.register(delete_tool, action_class="delete")

        principal = Principal(
            subject_id="admin",
            principal_class="employee",
            allowed_action_types=frozenset(["*"]),
            verified=True,
        )
        ctx = AuthContext(principal)
        result = await registry.execute("delete_tool", {}, auth=ctx)

        assert result == "deleted"

    @pytest.mark.asyncio
    async def test_execute_system_principal(self):
        """System principal bypasses all restrictions."""
        registry = ToolRegistry()

        def admin_delete() -> str:
            """Admin delete operation."""
            return "nuked"

        registry.register(admin_delete, required_clearance=100, action_class="admin")

        ctx = AuthContext.system()
        result = await registry.execute("admin_delete", {}, auth=ctx)

        assert result == "nuked"


# ---------------------------------------------------------------------------
# Channel-specific auth helpers
# ---------------------------------------------------------------------------


class TestChannelAuthHelpers:
    def test_verify_telegram_secret_match(self):
        """Telegram secret verification passes for matching tokens."""
        received = "secret_abc_123"
        expected = "secret_abc_123"
        assert verify_telegram_secret(received, expected) is True

    def test_verify_telegram_secret_mismatch(self):
        """Telegram secret verification fails for different tokens."""
        received = "secret_abc_123"
        expected = "secret_xyz_789"
        assert verify_telegram_secret(received, expected) is False

    def test_is_ceo_match_int(self):
        """CEO check passes when IDs match (int)."""
        assert is_ceo(123456, 123456) is True

    def test_is_ceo_match_str(self):
        """CEO check passes when IDs match (str)."""
        assert is_ceo("user_ceo", "user_ceo") is True

    def test_is_ceo_match_mixed_types(self):
        """CEO check passes with mixed int/str."""
        assert is_ceo(123, "123") is True

    def test_is_ceo_no_match(self):
        """CEO check fails when IDs don't match."""
        assert is_ceo(123, 456) is False

    def test_sign_approval_deterministic(self):
        """Signature is deterministic for same inputs."""
        secret = "shared_secret"
        req_id = "req_001"
        decision = "approve"
        ts = "2026-06-05T12:00:00Z"

        sig1 = sign_approval(secret, req_id, decision, ts)
        sig2 = sign_approval(secret, req_id, decision, ts)

        assert sig1 == sig2
        assert len(sig1) == 64  # SHA256 hex is 64 chars

    def test_verify_approval_valid(self):
        """Valid approval signature verifies."""
        secret = "shared_secret"
        req_id = "req_001"
        decision = "approve"
        ts = "2026-06-05T12:00:00Z"

        sig = sign_approval(secret, req_id, decision, ts)
        assert verify_approval(secret, req_id, decision, ts, sig) is True

    def test_verify_approval_invalid_signature(self):
        """Invalid signature fails verification."""
        secret = "shared_secret"
        req_id = "req_001"
        decision = "approve"
        ts = "2026-06-05T12:00:00Z"
        bad_sig = "0" * 64

        assert verify_approval(secret, req_id, decision, ts, bad_sig) is False

    def test_verify_approval_tampered_decision(self):
        """Tampered decision fails verification."""
        secret = "shared_secret"
        req_id = "req_001"
        decision = "approve"
        ts = "2026-06-05T12:00:00Z"

        sig = sign_approval(secret, req_id, decision, ts)
        assert verify_approval(secret, req_id, "deny", ts, sig) is False

    def test_verify_approval_tampered_timestamp(self):
        """Tampered timestamp fails verification."""
        secret = "shared_secret"
        req_id = "req_001"
        decision = "approve"
        ts = "2026-06-05T12:00:00Z"

        sig = sign_approval(secret, req_id, decision, ts)
        assert verify_approval(secret, req_id, decision, "2026-06-05T13:00:00Z", sig) is False

    def test_verify_approval_wrong_secret(self):
        """Wrong secret fails verification."""
        secret = "shared_secret"
        req_id = "req_001"
        decision = "approve"
        ts = "2026-06-05T12:00:00Z"

        sig = sign_approval(secret, req_id, decision, ts)
        assert verify_approval("wrong_secret", req_id, decision, ts, sig) is False
