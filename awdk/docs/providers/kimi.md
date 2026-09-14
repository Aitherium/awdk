# Moonshot Kimi (K3 / K2.7 Code / K2.6)

Kimi K3 is a 2.8T-parameter MoE (16 of 896 experts active) with a **1M-token
context** and native vision. For coding agents the practical draws are the long
horizon and that **thinking is always on** for K3.

## The thing that makes Kimi unusual here

Moonshot publishes **two** endpoints for the same models:

| endpoint | shape | use it for |
|---|---|---|
| `https://api.moonshot.ai/anthropic` | Anthropic Messages API | **Claude Code** |
| `https://api.moonshot.ai/v1` | OpenAI chat-completions | **awdk**, AitherOS services |

So Kimi is the one provider that needs **no bridge for Claude Code** — point
`ANTHROPIC_BASE_URL` straight at the `/anthropic` endpoint. Routing it through
AitherClaudeBridge would add a translation hop that can only lose fidelity.

Get a key at <https://platform.kimi.ai/console/api-keys>. K3 is a flagship model:
it unlocks after a top-up (minimum $1), and your cumulative top-up sets your rate
limits.

---

## 1. Claude Code

```bash
adk claude-model use kimi-k3
adk claude-model check
```

The profile resolves your key from `MOONSHOT_API_KEY` (env, then the vault) and
writes all six model variables plus a **1,048,576** compaction window and
`max` effort. Restart Claude Code, then `/status` should show
`https://api.moonshot.ai/anthropic`.

`/model` will **not** list Kimi — that menu is a fixed built-in list of Claude
aliases. This is expected and is not a sign the switch failed; `/status` is the
authority.

### Choosing a variant

| profile | context | thinking | notes |
|---|---|---|---|
| `kimi-k3` | 1M | always on | the default; works out of the box |
| `kimi-k2.7-code` | 256K | **required** | rejects requests with thinking off |
| `kimi-k2.7-code-highspeed` | 256K | **required** | ~5-6x output speed |
| `kimi-k2.6` | 256K | optional | lowest latency; safe with thinking off |

**The K2.7 trap:** `kimi-k2.7-code` requires `thinking` to be explicitly enabled.
With thinking off, every request fails with
`400 invalid thinking: only type=enabled is allowed for this model` — and
WebSearch fails with it. Press `Tab` in Claude Code until the "Thinking on"
indicator shows *before* you start working. K3 is unaffected. If you need
thinking off, use `kimi-k2.6`.

**WebFetch is not supported** by the Moonshot endpoint yet; it reports
"temporarily unavailable". Paste the page content in, or use an MCP scraping tool
(or AitherBrowser via the `aitherbrowser` skill). Unrelated to your config.

---

## 2. awdk

ADK talks to Kimi over the **OpenAI-shaped** endpoint. `moonshot` is a built-in
backend, aliased `kimi` and `kimi-k3`:

```bash
adk keys set moonshot sk-...      # stores + vault-syncs
adk ask "hello" --backend moonshot
```

Or via environment:

```bash
export MOONSHOT_API_KEY=sk-...
```

To make it the ADK default rather than a per-call `--backend`:

```bash
adk backend set moonshot --base-url https://api.moonshot.ai/v1 --model kimi-k3
```

`adk backend guide moonshot` prints these steps from the in-tree registry.

### Parameters K3 fixes — omit them

`temperature=1.0`, `top_p=0.95`, `n=1`, `presence_penalty=0` and
`frequency_penalty=0` are **fixed** for K3. Sending different values is an error,
not a hint. If you have an agent that sets a custom temperature, it must skip it
for this model. Reasoning depth is controlled by the top-level `reasoning_effort`
field instead (`low` | `high` | `max`, default `max`).

`max_completion_tokens` defaults to 131,072 and can go to 1,048,576.

### Multi-turn and tools

Return the **complete assistant message** unchanged into the next request —
keeping only `content` and dropping the rest breaks tool calls and thinking
continuity. Vision input must be an **array of content objects**, and public
image URLs are not supported: use base64 or `ms://<file-id>`.

---

## 3. AitherOS platform services

`moonshot` is already in `PROVIDER_REGISTRY`
(in the platform's provider registry) as base `https://api.moonshot.ai/v1`,
model `kimi-k3`, api shape `openai`. Switch the fleet with two vault writes:

```python
store("MOONSHOT_API_KEY", "<key>")
store("LLM_PROVIDER", "moonshot")     # or LLM_PROVIDER_AGENT_<AGENT> for one agent
```

Propagates fleet-wide within ~30s. No rebuild, no recreate.

You can also reach Kimi through AitherClaudeBridge for non-Claude-Code clients —
the `kimi-k3-openai` alias maps to `moonshot/kimi-k3` in
the bridge's alias configuration.

---

## Troubleshooting

| symptom | cause |
|---|---|
| `401` | `ANTHROPIC_AUTH_TOKEN` is not a valid Moonshot key, or `ANTHROPIC_API_KEY` is also set and conflicting |
| `model not found` | a typo in one of the six model vars — re-run `use kimi-k3` rather than hand-editing |
| main chat fine, **subagents fail** | classic partial config. Some model vars still name Claude models. `claude_model_profile.py status` reports exactly which. |
| `400 invalid thinking` | K2.7 Code with thinking off — press `Tab`, or switch to `kimi-k2.6` |
| config changes ignored | a stale `env` entry in `~/.claude/settings.json` overriding your shell, or Claude Code not restarted |
| Kimi missing from `/model` | expected — that menu is a fixed Claude-alias list. Trust `/status`. |
