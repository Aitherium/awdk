"""The shell deployer has no pull-based orchestrator path.

It used to pull an upstream Q4 orchestrator tag into a container its own compose
never created, so the pull always failed and the deploy reported success with a
warning. The orchestrator is the v18 Q8_0 GGUF on llama.cpp; the removed profiles
now fail loudly and point there.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from adk.shell import deployer


def test_no_pull_orchestrator_tag_remains():
    src = inspect.getsource(deployer)
    assert "nemotron-orchestrator:8b-q4_K_M" not in src
    assert [n for n in vars(deployer.Deployer) if n.startswith("pull_")] == ["pull_images"]


@pytest.mark.parametrize("profile", deployer.REMOVED_ORCHESTRATOR_PROFILES)
def test_removed_profiles_fail_loudly_with_the_llamacpp_pointer(profile):
    d = deployer.Deployer()
    result = asyncio.run(d.deploy(profile, dry_run=True))
    assert result["status"] == "failed"
    assert any("quickstart-local --backend llamacpp" in e for e in result["errors"])
