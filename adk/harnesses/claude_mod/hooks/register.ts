import type { EngineInterface, On, TurnStepChunk, TurnStepResult, TurnUsage } from 'claude-code'

/*
 * awsh for Claude Code: the `aw` subagent type.
 *
 * The step-replacement shape (a real subagent loop whose model request is
 * answered from elsewhere, chained through a no-op tool across the engine's
 * hook budget, signed with the model that actually answered) is adapted from
 * pi-agent-for-claude by Fazal Ali, MIT. This module talks HTTP to the awsh
 * harness daemon instead of shelling out to a CLI, so it needs no sh, no /tmp
 * and no tmux, and runs wherever the daemon does.
 */

/** How often a running session's events are read for new ones. */
const STREAM_POLL_MS = 150

/**
 * How long one step streams before handing off to the next. The engine gives a
 * hook call 10 s and, past it, finishes the step with the real model; the
 * daemon's session runs on its own, so a longer turn spans several steps,
 * chained through PROGRESS_TOOL.
 */
const STEP_BUDGET_MS = 7_000

/**
 * The no-op tool a step ends on while the harness is still working: its call
 * makes the engine run another step, which carries on streaming the same turn.
 */
const PROGRESS_TOOL = 'aw_progress'

/** How long a follow-up step waits for its message to reach the transcript. */
const FOLLOW_UP_WAIT_MS = 5_000
const POLL_MS = 200

/** How long a new session may take to leave `starting` before it is a failure. */
const START_WAIT_MS = 6_000

/**
 * The tool a subagent delivers its final report through, where the session
 * runs the hand-back contract. A report left as plain text is bounced back
 * with `[handback-send-enforce]`.
 */
const HANDBACK_TOOL = 'SubagentHandback'

/**
 * User messages the engine writes into a subagent's transcript itself. They
 * are not the person's follow-ups, so the harness is never sent them.
 */
const ENGINE_PREFIXES = [
  '<system-reminder>',
  '[handback-send-enforce]',
  '[Your previous response had no visible output',
]

/** The harness a spawn gets when neither its prompt nor mods.json names one. */
const FALLBACK_HARNESS = 'opencode'

/**
 * How deep a chain of mod-started Claude Code sessions may get. The daemon's
 * `adk/harnesses/mod.py` holds the same number.
 */
const MAX_DEPTH = 2

/** How many notice/raw lines a run keeps to explain a failed turn with. */
const NOISE_LINES = 6

/** Session states in which the daemon's harness is still producing a turn. */
const WORKING_STATES = ['starting', 'busy']

/** Where the daemon is and how to talk to it. */
export type Daemon = { base: string; token: string; home: string }

/** What a spawn asked to be run on. Every field is optional. */
export type Route = { harness?: string; backend?: string; model?: string; agent?: string }

/** One turn on a daemon session, as it streams across the steps it spans. */
export type Run = {
  /** The daemon session answering this agent. */
  session: string
  /** The harness that session runs. */
  harness: string
  /** This run's number for the agent, which keeps tool call ids unique. */
  id: number
  /** The last event `seq` read. */
  since: number
  /** How many reads in a row came back empty with the session not working. */
  quiet: number
  /** Every piece of text shown so far, across steps. */
  shown: string
  /** The text of the turn's answer. */
  report: string
  /** Whether the turn has ended. */
  ended: boolean
  /** Whether it ended on a failure this module reports, not on an answer. */
  failed: boolean
  /** Whether the answer must be handed back through HANDBACK_TOOL. */
  handback: boolean
  /** How many steps have handed off to the next through PROGRESS_TOOL. */
  steps: number
  /** The model the daemon reported for this session. */
  model: string
  /** The last few notice/raw lines: a failed turn's only account of itself. */
  noise: string[]
  /** Tokens used since the last step reported them. */
  usage: { i: number; o: number; cr: number; cw: number }
}

/**
 * Registers the `aw` subagent type.
 *
 * The spawn runs as it always does, so the engine starts a real subagent loop
 * with its own id, transcript and entry in `$.agent.list()`; only the model
 * request inside that loop is replaced, by a turn on an awsh daemon session.
 * Each loop keeps one daemon session, so a follow-up continues the same
 * conversation, and the session is visible and steerable from awsh the whole
 * time (its owner is `claude-code:<agent id>`).
 *
 * @param on the engine's registrar
 */
export function register(on: On) {
  const seeds = new Map<string, string>()
  const consumed = new Map<string, number>()
  // Loops whose last step delivered the answer through a tool call, with the
  // text the step after it ends on.
  const delivered = new Map<string, string>()
  // PROGRESS_TOOL's full name as the engine registered it, `mcp__<plugin>__…`.
  let progressTool = ''
  const lastAnswers = new Map<string, string>()
  const runs = new Map<string, Run>()
  // The daemon session each loop talks to, and the route it was opened on.
  const sessions = new Map<string, { id: string; harness: string }>()

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    try {
      ;({ tool: progressTool } = await $.tool.register({
        name: PROGRESS_TOOL,
        description: 'Internal to aw subagents: marks an awsh turn still in progress. Never call it.',
      }))
    } catch {
      // The plugin shares its name with an `awsh` MCP server in the session's
      // config, and the engine refuses to replace that server. Leave the
      // session alone; aw subagents report the collision instead of chaining.
      progressTool = ''
    }
    return started
  })

  on('tool.call', async ($, e, next) => {
    if (e.tool !== progressTool) return next(e)
    if (e.agentId === undefined || !runs.has(e.agentId)) {
      return { deny: `${PROGRESS_TOOL} is internal to aw subagents.` }
    }
    return { result: 'the awsh harness is still working.' }
  })

  on('agent.spawn', async ($, e, next) => {
    const started = await next(e)
    if (isAw(e.subagentType) && started.agentId) {
      seeds.set(started.agentId, e.prompt)
    }
    return started
  })

  on('session.end', async ($, e, next) => {
    // A daemon session must not outlive the Claude Code session that opened it.
    const daemon = await daemonOf($).catch(() => undefined)
    if (daemon !== undefined) {
      for (const { id } of sessions.values()) {
        await call($, daemon, 'DELETE', `/sessions/${id}`).catch(() => undefined)
      }
    }
    sessions.clear()
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    // An aw subagent's loop, keyed by its id; any other loop is the model's.
    const agentId = e.agentId
    const seed = agentId === undefined ? undefined : seeds.get(agentId)
    if (agentId === undefined || seed === undefined) {
      return yield* next(e)
    }
    const deadline = (await $.clock.now()) + STEP_BUDGET_MS

    // The step after a delivery is that tool's result coming back: the answer
    // is delivered, so the loop ends here without asking the harness again.
    const done = delivered.get(agentId)
    if (done !== undefined) {
      delivered.delete(agentId)
      return yield* respond(e, [
        { kind: 'text', index: 0, text: done },
        { kind: 'stop', stopReason: 'end_turn', usage: null },
      ], done, [])
    }

    let run = runs.get(agentId)
    if (run === undefined) {
      const already = consumed.get(agentId) ?? 0
      const { prompt, count, handback, bounced } = await promptOf($, agentId, seed, already)
      consumed.set(agentId, count)
      if (bounced || prompt === undefined) {
        // A bounce is the engine refusing a plain-text report: hand back the
        // answer already given rather than asking again.
        const text = bounced
          ? lastAnswers.get(agentId) ?? ''
          : "awsh: no new message reached this agent's transcript."
        return yield* finish(e, agentId, text, text, 1, handback || bounced, [{ kind: 'text', index: 0, text }])
      }
      const started = await start($, agentId, prompt, count, handback, sessions.get(agentId))
      if ('failure' in started) {
        // Never throw: a throw hands the step to the model beneath, a Claude
        // subagent with Claude's tools, and that answer would read as awsh's.
        const text = started.failure
        return yield* finish(e, agentId, text, text, 1, handback, [{ kind: 'text', index: 0, text }])
      }
      run = started.run
      sessions.set(agentId, { id: run.session, harness: run.harness })
      runs.set(agentId, run)
    }

    const { shown, blocks, lastKind } = yield* pump($, run, deadline, next.signal)
    if (!run.ended && progressTool === '') {
      runs.delete(agentId)
      const text = shown + '\n\nawsh: turn outran one step and cannot chain: an MCP server named '
        + '"awsh" in this session\'s config blocked registering aw_progress.'
      return yield* finish(e, agentId, text, text, 1, run.handback, [{ kind: 'text', index: 0, text }])
    }
    if (!run.ended) {
      const input = {}
      return yield* respond(e, [
        { kind: 'tool', index: blocks, id: `toolu_aw_${idOf(agentId)}_${run.id}_${run.steps++}`, name: progressTool },
        { kind: 'input', index: blocks, json: JSON.stringify(input) },
        { kind: 'stop', stopReason: 'tool_use', usage: takeUsage(run) },
      ], shown, [{ name: progressTool, input }])
    }

    runs.delete(agentId)
    // The hand-back reminder can reach the transcript after the run's first
    // step read it; checking again now saves a bounce.
    const handback = run.handback || (await transcriptOf($, agentId)).handback
    const answer = run.report.trim() || run.shown
    // Named from the daemon's own record of the session: a model asked what it
    // is often answers wrongly, and the parent has nothing else to check it by.
    // A failure is this module's account, not a model's answer: unsigned, so
    // the parent's rule ("no signature, not awsh's work") holds for it too.
    const report = run.failed
      ? answer
      : `${answer}\n\n— answered by awsh, ${run.harness}/${run.model || 'unreported'}`
    // The parent reads the last text block, so it must hold the whole answer.
    // A run spanning several steps streamed its answer across them and gets it
    // again whole; one whose answer streamed whole here gets just the
    // signature, onto that same block.
    const whole = run.steps > 0 && !shown.includes(answer)
    const extra = whole ? report : report.slice(answer.length)
    const into = !whole && lastKind === 'text' ? blocks - 1 : blocks
    const tail: TurnStepChunk[] = extra ? [{ kind: 'text', index: Math.max(into, 0), text: extra }] : []
    return yield* finish(e, agentId, shown + extra, report, Math.max(into, 0) + (extra ? 1 : 0), handback, tail, takeUsage(run))
  })

  /**
   * Ends a step on the answer: plain text, or text then the HANDBACK_TOOL call
   * that delivers it where the session runs the hand-back contract.
   */
  async function* finish(
    e: { turnId: string; index: number },
    agentId: string,
    shown: string,
    report: string,
    blocks: number,
    handback: boolean,
    chunks: TurnStepChunk[],
    usage: TurnUsage | null = null,
  ) {
    lastAnswers.set(agentId, report)
    if (!handback) {
      return yield* respond(e, [...chunks, { kind: 'stop', stopReason: 'end_turn', usage }], shown, [])
    }
    delivered.set(agentId, 'Report delivered.')
    const input = { message: report }
    const id = `toolu_aw_${idOf(agentId)}_${HANDBACK_TOOL}_${consumed.get(agentId) ?? 0}`
    return yield* respond(e, [
      ...chunks,
      { kind: 'tool', index: blocks, id, name: HANDBACK_TOOL },
      { kind: 'input', index: blocks, json: JSON.stringify(input) },
      { kind: 'stop', stopReason: 'tool_use', usage },
    ], shown, [{ name: HANDBACK_TOOL, input }])
  }
}

/** An agent id as a tool-call id may spell it. */
export function idOf(agentId: string) {
  return agentId.replace(/[^A-Za-z0-9_]/g, '_')
}

/**
 * The tokens a run used since the last step reported them, as this step's
 * usage under the model the daemon named; null before it named one.
 */
export function takeUsage(run: Run | undefined): TurnUsage | null {
  if (!run?.model) return null
  const { i, o, cr, cw } = run.usage
  run.usage = { i: 0, o: 0, cr: 0, cw: 0 }
  return {
    model: run.model,
    input_tokens: i,
    output_tokens: o,
    cache_read_input_tokens: cr,
    cache_creation_input_tokens: cw,
  }
}

/** Whether a spawn names this plugin's type, plain or plugin-qualified. */
export function isAw(subagentType: string) {
  return subagentType === 'aw' || subagentType.endsWith(':aw')
}

/**
 * Takes the `aw-harness:` / `aw-backend:` / `aw-model:` / `aw-agent:` lines out
 * of a prompt: the way a caller routes a spawn, since the Agent tool's own
 * `model` names Claude models only. Only the first line of each kind counts.
 */
export function routeOf(text: string): { prompt: string; route: Route } {
  const route: Route = {}
  let prompt = text
  const keys = [['harness', 'harness'], ['backend', 'backend'], ['model', 'model'], ['agent', 'agent']] as const
  for (const [word, field] of keys) {
    const line = prompt.match(new RegExp(`^[ \\t]*aw-${word}:[ \\t]*([A-Za-z0-9_.:\\/@+-]+)[ \\t]*$`, 'im'))
    if (!line) continue
    route[field] = line[1]
    prompt = prompt.replace(line[0], '')
  }
  return { prompt: prompt.trim(), route }
}

/**
 * Yields a step's chunks and returns the result the engine expects of it.
 *
 * @param e the step being answered
 * @param chunks what the person watches stream, in order
 * @param answer the step's visible text
 * @param toolUses the tool calls the chunks made
 */
export async function* respond(
  e: { turnId: string; index: number },
  chunks: TurnStepChunk[],
  answer: string,
  toolUses: TurnStepResult['toolUses'],
): AsyncGenerator<TurnStepChunk, TurnStepResult> {
  for (const chunk of chunks) yield chunk
  const stop = chunks.at(-1)
  const stopReason = stop?.kind === 'stop' ? stop.stopReason : 'end_turn'
  const usage = stop?.kind === 'stop' ? stop.usage : null
  return { turnId: e.turnId, index: e.index, answer, toolUses, stopReason, usage }
}

/** Reads a text file, or '' when it is absent or unreadable. */
export async function readText($: EngineInterface, path: string): Promise<string> {
  try {
    const got: unknown = await $.fs.read(path)
    if (typeof got === 'string') return got
    const text = (got as { text?: unknown } | undefined)?.text
    return typeof text === 'string' ? text : ''
  } catch {
    return ''
  }
}

/**
 * Finds the daemon: `AITHER_HARNESS_HOST`/`PORT` and the bearer file
 * `AITHER_HARNESS_TOKEN_FILE`, the same three names every other daemon client
 * reads, defaulting to 127.0.0.1:8362 and ~/.aither/harness_token. The token is
 * read on every call rather than cached, since the daemon rewrites it.
 */
export async function daemonOf($: EngineInterface): Promise<Daemon> {
  const home = ((await $.env.get('USERPROFILE')) || (await $.env.get('HOME')) || '').replace(/\\/g, '/')
  const host = (await $.env.get('AITHER_HARNESS_HOST')) || '127.0.0.1'
  const port = (await $.env.get('AITHER_HARNESS_PORT')) || '8362'
  const file = (await $.env.get('AITHER_HARNESS_TOKEN_FILE')) || `${home}/.aither/harness_token`
  const token = (await readText($, file)).trim()
  return { base: `http://${host}:${port}`, token, home }
}

/**
 * One call to the daemon. Never throws: a failure comes back as status 0 with
 * the reason, so the caller can end the step on it as text.
 */
export async function call(
  $: EngineInterface,
  daemon: Daemon,
  method: string,
  path: string,
  body?: unknown,
): Promise<{ status: number; json: any; detail: string }> {
  try {
    const got = await $.http.fetch(`${daemon.base}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${daemon.token}`,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    let json: any
    try {
      json = JSON.parse(got.text)
    } catch {
      json = undefined
    }
    const detail = typeof json?.detail === 'string' ? json.detail : got.ok ? '' : got.text.slice(0, 400)
    return { status: got.status, json, detail }
  } catch (err) {
    return { status: 0, json: undefined, detail: String(err) }
  }
}

/**
 * The spawn defaults from `~/.aither/mods.json` (`{ "aw": { "harness": … } }`),
 * the file `awsettings --domain mods` syncs. Absent or malformed, no defaults.
 */
export async function defaultsOf($: EngineInterface, daemon: Daemon): Promise<Route & { permission_mode?: string }> {
  try {
    const aw = JSON.parse(await readText($, `${daemon.home}/.aither/mods.json`))?.aw
    return aw !== null && typeof aw === 'object' ? aw : {}
  } catch {
    return {}
  }
}

/**
 * Opens (or reuses) the agent's daemon session and submits one turn to it.
 *
 * @param prompt what to send the harness, routing lines still in it
 * @param id this run's number for the agent
 * @param handback whether the answer must be handed back
 * @param open the session a previous turn of this agent opened, if any
 */
export async function start(
  $: EngineInterface,
  agentId: string,
  prompt: string,
  id: number,
  handback: boolean,
  open: { id: string; harness: string } | undefined,
): Promise<{ run: Run } | { failure: string }> {
  const daemon = await daemonOf($)
  if (!daemon.token) {
    return { failure: 'awsh: no harness token found, so the daemon cannot be reached. Start it once (`adk harness serve`) - it writes ~/.aither/harness_token.' }
  }
  // A Claude Code the mod started can start the mod again; the daemon tells
  // each one how deep it is, and the chain stops here rather than at a bill.
  const depth = Number((await $.env.get('AITHER_AW_DEPTH')) || 0)
  if (depth >= MAX_DEPTH) {
    return { failure: `awsh: refused - this Claude Code session is already ${depth} aw spawns deep (the limit is ${MAX_DEPTH}). Do the work here instead of delegating it again.` }
  }
  const asked = routeOf(prompt)
  let session = open
  // A follow-up that names another harness gets a new session; one that names
  // none continues the conversation it is part of.
  if (session !== undefined && asked.route.harness !== undefined && asked.route.harness !== session.harness) {
    await call($, daemon, 'DELETE', `/sessions/${session.id}`)
    session = undefined
  }
  if (session === undefined) {
    const defaults = await defaultsOf($, daemon)
    const harness = asked.route.harness ?? defaults.harness ?? FALLBACK_HARNESS
    const made = await call($, daemon, 'POST', '/sessions', {
      harness,
      cwd: await $.session.cwd(),
      model_profile: asked.route.backend ?? defaults.backend ?? '',
      model: asked.route.model ?? defaults.model ?? '',
      target: asked.route.agent ?? defaults.agent ?? '',
      permission_mode: defaults.permission_mode ?? '',
      title: `aw ${harness} (Claude Code subagent)`,
      // The depth rides in the owner because that is the one free-text field a
      // session create already carries to the child's environment.
      owner: `claude-code:${agentId}@d${depth + 1}`,
    })
    if (made.status !== 200 || typeof made.json?.id !== 'string') {
      const why = made.status === 0 ? `the daemon at ${daemon.base} did not answer (${made.detail})` : `${made.status} ${made.detail}`
      return { failure: `awsh: could not open a ${harness} session: ${why}` }
    }
    session = { id: made.json.id, harness }
    // A harness that cannot start says so within moments; submitting into a
    // session still `starting` is refused with a 409 that names nothing.
    for (let waited = 0; waited < START_WAIT_MS; waited += POLL_MS) {
      const info = await call($, daemon, 'GET', `/sessions/${session.id}`)
      if (info.json?.state !== 'starting') break
      await $.clock.sleep(POLL_MS)
    }
  }
  const before = await call($, daemon, 'GET', `/sessions/${session.id}/events?since=999999999`)
  const sent = await call($, daemon, 'POST', `/sessions/${session.id}/submit`, { text: asked.prompt, submit: true })
  if (sent.status !== 200) {
    const events = await call($, daemon, 'GET', `/sessions/${session.id}/events?since=0`)
    const errors = (events.json?.events ?? [])
      .filter((ev: any) => ev.kind === 'error')
      .map((ev: any) => ev.text)
      .join('\n')
    return { failure: `awsh: the ${session.harness} session refused the turn: ${sent.status} ${sent.detail}\n${errors}`.trim() }
  }
  return {
    run: {
      session: session.id,
      harness: session.harness,
      id,
      since: Number(before.json?.last_seq ?? 0),
      quiet: 0,
      shown: '',
      report: '',
      ended: false,
      failed: false,
      handback,
      steps: 0,
      model: '',
      noise: [],
      usage: { i: 0, o: 0, cr: 0, cw: 0 },
    },
  }
}

/**
 * Streams a run's new events as this step's chunks, until the turn ends or the
 * step's `deadline` passes: text as text, thinking as thinking, and each tool
 * the harness starts as a one-line `▸ tool: args` note in the text.
 *
 * Never throws: see `start`. An aborted step interrupts the harness, since the
 * daemon's session would otherwise carry on with a turn nobody is reading.
 *
 * @param deadline when this step must stop streaming, in `$.clock` ms
 * @param signal the step's abort signal
 * @returns the text this step showed, how many content blocks it used, and
 *   the kind of the last one
 */
export async function* pump(
  $: EngineInterface,
  run: Run,
  deadline: number,
  signal: AbortSignal,
): AsyncGenerator<TurnStepChunk, { shown: string; blocks: number; lastKind?: 'text' | 'thinking' }> {
  let shown = ''
  let index = -1
  let kind: 'text' | 'thinking' | undefined
  let failure = ''
  const daemon = await daemonOf($)

  try {
    while (!run.ended && !signal.aborted && (await $.clock.now()) < deadline) {
      const got = await call($, daemon, 'GET', `/sessions/${run.session}/events?since=${run.since}`)
      if (got.status !== 200) {
        failure = `awsh: lost the ${run.harness} session: ${got.status} ${got.detail}`
        break
      }
      const events: any[] = got.json?.events ?? []
      for (const event of events) {
        run.since = Math.max(run.since, Number(event.seq ?? 0))
        let pieceKind: 'text' | 'thinking' = 'text'
        let text = ''
        if (event.kind === 'text.delta') {
          text = event.text ?? ''
          run.report += text
        } else if (event.kind === 'thinking.delta') {
          pieceKind = 'thinking'
          text = event.text ?? ''
        } else if (event.kind === 'tool.call') {
          const args = JSON.stringify(event.data?.input ?? {})
          text = `\n▸ ${event.tool}: ${args.length > 100 ? `${args.slice(0, 100)}…` : args}\n`
        } else if (event.kind === 'error') {
          text = `\n[${run.harness} error] ${event.text ?? ''}\n`
          run.report += text
        } else if (event.kind === 'usage') {
          const u = event.data?.usage ?? {}
          run.usage.i += Number(u.input_tokens ?? u.input ?? 0)
          run.usage.o += Number(u.output_tokens ?? u.output ?? 0)
          run.usage.cr += Number(u.cache_read_input_tokens ?? 0)
          run.usage.cw += Number(u.cache_creation_input_tokens ?? 0)
        } else if (event.kind === 'notice' || event.kind === 'raw') {
          // Never shown as they arrive (a CLI's stderr is mostly noise), but a
          // failed turn's reason is only ever here, and so is the model name
          // of a harness the daemon cannot ask (`> build · provider/model`).
          const line = String(event.text ?? '').replace(/\u001b\[[0-9;]*m/g, '').trim()
          const named = line.match(/^>\s*\S+\s+·\s+(\S+)$/)
          if (named) run.model = named[1]
          else if (line) run.noise = [...run.noise, line].slice(-NOISE_LINES)
        } else if (event.kind === 'turn.completed') {
          // A harness that streams nothing still carries its answer here.
          if (!run.report.trim() && event.text) {
            text = event.text
            run.report = text
          } else if (event.data?.is_error && !run.report.trim()) {
            failure = `awsh: the ${run.harness} turn failed (exit ${event.data?.exit_code ?? '?'}):\n${[...new Set(run.noise)].join('\n')}`
          }
          run.ended = true
        } else if (event.kind === 'session.exited') {
          run.ended = true
        }
        if (!text) continue
        if (pieceKind !== kind) {
          kind = pieceKind
          index += 1
        }
        if (pieceKind === 'text') shown += text
        yield { kind: pieceKind, index, text }
      }
      // A session that stopped working without a closing event still ended.
      const working = WORKING_STATES.includes(got.json?.state)
      run.quiet = events.length === 0 && !working ? run.quiet + 1 : 0
      if (run.quiet >= 3) run.ended = true
      if (!run.ended) await $.clock.sleep(STREAM_POLL_MS)
    }
  } catch (err) {
    failure = `awsh: the ${run.harness} run failed: ${String(err)}`
  }

  if (signal.aborted && !run.ended) {
    run.ended = true
    await call($, daemon, 'POST', `/sessions/${run.session}/interrupt`)
  }
  if (run.ended && !failure && !(run.shown + shown).trim()) {
    failure = `awsh: the ${run.harness} session ended with no answer.`
  }
  if (failure) {
    run.ended = true
    run.failed = true
    shown += failure
    run.report = failure
    index += 1
    kind = 'text'
    yield { kind: 'text', index, text: failure }
  }
  if (run.ended) {
    const info = await call($, daemon, 'GET', `/sessions/${run.session}`)
    const binding = info.json?.model_binding
    run.model = info.json?.reported_model || binding?.model || binding?.profile || run.model
  }
  run.shown += shown
  return { shown, blocks: index + 1, lastKind: kind }
}

/**
 * Decides what to send the harness this step, and whether its answer must be
 * handed back through HANDBACK_TOOL.
 *
 * The first step sends the spawn's prompt. A later one waits for a person's
 * message past the first `already` in `agentId`'s own transcript and sends the
 * newest; the engine can start the step before it writes the message there,
 * so this polls briefly.
 *
 * @param seed the prompt the spawn was given
 * @param already how many of the person's messages the harness has been sent
 */
export async function promptOf($: EngineInterface, agentId: string, seed: string, already: number) {
  for (let waited = 0; ; waited += POLL_MS) {
    const { texts, handback, bounced } = await transcriptOf($, agentId)
    if (already === 0) return { prompt: seed, count: Math.max(texts.length, 1), handback, bounced: false }
    if (texts.length > already) return { prompt: texts.at(-1), count: texts.length, handback, bounced: false }
    if (bounced) return { prompt: undefined, count: already, handback: true, bounced }
    if (waited >= FOLLOW_UP_WAIT_MS) return { prompt: undefined, count: already, handback, bounced }
    await $.clock.sleep(POLL_MS)
  }
}

/**
 * Where Claude Code keeps `agentId`'s transcript:
 * `~/.claude/projects/<project>/<session id>/subagents/agent-<id>.jsonl`.
 *
 * Leans on Claude Code's on-disk layout, not an API: `$.session.messages()`
 * reads the main loop's transcript only. The project directory is the launch
 * directory with every non-alphanumeric turned into `-`; when that guess
 * misses, every project directory is tried for this session's id.
 */
export async function transcriptPathOf($: EngineInterface, agentId: string): Promise<string | undefined> {
  const { home } = await daemonOf($)
  const projects = `${home}/.claude/projects`
  const sessionId = await $.session.id()
  const tail = `${sessionId}/subagents/agent-${agentId}.jsonl`
  const guess = `${projects}/${(await $.session.cwd()).replace(/[^A-Za-z0-9]/g, '-')}/${tail}`
  if (await $.fs.exists(guess).catch(() => false)) return guess
  const listed: unknown = await $.fs.list(projects).catch(() => [])
  for (const entry of Array.isArray(listed) ? listed : []) {
    const name = typeof entry === 'string' ? entry : (entry as { name?: string })?.name
    if (!name) continue
    const path = `${projects}/${name}/${tail}`
    if (await $.fs.exists(path).catch(() => false)) return path
  }
  return undefined
}

/**
 * Reads `agentId`'s transcript: the text of every message the person (or the
 * spawn) sent it, oldest first; whether the engine asked it to hand its
 * report back through HANDBACK_TOOL; and whether the newest message is the
 * engine bouncing a report that was not.
 */
export async function transcriptOf($: EngineInterface, agentId: string) {
  const path = await transcriptPathOf($, agentId)
  const raw = path === undefined ? '' : await readText($, path)
  const all = raw
    .split('\n')
    .flatMap((line) => {
      try {
        return [JSON.parse(line)]
      } catch {
        return [] // the line Claude Code is still writing
      }
    })
    .filter((entry) => entry.type === 'user')
    .map((entry) => textOf(entry.message?.content))
    .filter(Boolean)
  return {
    texts: all.filter((text) => !ENGINE_PREFIXES.some((prefix) => text.startsWith(prefix))),
    handback: all.some((text) => text.includes(HANDBACK_TOOL)),
    bounced: all.at(-1)?.startsWith('[handback-send-enforce]') ?? false,
  }
}

/** Joins a message's text: a plain string, or the text blocks of a block list. */
export function textOf(content: unknown): string {
  if (typeof content === 'string') return content.trim()
  if (!Array.isArray(content)) return ''
  return content
    .filter((block) => block?.type === 'text')
    .map((block) => block.text)
    .join('\n')
    .trim()
}
