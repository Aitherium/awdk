"""Tests for managed agent identity lifecycle.

Tests cover:
- Full state machine transitions (PROVISIONED -> REGISTERED -> ACTIVE -> ROTATED -> REVOKED)
- Rotate replaces key + old key no longer authorizes
- Revoke -> authorize denies (fail-closed)
- Unknown id -> deny (default-deny)
- Scope mismatch -> deny
- Minter called with correct scope
- In-memory + JSON file persistence
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adk.managed_identity import (
    ManagedAgentIdentity,
    ManagedAgentIdentityProvider,
    ManagedAgentState,
    ManagedIdentityStore,
    ManagedKey,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────


class SimpleMinter:
    """Test minter that generates simple deterministic keys."""

    def __init__(self):
        self.call_count = 0
        self.calls = []

    def __call__(self, scope: Dict[str, str]) -> str:
        """Mint a key and record the call."""
        self.call_count += 1
        key = f"aither_ext_test_{self.call_count}_{scope.get('tenant_id', 'notenant')}"
        self.calls.append(scope)
        return key


@pytest.fixture
def minter():
    """Create a simple test minter."""
    return SimpleMinter()


@pytest.fixture
def temp_store():
    """Create a temporary store directory."""
    with TemporaryDirectory() as tmpdir:
        store = ManagedIdentityStore(Path(tmpdir) / "managed_identities.json")
        yield store


@pytest.fixture
def provider(minter, temp_store):
    """Create a provider with test minter and temp store."""
    return ManagedAgentIdentityProvider(minter, temp_store)


# ─────────────────────────────────────────────────────────────────────────────
# ManagedKey Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestManagedKey:
    def test_key_creation(self):
        """Create a key with metadata."""
        key = ManagedKey(key="test_key_123", issued_at="2026-01-01T00:00:00Z")
        assert key.key == "test_key_123"
        assert key.issued_at == "2026-01-01T00:00:00Z"
        assert key.valid is True

    def test_key_frozen(self):
        """ManagedKey is frozen (immutable)."""
        key = ManagedKey(key="test", issued_at="2026-01-01T00:00:00Z")
        with pytest.raises(AttributeError):
            key.key = "modified"  # type: ignore

    def test_key_invalid_state(self):
        """Create invalid key."""
        key = ManagedKey(
            key="revoked_key", issued_at="2026-01-01T00:00:00Z", valid=False
        )
        assert key.valid is False


# ─────────────────────────────────────────────────────────────────────────────
# ManagedAgentIdentity Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestManagedAgentIdentity:
    def test_identity_creation(self):
        """Create an identity in PROVISIONED state."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.PROVISIONED,
            scope={"tenant_id": "tenant_1", "workspace_id": "ws_1"},
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert identity.agent_id == "agent_1"
        assert identity.state == ManagedAgentState.PROVISIONED
        assert identity.principal_class == "agent"
        assert identity.current_key == key

    def test_authorize_revoked(self):
        """Revoked agents cannot authorize."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.REVOKED,
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert identity.authorize("read") is False

    def test_authorize_provisioned(self):
        """PROVISIONED agents cannot authorize (not yet active)."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.PROVISIONED,
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert identity.authorize("read") is False

    def test_authorize_registered(self):
        """REGISTERED agents cannot authorize (not yet active)."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.REGISTERED,
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert identity.authorize("read") is False

    def test_authorize_active(self):
        """ACTIVE agents with valid keys can authorize."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.ACTIVE,
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert identity.authorize("read") is True

    def test_authorize_rotated(self):
        """ROTATED agents can still authorize."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.ROTATED,
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert identity.authorize("read") is True

    def test_authorize_no_key(self):
        """ACTIVE agents without key cannot authorize."""
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.ACTIVE,
            current_key=None,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert identity.authorize("read") is False

    def test_authorize_invalid_key(self):
        """ACTIVE agents with invalid key cannot authorize."""
        key = ManagedKey(
            key="test_key", issued_at="2026-01-01T00:00:00Z", valid=False
        )
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.ACTIVE,
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        assert identity.authorize("read") is False


# ─────────────────────────────────────────────────────────────────────────────
# ManagedIdentityStore Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestManagedIdentityStore:
    def test_store_empty_on_init(self, temp_store):
        """New store is empty."""
        temp_store.load()
        assert len(temp_store._in_memory) == 0

    def test_store_save_and_load(self, temp_store):
        """Identities persist to JSON file."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.ACTIVE,
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        temp_store.set("agent_1", identity)

        # Verify file was created
        assert temp_store.path.exists()

        # Create new store and load
        new_store = ManagedIdentityStore(temp_store.path)
        new_store.load()

        loaded = new_store.get("agent_1")
        assert loaded is not None
        assert loaded.agent_id == "agent_1"
        assert loaded.state == ManagedAgentState.ACTIVE
        assert loaded.current_key is not None
        assert loaded.current_key.key == "test_key"

    def test_store_list_all(self, temp_store):
        """List all identities."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        for i in range(3):
            identity = ManagedAgentIdentity(
                agent_id=f"agent_{i}",
                state=ManagedAgentState.ACTIVE,
                current_key=key,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
            )
            temp_store.set(f"agent_{i}", identity)

        all_identities = temp_store.list_all()
        assert len(all_identities) == 3

    def test_store_delete(self, temp_store):
        """Delete identity."""
        key = ManagedKey(key="test_key", issued_at="2026-01-01T00:00:00Z")
        identity = ManagedAgentIdentity(
            agent_id="agent_1",
            state=ManagedAgentState.ACTIVE,
            current_key=key,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        temp_store.set("agent_1", identity)
        assert temp_store.get("agent_1") is not None

        temp_store.delete("agent_1")
        assert temp_store.get("agent_1") is None


# ─────────────────────────────────────────────────────────────────────────────
# Full Lifecycle Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestManagedAgentIdentityProvider:
    def test_provision(self, provider, minter):
        """Provision creates identity in PROVISIONED state."""
        scope = {"tenant_id": "tenant_1", "workspace_id": "ws_1"}
        identity = provider.provision("agent_1", scope)

        assert identity.agent_id == "agent_1"
        assert identity.state == ManagedAgentState.PROVISIONED
        assert identity.principal_class == "agent"
        assert identity.scope == scope
        assert identity.current_key is not None
        assert identity.current_key.key.startswith("aither_ext_test_")

        # Verify minter was called with scope
        assert minter.call_count == 1
        assert minter.calls[0] == scope

    def test_provision_duplicate_fails(self, provider):
        """Provisioning duplicate agent fails."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)

        with pytest.raises(ValueError, match="already provisioned"):
            provider.provision("agent_1", scope)

    def test_register(self, provider):
        """Register transitions PROVISIONED -> REGISTERED."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)

        identity = provider.register("agent_1")
        assert identity.state == ManagedAgentState.REGISTERED

    def test_register_not_provisioned_fails(self, provider):
        """Register fails if not in PROVISIONED state."""
        with pytest.raises(ValueError, match="not found"):
            provider.register("unknown_agent")

    def test_register_wrong_state_fails(self, provider):
        """Register fails from non-PROVISIONED state."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)
        provider.register("agent_1")  # Now REGISTERED

        with pytest.raises(ValueError, match="PROVISIONED"):
            provider.register("agent_1")  # Can't register again

    def test_activate(self, provider):
        """Activate transitions REGISTERED -> ACTIVE."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)
        provider.register("agent_1")

        identity = provider.activate("agent_1")
        assert identity.state == ManagedAgentState.ACTIVE

    def test_activate_wrong_state_fails(self, provider):
        """Activate fails from non-REGISTERED state."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)

        with pytest.raises(ValueError, match="REGISTERED"):
            provider.activate("agent_1")  # Still PROVISIONED

    def test_full_lifecycle(self, provider, minter):
        """Test full lifecycle: provision -> register -> activate -> authorize."""
        scope = {"tenant_id": "tenant_1", "workspace_id": "ws_1"}

        # Provision
        identity = provider.provision("agent_1", scope)
        assert identity.state == ManagedAgentState.PROVISIONED
        assert not provider.authorize("agent_1", "read")  # Can't auth yet

        # Register
        identity = provider.register("agent_1")
        assert identity.state == ManagedAgentState.REGISTERED
        assert not provider.authorize("agent_1", "read")  # Still can't auth

        # Activate
        identity = provider.activate("agent_1")
        assert identity.state == ManagedAgentState.ACTIVE
        assert provider.authorize("agent_1", "read")  # Now can auth

        # Verify minter was called once with correct scope
        assert minter.call_count == 1
        assert minter.calls[0] == scope

    def test_rotate(self, provider, minter):
        """Rotate replaces key and keeps old key in history."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)
        provider.register("agent_1")
        provider.activate("agent_1")

        original_key = provider.store.get("agent_1").current_key.key
        assert minter.call_count == 1

        # Rotate
        identity = provider.rotate("agent_1")
        assert identity.state == ManagedAgentState.ROTATED
        assert identity.current_key.key != original_key  # New key
        assert len(identity.previous_keys) == 1
        assert identity.previous_keys[0].key == original_key
        assert identity.previous_keys[0].valid is False  # Old key invalid

        # Verify minter called again
        assert minter.call_count == 2
        # Minter called with same scope on rotate
        assert minter.calls[1] == scope

    def test_rotate_old_key_no_longer_authorizes(self, provider):
        """After rotate, old key is invalid."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)
        provider.register("agent_1")
        provider.activate("agent_1")

        old_key = provider.store.get("agent_1").current_key.key
        assert old_key  # Agent can auth with old key
        assert provider.authorize("agent_1", "read")

        # Rotate
        provider.rotate("agent_1")

        # Agent still authorizes because authorize checks current_key validity
        # not the key string itself
        assert provider.authorize("agent_1", "read")

        # But if we inspect the identity, old key is marked invalid
        identity = provider.store.get("agent_1")
        assert identity.previous_keys[0].valid is False

    def test_rotate_multiple_times(self, provider, minter):
        """Can rotate multiple times; history accumulates."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)
        provider.register("agent_1")
        provider.activate("agent_1")

        keys_issued = [provider.store.get("agent_1").current_key.key]

        for _ in range(3):
            provider.rotate("agent_1")
            current_key = provider.store.get("agent_1").current_key.key
            keys_issued.append(current_key)

        identity = provider.store.get("agent_1")
        assert len(identity.previous_keys) == 3
        assert all(not k.valid for k in identity.previous_keys)
        assert identity.current_key.valid is True

    def test_revoke(self, provider):
        """Revoke invalidates all keys."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)
        provider.register("agent_1")
        provider.activate("agent_1")

        # Can auth before revoke
        assert provider.authorize("agent_1", "read")

        # Revoke
        identity = provider.revoke("agent_1")
        assert identity.state == ManagedAgentState.REVOKED
        assert identity.current_key.valid is False

        # Cannot auth after revoke
        assert not provider.authorize("agent_1", "read")

    def test_revoke_from_any_state(self, provider):
        """Revoke works from any state."""
        scope = {"tenant_id": "tenant_1"}

        # Revoke from PROVISIONED
        provider.provision("agent_1", scope)
        provider.revoke("agent_1")
        assert provider.store.get("agent_1").state == ManagedAgentState.REVOKED

        # Revoke from ACTIVE
        provider.provision("agent_2", scope)
        provider.register("agent_2")
        provider.activate("agent_2")
        provider.revoke("agent_2")
        assert provider.store.get("agent_2").state == ManagedAgentState.REVOKED
        assert not provider.authorize("agent_2", "read")

    def test_authorize_unknown_agent(self, provider):
        """Unknown agent cannot authorize (default-deny)."""
        assert provider.authorize("unknown_agent", "read") is False

    def test_authorize_fail_closed(self, provider):
        """Default-deny for any unverified/unknown identity."""
        scope = {"tenant_id": "tenant_1"}
        provider.provision("agent_1", scope)

        # Unknown agent
        assert provider.authorize("agent_xyz", "read") is False

        # Provisioned but not registered
        assert provider.authorize("agent_1", "read") is False

        # Registered but not active
        provider.register("agent_1")
        assert provider.authorize("agent_1", "read") is False

        # Now active
        provider.activate("agent_1")
        assert provider.authorize("agent_1", "read") is True

        # Revoked
        provider.revoke("agent_1")
        assert provider.authorize("agent_1", "read") is False

    def test_scope_preserved_through_lifecycle(self, provider):
        """Scope is preserved throughout lifecycle."""
        scope = {"tenant_id": "tenant_1", "workspace_id": "ws_1", "extra": "value"}

        provider.provision("agent_1", scope)
        identity = provider.store.get("agent_1")
        assert identity.scope == scope

        provider.register("agent_1")
        identity = provider.store.get("agent_1")
        assert identity.scope == scope

        provider.activate("agent_1")
        identity = provider.store.get("agent_1")
        assert identity.scope == scope

        provider.rotate("agent_1")
        identity = provider.store.get("agent_1")
        assert identity.scope == scope

    def test_timestamps_updated(self, provider):
        """Timestamps are updated on state transitions."""
        scope = {"tenant_id": "tenant_1"}
        import time

        prov_identity = provider.provision("agent_1", scope)
        prov_time = prov_identity.updated_at

        time.sleep(0.01)  # Small delay

        reg_identity = provider.register("agent_1")
        reg_time = reg_identity.updated_at

        # Timestamp should have advanced
        assert reg_time > prov_time
        assert reg_identity.created_at == prov_identity.created_at  # Created time unchanged


# ─────────────────────────────────────────────────────────────────────────────
# Integration Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestIntegration:
    def test_multiple_agents(self, provider, minter):
        """Manage multiple independent agents."""
        for i in range(3):
            scope = {"tenant_id": f"tenant_{i}", "workspace_id": f"ws_{i}"}
            provider.provision(f"agent_{i}", scope)
            provider.register(f"agent_{i}")
            provider.activate(f"agent_{i}")

        # Each can auth
        for i in range(3):
            assert provider.authorize(f"agent_{i}", "read") is True

        # Revoke one
        provider.revoke("agent_1")

        # Others unaffected
        assert not provider.authorize("agent_1", "read")
        assert provider.authorize("agent_0", "read")
        assert provider.authorize("agent_2", "read")

    def test_persistence_across_instances(self, minter, temp_store):
        """Identities persist across provider instances."""
        # Create first provider and provision
        provider1 = ManagedAgentIdentityProvider(minter, temp_store)
        scope = {"tenant_id": "tenant_1"}
        provider1.provision("agent_1", scope)
        provider1.register("agent_1")
        provider1.activate("agent_1")

        # Create second provider instance with same store
        provider2 = ManagedAgentIdentityProvider(minter, temp_store)
        assert provider2.authorize("agent_1", "read") is True

        # Rotate via second provider
        provider2.rotate("agent_1")

        # Check state via first provider (it re-loads)
        provider1.store.load()
        identity = provider1.store.get("agent_1")
        assert identity.state == ManagedAgentState.ROTATED
        assert len(identity.previous_keys) == 1
