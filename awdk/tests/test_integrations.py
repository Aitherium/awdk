"""Tests for aw* family integrations."""

import pytest
from adk.integrations import capabilities, require_capability


def test_capabilities_report():
    """Test that capabilities() returns the expected structure."""
    caps = capabilities()
    
    # Structure validation
    assert "installed" in caps
    assert "missing" in caps
    assert "extras_to_install" in caps
    assert "suggest" in caps
    
    # Types
    assert isinstance(caps["installed"], list)
    assert isinstance(caps["missing"], list)
    assert isinstance(caps["extras_to_install"], list)
    
    # Content of each installed capability
    for pkg in caps["installed"]:
        assert "name" in pkg
        assert "module" in pkg
        assert "description" in pkg
        assert "unlocks" in pkg
        assert isinstance(pkg["unlocks"], list)


def test_hard_dependencies_always_present():
    """Test that hard aw* dependencies are always reported as installed."""
    caps = capabilities()
    installed_names = {p["name"] for p in caps["installed"]}
    
    # Hard deps from pyproject.toml
    assert "awgraph" in installed_names
    assert "awgit" in installed_names
    assert "awrelay" in installed_names


def test_require_capability_existing():
    """Test that require_capability accepts existing capabilities."""
    caps = capabilities()
    for pkg in caps["installed"]:
        # Should not raise
        require_capability(pkg["name"])


def test_require_capability_missing():
    """Test that require_capability raises on missing optional packages."""
    caps = capabilities()
    if caps["missing"]:
        missing_name = caps["missing"][0]["name"]
        with pytest.raises(ImportError):
            require_capability(missing_name)


@pytest.mark.skipif(
    not pytest.importorskip("awm", minversion=None),
    reason="awm not installed",
)
def test_optional_package_available_if_installed():
    """Test that optional packages are reported if actually installed."""
    caps = capabilities()
    installed_names = {p["name"] for p in caps["installed"]}
    
    # If awm is installed, it should be reported
    try:
        import awm  # noqa: F401
        assert "awm" in installed_names
    except ImportError:
        # If not installed, it should be in missing
        missing_names = {p["name"] for p in caps["missing"]}
        assert "awm" in missing_names
