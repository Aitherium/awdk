# A Flue agent on Aitherium

[Flue](https://github.com/withastro/flue) (Apache-2.0, by the Astro team) is an
agent harness: an agent is a function in a file. This project is a Flue agent
whose model and tools come from the Aitherium platform:

| Piece | Where it comes from |
| --- | --- |
| Harness, sessions, `flue run` | Flue, as the published npm packages `@flue/runtime` and `@flue/cli` |
| Model | The platform's OpenAI-compatible endpoint (`https://127.0.0.1:8150/v1`, local) |
| Tools | The platform's MCP gateway (`http://localhost:8182/mcp`), with your own sign-in |

No Flue source is copied here. `src/aitherium.ts` holds the wiring; `src/agents/aither.ts` is the agent.

## Run it

Needs Node 22.19 or newer, and a sign-in (`adk login`).

```
npm install
npx flue run src/agents/aither.ts -m "What time is it on the platform right now? Use your tool and quote the utc value."
```

A working run prints a `tool mcp__aitherium__time_now` line and then the answer.
Continue the same conversation from a new process with the id it printed:

```
npx flue run src/agents/aither.ts --id <id> -m "What was the exact value you quoted?"
```

## Change it

Copy `.env.example` to `.env`. Everything is optional.

- `AITHER_MODEL` picks another model the endpoint serves (`GET <base>/models`).
- `AITHER_MCP_TOOLS` names the gateway tools to mount, comma separated. The
  gateway lists about 1,900 tools; a local model's context does not hold that
  many schemas, so the agent mounts only the ones named. A name the gateway
  does not expose fails the run with an error.
- `AITHER_MCP_URL=https://mcp.aitherium.com/mcp` uses the hosted gateway.

## Things that will stop you

- **TLS.** The local model endpoint is HTTPS with the platform's own CA. Node
  must trust it: `adk mcp setup` sets `NODE_EXTRA_CA_CERTS` for you. If a run
  ends in a certificate error, set `NODE_EXTRA_CA_CERTS` to the platform CA
  bundle and try again.
- **No sign-in.** With no `AITHER_MCP_TOKEN` and no `~/.aither/session-bearer`
  the run stops with "No Aitherium sign-in found". Run `adk login`.
- **Pinned versions.** `@flue/runtime` 2.3.0 asks for `@earendil-works/pi-ai`
  `^1.1.0`, and pi-ai 1.5.2 no longer has an export the runtime imports
  (measured 2026-11-12: `npx @flue/cli@2.3.0 init` fails with "does not provide
  an export named 'Type'"). `package.json` pins pi-ai and pi-agent-core to
  1.1.0, the versions in Flue's own lockfile. Drop the pins when upstream
  ranges are fixed.

## What is not here

Deploying this agent behind an HTTP server, channels, and sandboxes are Flue
features and work the way Flue documents them (`npx flue docs`). The platform
does not host a Flue agent for you yet.
