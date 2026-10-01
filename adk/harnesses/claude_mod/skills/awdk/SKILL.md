---
name: awdk
description: >-
  Run your own AI agent, on your machine, in three commands. [awdk](https://github.com/Aitherium/awdk) is the agent toolkit: an agent runtime, a local shell, inference setup, and control-plane enrollment in one package. You run the model, the agent loop, the memory, and your data on your own box — Aitherium hosts only the control plane, and you manage everything from api.aitherium.com. Nothing about your inference or data leaves your machine.
---

# awdk — run your own AI agent, on your machine, in three commands

[awdk](https://github.com/Aitherium/awdk) is the agent toolkit: an agent runtime, a
local shell, inference setup, and control-plane enrollment in one package. You run the model, the
agent loop, the memory, and your data **on your own box** — Aitherium hosts only the control plane,
and you manage everything from `api.aitherium.com`. Nothing about your inference or data leaves
your machine.

## Set it up

```bash
pip install awdk          # the whole toolkit — already on PyPI
adk onboard --quick             # detect hardware, stand up inference, install a pack, enroll
adk run --agents openclaw       # run an agent locally
```

`curl -fsSL https://aitherium.com/install.sh | sh` bootstraps the toolkit without a Python of your own.
After install, add an agent pack:

```bash
adk install pack:openclaw       # or hermes / claude-code
```

Verify it:

```bash
adk doctor                      # confirms toolkit, packs, and inference are ready
```

`adk onboard --quick` runs `adk quickstart-local`: it detects your CPU/RAM/GPU, picks a backend
(**Ollama**, **llama.cpp**, or **vLLM**), pulls and serves a model, and verifies it. Prefer a hosted
model? Skip local inference and set a key instead:

```bash
adk keys set anthropic <your-api-key>   # or openai / deepseek / openrouter / groq / together / google
```

## Use it

```bash
adk up                          # one command: stand up a persistent agent (hosted-brain default)
adk run --agents openclaw       # run a specific bundled pack (openclaw / hermes / claude-code)
adk chat                        # talk to your agent from the terminal
adk install pack:openclaw       # add an agent pack
adk pack customize openclaw --system-prompt "You are my focused research assistant."
adk doctor                      # check inference, packs, enrollment, and mesh health
```

Pack customization is written to an overlay (`~/.aither/agents/<pack>/agent.yaml.local`) — your
edits survive pack updates and never touch the shipped pack.

## Aither Hearth: your agent, reachable from your phone

`adk home` (also installed as `aither-hearth`) runs one personal agent that answers only you,
on the chat apps you already use:

```bash
adk home init --name pip                # your agent's home folder
adk home model --byo anthropic          # or --local ollama | llamacpp | bonsai
adk home signin                         # Sign in with Aitherium
export HEARTH_TELEGRAM_TOKEN=...        # a Telegram bot token from @BotFather
adk home serve --channels telegram --pair  # prints a 6-digit code: DM it to the bot
adk home channels                       # relay, telegram, discord, slack, email, whatsapp, sms, local
```

Channel credentials come from the environment (`HEARTH_TELEGRAM_TOKEN`, `HEARTH_SLACK_BOT_TOKEN`,
...), never from a flag. The agent messages you first when a reminder is due, and anything that
sends, books or adds (email, calendar event, to-do, a recurring follow-up) waits for your
`yes <code>`. `adk home receipts --verify` checks the signed log of what it did (exit 0 intact,
1 tampered, 2 cannot judge). A workspace admin can connect Google calendar and mail at
`api.aitherium.com/admin?tab=connections` (admin-only) after `adk home signin`; Microsoft 365 is
not available yet. On the same machine,
`adk home say "…"`, `adk home events` and `/hearth` in `adk-shell` talk to the running serve.
The full guide is `docs/agent-home.md` in the awdk repository.

## Log in & enroll (optional, for the portal + fleet)

```bash
adk login          # device-flow auth — you approve in the browser, no password typed into the CLI
adk enroll         # register this workstation (hardware + models) to your account
```

Then open **aitherium.com → Workstation** to see your node and wire in your own tools. BYO /
self-hosted nodes are uncapped by default — caps only apply on the metered hosted gateway.

## Part of one substrate

awdk is the runtime the rest plug into: stand up hardware as an [awnode](aithernode.md),
wire it to the control plane with [Awconnect](awconnect.md), provision the box with
[AitherZero](aitherzero.md), and pool compute across machines with
[OmniNode](omninode-node.md) over AitherMesh. Standing up compute and having your agents use it
should be one motion, not five projects.

MIT-licensed, like everything in `awskills`.
