"""The aither default pack must actually DRIVE the agent — persona + graphrag.

The sibling test_aither_default_pack.py builds agents with load_packs=False and never
inspects the system prompt, so it missed that the pack's persona wasn't wired into the
agent at all (the agent used its thin identity, not the pack's system_prompt). These
tests assert the REAL deliverable:
  1. Building the default aither agent applies the aither PACK's system_prompt.
  2. A specialized named agent is NOT hijacked by the aither pack.
  3. An explicit system_prompt still wins.
  4. GraphMemory initializes (regression guard for the `synced`-column crash that
     silently disabled graphrag on any pre-v4 DB).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_AITHER_PACK = Path(__file__).resolve().parents[1] / "adk" / "packs" / "aither" / "brain_pack.yaml"


@pytest.fixture(autouse=True)
def _fast_offline_env(monkeypatch):
    # Point the discovered brain pack at the bundled aither pack, disable the
    # embeddings docker-autodeploy (60s hang → CPU fallback), and neutralize any
    # dev-box private-companion vault that would override the persona under test.
    monkeypatch.setenv("AGENT_BRAIN_PACK", str(_AITHER_PACK))
    monkeypatch.setenv("AITHER_EMBED_AUTODEPLOY", "0")
    monkeypatch.setenv("AITHER_TYPED_MEMORY", "false")
    import adk.private_companion as pc
    monkeypatch.setattr(pc, "get_companion_vault", lambda *a, **k: None, raising=False)


def _agent(name, **kw):
    from adk.agent import AitherAgent
    return AitherAgent(name=name, load_packs=False, **kw)


def test_default_aither_agent_uses_pack_persona():
    a = _agent("aither", builtin_tools=True)
    sp = (a.system_prompt or "").lower()
    assert "system orchestrator" in sp and "synthesize" in sp, (
        f"aither agent must adopt the pack persona; got head: {sp[:100]!r}"
    )


def test_default_aither_agent_has_basic_tools():
    a = _agent("aither", builtin_tools=True)
    names = {t.name for t in a._tools.list_tools()}
    assert {"file_read", "file_write", "shell_exec", "web_search", "web_fetch"} <= names


def test_default_aither_agent_graphrag_active():
    # Uses the plain "aither" agent name → the SAME on-disk DB that hit the
    # `no such column: synced` crash. This guards that regression.
    a = _agent("aither", builtin_tools=False)
    assert a._graph is not None, "GraphMemory must initialize (synced-column regression)"


def test_specialized_agent_not_hijacked():
    h = _agent("hydra", builtin_tools=False)
    assert "system orchestrator" not in (h.system_prompt or "").lower(), (
        "a named specialist must NOT adopt the aither pack's persona"
    )


def test_explicit_system_prompt_wins():
    e = _agent("aither", system_prompt="EXPLICIT-PERSONA")
    assert e.system_prompt == "EXPLICIT-PERSONA"


@pytest.mark.asyncio
async def test_graphrag_roundtrip_on_default_agent():
    a = _agent("aither", builtin_tools=False)
    g = a._graph
    assert g is not None
    await g.remember("Aither Pack", "provides", "graphrag memory by default")
    res = await g.recall(subject="Aither Pack", relation="provides")
    assert any(r.get("object") == "graphrag memory by default" for r in res), res
