# OpenAI and any OpenAI-compatible endpoint

Covers OpenAI itself, vLLM/llama.cpp servers, OpenRouter, and anything
else exposing `/v1/chat/completions`.

## Adding one to the bridge (this is how Claude Code reaches it)

Add a backend to the bridge configuration:

```yaml
backends:
  my-provider:
    base_url: https://api.example.com/v1
    api_key_secret: MY_PROVIDER_API_KEY    # resolved from env, then AitherSecrets
    # headers:                             # optional extra headers
    #   X-Custom: value

model_aliases:
  my-model: my-provider/their-model-name
```

Then add a profile in the profile configuration:

```yaml
profiles:
  my-model:
    transport: bridge
    description: "What this is good for"
    model: my-model            # must match a model_aliases key
    context_window: 131072     # must match the model's REAL context
    effort: high
```

`config/` is bind-mounted, so a **restart** picks both up — no rebuild. Then:

```bash
adk claude-model check   # catches typos
adk claude-model use my-model
adk claude-model check
```

The checker exists because two mistakes here are silent: a `model` that is not in
`model_aliases` falls through to the **default backend** and answers from the
wrong model, and a `context_window` larger than the model's real context produces
context-length errors that look like failed turns.

## llama.cpp

Any `llama-server` exposes `/v1/chat/completions`; declare it as
`http://127.0.0.1:8080/v1` (or the port you started it on) and add an alias for
the model it serves. GGUF weights come from Aitherium's artifact store
(`adk node bootstrap` / `adk setup llamacpp` fetch them for you).

## awdk

```bash
adk keys set openai sk-...
adk backend set openai_compat --base-url https://api.example.com/v1 --model their-model
adk ask "hello" --backend openai_compat
```

`AITHER_LLM_BASE_URL` overrides the router's base URL globally, which is the
quickest way to point ADK at a self-hosted server.

## Tool calling is the thing to verify

An OpenAI-compatible endpoint that does not implement `tools` / `tool_calls`
properly will still return prose — so the agent looks lazy rather than broken.
Prove it with a tool-bearing request before trusting the provider:

```bash
adk claude-model check
```

`check` sends a real tool definition and prints which content blocks came back.
If you never see a `tool_use` block from a model that should have called one, the
provider's tool support is the suspect.
