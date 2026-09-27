"""``python -m adk.skilltasks {gate,freeze,run}`` -- exit 0 ok, 1 rejected/failed, 2 cannot judge."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m adk.skilltasks")
    sub = p.add_subparsers(dest="cmd")
    g = sub.add_parser("gate", help="acceptance gate over one task or a directory of tasks")
    g.add_argument("root", nargs="?")
    g.add_argument("--json", action="store_true")
    g.add_argument("--self-test", action="store_true")
    f = sub.add_parser("freeze", help="freeze tests/ + instruction.md before solution/ exists")
    f.add_argument("task")
    r = sub.add_parser("run", help="run the reasoning loop on a task with a LOCAL model")
    r.add_argument("task")
    r.add_argument("--model", required=True)
    r.add_argument("--max-calls", type=int, default=12)
    r.add_argument("--wall-s", type=float, default=900.0)
    r.add_argument("--scheduler-url", default=None)
    r.add_argument("--run-dir", default=None)
    r.add_argument("--mode", choices=("plain", "sase", "terminal-plain", "terminal-sase"),
                   default="plain")
    args = p.parse_args(argv)

    if args.cmd == "gate":
        from .gate import gate_root, self_test

        if args.self_test:
            return self_test()
        if not args.root:
            p.error("gate needs a task directory (or --self-test)")
        code, reports = gate_root(Path(args.root))
        if args.json:
            print(json.dumps({"exit_code": code, "tasks": [r.to_dict() for r in reports]}, indent=2))
        else:
            if not reports:
                print("cannot judge: no task.json under %s" % args.root)
            for rep in reports:
                verdict = "ACCEPTED" if rep.accepted else ("CANNOT JUDGE" if rep.could_not_judge
                                                           else "REJECTED")
                print("%-9s %s  oracle=%s nop=%s probes=%s" % (
                    verdict, rep.task,
                    rep.oracle and rep.oracle["reward"], rep.nop and rep.nop["reward"], rep.probes))
                for e in rep.errors:
                    print("    - " + e)
        return code
    if args.cmd == "freeze":
        from .freeze import FreezeError, freeze

        try:
            doc = freeze(Path(args.task))
        except FreezeError as exc:
            print(str(exc))
            return 1
        print("frozen %d file(s) at %s" % (len(doc["files"]), doc["frozen_at"]))
        return 0
    if args.cmd == "run":
        from .run import main_run

        return main_run(args)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
