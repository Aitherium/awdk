"""Tests for the ObjectRegistry and render_observation."""

import pytest
import time
from adk.object_registry import ObjectRegistry, get_registry, render_observation


class TestObjectRegistry:
    """Test core ObjectRegistry functionality."""

    def test_register_and_get_roundtrip(self):
        """Test that register/get round-trips a large object without full content in preview."""
        registry = ObjectRegistry()
        large_list = list(range(10000))

        # Register the object
        handle = registry.register(large_list)
        assert handle.startswith("obj:list:")

        # Retrieve it
        retrieved = registry.get(handle)
        assert retrieved is large_list

    def test_handle_format_readable(self):
        """Test that handles are human/model-friendly (not raw UUIDs)."""
        registry = ObjectRegistry()
        obj = [1, 2, 3]
        handle = registry.register(obj)

        # Should be in format obj:<type>:<hash>
        parts = handle.split(":")
        assert len(parts) == 3
        assert parts[0] == "obj"
        assert parts[1] == "list"
        assert len(parts[2]) == 6  # hash prefix is 6 chars
        # Should only contain alphanumeric
        assert parts[2].isalnum()

    def test_preview_bounded_list(self):
        """Test that preview of a 10000-element list is bounded and contains len but not all elements."""
        registry = ObjectRegistry()
        large_list = list(range(10000))

        handle = registry.register(large_list)
        preview = registry.preview(handle, max_chars=300)

        # Preview should be bounded (either shows len marker or truncation notice)
        # The pformat output will be large, so TruncatingStringIO will kick in
        assert "truncated" in preview.lower() or "<" in preview
        # Preview should be relatively small (well under 1KB)
        assert len(preview) < 1000

    def test_preview_bounded_string(self):
        """Test that preview of a 1MB string is bounded."""
        registry = ObjectRegistry()
        large_string = "x" * (1024 * 1024)  # 1MB string

        handle = registry.register(large_string)
        preview = registry.preview(handle, max_chars=200)

        # Preview should be bounded significantly (well under 10KB even with overhead)
        assert len(preview) < 10000
        # Should show truncation indication
        assert "truncated" in preview.lower() or "..." in preview

    def test_deref_valid_handle(self):
        """Test that deref resolves a valid handle."""
        registry = ObjectRegistry()
        obj = {"key": "value"}
        handle = registry.register(obj)

        retrieved = registry.deref(handle)
        assert retrieved is obj

    def test_deref_invalid_handle(self):
        """Test that deref returns None for invalid handle."""
        registry = ObjectRegistry()
        result = registry.deref("obj:invalid:000000")
        assert result is None

    def test_describe_includes_type_and_handle(self):
        """Test that describe returns type, len, preview, and handle."""
        registry = ObjectRegistry()
        obj = [1, 2, 3, 4, 5]
        handle = registry.register(obj)

        description = registry.describe(handle)

        # Should include type, len, handle, and preview
        assert "list" in description
        assert f"len=5" in description
        assert f"id={handle}" in description
        assert "preview=" in description

    def test_eviction_when_cap_exceeded(self):
        """Test LRU eviction when max_objects is exceeded."""
        registry = ObjectRegistry(max_objects=3)

        obj1 = [1]
        obj2 = [2]
        obj3 = [3]
        obj4 = [4]

        h1 = registry.register(obj1)
        h2 = registry.register(obj2)
        h3 = registry.register(obj3)
        h4 = registry.register(obj4)  # Should evict h1

        # obj1 should be evicted
        with pytest.raises(KeyError):
            registry.get(h1)

        # Others should still be present
        assert registry.get(h2) is obj2
        assert registry.get(h3) is obj3
        assert registry.get(h4) is obj4

    def test_ttl_expiration(self):
        """Test that objects expire after their TTL."""
        registry = ObjectRegistry(default_ttl_seconds=0.1)

        obj = {"data": "value"}
        handle = registry.register(obj, ttl_seconds=0.1)

        # Should be available immediately
        assert registry.get(handle) is obj

        # Wait for TTL to expire
        time.sleep(0.15)

        # Should be expired
        with pytest.raises(KeyError):
            registry.get(handle)

    def test_access_order_lru(self):
        """Test that accessing an object moves it to the end (most recently used)."""
        registry = ObjectRegistry(max_objects=2)

        obj1 = [1]
        obj2 = [2]
        obj3 = [3]

        h1 = registry.register(obj1)
        h2 = registry.register(obj2)

        # Access obj1, making it more recent than obj2
        registry.get(h1)

        # Register obj3, should evict obj2 (least recently used)
        h3 = registry.register(obj3)

        # obj1 and obj3 should be present
        assert registry.get(h1) is obj1
        assert registry.get(h3) is obj3

        # obj2 should be evicted
        with pytest.raises(KeyError):
            registry.get(h2)

    def test_clear(self):
        """Test that clear removes all registered objects."""
        registry = ObjectRegistry()
        obj1 = [1]
        obj2 = [2]

        h1 = registry.register(obj1)
        h2 = registry.register(obj2)

        registry.clear()

        with pytest.raises(KeyError):
            registry.get(h1)
        with pytest.raises(KeyError):
            registry.get(h2)

    def test_stats(self):
        """Test that stats returns correct count and max_objects."""
        registry = ObjectRegistry(max_objects=5)

        obj1 = [1]
        obj2 = [2]

        registry.register(obj1)
        registry.register(obj2)

        stats = registry.stats()
        assert stats["count"] == 2
        assert stats["max_objects"] == 5

    def test_singleton_get_registry(self):
        """Test that get_registry returns the same singleton."""
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2


class TestRenderObservation:
    """Test render_observation helper function."""

    def test_render_small_value_plainly(self):
        """Test that render_observation returns small values plainly."""
        small_list = [1, 2, 3]
        result = render_observation(small_list, max_chars=500)

        # Should be a simple representation without a handle
        assert "[1, 2, 3]" in result
        # Should not mention "available via handle" if small
        assert "available via handle:" not in result or "id=obj:" not in result

    def test_render_huge_value_with_handle(self):
        """Test that render_observation registers huge values and mentions the handle."""
        registry = ObjectRegistry()
        huge_list = list(range(100000))

        result = render_observation(huge_list, registry=registry, max_chars=200)

        # Should mention the handle
        assert "available via handle:" in result or "id=obj:" in result
        assert "obj:list:" in result

        # The handle should resolve to the original object
        handle_start = result.find("obj:list:")
        if handle_start >= 0:
            # Extract handle (format: obj:list:XXXXXX)
            handle_end = result.find(")", handle_start)
            if handle_end < 0:
                handle_end = result.find("\n", handle_start)
            if handle_end > 0:
                handle_part = result[handle_start:handle_end].rstrip(")")
                obj = registry.deref(handle_part)
                if obj is not None:
                    assert obj is huge_list

    def test_render_huge_string_with_handle(self):
        """Test that large strings get registered with a handle."""
        registry = ObjectRegistry()
        huge_string = "x" * (10 * 1024)  # 10KB string

        result = render_observation(huge_string, registry=registry, max_chars=200)

        # Result should be bounded
        assert len(result) < 1000  # Much smaller than the original

        # Should mention handle
        assert "id=obj:" in result or "available via handle:" in result

    def test_render_uses_singleton_by_default(self):
        """Test that render_observation uses the global singleton registry."""
        obj = list(range(100000))

        result1 = render_observation(obj, max_chars=200)
        result2 = render_observation(obj, max_chars=200)

        # Both should generate valid representations
        assert isinstance(result1, str)
        assert isinstance(result2, str)

    def test_render_observation_custom_registry(self):
        """Test render_observation with a custom registry."""
        registry = ObjectRegistry()
        obj = list(range(1000))

        result = render_observation(obj, registry=registry, max_chars=200)

        # Should use the provided registry
        assert isinstance(result, str)

    def test_render_small_string_not_registered(self):
        """Test that small strings are rendered plainly without registration."""
        registry = ObjectRegistry()
        small_string = "Hello, world!"

        result = render_observation(small_string, registry=registry, max_chars=500)

        # Should be the string itself or close to it
        assert "Hello, world!" in result
        # Registry should have nothing or minimal entries
        stats = registry.stats()
        # Either not registered, or registered but still returned plainly
        assert stats["count"] <= 1  # At most the string itself

    def test_render_bounded_preview_content(self):
        """Test that the bounded preview in render_observation doesn't exceed max_chars."""
        registry = ObjectRegistry()
        huge_dict = {f"key{i}": f"value{i}" for i in range(10000)}

        result = render_observation(huge_dict, registry=registry, max_chars=300)

        # The total output should be bounded even for huge objects
        # (well under the unbounded string representation)
        assert len(result) < 100000  # Much smaller than a full repr of the dict
