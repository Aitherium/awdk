"""Real gateway-key minting for managed agent identities — fail-closed.

Closes the residual "minter is offline/injectable, not wired to the real
gateway mint". `make_gateway_minter` wraps
`adk.fleet_enroll._self_mint_gateway_key`, which is BEST-EFFORT and returns ""
on any failure — unwrapped, that produced an ACTIVE identity holding an EMPTY
credential that `authorize()` happily approved (fail-OPEN). These tests pin the
fail-closed behaviour.
"""
import asyncio

import pytest

from adk.managed_identity import (
    ManagedAgentIdentityProvider,
    ManagedAgentState,
    ManagedIdentityStore,
    MintError,
    make_gateway_minter,
)

SCOPE = {"tenant_id": "acme", "workspace_id": "ws1"}


def _provider(tmp_path, minter=None, async_minter=None):
    store = ManagedIdentityStore(path=tmp_path / "ids.json")
    return ManagedAgentIdentityProvider(
        minter=minter, store=store, async_minter=async_minter
    )


# --- the real-mint wrapper ---------------------------------------------------

def test_gateway_minter_returns_key(tmp_path):
    async def fake_mint(bearer, node_id):
        assert bearer == "tok-123"
        assert node_id == "agent-1"  # scope.agent_id is threaded through
        return "aither_sk_live_abc"

    p = _provider(tmp_path, async_minter=make_gateway_minter("tok-123", mint_fn=fake_mint))
    ident = asyncio.run(p.provision_async("agent-1", SCOPE))
    assert ident.current_key.key == "aither_sk_live_abc"
    assert ident.state == ManagedAgentState.PROVISIONED
    assert ident.principal_class == "agent"


def test_gateway_minter_blank_key_raises_not_stored(tmp_path):
    """The real mint returns '' on failure — that must FAIL, not create an identity."""
    async def failing_mint(bearer, node_id):
        return ""  # exactly what _self_mint_gateway_key does on any error

    p = _provider(tmp_path, async_minter=make_gateway_minter("tok", mint_fn=failing_mint))
    with pytest.raises(MintError):
        asyncio.run(p.provision_async("agent-x", SCOPE))
    assert p.store.get("agent-x") is None, "no identity may be stored on a failed mint"


def test_gateway_minter_whitespace_key_raises(tmp_path):
    async def blank_mint(bearer, node_id):
        return "   "

    p = _provider(tmp_path, async_minter=make_gateway_minter("tok", mint_fn=blank_mint))
    with pytest.raises(MintError):
        asyncio.run(p.provision_async("agent-y", SCOPE))


def test_sync_provision_rejects_blank_mint(tmp_path):
    p = _provider(tmp_path, minter=lambda scope: "")
    with pytest.raises(MintError):
        p.provision("agent-z", SCOPE)
    assert p.store.get("agent-z") is None


# --- the fail-open hole this closed ------------------------------------------

def test_empty_key_never_authorizes(tmp_path):
    """Belt-and-braces: even if an empty key reached storage, it must not authorize."""
    from adk.managed_identity import ManagedAgentIdentity, ManagedKey

    ident = ManagedAgentIdentity(
        agent_id="a",
        state=ManagedAgentState.ACTIVE,
        scope=SCOPE,
        current_key=ManagedKey(key="", issued_at="now", valid=True),
        created_at="now",
        updated_at="now",
    )
    assert ident.authorize("anything") is False


def test_rotate_rejects_blank_mint(tmp_path):
    keys = iter(["good-key-1", ""])  # second mint fails
    p = _provider(tmp_path, minter=lambda scope: next(keys))
    p.provision("agent-r", SCOPE)
    p.register("agent-r")
    p.activate("agent-r") if hasattr(p, "activate") else None
    with pytest.raises(MintError):
        p.rotate("agent-r")


# --- guards --------------------------------------------------------------------

def test_provider_requires_a_minter(tmp_path):
    with pytest.raises(ValueError):
        ManagedAgentIdentityProvider(store=ManagedIdentityStore(path=tmp_path / "s.json"))


def test_provision_async_without_minter_raises(tmp_path):
    p = _provider(tmp_path, minter=lambda scope: "k")
    with pytest.raises(ValueError):
        asyncio.run(p.provision_async("a2", SCOPE))


def test_provision_async_rejects_duplicate(tmp_path):
    async def mint(bearer, node_id):
        return "k1"

    p = _provider(tmp_path, async_minter=make_gateway_minter("t", mint_fn=mint))
    asyncio.run(p.provision_async("dup", SCOPE))
    with pytest.raises(ValueError):
        asyncio.run(p.provision_async("dup", SCOPE))
