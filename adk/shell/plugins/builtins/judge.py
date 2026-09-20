"""
Judge Plugin for AitherShell
=============================

Grade output against criteria -- the eval-judge shape, on the decision door.

Usage:
    /judge <file> -- <criterion> ; <criterion> ...     grade a file's contents
    /judge - -- <criterion> ...                        grade the last output
    /judge teach <decision_id> right|wrong             a verdict was right/wrong
    /judge truth <file> -- <criterion> [yes|no]        state the truth outright

Each verdict says where it came from: `engine` (learned from outcomes on this
shape of output -- no model call, microseconds), `llm` (a model was asked), or
`none`, in which case the verdict is UNKNOWN rather than a pass. A judge that
cannot judge must not report a pass; a failing build graded green in silence is
the worst thing this can do.

The door keys on FEATURES of the output (traceback present, pass/fail counts,
exit status, criterion-keyword overlap), never on the text, so the second run of
the same kind of task is answered from evidence. Teach a correction once and it
is wrong once, not forever.

    /judge ci.log -- the tests passed ; no traceback is present
    /judge teach 7f31a0c9 wrong
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adk.shell.plugins import SlashCommand

_MAX_CHARS = 200_000


def _split(args: List[str]) -> Tuple[str, List[str]]:
    """`<source> -- <criterion> ; <criterion>` -> (source, criteria)."""
    if "--" not in args:
        raise ValueError("say what to grade: /judge <file|-> -- <criterion> ; <criterion>")
    i = args.index("--")
    source = " ".join(args[:i]).strip()
    tail = " ".join(args[i + 1 :]).strip()
    if not source:
        raise ValueError("no file given (use `-` to grade the last output)")
    criteria = [c.strip() for c in tail.split(";") if c.strip()]
    if not criteria:
        raise ValueError("no criteria given (separate several with `;`)")
    return source, criteria


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


def render(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    for v in result.get("verdicts") or []:
        mark = "?" if v.get("pass") is None else ("PASS" if v.get("pass") else "FAIL")
        prob = v.get("probability")
        prob_s = "" if prob is None else f"  p={prob:.2f}"
        lines.append(
            f"[{mark:4}] {v.get('criterion')}  ({v.get('source')}"
            f"{prob_s}, id {v.get('decision_id')})"
        )
    lines.append(
        f"-- {result.get('passed', 0)} pass / {result.get('failed', 0)} fail / "
        f"{result.get('unknown', 0)} unknown of {result.get('of', 0)}; "
        f"{result.get('from_evidence', 0)} answered from evidence, "
        f"{result.get('from_model', 0)} from a model"
    )
    return "\n".join(lines)


class JudgePlugin(SlashCommand):
    def __init__(self) -> None:
        super().__init__(
            name="judge",
            aliases=["grade"],
            description="Grade output against criteria on the decision door, and teach it",
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        from adk import choose as door

        try:
            if not args or args[0] in ("help", "-h", "--help"):
                return __doc__ or "judge"

            if args[0] == "teach":
                if len(args) != 3 or args[2] not in ("right", "wrong"):
                    return "usage: /judge teach <decision_id> right|wrong"
                res = door.judge_outcome(args[1], args[2] == "right")
                return f"taught: {res.get('answer')} -> {res.get('reward')}"

            if args[0] == "truth":
                rest = args[1:]
                source, criteria = _split(rest)
                verdict = True
                if criteria and criteria[-1].lower().split()[-1] in ("yes", "no"):
                    words = criteria[-1].split()
                    verdict = words[-1].lower() == "yes"
                    criteria[-1] = " ".join(words[:-1]).strip()
                text = _read(source, ctx)
                out = [
                    door.judge_outcome(criterion=c, should_pass=verdict, output=text)
                    for c in criteria
                    if c
                ]
                return f"taught {len(out)} criterion/criteria as {'pass' if verdict else 'fail'}"

            source, criteria = _split(args)
            return render(door.judge(_read(source, ctx), criteria))
        except ValueError as e:
            return f"[x] {e}"
        except door.DecideUnavailableError as e:
            return f"[!] {e}"

    def get_help(self) -> str:
        return __doc__ or "judge"
