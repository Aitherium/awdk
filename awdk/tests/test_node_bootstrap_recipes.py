"""Tests for node_bootstrap recipe engine.

Validates:
  - All 9 recipes load and conform to schema
  - Resolution matrix: various hardware configurations resolve to correct recipe
  - Explicit recipe_id overrides
  - No recipe emits catalog backend "ollama" (fleet trap; use ollama_remote)
"""

from __future__ import annotations

from adk.toolpacks.node_bootstrap.recipes import (
    RECIPE_IDS,
    RECIPES_DIR,
    get_recipe,
    list_recipes,
    resolve_recipe,
)

# Schema requirements per section (authoritative spec from task)
REQUIRED_ROOT_KEYS = {
    "id",
    "name",
    "tier",
    "description",
    "hardware_requirements",
    "inference_config",
    "deployment",
    "backend_config",
    "verify",
    "fleet_wiring",
}

REQUIRED_HW_KEYS = {
    "min_ram_gb",
    "min_cpu_cores",
    "gpu_vendor",
    "min_vram_gb",
    "unified_memory",
    "notes",
}

REQUIRED_INFERENCE_KEYS = {
    "engine",
    "models",
    "serve_args",
    "image",
    "platform_traps",
}

REQUIRED_DEPLOYMENT_KEYS = {
    "target",
    "compose_template",
    "delegate",
    "port",
}

REQUIRED_BACKEND_KEYS = {
    "backend_type",
    "health_path",
    "completion_path",
}

REQUIRED_VERIFY_KEYS = {
    "health_timeout_s",
    "completion",
}

REQUIRED_WIRING_KEYS = {
    "catalog_entry",
    "ms_env_var",
}


class TestRecipeLoading:
    """Test that all recipes load and conform to schema."""

    def test_all_recipes_load(self):
        """Every recipe loads and its `id` matches its filename."""
        assert RECIPE_IDS, "no recipes discovered at all"
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            assert recipe is not None, f"Recipe {recipe_id} failed to load"
            assert recipe.get("id") == recipe_id

    def test_every_yaml_on_disk_is_selectable(self):
        """A recipe file that RECIPE_IDS omits is INERT — it parses, `get_recipe` finds it by
        path, and nothing can ever select it, because `resolve_recipe` gates on membership.

        `bonsai-selfhost` shipped exactly that way against the old hardcoded list. This is the
        assertion the count-based test could not make: it compares against DISK, so adding a
        recipe and forgetting to register it fails here instead of silently doing nothing.
        """
        on_disk = {p.stem for p in RECIPES_DIR.glob("*.yaml")}
        assert on_disk, "recipes/ contains no yaml — the loader path is wrong"
        assert on_disk == set(RECIPE_IDS), (
            f"recipe files not selectable: {sorted(on_disk - set(RECIPE_IDS))}; "
            f"ids with no file: {sorted(set(RECIPE_IDS) - on_disk)}"
        )

    def test_recipe_list(self):
        """list_recipes() returns all recipe IDs."""
        recipes = list_recipes()
        assert set(recipes) == set(RECIPE_IDS)
        assert len(recipes) == len(RECIPE_IDS)

    def test_schema_root_keys(self):
        """Every recipe has required top-level keys."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            missing = REQUIRED_ROOT_KEYS - set(recipe.keys())
            assert not missing, f"{recipe_id}: missing root keys {missing}"

    def test_schema_hardware_requirements(self):
        """hardware_requirements section has required keys."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            hw = recipe.get("hardware_requirements", {})
            missing = REQUIRED_HW_KEYS - set(hw.keys())
            assert not missing, f"{recipe_id}: missing hw keys {missing}"

    def test_schema_inference_config(self):
        """inference_config section has required keys."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            inf = recipe.get("inference_config", {})
            missing = REQUIRED_INFERENCE_KEYS - set(inf.keys())
            assert not missing, f"{recipe_id}: missing inference keys {missing}"
            # Models should be a list with at least one entry
            models = inf.get("models", [])
            assert isinstance(models, list), f"{recipe_id}: models not a list"
            if recipe_id != "cloud-api":
                assert len(models) > 0, f"{recipe_id}: no models defined"

    def test_schema_deployment(self):
        """deployment section has required keys."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            dep = recipe.get("deployment", {})
            missing = REQUIRED_DEPLOYMENT_KEYS - set(dep.keys())
            assert not missing, f"{recipe_id}: missing deployment keys {missing}"

    def test_schema_backend_config(self):
        """backend_config section has required keys."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            bc = recipe.get("backend_config", {})
            missing = REQUIRED_BACKEND_KEYS - set(bc.keys())
            assert not missing, f"{recipe_id}: missing backend_config keys {missing}"

    def test_schema_verify(self):
        """verify section has required keys."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            ver = recipe.get("verify", {})
            missing = REQUIRED_VERIFY_KEYS - set(ver.keys())
            assert not missing, f"{recipe_id}: missing verify keys {missing}"

    def test_schema_fleet_wiring(self):
        """fleet_wiring section has required keys."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            fw = recipe.get("fleet_wiring", {})
            missing = REQUIRED_WIRING_KEYS - set(fw.keys())
            assert not missing, f"{recipe_id}: missing fleet_wiring keys {missing}"

    def test_no_ollama_backend_trap(self):
        """No recipe should emit catalog backend 'ollama' (fleet silent-remap trap)."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            backend = recipe.get("fleet_wiring", {}).get("catalog_entry", {}).get("backend")
            assert backend != "ollama", (
                f"{recipe_id}: uses forbidden backend 'ollama' "
                "(fleet silently remaps to vllm); use 'ollama_remote' instead"
            )


class TestRecipeResolution:
    """Test recipe resolution against various hardware configurations."""

    def test_resolution_cpu_8core_16gb_no_gpu(self):
        """8-core CPU, 16GB RAM, no GPU => cpu-ollama or cpu-1bit-llamacpp."""
        system = {
            "ram_gb": 16.0,
            "cpu_cores": 8,
            "gpu_vendor": "none",
            "gpu_vram_mb": 0,
            "unified_memory": False,
            "gpu_name": "",
        }
        result = resolve_recipe(system)
        assert result["recipe"]["id"] in [
            "cpu-ollama",
            "cpu-1bit-llamacpp",
        ], f"Got {result['recipe']['id']}"

    def test_resolution_nvidia_8gb_vram(self):
        """NVIDIA 8GB VRAM => cuda-vllm-8gb."""
        system = {
            "ram_gb": 8.0,
            "cpu_cores": 4,
            "gpu_vendor": "nvidia",
            "gpu_vram_mb": 8192,
            "unified_memory": False,
            "gpu_name": "RTX 4060",
        }
        result = resolve_recipe(system)
        assert result["recipe"]["id"] == "cuda-vllm-8gb"

    def test_resolution_nvidia_24gb_vram(self):
        """NVIDIA 24GB VRAM => cuda-vllm-24gb."""
        system = {
            "ram_gb": 12.0,
            "cpu_cores": 6,
            "gpu_vendor": "nvidia",
            "gpu_vram_mb": 24576,
            "unified_memory": False,
            "gpu_name": "RTX 4090 Mobile",
        }
        result = resolve_recipe(system)
        assert result["recipe"]["id"] == "cuda-vllm-24gb"

    def test_resolution_nvidia_32gb_vram(self):
        """NVIDIA 32GB VRAM => cuda-dual-stack-32gb (RTX 5090)."""
        system = {
            "ram_gb": 24.0,
            "cpu_cores": 16,
            "gpu_vendor": "nvidia",
            "gpu_vram_mb": 32768,
            "unified_memory": False,
            "gpu_name": "RTX 5090",
        }
        result = resolve_recipe(system)
        assert result["recipe"]["id"] == "cuda-dual-stack-32gb"

    def test_resolution_nvidia_40gb_vram(self):
        """NVIDIA 40GB VRAM => cuda-vllm-40gb."""
        system = {
            "ram_gb": 16.0,
            "cpu_cores": 8,
            "gpu_vendor": "nvidia",
            "gpu_vram_mb": 40960,
            "unified_memory": False,
            "gpu_name": "RTX 6000 Ada",
        }
        result = resolve_recipe(system)
        assert result["recipe"]["id"] == "cuda-vllm-40gb"

    def test_resolution_apple_16gb_unified(self):
        """Apple 16GB unified => metal-ollama."""
        system = {
            "ram_gb": 16.0,
            "cpu_cores": 10,
            "gpu_vendor": "apple",
            "gpu_vram_mb": 0,
            "unified_memory": True,
            "gpu_name": "Apple M3",
        }
        result = resolve_recipe(system)
        assert result["recipe"]["id"] == "metal-ollama"

    def test_resolution_apple_128gb_unified(self):
        """Apple 128GB unified => unified-memory-vllm."""
        system = {
            "ram_gb": 128.0,
            "cpu_cores": 20,
            "gpu_vendor": "apple",
            "gpu_vram_mb": 0,
            "unified_memory": True,
            "gpu_name": "Apple M4 Max",
        }
        result = resolve_recipe(system)
        assert result["recipe"]["id"] == "unified-memory-vllm"

    def test_resolution_no_gpu_no_ram(self):
        """Very limited hardware => cloud-api fallback."""
        system = {
            "ram_gb": 4.0,
            "cpu_cores": 2,
            "gpu_vendor": "none",
            "gpu_vram_mb": 0,
            "unified_memory": False,
            "gpu_name": "",
        }
        result = resolve_recipe(system)
        assert result["recipe"]["id"] == "cloud-api"

    def test_resolution_empty_system_info(self):
        """Empty system_info => cloud-api fallback."""
        result = resolve_recipe({})
        assert result["recipe"]["id"] == "cloud-api"

    def test_explicit_recipe_id_override(self):
        """Explicit recipe_id overrides resolution."""
        system = {
            "ram_gb": 4.0,
            "cpu_cores": 2,
            "gpu_vendor": "none",
            "gpu_vram_mb": 0,
            "unified_memory": False,
            "gpu_name": "",
        }
        # Even with minimal hardware, explicit recipe_id should work
        result = resolve_recipe(system, recipe_id="cloud-api")
        assert result["recipe"]["id"] == "cloud-api"
        assert result["match_score"] == 10.0

    def test_explicit_recipe_id_invalid_ignored(self):
        """Invalid explicit recipe_id falls back to resolution."""
        system = {
            "ram_gb": 8.0,
            "cpu_cores": 4,
            "gpu_vendor": "none",
            "gpu_vram_mb": 0,
            "unified_memory": False,
            "gpu_name": "",
        }
        result = resolve_recipe(system, recipe_id="not-a-real-recipe")
        # Should fall back to resolution
        assert result["recipe"]["id"] in RECIPE_IDS

    def test_resolution_returns_match_info(self):
        """Resolution result has required fields."""
        system = {
            "ram_gb": 16.0,
            "cpu_cores": 8,
            "gpu_vendor": "none",
            "gpu_vram_mb": 0,
            "unified_memory": False,
            "gpu_name": "",
        }
        result = resolve_recipe(system)
        assert "recipe" in result
        assert "match_score" in result
        assert "rationale" in result
        assert "warnings" in result
        assert isinstance(result["match_score"], (int, float))
        assert isinstance(result["rationale"], str)
        assert isinstance(result["warnings"], list)

    def test_resolution_rationale_includes_system_info(self):
        """Rationale describes the system that was matched."""
        system = {
            "ram_gb": 16.0,
            "cpu_cores": 8,
            "gpu_vendor": "nvidia",
            "gpu_vram_mb": 24576,
            "unified_memory": False,
            "gpu_name": "RTX 4090 Mobile",
        }
        result = resolve_recipe(system)
        rationale = result["rationale"]
        # Should mention cores, RAM, and GPU
        assert "8" in rationale or "core" in rationale.lower()
        assert "16" in rationale or "RAM" in rationale
        assert "nvidia" in rationale.lower() or "24" in rationale


class TestTierEdgeCases:
    """Test that tier logic handles edge cases correctly."""

    @staticmethod
    def _box(vram_gb=0, vendor="none", ram=32.0, cores=12, unified=False):
        return {
            "ram_gb": ram, "cpu_cores": cores, "gpu_vendor": vendor,
            "gpu_vram_mb": int(vram_gb * 1024), "unified_memory": unified,
            "gpu_name": "test-gpu" if vendor != "none" else "",
        }

    def test_tie_break_prefers_self_contained_over_delegate(self):
        """Ties must NOT be won by fleet-delegate recipes via alphabetical accident.

        A big CPU box matches both cpu recipes (same tier); cpu-1bit-llamacpp is
        a fleet delegate (public users can't run it) so cpu-ollama must win.
        """
        result = resolve_recipe(self._box(ram=64.0, cores=16))
        assert result["recipe"]["id"] == "cpu-ollama", result["rationale"]

    def test_vram_band_edges(self):
        """VRAM band edges resolve to the exact expected recipe."""
        expectations = [
            (7.9, "nvidia", 12, None),               # below the 8GB floor: no cuda
            (8.0, "nvidia", 12, "cuda-vllm-8gb"),    # exactly at the floor
            (15.0, "nvidia", 12, "cuda-vllm-8gb"),   # between bands stays small
            (24.0, "nvidia", 12, "cuda-vllm-24gb"),
            (32.0, "nvidia", 12, "cuda-vllm-24gb"),  # dual-stack needs 16 cores
            (32.0, "nvidia", 16, "cuda-dual-stack-32gb"),
            (40.0, "nvidia", 16, "cuda-vllm-40gb"),
            (48.0, "nvidia", 16, "cuda-vllm-40gb"),
        ]
        for vram, vendor, cores, expected in expectations:
            got = resolve_recipe(self._box(vram, vendor, cores=cores))["recipe"]["id"]
            if expected is None:
                assert not got.startswith("cuda-"), \
                    f"{vram}GB/{cores}c must not match a cuda recipe, got {got}"
            else:
                assert got == expected, f"{vram}GB/{cores}c: want {expected}, got {got}"

    def test_prefer_backend_honored_or_warned(self):
        """prefer_backend must either be honored or produce an explicit warning."""
        result = resolve_recipe(self._box(ram=64.0, cores=16),
                                prefer_backend="llamacpp")
        engine = result["recipe"]["inference_config"]["engine"]
        assert engine == "llamacpp" or any(
            "prefer_backend" in w for w in result.get("warnings", [])
        ), f"prefer_backend silently ignored (got engine={engine})"

    def test_recipe_with_platform_traps_surfaces_in_warnings(self):
        """Explicit-id resolution must surface the recipe's platform_traps as warnings."""
        result = resolve_recipe({}, recipe_id="cuda-vllm-8gb")
        traps = result["recipe"].get("inference_config", {}).get("platform_traps", [])
        assert traps, "cuda-vllm-8gb is expected to declare platform_traps"
        for trap in traps:
            assert trap in result["warnings"], \
                f"trap {trap!r} missing from warnings {result['warnings']}"


class TestRecipeConsistency:
    """Test that all recipes are internally consistent."""

    def test_recipe_tier_in_tier_ranks(self):
        """Every recipe's tier is known (in TIER_RANKS)."""
        from adk.toolpacks.node_bootstrap.recipes import TIER_RANKS

        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            tier = recipe.get("tier")
            assert tier in TIER_RANKS, f"{recipe_id}: unknown tier {tier}"

    def test_recipe_id_matches_filename(self):
        """Recipe 'id' field matches its filename."""
        for recipe_id in RECIPE_IDS:
            recipe = get_recipe(recipe_id)
            assert recipe.get("id") == recipe_id

    def test_no_duplicate_recipe_ids(self):
        """All recipe IDs in RECIPE_IDS are unique."""
        assert len(RECIPE_IDS) == len(set(RECIPE_IDS))
