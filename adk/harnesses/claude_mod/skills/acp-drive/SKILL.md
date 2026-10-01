---
name: acp-drive
description: >-
  Drive any external ACP agent from your agent. The Agent Client Protocol lets one agent drive another. When your task needs a different model, a different tool surface, or a sandbox your loop doesn't have, you can hand a sub-problem to an external ACP agent — Claude Code (claude-agent-acp), Codex (codex-acp), Gemini CLI (gemini-cli), or any binary that speaks ACP on stdio — and read back its answer. Your memory, faculties and approval gate stay in charge; the external agent runs its own loop underneath.
---

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
adk backend add acp --command claude            # claude-agent-acp
adk backend add acp --command codex             # codex-acp
adk backend set acp                             # make it the default
adk backend use acp                             # switch a running agent live
```

For headless runs the saved command can be overridden via the environment
(see `adk backend add acp` for the exact variable names). The external agent
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
adk acp connect --command claude
adk acp prompt --command claude "summarize the git log"
adk acp list-sessions --command claude
```

## When to use it

- Your loop lacks the tool the task needs; the external agent has it.
- You want a second model's judgement on a hard sub-problem.
- You need a sandboxed agent for untrusted input.
- **Not** for everything: spawning a subprocess per turn is heavier than a
  native backend. Reserve it for the sub-problem that genuinely needs it.

## Fail-loud contract

A missing command fails at registration, never at first use. A crashed agent
surfaces its error instead of hanging. If a prompt hangs, the agent binary
was probably not installed — `adk backend add acp` prints install hints from
the bundled manifests.
