"""The spec_* tools: scaffold, validate, list, tick, archive and export a change.

Every tool takes ``root`` (the project directory holding ``openspec/``) and
returns a JSON string so it reads the same from the agent, the CLI and a test.
Pure stdlib + pyyaml; nothing here touches the network or a platform — the
export file is what a connected platform ingests, and that call is not ours.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

from . import templates
from .deltas import (
    RequirementBlock,
    delta_purpose,
    merge_into_living,
    new_living_text,
    parse_delta,
)

OPENSPEC_DIR = "openspec"
CHANGES_DIR = "changes"
SPECS_DIR = "specs"
ARCHIVE_DIR = "archive"
CONSTITUTION_FILE = "CONSTITUTION.md"
CONFIG_FILE = ".openspec.yaml"
EXPORT_FILE = "export.md"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_H2_RE = re.compile(r"^##\s+\S")
_TASK_RE = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s+)(\d+(?:\.\d+)*)(\b.*)$")
_STORY_RE = re.compile(r"^###\s+P(\d+)\s*:\s*(.+?)\s*$")
_WHEN_RE = re.compile(r"^\s*[-*]\s+\*\*WHEN\*\*")
_THEN_RE = re.compile(r"^\s*[-*]\s+\*\*THEN\*\*")


# ── helpers ──────────────────────────────────────────────────────────────────


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def _root(root: str) -> Path:
    return Path(root or ".").expanduser().resolve()


def _check_id(change_id: str) -> str | None:
    """A change id is one path segment, so it can never escape ``changes/``."""
    if not change_id or not _ID_RE.match(change_id):
        return ("change_id must be one path segment of [A-Za-z0-9._-] "
                f"(got {change_id!r})")
    if change_id in {ARCHIVE_DIR, ".", ".."}:
        return f"change_id {change_id!r} is reserved"
    return None


def _change_dir(root: Path, change_id: str) -> Path:
    return root / OPENSPEC_DIR / CHANGES_DIR / change_id


def _delta_files(change_dir: Path) -> list[tuple[str, Path]]:
    """``(capability, path)`` for every ``specs/**/spec.md`` in a change."""
    specs = change_dir / SPECS_DIR
    if not specs.is_dir():
        return []
    out = []
    for p in sorted(specs.rglob("spec.md")):
        out.append((p.parent.relative_to(specs).as_posix(), p))
    return out


def _read_config(change_dir: Path) -> dict:
    cfg = change_dir / CONFIG_FILE
    if not cfg.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(_read(cfg)) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _section(lines: list[str], heading: str) -> tuple[int, list[str]] | None:
    """``(1-based line of the heading, body lines)`` for a ``## <heading>``
    section, or None. Body runs to the next level-2 heading."""
    pat = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE)
    for i, ln in enumerate(lines):
        if pat.match(ln):
            j = i + 1
            while j < len(lines) and not _H2_RE.match(lines[j]):
                j += 1
            return i + 1, lines[i + 1:j]
    return None


def _has_content(body: list[str]) -> bool:
    text = re.sub(r"<!--.*?-->", "", "\n".join(body), flags=re.DOTALL)
    return any(ln.strip() not in {"", "-", "*"} for ln in text.splitlines())


def _parse_tasks(text: str) -> list[dict]:
    out = []
    for i, ln in enumerate(text.splitlines()):
        m = _TASK_RE.match(ln)
        if m:
            out.append({
                "id": m.group(4),
                "text": m.group(5).strip(),
                "done": m.group(2).lower() == "x",
                "line": i + 1,
            })
    return out


def _task_counts(change_dir: Path) -> dict[str, int]:
    tasks_md = change_dir / "tasks.md"
    tasks = _parse_tasks(_read(tasks_md)) if tasks_md.is_file() else []
    return {"done": sum(1 for t in tasks if t["done"]), "total": len(tasks)}


# ── validation ───────────────────────────────────────────────────────────────


def _validate(root: Path, change_id: str) -> dict:
    """The dict behind spec_validate; shared by status and archive."""
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(path: Path, line: int, message: str) -> None:
        errors.append({"file": _rel(path, root), "line": line, "message": message})

    def warn(path: Path, line: int, message: str) -> None:
        warnings.append({"file": _rel(path, root), "line": line, "message": message})

    bad = _check_id(change_id)
    if bad:
        return {"ok": False, "change_id": change_id,
                "errors": [{"file": "", "line": 0, "message": bad}], "warnings": []}
    change_dir = _change_dir(root, change_id)
    if not change_dir.is_dir():
        err(change_dir, 0, "change does not exist (run spec_new first)")
        return {"ok": False, "change_id": change_id, "errors": errors, "warnings": warnings}

    # proposal.md — Why, Constitution check, user stories
    proposal = change_dir / "proposal.md"
    if not proposal.is_file():
        err(proposal, 0, "proposal.md is missing")
    else:
        plines = _read(proposal).splitlines()
        why = _section(plines, "Why")
        if why is None:
            err(proposal, 1, "proposal has no '## Why' section")
        elif not _has_content(why[1]):
            err(proposal, why[0], "'## Why' is empty — say what problem this solves and why now")
        if (root / OPENSPEC_DIR / CONSTITUTION_FILE).is_file():
            cc = _section(plines, "Constitution check")
            if cc is None:
                err(proposal, 1, f"openspec/{CONSTITUTION_FILE} exists but the proposal has no "
                                 "'## Constitution check' section")
            elif not _has_content(cc[1]):
                err(proposal, cc[0], "'## Constitution check' is empty")
        stories = _section(plines, "User Stories")
        if stories is None:
            warn(proposal, 1, "proposal has no '## User Stories' section")
        else:
            base = stories[0]
            body = stories[1]
            idx = [i for i, ln in enumerate(body) if _STORY_RE.match(ln)]
            if not idx:
                warn(proposal, base, "'## User Stories' lists no '### P<n>: <story>'")
            for n, i in enumerate(idx):
                j = idx[n + 1] if n + 1 < len(idx) else len(body)
                chunk = body[i:j]
                if not (any(_WHEN_RE.match(x) for x in chunk)
                        and any(_THEN_RE.match(x) for x in chunk)):
                    warn(proposal, base + i + 1,
                         f"story '{body[i].strip()}' has no WHEN/THEN acceptance line, "
                         "so it is not independently testable")

    # delta specs
    cfg = _read_config(change_dir)
    skip_specs = bool(cfg.get("skip_specs", False))
    deltas = _delta_files(change_dir)
    if not deltas and not skip_specs:
        err(change_dir / SPECS_DIR, 0,
            "no delta spec found (specs/<capability>/spec.md); set skip_specs: true in "
            f"{CONFIG_FILE} only if no requirement changes")
    for capability, path in deltas:
        text = _read(path)
        delta = parse_delta(text)
        total = sum(len(v) for v in delta.values())
        if total == 0:
            err(path, 1, "delta has no '### Requirement:' under any ADDED/MODIFIED/REMOVED "
                         "Requirements section")
        for section in ("ADDED", "MODIFIED"):
            for block in delta[section]:
                _check_block(block, path, err)
        if not capability or capability == ".":
            err(path, 1, "delta spec must live at specs/<capability>/spec.md")

    # tasks.md
    tasks_md = change_dir / "tasks.md"
    if not tasks_md.is_file():
        err(tasks_md, 0, "tasks.md is missing")
    else:
        tasks = _parse_tasks(_read(tasks_md))
        if not tasks:
            err(tasks_md, 1, "tasks.md has no '- [ ] N.M task' lines")
        elif all(t["done"] for t in tasks):
            err(tasks_md, tasks[0]["line"], "every task is already ticked; add the work that remains "
                                            "or archive the change")

    return {"ok": not errors, "change_id": change_id, "errors": errors, "warnings": warnings}


def _check_block(block: RequirementBlock, path: Path, err) -> None:
    if not block.scenarios:
        err(path, block.line, f"requirement '{block.name}' has no '#### Scenario:'")
        return
    for sc in block.scenarios:
        missing = [k for k, ok in (("WHEN", sc.has_when), ("THEN", sc.has_then)) if not ok]
        if missing:
            err(path, sc.line, f"scenario '{sc.name}' of requirement '{block.name}' lacks "
                               f"{' and '.join('- **' + m + '** ...' for m in missing)}")


# ── tools ────────────────────────────────────────────────────────────────────


def spec_new(change_id: str, why: str = "", root: str = ".") -> str:
    """Scaffold a new spec-driven change at openspec/changes/<change_id>/.

    change_id: One path segment, e.g. "add-rate-limits"; must not already exist.
    why: Optional text for the proposal's Why section.
    root: Project directory that holds (or will hold) openspec/.
    """
    r = _root(root)
    bad = _check_id(change_id)
    if bad:
        return _json({"ok": False, "error": bad})
    change_dir = _change_dir(r, change_id)
    if change_dir.exists():
        return _json({"ok": False, "error": f"change already exists: {_rel(change_dir, r)}"})
    has_constitution = (r / OPENSPEC_DIR / CONSTITUTION_FILE).is_file()
    proposal = templates.PROPOSAL.format(
        change_id=change_id,
        why=why.strip() or templates.WHY_PLACEHOLDER,
        constitution=templates.CONSTITUTION_SECTION if has_constitution else "",
    )
    files = {
        "proposal.md": proposal,
        "design.md": templates.DESIGN.format(change_id=change_id),
        "tasks.md": templates.TASKS.format(change_id=change_id),
        CONFIG_FILE: templates.OPENSPEC_YAML,
    }
    created = []
    for name, text in files.items():
        p = change_dir / name
        _write(p, text)
        created.append(_rel(p, r))
    (change_dir / SPECS_DIR).mkdir(parents=True, exist_ok=True)
    return _json({
        "ok": True,
        "change_id": change_id,
        "change_dir": _rel(change_dir, r),
        "created": created,
        "constitution": has_constitution,
        "next": (f"fill proposal.md, then write {_rel(change_dir, r)}/specs/<capability>/spec.md "
                 "from delta_template, then spec_validate"),
        "delta_template": templates.DELTA_SPEC.format(capability="<capability>"),
    })


def spec_validate(change_id: str, root: str = ".") -> str:
    """Validate a change: Why filled, delta specs with WHEN/THEN scenarios, open tasks.

    change_id: The change to validate.
    root: Project directory that holds openspec/.
    """
    return _json(_validate(_root(root), change_id))


def spec_status(root: str = ".") -> str:
    """List every open change with its task progress and whether it validates.

    root: Project directory that holds openspec/.
    """
    r = _root(root)
    changes_dir = r / OPENSPEC_DIR / CHANGES_DIR
    changes = []
    if changes_dir.is_dir():
        for d in sorted(changes_dir.iterdir()):
            if not d.is_dir() or d.name == ARCHIVE_DIR:
                continue
            v = _validate(r, d.name)
            changes.append({
                "change_id": d.name,
                "tasks": _task_counts(d),
                "valid": v["ok"],
                "errors": len(v["errors"]),
                "warnings": len(v["warnings"]),
            })
    archive = changes_dir / ARCHIVE_DIR
    archived = sorted(p.name for p in archive.iterdir() if p.is_dir()) if archive.is_dir() else []
    specs_dir = r / OPENSPEC_DIR / SPECS_DIR
    specs = ([p.parent.relative_to(specs_dir).as_posix() for p in sorted(specs_dir.rglob("spec.md"))]
             if specs_dir.is_dir() else [])
    return _json({
        "ok": True,
        "root": r.as_posix(),
        "constitution": (r / OPENSPEC_DIR / CONSTITUTION_FILE).is_file(),
        "changes": changes,
        "archived": archived,
        "specs": specs,
    })


def spec_tasks(change_id: str, root: str = ".", done: str = "") -> str:
    """List a change's tasks; with done="N.M" tick that task in tasks.md.

    change_id: The change whose tasks.md to read.
    root: Project directory that holds openspec/.
    done: Task id to mark complete, e.g. "1.2". Empty = list only.
    """
    r = _root(root)
    bad = _check_id(change_id)
    if bad:
        return _json({"ok": False, "error": bad})
    tasks_md = _change_dir(r, change_id) / "tasks.md"
    if not tasks_md.is_file():
        return _json({"ok": False, "error": f"missing {_rel(tasks_md, r)}"})
    text = _read(tasks_md)
    ticked = None
    if done:
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            m = _TASK_RE.match(ln)
            if m and m.group(4) == done:
                if m.group(2).lower() != "x":
                    lines[i] = f"{m.group(1)}x{m.group(3)}{m.group(4)}{m.group(5)}"
                    ticked = {"id": done, "line": i + 1, "already_done": False}
                else:
                    ticked = {"id": done, "line": i + 1, "already_done": True}
                break
        if ticked is None:
            return _json({"ok": False, "error": f"no task {done!r} in {_rel(tasks_md, r)}",
                          "tasks": _parse_tasks(text)})
        if not ticked["already_done"]:
            text = "\n".join(lines) + "\n"
            _write(tasks_md, text)
    tasks = _parse_tasks(text)
    out = {
        "ok": True,
        "change_id": change_id,
        "file": _rel(tasks_md, r),
        "done": sum(1 for t in tasks if t["done"]),
        "total": len(tasks),
        "tasks": tasks,
    }
    if ticked:
        out["ticked"] = ticked
    return _json(out)


def spec_archive(change_id: str, root: str = ".") -> str:
    """Merge a validated change's deltas into the living specs and archive it.

    change_id: The change to archive; it must pass spec_validate first.
    root: Project directory that holds openspec/.
    """
    r = _root(root)
    v = _validate(r, change_id)
    if not v["ok"]:
        return _json({"ok": False, "change_id": change_id,
                      "error": "change does not validate; fix the errors first",
                      "errors": v["errors"]})
    change_dir = _change_dir(r, change_id)
    today = _dt.date.today().isoformat()
    target = change_dir.parent / ARCHIVE_DIR / f"{today}-{change_id}"
    if target.exists():
        return _json({"ok": False, "change_id": change_id,
                      "error": f"archive target already exists: {_rel(target, r)}"})

    # Merge every delta in memory first; write nothing until all of them succeed.
    pending: list[tuple[Path, str]] = []
    merged: dict[str, dict] = {}
    errors: list[dict] = []
    for capability, path in _delta_files(change_dir):
        living = r / OPENSPEC_DIR / SPECS_DIR / capability / "spec.md"
        delta_text = _read(path)
        delta = parse_delta(delta_text)
        created = not living.is_file()
        living_text = (_read(living) if not created
                       else new_living_text(capability, delta_purpose(delta_text)))
        new_text, counts, errs = merge_into_living(living_text, delta)
        for e in errs:
            errors.append({"file": _rel(path, r), "living": _rel(living, r), "message": e})
        if not errs:
            pending.append((living, new_text))
            merged[capability] = {**counts, "created": created, "spec": _rel(living, r)}
    if errors:
        return _json({"ok": False, "change_id": change_id,
                      "error": "delta does not apply to the living specs; nothing was written",
                      "errors": errors})
    for living, new_text in pending:
        _write(living, new_text)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(change_dir), str(target))
    return _json({
        "ok": True,
        "change_id": change_id,
        "archived_to": _rel(target, r),
        "merged": merged,
        "totals": {
            k: sum(m[k] for m in merged.values()) for k in ("added", "modified", "removed")
        },
    })


def spec_export(change_id: str, root: str = ".") -> str:
    """Write <change>/export.md: proposal + every delta spec + tasks as one markdown file.

    change_id: The change to export.
    root: Project directory that holds openspec/.
    """
    r = _root(root)
    bad = _check_id(change_id)
    if bad:
        return _json({"ok": False, "error": bad})
    change_dir = _change_dir(r, change_id)
    if not change_dir.is_dir():
        return _json({"ok": False, "error": f"change does not exist: {_rel(change_dir, r)}"})
    parts = [f"# Change: {change_id}\n"]
    included = []

    def add(label: str, path: Path) -> None:
        if path.is_file():
            parts.append(f"\n---\n\n<!-- {label}: {_rel(path, change_dir)} -->\n\n"
                         + _read(path).rstrip("\n") + "\n")
            included.append(_rel(path, r))

    add("proposal", change_dir / "proposal.md")
    for capability, path in _delta_files(change_dir):
        add(f"delta spec {capability}", path)
    add("tasks", change_dir / "tasks.md")
    out = change_dir / EXPORT_FILE
    _write(out, "".join(parts))
    return _json({"ok": True, "change_id": change_id, "path": out.as_posix(),
                  "included": included, "bytes": out.stat().st_size})


TOOLS = (spec_new, spec_validate, spec_status, spec_tasks, spec_archive, spec_export)
