"""Multi-agent fleet running on Aitherium cloud — zero local setup.

Spins up 4 agents that auto-delegate tasks between each other.
All inference happens on mcp.aitherium.com. No GPU required.

Setup:
    pip install awdk
    export AITHER_API_KEY=aither_sk_live_...
    python cloud_fleet.py
"""

import asyncio
from adk import AitherAgent
from adk.forge import AgentForge, ForgeSpec

async def main():
    # Create specialized agents — each gets Aitherium's 16 built-in identities
    coder = AitherAgent("demiurge")     # Code generation + refactoring
    reviewer = AitherAgent("hydra")      # Code review + quality
    security = AitherAgent("athena")     # Security analysis
    orchestrator = AitherAgent("aither") # Orchestration + delegation

    # Or use Forge for automatic routing — picks the right agent for the task
    forge = AgentForge()

    # Auto-routes to the best agent based on task + effort level
    result = await forge.dispatch(ForgeSpec(
        task="Write a Python FastAPI endpoint that accepts file uploads with virus scanning",
        effort=7,  # 7+ = reasoning model, deep analysis
    ))
    print(f"Agent: {result.agent}")
    print(f"Result: {result.content[:500]}")

    # Chain agents: coder → reviewer → security
    results = await forge.chain([
        ForgeSpec(agent_type="demiurge", task="Write a JWT auth middleware for FastAPI"),
        ForgeSpec(agent_type="hydra", task="Review this code for quality issues"),
        ForgeSpec(agent_type="athena", task="Audit this code for security vulnerabilities"),
    ])

    for r in results:
        print(f"\n[{r.agent}] ({r.status})")
        print(r.content[:300])


if __name__ == "__main__":
    asyncio.run(main())
