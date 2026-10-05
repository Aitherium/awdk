---
name: acp-drive
description: >-
  Drive any external ACP agent from your agent. The Agent Client Protocol lets one agent drive another. When your task needs a different model, a different tool surface, or a sandbox your loop doesn't have, you can hand a sub-problem to an external ACP agent — Claude Code (claude-agent-acp), Codex (codex-acp), Gemini CLI (gemini-cli), or any binary that speaks ACP on stdio — and read back its answer. Your memory, faculties and approval gate stay in charge; the external agent runs its own loop underneath.
---
<!-- Generated from the awskills pack (skills/acp-drive.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# acp-drive — drive any external ACP agent from your agent

The Agent Client Protocol lets one agent drive another. When your task needs a
different model, a different tool surface, or a sandbox your loop doesn't have,
you can hand a sub-problem to an **external ACP agent** — Claude Code
(`claude-agent-acp`), Codex (`codex-acp`), Gemini CLI (`gemini-cli`), or any
binary that speaks ACP on stdio — and read back its answer. Your memory,
faculties and approval gate stay in charge; the external agent runs its own
loop underneath.

## Register one as your model backend

```bash
adk backend add acp --command claude-agent-acp  # or: --command codex-acp
adk backend use acp                             # switch a running agent live
```

`--command` is executed as-is (no shell), so it must be the binary that speaks
ACP on stdio; pass its arguments with repeated `--arg`. `add` stores ONE ACP
backend (a second `add` replaces the first) and already makes it the default.
For headless runs, `AITHER_ACP_COMMAND` and `AITHER_ACP_ARGS` override the
saved command. The external agent
becomes a normal LLM provider: `AitherAgent`'s memory + faculties ride on top
of the external agent's loop, and the external agent owns its own tool
execution.

## Drive it on demand (agent tools)

- `acp_list_agents` — enumerate the bundled manifests.
- `acp_connect <command> [args]` — spawn the agent, open a session, get a
  `session_id`.
- `acp_prompt <command> [args] <message> [session_id]` — one turn; reuse the
  `session_id` to continue the same conversation.
- `acp_close <command> [args]` — end the session and free the subprocess.

Sessions survive across tool calls **on one event loop** (the agent runtime).
A loop change reconnects fresh and drops the old sessions — the tool says so,
it does not hang.

## CLI one-shot

```bash
adk acp connect --command claude-agent-acp
adk acp prompt --command claude-agent-acp "summarize the git log"
adk acp list-sessions --command claude-agent-acp
```

Each one-shot spawns a fresh agent process; arguments go in `--arg`.

## When to use it

- Your loop lacks the tool the task needs; the external agent has it.
- You want a second model's judgement on a hard sub-problem.
- You need a sandboxed agent for untrusted input.
- **Not** for everything: spawning a subprocess per turn is heavier than a
  native backend. Reserve it for the sub-problem that genuinely needs it.

## Fail-loud contract

An empty command fails when the backend is built, never as an inert provider.
`adk backend add acp` does NOT check that the binary exists, so prove it before
registering: `adk acp connect --command <binary>` must print the agent's name
and a session id. A crashed agent surfaces its error instead of hanging.
