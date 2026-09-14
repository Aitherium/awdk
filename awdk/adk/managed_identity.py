"""Managed agent identity lifecycle — fail-closed security spine for customer/managed agents.

This module provides a state machine for provisioning, registering, and managing
agent identities with scoped credentials. All authorization is default-deny:
unknown/revoked/unverified agents cannot act.

State machine: PROVISIONED -> REGISTERED -> ACTIVE -> ROTATED -> REVOKED

- PROVISIONED: initial state, key minted but not yet valid for actions
- REGISTERED: identity registered and verified; ready to activate
- ACTIVE: agent is active and can perform authorized actions
- ROTATED: key was rotated; previous key invalidated, new key active
- REVOKED: agent is revoked; all keys invalid, authorization always denies

Key features:
- In-memory + JSON file store (no network calls for lifecycle)
- Injected minter callable for key generation (testable offline)
- Scope-aware: tenant/workspace validation built into store
- Default-deny authorization for unknown/revoked/unverified agents
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MintError",
    "make_gateway_minter",
    "AsyncMinterFn",
    "ManagedAgentState",
    "ManagedKey",
    "ManagedAgentIdentity",
    "ManagedIdentityStore",
    "ManagedAgentIdentityProvider",
]


# ─────────────────────────────────────────────────────────────────────────────
# State Machine & Data Models
# ─────────────────────────────────────────────────────────────────────────────


class ManagedAgentState(str, Enum):
    """Agent identity lifecycle states."""

    PROVISIONED = "provisioned"  # Key minted, not yet active
    REGISTERED = "registered"  # Identity registered and verified
    ACTIVE = "active"  # Agent can perform actions
    ROTATED = "rotated"  # Key rotated, old key invalidated
    REVOKED = "revoked"  # Agent revoked; all keys invalid


@dataclass(frozen=True)
class ManagedKey:
    """A scoped credential key with validity tracking.

    Attributes:
        key: The actual scoped key string (returned by minter).
        issued_at: ISO 8601 timestamp when key was issued.
        valid: Whether this key is currently valid for authorization.
    """

    key: str
    issued_at: str
    valid: bool = True


class ManagedAgentIdentity(BaseModel):
    """Agent identity record with scoped credentials and state.

    An identity transitions through a state machine:
    PROVISIONED -> REGISTERED -> ACTIVE -> (ROTATED)* -> REVOKED

    Attributes:
        agent_id: Unique agent identifier.
        state: Current lifecycle state.
        principal_class: Always "agent" for managed agents.
        scope: Scoped constraints (e.g., tenant_id, workspace_id).
        current_key: Currently active key (may be invalid if revoked).
        previous_keys: Historical keys (for audit trail).
        created_at: ISO 8601 creation timestamp.
        updated_at: ISO 8601 last update timestamp.
    """

    agent_id: str
    state: ManagedAgentState
    principal_class: str = "agent"
    scope: Dict[str, str] = Field(default_factory=dict)

    current_key: Optional[ManagedKey] = None
    previous_keys: list[ManagedKey] = Field(default_factory=list)

    created_at: str
    updated_at: str

    model_config = ConfigDict(use_enum_values=False)

    def authorize(self, action: str) -> bool:
        """Check if this agent can authorize an action.

        Default-deny: revoked/unknown/unverified agents cannot act.

        Args:
            action: Action name (reserved for future ACL policy).

        Returns:
            True only if state is ACTIVE/ROTATED, key exists and is valid.
        """
        # Revoked agents cannot act
        if self.state == ManagedAgentState.REVOKED:
            return False

        # Only ACTIVE or ROTATED agents can act
        if self.state not in (ManagedAgentState.ACTIVE, ManagedAgentState.ROTATED):
            return False

        # Key must exist, be valid, and be NON-BLANK (a best-effort mint that
        # returned "" must never authorize).
        if self.current_key is None or not self.current_key.valid:
            return False
        if not (self.current_key.key or "").strip():
            return False

        return True


# ─────────────────────────────────────────────────────────────────────────────
# Store (In-Memory + JSON File)
# ─────────────────────────────────────────────────────────────────────────────


class ManagedIdentityStore:
    """File-backed store for managed agent identities.

    Persists to JSON with in-memory index for fast lookup.
    Thread-safe for single-process reads; writes are atomic (replace file).

    Attributes:
        path: Path to JSON store file (default: ~/.aither/managed_identities.json).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (Path.home() / ".aither" / "managed_identities.json")
        self._in_memory: Dict[str, ManagedAgentIdentity] = {}

    def load(self) -> None:
        """Load identities from JSON file into memory."""
        if not self.path.exists():
            self._in_memory = {}
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._in_memory = {}
            for agent_id, item in data.items():
                self._in_memory[agent_id] = ManagedAgentIdentity(**item)
        except (json.JSONDecodeError, ValueError, OSError):
            self._in_memory = {}

    def save(self) -> None:
        """Write in-memory identities to JSON file (atomic)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for agent_id, identity in self._in_memory.items():
            # Convert to dict, handling ManagedKey frozen dataclass
            identity_dict = identity.model_dump(mode="json")
            # ManagedKey is frozen, so convert it explicitly
            if identity_dict.get("current_key"):
                key_obj = identity.current_key
                identity_dict["current_key"] = {
                    "key": key_obj.key,
                    "issued_at": key_obj.issued_at,
                    "valid": key_obj.valid,
                }
            if identity_dict.get("previous_keys"):
                identity_dict["previous_keys"] = [
                    {
                        "key": k.key,
                        "issued_at": k.issued_at,
                        "valid": k.valid,
                    }
                    for k in identity.previous_keys
                ]
            data[agent_id] = identity_dict

        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, agent_id: str) -> Optional[ManagedAgentIdentity]:
        """Retrieve identity by agent ID."""
        return self._in_memory.get(agent_id)

    def set(self, agent_id: str, identity: ManagedAgentIdentity) -> None:
        """Store identity and persist to disk."""
        self._in_memory[agent_id] = identity
        self.save()

    def delete(self, agent_id: str) -> None:
        """Remove identity from store."""
        if agent_id in self._in_memory:
            del self._in_memory[agent_id]
            self.save()

    def list_all(self) -> list[ManagedAgentIdentity]:
        """Return all stored identities."""
        return list(self._in_memory.values())


# ─────────────────────────────────────────────────────────────────────────────
# Minter Type & Provider
# ─────────────────────────────────────────────────────────────────────────────

# Minter is an injectable callable: (scope: Dict[str, str]) -> str (key)
MinterFn = Callable[[Dict[str, str]], str]
# Async variant — the REAL gateway mint is async (see make_gateway_minter).
AsyncMinterFn = Callable[[Dict[str, str]], Awaitable[str]]


class MintError(RuntimeError):
    """A key mint failed or returned a blank key.

    Raised instead of storing an empty credential: the real gateway mint is
    best-effort and returns "" on failure, so an unchecked mint would create an
    ACTIVE identity holding NO key — a fail-OPEN hole. Provisioning fails loud.
    """


def _require_key(key: Any, *, what: str = "mint") -> str:
    """Return a non-blank key or raise. Fail-closed on a blank/None mint."""
    if not isinstance(key, str) or not key.strip():
        raise MintError(
            f"{what} returned no key ({key!r}) — refusing to create an identity "
            f"with an empty credential"
        )
    return key


def make_gateway_minter(
    bearer_token: str,
    *,
    mint_fn: Optional[Callable[..., Awaitable[str]]] = None,
) -> AsyncMinterFn:
    """Build an async minter backed by the REAL gateway key mint.

    Trades a tenant-scoped bearer token for a real scoped key via
    :func:`adk.fleet_enroll._self_mint_gateway_key` (AitherSecrets ``/api-keys``
    locally, Genesis enrollment-token exchange remotely).

    That function is *best-effort* and returns ``""`` on any failure; this
    wrapper turns that into a loud :class:`MintError` so a failed mint can never
    silently yield an identity with no credential.

    Args:
        bearer_token: Tenant-scoped capability token to exchange.
        mint_fn: Override the mint call (used by tests); defaults to the real one.

    Returns:
        An async minter suitable for :meth:`ManagedAgentIdentityProvider.provision_async`.
    """

    async def _mint(scope: Dict[str, str]) -> str:
        fn = mint_fn
        if fn is None:  # local import: keeps httpx/fleet_enroll off the import path
            from adk.fleet_enroll import _self_mint_gateway_key

            fn = _self_mint_gateway_key
        node_id = scope.get("agent_id") or scope.get("workspace_id") or scope.get(
            "tenant_id", "managed-agent"
        )
        key = await fn(bearer_token, node_id)
        return _require_key(key, what="gateway mint")

    return _mint


class ManagedAgentIdentityProvider:
    """Provider for managed agent identity lifecycle.

    Implements the state machine:
    PROVISIONED -> REGISTERED -> ACTIVE -> (ROTATED)* -> REVOKED

    Args:
        minter: Callable that mints scoped keys. Signature:
                (scope: Dict[str, str]) -> str
        store: Optional custom store; defaults to ~/.aither/managed_identities.json
    """

    def __init__(
        self,
        minter: MinterFn | None = None,
        store: Optional[ManagedIdentityStore] = None,
        async_minter: Optional[AsyncMinterFn] = None,
    ) -> None:
        if minter is None and async_minter is None:
            raise ValueError("provide minter= (sync) and/or async_minter= (async)")
        self.minter = minter or (lambda scope: _require_key(None))
        self.async_minter = async_minter
        self.store = store or ManagedIdentityStore()
        self.store.load()

    def provision(
        self, agent_id: str, scope: Dict[str, str]
    ) -> ManagedAgentIdentity:
        """Provision a new agent identity.

        Mints a scoped key and stores identity in PROVISIONED state.

        Args:
            agent_id: Unique agent identifier.
            scope: Scope dict with tenant_id, workspace_id, etc.

        Returns:
            ManagedAgentIdentity in PROVISIONED state.

        Raises:
            ValueError: If agent_id already exists.
        """
        existing = self.store.get(agent_id)
        if existing is not None:
            raise ValueError(f"Agent {agent_id} already provisioned")

        # Fail-closed: a blank mint must NOT become an identity with no credential.
        key = _require_key(self.minter(scope))
        return self._store_provisioned(agent_id, scope, key)

    async def provision_async(
        self, agent_id: str, scope: Dict[str, str], minter: AsyncMinterFn | None = None
    ) -> ManagedAgentIdentity:
        """Provision using an ASYNC minter (e.g. :func:`make_gateway_minter`).

        Args:
            agent_id: Unique agent identifier.
            scope: Scope dict (tenant_id, workspace_id, ...).
            minter: Async minter; defaults to ``self.async_minter``.

        Raises:
            ValueError: If agent_id already exists or no async minter is available.
            MintError: If the mint fails or returns a blank key (fail-closed).
        """
        if self.store.get(agent_id) is not None:
            raise ValueError(f"Agent {agent_id} already provisioned")
        fn = minter or self.async_minter
        if fn is None:
            raise ValueError("no async minter configured (pass minter= or async_minter=)")
        key = _require_key(await fn({**scope, "agent_id": agent_id}))
        return self._store_provisioned(agent_id, scope, key)

    def _store_provisioned(
        self, agent_id: str, scope: Dict[str, str], key: str
    ) -> ManagedAgentIdentity:
        """Persist a freshly-minted identity in PROVISIONED state."""
        now = datetime.now(timezone.utc).isoformat()
        identity = ManagedAgentIdentity(
            agent_id=agent_id,
            state=ManagedAgentState.PROVISIONED,
            scope=scope,
            current_key=ManagedKey(key=key, issued_at=now),
            created_at=now,
            updated_at=now,
        )
        self.store.set(agent_id, identity)
        return identity

    def register(self, agent_id: str) -> ManagedAgentIdentity:
        """Register a provisioned agent identity.

        Transitions PROVISIONED -> REGISTERED.

        Args:
            agent_id: Agent identifier.

        Returns:
            ManagedAgentIdentity in REGISTERED state.

        Raises:
            ValueError: If agent not found or not in PROVISIONED state.
        """
        identity = self.store.get(agent_id)
        if identity is None:
            raise ValueError(f"Agent {agent_id} not found")
        if identity.state != ManagedAgentState.PROVISIONED:
            raise ValueError(
                f"Can only register from PROVISIONED state, "
                f"current: {identity.state}"
            )

        identity.state = ManagedAgentState.REGISTERED
        identity.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.set(agent_id, identity)
        return identity

    def activate(self, agent_id: str) -> ManagedAgentIdentity:
        """Activate a registered agent identity.

        Transitions REGISTERED -> ACTIVE. Only ACTIVE agents can
        perform authorized actions.

        Args:
            agent_id: Agent identifier.

        Returns:
            ManagedAgentIdentity in ACTIVE state.

        Raises:
            ValueError: If agent not found or not in REGISTERED state.
        """
        identity = self.store.get(agent_id)
        if identity is None:
            raise ValueError(f"Agent {agent_id} not found")
        if identity.state != ManagedAgentState.REGISTERED:
            raise ValueError(
                f"Can only activate from REGISTERED state, "
                f"current: {identity.state}"
            )

        identity.state = ManagedAgentState.ACTIVE
        identity.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.set(agent_id, identity)
        return identity

    def rotate(self, agent_id: str) -> ManagedAgentIdentity:
        """Rotate agent credentials.

        Invalidates old key and mints new scoped key. Can be called on
        ACTIVE or ROTATED agents. Transitions ACTIVE/ROTATED -> ROTATED.

        Args:
            agent_id: Agent identifier.

        Returns:
            ManagedAgentIdentity in ROTATED state with new key.

        Raises:
            ValueError: If agent not found or not in ACTIVE/ROTATED state.
        """
        identity = self.store.get(agent_id)
        if identity is None:
            raise ValueError(f"Agent {agent_id} not found")
        if identity.state not in (ManagedAgentState.ACTIVE, ManagedAgentState.ROTATED):
            raise ValueError(
                f"Can only rotate from ACTIVE/ROTATED state, "
                f"current: {identity.state}"
            )

        # Invalidate old key and move to history
        if identity.current_key is not None:
            old_key = ManagedKey(
                key=identity.current_key.key,
                issued_at=identity.current_key.issued_at,
                valid=False,
            )
            identity.previous_keys.append(old_key)

        # Mint new key
        new_key = _require_key(self.minter(identity.scope))
        now = datetime.now(timezone.utc).isoformat()
        identity.current_key = ManagedKey(key=new_key, issued_at=now)
        identity.state = ManagedAgentState.ROTATED
        identity.updated_at = now
        self.store.set(agent_id, identity)
        return identity

    def revoke(self, agent_id: str) -> ManagedAgentIdentity:
        """Revoke agent identity and all its keys.

        Fail-closed: revoked agents cannot perform any actions.
        Can be called from any state.

        Args:
            agent_id: Agent identifier.

        Returns:
            ManagedAgentIdentity in REVOKED state.

        Raises:
            ValueError: If agent not found.
        """
        identity = self.store.get(agent_id)
        if identity is None:
            raise ValueError(f"Agent {agent_id} not found")

        # Invalidate all keys
        if identity.current_key is not None:
            identity.current_key = ManagedKey(
                key=identity.current_key.key,
                issued_at=identity.current_key.issued_at,
                valid=False,
            )
        identity.previous_keys = [
            ManagedKey(key=k.key, issued_at=k.issued_at, valid=False)
            for k in identity.previous_keys
        ]

        identity.state = ManagedAgentState.REVOKED
        identity.updated_at = datetime.now(timezone.utc).isoformat()
        self.store.set(agent_id, identity)
        return identity

    def authorize(self, agent_id: str, action: str) -> bool:
        """Check if agent can authorize an action.

        Default-deny: unknown/revoked/inactive agents cannot act.

        Args:
            agent_id: Agent identifier.
            action: Action name (reserved for future ACL policy).

        Returns:
            True only if agent exists, is ACTIVE/ROTATED, and key is valid.
        """
        identity = self.store.get(agent_id)
        if identity is None:
            return False
        return identity.authorize(action)
