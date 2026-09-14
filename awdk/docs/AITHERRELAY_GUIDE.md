# AitherRelay — Realtime Multi-Agent Group Chat (Developer Guide)

`adk.chat.ChatRelay` ("AitherRelay") is awdk's built-in realtime chat room. It gives
you multi-channel chat, **agents as participants** (with `@mention` handlers), WebSocket
fan-out, presence, and persistent history — so you can put **humans and agents in the same
room** without writing a chat server.

This guide shows how to build a realtime, multi-agent group chat on top of it.

> **Mental model:** the **room** is AitherRelay (channels, @mentions, presence, history,
> broadcast). The **brains** are your awdk agents. A small **mention handler** bridges
> them: when someone @mentions an agent, you run the agent's real turn and `post()` the
> reply back into the channel. Clean separation; almost no glue.

```
browser ──ws──▶  AitherRelay (the room)  ──▶  your AitherAgents (the brains)
                 channels · @mentions · presence · history     memory · tools · grounding
```

---

## 0. Quickstart — a group chat in ~40 lines

```python
import asyncio, uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from adk.chat import get_chat_relay
from adk import AitherAgent

relay   = get_chat_relay(node_id="demo")
CHANNEL = "#team"
relay.create_channel(CHANNEL)

AGENTS = {name: AitherAgent(name) for name in ("researcher", "writer", "ops")}

def make_handler(agent):
    # _check_mentions calls handler(msg); a returned coroutine is auto-scheduled.
    async def handle(msg):
        resp = await agent.chat(msg.content)             # the real, grounded turn
        relay.post(CHANNEL, agent.name, resp.content)    # post the reply into the room
    return lambda msg: handle(msg)

for name, agent in AGENTS.items():
    relay.register_agent(name, channels=[CHANNEL], mention_handler=make_handler(agent))

app = FastAPI()

@app.websocket("/ws/chat")
async def ws_chat(sock: WebSocket):
    await sock.accept()
    init = await sock.receive_json()                     # first frame: {nick, channel}
    nick = init.get("nick") or ("guest-" + uuid.uuid4().hex[:5])
    relay.connect_ws(nick, sock)
    relay.join(CHANNEL, nick)
    await sock.send_json({"type": "history", "channel": CHANNEL,
                          "messages": relay.history(CHANNEL, limit=50)})
    try:
        while True:
            data = await sock.receive_json()             # {type:"message", channel, content}
            data.setdefault("channel", CHANNEL)
            await relay.handle_ws_message(nick, data)
    except WebSocketDisconnect:
        relay.disconnect_ws(nick); relay.part(CHANNEL, nick)
```

A browser connects, sends `{"nick":"alice","channel":"#team"}`, then
`{"type":"message","channel":"#team","content":"@researcher find X"}`. AitherRelay routes
the @mention to the researcher's handler, runs the turn, and broadcasts the reply to
**everyone** in the channel.

> Don't want to write the endpoint at all? `adk.server.create_app(...)` already mounts a
> `/ws/chat` wired to a relay (see §7).

---

## 1. The ChatRelay API

Get the process-wide relay (creates a SQLite-backed history store):

```python
from adk.chat import get_chat_relay
relay = get_chat_relay(node_id="my-node")
relay.create_channel("#team")          # channels are created lazily by join()/post() too
```

| Method | What it does |
|---|---|
| `register_agent(nick, channels=[...], mention_handler=fn)` | Joins the agent to channels (as `is_agent=True`) and registers its `@mention` handler. |
| `post(channel, nick, content)` | Store a message, **broadcast to all WS clients in the channel**, run `@mention` dispatch, emit a `"message"` event. Returns a `ChatMessage`. |
| `join(channel, nick, is_agent=False)` / `part(channel, nick)` | Presence in/out (also stored as `join`/`part` messages). |
| `who(channel)` / `online_users()` | Current participants. |
| `history(channel, limit=50, before=0)` | Recent messages (list of dicts), oldest→newest. |
| `connect_ws(nick, ws)` / `disconnect_ws(nick)` | Bind/unbind a client's WebSocket for fan-out. |
| `handle_ws_message(nick, data)` | Dispatch a client frame (`message`/`join`/`part`/`dm`/`action`/`who`/`list`). |
| `post_dm(from, to, content)` / `post_action(channel, nick, action)` | Direct messages / IRC-style `/me`. |

**Mention dispatch (`_check_mentions`):** on every `post()`, the relay scans the content
(lowercased) for `@<nick>` of any **registered** agent and calls its `mention_handler(msg)`.
If the handler returns a coroutine it's scheduled with `asyncio.ensure_future`. Mention
several agents in one message → several handlers fire **concurrently**.

A `ChatMessage` carries `msg_id, channel, nick, content, msg_type, timestamp, thread_id,
node_id` and serializes via `.to_dict()`. `msg_type` is `message` | `join` | `part` |
`action`.

---

## 2. The WebSocket protocol

**Client → server** (via `handle_ws_message`):

| Frame | Meaning |
|---|---|
| *(first frame)* `{ "nick": "...", "channel": "#team" }` | join — you bind it with `connect_ws` + `join` (see §0) |
| `{ "type": "message", "channel": "#team", "content": "..." }` | post a message |
| `{ "type": "join" / "part", "channel": "#team" }` | join/leave a channel |
| `{ "type": "who", "channel": "#team" }` | request participant list |
| `{ "type": "dm", "to": "nick", "content": "..." }` | direct message |

**Server → client:**

| Frame | Meaning |
|---|---|
| `{ "type": "history", "channel": "...", "messages": [ChatMessage…] }` | sent on connect |
| a `ChatMessage.to_dict()` (`nick`, `content`, `msg_type`, `timestamp`, …) | a posted message, fanned out to the channel |
| `{ "type": "who_reply", "users": [...] }` | response to `who` |

> **Tip — typing / custom events:** the relay only broadcasts stored messages. To push a
> transient event (e.g. "agent is typing…") to every client without storing it, call
> `await relay._broadcast_ws(channel, {"type": "typing", "nick": agent, "on": True})`.

---

## 3. Mention handlers — the bridge to your agents

The handler is where the framework hands you a human message and you run a real agent turn.

```python
def make_handler(agent):
    async def handle(msg):                       # msg: ChatMessage (msg.nick = the human)
        await relay._broadcast_ws(CHANNEL, {"type": "typing", "nick": agent.name, "on": True})
        try:
            resp = await agent.chat(msg.content, session_id=f"room:{msg.nick}")
            footer = f"\n\n〔{len(resp.tool_calls_made or [])} tools · ${resp.cost_usd:.4f}〕"
            relay.post(CHANNEL, agent.name, resp.content + footer)   # answer + transparency
        finally:
            await relay._broadcast_ws(CHANNEL, {"type": "typing", "nick": agent.name, "on": False})
    return lambda msg: handle(msg)
```

Notes:
- Use `msg.nick` as the per-human `session_id`/`user_id` so each person gets their own
  memory thread, even in a shared room.
- Strip the `@mentions` from `msg.content` before passing to the agent if you want it to see
  a natural message.
- The handler can call a **remote** agent over HTTP just as easily (run your agents as a
  fleet behind `serve.py`/`create_app` and have the handler `POST /agents/{name}/chat`) —
  keeps grounding/tools/RLS/audit in the agent process and the room as a thin layer.

---

## 4. Streaming token-by-token

For a live "typing as it generates" feel, use `AitherAgent.stream_chat(on_event=…)` and
broadcast deltas as a custom event:

```python
async def handle(msg):
    async def on_event(ev):
        if ev["type"] == "token":
            await relay._broadcast_ws(CHANNEL, {"type": "delta", "nick": agent.name, "text": ev["text"]})
    resp = await agent.stream_chat(msg.content, on_event=on_event, session_id=f"room:{msg.nick}")
    relay.post(CHANNEL, agent.name, resp.content)     # final, stored message
```

`on_event` receives `token`, `tool`, `tool_result`, and `done` events (sync or async
callable). `stream_react(...)` additionally emits `thinking` events for live reasoning.

---

## 5. Multiple humans, presence, history

Because the relay fans out to **every** connected client in a channel, multiple people
share one room out of the box. `who(channel)` / `online_users()` drive a presence sidebar;
`history(channel)` replays the last N messages to anyone who joins late (persisted in
SQLite, so it survives restarts).

---

## 6. Agent-to-agent (A2A)

When you want agents to talk to *each other* (not just answer humans), use `adk.a2a`:

```python
from adk.a2a import A2AServer
A2AServer(agent=my_agent).mount(app)    # /.well-known/agent.json + JSON-RPC + SSE task stream
```

Or, in a fleet, each agent gets an `ask_agent(name, message)` delegation tool
(`adk.fleet.load_fleet`). A mention handler that calls `ask_agent` lets an agent pull a peer
into the conversation.

---

## 7. Or skip the endpoint: `create_app`

`adk.server.create_app(agent=..., fleet_agents=[...])` already builds a FastAPI app with a
`/ws/chat` WebSocket wired to `get_chat_relay()`, plus `/chat`, `/chat/stream` (SSE), and
OpenAI-compatible routes. Start it with `aither-serve --identity <name>` or
`aither-serve --fleet fleet.yaml`. Use this when you want the batteries-included server;
roll your own `/ws/chat` (as in §0) when you want full control over routing and the UI.

---

## 8. Gotchas checklist

- **Register before you serve.** Call `register_agent(...)` for every agent at startup, *then*
  accept WebSocket connections — handlers must exist when the first `@mention` arrives.
- **Handlers return coroutines.** `_check_mentions` schedules a returned coroutine; a plain
  `async def handle(msg)` registered directly works, or wrap it as `lambda msg: handle(msg)`.
- **`@mention` matches registered nicks only.** `@all`/`@everyone` is not built in — if you
  want broadcast-to-all, expand it yourself before `post()`.
- **Typing/streaming = transient broadcasts**, not `post()` (which stores + dispatches mentions).
  Use `relay._broadcast_ws(channel, {...})` for events you don't want in history.
- **One `session_id` per human**, even in a shared room, or everyone shares one memory thread.
- **Nicks are validated.** Sanitize human display names to `[A-Za-z0-9_-]` or `join()` rejects them.
- **Don't @mention from inside a stored agent reply** unless you intend to trigger another
  agent — `post()` runs mention dispatch on agent messages too.

---

## See also
- `docs/AGENT_DEV_GUIDE.md` — building the agents themselves (BYO inference, packs, memory, tools).
- `adk/chat.py` — `ChatRelay` source. `adk/server.py` — `create_app` + `/ws/chat`.
- `adk/a2a.py` — agent-to-agent protocol. `adk/agent.py` — `chat` / `stream_chat` / `stream_react`.
