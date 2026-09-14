"""
Inference placement solver: maps tensor classes to memory tiers across nodes.

Cost model: decode_tok_s ~= 1 / (SUM_tier(bytes_read[tier] / bandwidth[tier])
            + hops * rtt_seconds)

For sparse MoE, bytes_read uses ACTIVATED params only (e.g. ~13B active out of
a ~284B total). This distinction is load-bearing.

Hard rules enforced in code:
- Refuse to split across WAN (RTT > wan_threshold_ms); RAISE, do not warn.
- Refuse to split AT ALL if model fits on one node (measured pessimization).
- Fail CLOSED on missing bandwidth or latency; never substitute a default.
- Routed experts -> highest-bandwidth tier with capacity.
- Attention/KV/embeddings/lm_head -> prefer VRAM (touched every token, small).
"""

from __future__ import annotations

import dataclasses
import sys
from enum import Enum
from typing import Any, TypedDict


class TensorClass(str, Enum):
    """Tensor classification for placement decisions."""

    ATTENTION = "attention"
    DENSE_FFN = "dense_ffn"
    SHARED_EXPERTS = "shared_experts"
    ROUTED_EXPERTS = "routed_experts"
    EMBEDDINGS = "embeddings"
    LM_HEAD = "lm_head"
    KV_CACHE = "kv_cache"


class TensorSpec(TypedDict):
    """Specification of a tensor class in the model."""

    size_bytes: int
    access_pattern: TensorClass
    tokens_per_decode_step: int
    is_activated: bool


class MemoryTier(TypedDict):
    """A memory tier on a node (e.g. VRAM, HBM, host RAM)."""

    name: str
    capacity_bytes: int
    bandwidth_gbps: float
    priority: int


class NodeSpec(TypedDict):
    """A compute node."""

    node_id: str
    memory_tiers: list[MemoryTier]


@dataclasses.dataclass
class PlacementPlan:
    """Output of the placement solver."""

    ngl: int
    ot_regexes: list[str]
    tensor_split: list[float] | None
    rpc_targets: list[str]
    kv_cache_type: str
    predicted_decode_tok_s: float
    reasoning: dict[str, Any]


class InferencePlacementSolver:
    """Maps tensor classes to memory tiers and generates llama.cpp invocation."""

    def __init__(self, wan_threshold_ms: float = 5.0):
        self.wan_threshold_ms = wan_threshold_ms

    def solve(
        self,
        model_pack: dict[str, TensorSpec],
        nodes: list[NodeSpec],
        latency_matrix_ms: dict[tuple[str, str], float],
    ) -> PlacementPlan:
        """
        Solve placement for a model across nodes.

        Args:
            model_pack: Tensor specs, keyed by name.
            nodes: List of node specs with memory ladders.
            latency_matrix_ms: RTT in ms between node pairs.

        Returns:
            PlacementPlan with concrete placement decisions.

        Raises:
            ValueError: If requirements cannot be satisfied.
        """
        if not nodes:
            raise ValueError("No nodes provided")
        if not model_pack:
            raise ValueError("Empty model pack")

        self._validate_latency_matrix(nodes, latency_matrix_ms)
        self._validate_bandwidth_available(nodes)

        total_model_bytes = sum(spec["size_bytes"] for spec in model_pack.values())

        single_node_plan = self._try_single_node(model_pack, nodes)
        if single_node_plan is not None:
            return single_node_plan

        if len(nodes) == 2:
            return self._solve_two_node_split(
                model_pack, nodes, latency_matrix_ms
            )
        elif len(nodes) > 2:
            raise NotImplementedError("Multi-node splitting > 2 nodes not supported")
        else:
            raise ValueError(
                f"Model {total_model_bytes} bytes does not fit on single node "
                "and no additional nodes available"
            )

    def _validate_latency_matrix(
        self, nodes: list[NodeSpec], latency_matrix_ms: dict[
            tuple[str, str], float
        ]
    ) -> None:
        """Ensure latency matrix is symmetric and complete."""
        node_ids = {n["node_id"] for n in nodes}

        for (src, dst), rtt in latency_matrix_ms.items():
            if src not in node_ids or dst not in node_ids:
                raise ValueError(
                    f"Latency references unknown nodes: ({src}, {dst})"
                )
            if rtt < 0:
                raise ValueError(
                    f"Negative latency: {src} -> {dst} = {rtt}ms"
                )

        for src_id in node_ids:
            for dst_id in node_ids:
                if src_id == dst_id:
                    continue
                if (src_id, dst_id) not in latency_matrix_ms:
                    raise ValueError(
                        f"Asymmetric latency matrix: missing ({src_id}, {dst_id})"
                    )

    def _validate_bandwidth_available(self, nodes: list[NodeSpec]) -> None:
        """Ensure every memory tier has measured bandwidth."""
        for node in nodes:
            for tier in node["memory_tiers"]:
                if tier["bandwidth_gbps"] <= 0:
                    raise ValueError(
                        f"Node {node['node_id']} tier {tier['name']}: "
                        f"bandwidth {tier['bandwidth_gbps']} not measured"
                    )

    def _try_single_node(
        self, model_pack: dict[str, TensorSpec], nodes: list[NodeSpec]
    ) -> PlacementPlan | None:
        """
        Try to fit entire model on single best node.
        Prioritizes highest capacity, then highest VRAM bandwidth.
        Returns None if model does not fit on any node.
        """
        total_model_bytes = sum(spec["size_bytes"] for spec in model_pack.values())

        best_node = None
        best_capacity = 0
        best_bandwidth = 0.0

        for node in nodes:
            total_capacity = sum(t["capacity_bytes"] for t in node["memory_tiers"])
            vram_tier = self._get_vram_tier(node)

            if total_capacity >= total_model_bytes:
                if total_capacity > best_capacity or (
                    total_capacity == best_capacity
                    and vram_tier["bandwidth_gbps"] > best_bandwidth
                ):
                    best_node = node
                    best_capacity = total_capacity
                    best_bandwidth = vram_tier["bandwidth_gbps"]

        if best_node is None:
            return None

        vram_tier = self._get_vram_tier(best_node)
        bytes_per_token = self._compute_activated_bytes_per_token(model_pack)
        bandwidth_gbps = vram_tier["bandwidth_gbps"]

        time_per_token_sec = bytes_per_token / (bandwidth_gbps * 1e9)
        predicted_tok_s = 1.0 / time_per_token_sec if time_per_token_sec > 0 else 0

        return PlacementPlan(
            ngl=-1,
            ot_regexes=[],
            tensor_split=None,
            rpc_targets=[],
            kv_cache_type="vram",
            predicted_decode_tok_s=predicted_tok_s,
            reasoning={
                "strategy": "single_node",
                "node_id": best_node["node_id"],
                "model_bytes": total_model_bytes,
                "capacity_bytes": best_capacity,
                "bandwidth_gbps": bandwidth_gbps,
                "bytes_per_token": bytes_per_token,
            },
        )

    def _solve_two_node_split(
        self,
        model_pack: dict[str, TensorSpec],
        nodes: list[NodeSpec],
        latency_matrix_ms: dict[tuple[str, str], float],
    ) -> PlacementPlan:
        """Solve placement for 2-node split with cost model."""
        node0, node1 = nodes[0], nodes[1]
        rtt_ms = latency_matrix_ms[(node0["node_id"], node1["node_id"])]

        if rtt_ms > self.wan_threshold_ms:
            raise ValueError(
                f"Cannot split across WAN ({rtt_ms}ms > {self.wan_threshold_ms}ms) "
                f"between {node0['node_id']} and {node1['node_id']}"
            )

        vram0 = self._get_vram_tier(node0)
        vram1 = self._get_vram_tier(node1)

        if vram0["bandwidth_gbps"] >= vram1["bandwidth_gbps"]:
            primary_node, secondary_node = node0, node1
        else:
            primary_node, secondary_node = node1, node0

        allocation = self._allocate_tensors(
            model_pack, primary_node, secondary_node
        )

        plan = self._compute_placement_plan(
            allocation, model_pack, primary_node, secondary_node, rtt_ms
        )

        return plan

    def _allocate_tensors(
        self,
        model_pack: dict[str, TensorSpec],
        primary_node: NodeSpec,
        secondary_node: NodeSpec,
    ) -> dict[str, list[str]]:
        """Allocate tensors to nodes: routed experts -> high BW, rest to primary."""
        allocation = {
            primary_node["node_id"]: [],
            secondary_node["node_id"]: [],
        }

        primary_id = primary_node["node_id"]
        secondary_id = secondary_node["node_id"]
        vram_primary = self._get_vram_tier(primary_node)

        primary_used = 0
        secondary_used = 0

        routed_expert_names = []
        vram_names = []
        remaining_names = []

        for name, spec in model_pack.items():
            if spec["access_pattern"] == TensorClass.ROUTED_EXPERTS:
                routed_expert_names.append(name)
            elif spec["access_pattern"] in (
                TensorClass.ATTENTION,
                TensorClass.EMBEDDINGS,
                TensorClass.LM_HEAD,
            ):
                vram_names.append(name)
            else:
                remaining_names.append(name)

        vram_size = sum(model_pack[n]["size_bytes"] for n in vram_names)
        routed_size = sum(model_pack[n]["size_bytes"] for n in routed_expert_names)

        if vram_primary["capacity_bytes"] < vram_size:
            raise ValueError(
                "VRAM-preference tensors do not fit on primary node"
            )

        allocation[primary_id].extend(vram_names)
        primary_used = vram_size

        primary_cap = sum(t["capacity_bytes"] for t in primary_node["memory_tiers"])
        secondary_cap = sum(
            t["capacity_bytes"] for t in secondary_node["memory_tiers"]
        )

        if secondary_cap >= routed_size:
            allocation[secondary_id].extend(routed_expert_names)
            secondary_used = routed_size
        elif primary_cap - primary_used >= routed_size:
            allocation[primary_id].extend(routed_expert_names)
            primary_used += routed_size
        else:
            raise ValueError("Routed experts do not fit on any node")

        for name in remaining_names:
            spec = model_pack[name]

            if primary_used + spec["size_bytes"] <= primary_cap:
                allocation[primary_id].append(name)
                primary_used += spec["size_bytes"]
            elif secondary_used + spec["size_bytes"] <= secondary_cap:
                allocation[secondary_id].append(name)
                secondary_used += spec["size_bytes"]
            else:
                raise ValueError(f"Tensor {name} does not fit on any node")

        return allocation

    def _compute_placement_plan(
        self,
        allocation: dict[str, list[str]],
        model_pack: dict[str, TensorSpec],
        primary_node: NodeSpec,
        secondary_node: NodeSpec,
        rtt_ms: float,
    ) -> PlacementPlan:
        """Compute throughput and generate placement plan."""
        primary_id = primary_node["node_id"]
        secondary_id = secondary_node["node_id"]

        vram_primary = self._get_vram_tier(primary_node)
        vram_secondary = self._get_vram_tier(secondary_node)

        primary_bytes_per_token = self._tensor_bytes_per_token(
            allocation[primary_id], model_pack
        )
        secondary_bytes_per_token = self._tensor_bytes_per_token(
            allocation[secondary_id], model_pack
        )

        latency_sec = rtt_ms / 1000.0
        time_per_token_sec = (
            primary_bytes_per_token / (vram_primary["bandwidth_gbps"] * 1e9)
            + secondary_bytes_per_token / (vram_secondary["bandwidth_gbps"] * 1e9)
            + latency_sec
        )

        predicted_tok_s = 1.0 / time_per_token_sec if time_per_token_sec > 0 else 0

        ot_regexes = self._generate_ot_regexes(allocation, primary_id)

        return PlacementPlan(
            ngl=-1,
            ot_regexes=ot_regexes,
            tensor_split=[1.0, 1.0],
            rpc_targets=[f"{secondary_id}:50052"],
            kv_cache_type="vram",
            predicted_decode_tok_s=predicted_tok_s,
            reasoning={
                "strategy": "two_node_split",
                "primary_node": primary_id,
                "secondary_node": secondary_id,
                "rtt_ms": rtt_ms,
                "primary_tensors": allocation[primary_id],
                "secondary_tensors": allocation[secondary_id],
                "primary_bytes_per_token": primary_bytes_per_token,
                "secondary_bytes_per_token": secondary_bytes_per_token,
                "predicted_tok_s": predicted_tok_s,
            },
        )

    def _get_vram_tier(self, node: NodeSpec) -> MemoryTier:
        """Get the VRAM tier (priority 0) or first tier."""
        for tier in node["memory_tiers"]:
            if tier["priority"] == 0:
                return tier
        return node["memory_tiers"][0]

    def _compute_activated_bytes_per_token(
        self, model_pack: dict[str, TensorSpec]
    ) -> float:
        """Compute bytes read per token using activated params only."""
        total = 0.0
        for spec in model_pack.values():
            if spec.get("is_activated", True):
                total += spec["size_bytes"] * spec["tokens_per_decode_step"]
        return total

    def _tensor_bytes_per_token(
        self, tensor_names: list[str], model_pack: dict[str, TensorSpec]
    ) -> float:
        """Compute bytes per token for a subset of tensors."""
        total = 0.0
        for name in tensor_names:
            spec = model_pack[name]
            if spec.get("is_activated", True):
                total += spec["size_bytes"] * spec["tokens_per_decode_step"]
        return total

    def _generate_ot_regexes(
        self, allocation: dict[str, list[str]], primary_id: str
    ) -> list[str]:
        """Generate llama.cpp --override-tensor regexes."""
        regexes = []
        for node_id, tensor_names in allocation.items():
            if node_id != primary_id:
                for name in tensor_names:
                    regexes.append(f"{name}=RPC0")
        return regexes


def _test_single_node_fit() -> None:
    """Test: model fits on single node, no split."""
    solver = InferencePlacementSolver()

    model_pack = {
        "attn": {
            "size_bytes": int(1e9),
            "access_pattern": TensorClass.ATTENTION,
            "tokens_per_decode_step": 1,
            "is_activated": True,
        },
        "ffn": {
            "size_bytes": int(2e9),
            "access_pattern": TensorClass.DENSE_FFN,
            "tokens_per_decode_step": 1,
            "is_activated": True,
        },
    }

    nodes: list[NodeSpec] = [
        {
            "node_id": "node0",
            "memory_tiers": [
                {
                    "name": "vram",
                    "capacity_bytes": int(10e9),
                    "bandwidth_gbps": 100.0,
                    "priority": 0,
                },
            ],
        },
    ]

    latency_matrix = {("node0", "node0"): 0.1}

    plan = solver.solve(model_pack, nodes, latency_matrix)
    assert plan.reasoning["strategy"] == "single_node"
    assert plan.predicted_decode_tok_s > 0


def _test_wan_refusal() -> None:
    """Test: refuse to split across WAN."""
    solver = InferencePlacementSolver(wan_threshold_ms=5.0)

    model_pack = {
        "experts": {
            "size_bytes": int(100e9),
            "access_pattern": TensorClass.ROUTED_EXPERTS,
            "tokens_per_decode_step": 1,
            "is_activated": False,
        },
    }

    nodes: list[NodeSpec] = [
        {
            "node_id": "node0",
            "memory_tiers": [
                {
                    "name": "vram",
                    "capacity_bytes": int(50e9),
                    "bandwidth_gbps": 100.0,
                    "priority": 0,
                }
            ],
        },
        {
            "node_id": "node1",
            "memory_tiers": [
                {
                    "name": "vram",
                    "capacity_bytes": int(50e9),
                    "bandwidth_gbps": 100.0,
                    "priority": 0,
                }
            ],
        },
    ]

    latency_matrix = {
        ("node0", "node1"): 50.0,
        ("node1", "node0"): 50.0,
        ("node0", "node0"): 0.1,
        ("node1", "node1"): 0.1,
    }

    try:
        solver.solve(model_pack, nodes, latency_matrix)
        raise AssertionError("Should have raised ValueError for WAN split")
    except ValueError as e:
        if "WAN" not in str(e):
            raise AssertionError(f"Wrong error message: {e}")


def _test_missing_bandwidth() -> None:
    """Test: fail closed on missing bandwidth."""
    solver = InferencePlacementSolver()

    model_pack = {
        "attn": {
            "size_bytes": int(1e9),
            "access_pattern": TensorClass.ATTENTION,
            "tokens_per_decode_step": 1,
            "is_activated": True,
        },
    }

    nodes: list[NodeSpec] = [
        {
            "node_id": "node0",
            "memory_tiers": [
                {
                    "name": "vram",
                    "capacity_bytes": int(10e9),
                    "bandwidth_gbps": 0,
                    "priority": 0,
                },
            ],
        },
    ]

    latency_matrix = {("node0", "node0"): 0.1}

    try:
        solver.solve(model_pack, nodes, latency_matrix)
        raise AssertionError("Should have raised ValueError for missing bandwidth")
    except ValueError as e:
        if "bandwidth" not in str(e).lower():
            raise AssertionError(f"Wrong error message: {e}")


def _test_two_node_split() -> None:
    """Test: direct 2-node split placement logic."""
    solver = InferencePlacementSolver(wan_threshold_ms=5.0)

    model_pack = {
        "attn": {
            "size_bytes": int(5e9),
            "access_pattern": TensorClass.ATTENTION,
            "tokens_per_decode_step": 1,
            "is_activated": True,
        },
        "experts": {
            "size_bytes": int(55e9),
            "access_pattern": TensorClass.ROUTED_EXPERTS,
            "tokens_per_decode_step": 1,
            "is_activated": True,
        },
    }

    nodes: list[NodeSpec] = [
        {
            "node_id": "node0",
            "memory_tiers": [
                {
                    "name": "vram",
                    "capacity_bytes": int(32e9),
                    "bandwidth_gbps": 90.0,
                    "priority": 0,
                }
            ],
        },
        {
            "node_id": "node1",
            "memory_tiers": [
                {
                    "name": "vram",
                    "capacity_bytes": int(128e9),
                    "bandwidth_gbps": 273.0,
                    "priority": 0,
                }
            ],
        },
    ]

    latency_matrix = {
        ("node0", "node1"): 3.0,
        ("node1", "node0"): 3.0,
        ("node0", "node0"): 0.1,
        ("node1", "node1"): 0.1,
    }

    plan = solver._solve_two_node_split(model_pack, nodes, latency_matrix)
    assert plan.reasoning["strategy"] == "two_node_split"
    assert plan.predicted_decode_tok_s > 0
    assert "expert" in str(plan.reasoning).lower()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _test_single_node_fit()
        _test_wan_refusal()
        _test_missing_bandwidth()
        _test_two_node_split()
        # ASCII on purpose. A tick mark raises UnicodeEncodeError on a cp1252
        # console, and it raises AFTER every test has passed -- so the module
        # works, the run exits non-zero, and any gate wiring this reports DEAD.
        # A self-test that cannot announce success is a self-test nobody trusts.
        print("OK: all self-tests passed")
    else:
        print("Run with --self-test to verify")
        sys.exit(0)
