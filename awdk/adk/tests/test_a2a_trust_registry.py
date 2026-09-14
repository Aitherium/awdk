"""Dynamic A2A trust via the authoritative cloud agent registry.

Proves _is_key_trusted consults the registry (owner-authed, per-tenant, source of
truth) for a mesh peer / portal-trust mode, fail-closed on an unknown or missing
key. Uses a monkeypatched registry fetch so no network / live mesh is needed.
"""
import asyncio

from adk import a2a_trust


def _reset_cache():
    a2a_trust._REGISTRY_KEYS_CACHE["keys"] = None
    a2a_trust._REGISTRY_KEYS_CACHE["ts"] = 0.0


def _with_registry(monkeypatch, keys):
    """Stub the registry fetch VIA monkeypatch so it is auto-restored after the
    test (a plain reassignment leaks across tests and corrupts later ones)."""
    async def _fake():
        return set(keys)
    monkeypatch.setattr(a2a_trust, "_fetch_registry_trusted_keys", _fake)
    _reset_cache()


GOOD = "a" * 64
OTHER = "b" * 64


def test_registry_key_trusted_for_mesh_peer(monkeypatch):
    monkeypatch.delenv("AITHER_A2A_TRUSTED_KEYS", raising=False)
    _with_registry(monkeypatch, {GOOD})
    trusted, reason = asyncio.run(a2a_trust._is_key_trusted(GOOD, node_id="node-1"))
    assert trusted is True and "registry" in reason.lower()


def test_registry_unknown_key_denied(monkeypatch):
    monkeypatch.delenv("AITHER_A2A_TRUSTED_KEYS", raising=False)
    _with_registry(monkeypatch, {OTHER})
    trusted, _ = asyncio.run(a2a_trust._is_key_trusted(GOOD, node_id="node-1"))
    assert trusted is False  # verified sig but key not registered -> fail-closed


def test_empty_registry_fails_closed(monkeypatch):
    monkeypatch.delenv("AITHER_A2A_TRUSTED_KEYS", raising=False)
    _with_registry(monkeypatch, set())  # registry outage / no token -> empty
    trusted, _ = asyncio.run(a2a_trust._is_key_trusted(GOOD, node_id="node-1"))
    assert trusted is False


def test_registry_not_consulted_without_node_or_portal(monkeypatch):
    """No node_id and portal-trust off -> registry is NOT consulted (static only)."""
    monkeypatch.delenv("AITHER_A2A_TRUSTED_KEYS", raising=False)
    monkeypatch.delenv("AITHER_A2A_TRUST_FROM_PORTAL", raising=False)
    called = {"n": 0}

    async def _fake():
        called["n"] += 1
        return {GOOD}

    monkeypatch.setattr(a2a_trust, "_fetch_registry_trusted_keys", _fake)
    _reset_cache()
    trusted, _ = asyncio.run(a2a_trust._is_key_trusted(GOOD, node_id=None))
    assert trusted is False and called["n"] == 0


def test_portal_mode_consults_registry_without_node(monkeypatch):
    monkeypatch.delenv("AITHER_A2A_TRUSTED_KEYS", raising=False)
    monkeypatch.setenv("AITHER_A2A_TRUST_FROM_PORTAL", "true")
    _with_registry(monkeypatch, {GOOD})
    trusted, _ = asyncio.run(a2a_trust._is_key_trusted(GOOD, node_id=None))
    assert trusted is True


def test_static_allowlist_still_wins(monkeypatch):
    monkeypatch.setenv("AITHER_A2A_TRUSTED_KEYS", GOOD)
    _with_registry(monkeypatch, set())  # registry empty, but static allowlist has it
    trusted, reason = asyncio.run(a2a_trust._is_key_trusted(GOOD))
    assert trusted is True and "TRUSTED_KEYS" in reason


def test_fetch_extracts_keys_from_a2a_fleet(monkeypatch):
    """POSITIVE wiring test (pattern #5): _fetch_registry_trusted_keys must use the
    A2A-FLEET source that CARRIES public_key — NOT _fetch_registry (which drops it,
    making the lookup inert). Mock the real fleet fetch and assert keys come back."""
    import types

    from adk import mesh_discovery

    def _ref(name, pk):
        return types.SimpleNamespace(name=name, public_key=pk)

    async def _fake_fleet(warnings):
        return [_ref("atlas", GOOD), _ref("iris", OTHER), _ref("nokey", "")]

    monkeypatch.setattr(mesh_discovery, "_fetch_a2a_fleet", _fake_fleet)
    keys = asyncio.run(a2a_trust._fetch_registry_trusted_keys())
    assert keys == {GOOD, OTHER}  # extracted both real keys, dropped the empty one
