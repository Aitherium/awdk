"""Tests for ADK creative tools and creator tier mapping.

Validates:
- "creative" category exists in TOOL_CATEGORIES
- iris/muse identity defaults include creative
- _PLAN_TO_TIER maps creator correctly
- Creative tool functions return proper JSON on connection error
"""

import json
import os
import sys
import pytest

# Add adk to path
_adk_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _adk_root not in sys.path:
    sys.path.insert(0, _adk_root)


# ── Tool categories ──────────────────────────────────────────────────────

class TestToolCategories:
    """Verify creative category registration."""

    def test_creative_in_categories(self):
        from adk.builtin_tools import TOOL_CATEGORIES
        assert "creative" in TOOL_CATEGORIES

    def test_creative_has_three_tools(self):
        from adk.builtin_tools import TOOL_CATEGORIES
        assert len(TOOL_CATEGORIES["creative"]) == 3

    def test_creative_tool_names(self):
        from adk.builtin_tools import TOOL_CATEGORIES
        names = [fn.__name__ for fn in TOOL_CATEGORIES["creative"]]
        assert "image_generate" in names
        assert "image_refine" in names
        assert "image_smart" in names


# ── Identity defaults ────────────────────────────────────────────────────

class TestIdentityDefaults:
    """Verify creative identities get creative tools."""

    def test_iris_has_creative(self):
        from adk.builtin_tools import IDENTITY_DEFAULTS
        assert "creative" in IDENTITY_DEFAULTS["iris"]

    def test_muse_has_creative(self):
        from adk.builtin_tools import IDENTITY_DEFAULTS
        assert "creative" in IDENTITY_DEFAULTS["muse"]

    def test_aither_has_creative(self):
        from adk.builtin_tools import IDENTITY_DEFAULTS
        assert "creative" in IDENTITY_DEFAULTS["aither"]

    def test_demiurge_no_creative(self):
        from adk.builtin_tools import IDENTITY_DEFAULTS
        assert "creative" not in IDENTITY_DEFAULTS["demiurge"]

    def test_atlas_no_creative(self):
        from adk.builtin_tools import IDENTITY_DEFAULTS
        assert "creative" not in IDENTITY_DEFAULTS["atlas"]


# ── MCP plan-to-tier mapping ─────────────────────────────────────────────

class TestMCPPlanToTier:
    """Verify _PLAN_TO_TIER in ADK mcp.py."""

    def test_creator_maps_to_creator(self):
        from adk.mcp import _PLAN_TO_TIER
        assert _PLAN_TO_TIER["creator"] == "creator"

    def test_creator_pro_maps_to_creator_pro(self):
        from adk.mcp import _PLAN_TO_TIER
        assert _PLAN_TO_TIER["creator_pro"] == "creator_pro"

    def test_explorer_still_free(self):
        from adk.mcp import _PLAN_TO_TIER
        assert _PLAN_TO_TIER["explorer"] == "free"

    def test_builder_still_pro(self):
        from adk.mcp import _PLAN_TO_TIER
        assert _PLAN_TO_TIER["builder"] == "pro"

    def test_plan_to_tier_function(self):
        from adk.mcp import _plan_to_tier
        assert _plan_to_tier("creator") == "creator"
        assert _plan_to_tier("creator_pro") == "creator_pro"
        assert _plan_to_tier("unknown_plan") == "free"


# ── Creative tool error handling ──────────────────────────────────────────

class TestCreativeToolErrors:
    """Test creative tools return proper JSON on connection errors."""

    def test_image_generate_connection_error(self):
        # Point to a non-existent server to trigger ConnectError
        import adk.builtin_tools as bt
        original = bt._CANVAS_URL
        bt._CANVAS_URL = "http://127.0.0.1:19999"
        try:
            result = bt.image_generate(prompt="test prompt")
            data = json.loads(result)
            assert data["success"] is False
            assert "error" in data
        finally:
            bt._CANVAS_URL = original

    def test_image_refine_connection_error(self):
        import adk.builtin_tools as bt
        original = bt._CANVAS_URL
        bt._CANVAS_URL = "http://127.0.0.1:19999"
        try:
            result = bt.image_refine(image_path="/tmp/test.png", prompt="test")
            data = json.loads(result)
            assert data["success"] is False
            assert "error" in data
        finally:
            bt._CANVAS_URL = original

    def test_image_smart_connection_error(self):
        import adk.builtin_tools as bt
        original = bt._CANVAS_URL
        bt._CANVAS_URL = "http://127.0.0.1:19999"
        try:
            result = bt.image_smart(prompt="test diagram")
            data = json.loads(result)
            assert data["success"] is False
            assert "error" in data
        finally:
            bt._CANVAS_URL = original
