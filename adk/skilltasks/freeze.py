"""Freeze the grading contract before a reference solution exists.

``freeze(task_dir)`` hashes every file under ``tests/`` (except ``freeze.json`` itself)
and ``instruction.md``, and REFUSES when ``solution/`` already holds a file: the tests
are written first and the solution adapts to them, never the reverse. ``check_freeze``
re-hashes and reports every drift. The acceptance gate rejects a task whose grading
contract changed after its freeze.

A prompt that says "write the tests first" is not enforcement; this is the mechanical
half of that rule. It cannot prove the solution was not drafted elsewhere and pasted in
later -- it proves the grading material the gate runs is the material that was frozen.

Stdlib only; 3.10-compatible.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Dict, List

__all__ = ["freeze", "check_freeze", "contract_hashes", "FreezeError", "FREEZE_FILE"]

FREEZE_FILE = "tests/freeze.json"
SCHEMA = 1


class FreezeError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    # Hash bytes with CRLF folded to LF so a checkout's autocrlf cannot fake a drift.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def contract_hashes(task_dir: Path) -> Dict[str, str]:
    task_dir = Path(task_dir)
    out: Dict[str, str] = {}
    for p in sorted((task_dir / "tests").rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(task_dir).as_posix()
        if rel == FREEZE_FILE:
            continue
        out[rel] = _sha256(p)
    inst = task_dir / "instruction.md"
    if inst.is_file():
        out["instruction.md"] = _sha256(inst)
    return out


def _solution_files(task_dir: Path) -> List[str]:
    sol = task_dir / "solution"
    if not sol.is_dir():
        return []
    return sorted(p.relative_to(task_dir).as_posix() for p in sol.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)


def freeze(task_dir: Path) -> Dict[str, object]:
    task_dir = Path(task_dir)
    present = _solution_files(task_dir)
    if present:
        raise FreezeError("refusing to freeze %s: solution/ already has %s -- freeze the tests "
                          "and rubric BEFORE writing the reference solution"
                          % (task_dir.name, present))
    hashes = contract_hashes(task_dir)
    if "tests/test.py" not in hashes or "tests/rubric.md" not in hashes:
        raise FreezeError("refusing to freeze %s: tests/test.py and tests/rubric.md must exist"
                          % task_dir.name)
    doc = {
        "schema": SCHEMA,
        "frozen_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "solution_files_at_freeze": [],
        "files": hashes,
    }
    (task_dir / FREEZE_FILE).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                                        encoding="utf-8", newline="\n")
    return doc


def check_freeze(task_dir: Path) -> List[str]:
    """Problems with the freeze record; an empty list means the contract is intact."""
    task_dir = Path(task_dir)
    path = task_dir / FREEZE_FILE
    if not path.is_file():
        return ["no %s: the grading contract was never frozen" % FREEZE_FILE]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return ["%s unreadable: %s" % (FREEZE_FILE, exc)]
    problems: List[str] = []
    if doc.get("solution_files_at_freeze"):
        problems.append("freeze was taken with solution files present: %s"
                        % doc["solution_files_at_freeze"])
    frozen = doc.get("files") or {}
    now = contract_hashes(task_dir)
    for rel in sorted(set(frozen) | set(now)):
        if rel not in now:
            problems.append("%s was frozen and is now missing" % rel)
        elif rel not in frozen:
            problems.append("%s was added after the freeze" % rel)
        elif frozen[rel] != now[rel]:
            problems.append("%s changed after the freeze" % rel)
    return problems
