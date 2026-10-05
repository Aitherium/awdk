"""The shell deployer has no Ollama orchestrator path.

It used to `ollama pull nemotron-orchestrator:8b-q4_K_M` into a container its own
compose never created, so the pull always failed and the deploy reported success
with a warning. The orchestrator is the v18 Q8_0 GGUF on llama.cpp; the two Ollama
profiles now fail loudly and point there.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from adk.shell import deployer


def test_no_ollama_orchestrator_tag_or_pull_remains():
    src = inspect.getsource(deployer)
    assert "nemotron-orchestrator:8b-q4_K_M" not in src
    assert not hasattr(deployer.Deployer, "pull_ollama_model")


@pytest.mark.parametrize("profile", deployer.REMOVED_OLLAMA_PROFILES)
def test_ollama_profiles_fail_loudly_with_the_llamacpp_pointer(profile):
    d = deployer.Deployer()
    d.get_profile(profile)  # still resolvable, so the error is the pointer, not "unknown"
    result = asyncio.run(d.deploy(profile, dry_run=True))
    assert result["status"] == "failed"
    assert any("quickstart-local --backend llamacpp" in e for e in result["errors"])
