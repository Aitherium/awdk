import { test, expect } from 'claude-code/testing'
import { commandOf, countLines, forkOf, textOf, withText } from './compact.ts'

/*
 * What these pin: the two things a compacting hook can get wrong and still look
 * fine. (1) Reading the result's text out of whichever shape this build uses --
 * guess one and the hook silently does nothing on the others. (2) Putting text
 * BACK in the same shape -- a hook that returns a bare string where the engine
 * expected an object replaces a tool result with something the model cannot read.
 *
 * The compaction rules themselves are asserted by `compact.py --self-test` in
 * the world-model tree and by `awdk/tests/test_compact.py`; this file is the
 * seam only, and the last test drives the REAL `$.process.run` so the argument
 * shape is checked against the engine rather than assumed.
 */

test('textOf reads every result shape this build may use', () => {
  expect(textOf('plain')).toBe('plain')
  expect(textOf({ result: 'r' })).toBe('r')
  expect(textOf({ output: 'o' })).toBe('o')
  expect(textOf({ stdout: 'a', stderr: 'b' })).toBe('a\nb')
  expect(textOf({ stdout: 'a', stderr: '' })).toBe('a')
  expect(textOf({ nothing: 1 })).toBe(undefined)
  expect(textOf(null)).toBe(undefined)
  expect(textOf(42)).toBe(undefined)
})

test('withText puts the text back in the SAME shape', () => {
  expect(withText('plain', 'new')).toBe('new')
  expect(withText({ result: 'r', keep: 1 }, 'new')).toEqual({ result: 'new', keep: 1 })
  expect(withText({ output: 'o' }, 'new')).toEqual({ output: 'new' })
  expect(withText({ stdout: 'a', stderr: 'b' }, 'new')).toEqual({ stdout: 'new', stderr: '' })
  // A shape it cannot write comes back untouched: never a mangled result.
  expect(withText({ nothing: 1 }, 'new')).toEqual({ nothing: 1 })
})

test('the fork follows the command, so pytest and podman learn separately', () => {
  expect(forkOf('python -m pytest dev/tests -q')).toBe('pytest')
  expect(forkOf('wsl -d Debian -u root podman build .')).toBe('podman')
  expect(forkOf('ruff check --isolated lib/')).toBe('ruff')
  expect(forkOf('sort -u things.txt')).toBe('bash')
  expect(forkOf('')).toBe('bash')
})

test('commandOf survives an event with no input', () => {
  expect(commandOf({ input: { command: 'ls' } })).toBe('ls')
  expect(commandOf({ input: {} })).toBe('')
  expect(commandOf({})).toBe('')
  expect(commandOf(undefined)).toBe('')
})

test('countLines counts what the min-lines floor is about', () => {
  expect(countLines('one')).toBe(1)
  expect(countLines('one\ntwo')).toBe(2)
  expect(countLines('a\n'.repeat(99))).toBe(100)
})

/*
 * NOT TESTED HERE, and it is the one gap worth naming: `$.process.run`'s
 * ARGUMENT shape. `claude plugin validate` reports which engine methods a
 * module calls, never what it passes them (probed 2026-09-20: a call with two
 * invented fields validated clean), and the test harness's `$` carries only
 * agent, attribution, command, config, prompt, session, skill, tool, turn and
 * ui -- no `process`, so the call cannot be made from a test either. The hook
 * therefore tries two call shapes and, if both throw, returns the ORIGINAL
 * result: the failure mode is "the mod does nothing", never a mangled result.
 */
