---
name: openclaw
description: Install OpenClaw (the local-first personal AI assistant) and wire it to your own hardware — point it at a local model instead of a paid API, connect the AitherOS toolset over MCP with one command, and install this skill pack into its workspace. Covers the automated `adk integrate openclaw` path and the manual config for when it isn't available.
---

# openclaw — a personal assistant that runs on your machine

[OpenClaw](https://github.com/openclaw/openclaw) is a local-first personal AI assistant: it
runs on your own devices, reaches you over 25+ messaging channels, and supports the Agent
Skills standard — so everything in this pack works inside it.

## Install

```bash
curl -fsSL https://openclaw.ai/install.sh | bash      # macOS / Linux
iwr -useb https://openclaw.ai/install.ps1 | iex       # Windows PowerShell
npm install -g openclaw@latest                        # or via npm
```

Then:

```bash
openclaw onboard --install-daemon
```

**Check:** `openclaw --version` prints a version, and `~/.openclaw/` exists.

## Point it at your own model

By default OpenClaw talks to a hosted provider, which costs money per token. Point it at the
local endpoint you stood up in [`local-inference`](local-inference.md) and it costs nothing.

Edit `~/.openclaw/openclaw.json`:

```json
{
  "agents": {
    "defaults": {
      "model": "custom/qwen3:8b",
      "providers": {
        "custom": {
          "baseUrl": "http://localhost:11434/v1",
          "apiKey": "not-needed-for-local"
        }
      }
    }
  }
}
```

- `baseUrl` — `http://localhost:11434/v1` for Ollama, `:8080/v1` for llama.cpp,
  `:8000/v1` for vLLM.
- `apiKey` — local servers ignore it, but the field usually must be **present and non-empty**.
  A missing key is a more common failure than a wrong one.

**Check — ask OpenClaw anything, and watch the model load on your side:**

```bash
ollama ps          # the model should be listed as loaded while it answers
```

If OpenClaw answers but `ollama ps` stays empty, it is still using the hosted provider — the
config didn't take. Confirm the file parses (`python -c "import json;json.load(open('$HOME/.openclaw/openclaw.json'))"`)
and that you edited the file OpenClaw actually reads.

## Connect the AitherOS toolset (one command)

This gives OpenClaw the platform tools — agent fleet, memory, files, GPU — over MCP:

```bash
adk integrate openclaw
```

It detects `~/.openclaw/`, writes the MCP server config, and reports what it changed. Options:

```bash
adk integrate openclaw --dry-run              # show the config, write nothing
adk integrate openclaw --mode local           # local | cloud | hybrid | auto (default: auto)
adk integrate openclaw --api-key <key>        # for cloud mode
adk integrate openclaw --force                # overwrite an existing integration
aither integrate list                            # what else can be integrated
```

**Always run `--dry-run` first.** It prints the exact config it would write, which is also the
config you'd write by hand if the command isn't available to you.

`aither` ships with the toolkit — if the command isn't found, install it first:

```bash
pip install awdk
```

**Check:** ask OpenClaw *"what MCP tools do you have?"* — it should name AitherOS tools, not
just its built-ins. If the integration wrote config but no tools appear, restart the OpenClaw
daemon; MCP servers are connected at startup.

## Install this skill pack into OpenClaw

OpenClaw reads Agent Skills from `~/.openclaw/workspace/skills/<name>/SKILL.md`:

```bash
bash scripts/install-awskills.sh --target openclaw
```

Or by hand, per skill:

```bash
mkdir -p ~/.openclaw/workspace/skills/local-inference
cp skills/local-inference.md ~/.openclaw/workspace/skills/local-inference/SKILL.md
```

See [`install-skills`](install-skills.md) for the full layout rules.

## Run OpenClaw as an agent pack under the toolkit

The toolkit bundles OpenClaw as an agent pack, so you can run it inside the AitherOS agent
loop instead of standalone:

```bash
adk install pack:openclaw
adk run --agents openclaw
adk pack customize openclaw --system-prompt "You are my focused research assistant."
```

This is a different thing from `adk integrate openclaw`, and the distinction matters:

| | What it does |
|---|---|
| `adk integrate openclaw` | your **standalone OpenClaw** gains AitherOS tools |
| `adk run --agents openclaw` | the **toolkit** runs an OpenClaw-flavored agent |

Use the first if OpenClaw is your daily driver. Use the second if `adk` is.

## Workspace files OpenClaw injects

`~/.openclaw/workspace/` holds prompt files that are injected into every conversation:

| File | Purpose |
|---|---|
| `AGENTS.md` | project/agent context |
| `SOUL.md | identity and voice |
| `TOOLS.md` | tool usage guidance |

Keep them short. They cost context on **every** turn, unlike skills — which load only when
relevant. Anything procedural belongs in a skill, not in `TOOLS.md`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Still billing a hosted provider | config didn't take | validate the JSON; confirm `ollama ps` shows a load |
| `aither: command not found` | toolkit not installed | `pip install awdk` |
| Integration ran, no new tools | daemon not restarted | restart the OpenClaw daemon |
| Skills not visible | wrong layout | must be `<name>/SKILL.md`, not a flat `.md` |
| Model answers slowly | CPU inference | expected — see [`local-inference`](local-inference.md) |

## Next

- **[`hermes-agent`](hermes-agent.md)** — the same wiring for Nous Research's Hermes
- **[`awnode`](aithernode.md)** — expose this machine's GPU/files to OpenClaw over MCP
- **[`ship-an-app-free`](ship-an-app-free.md)** — have it build and deploy something
