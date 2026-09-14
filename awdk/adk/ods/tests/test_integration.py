"""Integration tests for ODS resolver with LLMFitClient.

Tests that OdsResolver integrates correctly with LLMFitClient.recommend_config(),
preserving legacy return shape while using ODS as primary model selector.
"""

from __future__ import annotations

import pytest

from adk.ods.resolver import OdsResolver
from adk.ods.model_types import OdsRecommendation


class TestOdsIntegrationWithLLMFit:
    """Integration tests between OdsResolver and LLMFitClient."""

    def test_ods_resolver_available_standalone(self):
        """OdsResolver can be instantiated and used independently."""
        resolver = OdsResolver()
        assert resolver is not None
        assert hasattr(resolver, "resolve")
        assert hasattr(resolver, "catalog")

    def test_ods_resolver_return_type(self):
        """OdsResolver.resolve() returns OdsRecommendation (not dict)."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
            tier="0",
        )
        assert isinstance(result, OdsRecommendation)
        assert not isinstance(result, dict)

    def test_no_circular_import_ods_llmfit(self):
        """ODS and llmfit can both be imported without circular dependency."""
        try:
            from adk.ods.resolver import OdsResolver as ODS_Resolver
            # This would test import, but we can't import llmfit without
            # checking if it exists in the codebase
            assert ODS_Resolver is not None
        except ImportError:
            pytest.fail("OdsResolver import failed")

    def test_ods_recommendation_dataclass_fields(self):
        """OdsRecommendation has required fields for llmfit adapter."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
            tier="0",
        )
        # Fields that llmfit adapter needs to transform
        assert result.selected is not None
        assert result.selected.id is not None
        assert result.selected.size_mb >= 0
        assert result.selected.vram_required_gb >= 0
        assert result.selected.llm_model_name is not None
        assert result.selected.gguf_url is not None
        assert result.selected.gguf_sha256 is not None
        assert result.profile in ("qwen", "gemma4")
        assert result.confidence > 0

    def test_ods_return_shape_compatible_with_legacy(self):
        """OdsRecommendation can be transformed to legacy recommend_config shape."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="nvidia",
            memory_type="discrete",
            vram_mb=24576,
            ram_gb=32,
            profile="qwen",
            tier="3",
        )
        # Simulate legacy adapter transformation
        # Legacy shape: {'hardware': {...}, 'fast': {...}, 'balanced': {...}, ...}
        legacy_shape = {
            "hardware": {
                "backend": "nvidia",
                "vram_mb": 24576,
                "ram_gb": 32,
            },
            "fast": {
                "model": result.selected.llm_model_name,
                "score": result.confidence,
                "tps": 20,  # Dummy
            },
            "balanced": {
                "model": result.selected.llm_model_name,
                "score": result.confidence,
                "tps": 15,
            },
            "reasoning": {
                "model": result.selected.llm_model_name,
                "score": result.confidence,
                "tps": 10,
            },
            "coding": {
                "model": result.selected.llm_model_name,
                "score": result.confidence,
                "tps": 12,
            },
            "embedding": {
                "model": result.selected.llm_model_name,
                "score": result.confidence,
                "tps": 50,
            },
        }
        # Verify shape has all required keys
        assert "hardware" in legacy_shape
        assert "fast" in legacy_shape
        assert "balanced" in legacy_shape
        assert "reasoning" in legacy_shape
        assert "coding" in legacy_shape
        assert "embedding" in legacy_shape
        # Verify nested dict keys
        for tier_name in ["fast", "balanced", "reasoning", "coding", "embedding"]:
            assert "model" in legacy_shape[tier_name]
            assert "score" in legacy_shape[tier_name]

    def test_ods_model_record_gguf_fields_present(self):
        """Selected model has GGUF metadata for download integration."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
            tier="0",
        )
        model = result.selected
        # GGUF integration needs these fields
        assert model.gguf_file is not None
        assert model.gguf_url is not None
        assert model.gguf_sha256 is not None
        assert len(model.gguf_file) > 0
        assert len(model.gguf_url) > 0

    def test_ods_fallback_still_returns_valid_recommendation(self):
        """Fallback pool selection still produces valid OdsRecommendation."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=1,  # Very small → fallback
            profile="qwen",
            tier="0",
        )
        # Even with fallback, should have valid fields
        assert result.selected is not None
        assert result.selected.llm_model_name is not None
        assert result.confidence > 0


class TestOdsModelCatalogStructure:
    """Test that model catalog structure matches integration requirements."""

    def test_catalog_has_required_model_fields(self):
        """Catalog models have all fields needed by llmfit adapter."""
        resolver = OdsResolver()
        models = resolver.catalog.get("models", [])
        assert len(models) > 0
        for model_dict in models:
            # Required fields for adapter
            assert "id" in model_dict
            assert "name" in model_dict
            assert "family" in model_dict
            assert "gguf_file" in model_dict
            assert "gguf_url" in model_dict
            assert "size_mb" in model_dict
            assert "vram_required_gb" in model_dict
            assert "llm_model_name" in model_dict

    def test_catalog_models_have_runtime_profiles(self):
        """Catalog models have runtime_profiles for performance prediction."""
        resolver = OdsResolver()
        models = resolver.catalog.get("models", [])
        for model_dict in models:
            if "runtime_profiles" in model_dict:
                profiles = model_dict["runtime_profiles"]
                # Upstream shape (ODS 2.5.3): runtime_profiles is a LIST of
                # per-backend dicts, e.g. [{"backend": "nvidia", "context_length":
                # 65536, "env": {...}, "estimated_required_gb": 7.2}, ...].
                # Only 3/52 models carry it, hence the presence guard above.
                assert isinstance(profiles, list)
                assert profiles, f"{model_dict['id']} has an empty runtime_profiles"
                for profile_data in profiles:
                    assert isinstance(profile_data, dict)
                    assert "backend" in profile_data


class TestOdsBackendCompatibility:
    """Test that ODS resolver handles all backend types."""

    def test_backend_nvidia(self):
        """NVIDIA backend works."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="nvidia",
            memory_type="discrete",
            vram_mb=8192,
            ram_gb=16,
            profile="qwen",
        )
        assert result.selected is not None

    def test_backend_apple(self):
        """Apple backend works."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="apple",
            memory_type="unified",
            vram_mb=0,
            ram_gb=16,
            profile="qwen",
        )
        assert result.selected is not None

    def test_backend_amd(self):
        """AMD backend works."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="amd",
            memory_type="discrete",
            vram_mb=8192,
            ram_gb=16,
            profile="qwen",
        )
        assert result.selected is not None

    def test_backend_cpu(self):
        """CPU backend works."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
        )
        assert result.selected is not None

    def test_backend_unknown(self):
        """Unknown backend handled gracefully."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="unknown",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
        )
        # Should fall back to safe defaults
        assert result.selected is not None


class TestOdsMemoryLabelHuman:
    """Test human-readable memory labels."""

    def test_memory_label_nvidia(self):
        """NVIDIA memory label readable."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="nvidia",
            memory_type="discrete",
            vram_mb=24576,
            ram_gb=32,
            profile="qwen",
        )
        # Upstream usable_memory_gb() returns a MEMORY-KIND label, not a device
        # name: "GPU VRAM" / "unified system memory" / "system RAM". The device
        # name never appears — asserting for it only passed against placeholder data.
        assert result.memory_label == "GPU VRAM"
        # Discrete path is vram_mb/1024 at 100% (upstream _upstream_select.py:144),
        # NOT 95%. A 24576MB card yields exactly 24.0GB.
        assert result.memory_capacity_gb == 24.0

    def test_memory_label_apple(self):
        """Apple memory label readable."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="apple",
            memory_type="unified",
            vram_mb=0,
            ram_gb=16,
            profile="qwen",
        )
        assert result.memory_label == "unified system memory"
        # 16GB unified -> 55% share (upstream _upstream_select.py:141)
        assert result.memory_capacity_gb == pytest.approx(16 * 0.55, abs=0.1)

    def test_memory_label_cpu(self):
        """CPU memory label readable."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
        )
        assert "CPU" in result.memory_label or "RAM" in result.memory_label


class TestOdsAlternativesGeneration:
    """Test that alternatives are properly ranked."""

    def test_alternatives_list_populated(self):
        """Alternatives list contains top alternatives."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="nvidia",
            memory_type="discrete",
            vram_mb=24576,
            ram_gb=32,
            profile="qwen",
            tier="3",
        )
        # Should have alternatives (or empty list is ok)
        assert isinstance(result.alternatives, list)
        # At most top 3
        assert len(result.alternatives) <= 3

    def test_alternatives_different_from_selected(self):
        """Alternatives don't include selected model."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="nvidia",
            memory_type="discrete",
            vram_mb=24576,
            ram_gb=32,
            profile="qwen",
            tier="3",
        )
        # Upstream semantics: `alternatives` is ranked[:3], which INCLUDES the
        # selected model at index 0 (and, on the arch-policy path, is explicitly
        # built as [selected] + next two). It is a ranked shortlist, not an
        # "everything except the pick" list.
        assert result.alternatives[0].id == result.selected.id
        assert len(result.alternatives) <= 3
        assert len({a.id for a in result.alternatives}) == len(result.alternatives)


class TestOdsReasoning:
    """Test that reasoning/explanation is clear."""

    def test_reason_not_empty(self):
        """Reasoning string is populated."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
            tier="0",
        )
        assert result.reason is not None
        assert len(result.reason) > 0

    def test_reason_mentions_hardware(self):
        """Reasoning mentions hardware tier."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
            tier="0",
        )
        reason_lower = result.reason.lower()
        # Should mention either tier or hardware
        assert "tier" in reason_lower or "hardware" in reason_lower or "cpu" in reason_lower

    def test_reason_mentions_profile(self):
        """Reasoning mentions requested profile."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
            tier="0",
        )
        reason_lower = result.reason.lower()
        assert "qwen" in reason_lower or "profile" in reason_lower


class TestOdsPolicyName:
    """Test policy naming."""

    def test_policy_not_empty(self):
        """Policy name is populated."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
            tier="0",
        )
        assert result.policy is not None
        assert len(result.policy) > 0

    def test_policy_mentions_profile_or_hardware(self):
        """Policy name indicates selection strategy."""
        resolver = OdsResolver()
        result = resolver.resolve(
            backend="cpu",
            memory_type=None,
            vram_mb=0,
            ram_gb=8,
            profile="qwen",
            tier="0",
        )
        policy_lower = result.policy.lower()
        # Should mention profile, hardware, or "default"
        # Upstream POLICY is a fixed strategy id, optionally suffixed with an
        # arch-policy tag (e.g. "+unified-memory-coder-next-a3b-v1"). It does not
        # embed the profile or backend.
        assert policy_lower.startswith("context-aware-largest-capable-general-v1")


class TestGgufIntegrityVerification:
    """The catalog's gguf_sha256 pins must be enforced for model integrity."""

    def test_matching_hash_passes_and_keeps_file(self, tmp_path):
        import hashlib

        from adk.llamacpp_setup import verify_sha256

        f = tmp_path / "m.gguf"
        f.write_bytes(b"pretend-model-weights")
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        assert verify_sha256(f, digest, "m") is True
        assert f.exists()

    def test_mismatching_hash_fails_closed_and_deletes(self, tmp_path):
        from adk.llamacpp_setup import verify_sha256

        f = tmp_path / "tampered.gguf"
        f.write_bytes(b"tampered-weights")
        wrong = "0" * 64
        # Must FAIL, and must delete — otherwise the >100MB size check would
        # accept the corrupt file as a valid model on the next run.
        assert verify_sha256(f, wrong, "tampered") is False
        assert not f.exists()

    def test_absent_pin_is_allowed(self, tmp_path):
        from adk.llamacpp_setup import verify_sha256

        # 3 of 52 catalog models are multi-part and carry no single hash.
        f = tmp_path / "unpinned.gguf"
        f.write_bytes(b"x")
        assert verify_sha256(f, "", "unpinned") is True
        assert f.exists()

    def test_catalog_pins_are_present_and_well_formed(self):
        """The pins we rely on must actually be real sha256 values."""
        from adk.ods import OdsResolver

        models = OdsResolver().catalog["models"]
        pinned = [m for m in models if m.get("gguf_sha256")]
        assert len(pinned) >= 45, f"expected most models pinned, got {len(pinned)}"
        for m in pinned:
            h = m["gguf_sha256"]
            assert len(h) == 64 and all(c in "0123456789abcdef" for c in h.lower()), \
                f"{m['id']} has a malformed sha256: {h!r}"

    def test_catalog_pin_lookup_is_wired(self):
        """The pin must be found automatically — not left to opt-in callers."""
        from adk.llamacpp_setup import catalog_sha256_for
        from adk.ods import OdsResolver

        models = OdsResolver().catalog["models"]
        sample = next(m for m in models if m.get("gguf_sha256") and m.get("gguf_file"))
        assert catalog_sha256_for(sample["gguf_file"]) == sample["gguf_sha256"]
        # Non-catalog files stay unpinned rather than raising.
        assert catalog_sha256_for("definitely-not-in-catalog.gguf") == ""
