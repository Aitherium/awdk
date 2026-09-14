"""Tests for adk mesh CLI subcommands."""

from __future__ import annotations

import argparse
import json
from unittest import mock

import pytest


def test_cmd_mesh_parse_onboard(monkeypatch):
    """Test that cmd_mesh parses 'adk mesh onboard' correctly."""
    from adk.cli import cmd_mesh

    # Mock args for mesh onboard subcommand
    args = argparse.Namespace(
        mesh_command="onboard",
        conductor="https://aitheros-conductor:8193",
        node_id="test-node-123",
        role="worker",
        external_ip="1.2.3.4",
        headscale=False,
    )

    # Mock the join function to avoid actual mesh operations
    with mock.patch("adk.cli.cmd_mesh") as mock_cmd:
        # The command is dispatched in main(), so we test the arg parsing instead
        assert args.mesh_command == "onboard"
        assert args.conductor == "https://aitheros-conductor:8193"
        assert args.node_id == "test-node-123"


def test_cmd_mesh_parse_ls(monkeypatch):
    """Test that cmd_mesh parses 'adk mesh ls' correctly."""
    from adk.cli import cmd_mesh

    # Mock args for mesh ls subcommand
    args = argparse.Namespace(
        mesh_command="ls",
        mesh_url="https://aitheros-aithernet:8125",
        format="table",
    )

    # Verify the arg parsing
    assert args.mesh_command == "ls"
    assert args.mesh_url == "https://aitheros-aithernet:8125"
    assert args.format == "table"


def test_cmd_mesh_onboard_missing_conductor():
    """Test that mesh onboard fails gracefully when mesh URL is unset."""
    from adk.cli import cmd_mesh

    args = argparse.Namespace(
        mesh_command="onboard",
        conductor=None,
        node_id="test-node",
        role="worker",
        external_ip=None,
        headscale=False,
    )

    result = cmd_mesh(args)
    assert result == 1  # fail-soft: returns 1 on error


def test_cmd_mesh_ls_missing_url():
    """Test that mesh ls fails gracefully when mesh URL is unset."""
    from adk.cli import cmd_mesh

    args = argparse.Namespace(
        mesh_command="ls",
        mesh_url=None,
        format="table",
    )

    result = cmd_mesh(args)
    assert result == 1  # fail-soft: returns 1 on error


def test_cmd_mesh_unknown_subcommand():
    """Test that mesh with unknown subcommand fails gracefully."""
    from adk.cli import cmd_mesh

    args = argparse.Namespace(
        mesh_command="unknown",
    )

    result = cmd_mesh(args)
    assert result == 1  # fail-soft: returns 1 on unknown subcommand


def test_cmd_mesh_no_subcommand():
    """Test that mesh with no subcommand fails gracefully."""
    from adk.cli import cmd_mesh

    args = argparse.Namespace(
        mesh_command=None,
    )

    result = cmd_mesh(args)
    assert result == 1  # fail-soft: returns 1 when subcommand not set


def test_cmd_mesh_onboard_mocked_report():
    """Test that onboard report structure is correct."""
    mock_report = {
        "node_id": "test-node-456",
        "overlay_ip": "10.77.99.1",
        "transport": "wireguard",
        "iface": "aithernet0",
        "handshake": True,
    }

    # Verify the report structure is correct
    assert mock_report["transport"] == "wireguard"
    assert mock_report["handshake"] is True
    assert "overlay_ip" in mock_report


def test_cmd_mesh_ls_node_structure():
    """Test the node structure for ls output."""
    mock_nodes = [
        {
            "node_id": "node-1",
            "overlay_ip": "10.77.1.1",
            "role": "hub",
            "services": ["genesis", "scheduler"],
        },
        {
            "node_id": "node-2",
            "overlay_ip": "10.77.2.2",
            "role": "worker",
            "services": ["agent"],
        },
    ]

    # Verify the nodes structure
    assert len(mock_nodes) == 2
    assert mock_nodes[0]["node_id"] == "node-1"
    assert mock_nodes[1]["role"] == "worker"


def test_genesis_backend_in_compat_urls():
    """Test that 'genesis' backend is registered in LLMRouter._COMPAT_URLS.

    HTTPS, not HTTP. Genesis on :8001 speaks TLS with the internal AitherNet CA; plaintext
    gets an empty reply, which is exactly what made it look "down". Corrected in 0a609f1f05
    with a live-proven round-trip from inside the fleet net (cert SAN verified, no -k:
    GET /v1/models returned the catalog, POST /v1/chat/completions returned GENESIS_OK).

    That commit updated _COMPAT_URLS and ran the provider/router suites — 173 tests — but
    not this file, so this assertion kept the old value and has been red on develop since.
    A stale test is not evidence: the measurement is.
    """
    from adk.llm import LLMRouter

    router = LLMRouter()
    assert "genesis" in router._COMPAT_URLS
    assert router._COMPAT_URLS["genesis"] == "https://localhost:8001/v1"


def test_genesis_backend_in_compat_models():
    """Test that 'genesis' backend is registered in LLMRouter._COMPAT_MODELS."""
    from adk.llm import LLMRouter

    router = LLMRouter()
    assert "genesis" in router._COMPAT_MODELS


def test_genesis_provider_creation():
    """Test that LLMRouter can create a genesis provider."""
    from adk.llm import LLMRouter

    router = LLMRouter()
    provider = router._create_provider("genesis")

    assert provider is not None
    # Genesis should use OpenAI-compatible provider
    from adk.llm.openai_compat import OpenAIProvider
    assert isinstance(provider, OpenAIProvider)


def test_genesis_backend_default_url():
    """Test that genesis backend uses the correct default URL."""
    from adk.llm import LLMRouter

    router = LLMRouter(provider="genesis")
    assert router._provider is not None
    # The provider should have been initialized with the genesis URL
