# Skill: Create a first Aitherium agent that has called a platform tool

You are helping a person create their first agent with the Aitherium developer
kit (`awdk`, CLI `adk`). You are done when an agent is running on their machine,
answering over HTTP on a local model, and has made one real tool call through the
platform's MCP gateway with their own sign-in. Not before.

Every command below was run on 2026-11-12 on a machine with the platform running
(local model endpoint and local MCP gateway). The one command you must never run
yourself is `adk login`: it is interactive and it is the person's sign-in.

## Step 1: Find out what is already there

Run these and read the output before deciding anything.

```bash
adk --version
adk status
adk mcp status
```

- `adk` not found: run `pip install awdk`, then `adk --version` again.
- `adk status` must show one backend line marked `UP`. Note its URL. If nothing
  is `UP`, stop and tell the person: this prompt needs a model endpoint, and
  setting one up is a separate job (`adk agent-prompt` covers it).
- `adk mcp status` must show `[OK]` for `local`, with a tool count. `[OK]` means
  an authenticated tool listing succeeded, not that the port answered.
  - `[!!] ... no credential on this machine` or `AUTH REJECTED`: stop and ask
    the person to run `adk login` themselves, then run `adk mcp status` again.
  - `[--] local`: the local gateway is not running. Stop and say so.

## Step 2: Settle three things

Ask only for what the conversation has not already told you.

1. **Name.** Lowercase, digits and dashes. Default `my-agent`.
2. **Directory.** Look at the current directory first. `adk init` creates
   `./<name>`. If that folder exists and is not empty, pick another name; never
   overwrite a person's files.
3. **Model.** A model the `UP` backend serves. List them from the backend URL in
   `adk status`, for the local platform:

   ```bash
   curl -sk https://127.0.0.1:8150/v1/models
   ```

   Take one whose entry says it is available. Do not use the default if the
   listing marks it unavailable or parked. If the person named a model that is
   not in the list, ask before substituting another.

Write the contract down before you build:

- Name: `<name>`
- Directory: `<absolute path>` (does not exist yet, or is empty)
- Model: `<model id from the listing>`
- Port: `<a free port>` (default `8467`)

## Step 3: Build it

```bash
adk init <name>
cd <name>
adk mcp setup --bake-token
```

- `adk init` writes `agent.py`, `config.yaml` and `tools.py`. Leave them as they are.
- `adk mcp setup --bake-token` writes `.mcp.json` in this folder. It must print
  `Status: connected, authenticated (<n> tools)`. If it prints that the gateway
  rejected every credential, it writes nothing and you go back to Step 1.
- `.mcp.json` now holds the person's sign-in. Add `.mcp.json` to `.gitignore`
  in this folder before anything else. Never print the file, never commit it.

## Step 4: Run it and prove it

Start the server and leave it running (use a background process; it does not exit):

```bash
adk run -p <port> -m <model>
```

Then, from another shell:

```bash
curl -s http://127.0.0.1:<port>/health
curl -s http://127.0.0.1:<port>/chat -H "Content-Type: application/json" -d '{"message":"What time is it on the platform right now? Use the time_now tool and quote its utc value."}'
```

- `/health` must return `"status":"healthy"` and `"gateway_mcp_connected":true`.
- The `/chat` reply is JSON. It is a pass only if `tool_calls` contains an entry
  that starts with `mcp__aitheros__` and `response` quotes a UTC timestamp.
  A reply with an empty `tool_calls` is a model answering from memory: not a pass.
- A first attempt marked `[failed]` in `tool_calls` followed by a successful
  `mcp__aitheros__` call is normal with a small local model. It searches for
  the tool, then calls it.

## Step 5: Check your work against the contract

- The folder is the one in the contract and holds `agent.py`, `config.yaml`,
  `tools.py`, `.mcp.json`, `.gitignore`.
- `.gitignore` lists `.mcp.json`.
- `/chat` answered with the model in the contract (`model` in the reply).
- `tool_calls` contains an `mcp__aitheros__` entry.
- You invented no key, token or model name.

If one of these is false, fix it before you report.

Finish with five lines: the folder, the port, the model, the `tool_calls` list
from the passing reply, and the command to start the agent again.

## Rules that keep this honest

- Never run `adk login` and never ask for a password or token in chat. The
  person signs in themselves.
- Never claim a step passed without the output that shows it. If a step fails,
  report the exact line and stop; do not move on to the next step.
- Do not run `adk setup` to "fix" a missing backend. It starts model containers
  and can fight a running one for the GPU.
- The local model endpoint is HTTPS with the platform's own certificate. That
  is why the model listing uses `curl -k`. Do not disable TLS anywhere else.
- `adk run` listens on all interfaces. Stop it when the person is done with it.
- Want a TypeScript harness instead? `adk new flue` scaffolds a Flue agent wired
  to the same model endpoint and gateway.
