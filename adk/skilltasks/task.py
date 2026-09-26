"""One compiled skill task on disk, and the three ways to run it.

A task is a directory::

    task.json            id, skill provenance, outcome_tests, limits
    instruction.md       what the solver is asked (the only text it is given)
    environment/files/   the initial world, copied into a fresh workspace
    environment/setup.py optional: ``python setup.py <workspace> <private>`` builds
                         state that cannot be committed as files (a git repo, an inode
                         record); anything the verifier must remember goes in <private>
    tests/test.py        ``python test.py <workspace> <private>`` prints one JSON object
                         ``{"tests": {name: bool}}`` as its LAST stdout line
    tests/rubric.md      ## Must-do / ## Must-avoid / ## Best-practice
    tests/freeze.json    written by ``freeze`` BEFORE solution/ exists
    solution/solve.py    the reference solution: ``python solve.py <workspace>``
    solution/avoid_*.py  must-avoid probes: adversarial solutions that must NOT pass

The solver only ever sees the workspace. ``<private>`` sits beside it, outside the
workspace, so the verifier can keep facts (a base commit, an inode) the solver cannot
edit.

Reward: 0.0 unless every ``outcome_tests`` entry passed; otherwise the fraction of all
tests that passed. So a do-nothing run scores 0 by construction only if every outcome
test fails on the pristine world -- which the acceptance gate asserts test by test.

Stdlib only; 3.10-compatible.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["SkillTask", "Workspace", "VerifierResult", "load_task", "reward_of", "TaskError"]

REQUIRED_FILES = ("task.json", "instruction.md", "tests/test.py", "tests/rubric.md")


class TaskError(RuntimeError):
    """The task directory is malformed or a script could not run at all."""


@dataclass
class VerifierResult:
    tests: Dict[str, bool]
    reward: float
    outcome_failed: List[str]
    stdout_tail: str = ""
    returncode: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"tests": dict(self.tests), "reward": self.reward,
                "outcome_failed": list(self.outcome_failed), "returncode": self.returncode}


@dataclass
class SkillTask:
    root: Path
    id: str
    meta: Dict[str, Any]
    outcome_tests: List[str] = field(default_factory=list)

    @property
    def instruction(self) -> str:
        return (self.root / "instruction.md").read_text(encoding="utf-8")

    @property
    def timeout_s(self) -> float:
        return float(self.meta.get("timeout_s", 120))

    def probes(self) -> List[Path]:
        sol = self.root / "solution"
        return sorted(sol.glob("avoid_*.py")) if sol.is_dir() else []

    def materialize(self, parent: Optional[Path] = None) -> "Workspace":
        """A fresh copy of the initial world: ``<run>/workspace`` + ``<run>/private``."""
        run = Path(tempfile.mkdtemp(prefix="skilltask-%s-" % self.id[:24], dir=parent))
        ws, private = run / "workspace", run / "private"
        files = self.root / "environment" / "files"
        if files.is_dir():
            shutil.copytree(files, ws)
        else:
            ws.mkdir(parents=True)
        private.mkdir()
        setup = self.root / "environment" / "setup.py"
        if setup.is_file():
            proc = _run_py(setup, [str(ws), str(private)], cwd=ws, timeout=self.timeout_s)
            if proc.returncode != 0:
                shutil.rmtree(run, ignore_errors=True)
                raise TaskError("%s: environment/setup.py exited %d: %s"
                                % (self.id, proc.returncode, _tail(proc.stderr or proc.stdout)))
        return Workspace(task=self, run=run, path=ws, private=private)


@dataclass
class Workspace:
    task: SkillTask
    run: Path
    path: Path
    private: Path

    def apply(self, script: Path) -> subprocess.CompletedProcess:
        """Run a solution-shaped script (reference or probe) against this workspace."""
        return _run_py(script, [str(self.path)], cwd=self.path, timeout=self.task.timeout_s)

    def verify(self) -> VerifierResult:
        test = self.task.root / "tests" / "test.py"
        proc = _run_py(test, [str(self.path), str(self.private)], cwd=self.task.root / "tests",
                       timeout=self.task.timeout_s)
        doc = _last_json(proc.stdout or "")
        if doc is None or not isinstance(doc.get("tests"), dict) or not doc["tests"]:
            raise TaskError("%s: tests/test.py printed no {\"tests\": {...}} object (exit %d): %s"
                            % (self.task.id, proc.returncode, _tail(proc.stderr or proc.stdout)))
        tests = {str(k): v is True for k, v in doc["tests"].items()}
        missing = [t for t in self.task.outcome_tests if t not in tests]
        if missing:
            raise TaskError("%s: outcome_tests %s are not reported by tests/test.py"
                            % (self.task.id, missing))
        reward, failed = reward_of(tests, self.task.outcome_tests)
        return VerifierResult(tests=tests, reward=reward, outcome_failed=failed,
                              stdout_tail=_tail(proc.stdout), returncode=proc.returncode)

    def cleanup(self) -> None:
        shutil.rmtree(self.run, ignore_errors=True)


def reward_of(tests: Dict[str, bool], outcome: List[str]) -> "tuple[float, List[str]]":
    failed = [t for t in outcome if not tests.get(t, False)]
    if failed or not tests:
        return 0.0, failed
    return sum(1 for v in tests.values() if v) / len(tests), []


def load_task(root: Path) -> SkillTask:
    root = Path(root).resolve()  # scripts run with cwd=workspace; a relative root breaks them
    missing = [r for r in REQUIRED_FILES if not (root / r).is_file()]
    if missing:
        raise TaskError("%s: missing %s" % (root.name, ", ".join(missing)))
    try:
        meta = json.loads((root / "task.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaskError("%s: task.json unreadable: %s" % (root.name, exc)) from exc
    outcome = meta.get("outcome_tests")
    if not isinstance(outcome, list) or not outcome or not all(isinstance(t, str) for t in outcome):
        raise TaskError("%s: task.json needs a non-empty outcome_tests list" % root.name)
    return SkillTask(root=root, id=str(meta.get("id") or root.name), meta=meta,
                     outcome_tests=list(outcome))


def find_tasks(root: Path) -> List[Path]:
    root = Path(root)
    if (root / "task.json").is_file():
        return [root]
    return sorted(p.parent for p in root.glob("*/task.json"))


def _run_py(script: Path, args: List[str], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    try:
        return subprocess.run([sys.executable, str(script), *args], cwd=str(cwd), env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(exc.cmd, 124, stdout=str(exc.stdout or ""),
                                           stderr="timeout after %gs" % timeout)


def _last_json(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                return doc
    return None


def _tail(text: Optional[str], n: int = 600) -> str:
    return (text or "").strip()[-n:]
