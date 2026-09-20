"""The specflow toolpack: scaffold -> validate -> tick -> archive -> export, on a
tmp_path root, plus the delta parser/merge on plain text."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from adk.toolpacks.specflow import register
from adk.toolpacks.specflow.cli import add_spec_parser, cmd_spec
from adk.toolpacks.specflow.deltas import merge_into_living, parse_delta
from adk.toolpacks.specflow.tools import (
    spec_archive,
    spec_export,
    spec_new,
    spec_status,
    spec_tasks,
    spec_validate,
)

CONTROL = "CONTROL-SPEC-5150"

PROPOSAL_OK = f"""# Proposal: demo

## Why

Agents write code before anyone agrees what it should do. {CONTROL}

## What Changes

- Add a spec workflow.

## Capabilities

### New Capabilities
- `rate-limits`: per-caller request budgets

### Modified Capabilities
-

## User Stories

### P1: an operator caps a caller
As an operator, I want a per-caller budget, so that one caller cannot starve the rest.
- **WHEN** a caller exceeds its budget
- **THEN** the request is rejected with 429

## Impact

- The gateway.
"""

DELTA_ADDED = """# Spec Delta: rate-limits

## Purpose
Per-caller request budgets so one caller cannot starve the rest.

## ADDED Requirements

### Requirement: Per-caller budget
The system SHALL reject a request once its caller exceeds the configured budget.

#### Scenario: over budget
- **WHEN** a caller has used its whole budget in the window
- **THEN** the next request is rejected with 429
"""

TASKS_OK = """# Tasks: demo

## 1. Implementation

- [ ] 1.1 Add the budget counter
- [ ] 1.2 Reject over-budget callers

## 2. Verification

- [ ] 2.1 Test the 429 path
"""


def _j(s: str) -> dict:
    return json.loads(s)


def _change(root: Path, cid: str) -> Path:
    return root / "openspec" / "changes" / cid


def _fill(root: Path, cid: str, delta: str = DELTA_ADDED, cap: str = "rate-limits") -> None:
    d = _change(root, cid)
    (d / "proposal.md").write_text(PROPOSAL_OK, encoding="utf-8")
    (d / "tasks.md").write_text(TASKS_OK, encoding="utf-8")
    p = d / "specs" / cap / "spec.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(delta, encoding="utf-8")


# ── register ──────────────────────────────────────────────────────────────────


class FakeRegistry:
    def __init__(self):
        self.names: list[str] = []

    def register(self, fn, **kw):
        self.names.append(fn.__name__)


def test_register_returns_six():
    reg = FakeRegistry()
    assert register(reg) == 6
    assert sorted(reg.names) == sorted([
        "spec_new", "spec_validate", "spec_status", "spec_tasks", "spec_archive", "spec_export",
    ])
    assert all(n.startswith("spec_") for n in reg.names)


def test_real_registry_extracts_schema():
    from adk.tools import ToolRegistry
    reg = ToolRegistry()
    assert register(reg) == 6
    td = reg.get("spec_tasks")
    assert td is not None
    assert td.parameters["required"] == ["change_id"]
    assert "done" in td.parameters["properties"]


# ── new -> validate ───────────────────────────────────────────────────────────


def test_new_scaffolds_and_refuses_duplicate(tmp_path: Path):
    out = _j(spec_new("demo", root=str(tmp_path)))
    assert out["ok"], out
    d = _change(tmp_path, "demo")
    for name in ("proposal.md", "design.md", "tasks.md", ".openspec.yaml"):
        assert (d / name).is_file(), name
    assert (d / "specs").is_dir()
    assert "## ADDED Requirements" in out["delta_template"]
    assert "## Constitution check" not in (d / "proposal.md").read_text(encoding="utf-8")
    dup = _j(spec_new("demo", root=str(tmp_path)))
    assert not dup["ok"] and "already exists" in dup["error"]


def test_new_rejects_path_escape(tmp_path: Path):
    for bad in ("../x", "a/b", "archive", ""):
        assert not _j(spec_new(bad, root=str(tmp_path)))["ok"]
    assert not (tmp_path / "openspec").exists()


def test_fresh_change_fails_validation_naming_files(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    v = _j(spec_validate("demo", root=str(tmp_path)))
    assert v["ok"] is False
    msgs = {(e["file"].rsplit("/", 1)[-1], e["message"]) for e in v["errors"]}
    files = {f for f, _ in msgs}
    assert "proposal.md" in files
    assert any("Why" in m for f, m in msgs if f == "proposal.md")
    assert any("delta spec" in m for _, m in msgs)
    why_err = next(e for e in v["errors"] if e["file"].endswith("proposal.md"))
    assert why_err["line"] > 0


def test_why_given_at_new_passes_why_check(tmp_path: Path):
    spec_new("demo", why="Because reasons.", root=str(tmp_path))
    v = _j(spec_validate("demo", root=str(tmp_path)))
    assert not any("Why" in e["message"] for e in v["errors"])


def test_filled_change_validates(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    _fill(tmp_path, "demo")
    v = _j(spec_validate("demo", root=str(tmp_path)))
    assert v["ok"] is True, v["errors"]
    assert v["warnings"] == []


def test_scenario_missing_then_is_an_error_with_line(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    _fill(tmp_path, "demo", delta=DELTA_ADDED.replace("- **THEN** the next request", "- the next request"))
    v = _j(spec_validate("demo", root=str(tmp_path)))
    assert not v["ok"]
    e = next(e for e in v["errors"] if e["file"].endswith("spec.md"))
    assert "THEN" in e["message"] and e["line"] == 11


def test_requirement_without_scenario_is_an_error(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    _fill(tmp_path, "demo", delta=DELTA_ADDED.split("#### Scenario")[0])
    v = _j(spec_validate("demo", root=str(tmp_path)))
    assert any("no '#### Scenario:'" in e["message"] for e in v["errors"])


def test_skip_specs_allows_no_delta(tmp_path: Path):
    spec_new("demo", why="Refactor only.", root=str(tmp_path))
    d = _change(tmp_path, "demo")
    (d / ".openspec.yaml").write_text("skip_specs: true\n", encoding="utf-8")
    v = _j(spec_validate("demo", root=str(tmp_path)))
    assert v["ok"] is True, v["errors"]


def test_constitution_requires_check_section(tmp_path: Path):
    (tmp_path / "openspec").mkdir()
    (tmp_path / "openspec" / "CONSTITUTION.md").write_text("# Principles\n- Small PRs\n",
                                                            encoding="utf-8")
    out = _j(spec_new("demo", root=str(tmp_path)))
    assert out["constitution"] is True
    d = _change(tmp_path, "demo")
    assert "## Constitution check" in (d / "proposal.md").read_text(encoding="utf-8")
    _fill(tmp_path, "demo")  # PROPOSAL_OK has no Constitution check section
    v = _j(spec_validate("demo", root=str(tmp_path)))
    assert any("Constitution check" in e["message"] for e in v["errors"])
    (d / "proposal.md").write_text(
        PROPOSAL_OK + "\n## Constitution check\n\n- Small PRs: one capability per change.\n",
        encoding="utf-8")
    assert _j(spec_validate("demo", root=str(tmp_path)))["ok"]


def test_story_without_acceptance_is_a_warning_not_error(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    _fill(tmp_path, "demo")
    d = _change(tmp_path, "demo")
    (d / "proposal.md").write_text(PROPOSAL_OK.replace("- **THEN** the request is rejected with 429\n", ""),
                                   encoding="utf-8")
    v = _j(spec_validate("demo", root=str(tmp_path)))
    assert v["ok"] is True
    assert any("independently testable" in w["message"] for w in v["warnings"])


# ── tasks / status ────────────────────────────────────────────────────────────


def test_tasks_lists_and_ticks(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    _fill(tmp_path, "demo")
    t = _j(spec_tasks("demo", root=str(tmp_path)))
    assert t["total"] == 3 and t["done"] == 0
    t = _j(spec_tasks("demo", root=str(tmp_path), done="1.1"))
    assert t["ok"] and t["ticked"] == {"id": "1.1", "line": 5, "already_done": False}
    assert t["done"] == 1
    text = (_change(tmp_path, "demo") / "tasks.md").read_text(encoding="utf-8")
    assert "- [x] 1.1 Add the budget counter" in text
    assert "- [ ] 1.2 Reject" in text
    again = _j(spec_tasks("demo", root=str(tmp_path), done="1.1"))
    assert again["ticked"]["already_done"] is True
    missing = _j(spec_tasks("demo", root=str(tmp_path), done="9.9"))
    assert not missing["ok"] and "9.9" in missing["error"]


def test_status_lists_changes(tmp_path: Path):
    assert _j(spec_status(root=str(tmp_path)))["changes"] == []
    spec_new("a", root=str(tmp_path))
    spec_new("b", root=str(tmp_path))
    _fill(tmp_path, "b")
    spec_tasks("b", root=str(tmp_path), done="2.1")
    s = _j(spec_status(root=str(tmp_path)))
    by_id = {c["change_id"]: c for c in s["changes"]}
    assert by_id["a"]["valid"] is False and by_id["a"]["errors"] >= 2
    assert by_id["b"]["valid"] is True
    assert by_id["b"]["tasks"] == {"done": 1, "total": 3}
    assert s["archived"] == [] and s["specs"] == []


# ── archive ───────────────────────────────────────────────────────────────────


def test_archive_refuses_invalid_change(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    a = _j(spec_archive("demo", root=str(tmp_path)))
    assert not a["ok"] and a["errors"]
    assert _change(tmp_path, "demo").is_dir()


def test_archive_merges_added_into_new_living_spec_and_moves(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    _fill(tmp_path, "demo")
    a = _j(spec_archive("demo", root=str(tmp_path)))
    assert a["ok"], a
    assert a["totals"] == {"added": 1, "modified": 0, "removed": 0}
    assert a["merged"]["rate-limits"]["created"] is True
    living = tmp_path / "openspec" / "specs" / "rate-limits" / "spec.md"
    text = living.read_text(encoding="utf-8")
    assert text.startswith("# rate-limits\n\n## Purpose\n\nPer-caller request budgets")
    assert "## Requirements\n" in text
    assert "### Requirement: Per-caller budget" in text
    assert "- **THEN** the next request is rejected with 429" in text
    assert not _change(tmp_path, "demo").exists()
    archive = tmp_path / "openspec" / "changes" / "archive"
    moved = list(archive.iterdir())
    assert len(moved) == 1 and moved[0].name.endswith("-demo")
    assert (moved[0] / "proposal.md").is_file()
    s = _j(spec_status(root=str(tmp_path)))
    assert s["specs"] == ["rate-limits"] and s["archived"] == [moved[0].name]


def test_archive_modified_of_absent_requirement_errors_and_keeps_dir(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    delta = DELTA_ADDED.replace("## ADDED Requirements", "## MODIFIED Requirements")
    _fill(tmp_path, "demo", delta=delta)
    assert _j(spec_validate("demo", root=str(tmp_path)))["ok"]
    a = _j(spec_archive("demo", root=str(tmp_path)))
    assert not a["ok"]
    assert any("MODIFIED requirement 'Per-caller budget' not found" in e["message"]
               for e in a["errors"])
    assert _change(tmp_path, "demo").is_dir()
    assert not (tmp_path / "openspec" / "specs").exists()
    assert not (tmp_path / "openspec" / "changes" / "archive").exists()


def test_archive_modified_then_removed_round_trip(tmp_path: Path):
    spec_new("one", root=str(tmp_path))
    _fill(tmp_path, "one")
    assert _j(spec_archive("one", root=str(tmp_path)))["ok"]
    living = tmp_path / "openspec" / "specs" / "rate-limits" / "spec.md"

    spec_new("two", root=str(tmp_path))
    modified = (DELTA_ADDED.replace("## ADDED Requirements", "## MODIFIED Requirements")
                .replace("rejected with 429", "rejected with 503"))
    _fill(tmp_path, "two", delta=modified)
    a = _j(spec_archive("two", root=str(tmp_path)))
    assert a["ok"] and a["totals"]["modified"] == 1
    text = living.read_text(encoding="utf-8")
    assert "rejected with 503" in text and "rejected with 429" not in text
    assert text.count("### Requirement: Per-caller budget") == 1

    spec_new("three", root=str(tmp_path))
    removed = "# d\n\n## REMOVED Requirements\n\n### Requirement: Per-caller budget\nGone.\n"
    _fill(tmp_path, "three", delta=removed)
    a = _j(spec_archive("three", root=str(tmp_path)))
    assert a["ok"] and a["totals"]["removed"] == 1
    text = living.read_text(encoding="utf-8")
    assert "### Requirement:" not in text and text.startswith("# rate-limits")


# ── export ────────────────────────────────────────────────────────────────────


def test_export_concatenates_and_carries_control_string(tmp_path: Path):
    spec_new("demo", root=str(tmp_path))
    _fill(tmp_path, "demo")
    e = _j(spec_export("demo", root=str(tmp_path)))
    assert e["ok"]
    p = Path(e["path"])
    assert p.name == "export.md" and p.parent == _change(tmp_path, "demo").resolve()
    text = p.read_text(encoding="utf-8")
    assert CONTROL in text
    assert "### Requirement: Per-caller budget" in text
    assert "- [ ] 1.1 Add the budget counter" in text
    assert text.index("## Why") < text.index("## ADDED Requirements") < text.index("## 1. Implementation")
    assert [Path(x).name for x in e["included"]] == ["proposal.md", "spec.md", "tasks.md"]


# ── deltas.py on plain text ───────────────────────────────────────────────────


def test_parse_delta_sections_and_scenarios():
    text = (DELTA_ADDED
            + "\n## MODIFIED Requirements\n\n### Requirement: Other\nText.\n\n#### Scenario: s\n"
              "- **WHEN** x\n\n## REMOVED Requirements\n\n### Requirement: Old\n"
              "\n## Notes\n\n### Requirement: Ignored\n")
    d = parse_delta(text)
    assert [b.name for b in d["ADDED"]] == ["Per-caller budget"]
    assert [b.name for b in d["MODIFIED"]] == ["Other"]
    assert [b.name for b in d["REMOVED"]] == ["Old"]
    sc = d["ADDED"][0].scenarios[0]
    assert sc.name == "over budget" and sc.has_when and sc.has_then and sc.line == 11
    sc2 = d["MODIFIED"][0].scenarios[0]
    assert sc2.has_when and not sc2.has_then
    assert d["ADDED"][0].text.startswith("### Requirement: Per-caller budget\n")


def test_merge_into_living_is_all_or_nothing():
    living = "# cap\n\n## Purpose\n\nP.\n\n## Requirements\n\n### Requirement: A\nold A\n"
    d = parse_delta("## ADDED Requirements\n\n### Requirement: B\nnew B\n\n"
                    "## MODIFIED Requirements\n\n### Requirement: Missing\nx\n")
    new, counts, errors = merge_into_living(living, d)
    assert new == living and counts == {"added": 0, "modified": 0, "removed": 0}
    assert errors == ["MODIFIED requirement 'Missing' not found in the living spec"]

    d = parse_delta("## ADDED Requirements\n\n### Requirement: A\ndup\n")
    _, _, errors = merge_into_living(living, d)
    assert "already exists" in errors[0]

    d = parse_delta("## ADDED Requirements\n\n### Requirement: B\nnew B\n\n"
                    "## MODIFIED Requirements\n\n### Requirement: A\nnew A\n")
    new, counts, errors = merge_into_living(living, d)
    assert errors == [] and counts == {"added": 1, "modified": 1, "removed": 0}
    assert new == ("# cap\n\n## Purpose\n\nP.\n\n## Requirements\n\n### Requirement: A\nnew A\n\n"
                   "### Requirement: B\nnew B\n")


def test_merge_adds_requirements_heading_when_absent():
    d = parse_delta("## ADDED Requirements\n\n### Requirement: A\ntext\n")
    new, counts, errors = merge_into_living("# cap\n", d)
    assert errors == [] and counts["added"] == 1
    assert new == "# cap\n\n## Requirements\n\n### Requirement: A\ntext\n"


# ── cli ───────────────────────────────────────────────────────────────────────


def test_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture):
    p = argparse.ArgumentParser()
    add_spec_parser(p.add_subparsers(dest="command"))
    rc = cmd_spec(p.parse_args(["spec", "new", "demo", "--why", "Because.", "--root", str(tmp_path)]))
    assert rc == 0 and _j(capsys.readouterr().out)["ok"]
    rc = cmd_spec(p.parse_args(["spec", "validate", "demo", "--root", str(tmp_path)]))
    assert rc == 1 and _j(capsys.readouterr().out)["ok"] is False
    _fill(tmp_path, "demo")
    rc = cmd_spec(p.parse_args(["spec", "tasks", "demo", "--done", "1.1", "--root", str(tmp_path)]))
    assert rc == 0 and _j(capsys.readouterr().out)["ticked"]["id"] == "1.1"
    rc = cmd_spec(p.parse_args(["spec", "status", "--root", str(tmp_path)]))
    assert rc == 0 and _j(capsys.readouterr().out)["changes"][0]["tasks"]["done"] == 1
    rc = cmd_spec(p.parse_args(["spec", "export", "demo", "--root", str(tmp_path)]))
    assert rc == 0 and _j(capsys.readouterr().out)["path"].endswith("export.md")
    rc = cmd_spec(p.parse_args(["spec", "archive", "demo", "--root", str(tmp_path)]))
    assert rc == 0 and _j(capsys.readouterr().out)["totals"]["added"] == 1
    rc = cmd_spec(p.parse_args(["spec"]))
    assert rc == 2


def test_loader_discovers_specflow():
    from adk.tool_pack_loader import ToolPackLoader
    m = ToolPackLoader(enforce_entitlements=False).discover().get("specflow")
    assert m is not None
    assert m.tool_matches("spec_new") and not m.tool_matches("dr_recall")
    assert m.tool_modules == [] and m.entitlements == []
    assert "Fission-AI/OpenSpec" in m.description and "MIT" in m.description
