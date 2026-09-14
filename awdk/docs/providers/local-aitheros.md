# Local AitherOS fleet (qwen3.6, gemma4, bonsai, deepseek-r1, orchestrator)

Running the coding agent on your own GPUs — no per-token cost, nothing leaving
the box.

> **KNOWN UPSTREAM DEFECT (2026-07-31) — read before relying on this.**
> A real `claude` session may fail with `API Error: 502 ... returned an EMPTY
> answer`. **That error is the bridge working correctly.** Root cause is
> upstream: MicroScheduler intermittently ignores the pinned model and serves
> the DGX instead, which returns empty content with HTTP 200 and zero usage.
> Measured in one session — the bridge asked for `gemma4-12b`, got
> `served='qwen36-27b-dgx'` with zero tokens, while other calls in the *same*
> session correctly served `gemma4-12b` and succeeded.
>
> The bridge no longer hides this: it clamps `max_tokens` to the served model's
> real context (32,000 -> 12,282 for gemma4-12b) and returns a 502 naming the
> requested model, the served model and the usage. Before that fix it relayed
> the empty answer as a successful 200 and Claude Code rendered a blank reply
> with no error. If you hit it, check
> `curl -sk https://127.0.0.1:8150/llm/backend-health` and retry or pin another
> profile.

Every local model is served through **MicroScheduler** (`:8150`, HTTPS with the
internal CA), which owns the queue and coordinates VRAM. Never dial a vLLM
container directly and never use `verify=False` — trust the internal CA.

```bash
curl -sk https://127.0.0.1:8150/v1/models     # what is actually available
```

## Claude Code needs the bridge

MicroScheduler speaks OpenAI `/v1/chat/completions`. Claude Code speaks only the
Anthropic Messages API. **AitherClaudeBridge** (`:8151`) translates between them
and is the only supported path.

```bash
adk claude-model bridge start   # host-local
adk claude-model use aither-best
adk claude-model check
```

Or run it as a fleet service (`aither-claude-bridge`, profiles `gpu` / `chat-*`).
It reuses the microscheduler image and bind-mounts its two source files, so a
code change needs a **restart, not a rebuild** — but a bind mount makes the FILE
current, not the PROCESS, so restart the container or it keeps serving the copy
it imported at boot.

### Profiles

| profile | model | context | good for |
|---|---|---|---|
| `aither-best` | qwen3.6-27b (DGX) | 131,072 | real coding work |
| `aither-fast` | gemma4-12b | 16,384 | mechanical edits, quick questions |
| `aither-orchestrator` | alias → qwen36-27b-dgx | 65,536 | the orchestrator path |
| `aither-tiny` | bonsai on the 5090 | 4,096 | trivial turns only |

Aliases resolve in the bridge's alias configuration.

**Model aliases can reroute.** Asking for `aither-orchestrator` is answered by
`qwen36-27b-dgx` (measured 2026-07-31). The bridge reports the model that
*actually* served each turn rather than echoing your request — `check` prints it,
and the bridge logs `served=`. Do not assume the name you asked for is the name
that answered.

## Expectations, honestly

A local 12-27B model is **not** a drop-in replacement for a frontier model on
hard agentic work. What it is good at: mechanical refactors, well-scoped edits,
test writing, and anything you would otherwise burn tokens on. Tool calling works
correctly through the bridge (verified end-to-end: tool call → tool result →
grounded answer), but small models pick worse tools and recover from mistakes
less well.

Match the context window to the job. `aither-tiny` at 4,096 tokens will compact
almost immediately in a real session — it is there for trivial turns, not for
coding.

**Streaming is synthesized, not incremental.** The bridge fetches the complete
answer upstream, then renders the Anthropic event stream. This is deliberate:
MicroScheduler's streaming path drops `tool_calls` entirely and answers in
AitherOS-typed SSE rather than OpenAI chunks, so streaming upstream would trade
working tool calls for a token trickle — and a coding agent without tool calls is
inert. You wait for the turn, then it appears quickly. Set
`AITHER_BRIDGE_UPSTREAM_STREAM=1` to stream genuinely incremental text for
tool-free requests only.

## awdk

Point ADK's router at MicroScheduler's OpenAI endpoint directly — ADK is
OpenAI-shaped natively, so it needs no bridge:

```bash
export AITHER_LLM_BASE_URL=https://127.0.0.1:8150/v1
adk ask "hello" --backend openai_compat
# or persist it:
adk backend set openai_compat --base-url https://127.0.0.1:8150/v1 --model qwen3.6-27b
```

In-container the URL is `https://aitheros-microscheduler:8150/v1`. Note the ADK
daemon needs `AITHER_OFFLINE=1` for platform tools.

## Troubleshooting

| symptom | cause |
|---|---|
| `check` says cannot reach `:8151` | bridge not running — `bridge start`, wait ~25s |
| `503 no AITHER_BRIDGE_TOKEN` | auth fails closed by design. `provision-token`. |
| `401` from the bridge | token mismatch between `~/.aither/claude_bridge_token` and the running process — restart the bridge after re-provisioning |
| empty replies when streaming | you enabled upstream streaming with tools in play; set `AITHER_BRIDGE_UPSTREAM_STREAM=0` |
| a different model answered | expected for rerouting aliases — check `served=` in the bridge log |
| bridge shows a backend unreachable | `curl :8151/bridge/status` reports each backend and whether a credential resolved |
