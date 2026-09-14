"""Unit tests for site-ingest tools (extract_brand_tokens, save_sitespec)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from engine.tools import SiteIngestSession, build_ingest_tools


def test_extract_brand_tokens_empty_workspace():
    """Test extract_brand_tokens with an empty workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session = SiteIngestSession(workspace_dir=Path(tmpdir))
        tools = build_ingest_tools(session)
        extract_fn = next((t for t in tools if t.__name__ == 'extract_brand_tokens'), None)
        assert extract_fn is not None

        result_json = extract_fn(tmpdir)
        result = json.loads(result_json)

        # Should return token structure even if empty
        assert "tokens" in result
        assert result["tokens"]["company_name"] is None
        assert result["tokens"]["nap"]["phone"] is None


def test_extract_brand_tokens_with_markdown():
    """Test extract_brand_tokens with actual markdown content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        mirrors = workspace / "mirrors" / "example.com"
        mirrors.mkdir(parents=True, exist_ok=True)

        # Create a sample markdown file with brand info
        sample_md = """---
url: https://example.com
title: Example Corp
---

# Example Corporation

Your innovative tech partner.

## Contact Us

123 Main Street, San Francisco, CA 94102
Phone: (555) 123-4567

Follow us on [LinkedIn](https://linkedin.com/company/example-corp)
"""
        (mirrors / "index.md").write_text(sample_md, encoding="utf-8")

        session = SiteIngestSession(workspace_dir=workspace)
        tools = build_ingest_tools(session)
        extract_fn = next((t for t in tools if t.__name__ == 'extract_brand_tokens'), None)
        assert extract_fn is not None

        result_json = extract_fn(str(workspace))
        result = json.loads(result_json)

        assert result["extracted"] is True
        tokens = result["tokens"]
        assert tokens["company_name"] == "Example Corporation"
        assert "555" in (tokens["nap"]["phone"] or "")
        assert "123" in (tokens["nap"]["address"] or "")


def test_save_sitespec_valid():
    """Test save_sitespec with a valid SiteSpec."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        spec = {
            "slug": "example-site",
            "source_url": "https://example.com",
            "captured_at": "2026-01-15T10:30:00Z",
            "content_inventory": [
                {
                    "path": "/",
                    "title": "Home",
                    "headings": ["Example Corp"],
                    "text_md": "Welcome to our site.",
                    "assets": [],
                }
            ],
            "ia": {"pages": [{"path": "/", "title": "Home", "purpose": "home"}]},
            "brand": {
                "company_name": "Example Corp",
                "tagline": None,
                "tone": "professional",
                "accent_color": "#FF5733",
                "palette": {"primary": "#FF5733", "secondary": None, "accent": None},
                "typography": {"headings": None, "body": None},
                "logo_url": None,
            },
            "seo": {
                "title": "Example Corp | Home",
                "description": "Welcome to Example Corp",
                "keywords": ["example", "corp"],
                "nap": {
                    "name": "Example Corp",
                    "address": "123 Main St, SF, CA 94102",
                    "phone": "+1-555-123-4567",
                },
                "service_areas": [],
                "social_links": {},
                "jsonld_hints": {},
            },
            "integrations_hints": [],
        }

        session = SiteIngestSession(workspace_dir=workspace)
        tools = build_ingest_tools(session)
        save_fn = next((t for t in tools if t.__name__ == 'save_sitespec'), None)
        assert save_fn is not None

        result_json = save_fn(json.dumps(spec), str(workspace))
        result = json.loads(result_json)

        assert result["saved"] is True
        assert "sitespec.json" in result["path"]
        assert result["slug"] == "example-site"

        # Verify file was written
        spec_path = workspace / "sitespec.json"
        assert spec_path.exists()
        saved_spec = json.loads(spec_path.read_text(encoding="utf-8"))
        assert saved_spec["slug"] == "example-site"


def test_save_sitespec_invalid_json():
    """Test save_sitespec with invalid JSON."""
    with tempfile.TemporaryDirectory() as tmpdir:
        session = SiteIngestSession(workspace_dir=Path(tmpdir))
        tools = build_ingest_tools(session)
        save_fn = next((t for t in tools if t.__name__ == 'save_sitespec'), None)
        assert save_fn is not None

        result_json = save_fn("not valid json", tmpdir)
        result = json.loads(result_json)

        assert result["saved"] is False
        assert "invalid JSON" in result["error"]


def test_save_sitespec_missing_required():
    """Test save_sitespec with missing required fields."""
    with tempfile.TemporaryDirectory() as tmpdir:
        spec = {
            "slug": "example-site",
            # missing 'source_url' and 'captured_at'
        }

        session = SiteIngestSession(workspace_dir=Path(tmpdir))
        tools = build_ingest_tools(session)
        save_fn = next((t for t in tools if t.__name__ == 'save_sitespec'), None)
        assert save_fn is not None

        result_json = save_fn(json.dumps(spec), tmpdir)
        result = json.loads(result_json)

        assert result["saved"] is False
        assert "missing required fields" in result["error"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
