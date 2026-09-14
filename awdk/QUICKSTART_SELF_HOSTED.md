# Self-Hosted Agent Quickstart

Run your own AI agent **on your machine** — your model, your loop, your data — and manage it
from `api.aitherium.com`. Aitherium hosts only the control plane; inference, the agent loop,
memory, and your data never leave your box.

## TL;DR

```bash
pip install awdk          # 1. install the toolkit (no other hoops)
adk onboard --quick             # 2. one command: inference + a pack + enroll
adk run --agents openclaw       # 3. run your agent locally
```

Then open **api.aitherium.com → Workstation** to see your node and connect your own tools.

## What each step does

### 1. Install
`pip install awdk` — that's the whole toolkit (agent runtime, shell, inference setup,
enrollment). Already on PyPI.

### 2. Stand up inference — local LLM **or** a cloud key
`adk onboard --quick` runs `adk quickstart-local`: it detects your hardware, picks a backend
(**Ollama**, **llama.cpp**, or **vLLM**), pulls a model, serves it, and verifies it. Prefer a
hosted model instead? Skip local inference and set a key:

```bash
adk keys set anthropic sk-ant-...        # or openai / deepseek / openrouter / groq / together / google
```

### 3. Pick & customize one agent pack
Bundled packs: **openclaw** (web research), **hermes** (research), **claude-code** (coding).

```bash
adk install pack:openclaw
adk pack customize openclaw --system-prompt "You are my focused research assistant."
# or:  adk pack customize openclaw --system-prompt-file ./my-prompt.txt --capabilities code,memory
```

Customization is written to an overlay (`~/.aither/agents/openclaw/agent.yaml.local`) — your
edits survive pack updates and never touch the shipped pack.

### 4. Log in & enroll your machine
`adk login` uses **device-flow auth** (you approve in the browser — no password typed into the
CLI). Then enroll this box so it shows up in your portal:

```bash
adk login          # device flow against AitherIdentity (idp.aitherium.com)
adk enroll         # registers this workstation (hardware + models) to your account
```

Enrollment presents your login token to **AitherIdentity**, which records your machine as a
device in **AitherDirectory**, scoped to your tenant. A lightweight heartbeat keeps the
"last seen" fresh. Genesis and the chat brain are never in this path.

### 5. Manage it from the portal
**api.aitherium.com → Workstation** shows your enrolled node(s): GPU, VRAM, available models,
and liveness. From there you can also **connect your own MCP tool servers** (the bearer token is
held in the vault and never shown again), so your local agent can use your tools.

Stand up your workstation as an MCP server in one command:

```bash
adk mcp node --port 8182      # serves your local tools; optionally registers them
```

### 6. Run
```bash
adk run --agents openclaw            # start the agent loop locally
adk start                            # or: zero-config chat in the current project
```

## How it fits together

```
your machine                                api.aitherium.com (control plane only)
┌────────────────────────────┐              ┌───────────────────────────────────────┐
│ awdk                  │  device-flow │ AitherIdentity (idp) — verifies you     │
│  • local LLM / BYO key      │ ───login───▶ │ AitherDirectory      — your node entry  │
│  • agent loop (your data)   │ ──enroll───▶ │ Portal → Workstation — see & manage it  │
│  • your MCP tool servers    │ ◀─heartbeat─ │                                         │
└────────────────────────────┘              └───────────────────────────────────────┘
```

You run everything that matters locally. The portal is just where you sign in, see your node,
set a few keys, and connect tools.

## Troubleshooting

- **`adk enroll` says nothing appears in the portal** — confirm `adk login` succeeded
  (`adk whoami`) and that this machine can reach `idp.aitherium.com`. Enrollment is best-effort and
  never blocks `adk run`, so a failed enroll won't stop your agent.
- **No local model** — re-run `adk quickstart-local`, or set a cloud key with `adk keys set`.
- **Point enrollment at a different control plane** — set `AITHER_ENROLL_BASE` (or
  `AITHERIDENTITY_URL`) before `adk enroll`.
