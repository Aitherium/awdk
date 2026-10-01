---
name: claude-model-switcher
description: Switch Claude Code between DeepSeek (native 1M context), Kimi, local AitherOS models, and stock Anthropic — one command, no manual config. Use when you hit Claude rate limits, want cheaper/faster models, or need to keep coding on alternative backends.
---

# claude-model-switcher — keep coding when Claude hits limits

Switch Claude Code CLI to **DeepSeek V4 Flash** (1M context), **DeepSeek V4 Pro** (1M, reasoning),
**Kimi K3** (1M), **local open-weight models** (free), or back to **stock Anthropic** — one command.

```bash
adk claude-model use deepseek-flash   # 1M context, fast, native Anthropic API
adk claude-model use deepseek-pro     # 1M context, deep reasoning, native
adk claude-model use kimi-k3          # 1M context, Moonshot native
adk claude-model use aither-best      # local qwen3.6-27b, free (needs bridge)
adk claude-model use anthropic        # restore stock Claude Code
```

---

## How it works

DeepSeek and Kimi both serve **native Anthropic Messages API** endpoints. No translation
bridge needed. Just point `ANTHROPIC_BASE_URL` at them and set the model name.

```
Claude Code CLI
    │
    ├─ deepseek-flash/pro → https://api.deepseek.com/anthropic (NATIVE)
    ├─ kimi-k3/k2.6       → https://api.moonshot.ai/anthropic  (NATIVE)
    ├─ aither-best/fast   → http://127.0.0.1:8151 (bridge → local vLLM)
    └─ anthropic          → https://api.anthropic.com (stock, clears all)
```

---

## Quick start

```bash
# 1. Install awdk (if not already)
pip install -e ./awdk

# 2. Store your API key (local only, never sent anywhere)
adk keys set deepseek     # paste your DeepSeek API key
adk keys set openrouter   # or OpenRouter for 200+ models
adk keys set moonshot     # or Moonshot for Kimi K3

# 3. Switch and go
adk claude-model use deepseek-flash

# 4. Restart Claude Code (exit + re-run `claude`)
```

That's it. No bridge to start, no config files to edit, no model name tricks.

---

## Prerequisites

| Profile | What you need |
|---------|--------------|
| `deepseek-flash` / `deepseek-pro` | DeepSeek API key (`adk keys set deepseek`) |
| `kimi-k3` / `kimi-k2.6` | Moonshot API key (`adk keys set moonshot`) |
| `aither-best` / `aither-fast` | AitherClaudeBridge running + the model router |
| `mixed` | Bridge + the model router + DeepSeek key (all tiers) |
| `anthropic` | Nothing (restores stock Claude Code) |

---

## Multi-tier switching (no restart needed)

The `mixed` profile maps each Claude Code tier to a different backend:

| Tier (select with /model) | Routes to | Use for |
|--------------------------|-----------|---------|
| **Opus** (default) | DeepSeek V4 Pro | Deep reasoning, architecture |
| **Fable** | DeepSeek V4 Flash | Fast coding, iteration |
| **Sonnet** | Local aither-orchestrator/qwen3.6 | Free, offline, private |
| **Haiku** | Local a local 12B model | Fast local, subagents |

```bash
adk claude-model use mixed       # enable multi-tier
adk claude-model bridge start    # start the translation bridge
# Restart Claude Code, then:
# /model → pick Opus for reasoning, Fable for speed, Sonnet for local
```

---

## Commands

| Command | What it does |
|---------|-------------|
| `adk claude-model list` | Show all available profiles with context sizes |
| `adk claude-model use <profile>` | Switch to a profile (rewrites settings.json) |
| `adk claude-model status` | Show the active profile and all env vars |
| `adk claude-model check` | Send a real turn to prove the profile works |
| `adk claude-model bridge start` | Start the translation bridge (local models only) |
| `adk claude-model bridge stop` | Stop the bridge |
| `adk claude-model auto <profile>` | One-shot: bridge + switch + verify |

---

## Available profiles

| Profile | Backend | Context | Transport | Notes |
|---------|---------|---------|-----------|-------|
| `deepseek-flash` | DeepSeek V4 Flash | **1M** | Native | Fast, cheap, recommended daily driver |
| `deepseek-pro` | DeepSeek V4 Pro | **1M** | Native | Deep reasoning (2-5 min thinking) |
| `kimi-k3` | Kimi K3 (2.8T MoE) | **1M** | Native | Thinking always on, sometimes 429 |
| `kimi-k2.6` | Kimi K2.6 | 256K | Native | Always available, thinking optional |
| `aither-best` | Local qwen3.6-27b | 128K | Bridge | Free, needs local GPU |
| `aither-fast` | Local a local 12B model | 16K | Bridge | Fast mechanical edits |
| `anthropic` | Anthropic (stock) | 200K | Stock | Restores defaults |

---

## DeepSeek native Anthropic API

DeepSeek serves a native Anthropic Messages API at `https://api.deepseek.com/anthropic`.
No bridge, no translation, no model name spoofing needed.

Key details from DeepSeek's docs:
- **Model names**: `deepseek-v4-pro[1m]`, `deepseek-v4-flash`
- **The `[1m]` suffix**: requests the 1 million token context variant
- **Model mapping**: DeepSeek also accepts Claude model names:
  - `claude-opus*` → deepseek-v4-pro
  - `claude-sonnet*` / `claude-haiku*` → deepseek-v4-flash
- **Web Search**: natively supported — DeepSeek invokes it automatically when needed
- **Tool calls**: fully supported in the Anthropic protocol

---

## What the tool sets (all 6 vars, atomically)

```
ANTHROPIC_BASE_URL          → provider's /anthropic endpoint
ANTHROPIC_AUTH_TOKEN        → your API key (from ~/.aither/provider_keys.json)
ANTHROPIC_MODEL             → the model name
ANTHROPIC_DEFAULT_OPUS_MODEL   → same (DeepSeek Pro or Flash)
ANTHROPIC_DEFAULT_SONNET_MODEL → same
ANTHROPIC_DEFAULT_HAIKU_MODEL  → subagent model (Flash)
CLAUDE_CODE_SUBAGENT_MODEL     → subagent model
CLAUDE_CODE_AUTO_COMPACT_WINDOW → context window size
CLAUDE_CODE_EFFORT_LEVEL       → effort level
```

All 6 model vars are set atomically — a partial set leaves some Claude Code paths
pointing at unknown model names, causing silent failures on subagents and background tasks.

---

## Context-window adaptation

When switching to a model with <200K context (local models), the tool automatically:
1. Disables heavy stop hooks (self-review, lint-gate, debt-ledger, live-proof)
2. Raises the autocompact threshold from 70% → 85%
3. Backs up the original project settings
4. Restores everything when switching back to a ≥200K profile

For DeepSeek (1M) and Kimi (1M) this isn't needed — full hooks stay enabled.

---

## The bridge (local models only)

Only needed for `aither-best` / `aither-fast` / `aither-orchestrator` profiles.
`AitherClaudeBridge` (port 8151) translates Anthropic → OpenAI for the model router.

```bash
adk claude-model bridge start   # starts with fleet env vars
adk claude-model bridge status  # health check
adk claude-model bridge stop    # kill it
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Context limit reached" instantly | Model name not recognized. Use native DeepSeek profiles (not bridge) |
| "ECONNRESET" | Bridge died (local profiles) or network issue. `bridge start` |
| "API Error: Retrying" | Provider overloaded (Kimi K3 often 429). Switch to DeepSeek |
| Compacting every turn | Wrong profile. Use `deepseek-flash` (native 1M) |
| "Unable to resolve credential" | Run `adk keys set deepseek` to store your key |

---

## Adding your own provider

Any provider with an Anthropic-compatible `/v1/messages` endpoint works:

```yaml
# In your claude_profiles.yaml:
my-provider:
  transport: native
  description: "My Provider (native Anthropic API)"
  base_url: "https://api.myprovider.com/anthropic"
  auth_secret: MY_PROVIDER_API_KEY
  model: "my-model-name"
  context_window: 200000
  effort: high
```

Then: `adk claude-model use my-provider`
