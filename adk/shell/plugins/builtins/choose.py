"""
Choose Plugin for AitherShell
==============================

Ask the AitherOS decision door (a bounded fork), and teach it.
(`/decide` is the decision-CARDS verb -- asks to a human. This asks the model.)

Usage:
    /choose <fork> <state> -- <opt1> <opt2> [...]     choice
    /choose <fork> <state> --yesno                     P(yes)
    /choose <fork> <state> --score [1 2 3 4 5]         a scale point (or 0..1)
    /choose outcome <decision_id> <reward>             teach: -1..1
    /choose stats                                      what has it learned

The answer says WHERE it came from: `engine` (learned from outcomes on this
fork -- no model call, microseconds), `llm` (the local brain, first time it
sees this state), `prior` (what has worked in this fork regardless of state),
or `none` (confidence 0: it is guessing and says so). Post the outcome and the
next identical state is answered from evidence.
"""

from typing import Any, Dict, List, Optional

from adk.shell.plugins import SlashCommand


def _parse(args: List[str]) -> Dict[str, Any]:
    """`fork state words... -- options` / `--yesno` / `--score [points]`."""
    if len(args) < 2:
        raise ValueError("usage: /choose <fork> <state> -- <opt1> <opt2>  (or --yesno / --score)")
    fork = args[0]
    rest = args[1:]
    kind, options = "choice", None
    if "--" in rest:
        i = rest.index("--")
        state, options = " ".join(rest[:i]), rest[i + 1 :]
    elif "--yesno" in rest:
        i = rest.index("--yesno")
        state, kind = " ".join(rest[:i] + rest[i + 1 :]), "yesno"
    elif "--score" in rest:
        i = rest.index("--score")
        state, kind = " ".join(rest[:i]), "score"
        options = rest[i + 1 :] or None
    else:
        raise ValueError("say what the answers are: `-- a b c`, `--yesno` or `--score`")
    if not state.strip():
        raise ValueError("state is empty -- describe the situation at this fork")
    if kind == "choice" and (not options or len(options) < 2):
        raise ValueError("a choice needs at least two options after `--`")
    return {"fork": fork, "state": state.strip(), "kind": kind, "options": options}


def render(d: Any) -> str:
    conf = f"{d.confidence:.0%}"
    line = f"**{d.answer}**  ({conf}, {d.source}"
    if d.source == "engine":
        line += f", learned from {d.learned_from}"
    line += f", {d.latency_ms:.0f} ms)  id `{d.decision_id}`"
    if d.alternatives:
        seen = [
            f"{a['answer']}={a['value']:+.2f}x{a['n']}"
            for a in d.alternatives
            if a.get("value") is not None
        ]
        if seen:
            line += "\n  evidence: " + ", ".join(seen)
    return line


class ChoosePlugin(SlashCommand):
    name: str = "choose"
    aliases: List[str] = ["fork"]
    description: str = "Ask the decision door (choice/score/yesno) and teach it outcomes"
    category: str = "labs"

    def __init__(self) -> None:
        super().__init__(
            name="choose",
            aliases=["fork"],
            description="Ask the decision door (choice/score/yesno) and teach it outcomes",
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        from adk import choose as door

        try:
            if not args or args[0] in ("help", "-h", "--help"):
                return __doc__ or "choose"
            if args[0] == "stats":
                st = door.stats()
                rows = st.get("domains") or {}
                if not rows:
                    return "No decisions yet."
                return "\n".join(
                    f"{d}: {v['decisions']} decisions, engine {v['engine_pct']}%, "
                    f"llm {v['llm_pct']}%, outcomes {v['outcomes']}, "
                    f"mean reward {v['mean_reward']}"
                    for d, v in rows.items()
                )
            if args[0] == "outcome":
                if len(args) != 3:
                    return "usage: /choose outcome <decision_id> <reward -1..1>"
                res = door.outcome(args[1], float(args[2]))
                return (
                    f"taught: {res.get('answer')} -> {res.get('reward'):+.2f} "
                    f"(observed {res.get('observed')}, mode {res.get('mode')})"
                )
            req = _parse(args)
            d = door.decide(req["fork"], req["state"], options=req["options"], kind=req["kind"])
            return render(d)
        except ValueError as e:
            return f"[x] {e}"
        except door.DecideUnavailableError as e:
            return f"[!] {e}"

    def get_help(self) -> str:
        return __doc__ or "choose"
