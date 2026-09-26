"""A compiled skill task as a reasoning-loop ``Environment``.

The task's world is a workspace directory; the model works in it through REPL tools
the environment injects (``sh``, ``read``, ``write``, ``ls``) and ends the episode with
action 1, SUBMIT, which runs the frozen verifier. The contract is the structural one of
``adk.reasoning.solve.Environment`` (``observe`` / ``act`` / ``available_actions`` /
``done`` plus optional hooks), so this module does not import the loop and loads without
the ``reason`` extra; ``Obs`` falls back to a local dataclass with the same fields.

Feedback on a failed submit is the COUNT of passing tests, never their names -- test
names would leak the private grading contract into the prompt. After ``max_submits``
failed submits the episode ends (``died``). ``final()`` re-runs the verifier on whatever
the workspace holds, so an episode scores the same whether or not the model submitted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .task import SkillTask, VerifierResult, Workspace, load_task

try:  # the loop's own Obs when the reason extra is installed
    from adk.reasoning.solve._types import Obs  # type: ignore
except Exception:  # noqa: BLE001 - structural fallback, identical fields
    from dataclasses import dataclass, field

    @dataclass
    class Obs:  # type: ignore[no-redef]
        state: Any
        level: int = 0
        level_up: bool = False
        died: bool = False
        done: bool = False
        win_state: Any = None
        info: Dict[str, Any] = field(default_factory=dict)

__all__ = ["SkillTaskEnv", "SUBMIT", "find_shell"]

SUBMIT = 1
OUTPUT_CAP = 4000
ECHO_CAP = 1500


def find_shell() -> Optional[List[str]]:
    """A POSIX shell argv prefix. On Windows prefer Git Bash: a bare ``bash`` on PATH
    there is often the WSL launcher, which runs in a different filesystem namespace."""
    override = os.environ.get("ADK_SKILLTASK_SHELL")
    if override:
        return [override, "-c"]
    if os.name == "nt":
        for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if os.path.isfile(cand):
                return [cand, "-c"]
        return None
    sh = shutil.which("bash") or shutil.which("sh")
    return [sh, "-c"] if sh else None


def _state(passed: int, total: int, submits: int) -> Any:
    try:
        import numpy as np  # the loop books states as arrays

        return np.array([[passed, total, submits]], dtype=np.int16)
    except ImportError:
        return (passed, total, submits)


class SkillTaskEnv:
    def __init__(self, task: "SkillTask | Path | str", *, max_submits: int = 3,
                 work_root: Optional[Path] = None, cmd_timeout_s: float = 60.0) -> None:
        self.task = task if isinstance(task, SkillTask) else load_task(Path(task))
        self.ws: Workspace = self.task.materialize(work_root)
        self.max_submits = int(max_submits)
        self.cmd_timeout_s = float(cmd_timeout_s)
        self.submits = 0
        self.passed = 0
        self.total = 0
        self.over = False
        self.won = False
        self.last: Optional[VerifierResult] = None
        self.tool_calls = 0
        self._shell = find_shell()

    # -- Environment -------------------------------------------------------------
    def observe(self) -> Obs:
        return Obs(_state(self.passed, self.total, self.submits), level=1 if self.won else 0,
                   done=self.over, info={"won": self.won} if self.over else {})

    def act(self, action: Any, source: str = "model") -> Obs:
        if self.over:
            return Obs(_state(self.passed, self.total, self.submits), level=int(self.won),
                       done=True, info={"won": self.won})
        if int(action[0]) != SUBMIT:
            return self.observe()
        self.submits += 1
        res = self.ws.verify()
        self.last = res
        self.passed = sum(1 for v in res.tests.values() if v)
        self.total = len(res.tests)
        if res.reward == 1.0:
            self.won = self.over = True
            return Obs(_state(self.passed, self.total, self.submits), level=1, level_up=True,
                       done=True, info={"won": True, "reward": 1.0})
        died = self.submits >= self.max_submits
        self.over = died
        return Obs(_state(self.passed, self.total, self.submits), level=0, died=died, done=died,
                   info={"reward": res.reward, "passed": self.passed, "total": self.total,
                         **({"won": False} if died else {})})

    def available_actions(self) -> List[int]:
        return [SUBMIT]

    def done(self) -> bool:
        return self.over

    # -- optional hooks ------------------------------------------------------------
    def needs_xy(self, action_id: int) -> bool:
        return False

    def primer(self) -> str:
        return (
            "This is a TERMINAL TASK, not a grid game. The world is a working directory; "
            "a hidden verifier grades its FINAL STATE.\n\n"
            "TASK:\n" + self.task.instruction.strip() + "\n\n"
            "TOOLS (call them from your Python code; each returns a string AND prints it):\n"
            " sh(cmd)            # run a POSIX shell command in the working directory "
            "(%ds timeout)\n"
            " read(path)         # a file's text\n"
            " write(path, text)  # replace a file's text IN PLACE (same file, same inode)\n"
            " ls(path='.')       # a recursive listing\n"
            "The namespace has no os/pathlib/subprocess (imports are refused): use these tools "
            "for every file and shell operation.\n"
            "ACTION: act(1) submits the working directory for grading. A failed submit "
            "tells you how many checks passed, not which; you have %d submits. Inspect "
            "first, change the files, verify with sh(), then act(1)."
            % (int(self.cmd_timeout_s), self.max_submits)
        )

    def render(self, obs: Any, last: Any) -> str:
        head = "workspace: %s | submits %d/%d" % (self.ws.path.name, self.submits, self.max_submits)
        if self.last is not None:
            head += " | last submit passed %d/%d checks" % (self.passed, self.total)
        return head

    def describe(self, t: Any) -> str:
        if getattr(t, "level_up", False):
            return "SUBMIT accepted: every check passed"
        return "SUBMIT: %d/%d checks passed" % (self.passed, self.total)

    def state_key(self, state: Any) -> str:
        return "s%d-p%d" % (self.submits, self.passed)

    def significant_change(self, before: Any, after: Any) -> bool:
        try:
            return bool((before != after).any())
        except AttributeError:
            return before != after

    def tools(self, loop: Any) -> Dict[str, Tuple[Callable[..., str], str]]:
        # Each tool also PRINTS its (capped) result: only printed output reaches the model
        # next turn, and a bare ``sh('ls')`` statement otherwise shows it nothing
        # (measured: bonsai2-27b spent its first turns on exactly that).
        def echo(fn: Callable[..., str]) -> Callable[..., str]:
            def wrapped(*a: Any, **k: Any) -> str:
                out = fn(*a, **k)
                if len(out) > ECHO_CAP:
                    print(out[:ECHO_CAP] + "\n[... %d more chars]" % (len(out) - ECHO_CAP))
                else:
                    print(out)
                return out
            wrapped.__name__ = fn.__name__
            return wrapped

        return {
            "sh": (echo(self.sh), "sh(cmd) -> output: run a shell command in the workspace"),
            "read": (echo(self.read), "read(path) -> text"),
            "write": (echo(self.write), "write(path, text) -> str: in-place write"),
            "ls": (echo(self.ls), "ls(path='.') -> listing"),
        }

    # -- tools -----------------------------------------------------------------------
    def _inside(self, path: str) -> Path:
        p = (self.ws.path / path).resolve()
        root = self.ws.path.resolve()
        if p != root and root not in p.parents:
            raise ValueError("path escapes the workspace: %s" % path)
        return p

    def sh(self, cmd: str) -> str:
        self.tool_calls += 1
        if self._shell is None:
            return "ERROR: no POSIX shell on this host (set ADK_SKILLTASK_SHELL)"
        try:
            proc = subprocess.run(self._shell + [str(cmd)], cwd=str(self.ws.path),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.cmd_timeout_s)
        except subprocess.TimeoutExpired:
            return "ERROR: timed out after %gs" % self.cmd_timeout_s
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return ("[exit %d]\n" % proc.returncode) + out[-OUTPUT_CAP:]

    def read(self, path: str) -> str:
        self.tool_calls += 1
        try:
            return self._inside(path).read_text(encoding="utf-8", errors="replace")[:OUTPUT_CAP * 2]
        except (OSError, ValueError) as exc:
            return "ERROR: %s" % exc

    def write(self, path: str, text: str) -> str:
        self.tool_calls += 1
        try:
            p = self._inside(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as fh:  # truncate in place
                fh.write(text)
            return "wrote %d chars to %s" % (len(text), path)
        except (OSError, ValueError) as exc:
            return "ERROR: %s" % exc

    def ls(self, path: str = ".") -> str:
        self.tool_calls += 1
        try:
            base = self._inside(path)
            rows = [q.relative_to(self.ws.path).as_posix() + ("/" if q.is_dir() else "")
                    for q in sorted(base.rglob("*")) if ".git" not in q.parts]
            return "\n".join(rows[:400])
        except (OSError, ValueError) as exc:
            return "ERROR: %s" % exc

    # -- scoring ---------------------------------------------------------------------
    def final(self) -> VerifierResult:
        """The verifier on the workspace as it stands, independent of any submit."""
        return self.ws.verify()

    def close(self) -> None:
        self.ws.cleanup()
