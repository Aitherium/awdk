---
name: aither-powered-coding
description: Set up any coding agent (Claude Code, Cursor, Aider, Cline) to use AitherOS as its brain — with automatic model switching, 1272 MCP tools, managed agent dispatch, and transparent failover between DeepSeek/Kimi/local models/Anthropic. Supplements awknowledge with the AitherOS-specific infrastructure. Use when someone says "connect my agent to aither", "use aither as my backend", or "set up adk with claude code".
---

# aither-powered-coding — make any coding agent use AitherOS as its brain

Your coding agent (Claude Code, Cursor, Aider) becomes 10x more capable when backed by
AitherOS: 1272 MCP tools, managed agent fleet, automatic model routing, transparent
failover, and your own local GPU inference.

This skill supplements [awknowledge](awknowledge.md) with AitherOS-specific setup.

---

## Quick start (60 seconds)

```bash
# 1. Install awdk
pip install awdk   # or: pip install -e ./awdk (from repo)

# 2. Store your API key
adk keys set deepseek    # paste your DeepSeek API key (stored locally only)

# 3. Switch Claude Code to DeepSeek (native 1M context, no bridge)
adk claude-model use deepseek-flash

# 4. Restart Claude Code (exit + re-run `claude`)

# 5. Run /mcp in Claude Code to connect to AitherOS MCP gateway (1272 tools)
```

Done. You now have Claude Code running on DeepSeek V4 Flash with 1M context and
full AitherOS tool access.

---

## The three layers

### Layer 1: Model switching (`adk claude-model`)

Switch between providers without editing config files:

```bash
adk claude-model list              # see all profiles
adk claude-model use deepseek-flash   # DeepSeek Flash, 1M, native
adk claude-model use deepseek-pro     # DeepSeek Pro, 1M, reasoning
adk claude-model use kimi-k3          # Kimi K3, 1M, native
adk claude-model use aither-best      # local qwen3.6-27b (needs bridge)
adk claude-model use mixed            # multi-tier: /model switches Pro/Flash/local
adk claude-model use anthropic        # restore stock Claude
adk claude-model status               # what's active
adk claude-model check                # prove it works (real API call)
adk claude-model failover             # auto-switch to next working provider
```

**How providers connect:**

| Provider | Protocol | URL | Context |
|----------|----------|-----|---------|
| DeepSeek | Native Anthropic | `api.deepseek.com/anthropic` | 1M |
| Kimi/Moonshot | Native Anthropic | `api.moonshot.ai/anthropic` | 256K-1M |
| Anthropic | Native | `api.anthropic.com` | 200K |
| Local models | Bridge → the router | `127.0.0.1:8151` | varies |
| OpenRouter | Bridge | `openrouter.ai/api/v1` | varies |

**Key discovery:** DeepSeek model names need the `[1m]` suffix for 1M context
(e.g., `deepseek-v4-flash[1m]`). Without it, Claude Code defaults to a small window.

### Layer 2: MCP tools (1272 tools via AitherOS gateway)

Once connected (`/mcp` in Claude Code, or `.mcp.json` config), you get:

| Category | Tools | What they do |
|----------|-------|-------------|
| Agent delegation | the ask / spawn tool pair | Send tasks to fleet agents |
| Code intelligence | `codegraph_query`, `repowise_search` | Semantic code search |
| Memory | `recall`, `query_memory` | Knowledge graph search |
| LLM | `llm_chat`, `llm_generate` | Direct model access |
| Fleet | `get_service_status`, `get_fleet_health` | Infrastructure status |
| Files | `fs_read_file`, `fs_write_file` | File ops (container-side) |
| Forge | `forge_parallel`, `forge_worktree_commit` | Multi-agent coding |

**MCP config (`.mcp.json` at repo root):**

```json
{
  "mcpServers": {
    "aitheros": {
      "type": "streamable-http",
      "url": "http://localhost:8182/mcp",
      "headers": {
        "Authorization": "Bearer <your-token>"
      }
    }
  }
}
```

For VS Code Copilot, same config goes in `.vscode/mcp.json`. Use `localhost`
not `127.0.0.1` (Node.js fetch on Windows has issues with the IP).

**Remote access via tunnel:**

```json
{
  "aitheros-cloud": {
    "type": "streamable-http",
    "url": "https://mcp.aitherium.com/mcp",
    "headers": { "Authorization": "Bearer <token>" }
  }
}
```

### Layer 3: Managed agents (AitherOS fleet)

Your coding agent can delegate to specialized AitherOS agents:

Your gateway exposes two delegation tools — one that *asks* a named specialist a
question and returns its answer, and one that *spawns* a specialist to go and do
a piece of work. Point them at whichever agent owns the domain:

```
# ask a specialist a question
  agent="demiurge"  question="refactor X to use pattern Y"
  agent="atlas"     question="what services touch the auth flow?"
  agent="athena"    question="security review this diff"
  agent="lyra"      question="what's the test coverage gap?"

# hand a specialist a whole task
  agent="demiurge"  task="implement feature X"
```

Run your gateway's tool listing to get the exact tool names it publishes — they
differ between a self-hosted gateway and a managed one.

**Available agents:** Demiurge (code), Atlas (architecture), Lyra (quality),
Athena (security), Iris (visual), Saga (creative), Apollo (performance),
Prometheus (infrastructure), Viviane (memory), Scribe (docs) + 20 more.

---

## Connecting other coding agents to AitherOS

AitherOS exposes OpenAI-compatible endpoints that any agent can use:

### The model router (port 8150) — multi-model routing

```bash
# For Aider, Cursor, Cline, Roo Code, Continue.dev:
export OPENAI_BASE_URL=https://127.0.0.1:8150/v1
export OPENAI_API_KEY=<internal-secret>
```

### External Gateway (gateway.aitherium.com) — public API

```bash
# For remote agents:
export OPENAI_BASE_URL=https://gateway.aitherium.com/v1
export OPENAI_API_KEY=<acta-gateway-key>
```

### AitherClaudeBridge (port 8151) — Anthropic Messages API

```bash
# For Claude Code pointed at local models:
export ANTHROPIC_BASE_URL=http://127.0.0.1:8151
```

| Agent | Protocol | Connection |
|-------|----------|------------|
| Claude Code | Anthropic | `ANTHROPIC_BASE_URL` → bridge or DeepSeek native |
| Cursor | OpenAI | Settings → OpenAI Compatible → the router's base URL |
| Aider | OpenAI | `--openai-api-base` → the router |
| Cline | OpenAI | Provider → Custom → base URL |
| Codex | OpenAI | `OPENAI_BASE_URL` → the router |
| Hermes | OpenAI | `--base-url` → the router |

---

## AitherShell as your terminal

AitherShell (`/terminal` route) provides a tab-based coding interface with:
- Per-tab model selection (DeepSeek/local/auto)
- 1272 MCP tools
- Transparent failover (LLMRouter handles switching)
- No restart needed to change models

```bash
# Start AitherShell from its own checkout
npm run dev
# Open http://localhost:<port>/terminal
```

---

## adk start — zero-config coding agent

```bash
adk start                          # auto-detect best available model
adk start --model deepseek-flash   # explicit DeepSeek
adk start --provider openrouter    # via OpenRouter
adk start --model ollama           # local Ollama
adk start --mcp                    # connect AitherOS MCP tools
```

`adk start` provides: chat, file read/write, code search, persistent memory,
and auto-detection of the best available backend.

---

## Automatic failover

The LLMRouter has a built-in failover chain. When the primary provider fails
(429, timeout, 5xx), it automatically tries the next:

```python
# In your agent code:
router = LLMRouter(provider="anthropic", api_key="sk-...")
router.set_failover_chain([
    ("deepseek", None, deepseek_key),    # try DeepSeek next
    ("moonshot", None, moonshot_key),     # then Kimi
    ("ollama", None, None),              # then local
])
```

For Claude Code, use the watchdog:
```bash
adk claude-model watch --daemon    # monitors for rate limits, auto-switches
```

---

## Relationship to awknowledge

[awknowledge](awknowledge.md) teaches HOW to use a coding agent effectively
(prompt shape, live-proof gates, plan documents, memory). This skill teaches WHERE
to point it — the infrastructure that makes it resilient:

| awknowledge | aither-powered-coding |
|----------------|----------------------|
| Prompt discipline | Model switching |
| Live-proof gates | MCP tool access |
| Plan documents | Agent delegation |
| Memory scaffolding | Failover chain |
| Compaction strategy | 1M context (no compaction) |

Install both: `awknowledge` for the operating doctrine, `aither-powered-coding`
for the infrastructure that backs it.
