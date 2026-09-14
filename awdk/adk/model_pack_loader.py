"""
Model Pack Loader
=================

Load and validate model-agnostic quantization packs from model_packs.json.

Replaces the Kimi-only KIMI_K3_QUANTS with a generalized pack system
supporting multiple architectures (Deepseek, Kimi, etc.) and their
quantization ladders.

Public API:
    ModelPack: dataclass for a loaded model pack
    load_pack(id: str) -> ModelPack: load by id, raise on unknown
    list_packs() -> list[str]: available pack ids
    select_quant(pack: ModelPack, pool_gb: float) -> Optional[str]:
        select largest quant that fits total pool
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class QuantSpec:
    """Quantization spec from the pack's ladder."""

    id: str
    size_gb: int
    min_total_memory_gb: int


@dataclass
class ModelPack:
    """Loaded model pack with all configuration."""

    id: str
    display_name: str
    hf_repo: str
    arch: str
    total_params_b: int
    activated_params_b: Optional[int]
    is_moe: bool
    quant_ladder: list[QuantSpec]
    tensor_class_regexes: dict[str, Optional[str]]
    kv_cache_policy: str
    chat_template: str
    license: str
    attribution_required: bool
    draft_model: Optional[str]

    def validate(self) -> None:
        """
        Validate pack invariants.

        Raises:
            ValueError: If the pack violates a required constraint
        """
        if self.is_moe and self.activated_params_b is None:
            raise ValueError(
                f"Pack '{self.id}' is MoE but activated_params_b is null"
            )
        if not self.is_moe and self.activated_params_b is not None:
            raise ValueError(
                f"Pack '{self.id}' is dense but activated_params_b is "
                f"set to {self.activated_params_b}"
            )
        if self.total_params_b <= 0:
            raise ValueError(
                f"Pack '{self.id}': total_params_b must be > 0, "
                f"got {self.total_params_b}"
            )
        if not self.quant_ladder:
            raise ValueError(
                f"Pack '{self.id}': quant_ladder cannot be empty"
            )


def _load_pack_table() -> dict:
    """
    Load the model_packs.json table.

    Returns:
        Dict with top-level 'packs' key containing all model definitions

    Raises:
        FileNotFoundError: If model_packs.json not found
        json.JSONDecodeError: If JSON is malformed
    """
    pack_file = Path(__file__).parent / "model_packs.json"
    if not pack_file.exists():
        raise FileNotFoundError(
            f"model_packs.json not found at {pack_file}"
        )

    with open(pack_file, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("packs", {})


def list_packs() -> list[str]:
    """
    List all available model pack identifiers.

    Returns:
        Sorted list of pack ids (e.g., ["deepseek-v4-flash", "kimi-k3"])
    """
    table = _load_pack_table()
    return sorted(table.keys())


def load_pack(pack_id: str) -> ModelPack:
    """
    Load a model pack by id.

    Args:
        pack_id: Pack identifier (e.g., "deepseek-v4-flash")

    Returns:
        ModelPack dataclass with validated configuration

    Raises:
        KeyError: If pack_id not found; lists available ids
        ValueError: If pack fails validation
    """
    table = _load_pack_table()

    if pack_id not in table:
        available = ", ".join(sorted(table.keys()))
        raise KeyError(
            f"Pack '{pack_id}' not found. Available: {available}"
        )

    data = table[pack_id]

    # Parse quant ladder
    quant_ladder = [
        QuantSpec(
            id=q["id"],
            size_gb=q["size_gb"],
            min_total_memory_gb=q["min_total_memory_gb"],
        )
        for q in data.get("quant_ladder", [])
    ]

    pack = ModelPack(
        id=data["id"],
        display_name=data["display_name"],
        hf_repo=data["hf_repo"],
        arch=data["arch"],
        total_params_b=data["total_params_b"],
        activated_params_b=data.get("activated_params_b"),
        is_moe=data.get("is_moe", False),
        quant_ladder=quant_ladder,
        tensor_class_regexes=data.get("tensor_class_regexes", {}),
        kv_cache_policy=data.get("kv_cache_policy", ""),
        chat_template=data.get("chat_template", ""),
        license=data.get("license", ""),
        attribution_required=data.get("attribution_required", False),
        draft_model=data.get("draft_model"),
    )

    pack.validate()
    return pack


def select_quant(
    pack: ModelPack,
    pool_gb: float,
) -> Optional[str]:
    """
    Select largest quant that fits the total pool.

    Replicates the existing largest-that-fits semantics from kimi_coordinator.
    Iterates ladder in reverse (highest min_total_memory first) and returns
    the first (highest) quant that fits the pool.

    Args:
        pack: ModelPack to select from
        pool_gb: Combined RAM+VRAM across all participating nodes (GB)

    Returns:
        Quantization id (e.g., "UD-Q8_K_XL") or None if pool < minimum

    Raises:
        ValueError: If pack has no quant ladder
    """
    if not pack.quant_ladder:
        raise ValueError(f"Pack '{pack.id}' has no quant ladder")

    # Sort by min_total_memory_gb descending (largest first)
    sorted_quants = sorted(
        pack.quant_ladder,
        key=lambda q: q.min_total_memory_gb,
        reverse=True,
    )

    # Return first (largest) quant that fits
    for quant in sorted_quants:
        if pool_gb >= quant.min_total_memory_gb:
            return quant.id

    return None


def _validate_pack_schema(data: dict) -> None:
    """
    Validate pack structure (before dataclass creation).

    Raises:
        ValueError: If required fields are missing or malformed
    """
    required_fields = [
        "id",
        "display_name",
        "hf_repo",
        "arch",
        "total_params_b",
        "is_moe",
        "quant_ladder",
    ]
    for field in required_fields:
        if field not in data:
            raise ValueError(f"Pack missing required field: {field}")

    if not isinstance(data["quant_ladder"], list):
        raise ValueError("quant_ladder must be a list")

    for i, quant in enumerate(data["quant_ladder"]):
        quant_required = ["id", "size_gb", "min_total_memory_gb"]
        for field in quant_required:
            if field not in quant:
                raise ValueError(
                    f"Quant {i} missing required field: {field}"
                )


def _self_test() -> None:
    """
    Test load, validation, and selection at boundary pool sizes.

    Exits 1 on failure, 0 on success.
    """
    try:
        # Test 1: list_packs
        packs = list_packs()
        assert len(packs) >= 2, f"Expected >= 2 packs, got {len(packs)}"
        print(f"list_packs: {packs}")

        # Test 2: load_pack (valid)
        kimi = load_pack("kimi-k3")
        assert kimi.id == "kimi-k3"
        assert not kimi.is_moe
        assert kimi.activated_params_b is None
        print(f"load_pack(kimi-k3): {kimi.display_name}")

        # Test 3: load_pack (MoE)
        ds = load_pack("deepseek-v4-flash")
        assert ds.id == "deepseek-v4-flash"
        assert ds.is_moe
        assert ds.activated_params_b == 13
        print(
            f"load_pack(deepseek-v4-flash): {ds.display_name}, "
            f"activated={ds.activated_params_b}B"
        )

        # Test 4: validation rejection (MoE without activated_params_b)
        # Create a bad pack by mutating kimi to is_moe=True
        try:
            bad_pack = ModelPack(
                id="bad",
                display_name="Bad Pack",
                hf_repo="test/test",
                arch="test",
                total_params_b=1,
                activated_params_b=None,
                is_moe=True,
                quant_ladder=[
                    QuantSpec(
                        id="test",
                        size_gb=100,
                        min_total_memory_gb=110,
                    )
                ],
                tensor_class_regexes={},
                kv_cache_policy="",
                chat_template="",
                license="",
                attribution_required=False,
                draft_model=None,
            )
            bad_pack.validate()
            raise AssertionError(
                "Should have rejected MoE without activated_params_b"
            )
        except ValueError as e:
            assert "activated_params_b" in str(e)
            print("validation rejected MoE without activated_params_b")

        # Test 5: select_quant at pool boundaries
        kimi_ladder = kimi.quant_ladder
        assert len(kimi_ladder) > 0

        # Smallest quant in ladder
        min_quant = min(kimi_ladder, key=lambda q: q.min_total_memory_gb)
        min_gb = min_quant.min_total_memory_gb

        # Test below minimum
        result = select_quant(kimi, min_gb - 1)
        assert (result is None), (
            f"Expected None for {min_gb - 1}GB, got {result}"
        )
        print(f"select_quant({min_gb - 1}GB) = None")

        # Test at minimum
        result = select_quant(kimi, min_gb)
        assert (result == min_quant.id), (
            f"Expected {min_quant.id}, got {result}"
        )
        print(f"select_quant({min_gb}GB) = {result}")

        # Largest quant in ladder
        max_quant = max(kimi_ladder, key=lambda q: q.min_total_memory_gb)
        max_gb = max_quant.min_total_memory_gb

        # Test at maximum
        result = select_quant(kimi, max_gb)
        assert (result == max_quant.id), (
            f"Expected {max_quant.id}, got {result}"
        )
        print(f"select_quant({max_gb}GB) = {result}")

        # Test above maximum
        result = select_quant(kimi, max_gb + 100)
        assert (result == max_quant.id), (
            f"Expected {max_quant.id}, got {result}"
        )
        print(f"select_quant({max_gb + 100}GB) = {result}")

        # Test 6: load_pack (unknown)
        try:
            load_pack("nonexistent")
            raise AssertionError("Should have raised KeyError")
        except KeyError as e:
            assert "Available:" in str(e)
            print("load_pack(nonexistent) raised KeyError with available list")

        print("All tests passed")
        sys.exit(0)

    except Exception as e:
        print(f"Test failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--self-test":
        _self_test()
    else:
        print("Usage: python model_pack_loader.py --self-test")
        _sys.exit(1)
