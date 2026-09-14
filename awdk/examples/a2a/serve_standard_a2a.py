"""Expose an awdk agent as a STANDARD A2A server.

Any A2A-compliant client (Google's a2a SDK, LangGraph/CrewAI A2A bridges, or a
plain `curl`) can then discover this agent's card and drive it — no Aitherium
account, portal, or API key required. The Aitherium extensions (Ed25519 trust,
fleet registry, remote `skills/invoke`) are opt-in and layered on top.

Run:
    pip install "awdk[server]"
    python serve_standard_a2a.py
    # then, from anywhere:
    curl http://localhost:8080/.well-known/agent-card.json
    curl -X POST http://localhost:8080/a2a -H 'content-type: application/json' -d '{
      "jsonrpc":"2.0","id":1,"method":"message/send",
      "params":{"message":{"role":"user","parts":[{"type":"text","text":"hi"}]}}}'

By default the agent talks to a local Ollama; set a BYO key (ANTHROPIC_API_KEY,
OPENAI_API_KEY, DEEPSEEK_API_KEY, …) or --backend to use any other model.
"""
import os

import uvicorn
from fastapi import FastAPI

from adk import AitherAgent, A2AServer, tool
from adk.tools import get_global_registry


@tool
def add(a: float, b: float) -> str:
    """Add two numbers."""
    return str(a + b)


def build_app() -> FastAPI:
    # BYO model: local Ollama by default; any API key / --backend also works.
    agent = AitherAgent("aither", tools=[get_global_registry()])

    app = FastAPI(title="my-a2a-agent")
    base_url = os.getenv("A2A_BASE_URL", "http://localhost:8080")
    # .mount() adds:  GET /.well-known/agent-card.json  (+ legacy /agent.json)
    #                 POST /a2a  (JSON-RPC: message/send, tasks/get, tasks/cancel)
    #                 GET  /a2a/tasks/{id}/subscribe  (SSE task stream)
    A2AServer(agent=agent, base_url=base_url).mount(app)
    return app


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
