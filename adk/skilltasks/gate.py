"""The acceptance gate: a compiled task is kept only if it can tell right from nothing.

No model is anywhere in this module. Every verdict comes from running the task's own
scripts against fresh copies of its world:

* G1 layout     -- the required files exist; ``task.json`` is well formed.
* G2 rubric     -- three sections in order; every Must-do / Must-avoid bullet cites a
                   test or probe, and every cited name EXISTS.
* G3 freeze     -- the grading contract (tests/ + instruction.md) hashes to what was
                   frozen, and the freeze was taken while solution/ was empty.
* G4 privacy    -- nothing under environment/ carries grading or solution material, and
                   the verifier imports no network or model client.
* G5 oracle     -- the reference solution scores exactly 1.0 with every test passing.
* G6 repeat     -- a second oracle run on a second fresh world gives identical tests.
* G7 nop        -- doing nothing scores exactly 0.0 AND every outcome test fails on
                   the pristine world (no outcome test is free).
* G8 probes     -- every must-avoid probe (``solution/avoid_*.py``) scores below 1.0.

``gate`` exits 0 when every task is accepted, 1 when any is rejected, 2 when it could
not judge (no tasks found, a script could not run at all). ``--self-test`` builds
planted-bad tasks and proves each gate still fails.

Stdlib only; 3.10-compatible.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .freeze import check_freeze, freeze
from .rubric import parse_rubric
from .task import SkillTask, TaskError, find_tasks, load_task

__all__ = ["GateReport", "gate_task", "gate_root", "self_test", "MODEL_MODULES"]

#: A verifier (or this gate) importing any of these could consult a model or the network.
MODEL_MODULES = ("socket", "urllib", "http", "requests", "httpx", "aiohttp", "openai",
                 "anthropic", "adk.core.backends", "adk.reasoning")
_FORBIDDEN_ENV_NAMES = {"rubric.md", "freeze.json", "solve.py", "task.json", "instruction.md"}


@dataclass
class GateReport:
    task: str
    accepted: bool = False
    checks: Dict[str, bool] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    oracle: Optional[Dict[str, Any]] = None
    nop: Optional[Dict[str, Any]] = None
    probes: Dict[str, float] = field(default_factory=dict)
    could_not_judge: bool = False

    def fail(self, check: str, msg: str) -> None:
        self.checks[check] = False
        self.errors.append("%s: %s" % (check, msg))

    def to_dict(self) -> Dict[str, Any]:
        return {"task": self.task, "accepted": self.accepted, "checks": self.checks,
                "errors": self.errors, "oracle": self.oracle, "nop": self.nop,
                "probes": self.probes, "could_not_judge": self.could_not_judge}


def _verifier_imports(tests_dir: Path) -> List[str]:
    bad: List[str] = []
    for py in sorted(tests_dir.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            bad.append("%s does not parse: %s" % (py.name, exc))
            continue
        for node in ast.walk(tree):
            names: List[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if any(n == m or n.startswith(m + ".") for m in MODEL_MODULES):
                    bad.append("%s imports %s" % (py.name, n))
    return bad


def _privacy(task: SkillTask) -> List[str]:
    env = task.root / "environment"
    if not env.is_dir():
        return []
    private_bytes = set()
    for sub in ("tests", "solution"):
        d = task.root / sub
        if d.is_dir():
            for p in d.rglob("*"):
                if p.is_file() and p.stat().st_size > 64:
                    private_bytes.add(p.read_bytes())
    out: List[str] = []
    for p in (env / "files").rglob("*") if (env / "files").is_dir() else []:
        if not p.is_file():
            continue
        rel = p.relative_to(env).as_posix()
        if p.name in _FORBIDDEN_ENV_NAMES:
            out.append("environment ships grading/solution material by name: %s" % rel)
        elif p.read_bytes() in private_bytes:
            out.append("environment ships a byte copy of a tests/ or solution/ file: %s" % rel)
    return out


def gate_task(task_dir: Path, *, work_root: Optional[Path] = None) -> GateReport:
    rep = GateReport(task=Path(task_dir).name)
    # G1
    try:
        task = load_task(Path(task_dir))
    except TaskError as exc:
        rep.fail("layout", str(exc))
        return rep
    rep.task = task.id
    sol = task.root / "solution" / "solve.py"
    if not sol.is_file():
        rep.fail("layout", "solution/solve.py missing")
        return rep
    rep.checks["layout"] = True

    # G2 (static half; name resolution waits for the oracle's test list)
    rub = parse_rubric(task.root / "tests" / "rubric.md")
    for p in rub.problems:
        rep.fail("rubric", p)

    # G3
    for p in check_freeze(task.root):
        rep.fail("freeze", p)
    rep.checks.setdefault("freeze", True)

    # G4
    for p in _privacy(task) + _verifier_imports(task.root / "tests"):
        rep.fail("privacy", p)
    rep.checks.setdefault("privacy", True)

    # G5 / G6 / G7 / G8 -- each on its own fresh world
    try:
        oracle = _run(task, sol, work_root)
        rep.oracle = oracle.to_dict()
        if oracle.reward != 1.0 or not all(oracle.tests.values()):
            failing = sorted(k for k, v in oracle.tests.items() if not v)
            rep.fail("oracle", "reference scored %.3f; failing tests %s" % (oracle.reward, failing))
        else:
            rep.checks["oracle"] = True

        again = _run(task, sol, work_root)
        if again.tests != oracle.tests:
            rep.fail("repeat", "second oracle run differs: %s vs %s" % (again.tests, oracle.tests))
        else:
            rep.checks["repeat"] = True

        nop = _run(task, None, work_root)
        rep.nop = nop.to_dict()
        free = sorted(t for t in task.outcome_tests if nop.tests.get(t))
        if nop.reward != 0.0 or free:
            rep.fail("nop", "doing nothing scored %.3f; outcome tests passing on the pristine "
                     "world: %s" % (nop.reward, free))
        else:
            rep.checks["nop"] = True

        names = set(oracle.tests)
        probes = {p.stem[len("avoid_"):]: p for p in task.probes()}
        for sec in ("Must-do", "Must-avoid"):
            for kind, name, _b in rub.citations(sec):
                if kind == "test" and name not in names:
                    rep.fail("rubric", "%s cites unknown test %r (verifier reports %s)"
                             % (sec, name, sorted(names)))
                if kind == "probe" and name not in probes:
                    rep.fail("rubric", "%s cites probe %r but solution/avoid_%s.py is missing"
                             % (sec, name, name))
        rep.checks.setdefault("rubric", True)

        for name, script in sorted(probes.items()):
            res = _run(task, script, work_root)
            rep.probes[name] = res.reward
            if res.reward >= 1.0:
                rep.fail("probes", "must-avoid probe %s scored %.3f -- the verifier cannot tell "
                         "the trap from the fix" % (name, res.reward))
        rep.checks.setdefault("probes", True)
    except TaskError as exc:
        rep.could_not_judge = True
        rep.errors.append("could not run: %s" % exc)
        return rep

    rep.accepted = not rep.errors and all(rep.checks.values())
    return rep


def _run(task: SkillTask, script: Optional[Path], work_root: Optional[Path]):
    ws = task.materialize(work_root)
    try:
        if script is not None:
            proc = ws.apply(script)
            if proc.returncode != 0 and script.name == "solve.py":
                raise TaskError("%s exited %d: %s" % (script.name, proc.returncode,
                                                      (proc.stderr or proc.stdout)[-400:]))
        return ws.verify()
    finally:
        ws.cleanup()


def gate_root(root: Path, *, work_root: Optional[Path] = None) -> "tuple[int, List[GateReport]]":
    before = {m for m in sys.modules if any(m == x or m.startswith(x + ".") for x in MODEL_MODULES)}
    tasks = find_tasks(Path(root))
    if not tasks:
        return 2, []
    reports = [gate_task(t, work_root=work_root) for t in tasks]
    after = {m for m in sys.modules if any(m == x or m.startswith(x + ".") for x in MODEL_MODULES)}
    leaked = sorted(after - before)
    if leaked:  # the gate itself must never have loaded a model client
        for r in reports:
            r.fail("no_model", "the gate process imported %s" % leaked)
            r.accepted = False
    if any(r.could_not_judge for r in reports):
        return 2, reports
    return (0 if all(r.accepted for r in reports) else 1), reports


# --------------------------------------------------------------------------- self-test
_GOOD_TEST = '''import json, sys, pathlib
ws = pathlib.Path(sys.argv[1])
f = ws / "out.txt"
s = ws / "seed.txt"
print(json.dumps({"tests": {"wrote_out": f.is_file() and f.read_text() == "42\\n",
                            "kept_seed": s.is_file() and s.read_text() == "seed\\n"}}))
'''
_GOOD_RUBRIC = """## Must-do
- out.txt holds 42 [test: wrote_out]

## Must-avoid
- deleting the seed file [test: kept_seed] [probe: clobber]

## Best-practice
- write the file once, plainly
"""
_SOLVE = 'import pathlib, sys\n(pathlib.Path(sys.argv[1]) / "out.txt").write_text("42\\n")\n'
_CLOBBER = ('import pathlib, sys\nws = pathlib.Path(sys.argv[1])\n(ws / "out.txt").write_text("42\\n")\n'
            '(ws / "seed.txt").unlink()\n')


def _make_task(root: Path, name: str, *, test: str = _GOOD_TEST, solve: str = _SOLVE,
               rubric: str = _GOOD_RUBRIC, edit_after_freeze: bool = False,
               outcome: Optional[List[str]] = None) -> Path:
    t = root / name
    (t / "environment" / "files").mkdir(parents=True)
    (t / "environment" / "files" / "seed.txt").write_text("seed\n", encoding="utf-8")
    (t / "tests").mkdir()
    (t / "task.json").write_text(json.dumps({"id": name, "outcome_tests": outcome or ["wrote_out"]}),
                                 encoding="utf-8")
    (t / "instruction.md").write_text("Write 42 to out.txt.\n", encoding="utf-8")
    (t / "tests" / "test.py").write_text(test, encoding="utf-8")
    (t / "tests" / "rubric.md").write_text(rubric, encoding="utf-8")
    freeze(t)
    if edit_after_freeze:
        (t / "tests" / "test.py").write_text(test + "# weakened\n", encoding="utf-8")
    (t / "solution").mkdir()
    (t / "solution" / "solve.py").write_text(solve, encoding="utf-8")
    (t / "solution" / "avoid_clobber.py").write_text(_CLOBBER, encoding="utf-8")
    return t


def self_test() -> int:
    """Exit 0 when every planted defect is caught and the good task passes; 1 otherwise."""
    cases = {
        "good": ({}, True, None),
        "edited_after_freeze": ({"edit_after_freeze": True}, False, "freeze"),
        "reference_fails": ({"solve": "pass\n"}, False, "oracle"),
        "free_outcome": ({"outcome": ["kept_seed"]}, False, "nop"),
        "blind_verifier": ({"test": _GOOD_TEST.replace(
            's.is_file() and s.read_text() == "seed\\n"', 'True')}, False, "probes"),
        "uncited_rubric": ({"rubric": _GOOD_RUBRIC.replace(" [test: wrote_out]", "")}, False, "rubric"),
        "networked_verifier": ({"test": "import urllib.request\n" + _GOOD_TEST}, False, "privacy"),
    }
    bad = 0
    with tempfile.TemporaryDirectory(prefix="skilltasks-selftest-") as tmp:
        for name, (kw, want_ok, want_check) in cases.items():
            case_root = Path(tmp) / name
            case_root.mkdir()
            d = _make_task(case_root, name, **kw)
            rep = gate_task(d)
            ok = rep.accepted == want_ok and not rep.could_not_judge and (want_check is None or rep.checks.get(want_check) is False)
            print("  %s %-20s accepted=%s %s" % ("ok  " if ok else "FAIL", name, rep.accepted,
                                                 "" if ok else rep.errors))
            bad += 0 if ok else 1
        # freeze must refuse once a solution exists
        late = Path(tmp) / "late"
        (late / "tests").mkdir(parents=True)
        (late / "tests" / "test.py").write_text(_GOOD_TEST, encoding="utf-8")
        (late / "tests" / "rubric.md").write_text(_GOOD_RUBRIC, encoding="utf-8")
        (late / "solution").mkdir()
        (late / "solution" / "solve.py").write_text(_SOLVE, encoding="utf-8")
        try:
            freeze(late)
            print("  FAIL freeze accepted a task whose solution already existed")
            bad += 1
        except Exception:  # noqa: BLE001 - FreezeError is the expected refusal
            print("  ok   freeze refuses once solution/ has files")
    return 0 if bad == 0 else 1
