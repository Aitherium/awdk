---
name: ramble-driven-development
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, PowerShell
description: Stop writing careful prompts. Ramble to load intent, poke to steer, and put the precision in the harness instead — the measured prompt-shape law behind high-throughput agentic coding.
argument-hint: [analyze | install | explain]

---

# Ramble-Driven Development

> **The law:** the careful, fully-specified prompt still has to exist.
> You just should not be the one typing it.

Prompt engineering is dead. It did not die because models got smart enough to read
your mind — it died because the careful prompt got **demoted to a build artifact**.
Something still writes 3,000 characters of specification before real work happens.
In a mature setup that something is your harness, not your hands.

This skill teaches the prompt-shape that falls out of that, and helps you build the
harness that makes it work.

---

## The measurement

Every Claude Code session transcript is on your disk in `~/.claude/projects/*/*.jsonl`.
Mined across a 34-day window of one heavy user: **5,244 human prompts, 9,715 machine-written
agent dispatches, 359 sessions.** Independently corroborated by a separate 210-day
measurement of the same operator (27,939 prompts) that landed on a median within
2 characters of this one.

### Human prompts are bimodal, and it is not close

| bucket | share of prompts |
|---|---|
| under 20 chars | **26.9%** |
| 20–80 chars | 31.6% |
| 80–200 chars | 18.0% |
| 200–1,000 chars | 17.5% |
| over 1,000 chars | **5.9%** |

Median human prompt: **56 characters.** 58.5% are under 80.

And yet — **that top 5.9% carries 78.4% of every character the human typed.**

Six percent of the prompts carry seventy-eight percent of the information. There is
almost nothing in the middle. That is not sloppiness; it is two different tools that
happen to share a text box.

### The long ones really are rambles

Of the 312 prompts over 1,000 characters, **71% are prose**, not pasted logs or stack
traces. **31% of those contain uncorrected typos.** Sentence-enders run at 0.082 per
word — *above* normal prose, because a ramble is many short declaratives fired in
sequence, not long constructed paragraphs.

A real one, unedited, 14,585 characters:

> *"how can we really mkae computer generated graphics and take them to the enxt level?
> i want to make the game more interactive, more physics, more procedurally gernated
> characters beyond just stable diffusion..."*

`mkae`. `enxt`. `gernated`. Left in on purpose. Fixing `mkae` costs you the next three
ideas, and the model was never confused by it. **Typos are free. Unproven claims are not.**

### The precision did not disappear — it moved

Same corpus, human prompts vs the machine-written dispatches the harness generated:

| the prompt contains… | human | harness |
|---|---:|---:|
| a concrete file path | 6.0% | **90.0%** |
| an expected output shape | 1.2% | **57.9%** |
| a hard constraint (must / don't / required) | 2.7% | **77.7%** |
| a demand for proof or evidence | 3.6% | **64.5%** |
| "live / real / not mocked" | 8.5% | **73.3%** |
| a named gotcha, trap, or invariant | 0.4% | 12.3% |

Median human prompt: 56 characters. Median harness dispatch: **2,986 characters.**
Ratio: **1.85 machine dispatches per human prompt.**

The single most damning line in the whole dataset: **"don't assume" / "don't guess"
appears in 1 of 5,244 human prompts.** 0.0%.

Not because the operator tolerates guessing. Because it is written *once*, in a rules
file, and therefore never has to be typed again.

**You do not type your standards. You install them.**

---

## The two modes

### Mode 1 — RAMBLE (to load new intent)

Use when the model does not yet have the shape of what you want: new feature, new
direction, a frustration you have not diagnosed, a thing you have only felt so far.

**Do:**
- Talk or type at the speed of thought. Voice input is ideal; the bandwidth is the point.
- Leave the typos. Leave the false starts. Leave "actually no, wait —" in.
- Include the stuff a clean brief would amputate: what you already tried, what you are
  worried about, what "good" looks like, what you are *not* willing to do, the tangent
  you are not sure is relevant. **The tangent is usually the load-bearing part.**
- Dump the raw artifact with it — the whole error, the whole page, the whole log.

**Do not:**
- Edit it. Editing is the tax that stops you doing it next time.
- Ask for execution off a raw ramble. End it with the reflection step:

> *"Reflect that back as a clean brief, then tell me what's missing and what you'd
> have gotten wrong. Ask clarifying questions before you touch anything."*

That one line converts a mess into a spec, and — more importantly — surfaces the
constraints you forgot you were assuming.

### Mode 2 — POKE (to steer a harness that already knows)

Once the harness carries your standards, steering collapses to almost nothing. The
measured vocabulary, verbatim, by frequency:

```
continue          do it          yes          get it done
status?           gap analysis   close the gaps
commit and push   go             run it       fix it
```

`continue` appears 189 times. `do it`, 65. `status?`, 27.

If your steering prompts are still long, that is a harness bug, not a prompting habit.
Every requirement you retype is one you failed to install.

### The anti-mode

The failure everyone actually has is **the medium prompt** — 200 to 800 characters of
tidied-up, professional-sounding request. Too polished to carry your real intent, too
short to carry the spec, and aimed at a harness that does not know your standards. It
is the worst of all three and it is what most people type all day.

---

## The harness is the deliverable

A ramble into a bare chat window gets you a plausible wrong thing, fast. The ramble
works *because* something catches it. Build the catcher — this is what turns a 56-char
poke into a 3,000-char dispatch:

1. **A rules file the agent reads every session.** Your standards, written once:
   what "done" means, what proof you require, what it must never do. This is where
   "don't guess" lives so you never type it. Keep it short — it competes for context.
2. **Persistent memory with an index.** One line per fact, linking to detail. Traps get
   an emoji and a severity. Disproven theories get marked REFUTED, not deleted — the
   refutation is the valuable part when the symptom comes back.
3. **Skills for anything you have asked for twice.** A repeated request is a missing
   skill. That is the whole rule.
4. **A gate that can fail.** Lint, tests, a live round-trip, a Stop hook. Without one,
   the harness will cheerfully return "done" on the wrong thing and the ramble
   compounds the error instead of correcting it.
5. **Delegation for fan-out.** The 1.85:1 ratio is not overhead; it is the machine
   writing the careful prompts you stopped writing.

Without #4 in particular, this whole method is worse than careful prompting. Say that
out loud before adopting it.

---

## What this skill does when invoked

### `analyze` — measure your own prompt shape

Your telemetry is already on disk. Mine it:

1. Enumerate `~/.claude/projects/**/*.jsonl` (each line one message envelope).
2. Keep `type == "user"` records; **drop** any whose content array holds a `tool_result`
   block, plus `isMeta` records and text starting with `<` — those are tool output and
   injected context, not things the human typed. *(Skipping this filter is the single
   most common error; it inflates the median by ~30x and silently makes machine-written
   dispatches look like human prompts.)*
3. Separate real project directories from subagent/workflow transcript directories —
   the latter are the harness prompting itself, and mixing them is what produced a
   "median 1,863 chars" reading that was off by a factor of 33.
4. Report: median, the bucket histogram above, the share of characters held by the top
   6% of prompts, and the human-vs-harness specificity table.

Then say plainly which mode the user is missing. Most people have neither — they live
entirely in the 200–800 char anti-mode.

### `install` — build the harness

Set up, in this order (each is useless without the one before it):
1. A rules file at `.claude/rules/` (or append to `CLAUDE.md`) carrying their standards.
2. A memory index file, seeded with the three archetypes: program-state, trap, directive.
3. One gate that can fail — start with the cheapest real one (lint or a Stop hook).
4. Only then, skills and delegation.

Do not overwrite anything that exists. Merge and append; report exactly what was written.

### `explain` — teach it

Walk through the measurement, the two modes, and the anti-mode. Be honest about the
limits below.

---

## Honest limits

- **This is one operator, one 34-day window, one domain** (infrastructure and agent
  platforms). The bimodality is a strong signal; the exact percentages are not a law of
  nature. Measure your own — the tooling above takes ten minutes.
- **It is survivorship-shaped.** These numbers come from a setup that already had rules,
  memory, gates, and delegation. Ramble-then-poke on a *bare* install mostly produces
  confident garbage at speed. The harness is load-bearing; build it first.
- **The typo tolerance is a personal choice, not a finding.** What the data supports is
  that typos did not degrade outcomes in this corpus. It does not show that polish
  *never* helps — nobody ran the control.
- **Rambling costs tokens.** 78% of the human's characters sat in 6% of the prompts, and
  those get re-read on every subsequent turn until compaction. That is a real bill.
- **Voice is claimed, not measured here.** These are typed transcripts. The speed
  argument for dictation is mechanical and believable, but this corpus does not prove it.

---

## The one-line version

**Ramble to load. Poke to steer. Put the precision in the harness — and never type a
standard twice.**
