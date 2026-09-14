"""Serve agents as an OpenAI-compatible API — powered by Aitherium cloud.

Exposes your agents at localhost:8080 with:
  - OpenAI-compatible /v1/chat/completions (drop-in replacement)
  - SSE streaming
  - Multi-agent fleet with auto-delegation
  - MCP tool server (other agents can call your tools)

Setup:
    pip install awdk
    export AITHER_API_KEY=aither_sk_live_...

    # Single agent:
    aither run

    # Fleet (multiple agents):
    aither run --agents aither,demiurge,hydra,athena

    # Or programmatically:
    python cloud_server.py
"""

from adk.server import create_app
from adk.config import Config

# Create a fleet server with 4 agents
app = create_app(
    fleet_agents=["aither", "demiurge", "hydra", "athena"],
    config=Config.from_env(),
)

# That's it. Your agents are now accessible at:
#   POST /v1/chat/completions   — OpenAI-compatible (use with any client)
#   POST /chat                  — Simple chat
#   POST /forge/dispatch        — Agent dispatch with effort routing
#   GET  /agents                — List all agents
#   GET  /health                — Health check
#   GET  /metrics               — Prometheus metrics
#
# Connect from any OpenAI client:
#   from openai import OpenAI
#   client = OpenAI(base_url="http://localhost:8080/v1", api_key="unused")
#   response = client.chat.completions.create(
#       model="aither-orchestrator",
#       messages=[{"role": "user", "content": "Hello!"}],
#   )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
