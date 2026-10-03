"""Terminal/file-task mode: a compiled skill task as a full reasoning-loop Environment.

:class:`adk.skilltasks.env.SkillTaskEnv` exposes ONE action (submit) and leaves the work
to free REPL tools the loop never sees, so the loop's learning machinery -- episodic
transitions, the evidence table, hypotheses, the prediction ledger, PRISM -- had
nothing to learn from (measured: 0.0 twice with bonsai2-27b, 1 of 3 files fixed). This
adapter makes every piece of terminal work an ACTION the loop books:

====  ===========  ============================================================
 id    name         what it does
====  ===========  ============================================================
 1     SH           run a shell command in the workspace
 2     READ         read a file, or list a directory
 3     WRITE        replace a file's text IN PLACE (same inode)
 4     PATCH        replace one exact occurrence of a string, in place
 5     TEST         run the task's frozen tests on the workspace
 6     SUBMIT       grade the workspace and end the episode on a pass
 7     TRY          a change hypothesis: apply steps, run the tests, ROLL BACK
 8     APPLY        apply a change whose TRY verified it
====  ===========  ============================================================

An action's arguments (a command, a path, a text) cannot ride in the loop's integer
``(id, x, y)`` tuple, so each tool STAGES its arguments and acts ``(id, k, -1)`` where
``k`` indexes the staged record; the environment executes record ``k``.

**Every action goes through** :func:`adk.reasoning.solve.context.permits` with a
default-DENY sandbox policy: paths must resolve inside the task workspace, shell
commands may not name a path outside it (absolute paths, ``..`` escapes, ``~`` /
``$HOME``), and nothing may reach the network (network tools, remote git verbs,
package installs, ``/dev/tcp``, URLs, network-module imports in commands or in written
files). ``APPLY`` is additionally a CONTEXT rule: it is permitted only for a change the
context holds as verified by a TEST-kind evidence record (a passing TRY). That is the
POLICY layer. Under it is a JAIL (:mod:`adk.skilltasks.jail`): every shell command -- ``sh``
and the ``sh`` steps of a TRY/APPLY -- runs inside a podman container with no network, a
read-only root, only the workspace mounted, a non-root user, no capabilities and
CPU/memory/pids limits, so a program the model writes and then runs is confined too.
The frozen verifier executes what the model left in the workspace, so in a jailed run it
runs in a second container of its own (the workspace, ``private/`` and the read-only
tests) and never as a host process (:attr:`SkillTaskTerminalEnv.verifier_sandbox`).
Where podman is unavailable the adapter falls back to the policy alone and says so
loudly: :attr:`SkillTaskTerminalEnv.sandbox` is ``"policy-only, not a jail"`` (shells
then get dead proxies and, where ``unshare -rn`` works, no network namespace).

**Stall guard** (measured: the failed SASE runs re-read the same files -- 29 of 36 reads in
one run -- because the tools' printed output never reached the model). Every observation
is an EVIDENCE record keyed by content: a file read by its sha256, a command by its
output, a test run by its results on this workspace. A re-read of an unchanged file
returns ``UNCHANGED since t<n>`` and the known summary instead of the text again -- but
ONLY while that text is still in the model's context (the pending result or a turn the
loop's working memory still holds). Once the turn that carried it was evicted, a re-read
returns the text again and counts as evidence, never as a stall. The
situation lists the files already read and says which are still in context; and
``stall_actions`` consecutive actions that add
no evidence set intent=stuck and end the turn, so PRISM rotates to test-first (or, from
test-first, to minimal-patch's try-change).

**State** (int8, the loop's episodic dtype), shape ``(5, W)``:

* row 0 -- ``[exit class, tests run, submits, tests passing, tests known, trial]``
  (exit class 0 ok / 1 non-zero / 2 none / 3 denied; trial 0 none / 1 refuted / 2 verified)
* row 1 -- the workspace's per-test status from the last TEST (0 unknown, 1 fail, 2 pass)
* row 2 -- the per-test status of the last TRY (the trial's world, rolled back since)
* rows 3-4 -- two independent 1..120 digests per tracked file (0 = absent); ``<git>``
  is a pseudo-file covering each repository's HEAD, refs and index

**Hooks for terminal work** (the loop's SASE/intent/PRISM/prediction machinery is reused
unchanged): ``family``/``effects`` make the evidence table a COMMAND -> EFFECT table
(``write:check_landing.py``, ``test after patch:check_landing.py | test:x moved
(+1,+0)``); a TRY is a hypothesis "this change makes test X pass" verified by actually
running the tests -- replay_check's analogue -- and booked into the loop's hypothesis
store (``kind="change"``) so PRISM scores the strategy that verified it; ``run_tests(
expect_pass=[...])`` is an inline prediction the ledger scores. PRISM's arms are
presented as the terminal strategies (:data:`TERMINAL_STRATEGIES`): test-first,
read-first, minimal-patch, and a scripted baseline (the auto arms: run the tests, read
every file -- no model call).

Scoring is the frozen verifier on the final workspace (:meth:`final`); the rubric is a
secondary report (:meth:`rubric_report`), never reward.

Stdlib at import; numpy is used when present (the loop needs it anyway). 3.10-compatible.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .env import find_shell
from .jail import (
    POLICY_ONLY,
    VERIFY_PRIVATE,
    VERIFY_TESTS,
    Jail,
    JailBrokenError,
    JailUnavailableError,
    open_jail,
    open_verifier_jail,
)
from .rubric import parse_rubric
from .task import SkillTask, TaskError, VerifierResult, Workspace, load_task

try:  # the loop's own Obs when the reason extra is installed
    from adk.reasoning.solve._types import Obs  # type: ignore
except Exception:  # noqa: BLE001 - structural fallback, identical fields
    from dataclasses import dataclass as _dc

    @_dc
    class Obs:  # type: ignore[no-redef]
        state: Any
        level: int = 0
        level_up: bool = False
        died: bool = False
        done: bool = False
        win_state: Any = None
        info: Dict[str, Any] = field(default_factory=dict)


__all__ = [
    "SkillTaskTerminalEnv",
    "TERMINAL_STRATEGIES",
    "ACTION_NAMES",
    "SH",
    "READ",
    "WRITE",
    "PATCH",
    "TEST",
    "SUBMIT",
    "TRY",
    "APPLY",
    "POLICY_ONLY",
    "STALL_ACTIONS",
    "sandbox_policy",
    "shell_problems",
    "run_policy",
    "do_nothing_policy",
    "scripted_baseline_policy",
    "reference_policy",
]

_log = logging.getLogger(__name__)

SH, READ, WRITE, PATCH, TEST, SUBMIT, TRY, APPLY = 1, 2, 3, 4, 5, 6, 7, 8
ACTION_NAMES = {
    SH: "sh",
    READ: "read",
    WRITE: "write",
    PATCH: "patch",
    TEST: "test",
    SUBMIT: "submit",
    TRY: "try",
    APPLY: "apply",
}
_MUTATING = (SH, WRITE, PATCH, APPLY)
OUTPUT_CAP = 3000
ECHO_CAP = 6000
STALL_ACTIONS = 4  # consecutive actions with no new evidence -> intent=stuck
SUMMARY_LINES, SUMMARY_CHARS = 6, 400
ROWS = 5
_UNKNOWN, _FAIL, _PASS = 0, 1, 2


# --------------------------------------------------------------------------- strategies
@dataclass(frozen=True)
class TerminalStrategy:
    name: str
    principle: str
    heuristics: Tuple[str, ...]
    avoid: Tuple[str, ...] = ()


#: PRISM arm id (vendored, fixed) -> the terminal strategy it is presented as.
TERMINAL_STRATEGIES: Dict[str, TerminalStrategy] = {
    "rule_first": TerminalStrategy(
        "test-first",
        "Let the frozen tests say what is wrong before you change anything.",
        (
            "run_tests() first; pick ONE failing Must-do test.",
            "State a change hypothesis and verify it: "
            "try_change(name, steps, makes_pass=[that test]).",
            "apply_change(name) only once it is VERIFIED; then run_tests(expect_pass=[...]).",
        ),
        ("Editing before you know which test fails.", "Applying a change no TRY verified."),
    ),
    "goal_first": TerminalStrategy(
        "read-first",
        "Understand the files the task names before touching them.",
        (
            "read() every file the instruction names, and the contract/spec file if there is one.",
            "Map each Must-do bullet to the exact lines that must change; note() the mapping.",
            "Then make ONE change and try_change() it against the tests it should fix.",
        ),
        ("Rewriting a file you have not read.",),
    ),
    "analogy": TerminalStrategy(
        "minimal-patch",
        "The smallest edit that makes the failing test pass is the best edit.",
        (
            "patch(path, old, new) a few lines; never rewrite a whole file when a patch will do.",
            "Keep everything the Must-avoid bullets protect exactly as it is.",
            "try_change() each patch against the tests it should fix before apply_change().",
        ),
        ("Whole-file rewrites.", "Touching files no failing test is about."),
    ),
    "handoff": TerminalStrategy(
        "scripted-baseline", "No model call: run the tests, then read every file once.", ()
    ),
    "click_scan": TerminalStrategy(
        "scripted-scan", "No model call: read each file not read yet, and run the tests.", ()
    ),
}

TERMINAL_SYSTEM = "\n".join(
    [
        (
            "You are working on a TERMINAL TASK by writing Python. There is no grid. A hidden"
            ", frozen verifier grades the FINAL STATE of the working directory; the rubric sh"
            "own each turn names its tests."
        ),
        "",
        (
            "Code runs in a persistent namespace (variables survive between turns); each turn"
            " has a limited number of actions. Only what you print() reaches you next turn --"
            " every tool below prints its own result."
        ),
        "",
        "TOOLS (each call is ONE action and returns its output as a string):",
        (
            " sh(cmd)                         # POSIX shell, cwd = the workspace root. No net"
            "work. No paths outside the workspace."
        ),
        (
            " read(path)                      # a file's text; a directory gives its listing "
            "('.' = the whole tree)"
        ),
        (
            " write(path, text)               # replace a file's whole text IN PLACE (same fi"
            "le, same inode)"
        ),
        (
            " patch(path, old, new)           # replace exactly ONE occurrence of old with ne"
            "w, in place"
        ),
        (
            " run_tests(expect_pass=None)     # run the frozen tests on the workspace -> per-"
            "test pass/fail;"
        ),
        (
            "                                 #   expect_pass=[names] is your PREDICTION of w"
            "hich pass (scored)"
        ),
        ' try_change(name, steps, makes_pass, note="")',
        (
            '                                 # a HYPOTHESIS "these steps make these tests pa'
            'ss": applies the steps,'
        ),
        (
            "                                 #   runs the tests, then ROLLS THE WORKSPACE BA"
            "CK. VERIFIED only if every"
        ),
        (
            '                                 #   test in makes_pass passes. steps: [("patch"'
            ", path, old, new),"
        ),
        '                                 #   ("write", path, text), ("sh", cmd)]',
        (
            " apply_change(name)              # apply a VERIFIED change for real (refused for"
            " an unverified one)"
        ),
        " submit()                        # grade the workspace; a pass ends the task",
        (
            " evidence()                      # the full evidence table: each command family "
            "-> its effect on files/tests"
        ),
        " note(text)                      # pin a short fact into memory",
        (
            "Imports are refused in the namespace (no os/subprocess/pathlib): use the tools f"
            "or every file and shell operation."
        ),
        "{domain}",
        "",
        "{protocol}",
    ]
)

TERMINAL_SASE = "\n".join(
    [
        "Reply in exactly these four sections, in this order:",
        ("SITUATION: 1-3 lines -- what the files, the last output and the test status say now."),
        (
            "ANALYSIS: which hypotheses the evidence broke (a refuted change, a test that sti"
            'll fails -- and WHY, from its output), or "none".'
        ),
        "SYNTHESIS: one ```python block with a CHANGE HYPOTHESIS verified by the tests:",
        (
            '  try_change("fix_x", [("patch", "f.py", "old text", "new text")], makes_pass=["'
            'test_x"], note="E2: <what E2 shows>")'
        ),
        '  Write "none" instead of a block only if you must read more first.',
        (
            "EXECUTION: one ```python block of actions: read()/sh() to inspect, apply_change("
            '"fix_x") for a VERIFIED change,'
        ),
        (
            "  run_tests(expect_pass=[...]) with a SPECIFIC prediction, and submit() once the"
            " Must-do tests pass."
        ),
        "Never apply an unverified change; never re-propose a refuted one unchanged.",
    ]
)

TERMINAL_PLAIN = "Reply with at most 5 short lines of reasoning, then EXACTLY ONE ```python block."


# --------------------------------------------------------------------------- sandbox policy
_NET_CMD = re.compile(
    r"(?:^|[\s;&|(`$])(?:curl|wget|ssh|scp|sftp|ftp|telnet|nc|ncat|netcat|socat|ping|nslookup|dig"
    r"|aria2c|lynx|w3m)(?=\s|$|;|\|)",
    re.I,
)
_NET_GIT = re.compile(
    r"\bgit\b[^;&|\n]*\b(clone|fetch|pull|push|ls-remote|submodule|remote\s+add"
    r"|remote\s+set-url)\b",
    re.I,
)
_NET_PKG = re.compile(
    r"\b(pip3?|npm|pnpm|yarn|apt(?:-get)?|apk|brew|choco|winget|cargo|gem|go)"
    r"\s+(install|add|get|update|upgrade|i)\b",
    re.I,
)
_NET_MISC = re.compile(r"/dev/(tcp|udp)/|\b[a-z][a-z0-9+.-]*://", re.I)
_NET_IMPORT = re.compile(
    r"\b(socket|urllib|urllib2|urllib3|requests|httpx|aiohttp|http\.client|ftplib"
    r"|smtplib|telnetlib|websocket|websockets|paramiko)\b"
)
_NET_IMPORT_STMT = re.compile(
    r"^\s*(?:import|from)\s+(socket|urllib|urllib2|urllib3|requests|httpx|aiohttp|http|ftplib|smtplib"
    r"|telnetlib|websocket|websockets|paramiko)\b",
    re.M,
)
_HOME = re.compile(
    r"(?:^|[\s=:'\"(])~(?:[/\s'\"]|$)|\$\{?HOME\b|\$\{?USERPROFILE\b|%USERPROFILE%", re.I
)
_ABS_POSIX = re.compile(r"(?:^|(?<=[\s=:'\"(<>|;&]))(/[^\s'\";&|()<>]*)")
_ABS_WIN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]:[\\/][^\s'\";&|()<>]*)")
_RELTOKEN = re.compile(r"[^\s'\";&|()<>=]*\.\.[^\s'\";&|()<>]*")
_ALLOWED_ABS = ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr")
#: Ways to NAME a path without writing it (review, measured bypasses of the literal checks):
#: the working directory's parent via ``${PWD%/*}`` / ``dirname``, the temp directories,
#: ``$IFS`` word-splitting, octal/hex escapes that spell ``/``, and symlinks out.
_INDIRECT = re.compile(
    r"\$\{?(?:PWD|OLDPWD|TMPDIR|TEMP|TMP|IFS|PATH|SHELL|HOMEDRIVE|HOMEPATH|APPDATA|LOCALAPPDATA)\b"
    r"|\bdirname\b|\brealpath\b|\breadlink\b|\\(?:0?57|x2f)|\bln\s+-\w*s|\bmklink\b|\bcd\s+-(?:\s|$)",
    re.I,
)


def _inside(root: Path, rel: str) -> Optional[Path]:
    """``rel`` resolved against the workspace, or None when it leaves it."""
    try:
        p = (root / str(rel)).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    r = root.resolve()
    if p == r or r in p.parents:
        return p
    return None


def shell_problems(cmd: str, root: Path) -> List[str]:
    """Why ``cmd`` may not run in the sandbox at ``root`` (empty = permitted)."""
    out: List[str] = []
    text = str(cmd)
    if (
        _NET_CMD.search(text)
        or _NET_GIT.search(text)
        or _NET_PKG.search(text)
        or _NET_MISC.search(text)
    ):
        out.append(
            "network: the sandbox has no network (network tools, remote git, installs, URLs)"
        )
    if _NET_IMPORT.search(text) and re.search(r"\b(python3?|py)\b", text):
        out.append("network: inline code imports a network module")
    if _HOME.search(text):
        out.append("path: ~ / $HOME point outside the workspace")
    m = _INDIRECT.search(text)
    if m:
        out.append("path: %r can name a path outside the workspace" % m.group(0))
    for m in _ABS_POSIX.finditer(text):
        tok = m.group(1).rstrip(".,")
        if tok in ("/", "") and m.group(0).strip() in ("/",):
            out.append("path: / is outside the workspace")
            continue
        if not tok or tok in _ALLOWED_ABS or tok.startswith(_ALLOWED_ABS):
            continue
        out.append("path: %s is outside the workspace" % tok)
    for m in _ABS_WIN.finditer(text):
        tok = m.group(1)
        if _inside(root, tok) is None:
            out.append("path: %s is outside the workspace" % tok)
    for m in _RELTOKEN.finditer(text):
        tok = m.group(0)
        if not tok or "://" in tok:
            continue
        if _inside(root, tok) is None:
            out.append("path: %s escapes the workspace" % tok)
    return sorted(set(out))


def sandbox_policy(root: Path) -> Dict[str, Callable[[Any, Any], Any]]:
    """The ``permits()`` policy for kind ``"tool"``: the task sandbox, default deny."""
    from adk.reasoning.solve.context import Decision

    root = Path(root)

    def tool(ctx: Any, req: Any) -> Any:
        name = str(req.name)
        if name not in ACTION_NAMES.values() and name != "try_step":
            return Decision(False, "unknown tool %r" % name, "context", "sandbox_tool")
        for p in req.args.get("paths", ()) or ():
            if _inside(root, str(p)) is None:
                return Decision(
                    False, "path %r is outside the task workspace" % p, "context", "sandbox_path"
                )
        cmd = req.args.get("cmd")
        if cmd is not None:
            probs = shell_problems(str(cmd), root)
            if probs:
                rule = "sandbox_network" if probs[0].startswith("network") else "sandbox_path"
                return Decision(False, "; ".join(probs), "context", rule)
        text = req.args.get("text")
        if text is not None and _NET_IMPORT_STMT.search(str(text)):
            return Decision(
                False,
                "written code imports a network module; the sandbox has no network",
                "context",
                "sandbox_network",
            )
        if name == "apply":
            key = "change:%s" % req.args.get("change", "")
            if not ctx.holds(key):
                return Decision(
                    False,
                    "change %r is not verified in this context (%s): try_change it "
                    "first" % (req.args.get("change"), ctx.status(key)),
                    "context",
                    "change_needs_verification",
                )
        return Decision(True, "inside the task sandbox", "context", "sandbox")

    return {"tool": tool}


# --------------------------------------------------------------------------- records
@dataclass
class Step:
    """One staged action: what was asked, and (after ``act``) what happened."""

    aid: int
    args: Dict[str, Any]
    family: str = ""
    output: str = ""
    exit_class: int = 2
    done: bool = False
    denied: str = ""
    strategy: str = ""
    source: str = ""
    mutated: bool = False
    tests_after: Optional[Dict[str, bool]] = None
    k: int = -1
    evkey: str = ""
    new_evidence: bool = False
    reread: bool = False


def _sha(data: "bytes | str") -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def _summary(text: str) -> str:
    """The already-known summary of a file: its first lines, bounded."""
    lines = text.splitlines()
    head = [ln[:100] for ln in lines[:SUMMARY_LINES]]
    out = " | ".join(head)[:SUMMARY_CHARS]
    if len(lines) > SUMMARY_LINES:
        out += " | (+%d more lines)" % (len(lines) - SUMMARY_LINES)
    return out


def _digest(data: bytes, salt: bytes) -> int:
    return 1 + hashlib.blake2b(data, digest_size=4, key=salt).digest()[0] % 120


def _norm_steps(steps: Any) -> List[Tuple[str, ...]]:
    out: List[Tuple[str, ...]] = []
    if isinstance(steps, (tuple, list)) and steps and isinstance(steps[0], str):
        steps = [steps]
    for s in steps or []:
        if isinstance(s, dict):
            if "patch" in s:
                s = ("patch", s["patch"], s.get("old", ""), s.get("new", ""))
            elif "write" in s:
                s = ("write", s["write"], s.get("text", ""))
            elif "sh" in s:
                s = ("sh", s["sh"])
        s = tuple(str(v) for v in s)
        if not s or s[0] not in ("patch", "write", "sh"):
            raise ValueError(
                "a step is ('patch', path, old, new), ('write', path, text) or ('sh', cmd); "
                "got %r" % (s[:1],)
            )
        need = {"patch": 4, "write": 3, "sh": 2}[s[0]]
        if len(s) != need:
            raise ValueError("step %r needs %d fields, got %d" % (s[0], need, len(s)))
        out.append(s)
    if not out:
        raise ValueError("try_change needs at least one step")
    return out


# --------------------------------------------------------------------------- the environment
class SkillTaskTerminalEnv:
    """A skill task as a terminal Environment (actions in the module docstring)."""

    def __init__(
        self,
        task: "SkillTask | Path | str",
        *,
        max_submits: int = 3,
        work_root: Optional[Path] = None,
        cmd_timeout_s: float = 60.0,
        jail: Optional[str] = None,
        stall_actions: int = STALL_ACTIONS,
    ) -> None:
        self.task = task if isinstance(task, SkillTask) else load_task(Path(task))
        self.ws: Workspace = self.task.materialize(work_root)
        self.root = self.ws.path
        self.max_submits = int(max_submits)
        self.cmd_timeout_s = float(cmd_timeout_s)
        self.rubric = parse_rubric(self.task.root / "tests" / "rubric.md")
        cited = [
            n
            for sec in ("Must-do", "Must-avoid")
            for k, n, _b in self.rubric.citations(sec)
            if k == "test"
        ]
        self.test_names: List[str] = []
        for n in list(self.task.outcome_tests) + cited:
            if n not in self.test_names:
                self.test_names.append(n)
        self.files: List[str] = self._scan_files()
        self.width = max(24, len(self.files) + 12, len(self.test_names) + 8)
        self.steps: List[Step] = []
        self.tests: Dict[str, bool] = {}
        self.trial: Dict[str, bool] = {}
        self.trial_flag = 0
        self.last: Optional[Step] = None
        self.last_verify: Optional[VerifierResult] = None
        self.submits = 0
        self.over = False
        self.won = False
        self.reads: Dict[str, int] = {}
        self.denials: List[Dict[str, str]] = []
        self.changes: Dict[str, Dict[str, Any]] = {}
        self.loop: Any = None
        self._seen = 0
        self.fence_repairs = 0
        self.rollback_failures = 0
        self.mode = "policy"
        self._shell = find_shell()
        self._netns: Optional[List[str]] = None  # probed with the jail, only when needed
        #: ``auto`` (the jail when podman is usable), ``podman`` (required) or ``off``
        self.jail_mode = str(jail or os.environ.get("ADK_SKILLTASK_JAIL") or "auto")
        self._jail: Optional[Jail] = None
        #: the verifier's own jail (private/ and the frozen tests are mounted only there)
        self._vjail: Optional[Jail] = None
        self.verifier_sandbox = "unprobed"  # where tests/test.py runs: "podman" or "host"
        self.sandbox = "unprobed"  # "podman" or POLICY_ONLY once probed
        self.jail_why = ""
        # the evidence ledger: what the model has already observed, keyed by content
        self.known: Dict[str, Dict[str, Any]] = {}  # read path -> sha, t, lines, summary
        self._evidence: set = set()
        self.no_evidence = 0
        self.max_no_evidence = 0
        self.rereads = 0
        self.redelivered = 0  # re-reads that returned the text: its turn had been evicted
        self.stall_actions = max(1, int(stall_actions))
        self.stall_rotations = 0
        self._stall_pending = ""
        from adk.reasoning.solve.context import Context

        self.ctx = Context("skilltask:%s" % self.task.id, intent="terminal")
        self._policy = sandbox_policy(self.root)
        self.invalid = ""
        self._private_snap = _Tree.take(self.ws.private)
        self._tests_hash = _tree_hash(self.task.root / "tests")
        self._scratch = self.ws.run / "scratch"  # the shell's TMPDIR: not private/, not tests/
        self._scratch.mkdir(exist_ok=True)

    # -- Environment ---------------------------------------------------------------------
    def observe(self) -> Obs:
        info: Dict[str, Any] = {"won": self.won} if self.over else {}
        return Obs(self._state(), level=1 if self.won else 0, done=self.over, info=info)

    def act(self, action: Any, source: str = "model") -> Obs:
        aid, k = int(action[0]), int(action[1]) if len(action) > 1 else -1
        if self.over:
            return Obs(self._state(), level=int(self.won), done=True, info={"won": self.won})
        step = self.steps[k] if 0 <= k < len(self.steps) and self.steps[k].aid == aid else None
        if step is None or step.done:
            step = Step(
                aid,
                {},
                family="invalid",
                exit_class=3,
                output="ERROR: act(%d) has no staged arguments -- use the tools (sh, read, write, "
                "patch, run_tests, try_change, apply_change, submit)" % aid,
            )
            step.done = True
            self.steps.append(step)
            self.last = step
            return Obs(self._state(), info={"step": len(self.steps) - 1})
        step.source = str(source)
        # the loop's fallbacks and auto arms act as "explore"/"scan": that is the scripted
        # baseline, whichever PRISM arm happens to be active
        step.strategy = (
            self._strategy()
            if source in ("model", "plan") or self.loop is None
            else "scripted-baseline"
        )
        before = self._digests()
        d = self._permit(step)
        level_up = died = False
        if not d.allowed:
            step.denied = "%s/%s" % (d.layer, d.rule)
            step.output = "DENIED by permits() [%s/%s]: %s" % (d.layer, d.rule, d.reason)
            step.exit_class = 3
            self.denials.append(
                {
                    "action": ACTION_NAMES.get(aid, str(aid)),
                    "rule": d.rule,
                    "reason": d.reason[:200],
                }
            )
        else:
            try:
                if aid == SUBMIT:
                    level_up, died = self._submit(step)
                else:
                    getattr(self, "_do_" + ACTION_NAMES[aid])(step)
            except TaskError as exc:
                step.output, step.exit_class = "ERROR: the verifier could not run: %s" % exc, 3
        step.done = True
        step.mutated = self._digests() != before
        self._book_evidence(step)
        self.last = step
        info: Dict[str, Any] = {"step": k}
        if self.invalid:
            info["invalid"] = self.invalid
        if self.over:
            info["won"] = self.won
        return Obs(
            self._state(),
            level=1 if self.won else 0,
            level_up=level_up,
            died=died,
            done=self.over,
            info=info,
        )

    def available_actions(self) -> List[int]:
        return sorted(ACTION_NAMES)

    def done(self) -> bool:
        return self.over

    # -- staging (the tools' side of an action) ------------------------------------------
    def stage(self, aid: int, **args: Any) -> int:
        fam = ACTION_NAMES[aid]
        if aid == SH:
            fam = "sh:" + (str(args.get("cmd", "")).strip().split() or ["?"])[0][:24]
        elif aid in (READ, WRITE, PATCH):
            fam = "%s:%s" % (fam, str(args.get("path", "")).strip("./") or ".")
        elif aid == TEST:
            prev = next((s.family for s in reversed(self.steps) if s.done and s.mutated), "")
            fam = "test after " + prev if prev else "test"
        elif aid in (TRY, APPLY):
            fam = "%s:%s" % (fam, args.get("name", "?"))
        if aid == READ:  # the scripted arms stage every turn: re-use a pending twin
            for s in reversed(self.steps):
                if not s.done and s.aid == aid and s.args == args:
                    return s.k
        self.steps.append(Step(aid, dict(args), family=fam, k=len(self.steps)))
        return len(self.steps) - 1

    def _call(self, aid: int, expect: Any = None, **args: Any) -> str:
        """One tool call as one action. Through the loop when bound (booked, budgeted,
        predicted), else straight to ``act`` (policies, tests)."""
        loop = self.loop
        if loop is not None and loop.turn_actions >= loop.turn_cap:
            from adk.reasoning.solve._vendor.sandbox import ActionCap

            raise ActionCap("turn action cap %d reached" % loop.turn_cap)
        k = self.stage(aid, **args)
        action = (aid, k, -1)
        if loop is None:
            self.act(action, source="policy")
        else:
            exp = loop._resolve_expect(expect, action) if expect is not None else None
            loop.step(action, "model", expect=exp)
            ns = loop.sandbox.ns
            ns[loop.cfg.state_name] = loop.obs.state.copy()
            self._after_model_action(loop, self.steps[k])
        return self.steps[k].output

    # -- the stall guard ---------------------------------------------------------------------
    def _book_evidence(self, step: Step) -> None:
        """Is ``step`` NEW evidence (a new file or file version, a changed workspace, a
        test result or command output not seen on this workspace)? Keeps the streak of
        actions that added nothing."""
        if step.denied:
            key = "denied:%s:%s" % (step.denied, _sha(repr(sorted(step.args.items(), key=str))))
        elif step.evkey:
            key = step.evkey
        elif step.aid in (TEST, SUBMIT, TRY):
            ws = _sha(repr(sorted((r, _sha(b)) for r, b in self._digests().items())))
            what = repr(sorted((step.tests_after or {}).items()))
            if step.aid == TRY:
                what = repr(step.args.get("steps")) + what
            key = "%s:%s:%s" % (ACTION_NAMES.get(step.aid, "?"), ws, _sha(what))
        else:
            key = "%s:%s" % (ACTION_NAMES.get(step.aid, "invalid"), _sha(step.output))
        step.evkey = key
        step.new_evidence = step.mutated or key not in self._evidence
        self._evidence.add(key)
        self.no_evidence = 0 if step.new_evidence else self.no_evidence + 1
        self.max_no_evidence = max(self.max_no_evidence, self.no_evidence)

    def _after_model_action(self, loop: Any, step: Step) -> None:
        """With PRISM bound: new evidence counts as progress for the loop's intent
        classifier (a read of a new file changes no workspace cell, so the grid alone calls
        it "no new state"); ``stall_actions`` actions without any set intent=stuck and end
        the turn, and the next turn's rotation goes to test-first / try-change."""
        if getattr(loop, "prism", None) is None:
            return
        if step.new_evidence:
            if getattr(loop, "actions_since_novel", 0) > 0:
                loop.actions_since_novel = 0
                loop.turn_novel = getattr(loop, "turn_novel", 0) + 1
            return
        if self.no_evidence < self.stall_actions:
            return
        done = [s for s in self.steps if s.done][-self.stall_actions :]
        fams = sorted({s.family for s in done})
        self._stall_pending = "no new evidence in the last %d actions (%s)" % (
            self.stall_actions,
            ", ".join(fams)[:200],
        )
        self.no_evidence = 0  # one stall per streak
        intents = getattr(loop, "intents", None)
        need = int(getattr(intents, "stuck_actions", 1) or 1)
        loop.actions_since_novel = max(int(getattr(loop, "actions_since_novel", 0)), need)
        from adk.reasoning.solve._vendor.sandbox import TurnEnd

        raise TurnEnd(
            "STALLED: %s. Re-reading what you already have adds nothing: the strategy "
            "rotates -- run_tests(), then verify ONE change with try_change()."
            % self._stall_pending
        )

    # -- tools (namespace) ------------------------------------------------------------------
    def sh(self, cmd: str) -> str:
        return self._call(SH, cmd=str(cmd))

    def read(self, path: str = ".") -> str:
        return self._call(READ, path=str(path))

    def ls(self, path: str = ".") -> str:
        return self._call(READ, path=str(path))

    def write(self, path: str, text: str) -> str:
        return self._call(WRITE, path=str(path), text=str(text))

    def write_bytes(self, path: str, data: bytes) -> str:
        """A byte-exact in-place write (policies only; not a model tool)."""
        return self._call(WRITE, path=str(path), data=bytes(data))

    def patch(self, path: str, old: str, new: str) -> str:
        return self._call(PATCH, path=str(path), old=str(old), new=str(new))

    def run_tests(self, expect_pass: Optional[Sequence[str]] = None) -> str:
        expect = self._expect_tests(1, expect_pass) if expect_pass else None
        return self._call(TEST, expect=expect)

    def try_change(
        self, name: str, steps: Any, makes_pass: Sequence[str] = (), note: str = ""
    ) -> str:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:48] or "change"
        norm = _norm_steps(steps)
        if isinstance(makes_pass, str):
            makes_pass = [makes_pass]
        want = [str(t) for t in makes_pass or ()]
        if not want:
            raise ValueError(
                "try_change needs makes_pass=[test names] -- the claim the tests verify"
            )
        return self._call(
            TRY,
            expect=self._expect_tests(2, want),
            name=name,
            steps=norm,
            makes_pass=want,
            note=str(note)[:160],
        )

    def apply_change(self, name: str) -> str:
        return self._call(APPLY, name=str(name), change=str(name))

    def submit(self) -> str:
        return self._call(SUBMIT)

    def _expect_tests(self, row: int, names: Optional[Sequence[str]]) -> Optional[Dict[str, Any]]:
        cells = {}
        for n in names or ():
            if str(n) in self.test_names:  # a name the verifier never reported is no claim
                cells[(row, self._col(str(n)))] = _PASS
        return {"cells": cells} if cells else None

    # -- executing ---------------------------------------------------------------------------
    def _permit(self, step: Step) -> Any:
        from adk.reasoning.solve.context import Request, permits

        a = step.args
        paths = [a["path"]] if "path" in a else []
        req_args: Dict[str, Any] = {"paths": paths}
        if "cmd" in a:
            req_args["cmd"] = a["cmd"]
        if "text" in a:
            req_args["text"] = a["text"]
        if "new" in a:
            req_args["text"] = a["new"]
        if step.aid == APPLY:
            req_args["change"] = a.get("change", "")
        d = permits(
            self.ctx,
            Request("tool", ACTION_NAMES[step.aid], req_args),
            policy=self._policy,
            default=False,
        )
        if not d.allowed or step.aid not in (TRY, APPLY):
            return d
        steps = (
            a.get("steps")
            if step.aid == TRY
            else (self.changes.get(a.get("name", ""), {}).get("steps") or [])
        )
        for s in steps:  # every step of a change is its own sandbox request
            sub = {"paths": [s[1]]} if s[0] in ("patch", "write") else {"cmd": s[1]}
            if s[0] == "patch":
                sub["text"] = s[3]
            elif s[0] == "write":
                sub["text"] = s[2]
            d = permits(
                self.ctx, Request("tool", "try_step", sub), policy=self._policy, default=False
            )
            if not d.allowed:
                return d
        return d

    def _do_sh(self, step: Step) -> None:
        step.output, step.exit_class = self._run_sh(step.args["cmd"])

    def jail_status(self) -> str:
        """Probe AND START the jail once: ``"podman"`` or ``"policy-only, not a jail"``
        (logged as a warning, and reported by the run harness). A jail that cannot start
        counts as unavailable, so the default ``auto`` falls back to the policy here;
        ``jail="podman"`` makes a missing or unstartable jail an error instead."""
        if self.sandbox != "unprobed":
            return self.sandbox
        try:
            self._jail, self.jail_why = open_jail(self.root, self.jail_mode)
        except JailUnavailableError as exc:
            raise TaskError(
                "the jail is required (jail=%r) but unavailable: %s" % (self.jail_mode, exc)
            ) from exc
        if self._jail is not None:
            # the verifier executes what the model left in the workspace: it gets a jail of
            # its own NOW, or the run is not a jailed run (never "podman" with a host verifier)
            try:
                self._vjail = open_verifier_jail(
                    self._jail, self.ws.private, self.task.root / "tests"
                )
            except JailUnavailableError as exc:
                self._jail.close()
                self._jail = None
                if self.jail_mode.lower() == "podman":
                    raise TaskError(
                        "the jail is required (jail=%r) but the verifier cannot run in one: %s"
                        % (self.jail_mode, exc)
                    ) from exc
                self.jail_why = "the verifier jail could not start: %s" % exc
        if self._jail is not None:
            self.sandbox = self.verifier_sandbox = "podman"
        else:
            self.sandbox = POLICY_ONLY
            self.verifier_sandbox = "host"
            self._netns = _netns_prefix()
            _log.warning(
                "skill-task sandbox for %s is %s: %s -- a program the model writes and runs "
                "(or leaves for the verifier to run) is NOT confined",
                self.task.id,
                POLICY_ONLY.upper(),
                self.jail_why,
            )
        return self.sandbox

    def _jail_broken(self) -> bool:
        """A jail of this episode could not be removed (or its workspace handed over): a
        command may still run in the workspace, so the harness must not touch it by name."""
        return any(j is not None and j.broken for j in (self._jail, self._vjail))

    def _jail_broke(self, exc: Exception) -> TaskError:
        self.invalid = "the jail failed and the episode cannot go on: %s" % exc
        self.over = True
        _log.error("skill-task %s: %s", self.task.id, self.invalid)
        return TaskError("episode invalid: %s" % self.invalid)

    def _run_sh(self, cmd: str) -> Tuple[str, int]:
        if self.jail_status() == "podman" and self._jail is not None:
            try:
                rc, out, err, timed_out = self._jail.run(str(cmd), timeout=self.cmd_timeout_s)
            # the container could not be removed, or the workspace cannot be handed to
            # the jail user: nothing may touch the workspace again. Not scored, and loud.
            except JailBrokenError as exc:
                raise self._jail_broke(exc) from exc
            # the jail started once (jail_status) and a RESTART failed mid-episode: an
            # error, never a silent fallback to the policy mode
            except JailUnavailableError as exc:
                return "ERROR: the jail could not start: %s" % exc, 3
            if timed_out:
                return "ERROR: timed out after %gs\n%s" % (self.cmd_timeout_s, out[-OUTPUT_CAP:]), 1
            text = out + (("\n[stderr]\n" + err) if err else "")
            return ("[exit %d]\n" % rc) + text[-OUTPUT_CAP:], 0 if rc == 0 else 1
        if self._shell is None:
            return "ERROR: no POSIX shell on this host (set ADK_SKILLTASK_SHELL)", 3
        env = dict(os.environ)
        for var in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        ):
            env[var] = "http://127.0.0.1:9"
        env["no_proxy"] = env["NO_PROXY"] = ""
        env["HOME"] = str(self.root)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        for var in ("TMPDIR", "TEMP", "TMP"):  # never the parent of the task's run directory
            env[var] = str(self._scratch)
        try:
            proc = subprocess.run(
                (self._netns or []) + self._shell + [str(cmd)],
                cwd=str(self.root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.cmd_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return "ERROR: timed out after %gs" % self.cmd_timeout_s, 1
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return ("[exit %d]\n" % proc.returncode) + out[
            -OUTPUT_CAP:
        ], 0 if proc.returncode == 0 else 1

    def _do_read(self, step: Step) -> None:
        p = _inside(self.root, step.args["path"])
        assert p is not None  # permits() already refused an escape
        rel = p.relative_to(self.root.resolve()).as_posix() if p != self.root.resolve() else "."
        try:
            if p.is_dir():
                text, kind = self._tree(p), "dir"
            elif not _is_regular(p):  # a FIFO or device would block or misbehave
                raise OSError("not a regular file")
            else:
                text, kind = p.read_text(encoding="utf-8", errors="replace"), "file"
                self.reads[rel] = self.reads.get(rel, 0) + 1
        except OSError as exc:
            step.output, step.exit_class = "ERROR: %s" % exc, 1
            return
        sha = _sha(text)
        step.evkey = "%s:%s:%s" % (kind, rel, sha)
        step.family = "read:%s@%s" % (rel.strip("./") or ".", sha[:8])
        step.exit_class = 0
        seen = self.known.get(rel)
        if seen is not None and seen["sha"] == sha and not self._in_context(seen, text):
            # same bytes, but the turn that carried them left the loop's working memory:
            # the model cannot see the text any more, so this read DELIVERS it again and
            # is evidence (never a no-evidence action that feeds the stall guard)
            self.redelivered += 1
            step.evkey += ":again@%d" % self.redelivered
            seen = None
        if seen is not None and seen["sha"] == sha:  # the evidence table already has it
            step.reread = True
            self.rereads += 1
            step.output = "UNCHANGED since t%d (sha %s, %d lines): you already read %s. %s: %s" % (
                seen["t"],
                sha[:8],
                seen["lines"],
                rel,
                "listing" if kind == "dir" else "summary",
                text[:1500] if kind == "dir" else seen["summary"],
            )
            return
        step.output = text if kind == "dir" else text[: OUTPUT_CAP * 2]
        if len(step.output) < len(text):
            return  # only part of it was shown: a re-read must show it again, never "unchanged"
        self.known[rel] = {
            "sha": sha,
            "t": sum(1 for s in self.steps if s.done) + 1,
            "lines": len(text.splitlines()),
            "summary": _summary(text) if kind == "file" else "%d entries" % len(text.splitlines()),
            "kind": kind,
            "k": step.k,
            "turn": self._turn(),
        }

    def _turn(self) -> int:
        return int(getattr(self.loop, "turn", 0) or 0) if self.loop is not None else 0

    def _in_context(self, info: Dict[str, Any], text: str) -> bool:
        """Can the model still SEE the text of the read recorded in ``info``? Without a
        loop the caller holds the returned text. With one, the text is visible when it was
        printed this turn (it is in the next prompt), or is literally in the pending
        result or in a turn the working memory still holds. ``WorkingMemory.evict_to``
        drops whole turns (``wm_tokens``, and again to fit ``ctx_tokens``), so this asks
        the memory itself instead of assuming an earlier result is still there."""
        loop = self.loop
        if loop is None:
            return True
        if info.get("turn") == self._turn():
            return True
        probe = text.rstrip()
        if not probe:
            return True
        if probe in str(getattr(loop, "last_result", "") or ""):
            return True
        wm = getattr(loop, "wm", None)
        try:
            msgs = list(wm.messages()) if wm is not None else []
        except (AttributeError, TypeError):
            msgs = []
        return any(probe in str(m.get("content", "")) for m in msgs if isinstance(m, dict))

    def _write_text(self, rel: str, text: str) -> str:
        p = _inside(self.root, rel)
        assert p is not None
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+" if p.is_file() else "w"
        with open(p, mode, encoding="utf-8", newline="\n") as fh:  # same file, same inode
            fh.seek(0)
            fh.write(text)
            fh.truncate()
        return "wrote %d chars to %s (in place)" % (len(text), rel)

    def _do_write(self, step: Step) -> None:
        try:
            if "data" in step.args:
                p = _inside(self.root, step.args["path"])
                assert p is not None
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "r+b" if p.is_file() else "wb") as fh:
                    fh.seek(0)
                    fh.write(step.args["data"])
                    fh.truncate()
                step.output, step.exit_class = (
                    "wrote %d bytes to %s" % (len(step.args["data"]), step.args["path"]),
                    0,
                )
                return
            step.output, step.exit_class = self._write_text(step.args["path"], step.args["text"]), 0
        except OSError as exc:
            step.output, step.exit_class = "ERROR: %s" % exc, 1

    def _patch_text(self, rel: str, old: str, new: str) -> Tuple[str, int]:
        p = _inside(self.root, rel)
        assert p is not None
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            return "ERROR: %s" % exc, 1
        n = text.count(old) if old else 0
        if n != 1:
            return (
                "ERROR: patch needs exactly one occurrence of old in %s, found %d%s"
                % (rel, n, " (old is empty)" if not old else "")
            ), 1
        self._write_text(rel, text.replace(old, new, 1))
        return "patched %s (%+d chars)" % (rel, len(new) - len(old)), 0

    def _do_patch(self, step: Step) -> None:
        a = step.args
        step.output, step.exit_class = self._patch_text(a["path"], a["old"], a["new"])

    def _guard(self) -> None:
        """The verifier's inputs outside the workspace must be what the host left there.
        The shell filter is a policy, not a jail (a program the model writes and runs can
        reach a sibling directory), so the grading inputs are CHECKED before every
        verification: the private state against its snapshot, the frozen tests against
        their hash. Tampering ends the episode as invalid; it is never scored."""
        if _Tree.take(self.ws.private) != self._private_snap:
            self._private_snap.restore(self.ws.private)
            self.invalid = "the verifier's private state was modified from the sandbox"
        elif _tree_hash(self.task.root / "tests") != self._tests_hash:
            self.invalid = "the task's frozen tests were modified from the sandbox"
        if self.invalid:
            self.over = True
            raise TaskError("episode invalid: %s" % self.invalid)

    def _verifier_in_jail(self, ws: Workspace) -> "subprocess.CompletedProcess[str]":
        """``tests/test.py`` inside the verifier jail (the :meth:`Workspace.verify` runner)."""
        assert self._vjail is not None
        cmd = "python3 test.py /work %s" % VERIFY_PRIVATE
        timeout = self.task.timeout_s
        try:
            rc, out, err, timed_out = self._vjail.run(cmd, timeout=timeout, cwd=VERIFY_TESTS)
        except JailBrokenError as exc:
            raise self._jail_broke(exc) from exc
        except (JailUnavailableError, OSError, subprocess.SubprocessError) as exc:
            # a RESTART failed mid-episode: no score, and never a host run instead
            raise TaskError(
                "%s: the verifier jail could not start, so the task was not verified: %s"
                % (self.task.id, exc)
            ) from exc
        if timed_out:
            return subprocess.CompletedProcess(
                cmd, 124, stdout=out, stderr=err or "timeout after %gs" % timeout
            )
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def _run_verifier(self) -> VerifierResult:
        """The frozen verifier, wherever this episode's sandbox puts it. It executes files
        the model wrote (a checker under test, a repository's ``.git/config``), so in a
        jailed run it runs in the verifier jail and NEVER as a host process; only a run
        already marked policy-only verifies on the host. Either way ``private/`` is put
        back to what setup left once the verifier returns: its scratch files are dropped,
        and so is anything workspace code wrote there while the verifier ran it."""
        try:
            if self.jail_status() == "podman":
                if self._vjail is None:  # unreachable by construction; refuse, never the host
                    raise TaskError("%s: jailed run without a verifier jail" % self.task.id)
                return self.ws.verify(runner=self._verifier_in_jail)
            return self.ws.verify()
        finally:
            try:
                # a jail that could not be removed may still run: its mounts are not touched
                left = [] if self._jail_broken() else self._private_snap.restore(self.ws.private)
            except OSError as exc:
                left = [str(exc)]
            if left:  # the next verification would read state the host did not write
                self.invalid = "the verifier's private state could not be restored (%s)" % (
                    ", ".join(left[:5])
                )
                self.over = True
                raise TaskError("episode invalid: %s" % self.invalid)

    def _verify(self) -> VerifierResult:
        self._guard()
        res = self._run_verifier()
        for n in res.tests:
            self._col(n)
        self.last_verify = res
        return res

    def _do_test(self, step: Step) -> None:
        res = self._verify()
        self.tests = dict(res.tests)
        step.tests_after = dict(res.tests)
        step.output = self._test_text(res)
        step.exit_class = 0 if res.reward == 1.0 else 1

    def _apply_steps(self, steps: List[Tuple[str, ...]]) -> Tuple[List[str], bool]:
        out, ok = [], True
        for s in steps:
            if s[0] == "patch":
                txt, code = self._patch_text(s[1], s[2], s[3])
            elif s[0] == "write":
                txt, code = self._write_text(s[1], s[2]), 0
            else:
                txt, code = self._run_sh(s[1])
            out.append("%s: %s" % (s[0], txt[:400]))
            ok = ok and code == 0
        return out, ok

    def _do_try(self, step: Step) -> None:
        a = step.args
        snap = _Tree.take(self.root)
        try:
            outs, ok = self._apply_steps(a["steps"])
            res = self._verify()
        finally:
            # measured: git marks its objects read-only on Windows, a plain unlink failed
            # half-way and a TRIED commit survived the rollback. Restore, then PROVE it.
            # (a jail that could not be removed may still run in the workspace: the
            # episode is already invalid and the harness must not write there by name)
            left = [] if self._jail_broken() else snap.restore(self.root)
            if left:  # an unverified change is live: the episode cannot be scored honestly
                self.rollback_failures += 1
                self.invalid = "rollback left %d path(s) changed" % len(left)
                self.over = True
                raise TaskError(
                    "rollback after try_change left %d path(s) changed: %s"
                    % (len(left), ", ".join(left[:5]))
                )
        self.trial = dict(res.tests)
        step.tests_after = dict(res.tests)
        want = a["makes_pass"]
        failing = [t for t in want if not res.tests.get(t, False)]
        unknown = [t for t in want if t not in res.tests]
        verified = ok and not failing
        self.trial_flag = 2 if verified else 1
        if verified:
            reason = "every named test passed: %s" % ", ".join(want)
        elif not ok:
            reason = (
                "a step failed: "
                + " | ".join(o for o in outs if "ERROR" in o or "[exit" in o)[:300]
            )
        else:
            reason = "still failing: %s%s" % (
                ", ".join(failing),
                (" (the verifier has no test %s)" % ", ".join(unknown)) if unknown else "",
            )
        self.changes[a["name"]] = {
            "steps": a["steps"],
            "makes_pass": want,
            "verified": verified,
            "reason": reason,
            "note": a.get("note", ""),
        }
        from adk.reasoning.solve.context import Evidence

        if verified:
            self.ctx.widen(
                "change:" + a["name"],
                want,
                evidence=(Evidence("test", "try#%d" % step.k, reason[:120]),),
                clock=len(self.steps),
            )
        else:
            self.ctx.narrow(
                "change:" + a["name"],
                Evidence("test", "try#%d" % step.k, reason[:120]),
                clock=len(self.steps),
            )
        self._book_hypothesis(a["name"], a["steps"], want, verified, reason, a.get("note", ""))
        step.output = (
            "TRY %s: %s -- %s\n%s\n(the workspace was rolled back; apply_change(%r) to keep a "
            "VERIFIED change)"
            % (
                a["name"],
                "VERIFIED" if verified else "REFUTED",
                reason,
                self._test_text(res),
                a["name"],
            )
        )
        step.exit_class = 0 if verified else 1

    def _do_apply(self, step: Step) -> None:
        ch = self.changes.get(step.args["name"])
        if ch is None or not ch["verified"]:
            step.output, step.exit_class = "ERROR: no verified change %r" % step.args["name"], 1
            return
        outs, ok = self._apply_steps(ch["steps"])
        step.output = "APPLIED %s:\n%s" % (step.args["name"], "\n".join(outs))
        step.exit_class = 0 if ok else 1

    def _submit(self, step: Step) -> Tuple[bool, bool]:
        self.submits += 1
        res = self._verify()
        self.tests = dict(res.tests)
        step.tests_after = dict(res.tests)
        if res.reward == 1.0:
            self.won = self.over = True
            step.output, step.exit_class = "SUBMIT accepted: every test passed", 0
            return True, False
        died = self.submits >= self.max_submits
        self.over = died
        step.output = "SUBMIT rejected (%d/%d submits used):\n%s" % (
            self.submits,
            self.max_submits,
            self._test_text(res),
        )
        step.exit_class = 1
        return False, died

    # -- hypothesis bookkeeping (the loop's store; kind "change") -----------------------------
    def _book_hypothesis(
        self,
        name: str,
        steps: List[Tuple[str, ...]],
        want: List[str],
        ok: bool,
        reason: str,
        note: str,
    ) -> None:
        loop = self.loop
        if loop is None:
            return
        from adk.reasoning.solve._vendor.memory import Hypothesis

        hs = loop.hyps
        src = "try_change(%r, %r, makes_pass=%r)" % (name, steps, want)
        h = Hypothesis(
            name=name,
            kind="change",
            source=src[:2000],
            fn=None,
            note=note[:120],
            status="verified" if ok else "refuted",
            support=len(want),
            reason="" if ok else reason[:160],
            turn=int(getattr(loop, "turn", 0)),
        )
        hs.n_proposed += 1
        if ok:
            hs.active[name] = h
            hs.n_verified_ever.add(name)
            hs.verified_origin[name] = "model"
        else:
            hs.active.pop(name, None)
            hs.refuted.append(h)
            hs.n_refuted += 1
            loop.turn_refutations.append("%s: %s" % (name, reason[:200]))

    # -- state ------------------------------------------------------------------------------
    def _col(self, name: str) -> int:
        if name not in self.test_names:
            self.test_names.append(name)
        return min(1 + self.test_names.index(name), self.width - 1)

    def _scan_files(self) -> List[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and ".git" not in p.relative_to(self.root).parts:
                out.append(p.relative_to(self.root).as_posix())
        if any(self.root.rglob(".git")):
            out.append("<git>")
        return out

    def _digests(self) -> Dict[str, bytes]:
        # The workspace is written by the model and read here by the harness, which may be
        # root. A link is recorded as a link and never followed, and only regular files are
        # read (never a FIFO, which would block): a planted link must not make the harness
        # read or walk host files (security review of #10876, follow-up 1).
        cur: Dict[str, bytes] = {}
        if self._jail_broken():  # a command may still be running in the workspace
            return cur
        git_dirs: List[Path] = []
        for p, st in _entries(self.root):
            rel = p.relative_to(self.root)
            if ".git" in rel.parts:
                if p.name == ".git" and stat.S_ISDIR(st.st_mode):
                    git_dirs.append(p)
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    cur[rel.as_posix()] = b"<link:" + os.fsencode(os.readlink(p)) + b">"
                except OSError:
                    cur[rel.as_posix()] = b"<link:?>"
            elif stat.S_ISREG(st.st_mode):
                cur[rel.as_posix()] = _read_regular(p)
        g = b""
        for gd in sorted(git_dirs):
            for part in ["HEAD", "index", "packed-refs"]:
                f = gd / part
                if _is_regular(f):
                    g += part.encode() + _read_regular(f)
            refs = gd / "refs"
            if _is_dir_not_link(refs):
                for f, st in sorted(_entries(refs)):
                    if stat.S_ISREG(st.st_mode):
                        g += f.relative_to(gd).as_posix().encode() + _read_regular(f)
        if g:
            cur["<git>"] = g
        return cur

    def _state(self) -> Any:
        grid = [[0] * self.width for _ in range(ROWS)]
        last = self.last
        known = [n for n in self.test_names if n in self.tests]
        grid[0][:6] = [
            last.exit_class if last is not None else 2,
            1 if self.tests else 0,
            min(self.submits, 100),
            min(sum(1 for v in self.tests.values() if v), 100),
            min(len(known), 100),
            self.trial_flag,
        ]
        for n, v in self.tests.items():
            grid[1][self._col(n)] = _PASS if v else _FAIL
        for n, v in self.trial.items():
            grid[2][self._col(n)] = _PASS if v else _FAIL
        cur = self._digests()
        for rel in cur:
            if rel not in self.files:
                self.files.append(rel)
        for i, rel in enumerate(self.files):
            col = min(i, self.width - 1)
            data = cur.get(rel)
            if data is None:
                continue
            grid[3][col] = _digest(data, b"a")
            grid[4][col] = _digest(data, b"b")
        try:
            import numpy as np

            return np.array(grid, dtype=np.int8)
        except ImportError:
            return tuple(tuple(r) for r in grid)

    # -- optional hooks (adk.reasoning.solve HOOK_ARGS) ------------------------------------
    def needs_xy(self, action_id: int) -> bool:
        return False

    def primer(self) -> str:
        return (
            "TASK KIND: terminal/file task (%s). You have %d submits; a passing submit ends the "
            "task, the verifier also grades the workspace as you leave it."
            % (self.task.id, self.max_submits)
        )

    def render(self, obs: Any, last: Any) -> str:
        self._stall_pending = ""  # the turn's rotation (if any) has already happened
        parts = ["TASK:\n" + self.task.instruction.strip()]
        for sec in ("Must-do", "Must-avoid"):
            items = self.rubric.items.get(sec) or []
            if items:
                parts.append("RUBRIC %s:\n" % sec + "\n".join("- " + b for b in items))
        parts.append("WORKSPACE FILES:\n" + self._tree(self.root, limit=60))
        read_lines = self._known_lines()
        if read_lines:
            parts.append(
                "FILES YOU ALREADY READ (a re-read of an unchanged file whose text is still in "
                "your earlier results returns only this line; one marked 'no longer in your "
                "context' returns its text again):\n" + "\n".join(read_lines)
            )
        unseen = [s for s in self.steps[self._seen :] if s.done and s.source != "model"]
        self._seen = len(self.steps)
        if unseen:  # the scripted arms act without the model: show it what they found
            budget, lines = 4000, []
            for s in unseen:
                chunk = "[%s] %s" % (s.family, s.output[:900])
                if budget - len(chunk) < 0:
                    lines.append("(+%d more actions)" % (len(unseen) - len(lines)))
                    break
                budget -= len(chunk)
                lines.append(chunk)
            parts.append(
                "ACTIONS TAKEN WITHOUT YOU (scripted baseline), with their output:\n"
                + "\n".join(lines)
            )
        if self.last is not None:
            s = self.last
            parts.append(
                "LAST ACTION: %s -> %s\n%s"
                % (
                    s.family,
                    {0: "ok", 1: "failed", 2: "-", 3: "DENIED/ERROR"}.get(s.exit_class, "?"),
                    s.output[-4000:],
                )
            )
        if self.tests:
            passing = sorted(n for n, v in self.tests.items() if v)
            failing = sorted(n for n, v in self.tests.items() if not v)
            parts.append(
                "TESTS (last run): %d/%d pass. FAILING: %s. passing: %s"
                % (
                    len(passing),
                    len(self.tests),
                    ", ".join(failing) or "none",
                    ", ".join(passing) or "none",
                )
            )
        else:
            parts.append("TESTS: not run yet -- run_tests()")
        if self.changes:
            parts.append(
                "CHANGES: "
                + "; ".join(
                    "%s %s" % (n, "VERIFIED" if c["verified"] else "REFUTED")
                    for n, c in list(self.changes.items())[-6:]
                )
            )
        parts.append(
            "submits %d/%d; actions so far %d"
            % (self.submits, self.max_submits, sum(1 for s in self.steps if s.done))
        )
        return "\n\n".join(parts)

    def _known_lines(self, limit: int = 30) -> List[str]:
        out = []
        broken = self._jail_broken()
        for rel, info in list(self.known.items())[-limit:]:
            if info.get("kind") != "file":
                continue
            p = self.root / rel
            # Runs every turn as the harness user: never through a link (security review
            # of #10882), and not at all once a jail could not be removed.
            text = None
            if not broken and _is_regular(p):
                text = _read_regular(p).decode("utf-8", errors="replace")
                # the same text _do_read hashed: read_text() turns CRLF and CR into LF
                text = text.replace("\r\n", "\n").replace("\r", "\n")
            now = _sha(text) if text is not None else ""
            if now != info["sha"]:
                state = "DELETED since" if not now else "CHANGED since -- read it again"
            elif self._in_context(info, text or ""):
                state = "unchanged"
            else:
                state = "unchanged, text no longer in your context -- read() returns it again"
            out.append(
                "- %s  (t%d, %d lines, sha %s, %s): %s"
                % (rel, info["t"], info["lines"], info["sha"][:8], state, info["summary"][:160])
            )
        return out

    def describe(self, t: Any) -> str:
        k = int(t.action[1])
        if not 0 <= k < len(self.steps):
            return "invalid action"
        s = self.steps[k]
        bits = ["%s -> %s" % (s.family, {0: "ok", 1: "failed", 3: "denied"}.get(s.exit_class, "-"))]
        eff = self.effects(t) or []
        if eff:
            bits.append(", ".join("%s %+d" % (c, d[0] or d[1]) for c, d in eff[:6]))
        return "; ".join(bits)

    def family(self, t: Any) -> str:
        k = int(t.action[1])
        return self.steps[k].family if 0 <= k < len(self.steps) else "invalid"

    def effects(self, t: Any) -> List[Tuple[str, Tuple[int, int]]]:
        """Per-transition effects for the evidence table: tests flipping (dy = +1 pass /
        -1 fail) on the workspace (row 1) or in a trial (row 2), files changing (dx = 1)."""
        b, a = t.before, t.after
        out: List[Tuple[str, Tuple[int, int]]] = []
        for row, label in ((1, "test"), (2, "trial")):
            for i, name in enumerate(self.test_names[: self.width - 1]):
                x, y = int(b[row][1 + i]), int(a[row][1 + i])
                if x != y and y in (_PASS, _FAIL):
                    out.append(("%s:%s" % (label, name), (1 if y == _PASS else -1, 0)))
        for i, rel in enumerate(self.files[: self.width]):
            if int(b[3][i]) != int(a[3][i]) or int(b[4][i]) != int(a[4][i]):
                out.append(("file:%s" % rel, (0, 1)))
        return out

    def state_key(self, state: Any) -> str:
        try:
            body = state[1:].tobytes()
        except AttributeError:
            body = repr(state[1:]).encode()
        return hashlib.blake2b(body, digest_size=8).hexdigest()

    def significant_change(self, before: Any, after: Any) -> bool:
        try:
            return bool((before[1:] != after[1:]).any())
        except AttributeError:
            return before[1:] != after[1:]

    def candidates(self) -> List[Tuple[int, int, int]]:
        out = []
        for rel in self._unread()[:10]:
            out.append((READ, self.stage(READ, path=rel), -1))
        if not self.tests:
            out.append((TEST, self.stage(TEST), -1))
        return out

    def auto_action(self) -> Optional[Tuple[int, int, int]]:
        """The scripted baseline: run the tests once, then read every file once."""
        if not self.tests and not any(s.aid == TEST and s.done for s in self.steps):
            return (TEST, self.stage(TEST), -1)
        todo = self._unread()
        if todo:
            return (READ, self.stage(READ, path=todo[0]), -1)
        return None

    def handoff(self, n: int) -> Dict[str, Any]:
        k = 0
        for _ in range(max(0, int(n))):
            a = self.auto_action()
            if a is None:
                break
            self.act(a, source="policy")
            k += 1
        return {"actions": k}

    def tools(self, loop: Any) -> Dict[str, Tuple[Callable[..., Any], str]]:
        """The terminal tools; with a loop, also BIND it: tools act through
        ``loop.step`` (booked, budgeted, predicted), the system prompt becomes the
        terminal one, PRISM's arms are shown as the terminal strategies, and the grid
        ``plan()`` is replaced (there is no simulator to plan in)."""
        self.loop = loop
        if loop is not None:
            self._bind(loop)

        def echo(fn: Callable[..., str]) -> Callable[..., str]:
            def wrapped(*a: Any, **k: Any) -> str:
                n = len(self.steps)
                out = fn(*a, **k)
                self._emit(
                    out
                    if len(out) <= ECHO_CAP
                    else out[:ECHO_CAP] + "\n[... %d more chars]" % (len(out) - ECHO_CAP),
                    self.steps[n:],
                )
                return out

            wrapped.__name__ = fn.__name__
            return wrapped

        def plan(*_a: Any, **_k: Any) -> Dict[str, Any]:
            return {
                "ok": False,
                "reason": "no plan() in terminal tasks: verify a change with "
                "try_change() and apply_change() it",
            }

        return {
            "sh": (echo(self.sh), "sh(cmd)"),
            "read": (echo(self.read), "read(path)"),
            "ls": (echo(self.ls), "ls(path='.')"),
            "write": (echo(self.write), "write(path, text)"),
            "patch": (echo(self.patch), "patch(path, old, new)"),
            "run_tests": (echo(self.run_tests), "run_tests(expect_pass=None)"),
            "try_change": (echo(self.try_change), "try_change(name, steps, makes_pass, note='')"),
            "apply_change": (echo(self.apply_change), "apply_change(name)"),
            "submit": (echo(self.submit), "submit()"),
            "plan": (plan, "not available"),
        }

    def _emit(self, text: str, new_steps: Sequence[Step]) -> None:
        """Print a tool's result where the MODEL sees it: the loop sandbox's capped stdout.
        Measured: the builtin print() went to the host process, so no tool output ever
        reached the model (1 of 58 SASE turn results carried any stdout) and it re-read the
        same files. A read whose text was cut by the stdout cap was NOT delivered: it is
        forgotten, so the next read returns the text instead of "unchanged"."""
        sb = getattr(self.loop, "sandbox", None) if self.loop is not None else None
        pr = getattr(sb, "_print", None)
        if pr is None:
            print(text)
            return
        pr(text)
        cap = getattr(sb, "_out", None)
        if cap is not None and getattr(cap, "capped", False):
            for s in new_steps:
                if s.aid == READ and not s.reread:
                    for rel, info in list(self.known.items()):
                        if info.get("k") == s.k:
                            del self.known[rel]
                    self._evidence.discard(s.evkey)

    def _bind(self, loop: Any) -> None:
        cfg = loop.cfg
        self.mode = "sase" if getattr(cfg, "sase", False) else "plain"
        domain = str(loop._hook("primer", default="") or "")
        loop.system = TERMINAL_SYSTEM.format(
            domain=domain, protocol=TERMINAL_SASE if self.mode == "sase" else TERMINAL_PLAIN
        )
        llm = getattr(loop, "llm", None)
        if llm is not None and hasattr(llm, "chat"):
            orig_chat = llm.chat

            def chat(*a: Any, **k: Any) -> Any:
                # measured: bonsai2-27b ends replies on the closing fence, so the reply has an
                # unterminated ```python block the core cannot parse ("no python block") and
                # the turn is lost. Close it; count every repair.
                r = orig_chat(*a, **k)
                c = str(getattr(r, "content", "") or "")
                if c.count("```") % 2 == 1:
                    r.content = c.rstrip() + "\n```\n"
                    self.fence_repairs += 1
                return r

            llm.chat = chat
        prism = getattr(loop, "prism", None)
        if prism is None:
            return
        orig = prism.overlay

        def overlay(extra: str = "") -> str:
            ts = TERMINAL_STRATEGIES.get(prism.active.id)
            if ts is None:
                return orig(extra)
            lines = ["=== STRATEGY: %s -- %s ===" % (ts.name, ts.principle)]
            lines += ["- " + h for h in ts.heuristics]
            if ts.avoid:
                lines.append("Avoid: " + "; ".join(ts.avoid))
            if prism.diagnoses:
                lines.append("Strategies that already stalled (do NOT repeat their approach):")
                for d in prism.diagnoses[-3:]:
                    name = TERMINAL_STRATEGIES.get(d.strategy)
                    lines.append("- [%s] %s" % (name.name if name else d.strategy, d.summary))
            return "\n".join(lines)

        prism.overlay = overlay
        orig_rotate = prism.rotate

        def rotate(diagnosis: Any) -> Any:
            # a stall on evidence (re-reads, repeated outputs) rotates to the strategies
            # that GENERATE evidence: test-first, or from it minimal-patch (try_change)
            why, old = self._stall_pending, prism.active.id
            if why:
                try:
                    diagnosis.summary = why + "; " + diagnosis.summary
                except AttributeError:
                    _log.debug("diagnosis has no summary to annotate")
            st = orig_rotate(diagnosis)
            if why:
                self._stall_pending = ""
                from adk.reasoning.solve._vendor.prism import BY_ID

                target = "rule_first" if old != "rule_first" else "analogy"
                if target in getattr(prism, "allowed", [target]):
                    prism.active = st = BY_ID[target]
                    if prism.log:
                        prism.log[-1]["to"] = target
                self.stall_rotations += 1
            return st

        prism.rotate = rotate

    # -- helpers ------------------------------------------------------------------------------
    def _strategy(self) -> str:
        loop = self.loop
        if loop is None:
            return "policy"
        prism = getattr(loop, "prism", None)
        if prism is None:
            return "plain"
        ts = TERMINAL_STRATEGIES.get(prism.active.id)
        return ts.name if ts else prism.active.id

    def _unread(self) -> List[str]:
        if self._jail_broken():
            return []
        sizes: Dict[str, int] = {}
        for f in self.files:
            if f == "<git>":
                continue
            try:
                st = os.lstat(self.root / f)  # a link is not a file the model may read
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                sizes[f] = st.st_size
        files = sorted(sizes, key=lambda f: (self.reads.get(f, 0), sizes[f]))
        return [f for f in files if not self.reads.get(f)]

    def _tree(self, base: Path, limit: int = 200) -> str:
        rows = []
        for q, st in sorted(_entries(base)):  # never through a link (see _digests)
            rel = q.relative_to(self.root)
            if ".git" in rel.parts:
                if q.name == ".git":
                    rows.append(rel.as_posix() + "/ (git repository)")
                continue
            if stat.S_ISLNK(st.st_mode):
                rows.append(rel.as_posix() + " (link)")
            elif stat.S_ISDIR(st.st_mode):
                rows.append(rel.as_posix() + "/")
            else:
                rows.append(rel.as_posix() + " (%d B)" % st.st_size)
        if len(rows) > limit:
            rows = rows[:limit] + ["(+%d more)" % (len(rows) - limit)]
        return "\n".join(rows) or "(empty)"

    def _test_text(self, res: VerifierResult) -> str:
        fail = sorted(n for n, v in res.tests.items() if not v)
        ok = sorted(n for n, v in res.tests.items() if v)
        return "tests: %d/%d pass; reward %.2f. FAILING: %s. passing: %s" % (
            len(ok),
            len(res.tests),
            res.reward,
            ", ".join(fail) or "none",
            ", ".join(ok) or "none",
        )

    # -- scoring --------------------------------------------------------------------------------
    def final(self) -> VerifierResult:
        """The frozen verifier on the workspace as it stands: THE score. An invalid episode
        (tampered grading inputs, a rollback that left a change live) raises instead."""
        if self.invalid:
            raise TaskError("episode invalid, not scored: %s" % self.invalid)
        self._guard()
        return self._run_verifier()

    def rubric_report(self, res: Optional[VerifierResult] = None) -> Dict[str, Any]:
        """Secondary report, never reward: each Must-do / Must-avoid bullet with the status
        of the tests it cites (probes are not trajectory-checkable and are listed as such)."""
        res = res or self.final()
        out: Dict[str, Any] = {}
        for sec in ("Must-do", "Must-avoid"):
            rows = []
            for bullet in self.rubric.items.get(sec, []):
                cites = re.findall(r"\[(test|probe):\s*([A-Za-z0-9_\-]+)\]", bullet)
                tests = {n: res.tests.get(n) for k, n in cites if k == "test"}
                rows.append(
                    {
                        "bullet": bullet[:100],
                        "tests": tests,
                        "met": bool(tests) and all(v is True for v in tests.values()),
                        "probes": [n for k, n in cites if k == "probe"],
                    }
                )
            out[sec] = {"met": sum(1 for r in rows if r["met"]), "total": len(rows), "items": rows}
        return out

    def solved_by(self, final_passed: bool = False) -> Optional[str]:
        """The strategy active on the last mutating action before the workspace first
        tested (or submitted) all-green. A run that never observed green but whose FINAL
        workspace passes (``final_passed``) is credited to its last mutating action (a
        model that fixed the task and stopped without re-testing). None otherwise."""
        last_mut = None
        for s in self.steps:
            if not s.done:
                continue
            if s.mutated and s.aid in _MUTATING:
                last_mut = s.strategy
            if s.aid in (TEST, SUBMIT) and s.tests_after and all(s.tests_after.values()):
                return last_mut or s.strategy
        return last_mut if final_passed else None

    def trace(self) -> List[Dict[str, Any]]:
        return [
            {
                "family": s.family,
                "exit": s.exit_class,
                "strategy": s.strategy,
                "mutated": s.mutated,
                "denied": s.denied,
                "new_evidence": s.new_evidence,
                "reread": s.reread,
            }
            for s in self.steps
            if s.done
        ]

    def close(self) -> None:
        for jail in (self._jail, self._vjail):
            if jail is not None:
                jail.close()
        self.ws.cleanup()


# --------------------------------------------------------------------------- snapshots
def _snapshot(root: Path) -> Dict[str, bytes]:
    snap: Dict[str, bytes] = {}
    for p in root.rglob("*"):
        if p.is_file():
            snap[p.relative_to(root).as_posix()] = p.read_bytes()
    return snap


def _writable(p: Path) -> None:
    try:
        os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE)
    except OSError as exc:  # the write that follows reports the real failure
        _log.debug("could not make %s writable: %s", p, exc)


@dataclass
class _Tree:
    """A byte-exact picture of a directory: files (bytes + mode), directories, and every
    other entry (a symlink with its target, a FIFO...) so a planted link cannot survive a
    rollback unseen. Walks with ``followlinks=False``: a link is an entry, never a door."""

    files: Dict[str, Tuple[bytes, int]]
    dirs: List[str]
    others: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def take(cls, root: Path) -> "_Tree":
        files: Dict[str, Tuple[bytes, int]] = {}
        dirs: List[str] = []
        others: Dict[str, str] = {}
        for base, dnames, fnames in os.walk(root, followlinks=False):
            for name in dnames + fnames:
                p = Path(base) / name
                rel = p.relative_to(root).as_posix()
                if p.is_symlink():
                    try:
                        others[rel] = "link:" + os.readlink(p)
                    except OSError:
                        others[rel] = "link:?"
                elif p.is_dir():
                    dirs.append(rel)
                elif p.is_file():
                    files[rel] = (p.read_bytes(), os.stat(p).st_mode)
                else:
                    others[rel] = "special"
        return cls(files, sorted(dirs), others)

    def restore(self, root: Path) -> List[str]:
        """Put the directory back, IN PLACE where a file survived (inodes are part of some
        tasks' contract). Returns the paths that still differ afterwards (empty = exact)."""
        now = _Tree.take(root)
        keep_dirs = set(self.dirs)
        for rel in sorted(now.others, key=lambda r: -r.count("/")):
            if now.others[rel] != self.others.get(rel):
                try:
                    os.unlink(root / rel)
                except OSError as exc:  # the comparison below reports it
                    _log.debug("could not remove %s: %s", rel, exc)
        for rel in now.files:
            if rel not in self.files:
                p = root / rel
                _writable(p)
                p.unlink()
        for rel in sorted(now.dirs, key=lambda r: -r.count("/")):
            if rel not in keep_dirs:
                try:  # deepest first: its new entries are already gone
                    (root / rel).rmdir()
                except OSError as exc:  # left in place; the comparison below reports it
                    _log.debug("could not remove %s: %s", rel, exc)
        for rel in self.dirs:
            (root / rel).mkdir(parents=True, exist_ok=True)
        for rel, (data, mode) in self.files.items():
            p = root / rel
            if p.is_file() and not p.is_symlink():
                if p.read_bytes() != data:
                    _writable(p)
                    with open(p, "r+b") as fh:
                        fh.seek(0)
                        fh.write(data)
                        fh.truncate()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            try:
                os.chmod(p, mode)
            except OSError as exc:  # content is what the verifier reads; it is compared below
                _log.debug("could not restore the mode of %s: %s", p, exc)
        after = _Tree.take(root)
        bad = sorted(
            k
            for k in set(after.files) | set(self.files)
            if after.files.get(k, (None,))[0] != self.files.get(k, (None,))[0]
        )
        bad += sorted(
            k
            for k in set(after.others) | set(self.others)
            if after.others.get(k) != self.others.get(k)
        )
        return bad + sorted(set(after.dirs) ^ set(self.dirs))


_READ_CAP = 8 << 20  # bytes of one workspace file held in memory; larger ones are hashed


def _entries(root: Path) -> List[Tuple[Path, os.stat_result]]:
    """Every entry under ``root`` with its lstat, never descending through a link."""
    out: List[Tuple[Path, os.stat_result]] = []
    for base, dnames, fnames in os.walk(root, followlinks=False):
        for name in dnames + fnames:
            p = Path(base) / name
            try:
                out.append((p, os.lstat(p)))
            except OSError:
                continue
    return out


def _is_regular(p: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(p).st_mode)
    except OSError:
        return False


def _is_dir_not_link(p: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(p).st_mode)
    except OSError:
        return False


def _read_regular(p: Path, cap: int = _READ_CAP) -> bytes:
    """Bytes of a REGULAR file, opened without following a link and without blocking; a
    file over ``cap`` becomes its sha256 so a huge file cannot exhaust memory."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(p, flags)
    except OSError:
        return b"<unreadable>"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return b"<not a regular file>"
        h = hashlib.sha256()
        chunks: List[bytes] = []
        size = 0
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            size += len(b)
            h.update(b)
            if size <= cap:
                chunks.append(b)
        if size > cap:
            return b"<sha256:" + h.hexdigest().encode() + b">"
        return b"".join(chunks)
    except OSError:
        return b"<unreadable>"
    finally:
        os.close(fd)


def _tree_hash(root: Path) -> str:
    """One digest over a directory's files (names + bytes), for tamper checks."""
    h = hashlib.sha256()
    t = _Tree.take(root)
    for rel in sorted(t.files):
        h.update(rel.encode() + b"\0" + t.files[rel][0] + b"\0")
    for rel in sorted(t.others):
        h.update(rel.encode() + b"\0" + t.others[rel].encode() + b"\0")
    return h.hexdigest()


def _netns_prefix() -> List[str]:
    """``unshare -rn`` when it works here (Linux): the shell gets no network at all."""
    if os.name == "nt" or not shutil.which("unshare"):
        return []
    try:
        ok = (
            subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=5).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        ok = False
    return ["unshare", "-rn"] if ok else []


# --------------------------------------------------------------------------- policies (no model)
_SHELL_WRITE = (
    'python -c "import base64,os,sys;p=sys.argv[1];'
    "os.makedirs(p.rpartition(chr(47))[0] or chr(46),exist_ok=True);"
    'open(p,chr(119)+chr(98)).write(base64.urlsafe_b64decode(sys.argv[2]))" '
    "'%s' '%s'"
)


def run_policy(
    env: SkillTaskTerminalEnv, policy: Callable[[SkillTaskTerminalEnv], None]
) -> VerifierResult:
    """Drive ``env`` with a scripted policy, then score the final workspace."""
    policy(env)
    return env.final()


def do_nothing_policy(env: SkillTaskTerminalEnv) -> None:
    env.submit()


def scripted_baseline_policy(env: SkillTaskTerminalEnv) -> None:
    """The non-LLM arm: run the tests, read every file, submit. Changes nothing."""
    while True:
        a = env.auto_action()
        if a is None:
            break
        env.act(a, source="policy")
    env.submit()


def reference_policy(env: SkillTaskTerminalEnv, via_shell: bool = False) -> None:
    """The reference solution, replayed THROUGH the adapter: run ``solution/solve.py`` on
    a scratch copy of the pristine world, then carry its byte-level effect over with
    ``write`` actions (``rm`` for a deleted file), every one through ``permits()``. Git
    internals and binary files are carried as byte-exact writes. ``via_shell`` carries every
    file with a shell command instead (a python one-liner fed the bytes as urlsafe base64),
    so the whole solution goes THROUGH the sandbox's shell -- the jail, when there is one."""
    ws = env.task.materialize()
    try:
        before = _snapshot(ws.path)
        proc = ws.apply(env.task.root / "solution" / "solve.py")
        if proc.returncode != 0:
            raise TaskError(
                "solve.py exited %d: %s" % (proc.returncode, (proc.stderr or "")[-300:])
            )
        after = _snapshot(ws.path)
    finally:
        ws.cleanup()
    for rel in sorted(set(before) - set(after)):
        env.sh("rm -f -- '%s'" % rel)
    for rel in sorted(after):
        data = after[rel]
        if before.get(rel) == data:
            continue
        if via_shell:
            env.sh(_SHELL_WRITE % (rel, base64.urlsafe_b64encode(data).decode()))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and ".git" not in rel.split("/") and chr(13) not in text:
            env.write(rel, text)
        else:  # binary, CRLF, or a repository's objects/refs/index: carried byte for byte
            env.write_bytes(rel, data)
    env.run_tests()
    env.submit()
