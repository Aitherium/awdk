---
name: adk-harnesses
description: >-
  One shell that drives every coding shell. Your agent can hand a task to another coding agent's real product — Claude Code, Codex, Gemini CLI, Aider, OpenCode — and read back one answer.
---

# adk-harnesses — one shell that drives every coding shell

Your agent can hand a task to **another coding agent's real product** — Claude Code,
Codex, Gemini CLI, Aider, OpenCode — and read back one answer.

The important word is *real*. The instinct is to rebuild Claude Code's behaviour
against the raw API: you control the prompt, you control the tools, it feels tidy.
It is a trap. You inherit none of its skills, none of its hooks, none of its account
handling, and you spend the next six months chasing a product that ships faster than
you can track it. So the ADK resolves the binary that is actually installed and runs
*that*.

## See what this machine can drive

```bash
adk harness harnesses
```

```
ID           INSTALLED  TRANSPORT         DESCRIPTION
claude       yes        structured-bidi   Anthropic Claude Code — bidirectional stream-json
gemini       yes        oneshot-per-turn  Google Gemini CLI — one process per turn
terminal     yes        pty-stream        A real shell behind a pseudo-terminal (pwsh/bash)
sandbox      NO         pty-stream        A real Linux TTY inside a dev container
                                          -> Install Docker Desktop
aither       yes        http-stream       An AitherOS agent relayed over SSE
group        yes        http-stream       Several agents in one room, answering concurrently
codex        NO         oneshot-per-turn  OpenAI Codex CLI (codex exec --json)
                                          -> npm i -g @openai/codex
aider        NO         oneshot-per-turn  Aider — pair-programming CLI
                                          -> pip install aider-install && aider-install
opencode     NO         oneshot-per-turn  OpenCode — open-source coding agent
                                          -> npm i -g opencode-ai
```

The `installed` column is per-machine — yours will differ. **ACP is not in this
table.** An ACP agent is driven as a *model backend*, not as a harness session;
see `acp-drive` below.

A harness you have not installed says **NO** and prints the command to get it. That
is deliberate: an absent harness is a missing install, not a missing feature, and
the difference is printed rather than guessed at.

## Drive one

```bash
adk harness serve                         # run the daemon (127.0.0.1:8362)
adk harness new --harness claude          # start a session, get an id
adk harness send  <id> "refactor the retry logic in billing/"
adk harness attach <id>                   # watch it work, live
adk harness list                          # what's running
adk harness kill  <id>                    # teardown, whole process tree
```

`adk harness serve` exposes the whole thing over HTTP so a remote agent can drive
it. `adk harness agents` and `adk harness profiles` list the sovereign agents and
the per-session model profiles available to you.

**Note the namespace is `adk harness`, not `adk shell`.** `adk shell` is a
different command — a passthrough to the AitherShell binary — and it does not
understand any of the subcommands above.

## The four transports, and why the distinction matters

| transport | shape | consequence |
|---|---|---|
| `structured-bidi` | one persistent process, JSON messages both ways | session state survives across turns; full tool use |
| `oneshot-per-turn` | fresh process per turn | **no cross-turn memory in the harness** — you carry context yourself |
| `pty-stream` | a real TTY behind a pseudo-terminal | interactive programs work; output is bytes, not structured events |
| `http-stream` | remote agent over SSE | the agent is not on this machine at all |

The one that surprises people is `oneshot-per-turn`. Codex, Aider and OpenCode each
start a new process for every turn, so anything you want remembered has to be in the
prompt you send. A multi-turn refactor driven through a oneshot harness will
cheerfully forget what it just did, and it will not tell you that is why.

## Scope it — the part people skip

A subagent is launched with an explicit allow-list, and the runner re-validates it
**fail-closed** rather than trusting the caller:

```python
from adk.claude_runner import ClaudeRunner, RunScope

runner = ClaudeRunner()
scope  = RunScope(allowed_tools=["Read", "Grep", "Glob"])     # read-only
rec    = runner.submit(task="audit error handling in ./api", scope=scope)

rec = runner.get(rec.run_id)      # queued | running | completed | failed | cancelled
print(rec.result_text)
runner.kill(rec.run_id)
```

The scope becomes `--allowedTools` on the real CLI, so a subagent asked to *audit*
code cannot write to your disk — enforced by the product you delegated to, not by a
prompt politely asking it not to.

`RunScope` also carries `disallowed_tools`, `cwd`, `add_dirs`, `model`,
`system_prompt` and `timeout_sec`. Set `cwd` and `add_dirs` deliberately: they are
the difference between "read my project" and "read my home directory".

## Three things that will bite you

**1. The prompt goes on stdin, not argv — for `claude`.** argv is readable by any
local process (`ps auxww`, `/proc/<pid>/cmdline`), and a task prompt routinely
carries file contents and occasionally a credential. The Claude runner passes it on
stdin for exactly that reason. The oneshot harnesses (`codex`, `aider`, `opencode`)
take the prompt as a command-line argument because **their CLIs offer no stdin
path** — so on a shared or multi-user host, treat a prompt sent through those three
as visible. That is a property of those tools, not a bug you can configure away.

**2. Each run needs its own account state.** Concurrent subagents sharing one config
directory will corrupt each other's session. The Claude runner sets a per-run
`CLAUDE_CONFIG_DIR`; if you drive a CLI yourself, do the equivalent or run them
strictly one at a time.

**3. Teardown is the whole process tree, not the process.** A coding agent spawns
children — language servers, test runners, watchers. Killing the parent leaves them
holding ports and file locks, and the symptom shows up minutes later as "address
already in use" somewhere unrelated.

## When to reach for this

Delegate to another harness when the sub-problem wants **a different model, a
different tool surface, or a blast radius you want bounded**. A read-only audit, a
migration you want run in a container, a second opinion from a different vendor's
agent on the same diff.

Do not delegate a task that needs your agent's memory or faculties mid-flight — the
subagent has neither. One self-contained task out, one answer back. If you find
yourself sending five follow-ups to the same harness session, the work wanted to
stay in your own loop.

## See also

- `acp-drive` — driving an external ACP agent as a model backend
- `acp-serve` — serving *your* agent to an editor over ACP
- `awdk` — the SDK these harnesses hang off
