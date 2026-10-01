---
name: aither-agent-notebook
description: >-
  Turn 'build X' into a reviewable, re-runnable plan. A one-shot agent dispatch is a black box: it runs, it either worked or it didn't, and you can't see *why*, replay it with one input changed, or hand it to a human before it touches anything. An Agent Notebook (.anb) fixes that. It's AitherOS's executable, reviewable unit of agent work — an ordered list of typed cells that runs on the platform, records a cost-tracked run every time it executes, and can be replayed, diffed, reviewed, gated, and exported to a Jupyter .ipynb.
---

# aither-agent-notebook — turn "build X" into a reviewable, re-runnable plan

A one-shot agent dispatch is a black box: it runs, it either worked or it didn't, and you
can't see *why*, replay it with one input changed, or hand it to a human before it touches
anything. An **Agent Notebook** (`.anb`) fixes that. It's AitherOS's executable, reviewable
unit of agent work — an ordered list of **typed cells** that runs on the platform, records a
**cost-tracked run** every time it executes, and can be **replayed, diffed, reviewed, gated,
and exported to a Jupyter `.ipynb`**.

Think "a Jupyter notebook whose cells are agent steps" — plan, prompt, tool call, agent
delegation, human checkpoint, result — instead of Python snippets. It is the durable
counterpart to `adk forge` (which is fire-and-forget).

## Cell types

An Agent Notebook is a graph of cells (each can `depends_on` others):

| Cell | What it does |
|------|--------------|
| `context` / `plan` / `note` / `result` | Content cells — grounding, the plan, notes, the final answer (no execution). |
| `prompt` | Ask an LLM / agent persona a question. |
| `tool_call` | Invoke a single platform tool. |
| `agent_delegate` | Hand a sub-task to another agent (demiurge, hydra, athena…). |
| `service_call` | Call an internal service endpoint. |
| `transform` | Reshape the output of upstream cells. |
| `checkpoint` | **Human gate** — pause the run until someone approves/edits/rejects. |
| `parallel_block` / `loop` / `condition` | Control flow over the cell graph. |
| `script` | Run code in the sandbox. |

Every execution is a **run** with per-cell traces, token/cost totals, and a status. Set one
run as the **baseline**, then `diff` or `replay` future runs against it — "change the prompt /
a variable / the model and see how it changes the result" without touching the original.

## Use it from awdk

The `notebooks` tool category ships with [awdk](awdk.md) — it proxies the Genesis
`/notebooks/*` API (the same surface the portal notebook UI drives). Agents with the
`demiurge`, `atlas`, or `analyst` identity get these tools by default; add them to any agent
with `--tools notebooks` (or `apply_pack`).

### CLI

```bash
# Turn a task into a runnable notebook (an LLM decomposes it into cells)
adk notebook plan "audit our auth flow for fail-open gates" --agent athena --effort 7

adk notebook list                       # what notebooks exist
adk notebook get <notebook_id>          # inspect its cells, spec, variables
adk notebook run <notebook_id> --var target=lib/core   # execute → prints a run_id
adk notebook status <run_id>            # per-cell traces + cost, poll while it runs
adk notebook export <notebook_id> -o plan.ipynb        # open in VS Code / JupyterLab
```

A run pauses at any `checkpoint` cell; resolve the gate from
**aitherium.com → Notebooks** (approve / edit / reject), then it continues.

### As agent tools (the ReAct loop)

Give the model the six tools and it can plan-then-run its own work:

| Tool | Genesis endpoint |
|------|------------------|
| `notebook_plan(prompt, agent, effort, context)` | `POST /notebooks/plan` |
| `notebook_list(workspace, status, limit)` | `GET /notebooks/` |
| `notebook_get(notebook_id)` | `GET /notebooks/{id}` |
| `notebook_execute(notebook_id, variables, mode)` | `POST /notebooks/{id}/execute` |
| `notebook_run_status(run_id)` | `GET /notebooks/runs/{id}` |
| `notebook_export(notebook_id, path)` | `GET /notebooks/{id}/export` |

Each returns a JSON string the loop reads directly; transport/HTTP errors come back as
`{"error": …}` rather than raising, so a wedged Genesis degrades the tool, not the agent.
The tools also **auto-retry transient upstream errors** (connect failures / 502 / 503 / 504
— a Genesis that's mid-restart or has a cold worker) with bounded backoff, so a routine
restart rides through instead of surfacing a spurious `504`; a genuinely-down Genesis returns
a clear `"Genesis unavailable after N attempts"` rather than a raw gateway error.

## What this is (and isn't)

- **Agent Notebooks (`.anb`)** — *this skill*: structured, executable plans with runs, gates,
  replay/diff, `.ipynb` export. Lives in `lib/orchestration/NotebookEngine.py` + the
  `/notebooks` Genesis router.
- **Research Notebooks** — the *other* thing: a NotebookLM-style "chat with your sources /
  make a podcast" surface (`lib/notebooks/`, `/research-notebooks`). Not this.

## Config & auth

Point the tools at your Genesis with `AITHER_GENESIS_URL` (default `http://a local Genesis endpoint`).
LLM/embeddings route through the governed gateway; the tools trust the internal CA — set
`AITHER_TLS_VERIFY=false` only if you deliberately need to skip verification (never the
default).

**Auth is automatic.** Creating and running (`plan` / `run`) is fail-closed **RBAC-gated**
(requires `can_execute`), so the tools carry your session — they read the `adk login` bearer
from `~/.aither/auth.json` (or `AITHER_API_KEY`) and run **as you**. Nothing extra to do if
you're logged in. Verified live end-to-end as an authenticated user: `create → get → execute
(completed) → status → export .ipynb → delete`, all `200`. If no session is found, reads still
work and writes return a clean `{"error": "HTTP 403 …"}` — the tools degrade, never crash the
agent loop.

MIT-licensed, like everything in `awskills`.
