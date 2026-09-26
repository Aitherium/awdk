"""adk.skilltasks -- skill documents compiled into verified, gradeable tasks.

A task is a small world (a workspace), an instruction, a frozen verifier that grades the
world's FINAL STATE, a rubric, a reference solution and must-avoid probes. The
acceptance gate keeps a task only if the reference scores 1.0, doing nothing scores
0.0, every must-avoid probe scores below 1.0, and the grading contract is unchanged
since it was frozen -- with no model anywhere in the gate. ``SkillTaskEnv`` exposes a
task through the reasoning loop's ``Environment`` contract.

The idea follows Skill2Env (NVlabs, Apache-2.0: skills -> Harbor tasks gated by an
Oracle/NOP run); no code is copied from it. See ``python -m adk.skilltasks --help``.

Stdlib only; 3.10-compatible.
"""

from __future__ import annotations

from .freeze import FreezeError, check_freeze, freeze
from .gate import GateReport, gate_root, gate_task
from .rubric import Rubric, parse_rubric
from .task import SkillTask, TaskError, VerifierResult, Workspace, load_task, reward_of

__all__ = [
    "SkillTask", "Workspace", "VerifierResult", "TaskError", "load_task", "reward_of",
    "freeze", "check_freeze", "FreezeError",
    "Rubric", "parse_rubric",
    "GateReport", "gate_task", "gate_root",
]
