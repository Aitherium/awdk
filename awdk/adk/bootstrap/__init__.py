"""adk.bootstrap — build a live agent from a declarative spec.

Phase-0 of the generic self-bootstrapping agent: one function, ``build_agent_from_spec``,
turns an ``agent.yaml`` spec into a constructed ``AitherAgent`` plus a small ``RunCtx``
carrying the resolved prompts, the memory-map role/tier overrides, the loop budget, and
the LLM role table (which the current adk ``LLMRouter`` does not itself model, so the
caller wires it).
"""

from adk.bootstrap.generic_agent import RunCtx, build_agent_from_spec

__all__ = ["RunCtx", "build_agent_from_spec"]
