# Durable turns

A turn survives the process that was running it being killed.

Measured 2026-11-12 against Flue (commit `deee93b`): after a hard kill and a
restart the kit kept the conversation and nothing else. The interrupted turn was
not resumed, the reconnecting client read 0 bytes, and nothing recorded what
happened to the tool call that was in flight. `adk.turn_journal` closes that.

## Where the journal lives

In the store that already holds the session's history: the agent's memory
database, `~/.aither/memory/<agent>.db` (`adk.memory.Memory`). Two tables beside
`conversations`:

- `turn_journal`: append-only, one row per step.
  `turn_accepted` (the request, its replayable arguments, the owner pid and boot
  id), `tool_started` (name, arguments, idempotency key), `tool_finished` (result
  or error), `tool_replayed` / `tool_unknown` (what a resumed attempt did with a
  recorded step), `turn_resumed`, `turn_finished` (the outcome), `turn_delivered`.
- `turn_open`: the turns with no terminal row, so finding them is one small read.

Every row is its own transaction on a connection with `synchronous=FULL`: when a
write returns, the row is on disk. The row for a step is written before the step
runs. Finished turns are pruned after 14 days (`AITHER_TURN_JOURNAL_KEEP_DAYS`).

## What resumes a turn

A turn is an orphan when it has no terminal row and the process that owned it
(pid and boot id, from its last `turn_accepted` / `turn_resumed` row) is gone.

- **At startup** the server scans every `*.db` in the memory directory and runs
  each orphan again in the background (`resume_open_turns`). A client that never
  reconnects finds the answer in the session's history and in
  `GET /sessions/{id}/turns`.
- **The client that reconnects** sends the same request for the same session
  again. If the resume is running it joins it; if nobody has claimed the turn
  yet, this request carries the resume; if the resume already finished, it is
  handed the recorded answer once (and the request sent again after that is a
  new turn).

A turn is resumed at most `AITHER_TURN_RESUME_ATTEMPTS` times (default 2) and
not after `AITHER_TURN_RESUME_MAX_AGE_S` (default one day); past either it is
`abandoned`.

## The resume decision

The resumed turn is told what the journal knows (a `[TURN RESUMED AFTER A
RESTART]` block in its system prompt) and runs again. For each tool call it
makes, keyed by tool name, arguments and the count of that same call so far:

| journal says | tool declared | the resumed turn |
|---|---|---|
| finished (result or error) | anything | gets the recorded result; the tool is **not** run again |
| started, no finish | `repeat_safe` | runs it again |
| started, no finish | not declared (default) | is told `outcome_unknown`; the tool is **not** run. The model may check the effect, or call it again, which runs it |
| no record | anything | runs it |

Declare a tool safe to repeat when it is idempotent or a pure read:

```python
@agent.tool(repeat_safe=True)
async def lookup_rate(currency: str) -> str: ...
```

`ToolRegistry.register(..., repeat_safe=True)` and the `@tool` decorator take the
same flag; a declared read `action_class` counts too. The tool name is never
used to guess. While a journaled call runs, `adk.turn_journal.IDEMPOTENCY_KEY`
holds `<turn_id>:<step key>` for a tool that talks to a system with idempotency
keys of its own.

## Outcomes

Every turn ends with one: `finished`, `failed` (with the reason; an empty answer
is a failure), `resumed` (interrupted by a kill and completed after it) or
`abandoned` (cancelled, too old, out of attempts). It is emitted as a
`turn_outcome` event, returned by `GET /sessions/{id}/turns` with the steps, and
summarized under `turns` in `/health` (`open`, `resumed`, `abandoned`, `failed`,
`replayed_steps`, `unknown_steps`, `rerun_steps`, `startup_resume`, `last`).

`GET /sessions/{id}/turns?wait=N` holds the answer until the session has no open
turn (at most 120 s). `?agent=<name>` picks a named agent's store.

## Switch

On by default for every agent whose memory is the SQLite store.
`AITHER_TURN_JOURNAL=0` turns it off. A journal failure (a full disk, a locked
file) is logged and counted in `/health` (`journal_errors`); the turn runs
without it.

## awsh

- `awsh_resumable(harness="awdk")` lists the kit's sessions from this store,
  read-only, and marks the ones whose last turn a kill cut off (`open_turn`).
- A resumed `awdk` session carries the resumed id on every turn, so it continues
  the same conversation; the principal that resumed it reaches it (the spawner
  rule: exact principal match, text framed as peer text), nobody else.
- A relay turn whose kit is killed mid-stream waits for the kit to return
  (`AITHER_RELAY_RESUME_WAIT_S`, default 180) and emits the resumed turn's
  answer as that turn's answer (`resumed_after_kill: true`).
- `awsh_interrupt` reaches a streaming `awdk` turn: the session reports `busy`
  for the whole turn, and the interrupt is `POST /chat/steer {action: "cancel"}`
  on the kit, which now implements it.
- The spawn `cwd` travels with each `awdk` turn and the kit binds it for that
  turn's built-in file, search, git and shell tools (`adk.turn_cwd`). The file
  tools' allowed roots still apply.

## Not covered

- A kill **during model generation** loses that generation: the turn restarts
  from its last recorded tool result.
- Tokens already streamed to a client are not replayed; the reconnecting client
  gets the final answer.
- A turn that ran under a caller's `auth` context is not resumed at startup (the
  principal cannot be restored from disk); it waits for that caller to send the
  request again, and is abandoned after the resume window.
- Several server processes sharing one home: a live owner is never claimed, and
  a claim is one SQLite transaction, but the join of a running resume is
  per-process. A dead owner whose pid was reused by another process is not
  detected, so that turn stays open.
- A synchronous tool (the built-in `shell_exec`) blocks the kit's event loop
  while it runs, so a cancel sent during it lands when it returns.
- A side effect that a killed tool had already started keeps going on its own
  (a child process outlives the kit). The journal says `outcome_unknown`; it
  does not undo anything.
- Two turns of one agent at the same time share `_current_intent`; the prompt
  mix-up between concurrent sessions that the kill test found is fixed, that
  field is not.

## Proof

`awdk/tests/test_turn_resume_kill.py` starts `python -m adk run` on a temporary
home, puts two sessions mid-tool, kills the process hard, restarts it and checks
the effects file, the journal and the reconnecting client. As a pytest test the
model is a deterministic OpenAI-compatible stub; run the file directly for a
transcript, with `--real` for a local model:

```
python awdk/tests/test_turn_resume_kill.py
python awdk/tests/test_turn_resume_kill.py --real --base-url https://127.0.0.1:8150/v1 --model bonsai2-27b-5090
```

Real model, 2026-11-12 (`bonsai2-27b-5090`, the kit's `/chat` route):

```
effects before kill: ['record_receipt ran', 'record_receipt ran', 'slow_lookup start', 'slow_job start']
KILLED pid 55976 (exit 1)
clients at kill: job -> 0 bytes (ConnectionResetError ...), lookup -> 0 bytes
restarted; /health .turns.startup_resume = {'resumed': 2, 'abandoned': 0, 'waiting': 0, 'skipped': 0, 'pruned': 0, 'tasks': 2}
reconnect POST /chat "kill-job" -> 460 bytes: 'The receipt was recorded (R-1) and the slow job returned "outcome_unknown" since it was started before a process kill and was not re-run.'
GET /sessions/kill-job/turns -> outcome=resumed attempts=1 steps=[('record_receipt', 'finished'), ('slow_job', 'started')] notes=[('tool_replayed', 'record_receipt'), ('tool_unknown', 'slow_job')]
GET /sessions/kill-lookup/turns -> outcome=resumed attempts=1 steps=[('record_receipt', 'finished'), ('slow_lookup', 'finished')] notes=[('tool_replayed', 'record_receipt')]
effects after resume: ['record_receipt ran', 'record_receipt ran', 'slow_lookup start', 'slow_job start', 'slow_lookup start', 'slow_lookup end']
GET /health .turns = {"open": 0, "resumed": 2, "abandoned": 0, "failed": 0, "replayed_steps": 2, "unknown_steps": 1, "rerun_steps": 1}
RESULT: PASS
```

The finished step (`record_receipt`) ran once per session and was replayed from
the journal; the in-flight `slow_job` was not run again and the model reported
its outcome as unknown; the in-flight `slow_lookup` (`repeat_safe`) ran again.
The stub run asserts the same, exactly.

awsh, the same day, against a harness daemon and a kit started from the checkout
on free ports and a temporary home (`awdk/tests/live_awsh_durable_e2e.py`, real
model, the streaming route):

```
turn 1 answer: ' C:\...\adk-awsh-e2e-_wnv_sg_\work'                     CWD HONOURED: True
session state while the turn streams: busy
KILLED pid 38876 (exit 1)
event notice: 'the awdk loop at http://127.0.0.1:55729 dropped the connection mid-turn (ReadError); waiting up to 240s ...'
event turn.completed: 'The in-flight command from the restarted turn had already been run (its effect is visible: `started.txt` exists), and I did not re-issue it; ...' {"is_error": false, "resumed_after_kill": true, "turn_outcome": "resumed", "attempts": 1}
started.txt lines after resume: 1 (1 = the shell step was not re-run)
POST /sessions/c98f6c7ab64441c3/interrupt -> 200 {'interrupted': True}    state after interrupt: idle
kit turn outcomes: [('abandoned', 'cancelled before it finished')]
awsh_resumable(harness=awdk) -> count=3 sessions=[...]
POST /sessions resume_session_id=e0073d17a0694ae6 -> 200 new id 753efce333c94130
resumed session answered: 'C:\...\adk-awsh-e2e-_wnv_sg_\work'           RESUMED SESSION REMEMBERS: True
```
