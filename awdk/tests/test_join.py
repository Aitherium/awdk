"""Tests for adk join command.

Coverage:
  - Dry-run prints full ordered plan without side effects
  - Mid-chain failure aborts with clear message, no partial-success claim
  - Secret values never appear in output
  - All surfaces mocked
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from adk.commands.join import join_mesh, cmd_join, _github_device_flow_login


@pytest.mark.asyncio
async def test_dry_run_prints_plan(capsys):
    """Dry-run walks the plan without side effects."""
    result = await join_mesh(dry_run=True)
    captured = capsys.readouterr()

    assert result == 0
    assert "[DRY RUN] Planned steps:" in captured.out
    assert "1. GitHub device flow auth" in captured.out
    assert "2. Detect hardware (CPU, RAM, GPU)" in captured.out
    assert "3. Resolve inference recipe" in captured.out
    assert "4. Apply recipe (serve)" in captured.out
    assert "5. Verify deployment" in captured.out
    assert "6. Register node with platform" in captured.out
    assert "7. Obtain mesh overlay key" in captured.out
    assert "8. Join mesh overlay" in captured.out
    assert "9. Register backend for routing" in captured.out
    assert "10. Print success summary + earnings" in captured.out


@pytest.mark.asyncio
async def test_github_device_flow_failure_aborts(capsys):
    """GitHub auth failure aborts with clear message."""
    with patch(
        "adk.commands.join._github_device_flow_login",
        side_effect=RuntimeError("GitHub device start failed: HTTP 502")
    ):
        result = await join_mesh(dry_run=False)
        captured = capsys.readouterr()

        assert result == 1
        assert "[x]" in captured.out
        assert "GitHub auth returned no token or tenant_id" in captured.out or \
               "Onboarding failed" in captured.out
        # Verify no partial success claim
        assert "✓" not in captured.out


@pytest.mark.asyncio
async def test_hardware_detection_failure_aborts(capsys):
    """Hardware detection failure aborts with clear message."""
    with patch(
        "adk.commands.join._github_device_flow_login",
        new_callable=AsyncMock,
        return_value={
            "access_token": "token_abc123",
            "tenant_id": "tenant_xyz",
            "username": "testuser",
            "user_id": "user123",
        }
    ):
        with patch(
            "adk.toolpacks.node_bootstrap.tools.node_detect_hardware",
            return_value={"error": "No GPU detected"}
        ):
            result = await join_mesh(dry_run=False)
            captured = capsys.readouterr()

            assert result == 1
            assert "[x]" in captured.out
            assert "Hardware detection failed" in captured.out
            # Verify no partial success claim
            assert "✓ Community node onboarded!" not in captured.out


@pytest.mark.asyncio
async def test_recipe_resolution_failure_aborts(capsys):
    """Recipe resolution failure aborts with clear message."""
    with patch(
        "adk.commands.join._github_device_flow_login",
        new_callable=AsyncMock,
        return_value={
            "access_token": "token_abc123",
            "tenant_id": "tenant_xyz",
            "username": "testuser",
            "user_id": "user123",
        }
    ):
        with patch(
            "adk.toolpacks.node_bootstrap.tools.node_detect_hardware",
            return_value={
                "system_info": {"gpu_vram_mb": 8000},
                "recommendation": "cuda-vllm",
            }
        ):
            with patch(
                "adk.toolpacks.node_bootstrap.tools.node_resolve_recipe",
                return_value={"error": "Unsupported GPU"}
            ):
                result = await join_mesh(dry_run=False)
                captured = capsys.readouterr()

                assert result == 1
                assert "[x]" in captured.out
                assert "Recipe resolution failed" in captured.out
                # Verify no partial success claim
                assert "✓ Community node onboarded!" not in captured.out


@pytest.mark.asyncio
async def test_mesh_key_unavailable_fails_gracefully(capsys):
    """Mesh key issuance endpoint unavailable fails with clear message."""
    with patch(
        "adk.commands.join._github_device_flow_login",
        new_callable=AsyncMock,
        return_value={
            "access_token": "token_abc123",
            "tenant_id": "tenant_xyz",
            "username": "testuser",
            "user_id": "user123",
        }
    ):
        with patch(
            "adk.toolpacks.node_bootstrap.tools.node_detect_hardware",
            return_value={"system_info": {"gpu_vram_mb": 8000}}
        ):
            with patch(
                "adk.toolpacks.node_bootstrap.tools.node_resolve_recipe",
                return_value={"recipe": "cuda-vllm-8gb", "models": ["model1"]}
            ):
                with patch(
                    "adk.toolpacks.node_bootstrap.tools.node_apply",
                    return_value={"status": "applied"}
                ):
                    with patch(
                        "adk.toolpacks.node_bootstrap.tools.node_verify",
                        return_value={"status": "verified"}
                    ):
                        with patch(
                            "adk.enrollment.rich_enroll",
                            new_callable=AsyncMock,
                            return_value={"node_id": "node-abc123"}
                        ):
                            with patch(
                                "adk.commands.join._obtain_mesh_key",
                                side_effect=RuntimeError(
                                    "Mesh key issuance unavailable "
                                    "(endpoint not yet deployed)"
                                )
                            ):
                                result = await join_mesh(dry_run=False)
                                captured = capsys.readouterr()

                                assert result == 1
                                assert "[x]" in captured.out
                                assert "Mesh key issuance failed" in captured.out
                                assert "unavailable" in captured.out


@pytest.mark.asyncio
async def test_secret_values_never_logged(capsys):
    """Secret values (tokens, keys) never appear in output."""
    secret_token = "sk_test_super_secret_token_abc123xyz789"

    with patch(
        "adk.commands.join._github_device_flow_login",
        new_callable=AsyncMock,
        return_value={
            "access_token": secret_token,
            "tenant_id": "tenant_xyz",
            "username": "testuser",
            "user_id": "user123",
        }
    ):
        result = await join_mesh(dry_run=False)
        captured = capsys.readouterr()

        # Verify secret token does not appear anywhere
        assert secret_token not in captured.out
        assert secret_token not in captured.err


def test_cmd_join_handler_dispatches_async():
    """cmd_join properly dispatches to async join_mesh."""
    args = MagicMock()
    args.no_github = False
    args.cloud_provider = None
    args.model = None
    args.no_browser = False
    args.dry_run = True

    result = cmd_join(args)
    assert result == 0


def test_cmd_join_keyboard_interrupt():
    """cmd_join handles KeyboardInterrupt gracefully."""
    args = MagicMock()
    args.no_github = False
    args.cloud_provider = None
    args.model = None
    args.no_browser = False
    args.dry_run = False

    with patch(
        "adk.commands.join.asyncio.run",
        side_effect=KeyboardInterrupt()
    ):
        result = cmd_join(args)
        assert result == 130  # Standard interrupt exit code


def test_success_summary_printed(capsys):
    """Success summary prints with correct details."""
    from adk.commands.join import _print_success_summary

    _print_success_summary(
        node_id="node-abc123",
        mesh_ip="10.77.1.42",
        models=["qwen-8b", "mistral-7b"],
        tenant_id="tenant_xyz"
    )
    captured = capsys.readouterr()

    assert "✓ Community node onboarded!" in captured.out
    assert "Node ID:         node-abc123" in captured.out
    assert "Mesh IP:         10.77.1.42" in captured.out
    assert "Models:          qwen-8b, mistral-7b" in captured.out
    assert "Earning to:      tenant_xyz" in captured.out


@pytest.mark.asyncio
async def test_github_device_flow_timeout():
    """GitHub device flow timeout raises clear error."""
    import httpx

    with patch("httpx.AsyncClient") as mock_client:
        mock_inst = AsyncMock()
        mock_inst.post = AsyncMock(
            side_effect=httpx.RequestError("Connection timeout")
        )
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_inst)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        with pytest.raises(RuntimeError, match="unreachable"):
            await _github_device_flow_login("https://localhost:8115")


@pytest.mark.asyncio
async def test_github_device_flow_completes():
    """GitHub device flow successfully completes."""
    mock_response_start = MagicMock()
    mock_response_start.status_code = 200
    mock_response_start.json.return_value = {
        "handle": "handle_abc",
        "user_code": "USER-CODE-123",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": 5,
    }

    mock_response_poll = MagicMock()
    mock_response_poll.status_code = 200
    mock_response_poll.json.return_value = {
        "status": "complete",
        "access_token": "ghu_test_token",
        "token_type": "bearer",
        "username": "testuser",
        "user_id": "user123",
        "tenant_id": "tenant_xyz",
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_inst = AsyncMock()
        mock_inst.post = AsyncMock(
            side_effect=[mock_response_start, mock_response_poll]
        )
        mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_inst)
        mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await _github_device_flow_login("https://localhost:8115")

        assert result["access_token"] == "ghu_test_token"
        assert result["username"] == "testuser"
        assert result["tenant_id"] == "tenant_xyz"
