"""``adk spec`` — the specflow tools from the command line.

``add_spec_parser(sub)`` attaches the ``spec`` subcommand to an existing
``add_subparsers()`` object; ``cmd_spec(args)`` runs it and prints the tool's
JSON. Exit 0 when the tool reports ``ok``, 1 otherwise, 2 for a missing action.
"""
from __future__ import annotations

import argparse
import json

from . import tools

ACTIONS = ("new", "validate", "status", "tasks", "archive", "export")


def add_spec_parser(sub) -> argparse.ArgumentParser:
    """Attach ``spec {new,validate,status,tasks,archive,export}`` to *sub*."""
    p = sub.add_parser(
        "spec",
        help="Spec-driven change workflow: proposal -> delta specs -> tasks -> archive",
        description=("Manage openspec/changes/<change-id>/ (proposal.md, specs/<capability>/"
                     "spec.md, design.md, tasks.md) and merge shipped changes into the living "
                     "specs under openspec/specs/. Output is JSON."),
    )
    ssub = p.add_subparsers(dest="spec_action")

    def _root(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default=".", help="Project dir holding openspec/ (default: .)")

    sp = ssub.add_parser("new", help="Scaffold a change (proposal, design, tasks, .openspec.yaml)")
    sp.add_argument("change_id", help="One path segment, e.g. add-rate-limits")
    sp.add_argument("--why", default="", help="Text for the proposal's Why section")
    _root(sp)

    sp = ssub.add_parser("validate", help="Check a change: Why, delta specs, scenarios, tasks")
    sp.add_argument("change_id")
    _root(sp)

    sp = ssub.add_parser("status", help="List open changes with task progress and validity")
    _root(sp)

    sp = ssub.add_parser("tasks", help="List a change's tasks; --done N.M ticks one")
    sp.add_argument("change_id")
    sp.add_argument("--done", default="", metavar="N.M", help="Task id to mark complete")
    _root(sp)

    sp = ssub.add_parser("archive", help="Validate, merge deltas into living specs, move to archive/")
    sp.add_argument("change_id")
    _root(sp)

    sp = ssub.add_parser("export", help="Write <change>/export.md (proposal + deltas + tasks)")
    sp.add_argument("change_id")
    _root(sp)
    return p


def cmd_spec(args) -> int:
    """Run the chosen spec action and print its JSON. Returns the exit code."""
    action = getattr(args, "spec_action", None)
    root = getattr(args, "root", ".")
    if action == "new":
        out = tools.spec_new(args.change_id, why=args.why, root=root)
    elif action == "validate":
        out = tools.spec_validate(args.change_id, root=root)
    elif action == "status":
        out = tools.spec_status(root=root)
    elif action == "tasks":
        out = tools.spec_tasks(args.change_id, root=root, done=args.done)
    elif action == "archive":
        out = tools.spec_archive(args.change_id, root=root)
    elif action == "export":
        out = tools.spec_export(args.change_id, root=root)
    else:
        print(json.dumps({"ok": False, "error": f"adk spec {{{','.join(ACTIONS)}}}"}))
        return 2
    print(out)
    try:
        return 0 if json.loads(out).get("ok") else 1
    except ValueError:
        return 1
