---
name: local-research
description: Delegate codebase and web exploration to the local aither-local MCP server so frontier context stays small.
---
<!-- Generated from the awskills pack (skills/local-research.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# Local research — delegate the digging

`aither-local` is a stdio MCP server that gives every Claude Code tier the same
five tools: the main frontier session, every subagent, and every workflow agent.
A local model runs the search/read loop and returns a short brief; the frontier
model keeps only the brief, not the raw tool output.

`adk claude setup` registers it at USER scope in `~/.claude.json` and ships this
skill through the `awsh` plugin. The registration is idempotent and never
replaces an entry you edited. Restart Claude Code after setup.

## Which tool

| Tool | Use it when |
|---|---|
| `research` | Where/how/why does this codebase work; multi-file tracing; any exploration that would take more than ~5 searches or reads. `scope: code` for the repo, `web` for docs, `both` (default) for either. |
| `recall` | Before investigating anything: has this already been answered? Returns brief ids, ages and first lines. |
| `brief` | Re-read a stored brief in full (answer, notes, sources) by id. |
| `deep_research` | The OUTSIDE world: papers, libraries, vendors, standards, current events. Runs AitherSearch's DeepResearchPipeline with citations. |
| `ask_aither` | Hand a whole scoped task to a local agent that has the platform's tools (code graph, memory, search, fleet agents). Unlike the others it CAN write files and take actions. |

`research` accepts `paths` to focus the search, `max_steps` (default 50),
`budget_s`, a driver `model` override, and `fresh` to ignore a stored brief.

## How crystallization works

Every brief is stored under `~/.aither/crystal` with the size and mtime of every
file it read or cites. `research` returns the stored brief while all of those
files are unchanged; when one changes it re-investigates. Briefs are also offered
to awm memory, at the scope derived from the repository's origin remote and the
current user (`AWM_SCOPE` or `awm_scope` in `~/.aither/delegate.json` overrides
it). Full trajectories are kept under `~/.aither/agent-trace` as training data
for the local driver.

## Measured (2026-10-26)

- **28 local tool calls in 82 s**, with **65,944 characters kept out of frontier
  context**.
- A DeepSeek subagent **recalled a stored brief in 42 s**.
- The synthesizer is `deepseek-v4-flash` (the execution tier, not a frontier
  model). `deepseek-v4-flash-pool` keeps synthesis fully local at roughly 2 min
  per brief.

## Traps

- **The GPU is shared.** A local step measured 0.19 s once and 30.9 s the next
  time while another lane held the GPU. Each step has a 25 s deadline and falls
  back to the configured fallback model; three misses in a row move the rest of
  the run there. A slow brief is not a broken brief — read the `Gaps:` section
  before re-running.
- **Fleet ports are HTTPS relays.** AitherSearch and the other fleet services
  use self-signed TLS. Point the client's trust store at the node CA bundle
  (`~/.aither/aithernet-ca-bundle.pem`); without it the call fails closed.
- **`ask_aither` CAN write files.** Give it explicit paths, constraints and the
  expected output, and read back every file it reports writing.
- **Roots are per machine.** The default roots are the session's working
  directory, the crystal store and Claude's project memory. Add more with `roots`
  in `~/.aither/delegate.json`; a path outside the roots is refused, as are
  credential-shaped files and VCS/SSH internals.
- **One implementation.** The server of record is `adk/awdelegate.py` in awdk.
  Registrations that point at `tools/claude-backend/awdelegate.py` load a thin
  compatibility shim for the same file.
- **User-scope registration matters.** A project-scoped entry would deny the
  tools to subagents and workflow agents. Keep the server in `~/.claude.json`.

## When not to use it

A single file you already know, a two-line grep, an edit, or a question whose
answer is in your current context. Delegation has a fixed cost; below ~5
searches or reads, pay it yourself.
