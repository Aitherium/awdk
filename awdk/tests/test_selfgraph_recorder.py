"""Tests for the user-facing recording API for provenance updates."""

import os
from pathlib import Path

import pytest

from adk.selfgraph.recorder import (
    RunRecorder,
    current_run,
    new_run_id,
    record_artifact,
    record_claim,
    record_open_question,
    record_run,
    record_tool_call,
)
from adk.selfgraph.schema import (
    ProvEdgeType,
    ProvNodeType,
    check_invariants,
)
from adk.selfgraph.spool import Spool


@pytest.fixture
def spool_db(tmp_path):
    """Create a Spool instance with a temporary database."""
    db_path = tmp_path / "test_spool.db"
    spool = Spool(db_path=db_path)
    yield spool
    spool.close()


@pytest.fixture
def temp_spool_env(tmp_path, monkeypatch):
    """Set AITHER_SELFGRAPH_DB to a temporary path."""
    db_path = tmp_path / "spool.db"
    monkeypatch.setenv("AITHER_SELFGRAPH_DB", str(db_path))
    yield db_path


class TestRunIdGeneration:
    """Test run ID generation."""

    def test_new_run_id_generates_unique_ids(self):
        """new_run_id() should generate unique ids."""
        id1 = new_run_id()
        id2 = new_run_id()
        assert id1 != id2, "Should generate different ids"

    def test_new_run_id_has_prefix(self):
        """new_run_id() should have the given prefix."""
        run_id = new_run_id(prefix="test")
        assert run_id.startswith("test-"), "Should start with prefix"

    def test_new_run_id_default_prefix(self):
        """new_run_id() should use 'run' as default prefix."""
        run_id = new_run_id()
        assert run_id.startswith("run-"), "Should use 'run' prefix by default"


class TestCurrentRun:
    """Test current_run() function."""

    def test_current_run_returns_none_outside_context(self):
        """current_run() should return None outside a record_run() context."""
        run = current_run()
        assert run is None, "Should return None when no active run"

    def test_current_run_returns_recorder_inside_context(self):
        """current_run() should return the active RunRecorder inside context."""
        with record_run(agent_id="agent1") as recorder:
            run = current_run()
            assert run is recorder, "Should return the active recorder"


class TestRunRecorderInitialization:
    """Test RunRecorder initialization."""

    def test_run_recorder_creates_run_node(self, spool_db):
        """RunRecorder.__init__ should create the RUN node."""
        recorder = RunRecorder(
            run_id="run1",
            agent_id="agent1",
            spool=spool_db,
        )
        assert recorder.run_id == "run1"
        assert len(recorder._nodes) >= 1, "Should have created at least RUN node"
        # RUN node should be first
        assert recorder._nodes[0].node_type == ProvNodeType.RUN

    def test_run_recorder_creates_agent_node(self, spool_db):
        """RunRecorder.__init__ should create an AGENT node."""
        recorder = RunRecorder(
            agent_id="agent1",
            spool=spool_db,
        )
        agent_nodes = [n for n in recorder._nodes if n.node_type == ProvNodeType.AGENT]
        assert len(agent_nodes) >= 1, "Should have created AGENT node"

    def test_run_recorder_creates_objective_node_if_given(self, spool_db):
        """RunRecorder.__init__ should create OBJECTIVE node if objective is given."""
        recorder = RunRecorder(
            agent_id="agent1",
            objective="find the answer",
            spool=spool_db,
        )
        objective_nodes = [n for n in recorder._nodes if n.node_type == ProvNodeType.OBJECTIVE]
        assert len(objective_nodes) == 1, "Should have created OBJECTIVE node"
        assert objective_nodes[0].name == "find the answer"

    def test_run_recorder_autoflush_threshold(self, spool_db):
        """RunRecorder should respect autoflush threshold."""
        recorder = RunRecorder(
            agent_id="agent1",
            autoflush=2,
            spool=spool_db,
        )
        # Add claims to trigger autoflush
        recorder.claim("claim 1", sources=["http://example.com"])
        # After autoflush triggered, buffer should be empty
        # The claim itself should be flushed
        stats = spool_db.stats()
        assert stats["pending"] >= 1, "Should have enqueued at least one update"


class TestClaimRecording:
    """Test claim() recording method."""

    def test_claim_requires_sources_or_inference(self, spool_db):
        """claim() should require either sources or inference=True."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        with pytest.raises(ValueError, match="sources and is not marked inference"):
            recorder.claim("unsourced claim")

    def test_claim_with_sources(self, spool_db):
        """claim() should create CLAIM node and SOURCE nodes."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        claim = recorder.claim(
            "the sky is blue",
            sources=["https://example.com/sky"],
        )

        assert claim.node_type == ProvNodeType.CLAIM
        assert "the sky is blue" in claim.name
        # Should have created CLAIM and SOURCE nodes
        assert len(recorder._nodes) >= 3  # RUN, AGENT, CLAIM, SOURCE

    def test_claim_creates_cites_edge(self, spool_db):
        """claim() should create CITES edges to sources."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        claim = recorder.claim(
            "test claim",
            sources=["https://example.com"],
        )

        # Should have CITES edges
        cites_edges = [e for e in recorder._edges if e.edge_type == ProvEdgeType.CITES]
        assert len(cites_edges) >= 1, "Should have created CITES edge"

    def test_claim_with_inference_requires_derived_from(self, spool_db):
        """claim() with inference=True should require derived_from."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        with pytest.raises(ValueError, match="derived_from is empty"):
            recorder.claim("inferred claim", inference=True)

    def test_claim_with_inference_and_derived_from(self, spool_db):
        """claim() with inference=True and derived_from should work."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        parent_claim = recorder.claim(
            "parent claim",
            sources=["https://example.com"],
        )

        derived_claim = recorder.claim(
            "derived claim",
            inference=True,
            derived_from=(parent_claim.id,),
        )

        assert derived_claim.properties["inference"] is True
        # Should have DERIVED_FROM edge
        derived_edges = [
            e for e in recorder._edges
            if e.edge_type == ProvEdgeType.DERIVED_FROM
        ]
        assert len(derived_edges) >= 1

    def test_claim_with_multiple_sources(self, spool_db):
        """claim() should handle multiple sources."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        claim = recorder.claim(
            "well-sourced claim",
            sources=("https://example1.com", "https://example2.com"),
        )

        # Should have 2 CITES edges
        cites_edges = [e for e in recorder._edges if e.edge_type == ProvEdgeType.CITES]
        assert len(cites_edges) >= 2, "Should have created 2 CITES edges"

    def test_claim_with_confidence(self, spool_db):
        """claim() should record confidence."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        claim = recorder.claim(
            "uncertain claim",
            sources=["https://example.com"],
            confidence=0.5,
        )

        assert claim.properties["confidence"] == 0.5

    def test_claim_auto_creates_source_nodes(self, spool_db):
        """claim() should auto-create SOURCE nodes for URL-like sources."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        claim = recorder.claim(
            "test",
            sources=["https://example.com/doc", "file:///path/to/file"],
        )

        source_nodes = [n for n in recorder._nodes if n.node_type == ProvNodeType.SOURCE]
        assert len(source_nodes) >= 2, "Should have created SOURCE nodes for URLs"


class TestArtifactRecording:
    """Test artifact() recording method."""

    def test_artifact_creates_artifact_node(self, spool_db):
        """artifact() should create an ARTIFACT node."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        artifact = recorder.artifact("report.pdf", version=2)

        assert artifact.node_type == ProvNodeType.ARTIFACT
        assert artifact.version == 2

    def test_artifact_with_properties(self, spool_db):
        """artifact() should record properties."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        artifact = recorder.artifact(
            "data.csv",
            version=1,
            content_hash="sha256:abc123",
            path="/data/export.csv",
        )

        assert artifact.properties["content_hash"] == "sha256:abc123"
        assert artifact.properties["path"] == "/data/export.csv"

    def test_artifact_with_revises(self, spool_db):
        """artifact() should create REVISES edge."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        artifact1 = recorder.artifact("report.pdf", version=1)
        artifact2 = recorder.artifact("report.pdf", version=2, revises=artifact1.id)

        revises_edges = [e for e in recorder._edges if e.edge_type == ProvEdgeType.REVISES]
        assert len(revises_edges) >= 1


class TestEvaluationRecording:
    """Test evaluation() recording method."""

    def test_evaluation_creates_evaluation_node(self, spool_db):
        """evaluation() should create an EVALUATION node."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        rubric = recorder.rubric("quality", criteria={"readability": "clear text"})
        claim = recorder.claim("test", sources=["https://example.com"])

        eval_node = recorder.evaluation(
            "pass",
            rubric_id=rubric.id,
            target_id=claim.id,
        )

        assert eval_node.node_type == ProvNodeType.EVALUATION
        assert eval_node.properties["rubric_id"] == rubric.id

    def test_evaluation_creates_evaluates_edge(self, spool_db):
        """evaluation() should create EVALUATES edge."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        rubric = recorder.rubric("quality", criteria={})
        claim = recorder.claim("test", sources=["https://example.com"])

        eval_node = recorder.evaluation("pass", rubric_id=rubric.id, target_id=claim.id)

        evaluates_edges = [e for e in recorder._edges if e.edge_type == ProvEdgeType.EVALUATES]
        assert len(evaluates_edges) >= 1


class TestExperimentRecording:
    """Test experiment() recording method."""

    def test_experiment_creates_experiment_node(self, spool_db):
        """experiment() should create an EXPERIMENT node."""
        recorder = RunRecorder(agent_id="agent1", spool=spool_db)

        exp = recorder.experiment(
            "test hypothesis",
            metric_name="accuracy",
            metric_value=0.95,
            status="complete",
        )

        assert exp.node_type == ProvNodeType.EXPERIMENT
        assert exp.properties["metric_name"] == "accuracy"
        assert exp.properties["metric_value"] == 0.95
        assert exp.properties["status"] == "complete"


class TestContextManager:
    """Test record_run() context manager."""

    def test_record_run_sets_current_run(self, temp_spool_env):
        """record_run() should set current_run() inside context."""
        assert current_run() is None, "No run outside context"

        with record_run(agent_id="agent1") as recorder:
            assert current_run() is recorder, "Should set current run"

        assert current_run() is None, "Should restore None after context"

    def test_record_run_flushes_on_exit(self, temp_spool_env):
        """record_run() should flush to spool on exit."""
        with record_run(agent_id="agent1") as recorder:
            recorder.claim("test", sources=["https://example.com"])

        # Check that spool has entries
        spool = Spool()
        stats = spool.stats()
        assert stats["pending"] >= 1, "Should have flushed to spool"
        spool.close()

    def test_record_run_marks_status_on_exception(self, temp_spool_env):
        """record_run() should mark status as failed on exception."""
        with pytest.raises(ValueError):
            with record_run(agent_id="agent1") as recorder:
                recorder.claim("test", sources=["https://example.com"])
                raise ValueError("test error")

        # The run should be marked as failed in the spool
        spool = Spool()
        pending = spool.pending(limit=100)
        # Should have at least one entry
        assert len(pending) >= 1
        # The RUN node should be marked as failed
        for entry in pending:
            update = entry.to_update()
            if update:
                for node in update.nodes:
                    if node.node_type == ProvNodeType.RUN:
                        assert node.properties.get("status") == "failed"
        spool.close()

    def test_record_run_re_raises_exception(self, temp_spool_env):
        """record_run() should re-raise exceptions, not swallow them."""
        with pytest.raises(ValueError, match="test error"):
            with record_run(agent_id="agent1"):
                raise ValueError("test error")

    def test_record_run_marks_status_complete_on_success(self, temp_spool_env):
        """record_run() should mark status as complete on normal exit."""
        with record_run(agent_id="agent1") as recorder:
            recorder.claim("test", sources=["https://example.com"])

        spool = Spool()
        pending = spool.pending(limit=100)
        for entry in pending:
            update = entry.to_update()
            if update:
                for node in update.nodes:
                    if node.node_type == ProvNodeType.RUN:
                        assert node.properties.get("status") == "complete"
        spool.close()


class TestFreeFormApiFunctions:
    """Test free-form recording functions (record_claim, etc)."""

    def test_record_claim_with_active_run(self, temp_spool_env):
        """record_claim() should record on active run."""
        with record_run(agent_id="agent1"):
            claim = record_claim("test claim", sources=["https://example.com"])
            assert claim is not None, "Should return claim node"
            assert claim.node_type == ProvNodeType.CLAIM

    def test_record_claim_without_active_run(self):
        """record_claim() should return None when no active run."""
        claim = record_claim("test")
        assert claim is None, "Should return None when no active run"

    def test_record_artifact_with_active_run(self, temp_spool_env):
        """record_artifact() should record on active run."""
        with record_run(agent_id="agent1"):
            artifact = record_artifact("output.txt")
            assert artifact is not None
            assert artifact.node_type == ProvNodeType.ARTIFACT

    def test_record_artifact_without_active_run(self):
        """record_artifact() should return None when no active run."""
        artifact = record_artifact("output.txt")
        assert artifact is None

    def test_record_tool_call_with_active_run(self, temp_spool_env):
        """record_tool_call() should record on active run."""
        with record_run(agent_id="agent1"):
            call = record_tool_call("search", ok=True, duration_ms=100)
            assert call is not None
            assert call.node_type == ProvNodeType.ARTIFACT

    def test_record_open_question_with_active_run(self, temp_spool_env):
        """record_open_question() should record on active run."""
        with record_run(agent_id="agent1"):
            question = record_open_question("Is this correct?")
            assert question is not None
            assert question.node_type == ProvNodeType.OPEN_QUESTION


class TestDisabledRecording:
    """Test recording with AITHER_SELFGRAPH=0."""

    def test_disabled_record_run_is_noop(self, monkeypatch, tmp_path):
        """AITHER_SELFGRAPH=0 should make record_run a no-op."""
        monkeypatch.setenv("AITHER_SELFGRAPH", "0")
        db_path = tmp_path / "spool.db"
        monkeypatch.setenv("AITHER_SELFGRAPH_DB", str(db_path))

        # Re-import to pick up the env var
        import importlib
        import adk.selfgraph.recorder as recorder_module
        importlib.reload(recorder_module)

        with recorder_module.record_run(agent_id="agent1"):
            record_claim = recorder_module.record_claim("test")
            assert record_claim is None, "Should be no-op when disabled"

        # Reset for other tests
        monkeypatch.setenv("AITHER_SELFGRAPH", "1")
        importlib.reload(recorder_module)

    def test_disabled_record_claim_is_noop(self, monkeypatch):
        """record_claim() should return None when disabled."""
        monkeypatch.setenv("AITHER_SELFGRAPH", "0")

        import importlib
        import adk.selfgraph.recorder as recorder_module
        importlib.reload(recorder_module)

        result = recorder_module.record_claim("test")
        assert result is None, "Should be no-op when disabled"

        monkeypatch.setenv("AITHER_SELFGRAPH", "1")
        importlib.reload(recorder_module)


class TestInvariantChecks:
    """Test that recorded runs pass check_invariants()."""

    def test_recorded_update_passes_invariants(self, spool_db):
        """A recorded update should pass check_invariants()."""
        with record_run(agent_id="agent1", spool=spool_db) as recorder:
            recorder.claim("test claim", sources=["https://example.com"])
            recorder.artifact("output.txt", version=1)

        # Get the recorded update
        spool_db2 = Spool(db_path=spool_db._db_path)
        pending = spool_db2.pending(limit=1)
        assert len(pending) >= 1

        update = pending[0].to_update()
        problems = check_invariants(update)

        # Should have no invariant violations
        assert not problems, f"Update has invariant violations: {problems}"
        spool_db2.close()

    def test_unsourced_claim_fails_invariants(self, spool_db):
        """An unsourced claim not marked inference should fail invariants."""
        # This should raise during recording, not just invariant check
        with record_run(agent_id="agent1", spool=spool_db) as recorder:
            with pytest.raises(ValueError):
                recorder.claim("unsourced")

    def test_evaluation_without_rubric_fails_invariants(self, spool_db):
        """An evaluation without rubric_id should fail invariants."""
        from adk.selfgraph.schema import GraphUpdate, ProvNode, ProvEdge

        # Manually create a malformed evaluation
        eval_node = ProvNode(
            id="evaluation:x:abc",
            node_type=ProvNodeType.EVALUATION,
            name="bad eval",
            run_id="run1",
            agent_id="agent1",
            properties={},  # Missing rubric_id
        )

        update = GraphUpdate(
            nodes=[eval_node],
            edges=[],
            run_id="run1",
            agent_id="agent1",
        )

        problems = check_invariants(update)
        assert len(problems) > 0, "Should find invariant violations"
