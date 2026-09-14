"""Differential test between ADK and platform provenance schemas.

The ADK's selfgraph.schema is a mirror of AitherOS/lib/graph/provenance/schema.py.
A mirror without a differential test is just a fork nobody has noticed yet. This
suite asserts that enum members, hashing algorithms, and SCHEMA_VERSION stay in
byte-level lockstep.

If the platform tree is absent (ADK installed standalone from PyPI), the test suite
is SKIPPED via pytest.mark.skipif at collection time (the repo rule forbids
pytest.skip() inside test bodies).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from adk.selfgraph.schema import (
    SCHEMA_VERSION,
    ProvEdgeType,
    ProvNodeType,
    make_edge_id,
    make_node_id,
)


def _load_platform_schema():
    """Load the platform schema module by file path.

    Returns None if the file doesn't exist (e.g., ADK installed from PyPI).
    """
    # Walk up from the adk package to the repo root
    adk_root = Path(__file__).parent.parent
    repo_root = adk_root.parent
    platform_schema_path = repo_root / "AitherOS" / "lib" / "graph" / "provenance" / "schema.py"

    if not platform_schema_path.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        "platform_provenance_schema",
        platform_schema_path,
    )
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules["platform_provenance_schema"] = module
    spec.loader.exec_module(module)
    return module


_platform_schema = _load_platform_schema()
_has_platform = _platform_schema is not None

pytestmark = pytest.mark.skipif(
    not _has_platform,
    reason="Platform schema not found (expected for PyPI-installed ADK)",
)


class TestNodeTypeEnumParity:
    """Assert ProvNodeType members match exactly between ADK and platform."""

    def test_node_type_members_match(self):
        """Every member name and value must match exactly."""
        adk_members = {name: member.value for name, member in ProvNodeType.__members__.items()}
        platform_members = {
            name: member.value
            for name, member in _platform_schema.ProvNodeType.__members__.items()
        }

        assert adk_members == platform_members, (
            f"ProvNodeType members differ:\n"
            f"ADK:      {sorted(adk_members.items())}\n"
            f"Platform: {sorted(platform_members.items())}"
        )

    def test_node_type_no_missing_in_adk(self):
        """No member in platform should be missing from ADK."""
        adk_names = set(ProvNodeType.__members__.keys())
        platform_names = set(_platform_schema.ProvNodeType.__members__.keys())
        missing = platform_names - adk_names

        assert not missing, f"ADK missing node types: {missing}"

    def test_node_type_no_extra_in_adk(self):
        """No member in ADK should be absent from platform."""
        adk_names = set(ProvNodeType.__members__.keys())
        platform_names = set(_platform_schema.ProvNodeType.__members__.keys())
        extra = adk_names - platform_names

        assert not extra, f"ADK has unexpected node types: {extra}"


class TestEdgeTypeEnumParity:
    """Assert ProvEdgeType members match exactly between ADK and platform."""

    def test_edge_type_members_match(self):
        """Every member name and value must match exactly."""
        adk_members = {name: member.value for name, member in ProvEdgeType.__members__.items()}
        platform_members = {
            name: member.value
            for name, member in _platform_schema.ProvEdgeType.__members__.items()
        }

        assert adk_members == platform_members, (
            f"ProvEdgeType members differ:\n"
            f"ADK:      {sorted(adk_members.items())}\n"
            f"Platform: {sorted(platform_members.items())}"
        )

    def test_edge_type_no_missing_in_adk(self):
        """No member in platform should be missing from ADK."""
        adk_names = set(ProvEdgeType.__members__.keys())
        platform_names = set(_platform_schema.ProvEdgeType.__members__.keys())
        missing = platform_names - adk_names

        assert not missing, f"ADK missing edge types: {missing}"

    def test_edge_type_no_extra_in_adk(self):
        """No member in ADK should be absent from platform."""
        adk_names = set(ProvEdgeType.__members__.keys())
        platform_names = set(_platform_schema.ProvEdgeType.__members__.keys())
        extra = adk_names - platform_names

        assert not extra, f"ADK has unexpected edge types: {extra}"


class TestSchemaVersionParity:
    """Assert SCHEMA_VERSION matches."""

    def test_schema_version_matches(self):
        """SCHEMA_VERSION must be identical."""
        platform_version = _platform_schema.SCHEMA_VERSION
        assert SCHEMA_VERSION == platform_version, (
            f"SCHEMA_VERSION mismatch: ADK={SCHEMA_VERSION}, Platform={platform_version}"
        )


class TestNodeIdParity:
    """Assert make_node_id produces identical results in both implementations."""

    @pytest.mark.parametrize(
        "node_type_name,name,tenant_id",
        [
            # Basic cases
            ("CLAIM", "the sky is blue", ""),
            ("ARTIFACT", "report.pdf", ""),
            ("ENTITY", "Alice", ""),
            # With tenant scope
            ("CLAIM", "the sky is blue", "tenant1"),
            ("ARTIFACT", "report.pdf", "acme-corp"),
            # Unicode
            ("ENTITY", "François", ""),
            ("ENTITY", "François", "tenant1"),
            # Empty name (should default to "unnamed")
            ("CLAIM", "", ""),
            # Punctuation and special chars (should be slugified)
            ("ENTITY", "John Doe-Smith, Jr.", ""),
            ("SOURCE", "https://example.com/path?query=value", ""),
            # Mixed case (should be lowercased)
            ("OBJECTIVE", "FIND THE ANSWER", ""),
            # Whitespace (should be stripped)
            ("CLAIM", "  spaces around  ", ""),
        ],
    )
    def test_node_id_byte_identical(self, node_type_name, name, tenant_id):
        """Produce identical node ids for the same inputs."""
        adk_node_type = getattr(ProvNodeType, node_type_name)
        platform_node_type = getattr(_platform_schema.ProvNodeType, node_type_name)

        adk_id = make_node_id(adk_node_type, name, tenant_id)
        platform_id = _platform_schema.make_node_id(platform_node_type, name, tenant_id)

        assert adk_id == platform_id, (
            f"Node id mismatch for ({node_type_name}, {name!r}, {tenant_id!r}):\n"
            f"ADK:      {adk_id}\n"
            f"Platform: {platform_id}"
        )


class TestEdgeIdParity:
    """Assert make_edge_id produces identical results in both implementations."""

    @pytest.mark.parametrize(
        "source_id,edge_type_name,target_id,source_doc",
        [
            # Basic case
            ("claim:x:abc123", "CITES", "source:y:def456", ""),
            # With source_doc
            ("claim:x:abc123", "CITES", "source:y:def456", "https://example.com/doc"),
            # Different edge types
            ("run:r1:abc", "PRODUCED", "artifact:a1:def", ""),
            ("artifact:a1:x", "REVISES", "artifact:a0:y", ""),
            ("claim:c1:x", "DERIVED_FROM", "claim:c0:y", ""),
            # URL as source_doc
            ("claim:x:123", "SUPPORTS", "claim:y:456", "file:///home/user/notes.txt"),
            # Empty source_doc (most common)
            ("claim:a:1", "ABOUT", "entity:b:2", ""),
            ("plan:p:x", "DEPENDS_ON", "plan:q:y", ""),
        ],
    )
    def test_edge_id_byte_identical(self, source_id, edge_type_name, target_id, source_doc):
        """Produce identical edge ids for the same inputs."""
        adk_edge_type = getattr(ProvEdgeType, edge_type_name)
        platform_edge_type = getattr(_platform_schema.ProvEdgeType, edge_type_name)

        adk_id = make_edge_id(source_id, adk_edge_type, target_id, source_doc)
        platform_id = _platform_schema.make_edge_id(
            source_id, platform_edge_type, target_id, source_doc
        )

        assert adk_id == platform_id, (
            f"Edge id mismatch for ({source_id!r}, {edge_type_name}, {target_id!r}, {source_doc!r}):\n"
            f"ADK:      {adk_id}\n"
            f"Platform: {platform_id}"
        )

    def test_edge_id_empty_source_doc_matches_missing_source_doc(self):
        """Edge ids with empty string source_doc must match omitted source_doc."""
        source_id = "claim:x:abc"
        edge_type = ProvEdgeType.CITES
        target_id = "source:y:def"

        # Call without source_doc (defaults to "")
        id_default = make_edge_id(source_id, edge_type, target_id)
        # Call with explicit empty string
        id_empty = make_edge_id(source_id, edge_type, target_id, "")

        assert id_default == id_empty, "Empty source_doc and missing source_doc should produce same id"

        # Also verify against platform
        platform_edge_type = _platform_schema.ProvEdgeType.CITES
        platform_id = _platform_schema.make_edge_id(source_id, platform_edge_type, target_id)
        assert id_default == platform_id, "Default edge id should match platform"


class TestDataClassParity:
    """Assert ProvNode and ProvEdge match in structure and serialization."""

    def test_prov_node_to_dict_keys_match(self):
        """ProvNode.to_dict() must have the same keys as platform."""
        from adk.selfgraph.schema import ProvNode
        from adk.selfgraph.schema import make_node_id as adk_make_node_id
        from adk.selfgraph.schema import ProvNodeType as adk_node_type

        adk_node = ProvNode(
            id=adk_make_node_id(adk_node_type.CLAIM, "test"),
            node_type=adk_node_type.CLAIM,
            name="test",
            tenant_id="t1",
        )
        adk_dict = adk_node.to_dict()

        platform_node_cls = _platform_schema.ProvNode
        platform_make_node_id = _platform_schema.make_node_id
        platform_node_type = _platform_schema.ProvNodeType
        platform_node = platform_node_cls(
            id=platform_make_node_id(platform_node_type.CLAIM, "test"),
            node_type=platform_node_type.CLAIM,
            name="test",
            tenant_id="t1",
        )
        platform_dict = platform_node.to_dict()

        assert set(adk_dict.keys()) == set(platform_dict.keys()), (
            f"ProvNode.to_dict() keys differ:\n"
            f"ADK:      {sorted(adk_dict.keys())}\n"
            f"Platform: {sorted(platform_dict.keys())}"
        )

    def test_prov_edge_to_dict_keys_match(self):
        """ProvEdge.to_dict() must have the same keys as platform."""
        from adk.selfgraph.schema import ProvEdge

        adk_edge = ProvEdge(
            source_id="claim:x:1",
            target_id="source:y:2",
            edge_type=ProvEdgeType.CITES,
        )
        adk_dict = adk_edge.to_dict()

        platform_edge_cls = _platform_schema.ProvEdge
        platform_edge_type = _platform_schema.ProvEdgeType
        platform_edge = platform_edge_cls(
            source_id="claim:x:1",
            target_id="source:y:2",
            edge_type=platform_edge_type.CITES,
        )
        platform_dict = platform_edge.to_dict()

        assert set(adk_dict.keys()) == set(platform_dict.keys()), (
            f"ProvEdge.to_dict() keys differ:\n"
            f"ADK:      {sorted(adk_dict.keys())}\n"
            f"Platform: {sorted(platform_dict.keys())}"
        )


class TestRoundTripParity:
    """Assert from_dict(to_dict()) round-trip is lossless and matches platform."""

    def test_prov_node_round_trip_parity(self):
        """ProvNode serialization must be lossless and match platform."""
        from adk.selfgraph.schema import ProvNode
        from adk.selfgraph.schema import make_node_id as adk_make_node_id
        from adk.selfgraph.schema import ProvNodeType as adk_node_type

        original = ProvNode(
            id=adk_make_node_id(adk_node_type.CLAIM, "test claim"),
            node_type=adk_node_type.CLAIM,
            name="test claim",
            tenant_id="tenant1",
            workspace_id="ws1",
            properties={"confidence": 0.95, "inference": False},
            run_id="run1",
            agent_id="agent1",
            version=2,
            superseded_by="claim:superseded:xyz",
        )

        # Round trip through ADK
        dict_form = original.to_dict()
        restored = ProvNode.from_dict(dict_form)

        assert restored == original, "ProvNode round-trip should be lossless"

        # Verify keys match platform format
        platform_node_cls = _platform_schema.ProvNode
        platform_restored = platform_node_cls.from_dict(dict_form)
        platform_dict = platform_restored.to_dict()

        assert dict_form == platform_dict, "Serialization format must match platform"

    def test_prov_edge_round_trip_parity(self):
        """ProvEdge serialization must be lossless and match platform."""
        from adk.selfgraph.schema import ProvEdge

        original = ProvEdge(
            source_id="claim:x:abc",
            target_id="source:y:def",
            edge_type=ProvEdgeType.CITES,
            tenant_id="tenant1",
            workspace_id="ws1",
            run_id="run1",
            agent_id="agent1",
            source_doc="https://example.com/doc",
            confidence=0.9,
            properties={"verified": True},
        )

        # Round trip through ADK
        dict_form = original.to_dict()
        restored = ProvEdge.from_dict(dict_form)

        assert restored == original, "ProvEdge round-trip should be lossless"

        # Verify keys match platform format
        platform_edge_cls = _platform_schema.ProvEdge
        platform_restored = platform_edge_cls.from_dict(dict_form)
        platform_dict = platform_restored.to_dict()

        assert dict_form == platform_dict, "Serialization format must match platform"
