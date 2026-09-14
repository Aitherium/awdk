"""Tests for AitherOS framework connect templates."""

import pytest
import re
from adk.connect import render_connect, SUPPORTED_FRAMEWORKS


class TestRenderConnect:
    """Test framework connection template rendering."""

    @pytest.fixture
    def test_params(self):
        """Standard test parameters."""
        return {
            "gateway_url": "http://localhost:8001",
            "mcp_url": "http://localhost:8182/mcp",
            "api_key": "sk-test-key-12345",
        }

    def test_render_hermes(self, test_params):
        """Test Hermes template rendering."""
        result = render_connect("hermes", **test_params)

        # Assert output contains substituted values
        assert test_params["gateway_url"] in result
        assert test_params["mcp_url"] in result
        assert test_params["api_key"] in result

        # Assert YAML structure is valid
        assert "models:" in result
        assert "mcp_servers:" in result
        assert "aitheros" in result

        # Assert no leftover placeholders
        assert not re.search(r"\{[a-z_]+\}", result)

    def test_render_deer_flow(self, test_params):
        """Test Deer Flow template rendering."""
        result = render_connect("deer_flow", **test_params)

        # Assert output contains substituted values
        assert test_params["gateway_url"] in result
        assert test_params["mcp_url"] in result
        assert test_params["api_key"] in result

        # Assert YAML structure is valid
        assert "models:" in result
        assert "extensions_config:" in result
        assert "api_base" in result

        # Assert no leftover placeholders
        assert not re.search(r"\{[a-z_]+\}", result)

    def test_render_nooa(self, test_params):
        """Test Nooa template rendering."""
        result = render_connect("nooa", **test_params)

        # Assert output contains substituted values
        assert test_params["gateway_url"] in result
        assert test_params["mcp_url"] in result
        assert test_params["api_key"] in result

        # Assert YAML structure is valid
        assert "litellm:" in result
        assert "mcp:" in result
        assert "base_url" in result

        # Assert no leftover placeholders
        assert not re.search(r"\{[a-z_]+\}", result)

    def test_render_openclaw(self, test_params):
        """Test Openclaw template rendering."""
        result = render_connect("openclaw", **test_params)

        # Assert output contains substituted values
        assert test_params["gateway_url"] in result
        assert test_params["mcp_url"] in result
        assert test_params["api_key"] in result

        # Assert YAML structure is valid
        assert "provider:" in result
        assert "tools:" in result
        assert "mcp:" in result

        # Assert no leftover placeholders
        assert not re.search(r"\{[a-z_]+\}", result)

    def test_unknown_framework_raises(self, test_params):
        """Test that unknown framework raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported framework"):
            render_connect("unknown_framework", **test_params)

    def test_all_supported_frameworks(self, test_params):
        """Test that all supported frameworks render without error."""
        for framework in SUPPORTED_FRAMEWORKS:
            result = render_connect(framework, **test_params)
            assert isinstance(result, str)
            assert len(result) > 0
            # All should contain the values
            assert test_params["gateway_url"] in result
            assert test_params["mcp_url"] in result
            assert test_params["api_key"] in result

    def test_no_leftover_braces_all_frameworks(self, test_params):
        """Verify no leftover placeholder braces in any framework."""
        for framework in SUPPORTED_FRAMEWORKS:
            result = render_connect(framework, **test_params)
            # Check for unsubstituted placeholders like {gateway_url}
            placeholders = re.findall(r"\{[a-z_]+\}", result)
            assert not placeholders, (
                f"Found unsubstituted placeholders in {framework}: {placeholders}"
            )

    def test_render_with_different_urls(self, test_params):
        """Test rendering with different gateway and MCP URLs."""
        gateway_url = "https://gateway.example.com"
        mcp_url = "https://mcp.example.com/v1"
        api_key = "sk-example-key"

        for framework in SUPPORTED_FRAMEWORKS:
            result = render_connect(framework, gateway_url, mcp_url, api_key)
            assert gateway_url in result
            assert mcp_url in result
            assert api_key in result

    def test_render_with_complex_api_key(self, test_params):
        """Test rendering with complex API key containing special characters."""
        complex_key = "sk-proj-abc123!@#$%xyz789==/"
        result = render_connect(
            "hermes",
            gateway_url=test_params["gateway_url"],
            mcp_url=test_params["mcp_url"],
            api_key=complex_key,
        )
        assert complex_key in result

    def test_output_is_valid_yaml_structure(self, test_params):
        """Test that output is valid YAML structure (basic check)."""
        for framework in SUPPORTED_FRAMEWORKS:
            result = render_connect(framework, **test_params)
            # Check for basic YAML structure markers
            assert ":" in result  # Has key: value pairs
            # Should not have unbalanced braces
            assert result.count("{") == result.count("}")
