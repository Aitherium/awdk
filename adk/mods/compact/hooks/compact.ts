import type { On } from 'claude-code'

/*
 * awcompact: a long tool result reaches the model as the lines that decide what
 * happens next.
 *
 * This is the half the PostToolUse shell hook cannot do. A PostToolUse hook may
 * only ADD context, so the raw 1,000-line pytest run still reaches the model and
 * nothing is saved. A `tool.call` function hook sits AROUND the call: it awaits
 * `next(e)`, and what it returns IS the result the model reads. So the compacted
 * form REPLACES the output here, and the saving is real.
 *
 * What decides: the AitherOS decision door, asked per distinct LINE SHAPE
 * (`kind:pytest-pass|len:<80|rep:20+|pos:middle`), never per line and never
 * about the line's text -- so a secret in an env dump cannot leave the process
 * through the model rung, and the second run of the same kind of command is
 * answered from evidence in microseconds with no model call at all. A shape the
 * door has nothing to say about is KEPT: dropping a line on no evidence is the
 * one failure this may not have, and the always-keep rules (tracebacks, summary
 * lines, error lines, the first two and last five lines) never reach the door.
 *
 * The classification and the door call live in `python -m adk.compact`, one
 * implementation for the shell hook, the awsh mod and this one. That is
 * deliberate: three copies of a shape grammar keyed on one fork name would each
 * teach the door a different descriptor for the same line.
 *
 * Install (both lines are the OWNER's -- an agent cannot edit settings here):
 *   claude --plugin-dir awdk/adk/mods/compact          # this session only
 *   CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1                # in the settings env block
 *
 * Config (`pluginConfigs.awcompact` in settings, each also a row in the config
 * menu): minLines, tools, mode, verbose, timeoutMs.
 */

/** Below this many lines a result is left alone: the compactor costs more than it saves. */
const DEFAULT_MIN_LINES = 60

/** Tools whose results are compacted. Others pass through untouched. */
const DEFAULT_TOOLS = ['Bash', 'BashOutput']

/** How long the compactor may take before the original result is used instead. */
const DEFAULT_TIMEOUT_MS = 20_000

/** Commands whose fork the output learns on, longest name first so `npm test` is npm. */
const FORKS = [
  'pytest', 'ruff', 'mypy', 'terraform', 'ansible', 'podman', 'docker', 'cargo',
  'yarn', 'npm', 'curl', 'make', 'pip', 'git', 'go',
]

type Options = {
  minLines?: number
  tools?: string
  mode?: string
  verbose?: boolean
  timeoutMs?: number
}

export function register(on: On, options: Options = {}) {
  const minLines = Number(options.minLines ?? DEFAULT_MIN_LINES)
  const tools = String(options.tools ?? DEFAULT_TOOLS.join(','))
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
  const mode = String(options.mode ?? 'auto')
  const verbose = Boolean(options.verbose ?? false)
  const timeoutMs = Number(options.timeoutMs ?? DEFAULT_TIMEOUT_MS)

  on('tool.call', async ($, e, next) => {
    const result = await next(e)
    if (!tools.includes(e.tool)) return result

    const text = textOf(result)
    if (text === undefined || countLines(text) < minLines) return result

    const fork = forkOf(commandOf(e))
    let compacted: Compacted | undefined
    try {
      compacted = await runCompactor($, text, fork, mode, minLines, timeoutMs)
    } catch (err) {
      // The compactor is away, slow or broken. The ORIGINAL result stands --
      // a compactor that eats a tool result when it fails is worse than none.
      await $.ui.log(`awcompact: ${String(err).slice(0, 200)}`)
      return result
    }
    if (compacted === undefined || compacted.kept_lines >= compacted.total_lines) return result

    const ids = (compacted.decision_ids ?? []).slice(0, 3).join(', ')
    const header =
      `[compacted by the decision door: kept ${compacted.kept_lines} of ` +
      `${compacted.total_lines} lines; fork decide.compact.${fork}; ` +
      `mode ${compacted.mode}` +
      (ids ? `; a dropped line mattered? adk compact teach ${ids.split(',')[0].trim()}` : '') +
      ']'
    if (verbose) {
      await $.ui.log(
        `awcompact ${fork}: ${compacted.kept_lines}/${compacted.total_lines} lines, ` +
          `sources ${JSON.stringify(compacted.source_counts ?? {})}`,
      )
    }
    return withText(result, `${header}\n${compacted.kept}`)
  })
}

type Compacted = {
  kept: string
  kept_lines: number
  total_lines: number
  dropped: number
  mode: string
  source_counts?: Record<string, number>
  decision_ids?: string[]
}

/** The tool result's text, whichever shape this build uses. undefined = leave it alone. */
export function textOf(result: unknown): string | undefined {
  if (typeof result === 'string') return result
  if (result === null || typeof result !== 'object') return undefined
  const r = result as Record<string, unknown>
  if (typeof r.result === 'string') return r.result
  if (typeof r.output === 'string') return r.output
  if (typeof r.stdout === 'string' || typeof r.stderr === 'string') {
    return [r.stdout, r.stderr].filter((s) => typeof s === 'string' && s.length > 0).join('\n')
  }
  return undefined
}

/** The same shape back with new text, so nothing else about the result changes. */
export function withText(result: unknown, text: string): unknown {
  if (typeof result === 'string') return text
  const r = result as Record<string, unknown>
  if (typeof r.result === 'string') return { ...r, result: text }
  if (typeof r.output === 'string') return { ...r, output: text }
  if (typeof r.stdout === 'string' || typeof r.stderr === 'string') {
    return { ...r, stdout: text, stderr: '' }
  }
  return result
}

export function commandOf(e: unknown): string {
  const input = (e as { input?: Record<string, unknown> })?.input
  const cmd = input?.command ?? input?.cmd ?? input?.script
  return typeof cmd === 'string' ? cmd : ''
}

/** Which fork this output learns on. pytest filler looks nothing like build filler. */
export function forkOf(command: string): string {
  const low = command.toLowerCase()
  for (const name of FORKS) {
    if (new RegExp(`(^|[\\s;&|/\\\\"'])${name}(\\s|$)`).test(low)) return name
  }
  return 'bash'
}

export function countLines(text: string): number {
  let n = 1
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) n++
  return n
}

/*
 * `$.process.run`'s ARGUMENT shape is the one thing here that could not be
 * verified on this box (2026-09-20): the plugin validator reports which engine
 * methods a module calls but not what it passes them, and the test harness's
 * `$` carries no `process` noun, so the call cannot be made from a test. Two
 * shapes are tried, and if both throw the caller keeps the ORIGINAL result --
 * the failure mode is "the mod does nothing", never a mangled tool result.
 */
async function runCompactor(
  $: { process: { run: (...a: any[]) => Promise<any> } },
  text: string,
  fork: string,
  mode: string,
  minLines: number,
  timeoutMs: number,
): Promise<Compacted | undefined> {
  const args = ['-m', 'adk.compact', '-', '--tool', fork, '--json', '--mode', mode,
                '--min-lines', String(minLines)]
  let got: any
  try {
    got = await $.process.run({ command: 'python', args, stdin: text, timeout: timeoutMs })
  } catch {
    got = await $.process.run('python', args, { stdin: text, timeout: timeoutMs })
  }
  const out = typeof got?.stdout === 'string' ? got.stdout : (typeof got === 'string' ? got : '')
  if (!out.trim()) return undefined
  const parsed = JSON.parse(out) as Compacted
  if (typeof parsed?.kept !== 'string') return undefined
  return parsed
}
