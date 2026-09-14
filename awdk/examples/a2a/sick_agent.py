"""A 'sick' agent — tools + memory + streaming, exposed over standard A2A.

This is the pattern for a genuinely useful agent you can drop into any A2A stack:
  • real @tool functions the model can call (typed args, auto-discovered)
  • persistent memory (agent.remember / recalled automatically in later turns)
  • served over A2A so ANY client can discover + drive it
  • BYO model — local Ollama by default, or any API key / --backend

Run it as a server:
    python sick_agent.py serve            # A2A server on :8080
Or drive it locally in-process:
    python sick_agent.py                   # one-shot local chat
"""
import asyncio
import os
import sys

from adk import AitherAgent, tool
from adk.tools import get_global_registry


# ── Tools: typed, documented; the model calls them by name ───────────────────
@tool
def word_count(text: str) -> str:
    """Count the words in a piece of text."""
    return str(len(text.split()))


@tool
def to_kebab(text: str) -> str:
    """Convert text to kebab-case (lowercase, dash-separated)."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@tool
def pick(options: str) -> str:
    """Pick one item from a comma-separated list of options (deterministic hash pick)."""
    items = [o.strip() for o in options.split(",") if o.strip()]
    if not items:
        return "(no options)"
    return items[sum(map(ord, options)) % len(items)]


def build_agent() -> AitherAgent:
    agent = AitherAgent(
        "atlas",                              # persona; BYO model under the hood
        tools=[get_global_registry()],        # auto-discovers the @tool functions above
    )
    return agent


async def run_local():
    """Drive the agent in-process — shows tools + memory in action."""
    agent = build_agent()
    await agent.remember("style_guide", "answers should be terse and use kebab-case for ids")

    r = await agent.chat("How many words are in 'the quick brown fox jumps'? "
                         "Then give me a kebab-case id for a 'User Profile Service'.")
    print(f"[{r.model}] {r.content}")


def serve():
    """Expose the same agent over standard A2A (any A2A client can drive it)."""
    import uvicorn
    from fastapi import FastAPI
    from adk import A2AServer

    app = FastAPI(title="sick-agent")
    A2AServer(agent=build_agent(),
              base_url=os.getenv("A2A_BASE_URL", "http://localhost:8080")).mount(app)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        asyncio.run(run_local())
