"""A debugging environment: make a failing test pass.

The first non-ARC domain for the reasoning loop. The world is a small source
tree in a private scratch directory; the model edits it and runs its tests.

* **Staging** is a REPL tool, not an action: ``stage_patch(path, old, new)``
  checks that ``old`` occurs exactly once in ``path`` and returns a patch id.
  ``read_file`` / ``list_files`` / ``test_output`` are read-only tools.
* **Acting** is what the ledger scores:

  ======  ==============================  ===========================
  action  meaning                         call from model code
  ======  ==============================  ===========================
  1       apply staged patch ``x``, test  ``act(1, patch_id, 0)``
  2       revert to the original tree     ``act(2)``
  3       run the tests unchanged         ``act(3)``
  ======  ==============================  ===========================

  Every action runs the test command. Tests exiting 0 clears the only level
  and wins the episode.

* **State** (numpy, shape ``(1, 5)``): ``[failing, n_failed, n_passed,
  patches_applied, tree_digest]`` -- counts are -1 when the runner's summary
  line cannot be parsed; ``tree_digest`` makes every distinct tree a novel
  state.

Guards, because model-written code runs here: patches cannot leave the scratch
tree (absolute paths and ``..`` are refused), the tests run in a subprocess
with a wall-clock timeout and a SCRUBBED environment (``PATH`` and the OS
basics only -- no tokens or service keys reach the code under test), and the
scratch tree is a copy: the caller's files are never modified. This is a guard,
not an isolation boundary (``docs/reasoning-loop-design.md`` risk 3).

Needs numpy (``adk[reason]``), like every environment the vendored core plays.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .._types import Action, Obs

__all__ = ["FailingTestEnv", "DEMO_FILES", "PASSTHROUGH_ENV"]

APPLY, REVERT, RUN = 1, 2, 3

#: The only environment variables the test subprocess inherits.
PASSTHROUGH_ENV: Tuple[str, ...] = (
    "PATH",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
)

#: The built-in example: an off-by-one in ``mean`` and the stdlib test that sees it.
DEMO_FILES: Dict[str, str] = {
    "calc.py": (
        "def mean(xs):\n"
        '    """Arithmetic mean of a non-empty list."""\n'
        "    return sum(xs) / (len(xs) + 1)\n"
    ),
    "test_calc.py": (
        "import unittest\n\n"
        "from calc import mean\n\n\n"
        "class TestMean(unittest.TestCase):\n"
        "    def test_mean(self):\n"
        "        self.assertEqual(mean([2, 4, 6]), 4)\n\n"
        "    def test_single(self):\n"
        "        self.assertEqual(mean([7]), 7)\n"
    ),
}

_PYTEST = re.compile(r"(\d+) (failed|passed|error|errors)\b")
_UNITTEST_RAN = re.compile(r"Ran (\d+) tests?")
_UNITTEST_FAIL = re.compile(r"(failures|errors)=(\d+)")


def _counts(out: str) -> Tuple[int, int]:
    """(n_failed, n_passed) from a pytest or unittest summary; -1 when unknown."""
    found = _PYTEST.findall(out)
    if found:
        failed = sum(int(n) for n, k in found if k != "passed")
        passed = sum(int(n) for n, k in found if k == "passed")
        return failed, passed
    ran = _UNITTEST_RAN.findall(out)
    if ran:
        total = int(ran[-1])
        failed = sum(int(n) for _k, n in _UNITTEST_FAIL.findall(out))
        return failed, max(0, total - failed)
    return -1, -1


class FailingTestEnv:
    """Make ``test_cmd`` exit 0 by patching a copy of a source tree.

    ``files`` (relative path -> text) or ``root`` (a directory to copy) gives the
    starting tree. ``test_cmd`` defaults to stdlib ``unittest`` discovery with
    this interpreter, so the example needs no pytest. Call :meth:`close` (or use
    ``with``) to delete the scratch copy.
    """

    def __init__(
        self,
        files: Optional[Mapping[str, str]] = None,
        *,
        root: Optional[str] = None,
        test_cmd: Optional[Sequence[str]] = None,
        timeout_s: float = 60.0,
        max_patches: int = 64,
        output_cap: int = 4000,
        description: str = "",
    ) -> None:
        if (files is None) == (root is None):
            raise ValueError("give exactly one of files= or root=")
        self.test_cmd = list(test_cmd or [sys.executable, "-m", "unittest", "-q"])
        self.timeout_s = float(timeout_s)
        self.max_patches = int(max_patches)
        self.output_cap = int(output_cap)
        self.description = description
        self.root = Path(tempfile.mkdtemp(prefix="adk-debug-"))
        self._orig = self.root.parent / (self.root.name + ".orig")
        if files is not None:
            for rel, text in files.items():
                p = self._path(rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8", newline="\n")
        else:
            shutil.copytree(str(root), str(self.root), dirs_exist_ok=True)
        shutil.copytree(str(self.root), str(self._orig))
        self.patches: List[Tuple[str, str, str]] = []
        self.applied = 0
        self.over = False
        self.level = 0
        self.last_output = ""
        self.last_rc: Optional[int] = None
        self.notes: List[str] = []
        self._state = self._run_tests()
        self.notes.append("initial: " + self._verdict())

    @classmethod
    def demo(cls, **kw: Any) -> "FailingTestEnv":
        """The built-in example: fix ``mean`` so ``test_calc.py`` passes."""
        kw.setdefault(
            "description", "calc.mean is wrong; make test_calc.py pass without editing the test."
        )
        return cls(dict(DEMO_FILES), **kw)

    # ------------------------------------------------------------------ files
    def _path(self, rel: str) -> Path:
        rel = str(rel).replace("\\", "/")
        if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in rel.split("/"):
            raise ValueError("path must be relative and inside the tree: %r" % rel)
        p = (self.root / rel).resolve()
        root = self.root.resolve()
        if p != root and root not in p.parents:
            raise ValueError("path escapes the tree: %r" % rel)
        return p

    def list_files(self) -> List[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                out.append(p.relative_to(self.root).as_posix())
        return out

    def read_file(self, path: str) -> str:
        return self._path(path).read_text(encoding="utf-8")

    def stage_patch(self, path: str, old: str, new: str) -> int:
        """Stage ``old -> new`` in ``path``; returns the patch id for ``act(1, id, 0)``.

        ``old == ""`` creates ``path`` (which must not exist yet)."""
        if len(self.patches) >= self.max_patches:
            raise ValueError("patch limit %d reached" % self.max_patches)
        self._check_patch(str(path), str(old))
        self.patches.append((str(path), str(old), str(new)))
        return len(self.patches) - 1

    def _check_patch(self, path: str, old: str) -> Path:
        p = self._path(path)
        if old == "":
            if p.exists():
                raise ValueError("%s exists; give the text to replace" % path)
            return p
        if not p.is_file():
            raise ValueError("no such file: %s" % path)
        n = p.read_text(encoding="utf-8").count(old)
        if n != 1:
            raise ValueError(
                "old text occurs %d times in %s; it must occur exactly once" % (n, path)
            )
        return p

    def _digest(self) -> int:
        h = hashlib.sha256()
        for rel in self.list_files():
            h.update(rel.encode("utf-8") + b"\0" + (self.root / rel).read_bytes() + b"\0")
        return int(h.hexdigest()[:12], 16)

    # ------------------------------------------------------------------ tests
    def _run_tests(self) -> np.ndarray:
        env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            cp = subprocess.run(
                self.test_cmd,
                cwd=str(self.root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                check=False,
            )
            rc: int = cp.returncode
            out = (cp.stdout or "") + (cp.stderr or "")
        except subprocess.TimeoutExpired as exc:
            rc = -1
            out = "TIMEOUT after %.0fs\n%s" % (self.timeout_s, exc.stdout or "")
        except OSError as exc:
            rc = -2
            out = "could not run %s: %s" % (self.test_cmd[0], exc)
        self.last_rc = rc
        self.last_output = out[-self.output_cap :]
        failed, passed = _counts(out)
        return np.array(
            [[0 if rc == 0 else 1, failed, passed, self.applied, self._digest()]], dtype=np.int64
        )

    def _verdict(self) -> str:
        s = self._state[0]
        if s[0] == 0:
            return "tests PASS (%d passed)" % s[2]
        if self.last_rc == -1:
            return "tests TIMED OUT"
        return "tests FAIL (%d failed, %d passed, exit %s)" % (s[1], s[2], self.last_rc)

    # ------------------------------------------------------------ Environment
    def observe(self) -> Obs:
        return Obs(self._state.copy(), level=self.level, done=self.over)

    def act(self, action: Action, source: str = "model") -> Obs:
        if self.over:
            return Obs(self._state.copy(), level=self.level, done=True)
        a, x = int(action[0]), int(action[1])
        note = ""
        if a == APPLY:
            if not 0 <= x < len(self.patches):
                note = "no staged patch %d (stage one with stage_patch first)" % x
            else:
                path, old, new = self.patches[x]
                try:
                    p = self._check_patch(path, old)
                    if old == "":
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(new, encoding="utf-8", newline="\n")
                    else:
                        text = p.read_text(encoding="utf-8")
                        p.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
                    self.applied += 1
                    note = "applied patch %d to %s" % (x, path)
                except ValueError as exc:
                    note = "patch %d not applied: %s" % (x, exc)
        elif a == REVERT:
            shutil.rmtree(str(self.root))
            shutil.copytree(str(self._orig), str(self.root))
            self.applied = 0
            note = "reverted to the original tree"
        elif a == RUN:
            note = "ran the tests"
        else:
            note = "unknown action %d" % a
        self._state = self._run_tests()
        self.notes.append("%s; %s" % (note, self._verdict()))
        if self._state[0, 0] == 0:
            self.level = 1
            self.over = True
            return Obs(
                self._state.copy(),
                level=1,
                level_up=True,
                done=True,
                win_state=self._state.copy(),
                info={"won": True, "note": note},
            )
        return Obs(self._state.copy(), level=self.level, info={"note": note})

    def available_actions(self) -> List[int]:
        return [APPLY, REVERT, RUN]

    def done(self) -> bool:
        return self.over

    # ------------------------------------------------------------------ hooks
    def needs_xy(self, action_id: int) -> bool:
        return int(action_id) == APPLY

    def primer(self) -> str:
        return (
            "DOMAIN: debugging. A source tree has a failing test; make the test command exit 0 by "
            "editing SOURCE files (do not weaken the tests).\n"
            "Tools: list_files() -> [paths]; read_file(path) -> text; test_output() -> the last "
            "test run's output; stage_patch(path, old, new) -> patch_id (old must occur exactly "
            "once in path; old='' creates a new file).\n"
            "Actions (each one re-runs the tests): act(1, patch_id, 0) applies a staged patch; "
            "act(2) reverts every applied patch; act(3) re-runs the tests unchanged.\n"
            "State (1x5 int array): [failing, n_failed, n_passed, patches_applied, tree_digest]; "
            "counts are -1 when the runner summary cannot be parsed. Winning = failing becomes 0."
            + ("\nTASK: " + self.description if self.description else "")
        )

    def render(self, obs: Obs, last: Any) -> str:
        head = "files: %s\n%s; %d patch(es) staged, %d applied" % (
            ", ".join(self.list_files()),
            self._verdict(),
            len(self.patches),
            self.applied,
        )
        tail = self.last_output[-1500:]
        return head + "\nlast test output (tail):\n" + tail

    def describe(self, t: Any) -> str:
        i = getattr(t, "i", None)
        # notes[0] is the initial run, so action i's note is notes[i + 1].
        if isinstance(i, int) and 0 <= i + 1 < len(self.notes):
            return self.notes[i + 1]
        return self.notes[-1] if self.notes else "no change"

    def tools(self, loop: Any) -> Dict[str, Tuple[Callable[..., Any], str]]:
        return {
            "list_files": (self.list_files, "list_files() -> relative paths in the tree"),
            "read_file": (self.read_file, "read_file(path) -> file text"),
            "test_output": (lambda: self.last_output, "test_output() -> last test run output"),
            "stage_patch": (self.stage_patch, "stage_patch(path, old, new) -> patch_id"),
        }

    # ---------------------------------------------------------------- cleanup
    def close(self) -> None:
        for p in (self.root, self._orig):
            shutil.rmtree(str(p), ignore_errors=True)

    def __enter__(self) -> "FailingTestEnv":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
