"""adk.packs — activate tool packs onto a live agent.

PackActivator wraps ``adk.builtin_tools.register_tool_packs`` (which drives the optional
``adk.tool_pack_loader``) with a required/optional contract: required packs must register
tools or bootstrap fails loudly; optional packs soft-degrade. Activation is EAGER ONLY —
a tool that is not registered is invisible to the LLM, so lazy activation would be a
silent capability hole.
"""

from adk.packs.activator import PackActivator, PackUnavailable

__all__ = ["PackActivator", "PackUnavailable"]
