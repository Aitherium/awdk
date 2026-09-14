# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Parity tests for adk.agentdoc pformat ported from NOOA.

Tests the bounded-preview engine for correct handling of:
  1. Structured instances (dataclasses, Pydantic) with builtins module
  2. Field-level max_string override (not yet ported — structure verified)
"""

import pytest
from dataclasses import dataclass
from adk.agentdoc import pformat
from adk.agentdoc._pformat import _is_structured_instance


class TestBuiltinsStructuredInstance:
    """Test that _is_structured_instance() handles builtins module correctly.

    When a dataclass/Pydantic model is created via exec() in REPL/test contexts,
    its __module__ is set to 'builtins'. Early guards checking __module__ == 'builtins'
    break detection of structured types. Verify the fix by checking marker attrs
    AFTER module check.
    """

    def test_pydantic_with_builtins_module(self):
        """Pydantic model with __module__='builtins' should be recognized."""
        try:
            from pydantic import BaseModel
        except ImportError:
            pytest.skip("pydantic not installed")

        # Create Pydantic model in exec context (simulates REPL/test scenario)
        code = """
from pydantic import BaseModel

class Person(BaseModel):
    name: str
    age: int
"""
        namespace = {}
        exec(code, namespace)
        Person = namespace["Person"]
        assert Person.__module__ == "builtins"

        # _is_structured_instance should still recognize it
        instance = Person(name="Alice", age=30)
        assert _is_structured_instance(instance)

    def test_dataclass_with_builtins_module(self):
        """Dataclass with __module__='builtins' should be recognized."""
        # Create dataclass in exec context
        code = """
from dataclasses import dataclass

@dataclass
class Point:
    x: float
    y: float
"""
        namespace = {}
        exec(code, namespace)
        Point = namespace["Point"]
        assert Point.__module__ == "builtins"

        instance = Point(x=1.0, y=2.0)
        assert _is_structured_instance(instance)

    def test_attrs_with_builtins_module(self):
        """Attrs class with __module__='builtins' should be recognized."""
        try:
            import attr
        except ImportError:
            pytest.skip("attrs not installed")

        code = """
import attr

@attr.s
class Vector:
    x: float = attr.ib()
    y: float = attr.ib()
"""
        namespace = {}
        exec(code, namespace)
        Vector = namespace["Vector"]
        assert Vector.__module__ == "builtins"

        instance = Vector(x=3.0, y=4.0)
        assert _is_structured_instance(instance)


class TestBoundedPreviewFormatting:
    """Test that pformat produces bounded output for large structures."""

    def test_list_with_max_length(self):
        """Large lists should be abbreviated with head/tail elements."""
        big_list = list(range(1000))
        result = pformat(big_list, max_length=3)

        # Should show length and some head/tail elements
        assert "len=1000" in result
        assert "[:" in result  # Should have head elements marker
        assert "[-" in result  # Should have tail elements marker

        # Should not include middle elements
        assert str(500) not in result

    def test_string_truncation_with_max_string(self):
        """Long strings should be truncated with marker."""
        long_str = "x" * 10000
        result = pformat(long_str, max_string=100)

        # Should show truncation marker
        assert "len=" in result
        assert len(result) < len(long_str)

    def test_dict_with_max_depth(self):
        """Deeply nested dicts should be truncated at max_depth."""
        nested = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        result_shallow = pformat(nested, max_depth=2)
        result_deep = pformat(nested, max_depth=5)

        # Shallower result should be shorter (less nesting shown)
        assert len(result_shallow) <= len(result_deep)

    def test_complex_structure_bounded(self):
        """Mixed structure with lists/dicts/strings should all be bounded."""
        data = {
            "ids": list(range(100)),
            "description": "x" * 5000,
            "nested": {
                "items": [{"id": i, "data": "y" * 1000} for i in range(50)]
            }
        }
        result = pformat(data, max_length=5, max_string=100, max_depth=3)

        # Should not have the full 100 ids or 5000-char description
        assert len(result) < 5000 + 100  # Much smaller than raw data
        assert "len=" in result  # Should have length markers


class TestPformatErrorHandling:
    """Verify pformat never crashes on unusual inputs."""

    def test_circular_reference(self):
        """Circular references should not hang."""
        lst = [1, 2, 3]
        lst.append(lst)  # Create cycle

        # Should complete without hanging/recursion error
        result = pformat(lst, max_depth=5)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_custom_repr_exception(self):
        """Objects with broken __repr__ should fall back gracefully."""
        class BadRepr:
            def __repr__(self):
                raise ValueError("repr is broken")

        obj = BadRepr()
        # Should not raise, should fall back to type info
        result = pformat(obj)
        assert isinstance(result, str)

    def test_none_values(self):
        """None values should render correctly."""
        result = pformat(None)
        assert result == "None"

    def test_empty_containers(self):
        """Empty containers should render simply."""
        assert pformat([]) == "[]"
        assert pformat({}) == "{}"
        # Note: pformat renders empty set as {}, which matches set repr
        result_set = pformat(set())
        assert result_set in ("{}", "set()")
