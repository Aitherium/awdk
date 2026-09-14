"""The Agent Onboarding Fabric must be reachable from the top-level `adk` namespace.

Every fabric capability is wired through `adk/__init__.py`'s lazy `__getattr__`.
A typo in one of those branches is invisible until a user hits it, and the modules
themselves keep passing their own tests — so assert the public surface directly.
"""
import importlib

import pytest

import adk

FABRIC_EXPORTS = [
    # ACP — both directions
    "ACPClient",
    "ACPServer",
    "serve_stdio",
    "ACPPromptResult",
    "ACPToolCall",
    # Universal pack + supervision
    "AgentPackManifest",
    "AgentHandle",
    "Supervisor",
    "load_agent_pack",
    # Managed identity
    "ManagedAgentIdentityProvider",
    "ManagedAgentIdentity",
    "ManagedAgentState",
    # Zero-code connect
    "render_connect",
    "SUPPORTED_FRAMEWORKS",
    # Validation
    "CodeValidator",
    "ValidationIssue",
    "ValidationContext",
    # Code as action
    "CodeActLoop",
    # Pass by reference
    "ObjectRegistry",
    "get_registry",
    "render_observation",
    # Pack drivers + distribution
    "ToolCall",
    "DriverResult",
    "ProtocolDriver",
    "get_driver",
    "PackRegistry",
    "PublishReceipt",
    "PackSummary",
    "validate_pack",
    # Harness APIs + ellipsis ergonomics
    "EventLog",
    "ContextBlocks",
    "has_ellipsis_body",
    "strategy",
    "get_strategy_meta",
]


@pytest.mark.parametrize("name", FABRIC_EXPORTS)
def test_fabric_export_resolves(name):
    assert getattr(adk, name) is not None


@pytest.mark.parametrize("name", FABRIC_EXPORTS)
def test_fabric_export_is_declared(name):
    assert name in adk.__all__, f"{name} resolves but is missing from adk.__all__"


def test_unknown_attribute_still_raises():
    with pytest.raises(AttributeError):
        adk.definitely_not_a_real_export  # noqa: B018


@pytest.mark.parametrize(
    "module",
    [
        "adk.core.codeact",
        "adk.object_registry",
        "adk.pack_drivers",
        "adk.pack_registry",
        "adk.harness",
        "adk.ellipsis",
    ],
)
def test_module_imports_standalone(module):
    """Each module must import without pulling in the whole adk surface."""
    assert importlib.import_module(module) is not None
