"""Decision cards — the anti-wall-of-text channel between an agent and its owner.

A coding agent that needs a human has exactly one channel today: prose in a
terminal. That channel loses. The important sentence sits in paragraph four of a
dense block, the owner is in another window, and the session sits idle for forty
minutes because nobody knew it was waiting.

A decision card inverts that. The agent writes a small, structured artifact —
headline, what you need to know, the options, the recommendation, what happens if
you say nothing — into a durable store. Every surface then renders the SAME card:
the terminal, a native toast, AitherShell's cockpit, aitherium.com, the phone.

Three properties are load-bearing and each one is a rule, not a nicety:

1. **The card is durable and out-of-process.** It lives in ``~/.aither/decisions``,
   not in a transcript. A card outlives the session that raised it, so closing the
   laptop does not lose the question, and a session in another Windows Terminal tab
   can raise one without this process cooperating at all.

2. **A card always states its default.** "What happens if you ignore this" is the
   field that makes a card safe to ignore. An agent that cannot name a default is
   not blocked on a decision — it is blocked on doing its own thinking.

3. **Answering closes the loop.** The answer is written back to the raising
   session's steering mailbox, which its hooks drain. A card that a human answers
   into a void is worse than no card, because it reports resolution that never
   reached anyone.

See ``.claude/skills/decision-card/SKILL.md`` for when an agent should raise one,
and ``.PRODUCTS/.AITHERSHELL/COCKPIT-DESIGN.md`` for how the cockpit consumes them.
"""

from __future__ import annotations

from adk.decisions.store import (
    STATUS_ANSWERED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    DecisionCard,
    DecisionOption,
    DecisionStore,
    get_store,
)

__all__ = [
    "DecisionCard",
    "DecisionOption",
    "DecisionStore",
    "get_store",
    "STATUS_OPEN",
    "STATUS_ANSWERED",
    "STATUS_EXPIRED",
    "STATUS_CANCELLED",
]
