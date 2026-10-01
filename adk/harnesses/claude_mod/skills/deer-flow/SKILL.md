---
name: deer-flow
description: Run ByteDance's DeerFlow — a LangGraph-based super-agent harness for autonomous research, coding and content creation with sub-agents, persistent memory and sandboxed execution. Covers Docker and local setup, pointing it at your own OpenAI-compatible endpoint via config.yaml, its MCP server support, and driving it from the AitherOS toolkit as a managed agent rather than a separate silo.
---

# deer-flow — a long-running research/coding harness you host yourself

[DeerFlow](https://github.com/bytedance/deer-flow) (MIT, ByteDance) is a super-agent harness:
it orchestrates **sub-agents**, keeps persistent memory, and runs work in a sandbox, aimed at
tasks that take minutes to hours rather than one turn. Python backend on LangChain/LangGraph,
Node/TypeScript frontend.

**Use it when** the job is a long autonomous run — deep research, a multi-file build, a
content pipeline. **Don't reach for it** for a quick edit; [`tau`](tau.md) or
[`openclaw`](openclaw.md) start in seconds and DeerFlow is a whole stack.

## Install

```bash
git clone https://github.com/bytedance/deer-flow.git
cd deer-flow
make setup            # interactive wizard: LLM provider + optional web search
```

Then either path:

```bash
make docker-init      # first time only
make docker-start     # recommended
```

```bash
make install          # local dev instead
make dev
```

Open **`http://localhost:2026`**.

**Check:** the UI loads and a trivial task ("summarize this repo") produces a plan with
sub-steps — DeerFlow's whole point is decomposition, so a single flat answer means it's
falling back rather than orchestrating.

## Point it at your own model

`make setup` writes **`config.yaml`** (reference template: `config.example.yaml`); API keys
live in **`.env`**. Add an OpenAI-compatible endpoint under `models:`:

```yaml
models:
  - name: local-qwen
    use: deerflow.models.vllm_provider:VllmChatModel
    model: qwen3:8b
    base_url: http://localhost:8000/v1
```

- `base_url` — `:8000/v1` vLLM · `:11434/v1` Ollama · `:8080/v1` llama.cpp (and ODS on
  macOS/Windows — see [`ods`](ods.md)).
- `use:` selects the provider class; the vLLM provider works for any OpenAI-compatible server,
  not only vLLM.
- The file is **`config.yaml`**, not `conf.yaml` — older write-ups and forks use the latter
  and the app will not read it.

Long autonomous runs are exactly where a **local** model saves real money: DeerFlow can burn
hours of tokens on one task. That's the argument for [`local-inference`](local-inference.md)
here more than anywhere else in this pack.

**Check:** start a task and watch your own server's log (or `ollama ps`) — sustained requests
should hit *your* endpoint, not a hosted provider.

## MCP servers

DeerFlow supports MCP servers in **HTTP/SSE and stdio** modes, configured in `config.yaml`,
with per-tool-call timeouts via `tool_call_timeout`. That matters for long runs: a sub-agent
blocked on a slow tool with no timeout stalls the whole graph, and the symptom is a run that
looks alive but never advances. **Set `tool_call_timeout` before your first long task**, not
after one hangs.

This is also how you give DeerFlow the AitherOS toolset — add the platform MCP endpoint
alongside its built-ins. Get a scoped key rather than reusing a personal one:

```bash
pip install awdk
adk connect          # writes the gateway URL + a scoped key
```

## Driving it from the toolkit

The toolkit can treat DeerFlow as a **managed** agent rather than a separate silo. `adk`'s
pack drivers include a LangGraph-REST driver precisely so LangGraph-shaped agents like
DeerFlow can be *invoked and supervised*, not merely connected:

```bash
pip install awdk
adk connect
```

The distinction is the same one drawn in [`openclaw`](openclaw.md): *connecting* gives
DeerFlow your tools; *managing* lets the toolkit dispatch work to DeerFlow and collect the
result. Managing is the interesting mode for long runs, because it puts the run in the same
ledger as everything else you dispatch.

> **Honest limitation:** the driver has been exercised against a real HTTP server speaking
> the LangGraph wire shape, **not** against a live DeerFlow instance. Treat the managed path
> as "should work, verify on first use" and check the response shape before building on it.

## Sandboxing

DeerFlow runs execution in a sandbox — which is the right default for an agent authorized to
run for hours unattended. Keep it. If you disable it to make something work, you've converted
a long autonomous run into arbitrary code execution on your box with no supervision, which is
a materially different risk from a single supervised turn.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Config edits ignored | wrote `conf.yaml` | the file is `config.yaml` |
| Still using a hosted model | `models:` entry not selected, or key still in `.env` | check the wizard's choice |
| Run looks alive, never advances | a tool call with no timeout | set `tool_call_timeout` |
| One flat answer, no sub-steps | not orchestrating — model too weak for planning | use a stronger model for the planner role |
| Docker start fails | first-run init skipped | `make docker-init` before `make docker-start` |
| Huge token spend | long runs against a paid API | point it at a local endpoint |

## Next

- **[`local-inference`](local-inference.md)** — the endpoint that makes long runs affordable
- **[`ods`](ods.md)** — a full local stack to host the model DeerFlow uses
- **[`tau`](tau.md)** / **[`openclaw`](openclaw.md)** — lighter agents for short tasks
- **[`awdk`](awdk.md)** — the toolkit that manages and dispatches
