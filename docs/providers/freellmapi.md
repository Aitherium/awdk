# Free cloud (your own free-tier keys) — FreeLLMAPI

[FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) (MIT, by Tashfeen Ahmed)
puts the free tiers of many hosted model providers behind one OpenAI-compatible
`/v1` endpoint, with routing, failover and per-key quota tracking. awdk uses it as
its **non-local route**: when no local model can take a turn, it goes to *your*
FreeLLMAPI before any paid API key.

> **Bring your own keys.** You run your own FreeLLMAPI with your own provider
> accounts. Those providers' terms still apply to traffic proxied through it, and
> upstream describes it as *for personal experimentation and learning, not
> production*. Read the
> [terms-of-service review](https://github.com/tashfeenahmed/freellmapi/blob/main/docs/en/architecture/00-high-level-index.md#terms-of-service-review)
> before you turn it on. Aitherium does not run, pool or resell it.

## Turn it on

```bash
adk free-cloud up            # starts the pinned FreeLLMAPI image on 127.0.0.1:3001
# open http://127.0.0.1:3001 -> add YOUR provider keys on the Keys page -> copy the unified key
adk keys set freellmapi      # paste the unified key (hidden input; stays on this machine)
adk free-cloud enable
```

Already use the FreeLLMAPI desktop app? Skip `up`: awdk finds it on `:31415`
(and the server/Docker install on `:3001`). Elsewhere: `adk free-cloud enable --url http://host:3001`.

## Routing order

1. A local model (a configured desktop spine, a self-hosted server, vLLM).
2. **FreeLLMAPI** — only when enabled, a unified key is set, and at least one of
   your provider keys can serve a model right now.
3. Paid / BYO API keys (gateway, Anthropic, OpenAI, DeepSeek, ...).

`adk --backend freellmapi ...` (or `adk backend set freellmapi`) pins it explicitly.
The model is FreeLLMAPI's own `auto` router unless you name preferred models:
`adk free-cloud enable --models gemini-2.5-flash,llama-3.3-70b-versatile`.

## Commands

| command | does |
|---|---|
| `adk free-cloud status` | detected? enabled? catalog / ready model counts |
| `adk free-cloud models [--all]` | models it can serve now (`--all`: whole catalog) |
| `adk free-cloud up [--replace]` | start (or restart) the pinned image; idempotent |
| `adk free-cloud down` | remove the container — your keys volume stays |
| `adk free-cloud enable` / `disable` | route to it, or stop |

`adk backend list` also shows it. With docker or podman installed, `up` runs
`ghcr.io/tashfeenahmed/freellmapi` at a pinned digest, bound to `127.0.0.1` only,
with a named volume `freellmapi-data`. Its encryption key is generated once into
`~/.aither/freellmapi/freellmapi.env` and never rotated by awdk.

## Where things live

| what | where | synced? |
|---|---|---|
| provider keys | FreeLLMAPI's own encrypted store | never touches awdk |
| unified key | `FREELLMAPI_API_KEY` or `adk keys set freellmapi` | no — this machine only |
| enabled / URL / preferred models | `~/.aither/mods.json` → `freellmapi` | yes, via `awsettings --domain mods push` |

Env overrides: `ADK_FREELLMAPI=1|0`, `ADK_FREELLMAPI_URL`, `ADK_FREELLMAPI_MODELS`.

**The unified key only goes to loopback** (`127.0.0.1`, `::1`, `localhost`) by default. A URL can
arrive from another machine through settings sync, so a non-loopback URL is ignored (and
`adk free-cloud status` says so) until you allow it on THIS machine:
`adk free-cloud enable --url http://host:3001 --allow-remote` (stored in
`~/.aither/freellmapi/local.json`, never synced) or `ADK_FREELLMAPI_ALLOW_REMOTE=1`.

## Agent pack

`awpack install free-cloud` installs the pack that does the steps above
(`adk free-cloud up` + `enable`); `awpack remove free-cloud` followed by
`adk free-cloud down` removes it and leaves your keys volume in place.
