# DeepSeek V4

OpenAI-shaped API. Cheap, strong at reasoning, and already wired into the
platform as the cloud reasoning overflow.

**`deepseek-chat` is RETIRED.** The API rejects it with "supported API model names
are deepseek-v4-pro or deepseek-v4-flash". Use:

| model | for |
|---|---|
| `deepseek-v4-pro` | reasoning, hard problems |
| `deepseek-v4-flash` | fast default |

Key from <https://platform.deepseek.com/api_keys>.

## Claude Code — via the bridge

DeepSeek is OpenAI-shaped, so it needs AitherClaudeBridge to back a Claude Code
session:

```bash
adk claude-model bridge start
adk claude-model use deepseek-pro   # or deepseek-flash
adk claude-model check
```

The bridge resolves `DEEPSEEK_API_KEY` from env, then AitherSecrets. If
`bridge/status` shows `"has_credential": false` for the `deepseek` backend, the
key is not resolving and every call will 401 — fix that before debugging anything
else, because the failure surfaces as an unhelpful upstream error.

## awdk

```bash
adk keys set deepseek sk-...
adk ask "hello" --backend deepseek
```

`adk backend guide deepseek` prints the same steps from the in-tree registry.

## AitherOS platform services

Already in `PROVIDER_REGISTRY` with base `https://api.deepseek.com`, default
model `deepseek-v4-flash`:

```python
store("DEEPSEEK_API_KEY", "<key>")
store("LLM_PROVIDER", "deepseek")
```

Note the Model Assignment Plane resolves `reasoning` to `deepseek-v4-pro`.

**A keyless DeepSeek hangs think-mode rather than failing fast** — if reasoning
requests stall, check the key before suspecting the scheduler.
