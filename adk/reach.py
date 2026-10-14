"""Reach lines: what this agent can reach beyond its tool menu, stated in the system prompt.

This module is a CONTRACT. ``AitherAgent.stream_react`` appends ``reach_lines(...)`` to
the system prompt next to the tools menu; the body is filled in independently. Until
then it returns "", so the prompt is unchanged.

Rules every implementation keeps:

- FAIL OPEN and SYNCHRONOUS. It runs while the system prompt is built; it must not do
  network I/O. Read state the agent already holds; on any fault log and return "".
- BUDGET. System prompt + tools menu + reach lines share about 30% of the compaction
  window, so reach lines stay under ``REACH_MAX_CHARS``.

Why the lines exist at all: on an unclassified turn only the core tool schemas ship,
and a capability the model cannot see is one it does not use. Measured on the awsh
daemon: delegation, the code graph and the skill library were all registered or
reachable, and the small local model never called any of them because nothing in the
prompt named them. Each line is emitted only when the tool it names is REGISTERED on
this agent -- advertising a tool that is not there is the opposite defect (the model
calls it and gets "unknown tool").
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("adk.reach")

REACH_MAX_CHARS = 600

#: What this implementation aims for: about 120 tokens by the chars/4 estimate.
REACH_TARGET_CHARS = 480

#: Placeholder for the delegation line, built per agent: call_tool refuses a name the
#: host or agent deny grant blocks, so a denied delegate is never advertised.
_DELEGATE = "<delegate>"
_DELEGATES = (("ask_agent", "ask_agent {agent,question}"),
              ("forge_subagent", "forge_subagent {task}"))


def _delegate_line(agent: Any) -> str:
    import sys

    denied = getattr(sys.modules.get("adk.server"), "gateway_tool_denied", None)
    allowed = [sig for name, sig in _DELEGATES
               if not (callable(denied) and denied(name, agent))]
    if not allowed:
        return ""
    return "- delegate: call_tool name=" + " or ".join(allowed)


#: (tool names that must ALL be registered, any-of names, the line). Priority order:
#: when the budget runs out the later lines are dropped whole, never cut mid-line.
_LINES: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("call_tool",), (), _DELEGATE),
    ((), ("graph_code_search", "code_concept_search"),
     "- code graph: code_concept_search(query) -> path:line + signature"),
    (("skill_search",), (),
     "- skills: skill_search(query), then skill_load(name)"),
    (("a2a_send",), (), "- peer agent: a2a_send(agent_name, text)"),
    (("repl_run",), (), "- python: repl_run(code), variables persist"),
    (("recurse_file",), (), "- big file: recurse_file(path, query)"),
    (("browse_page",), (), "- JS page: browse_page(url)"),
)

_HEADER = "Also reachable by name:"


def _registered(agent: Any):
    reg = getattr(agent, "_tools", None)
    get = getattr(reg, "get", None)
    if not callable(get):
        return None

    def has(name: str) -> bool:
        try:
            return get(name) is not None
        except Exception:  # noqa: BLE001 -- a broken registry lookup = not reachable
            return False

    return has


def reach_lines(agent: Any, decision: Any = None) -> str:
    """Lines naming the planes this turn can reach (memory, graph, peers, harnesses).

    Args:
        agent: the ``AitherAgent`` running the turn.
        decision: the turn's ``adk.intent.IntentDecision``, or None; lets a light turn
            get fewer lines.

    Returns newline-joined lines of at most ``REACH_MAX_CHARS`` characters, or "".
    """
    try:
        # A non-agentic turn runs no tool loop; naming tools there is pure tax.
        if decision is not None and getattr(decision, "agentic", True) is False:
            return ""
        has = _registered(agent)
        if has is None:
            return ""
        budget = min(REACH_TARGET_CHARS, REACH_MAX_CHARS)
        out = [_HEADER]
        used = len(_HEADER)
        for need_all, need_any, line in _LINES:
            if need_all and not all(has(n) for n in need_all):
                continue
            if need_any and not any(has(n) for n in need_any):
                continue
            if line == _DELEGATE:
                line = _delegate_line(agent)
                if not line:
                    continue
            if used + 1 + len(line) > budget:
                continue
            out.append(line)
            used += 1 + len(line)
        return "\n".join(out) if len(out) > 1 else ""
    except Exception as exc:  # noqa: BLE001 -- fail open: the prompt goes out without it
        logger.warning("reach_lines failed: %s", exc)
        return ""
