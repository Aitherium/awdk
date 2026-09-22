"""
Compact Plugin for AitherShell
===============================

Shrink a long tool output through the decision door -- per LINE SHAPE, not per
word, so the second output of the same kind costs no model call.

Usage:
    /compact <file> [--tool pytest] [--budget 40]   compact a file
    /compact - [--tool bash]                        compact the last output
    /compact teach <id> [<id> ...]                  those verdicts were WRONG
    /compact right <id> [<id> ...]                  those verdicts were right
    /compact shape <tool> keep|drop <shape>         teach a shape outright

`--tool` names the FORK (`decide.compact.<tool>`), so pytest output and build
logs learn separately; the default is guessed from the file's extension or
"tool". `--json` prints the decisions instead of the text.

What comes back says where each verdict came from: `engine` (learned on this
fork -- microseconds, no model call), `llm` (a model was asked about the SHAPE,
never the text), `none`/`no-door` (nothing to go on -- the line is KEPT; the one
unforgivable failure here is dropping a line on no evidence).

Tracebacks, summary lines ("42 passed", "exit code: 1"), error lines and the
first two and last five lines are kept by RULE and never sent anywhere.

    /compact ci.log --tool pytest
    /compact teach 7f31a0c9        # that dropped line mattered
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand

_MAX_CHARS = 2_000_000
_EXT_TOOL = {
    ".log": "log", ".txt": "tool", ".json": "json", ".diff": "git", ".patch": "git",
}
_NAME_TOOL = ("pytest", "ruff", "mypy", "npm", "podman", "docker", "git", "curl", "build")


def _guess_tool(source: str) -> str:
    low = source.lower()
    for name in _NAME_TOOL:
        if name in low:
            return name
    return _EXT_TOOL.get(Path(source).suffix.lower(), "tool")


def _parse(args: List[str]) -> Dict[str, Any]:
    """`<file|-> [--tool X] [--budget N] [--json] [--url U]`."""
    src: Optional[str] = None
    opts: Dict[str, Any] = {"tool": None, "budget": 40, "json": False, "url": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--tool" and i + 1 < len(args):
            opts["tool"] = args[i + 1]
            i += 2
        elif a == "--budget" and i + 1 < len(args):
            opts["budget"] = int(args[i + 1])
            i += 2
        elif a == "--url" and i + 1 < len(args):
            opts["url"] = args[i + 1]
            i += 2
        elif a == "--json":
            opts["json"] = True
            i += 1
        elif src is None:
            src = a
            i += 1
        else:
            raise ValueError(f"unexpected argument: {a}")
    if not src:
        raise ValueError("usage: /compact <file|-> [--tool pytest] [--budget 40]")
    opts["source"] = src
    opts["tool"] = opts["tool"] or _guess_tool(src)
    return opts


def _read(source: str, ctx: Dict[str, Any]) -> str:
    if source == "-":
        for key in ("last_output", "last_result", "last_stdout"):
            val = ctx.get(key)
            if isinstance(val, str) and val.strip():
                return val[:_MAX_CHARS]
        raise ValueError("no last output in this session -- pass a file instead")
    path = Path(source).expanduser()
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    return path.read_text(encoding="utf-8", errors="replace")[:_MAX_CHARS]


def render(result: Any) -> str:
    saved = result.total_lines - result.kept_lines
    tail = (
        f"\n\n-- kept {result.kept_lines} of {result.total_lines} lines "
        f"({saved} dropped, {result.mode}, {result.latency_ms:.0f} ms); "
        f"sources {result.source_counts or '{}'}"
    )
    ids = result.decision_ids()
    if ids:
        tail += f"\n-- a dropped line mattered? /compact teach {ids[0]}"
    return result.kept + tail


def render_decisions(result: Any) -> str:
    lines = [f"{result.tool}: {result.kept_lines}/{result.total_lines} lines kept "
             f"({result.mode})"]
    for d in sorted(result.decisions, key=lambda x: -x["count"]):
        lines.append(
            f"  [{d['verdict']:<7}] x{d['count']:<4} {d['source']:<9} "
            f"conf {d.get('confidence', 0):.2f}  {d['shape']}  id {d.get('decision_id')}"
        )
    return "\n".join(lines)


class CompactPlugin(SlashCommand):
    # Class attributes, not only __init__ kwargs: PluginRegistry discovers a
    # builtin by `attr.name` on the CLASS, so a plugin that sets its name only
    # in __init__ is silently never registered (measured 2026-09-21: /judge and
    # /compact both absent while their modules imported cleanly).
    name: str = "compact"
    aliases: List[str] = ["shrink"]
    description: str = "Compact a long tool output through the decision door, and teach it"
    category: str = "labs"

    def __init__(self) -> None:
        super().__init__(
            name="compact",
            aliases=["shrink"],
            description="Compact a long tool output through the decision door, and teach it",
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        from adk import compact as api

        try:
            if not args or args[0] in ("help", "-h", "--help"):
                return __doc__ or "compact"

            if args[0] in ("teach", "right"):
                ids = args[1:]
                if not ids:
                    return f"usage: /compact {args[0]} <decision_id> [<decision_id> ...]"
                wrong = ids if args[0] == "teach" else []
                stub = api.CompactResult(
                    kept="", kept_lines=0, total_lines=0, dropped=0,
                    mode="teach", tool=ctx.get("compact_tool", "tool"),
                    decisions=[{"decision_id": i} for i in ids],
                )
                res = api.teach(stub, wrong_ids=wrong)
                bad = [r for r in res if r.get("error")]
                if bad:
                    return f"[!] {len(bad)} of {len(res)} outcomes failed: {bad[0]['error']}"
                verdict = "WRONG" if args[0] == "teach" else "right"
                return f"taught {len(res)} decision(s) as {verdict}"

            if args[0] == "shape":
                if len(args) < 4 or args[2] not in ("keep", "drop"):
                    return "usage: /compact shape <tool> keep|drop <shape descriptor>"
                impl = api._impl()
                if impl is None:
                    return "[!] the world-model service tree is not on this box"
                decider, _ = api._decider("auto", None, 30.0)
                out = impl.teach_shape(decider, args[1], " ".join(args[3:]), args[2] == "keep")
                return (f"taught {args[1]}: {args[2]} for that shape "
                        f"({out.get('observed')} observed)")

            opts = _parse(args)
            text = _read(opts["source"], ctx)
            result = api.compact(
                text, opts["tool"], budget_lines=opts["budget"], url=opts["url"]
            )
            ctx["compact_tool"] = result.tool
            ctx["compact_last"] = result
            return render_decisions(result) if opts["json"] else render(result)
        except ValueError as e:
            return f"[x] {e}"
        except api.CompactUnavailableError as e:
            return f"[!] {e}"

    def get_help(self) -> str:
        return __doc__ or "compact"
