# AitherADK Agent Setup Prompt
# Copy this entire block into Claude Code, Cursor, Copilot, or any AI coding agent.
# Version: 1.5.0 | Updated: 2026-05-23

You are setting up the AitherADK agent development kit. Follow these steps exactly.
Do NOT deviate from these instructions -- they encode solutions to real failure modes.

## Critical Facts

- The CLI command is `adk`, NOT `aither`. The `aither` command is a separate REPL.
- The pip package is `awdk`. Install: `pip install awdk`
- Auto-detection works. `LLMRouter()` with no args finds vLLM, Ollama, or cloud backends.
- All config lives in `~/.aither/` -- never set env vars manually unless told to.

## Step 1: Install

```bash
pip install "awdk"
adk --help
```

Verify `adk --help` shows subcommands: init, setup, login, connect, run, status, whoami.
If `adk` is not found, check `pip show awdk` and ensure your PATH includes pip scripts.

## Step 2: Check existing infrastructure

```bash
adk status
```

Read the output carefully:
- **vLLM UP on :8200** -> You already have inference. DO NOT run `adk setup`. Go to Step 4.
- **Ollama UP** -> You have inference. Go to Step 4.
- **Genesis UP on :8001** -> Full AitherOS is running. Go to Step 4.
- **Everything DOWN** -> You need inference. Go to Step 3.

## Step 3: Set up inference (ONLY if nothing is running)

```bash
adk setup
```

This is an interactive wizard. It auto-detects your GPU and recommends a tier.

- NVIDIA GPU with 8GB+ VRAM -> vLLM containers (parallel inference, recommended)
- AMD/Apple/No GPU -> Ollama (serialized, but works everywhere)
- First run downloads 16-30GB of model weights. Takes 10-15 minutes. Be patient.
- For CI/automation: `adk setup --non-interactive`
- For dry run: `adk setup --dry-run`

**WARNING: NEVER run `adk setup` if vLLM is already running on your GPU.**
It will start a competing container that fights for VRAM and crash with OOM.
v1.1.2+ detects this and offers "connect to existing" automatically.

After setup completes, verify: `adk status` should show vLLM or Ollama as UP.

## Step 4: Authenticate

Check current auth status:
```bash
adk whoami
```

If not logged in, choose one:
```bash
adk login                        # Opens browser -- approve on portal (recommended)
adk login --email you@example.com  # Email/password flow
adk login --api-key aither_pat_... # Direct API key (from portal)
```

To generate a key manually: api.aitherium.com → Settings → API Keys → "+ New Token".

Auth is optional for local-only usage. Required for cloud inference, sovereign deploy, and fleet sync.

## Step 5: Create an agent project

```bash
adk init my-agent
cd my-agent
```

This creates:
- `agent.py` -- Agent definition with a sample tool
- `config.yaml` -- Backend, model, port configuration
- `tools.py` -- Custom tool definitions

## Step 6: Test basic chat

```python
import asyncio
from adk import AitherAgent

async def main():
    agent = AitherAgent("my-agent")
    response = await agent.chat("Hello! What can you help me with?")
    print(response.content)

asyncio.run(main())
```

The LLMRouter auto-detects backends in this priority order:
1. vLLM on port 8200 (highest priority -- continuous batching)
2. Desktop AitherOS MicroScheduler
3. Ollama on port 11434
4. Aitherium Gateway (cloud, needs auth)
5. OpenAI/Anthropic API keys (if set)

## Step 7: Add custom tools

```python
import asyncio
from adk import AitherAgent

agent = AitherAgent("my-agent")

@agent.tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Weather in {city}: 72F, sunny"

@agent.tool
def search_docs(query: str) -> str:
    """Search project documentation."""
    return f"Found 3 results for: {query}"

async def main():
    response = await agent.chat("What's the weather in Tokyo?")
    print(response.content)

asyncio.run(main())
```

The agent has 7 built-in tools enabled by default:
`file_read`, `file_write`, `file_edit`, `file_list`, `file_search`, `web_search`, `web_fetch`

## Step 8: Run the API server

```bash
adk run                                    # Single agent, reads config.yaml
adk run --identity lyra --port 9000        # Specific identity + port
adk run --agents lyra,atlas,demiurge       # Fleet mode (parallel agents)
```

This starts a FastAPI server with an OpenAI-compatible endpoint at `/v1/chat/completions`.

## Step 9: Fleet mode (multi-agent)

```python
import asyncio
from adk.fleet import FleetConfig

async def main():
    fleet = FleetConfig(agent_names=["aither", "lyra", "atlas"])
    # Each agent gets its own personality, tools, and conversation state
    # All share the same LLM backend via continuous batching

asyncio.run(main())
```

17 bundled identities: aither, apollo, athena, atlas, chaos, demiurge, hera,
hydra, iris, lyra, morgana, muse, prometheus, saga, themis, vera, viviane.

## Step 10: Graph memory (optional)

```python
# Store knowledge
await agent.graph_remember("user", "prefers", "dark mode")
await agent.graph_remember("project", "uses", "FastAPI")

# Query knowledge
results = await agent.graph_query("What does the user prefer?")
```

Graph memory is SQLite-backed, works offline, persisted at `~/.aither/graph/`.

## Step 11: Deploy sovereign node (optional)

Deploy a full AitherOS instance on your own hardware that federates with the hub.

```bash
# Full node with GPU, dashboard, and mesh + register with hub
adk deploy node --gpu --dashboard --mesh --sovereign

# Lighter: core services only + federation
adk deploy core --sovereign

# Specify tenant and hub URL
adk deploy node --sovereign --tenant my-org --hub https://api.aitherium.com

# Dry run (show what would happen)
adk deploy node --sovereign --dry-run
```

After deployment:
- Node auto-registers with `api.aitherium.com/federation/register`
- Federation credentials saved to `~/.aither/.env.federation`
- Node appears in your fleet dashboard at `/workspace/fleet`
- Heartbeats keep the hub updated with status and metrics

Check federation status: `adk connect`

---

## Common Mistakes (avoid these)

1. **Wrong CLI**: `aither` is NOT `adk`. Use `adk init`, `adk setup`, `adk run`.
2. **Double vLLM**: Never run `adk setup` when vLLM is already on your GPU.
   Always check `adk status` first.
3. **Wrong env var**: Don't set `AITHER_API_KEY`. Run `adk login` instead --
   it saves the token to `~/.aither/config.yaml` automatically.
4. **Wrong tool names**: Built-in tools are `file_read`, `file_write`,
   `file_edit` -- NOT `read_file`, `write_file`.
5. **Wrong memory API**: Use `agent.graph_remember()` and `agent.graph_query()`,
   NOT `GraphMemory.add_triple()`.
6. **Elysium no-key**: `Elysium()` without args works -- it reads from saved config.
   Don't pass `api_key=""` explicitly.
7. **IRC noise**: `adk run` starts an IRC bridge on port 6667. This is normal,
   not an error.

## Quick Reference

| I want to...              | Command / Code                                    |
|---------------------------|---------------------------------------------------|
| Install                   | `pip install awdk`                          |
| Check system              | `adk status`                                      |
| Set up GPU inference      | `adk setup`                                       |
| Login                     | `adk login`                                       |
| Check auth                | `adk whoami`                                      |
| Create project            | `adk init my-agent`                               |
| Run agent                 | `adk run`                                         |
| Run fleet                 | `adk run --agents lyra,atlas`                     |
| Start API server          | `adk run --port 8080`                             |
| Diagnose issues           | `adk doctor`                                      |
| Connect to cloud          | `adk connect --api-key KEY`                       |
| Deploy sovereign node     | `adk deploy node --sovereign`                     |
| Register with hub         | `adk register`                                    |
