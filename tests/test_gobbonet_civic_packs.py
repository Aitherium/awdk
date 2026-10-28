"""GobboNet's agent spec attaches the civic and shadowbroker packs, as OPTIONAL.

Optional is the whole contract. Both are AitherOS platform packs, so a stranger's
install does not have them: activation there must leave the agent running with the
packs simply absent, never fail the build. And NETWORK_EGRESS stays undeclared,
because the core loop is still network-free (check_pack_tool_surface reads that
same agent.yaml for the pack's capability contract).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

SPEC = Path(__file__).resolve().parents[1] / "adk" / "packs" / "gobbonet" / "agent.yaml"


def _spec() -> dict:
    return yaml.safe_load(SPEC.read_text(encoding="utf-8")) or {}


def test_civic_and_shadowbroker_are_optional_never_required():
    packs = (_spec().get("tools") or {}).get("packs") or {}
    assert {"civic", "shadowbroker"} <= set(packs.get("optional") or [])
    # A required platform pack would fail every stranger's install at build time.
    assert not packs.get("required")


def test_network_egress_stays_undeclared():
    doc = _spec()
    granted = set(doc.get("capabilities") or []) | set(doc.get("optional_capabilities") or [])
    assert "NETWORK_EGRESS" not in granted


def test_absent_platform_packs_leave_the_agent_running(monkeypatch):
    # A stranger's install: the loader finds neither pack and registers nothing.
    import adk.builtin_tools as bt
    from adk.packs import PackActivator

    monkeypatch.setattr(bt, "register_tool_packs", lambda agent, pack_ids=None, packs_dir=None: 0)
    agent = SimpleNamespace(name="gobbonet", _tools=SimpleNamespace(list_tools=lambda: []))
    assert PackActivator(agent).ensure(_spec()["tools"]["packs"]) == {}
