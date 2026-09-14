# Build sick agents that speak **standard A2A**

An `awdk` agent is a standard [A2A (Agent2Agent)](https://a2a-protocol.org)
server out of the box. That means an agent you build here **drops straight into any
A2A infrastructure** — Google's A2A, LangGraph/CrewAI A2A bridges, or a plain
`curl` — with no Aitherium account, portal, or API key required. The Aitherium
extensions (Ed25519 peer trust, fleet registry, remote `skills/invoke`) are
**opt-in** and layered on top; the interop core is pure spec.

## What's standard vs. what's an extension

| | Standard A2A (interops with anyone) | Aitherium extension (opt-in) |
|---|---|---|
| **Discovery** | `GET /.well-known/agent-card.json` | `public_key` on the card + fleet registry |
| **Messaging** | `POST /a2a` → `message/send`, `tasks/get`, `tasks/cancel` | — |
| **Streaming** | SSE at `GET /a2a/tasks/{id}/subscribe` | — |
| **Remote tools** | — | `skills/invoke` (run your tools remotely), hard-gated by Ed25519 trust |
| **Auth** | `message/send` works **unsigned** | `AITHER_A2A_REQUIRE_TRUST` + signed requests |

Key point: **talking to an agent needs no Aitherium auth.** Only letting a peer
execute your local tools (`skills/invoke`) requires a trusted signature.

## The recipes

| File | What it shows |
|---|---|
| [`serve_standard_a2a.py`](serve_standard_a2a.py) | Expose your agent as an A2A server in ~10 lines — discoverable + drivable by any A2A client. |
| [`call_any_a2a_agent.py`](call_any_a2a_agent.py) | Call **any** external A2A agent (Google's, LangGraph, another adk) by its card URL — vendor-neutral. |
| [`sick_agent.py`](sick_agent.py) | The full pattern: typed `@tool`s + persistent memory + BYO model, served over A2A. |
| [`interop_selftest.py`](interop_selftest.py) | Offline proof (plain httpx client ↔ your agent, no model, no port) — run it in CI: exit 0 = interop OK. |

## 60-second version

```python
from fastapi import FastAPI
from adk import AitherAgent, A2AServer, tool
from adk.tools import get_global_registry

@tool
def add(a: float, b: float) -> str:
    "Add two numbers."
    return str(a + b)

agent = AitherAgent("aither", tools=[get_global_registry()])   # BYO model (Ollama default)
app = FastAPI()
A2AServer(agent=agent, base_url="http://localhost:8080").mount(app)
# uvicorn app  →  now any A2A client can hit /.well-known/agent-card.json + /a2a
```

Then from **any** A2A stack:

```bash
curl http://localhost:8080/.well-known/agent-card.json
curl -X POST http://localhost:8080/a2a -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","id":1,"method":"message/send",
  "params":{"message":{"role":"user","parts":[{"type":"text","text":"hi"}]}}}'
```

## Bring your own model

Every example runs on a **local Ollama** by default (no keys, no caps). Point it at
anything else with an env key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`DEEPSEEK_API_KEY`, `GROQ_API_KEY`, …) or `--backend`. Local + BYO backends are
**never** metered — caps only apply to Aitherium's optional hosted gateway.

## Verified

`interop_selftest.py` drives an adk agent with a plain JSON-RPC client over an
in-process ASGI transport and asserts the card shape + `message/send` +
`tasks/get` lifecycle — **PASS** with zero Aitherium headers. That's the whole
claim, checkable in one command.
