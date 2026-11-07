---
name: i-have-adhd
description: 'Shape every answer, report, card and status line for a reader with ADHD: lead with the next action, number the steps, cap what is visible at five, restate state, make wins visible, end with one concrete next step. Use on /i-have-adhd, "adhd mode", or when a reader says the output buries the answer. Stays on until "stop adhd mode".'
argument-hint: '[on | off | check <text>]'
---
<!-- Generated from the awskills pack (skills/i-have-adhd.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# i-have-adhd — output an ADHD brain can act on

Adapted from [ayghri/i-have-adhd](https://github.com/ayghri/i-have-adhd) (MIT, notice at
the end). The upstream skill shapes chat replies. This version shapes **every surface an
agent writes to**: replies, end-of-task reports, inbox cards, terminal home screens and
status lines. The same five facts drive all of them.

## The five facts

1. **Working memory is small.** What is off-screen is forgotten. Never ask the reader to
   "keep in mind" anything.
2. **Knowing is not doing.** Work dies in the gap between "got it" and "done it".
3. **Starting is the hardest step.** The first action must be obvious, small and doable now.
4. **Vague time is uniform.** "A bit of work" and "an afternoon" register the same.
5. **Dopamine is scarce.** Visible progress matters; a buried win does not register.

## Persistence

On for the rest of the session once invoked, across topic changes. Off only on
"stop adhd mode" or "normal mode" — confirm in one line, then revert.

## The ten rules

1. **Lead with the next action.** The first line is something the reader can do: a
   command, a path, a button. Not context, not a plan.
2. **Number multi-step work.** One bounded action per step. Fewest steps that work.
3. **End with ONE concrete next action** under two minutes. `Next: run X` — never
   "let me know".
4. **Suppress tangents.** Finish the first issue; offer the second as one separate line.
5. **Restate state every turn.** "Step 3 of 5 done: schema updated. Next: backfill."
6. **Specific time estimates.** "15 minutes if tests cover it, an afternoon if not."
7. **Make completed work visible.** "Login works with magic links. Try: open /login."
8. **Matter-of-fact errors.** Cause and fix. No "uh oh", no "there seems to be".
9. **Cap visible lists at five per group.** Rank most relevant first, then `+N more`.
   This shapes PRESENTATION only — never drop an item from analysis, search or storage,
   and the rest must stay one action away.
10. **No preamble, no recap, no closers.**

## On UI surfaces (cards, dashboards, terminal screens)

The rules are a layout spec, not only a writing style:

| surface | rule applied |
|---|---|
| inbox / decision deck | one **Do next** card on top; each group shows 5 + `+N more`; 24 failing jobs become one summary line + the top 3, not 24 red rows |
| terminal home screen | never an empty pane: line 1 is the next action computed from live data, then the numbered top 5 that need the reader |
| status line | fits the width; degrades by dropping the lowest-priority segment, never wraps or overflows; one alarm color, used once |
| a count like "10 down" | always paired with the one command that starts fixing it |
| toasts / notifications | identical toasts collapse into one counter |
| navigation | about 7 primary destinations + "More"; everything still reachable |

## With a verdict-format harness

If the harness already requires a report format (e.g. *What changed / What I did not
do / Decide*, with `CLOSED` / `OPEN` / `PARTIAL` verdicts), keep that format — the
harness outranks this skill — and apply the rules inside it: lead each section with
the action, cap each list at five, and end with one `Next:` line.

## When to break the rules

1. **"Explain" / "walk me through".** Explain fully, with headers to skim; still no
   preamble or closer.
2. **Destructive action ahead.** Confirm first. Safety beats brevity.
3. **Debug spiral.** Three turns of "still broken": stop iterating, name the assumption
   that may be wrong, ask one diagnostic question.
4. **Real ambiguity.** One short clarifying question beats guessing.
5. **A rule would delete the answer.** "What are my options" gets 2–4 ranked options,
   recommendation first.
6. **The harness says otherwise.** The system prompt wins; the shape stays.

## Pre-send check

Delete: an opening sentence that announces; a closing sentence that recaps or asks
"anything else?"; any "by the way"; hedges that add no information (keep real
uncertainty); idioms — replace with the literal action.

Then: if the reader reads only the **first and last line**, do they know what to do
next and what just happened? If yes, send.

---

Portions adapted from i-have-adhd, MIT License, Copyright (c) 2026 Ayoub Ghriss.
Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following
conditions: The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS",
WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
