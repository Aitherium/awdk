"""Tests for adk.skills module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


def test_skill_dataclass():
    from adk.skills import Skill

    s = Skill(name="deploy", description="Deploy to production")
    assert s.name == "deploy"
    assert s.success_count == 0
    assert s.tags == []


def test_skill_store_save_load():
    from adk.skills import Skill, SkillStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SkillStore(data_dir=Path(tmpdir))
        skill = Skill(
            name="write-tests",
            description="Write pytest test files",
            steps=["Identify code to test", "Write test functions", "Run pytest"],
            tools_used=["write_file", "run_command"],
            tags=["testing", "python"],
        )
        store.save(skill)

        loaded = store.load("write-tests")
        assert loaded is not None
        assert loaded.name == "write-tests"
        assert loaded.description == "Write pytest test files"
        assert len(loaded.steps) == 3
        assert "testing" in loaded.tags


def test_skill_store_search():
    from adk.skills import Skill, SkillStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SkillStore(data_dir=Path(tmpdir))
        store.save(Skill(name="deploy-docker", description="Deploy with Docker containers", tags=["deploy"]))
        store.save(Skill(name="write-tests", description="Write unit tests", tags=["testing"]))
        store.save(Skill(name="deploy-k8s", description="Deploy to Kubernetes", tags=["deploy"]))

        results = store.search("deploy")
        assert len(results) == 2
        names = {s.name for s in results}
        assert "deploy-docker" in names
        assert "deploy-k8s" in names


def test_skill_store_list_all():
    from adk.skills import Skill, SkillStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SkillStore(data_dir=Path(tmpdir))
        store.save(Skill(name="a-skill", description="First"))
        store.save(Skill(name="b-skill", description="Second"))

        all_skills = store.list_all()
        assert len(all_skills) == 2


def test_skill_store_delete():
    from adk.skills import Skill, SkillStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SkillStore(data_dir=Path(tmpdir))
        store.save(Skill(name="temp-skill", description="Temporary"))
        assert store.delete("temp-skill") is True
        assert store.load("temp-skill") is None
        assert store.delete("nonexistent") is False


def test_skill_store_export():
    from adk.skills import Skill, SkillStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SkillStore(data_dir=Path(tmpdir))
        store.save(Skill(name="my-skill", description="A skill"))

        export = store.export_agentskills()
        assert export["version"] == "1.0"
        assert export["agent"] == "adk"
        assert len(export["skills"]) == 1
        assert export["skills"][0]["name"] == "my-skill"


def test_skill_extractor_no_tools():
    from adk.skills import SkillExtractor

    extractor = SkillExtractor()
    result = extractor.extract_from_session(
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ],
        tools_called=[],
    )
    # Simple chat without tools should return None
    assert result is None


def test_skill_extractor_with_tools():
    from adk.skills import SkillExtractor

    extractor = SkillExtractor()
    result = extractor.extract_from_session(
        messages=[
            {"role": "user", "content": "Read the config file and fix the port"},
            {"role": "assistant", "content": "I'll read the config and update the port."},
            {"role": "assistant", "content": "Done. Changed port from 8080 to 9000."},
        ],
        tools_called=["read_file", "replace_text", "read_file"],
    )
    assert result is not None
    assert len(result.tools_used) > 0
    assert result.steps  # Should have extracted some steps
