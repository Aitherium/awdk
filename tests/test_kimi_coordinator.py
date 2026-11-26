"""
Tests for kimi_coordinator module.

Tests pure planning functions: quant selection, split planning,
bind validation, command rendering.
"""

from __future__ import annotations

import pytest
from adk.kimi_coordinator import (
    NodeBudget,
    assert_rpc_bind,
    plan_kimi_split,
    render_deploy_commands,
    select_quant,
    validate_rpc_bind,
)


class TestSelectQuant:
    """Test quantization selection by pool size."""

    def test_below_minimum_610_gb(self):
        """Pool < 610 GB returns None."""
        assert select_quant(609.9) is None

    def test_at_minimum_610_gb(self):
        """Pool == 610 GB selects UD-IQ1_S."""
        result = select_quant(610.0)
        assert result == "UD-IQ1_S"

    def test_mid_range_800_gb(self):
        """Pool in mid-range selects appropriate quant."""
        result = select_quant(800.0)
        assert result == "UD-IQ2_XXS"

    def test_at_max_1600_gb(self):
        """Pool >= 1600 GB selects UD-Q8_K_XL."""
        result = select_quant(1600.0)
        assert result == "UD-Q8_K_XL"

    def test_above_max(self):
        """Pool >> 1600 GB still selects UD-Q8_K_XL."""
        result = select_quant(2000.0)
        assert result == "UD-Q8_K_XL"


class TestValidateRpcBind:
    """Test bind address validation."""

    def test_rfc1918_10_x_is_valid(self):
        """10.x addresses are valid."""
        assert validate_rpc_bind("10.77.0.1") is True
        assert validate_rpc_bind("10.0.0.0") is True
        assert validate_rpc_bind("10.255.255.255") is True

    def test_rfc1918_172_16_31_is_valid(self):
        """172.16-31.x addresses are valid."""
        assert validate_rpc_bind("172.16.0.0") is True
        assert validate_rpc_bind("172.31.255.255") is True
        assert validate_rpc_bind("172.20.1.1") is True

    def test_rfc1918_192_168_is_valid(self):
        """192.168.x addresses are valid."""
        assert validate_rpc_bind("192.168.0.1") is True
        assert validate_rpc_bind("192.168.1.112") is True

    def test_cgnat_100_64_to_127_is_valid(self):
        """CGNAT 100.64-127.x addresses are valid."""
        assert validate_rpc_bind("100.64.0.0") is True
        assert validate_rpc_bind("100.127.255.255") is True
        assert validate_rpc_bind("100.96.0.1") is True

    def test_loopback_127_is_valid(self):
        """Loopback 127.0.0.1 is valid."""
        assert validate_rpc_bind("127.0.0.1") is True

    def test_ipv6_loopback_is_valid(self):
        """IPv6 loopback ::1 is valid."""
        assert validate_rpc_bind("::1") is True

    def test_zero_address_is_invalid(self):
        """0.0.0.0 is rejected."""
        assert validate_rpc_bind("0.0.0.0") is False

    def test_ipv6_unspecified_is_invalid(self):
        """:: and [::] are rejected."""
        assert validate_rpc_bind("::") is False
        assert validate_rpc_bind("[::]") is False

    def test_public_8_8_8_8_is_invalid(self):
        """Public IP 8.8.8.8 is rejected."""
        assert validate_rpc_bind("8.8.8.8") is False

    def test_public_1_1_1_1_is_invalid(self):
        """Public IP 1.1.1.1 is rejected."""
        assert validate_rpc_bind("1.1.1.1") is False

    def test_empty_string_is_invalid(self):
        """Empty string is rejected."""
        assert validate_rpc_bind("") is False

    def test_garbage_is_invalid(self):
        """Malformed IP is rejected."""
        assert validate_rpc_bind("not-an-ip") is False


class TestAssertRpcBind:
    """Test bind address assertion."""

    def test_private_raises_nothing(self):
        """Private address does not raise."""
        assert_rpc_bind("192.168.1.100")

    def test_public_ip_raises(self):
        """Public IP raises ValueError."""
        with pytest.raises(ValueError, match="not a private/overlay address"):
            assert_rpc_bind("8.8.8.8")

    def test_zero_raises(self):
        """0.0.0.0 raises ValueError."""
        with pytest.raises(ValueError, match="not a private/overlay address"):
            assert_rpc_bind("0.0.0.0")


class TestPlanKimiSplit:
    """Test Kimi-K3 split planning."""

    def test_empty_nodes_raises(self):
        """Empty nodes list raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            plan_kimi_split([])

    def test_single_node_pool_too_small(self):
        """Single node < 610 GB raises ValueError."""
        nodes = [NodeBudget("node0", "192.168.1.1", 300, 200)]
        with pytest.raises(ValueError, match="shortfall"):
            plan_kimi_split(nodes, quant="auto")

    def test_single_node_610_gb_exact(self):
        """Single node at 610 GB minimum succeeds."""
        nodes = [NodeBudget("node0", "192.168.1.1", 300, 310)]
        plan = plan_kimi_split(nodes, quant="auto")
        assert plan["quant"] == "UD-IQ1_S"
        assert plan["coordinator"].node_id == "node0"
        assert len(plan["rpc_backends"]) == 0
        assert plan["pool_total_gb"] == 610.0

    def test_coordinator_is_largest_node(self):
        """Coordinator is the largest total_gb node."""
        nodes = [
            NodeBudget("small", "192.168.1.1", 10, 10),
            NodeBudget("large", "192.168.1.2", 400, 400),
            NodeBudget("medium", "192.168.1.3", 100, 100),
        ]
        plan = plan_kimi_split(nodes, quant="UD-IQ1_S")
        assert plan["coordinator"].node_id == "large"

    def test_backends_include_32gb_threshold(self):
        """Backends with >= 32 GB are included; < 32 GB are skipped."""
        nodes = [
            NodeBudget("big", "192.168.1.1", 400, 400),
            NodeBudget("ok", "192.168.1.2", 20, 20),
            NodeBudget("tiny", "192.168.1.3", 10, 10),
            NodeBudget("small", "192.168.1.4", 16, 16),
        ]
        plan = plan_kimi_split(nodes, quant="UD-IQ1_S")
        assert plan["coordinator"].node_id == "big"
        assert len(plan["rpc_backends"]) == 1
        assert plan["rpc_backends"][0].node_id == "ok"
        assert set(plan["skipped_nodes"]) == {"tiny", "small"}

    def test_tensor_split_sums_to_one(self):
        """tensor_split weights sum to ~1.0."""
        nodes = [
            NodeBudget("n1", "192.168.1.1", 200, 200),
            NodeBudget("n2", "192.168.1.2", 100, 100),
            NodeBudget("n3", "192.168.1.3", 100, 100),
        ]
        plan = plan_kimi_split(nodes, quant="UD-IQ1_S")
        weight_sum = sum(plan["tensor_split"])
        assert abs(weight_sum - 1.0) < 0.01

    def test_tensor_split_coordinator_first(self):
        """tensor_split lists coordinator (largest) first."""
        nodes = [
            NodeBudget("small", "192.168.1.1", 50, 50),
            NodeBudget("big", "192.168.1.2", 400, 400),
        ]
        plan = plan_kimi_split(nodes, quant="UD-IQ1_S")
        assert plan["coordinator"].node_id == "big"
        # Coordinator's weight should be highest
        assert plan["tensor_split"][0] > plan["tensor_split"][1]

    def test_invalid_quant_raises(self):
        """Explicitly invalid quant name raises ValueError."""
        nodes = [NodeBudget("n", "192.168.1.1", 400, 400)]
        with pytest.raises(ValueError, match="Unknown quant"):
            plan_kimi_split(nodes, quant="INVALID_QUANT")

    def test_explicit_quant_ud_q8_k_xl(self):
        """Explicit UD-Q8_K_XL quant with sufficient pool."""
        nodes = [NodeBudget("n", "192.168.1.1", 800, 800)]
        plan = plan_kimi_split(nodes, quant="UD-Q8_K_XL")
        assert plan["quant"] == "UD-Q8_K_XL"
        assert plan["required_gb"] == 1600


class TestRenderDeployCommands:
    """Test command rendering."""

    def test_render_single_backend(self):
        """Render commands for 1 coordinator + 1 backend."""
        plan = {
            "quant": "UD-IQ1_S",
            "coordinator": NodeBudget("coord", "192.168.1.1", 300, 310),
            "rpc_backends": [
                NodeBudget("back1", "192.168.1.2", 300, 300),
            ],
            "tensor_split": [0.5, 0.5],
            "pool_total_gb": 610.0,
            "required_gb": 610,
            "skipped_nodes": [],
            "est_tok_s": "unproven",
        }
        result = render_deploy_commands(
            plan,
            model_dir="/work/kimi",
            bin_dir="/work/build-rpc/bin",
            mmproj_path="/work/kimi/mmproj-BF16.gguf",
            port=50052,
        )
        assert len(result["rpc_commands"]) == 1
        assert result["rpc_commands"][0]["host"] == "192.168.1.2"
        assert result["rpc_commands"][0]["port"] == 50052
        assert "--mmproj" in result["coordinator_command"]
        assert "--temp 1.0" in result["coordinator_command"]
        assert "--top-p 0.95" in result["coordinator_command"]
        assert "--rpc 192.168.1.2:50052" in result["coordinator_command"]
        assert "--tensor-split 0.5,0.5" in result["coordinator_command"]

    def test_render_multiple_backends(self):
        """Render commands for multiple backends with incremented ports."""
        plan = {
            "quant": "UD-Q2_K_XL",
            "coordinator": NodeBudget("c", "192.168.1.1", 300, 300),
            "rpc_backends": [
                NodeBudget("b1", "192.168.1.2", 200, 200),
                NodeBudget("b2", "192.168.1.3", 200, 200),
                NodeBudget("b3", "192.168.1.4", 200, 200),
            ],
            "tensor_split": [0.25, 0.25, 0.25, 0.25],
            "pool_total_gb": 1200.0,
            "required_gb": 880,
            "skipped_nodes": [],
            "est_tok_s": "unproven",
        }
        result = render_deploy_commands(
            plan,
            model_dir="/work/kimi",
            bin_dir="/work/build-rpc/bin",
            mmproj_path="/work/kimi/mmproj-BF16.gguf",
            port=50052,
        )
        assert len(result["rpc_commands"]) == 3
        assert result["rpc_commands"][0]["port"] == 50052
        assert result["rpc_commands"][1]["port"] == 50053
        assert result["rpc_commands"][2]["port"] == 50054
        rpc_list = ",".join(
            f"{cmd['host']}:{cmd['port']}" for cmd in result["rpc_commands"]
        )
        assert f"--rpc {rpc_list}" in result["coordinator_command"]

    def test_render_refuses_public_coordinator_bind(self):
        """render_deploy_commands refuses public coordinator IP."""
        plan = {
            "quant": "UD-IQ1_S",
            "coordinator": NodeBudget("c", "8.8.8.8", 400, 400),
            "rpc_backends": [],
            "tensor_split": [1.0],
            "pool_total_gb": 800.0,
            "required_gb": 610,
            "skipped_nodes": [],
            "est_tok_s": "unproven",
        }
        with pytest.raises(ValueError, match="not a private/overlay address"):
            render_deploy_commands(
                plan,
                model_dir="/work/kimi",
                bin_dir="/work/build-rpc/bin",
                mmproj_path="/work/kimi/mmproj-BF16.gguf",
            )

    def test_render_refuses_public_backend_bind(self):
        """render_deploy_commands refuses public backend IP."""
        plan = {
            "quant": "UD-IQ1_S",
            "coordinator": NodeBudget("c", "192.168.1.1", 300, 300),
            "rpc_backends": [
                NodeBudget("b", "1.1.1.1", 300, 300),
            ],
            "tensor_split": [0.5, 0.5],
            "pool_total_gb": 600.0,
            "required_gb": 610,
            "skipped_nodes": [],
            "est_tok_s": "unproven",
        }
        with pytest.raises(ValueError, match="not a private/overlay address"):
            render_deploy_commands(
                plan,
                model_dir="/work/kimi",
                bin_dir="/work/build-rpc/bin",
                mmproj_path="/work/kimi/mmproj-BF16.gguf",
            )

    def test_render_includes_steps_and_warnings(self):
        """Render result includes deployment steps and warnings."""
        plan = {
            "quant": "UD-IQ1_S",
            "coordinator": NodeBudget("c", "192.168.1.1", 400, 400),
            "rpc_backends": [],
            "tensor_split": [1.0],
            "pool_total_gb": 800.0,
            "required_gb": 610,
            "skipped_nodes": [],
            "est_tok_s": "unproven",
        }
        result = render_deploy_commands(
            plan,
            model_dir="/work/kimi",
            bin_dir="/work/build-rpc/bin",
            mmproj_path="/work/kimi/mmproj-BF16.gguf",
        )
        assert len(result["steps"]) >= 3
        assert len(result["warnings"]) >= 1
        assert any("unsloth fork" in w.lower() for w in result["warnings"])


class TestDeployKimiSplit:
    """Test deployment orchestrator wrapper."""

    @pytest.mark.asyncio
    async def test_deploy_dry_run_returns_steps(self):
        """deploy_kimi_split dry_run=True returns steps."""
        plan = {
            "quant": "UD-IQ1_S",
            "coordinator": NodeBudget("c", "192.168.1.1", 400, 400),
            "rpc_backends": [],
            "tensor_split": [1.0],
            "pool_total_gb": 800.0,
            "required_gb": 610,
            "skipped_nodes": [],
            "est_tok_s": "unproven",
        }
        result = await (
            pytest.importorskip("adk.kimi_coordinator").deploy_kimi_split(
                plan,
                model_dir="/work/kimi",
                bin_dir="/work/build-rpc/bin",
                mmproj_path="/work/kimi/mmproj-BF16.gguf",
                dry_run=True,
            )
        )
        assert result["dry_run"] is True
        assert "steps" in result
        assert "commands" in result
        assert result["plan"] == plan

    @pytest.mark.asyncio
    async def test_deploy_live_raises_not_implemented(self):
        """deploy_kimi_split dry_run=False raises NotImplementedError."""
        plan = {
            "quant": "UD-IQ1_S",
            "coordinator": NodeBudget("c", "192.168.1.1", 400, 400),
            "rpc_backends": [],
            "tensor_split": [1.0],
            "pool_total_gb": 800.0,
            "required_gb": 610,
            "skipped_nodes": [],
            "est_tok_s": "unproven",
        }
        kimi_mod = pytest.importorskip("adk.kimi_coordinator")
        with pytest.raises(NotImplementedError, match="S3"):
            await kimi_mod.deploy_kimi_split(
                plan,
                model_dir="/work/kimi",
                bin_dir="/work/build-rpc/bin",
                mmproj_path="/work/kimi/mmproj-BF16.gguf",
                dry_run=False,
            )
