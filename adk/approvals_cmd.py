"""``adk approvals`` -- the owner's durable allow list for tool approval.

A per-call Allow answers one card. A tool the owner runs all day (opening a page,
searching the web) should not cost a card per call, so this records the decision
once. It never overrides a runtime gate: a live guard that raised on evidence
(the Hearth taint guard) still asks.
"""

from __future__ import annotations

import argparse
import json

from adk.approval import allow_tool, forget_tool, list_allow


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="adk approvals",
                                 description="Tools the owner has allowed without a card.")
    sub = ap.add_subparsers(dest="verb")

    p_list = sub.add_parser("list", help="show every durable allow")  # noqa: F841 -- verb
    p_allow = sub.add_parser("allow", help="stop asking for this tool")
    p_allow.add_argument("tool")
    p_allow.add_argument("--agent", default="", help="only for this agent (default: every agent)")
    p_allow.add_argument("--note", default="", help="why (kept with the entry)")
    p_forget = sub.add_parser("forget", help="ask again for this tool")
    p_forget.add_argument("tool")
    p_forget.add_argument("--agent", default="")

    args = ap.parse_args(argv if argv is not None else [])
    verb = args.verb or "list"
    if verb == "list":
        entries = list_allow()
        if not entries:
            print("no durable allows (every gated tool asks first)")
            return 0
        for key, meta in sorted(entries.items()):
            print(f"{key:40s} {meta.get('added', '')}  {meta.get('note', '')}")
        return 0
    if verb == "allow":
        path = allow_tool(args.tool, agent_name=args.agent, note=args.note)
        print(json.dumps({"allowed": args.tool, "agent": args.agent or "*", "file": str(path)}))
        return 0
    if verb == "forget":
        gone = forget_tool(args.tool, agent_name=args.agent)
        print(json.dumps({"forgot": args.tool, "was_allowed": gone}))
        return 0 if gone else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
