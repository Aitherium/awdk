---
name: agent-integrations
description: Integrate awdk and AitherOS with mainstream coding agent harnesses (Claude Code, Cursor, Aider, Cline, Codex, Hermes, Roo Code) — both as a CLIENT of cloud providers (OpenRouter, DeepSeek, Anthropic, Moonshot) and as a BACKEND that other agents can use. Two-way integration.
---

# agent-integrations — aither talks to everything, everything talks to aither

AitherOS integrates with coding agents in TWO directions:

1. **Aither AS CLIENT** — `adk claude-model use` switches Claude Code to any backend
2. **Aither AS BACKEND** — other agents point at AitherOS as their model provider

---

## Direction 1: Aither uses external providers

### Supported providers (native Anthropic protocol — no bridge needed)

| Provider | Base URL | Models | Context |
|----------|----------|--------|---------|
| **DeepSeek** | `https://api.deepseek.com/anthropic` | `deepseek-v4-pro[1m]`, `deepseek-v4-flash[1m]` | 1M |
| **Moonshot (Kimi)** | `https://api.moonshot.ai/anthropic` | `kimi-k3`, `kimi-k2.6`, `kimi-k2.7-code` | 256K-1M |
| **Anthropic** | `https://api.anthropic.com` | `fable`, `claude-sonnet-4-6`, etc. | 200K |

### Supported providers (OpenAI-compatible — needs bridge)

| Provider | Base URL | Notes |
|----------|----------|-------|
| **OpenRouter** | `https://openrouter.ai/api/v1` | 200+ models, auto-fallback |
| **Local vLLM** | `https://127.0.0.1:8150/v1` | Via the router |
| **Local Ollama** | `http://127.0.0.1:11434/v1` | Any GGUF model |
| **Together AI** | `https://api.together.xyz/v1` | Llama, Mixtral, etc. |
| **Fireworks** | `https://api.fireworks.ai/inference/v1` | Fast inference |
| **Groq** | `https://api.groq.com/openai/v1` | Ultra-fast, limited models |

### Setup

```bash
# Native providers (Claude Code talks directly):
adk claude-model use deepseek-flash    # DeepSeek V4 Flash, 1M context
adk claude-model use deepseek-pro      # DeepSeek V4 Pro, 1M, reasoning
adk claude-model use kimi-k3           # Kimi K3, 1M context
adk claude-model use anthropic         # Stock Claude

# Bridge providers (needs AitherClaudeBridge running):
adk claude-model use openrouter-auto   # OpenRouter, auto-routing
adk claude-model use aither-best       # Local qwen3.6-27b
adk claude-model use aither-routed     # router auto-routing
```

---

## Direction 2: Other agents use Aither as their backend

AitherOS exposes **three OpenAI-compatible endpoints** that any coding agent can use:

### The model router (port 8150) — multi-model routing

```bash
# Any agent that accepts OPENAI_BASE_URL:
export OPENAI_BASE_URL=https://127.0.0.1:8150/v1
export OPENAI_API_KEY=<your-internal-secret>
```

Works with: **Aider**, **Cursor**, **Cline**, **Roo Code**, **Kilo Code**, **Continue.dev**

### AitherClaudeBridge (port 8151) — Anthropic Messages API

```bash
# Any agent that accepts ANTHROPIC_BASE_URL:
export ANTHROPIC_BASE_URL=http://127.0.0.1:8151
export ANTHROPIC_API_KEY=<bridge-token>
```

Works with: **Claude Code**, **Claude Desktop**

### External Gateway (gateway.aitherium.com) — public API

```bash
# Remote access (through Cloudflare tunnel):
export OPENAI_BASE_URL=https://gateway.aitherium.com/v1
export OPENAI_API_KEY=<your-acta-gateway-key>
```

Works with: any agent, anywhere, with ACTA billing.

---

## Coding agent compatibility matrix

| Agent | Protocol | How to connect to Aither |
|-------|----------|--------------------------|
| **Claude Code** | Anthropic Messages | `ANTHROPIC_BASE_URL=http://127.0.0.1:8151` |
| **Cursor** | OpenAI | `OPENAI_BASE_URL=https://127.0.0.1:8150/v1` in settings |
| **Aider** | OpenAI | `--openai-api-base https://127.0.0.1:8150/v1` |
| **Cline** | OpenAI | Provider settings → Custom → base URL |
| **Roo Code** | OpenAI | Provider config → OpenAI Compatible |
| **Codex** | OpenAI | `OPENAI_BASE_URL=https://127.0.0.1:8150/v1` |
| **Hermes** | OpenAI | `--base-url https://127.0.0.1:8150/v1` |
| **Continue.dev** | OpenAI | `config.json` → `apiBase` field |
| **OpenCode** | OpenAI | `OPENAI_BASE_URL` env var |

---

## Adding a new provider to awdk

### Native (Anthropic protocol — best, no bridge)

If the provider speaks Anthropic Messages API natively:

```yaml
# In your claude_profiles.yaml:
my-provider:
  transport: native
  description: "My Provider (native Anthropic)"
  base_url: "https://api.myprovider.com/anthropic"
  auth_secret: MY_PROVIDER_API_KEY
  model: "my-model-name"
  subagent_model: "my-fast-model"
  haiku_model: "my-fast-model"
  context_window: 200000
  effort: high
```

### Bridge (OpenAI protocol — needs AitherClaudeBridge)

If the provider speaks OpenAI `/v1/chat/completions`:

```yaml
# In your claude_bridge.yaml, add backend:
my_provider:
  base_url: https://api.myprovider.com/v1
  api_key_secret: MY_PROVIDER_API_KEY

# In claude_bridge.yaml, add model alias:
model_aliases:
  my-model: my_provider/actual-model-name

# In claude_profiles.yaml, add profile:
my-provider:
  transport: bridge
  description: "My Provider via bridge"
  bridge_backend: my_provider
  model: "claude-opus-5"   # spoof for Claude Code context sizing
  context_window: 200000
  effort: high
```

---

## OpenRouter integration (200+ models)

OpenRouter provides a unified gateway with automatic fallback routing:

```bash
# Store your key
adk keys set openrouter

# Use via bridge
adk claude-model use openrouter-auto
adk claude-model bridge start  # if not already running
```

OpenRouter model slugs for coding:
- `anthropic/claude-sonnet-4` — Claude Sonnet 4
- `deepseek/deepseek-v4-flash` — DeepSeek Flash
- `google/gemini-2.5-pro` — Gemini Pro
- `~auto` — auto-route to cheapest capable model

---

## Making AitherShell/Genesis swap models seamlessly

AitherShell talks to Genesis, which routes through the model router. Model selection
is already built in via `model_assignments.yaml` and the effort/complexity classifier.

To manually override the model in AitherShell:
1. Open AitherShell → Settings → Model
2. Or use the `/model` command in chat
3. Or via API: `POST /chat` with `force_model: "deepseek-v4-flash"`

To change the DEFAULT routing:
```bash
# Edit config/model_assignments.yaml:
roles:
  orchestrator: deepseek-v4-flash    # fast daily driver
  reasoning: deepseek-v4-pro         # complex tasks
  coding: deepseek-v4-flash          # code generation
  vision: a local 12B model                 # multimodal
```

---

## The adk CLI as a universal model switch

```bash
adk claude-model list              # all profiles
adk claude-model use <profile>     # switch Claude Code
adk claude-model status            # what's active
adk claude-model check             # prove it works

adk routing preset balanced        # set router routing preset
adk routing set coding deepseek    # pin coding to deepseek
adk routing reset                  # back to auto

adk keys set <provider>            # store API keys
adk keys list                      # show configured providers
```
