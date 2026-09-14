"""Self-contained proof that an awdk agent IS a standard A2A server.

Drives the agent with a plain httpx JSON-RPC client (zero Aitherium headers, no
portal, no API key) over an in-process ASGI transport — so it runs offline in CI
with no model and no open port. If this passes, any A2A-compliant stack can talk
to your agent the same way.

Run:  python interop_selftest.py     (exit 0 = interop OK)
"""
import asyncio
import sys
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from adk import AitherAgent, A2AServer


def build_app() -> FastAPI:
    agent = AitherAgent(name="echo-bot")

    # Stub the model so the test is deterministic + offline; the POINT here is the
    # A2A protocol surface, not the LLM. Swap in a real backend for a live agent.
    async def _echo(text, history=None):
        return SimpleNamespace(content=f"echo: {text}", artifacts=[])

    agent.chat = _echo
    app = FastAPI()
    A2AServer(agent=agent, base_url="http://echo-bot.local").mount(app)
    return app


async def main() -> int:
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://echo-bot.local") as c:
        card = (await c.get("/.well-known/agent-card.json")).json()
        std = [f for f in ("name", "url", "version", "capabilities", "skills") if f in card]
        print(f"card: name={card.get('name')!r} standard_fields={std}")

        rpc = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
               "params": {"message": {"role": "user",
                                      "parts": [{"type": "text", "text": "hello from a plain A2A client"}]}}}
        res = (await c.post("/a2a", json=rpc)).json().get("result", {})
        reply = " ".join(p.get("text", "") for p in res.get("message", {}).get("parts", []))
        state = res.get("task", {}).get("status", {}).get("state")
        tid = res.get("task", {}).get("id")
        print(f"message/send: state={state} reply={reply!r}")

        got = (await c.post("/a2a", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                                          "params": {"id": tid}})).json()
        round_tripped = got.get("result", {}).get("task", {}).get("id") == tid
        print(f"tasks/get: round-tripped={round_tripped}")

    ok = (card.get("name") == "echo-bot" and len(std) >= 4 and state == "completed"
          and reply == "echo: hello from a plain A2A client" and round_tripped)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
