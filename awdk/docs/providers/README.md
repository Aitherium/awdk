# Model providers — for Aither agents, awdk, and Claude Code

One page per provider, each covering the same three surfaces:

1. **awdk agents** — `adk ask`, `adk up`, the daemon, agent packs.
2. **Claude Code** — pointing the coding agent itself at that model.
3. **AitherOS services** — the platform-wide default for every internal caller.

| provider | doc | ADK backend | Claude Code |
|---|---|---|---|
| Moonshot **Kimi K3 / K2.7 / K2.6** | [kimi.md](kimi.md) | `moonshot` | native — no bridge |
| **DeepSeek** V4 | [deepseek.md](deepseek.md) | `deepseek` | via the bridge |
| **Local AitherOS fleet** (qwen3.6, gemma4, bonsai, orchestrator) | [local-aitheros.md](local-aitheros.md) | `aither` / gateway | via the bridge |
| Anthropic Claude | — | `anthropic` | default, no config |
| OpenAI / any OpenAI-compatible | [openai-compatible.md](openai-compatible.md) | `openai` | via the bridge |

---

## The one concept that explains every setup below

**There are two transports, and picking the wrong one is the most common failure.**

```
                    ┌─ NATIVE ──────────────────────────────────────┐
Claude Code ────────┤  Provider already speaks the Anthropic        │
  (/v1/messages)    │  Messages API. Point ANTHROPIC_BASE_URL       │
                    │  straight at it.                              │
                    │  → Anthropic, Kimi (api.moonshot.ai/anthropic)│
                    └───────────────────────────────────────────────┘
                    ┌─ BRIDGED ─────────────────────────────────────┐
                    │  Provider is OpenAI-shaped. Route through     │
                    │  AitherClaudeBridge :8151, which translates   │
                    │  and preserves tool calls.                    │
                    │  → local fleet, DeepSeek, Ollama, OpenAI      │
                    └───────────────────────────────────────────────┘
```

Claude Code speaks **exactly one protocol**: `POST /v1/messages`. That is the only
thing `ANTHROPIC_BASE_URL` can point at. A provider serving OpenAI's
`/v1/chat/completions` cannot back a Claude Code session directly — not "works
with reduced features", but returns nothing usable.

awdk is the opposite: its router is **OpenAI-shaped by default** and has a
separate Anthropic provider. So a provider that is easy for ADK may need the
bridge for Claude Code, and vice versa. Each doc says which.

## Do not hand-write the Claude Code variables

Claude Code resolves a model **per scenario** — the main turn, the four tier
aliases (opus/sonnet/haiku/fable), and subagents each read a *different*
variable. Setting only `ANTHROPIC_MODEL` leaves the rest pointing at Claude model
names your provider has never heard of. The result is not an error: **the main
chat works perfectly while every subagent and all background summarisation
fail**, which reads as "this model is flaky" rather than "my config is half
written".

So use the switcher, which writes the complete set atomically or writes nothing:

```bash
adk claude-model list
adk claude-model use kimi-k3
adk claude-model check     # proves a real turn
adk claude-model use anthropic   # restore
```

`check` is the important one. `/status` showing your base URL proves
*configuration*, not *function* — it cannot tell a working provider from one that
401s on every call. `check` sends a real Anthropic-protocol request with a tool
definition and reports which model actually answered.

Two more traps, both worth knowing before you debug anything:

- **`env` in `settings.json` overrides your shell exports.** A stale entry there
  silently beats a correct `export`. The switcher warns when it sees both.
- **`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` conflict** when both are set.
  The switcher writes only the latter and actively removes the former.

## Configuring awdk

ADK has first-class provider commands — prefer them over raw env vars, because
they persist and sync to the vault:

```bash
adk backend guide              # list every provider + its setup steps
adk keys set moonshot sk-...   # store a key (also vault-syncs)
adk backend set <provider> --base-url <url> --model <model>
adk ask "hello" --backend moonshot     # test one provider without switching
```

Keys land in `~/.aither/provider_keys.json` and are exported into the environment
for provider constructors. `adk ask --backend X` is the fastest way to prove a
provider works before you make it the default.

## Configuring AitherOS platform services

Every internal service resolves its provider through
the platform's LLM provider configuration module, which reads **AitherSecrets first**.
Switching the platform default is a single vault write — no per-container env, no
image rebuild, no recreate (the ~30s cache TTL means it propagates fleet-wide in
about half a minute):

```python
store("LLM_PROVIDER", "moonshot")            # platform-wide default
store("LLM_PROVIDER_AGENT_SAGA", "deepseek") # bind one agent
store("MOONSHOT_API_KEY", "<key>")           # the credential
```

Precedence runs env → per-agent → per-tenant → per-service → global → `local`.

## Verifying, in order of how much they prove

| check | proves |
|---|---|
| `/status` in Claude Code | the config was read. Nothing about whether it works. |
| `claude_model_profile.py check` | a real turn round-tripped, and names the model that served it |
| `adk ask "hi" --backend X` | ADK can reach that provider |
| `curl :8151/bridge/status` | which backends the bridge sees as reachable, and whether a credential resolved |
| `check_claude_provider_profiles.py` | no profile is half-written or points at a dangling alias |
