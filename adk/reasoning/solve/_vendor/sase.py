# vendored from h30-repl-agent@f27271775d6786b1df5dd40234005436af8081c8:agent/repl/core/sase.py -- edit only by re-vendoring (see adk/reasoning/solve/_provenance.py)
"""SASE turn structure: Situation, Analysis, Synthesis, Execution.

Ported from AitherOS ``lib/cognitive/SASEIntegration.py`` (the four phases,
each producing an explicit result) without its fleet session plumbing.  In
the REPL loop one model reply carries all four phases:

    SITUATION: what the observation and memory digest say (1-3 lines)
    ANALYSIS:  which hypotheses the new evidence breaks, and why
    SYNTHESIS: ```python``` new/updated predict()/goal() code + hypothesize()
    EXECUTION: ```python``` act(..., expect=<prediction>) calls

The loop supplies the Situation text; Synthesis code must pass replay_check
(``hypothesize`` enforces it); every Execution act carries a prediction that
the learning ledger scores.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

PHASES = ("SITUATION", "ANALYSIS", "SYNTHESIS", "EXECUTION")
_HEADER_RE = re.compile(r"^[ \t>*#]*\**(SITUATION|ANALYSIS|SYNTHESIS|EXECUTION)\**[ \t]*:?", re.I | re.M)
_BLOCK_RE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.S | re.I)

PLAIN_PROTOCOL = (
    "Reply with at most 5 short lines of reasoning, then EXACTLY ONE ```python block.")

SASE_PROTOCOL = """Reply in exactly these four sections, in this order:
SITUATION: 1-3 lines -- what the observation and memory say now.
ANALYSIS: which hypotheses the new evidence breaks (name them and the evidence), or "none".
SYNTHESIS: one ```python block defining new/updated predict(frame, action) / goal(frame) functions and calling
  hypothesize(fn, "predict"|"goal"). Write "none" instead of a block only if nothing new was learned.
EXECUTION: one ```python block with the actions. EVERY act carries a SPECIFIC prediction:
  act(a, x, y, expect={"cells": {(r, c): colour, ...}, "changed": True/False})  or  expect=<full next frame>
  or expect="<name of a hypothesis>".  "changed" means the board changed (a HUD tick does not count).
  Predict cells, not just "changed". Wrong predictions are recorded as refuted evidence.
Never hypothesize stubs (pass / return None): they are rejected. Only hypothesize a rule you can compute."""

# h31: rules are grounded in the harness's evidence table and induced candidates
GROUNDED_SYNTHESIS = """SYNTHESIS: rules come FROM THE DATA. Read the EVIDENCE TABLE and the CANDIDATE RULES first; then either
  SELECT a candidate (hypothesize(wm_A1_b0) -- it already replays with 0 wrong), COMBINE candidates (call several inside
  one predict), GENERALISE one to cover more rows, or write a NEW rule that must predict cells on MORE transitions than
  the candidates for the rows it cites. EVERY rule you write cites its row(s): hypothesize(fn, note="E2: <what E2 shows>").
  A rule with no cited row is rejected; a rule the rows contradict is refuted before you spend an action.
  Once a predict rule is VERIFIED, plan(goal) simulates it to find the action path."""


def grounded_protocol() -> str:
    """SASE with the SYNTHESIS section replaced by the grounded one."""
    head, rest = SASE_PROTOCOL.split("SYNTHESIS:", 1)
    tail = rest.split("EXECUTION:", 1)[1]
    return head + GROUNDED_SYNTHESIS + chr(10) + "EXECUTION:" + tail


@dataclass
class ParsedReply:
    blocks: List[Tuple[str, str]] = field(default_factory=list)  # (phase, code) in document order
    phases: Dict[str, str] = field(default_factory=dict)         # phase -> its text
    found: List[str] = field(default_factory=list)                # phase headers present

    @property
    def code(self) -> str:
        return "\n\n".join(c for _p, c in self.blocks)


def parse_reply(content: str) -> ParsedReply:
    content = content or ""
    heads = [(m.start(), m.group(1).upper()) for m in _HEADER_RE.finditer(content)]
    out = ParsedReply()
    for i, (pos, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(content)
        if name not in out.phases:
            out.phases[name] = content[pos:end].strip()
            out.found.append(name)
    for m in _BLOCK_RE.finditer(content):
        phase = ""
        for pos, name in heads:
            if pos <= m.start():
                phase = name
        code = m.group(1).strip()
        if code:
            out.blocks.append((phase, code))
    return out


__all__ = ["parse_reply", "ParsedReply", "PHASES", "PLAIN_PROTOCOL", "SASE_PROTOCOL", "GROUNDED_SYNTHESIS",
           "grounded_protocol"]
