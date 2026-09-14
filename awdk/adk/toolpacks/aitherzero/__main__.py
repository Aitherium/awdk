"""CLI for AitherZero pack tools via argparse.

Subcommands map 1:1 to az_* tools. Output is JSON to stdout. Exit 0 on success,
1 when the result dict contains an "error" key. Useful for a live smoke test:
    python -m adk.toolpacks.aitherzero inventory
    python -m adk.toolpacks.aitherzero describe Bootstrap-AitherOS
"""

from __future__ import annotations

import argparse
import json
import sys

from . import tools


def main() -> int:
    p = argparse.ArgumentParser(
        description="AitherZero — inventory, config generation & automation authoring",
        prog="python -m adk.toolpacks.aitherzero",
    )
    p.add_argument("--root", default=None, help="AitherZero product root (holds config/ + library/)")
    sub = p.add_subparsers(dest="command", help="subcommand")

    inv = sub.add_parser("inventory", help="list scripts + playbooks")
    inv.add_argument("--category", default=None, help="filter to one category")

    desc = sub.add_parser("describe", help="describe one script's parameters")
    desc.add_argument("name", help="script name (exact or substring)")

    exp = sub.add_parser("export-schema", help="regenerate config-schema.json")
    exp.add_argument("--script-root", default=None, help="automation-scripts folder (public or private)")
    exp.add_argument("--playbook-root", default=None)

    gen = sub.add_parser("generate", help="emit config.local.psd1 from JSON overrides")
    gen.add_argument("--sections", default="{}", help="JSON of section overrides")
    gen.add_argument("--automation", default="{}", help='JSON of {"cat/Script":{param:val}}')

    val = sub.add_parser("validate", help="run the fail-closed config traps")
    val.add_argument("--path", default=None, help="config psd1 path")

    plan = sub.add_parser("plan-playbook", help="resolve a playbook into steps")
    plan.add_argument("name", help="playbook name")

    args = p.parse_args()
    if not args.command:
        p.print_help()
        return 1

    if args.command == "inventory":
        result = tools.az_inventory(category=args.category, root=args.root)
    elif args.command == "describe":
        result = tools.az_describe_script(args.name, root=args.root)
    elif args.command == "export-schema":
        result = tools.az_export_schema(
            script_root=args.script_root, playbook_root=args.playbook_root, root=args.root)
    elif args.command == "generate":
        try:
            sections = json.loads(args.sections)
            automation = json.loads(args.automation)
        except json.JSONDecodeError as e:
            result = {"error": f"bad JSON: {e}"}
        else:
            result = tools.az_generate_config(sections=sections, automation=automation)
    elif args.command == "validate":
        result = tools.az_validate_config(psd1_path=args.path, root=args.root)
    elif args.command == "plan-playbook":
        result = tools.az_plan_playbook(args.name, root=args.root)
    else:
        p.print_help()
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 1 if isinstance(result, dict) and "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
