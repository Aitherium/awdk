"""build_agent_from_spec — construct an AitherAgent from a declarative spec.

Sequence (each step degrades gracefully so a partial/pure-adk env still boots):

  1. ``spec = pack_discovery.load_agent_spec(spec_path)``  (deep-merges agent.yaml.local)
  2. ``ident = load_identity(spec['identity'])``           when an identity name is given
  3. ``prompts = PromptBridge.resolve(spec['prompts'], base_dir=dirname(spec_path))``
  4. LLM roles: the adk ``LLMRouter`` has no role table (verified: its ``__init__`` takes
     provider/base_url/api_key/model/config/response_cache — no ``roles``). So roles are
     NOT wired into a router here; they are carried on ``RunCtx.roles`` for the caller to
     apply (e.g. per-node ``switch_backend``/``set_reasoning_backend``).
  5. ``agent = AitherAgent(name=..., identity=ident, system_prompt=prompts['system'],
     load_packs=False)``. NOTE (real-signature adaptation): the constructor's ``memory=``
     param is typed ``adk.memory.Memory`` (the KV/history store that ``chat()`` drives via
     ``add_message``/``get_history``). ``GraphMemory`` is a DIFFERENT class and passing it
     as ``memory=`` would break ``chat()``. The agent ALREADY instantiates
     ``GraphMemory(agent_name=name)`` internally as ``self._graph`` (see
     ``AitherAgent.__init__``), so graph memory is attached automatically without an
     override. We surface that graph on ``RunCtx.graph`` for convenience.
  6. ``PackActivator(agent).ensure(spec['tools']['packs'])``  (eager; required must fire)
  7. return ``(agent, RunCtx(...))``

Run standalone to inspect a spec without a live loop:
    python -m adk.bootstrap.generic_agent <spec.yaml>
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Tuple

from adk.packs import PackActivator
from adk.prompts import PromptBridge


@dataclass
class RunCtx:
    """Everything the run loop needs beyond the agent itself.

    Attributes:
        agent:      the constructed AitherAgent.
        memory_map: node_type -> [RoleName, TierName]. Consulted by ``stamp`` as an
                    OVERRIDE for ``add_node(role=, tier=)`` — never a gate/validator.
        prompts:    resolved prompt key -> text (from PromptBridge).
        budget:     loop budget dict from ``spec['loop']['budget']``.
        roles:      the LLM role table from ``spec['llm']['roles']`` (caller wires it; the
                    adk router has no native role concept).
        packs:      activated pack id -> tool count (filled by build_agent_from_spec).
        graph:      the agent's GraphMemory (``agent._graph``) when present, else None.
    """

    agent: Any
    memory_map: dict = field(default_factory=dict)
    prompts: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    packs: dict = field(default_factory=dict)
    graph: Any = None

    def stamp(self, node_type: str) -> Tuple[Optional[str], Optional[str]]:
        """Return the (role, tier) OVERRIDE for a memory node type.

        This is a pure lookup into ``memory_map``: the value is ``[RoleName, TierName]``.
        Returns ``(None, None)`` when the node type is absent. It is an override to feed
        into ``add_node(role=, tier=)`` — it NEVER gates, filters, or validates writes.
        """
        entry = self.memory_map.get(node_type)
        if not entry:
            return (None, None)
        role = entry[0] if len(entry) > 0 else None
        tier = entry[1] if len(entry) > 1 else None
        return (role, tier)


def build_agent_from_spec(spec_path):
    """Build an ``AitherAgent`` + ``RunCtx`` from a spec YAML.

    Args:
        spec_path: path to an ``agent.yaml``-style spec.

    Returns:
        (agent, RunCtx)
    """
    from adk.agent import AitherAgent

    spec_path = Path(spec_path)
    # load_agent_spec takes an explicit Path and deep-merges <path>.local when present.
    spec = pack_discovery_load(spec_path)
    if not spec:
        raise ValueError(
            f"spec at {spec_path} loaded empty (missing/invalid YAML). "
            f"load_agent_spec returned {{}}."
        )

    name = spec.get("name") or spec_path.stem

    # Identity (optional) — load_identity takes a NAME, not a path; falls back to a bare
    # Identity(name) when the YAML can't be found, so this never hard-fails.
    ident = None
    ident_ref = spec.get("identity")
    if ident_ref:
        try:
            from adk.identity import load_identity
            ident = load_identity(ident_ref)
        except Exception:
            ident = None

    # Prompts — resolve file refs relative to the spec's directory.
    prompts = PromptBridge.resolve(spec.get("prompts", {}) or {}, base_dir=spec_path.parent)

    # LLM roles carried for the caller (router has no native role table).
    roles = ((spec.get("llm") or {}).get("roles")) or {}

    agent = AitherAgent(
        name=name,
        identity=ident,
        system_prompt=prompts.get("system"),
        load_packs=False,
    )

    # Eagerly activate declared tool packs (required must register tools).
    packs_spec = ((spec.get("tools") or {}).get("packs")) or {}
    activated = PackActivator(agent).ensure(packs_spec)

    ctx = RunCtx(
        agent=agent,
        memory_map=spec.get("memory_map", {}) or {},
        prompts=prompts,
        budget=((spec.get("loop") or {}).get("budget")) or {},
        roles=roles,
        packs=activated,
        graph=getattr(agent, "_graph", None),
    )
    return agent, ctx


def pack_discovery_load(spec_path: Path) -> dict:
    """Thin wrapper over ``adk.pack_discovery.load_agent_spec`` (kept separate so the
    __main__ inspector and build path share exactly one loader)."""
    from adk import pack_discovery
    return pack_discovery.load_agent_spec(spec_path)


def _main(argv=None) -> int:
    """Load a spec and print what WOULD be built, without running a live loop."""
    import sys

    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m adk.bootstrap.generic_agent <spec.yaml>")
        return 2

    spec_path = Path(argv[0])
    agent, ctx = build_agent_from_spec(spec_path)

    print(f"agent name     : {agent.name}")
    if ctx.packs:
        for pid, cnt in ctx.packs.items():
            print(f"pack activated : {pid} (+{cnt} tools)")
    else:
        print("pack activated : (none)")
    print(f"prompt keys    : {sorted(ctx.prompts.keys())}")
    print(f"memory_map     : {ctx.memory_map}")
    print(f"llm roles      : {sorted(ctx.roles.keys())}")
    print(f"loop budget    : {ctx.budget}")
    print(f"graph memory   : {'attached' if ctx.graph is not None else 'none'}")
    print(f"total tools    : {len(agent._tools.list_tools())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
