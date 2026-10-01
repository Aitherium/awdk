---
name: awknowledge
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, PowerShell
description: Install a measured operating doctrine for running an agentic coding tool at program scale — prompt shape, live-proof gates, plan documents, persistent memory, delegation, compaction and model routing. Mined from 27,939 prompts over 210 days plus a 34-day re-measurement, not invented.
argument-hint: [install | explain | analyze]
---

# Aither World Knowledge — an operating doctrine you can install

Most advice about working with coding agents is vibes. This one is telemetry.

Every rule below was mined from one operator's real logs: **27,939 prompts across
3,183 sessions over 210 days**, then independently re-measured on a disjoint
**34-day window (5,244 human prompts, 9,715 machine-written agent dispatches,
359 sessions)**. The two windows agree on the load-bearing number — median human
prompt **58 chars** and **56 chars** respectively.

Each rule says WHEN it fires, WHAT to do, and the EVIDENCE it came from.

> **Read §0 and §2 together or not at all.** §0 makes you fast. §2 is what stops
> fast from meaning wrong. Installing §0 alone on a bare setup produces confident
> garbage at speed.

---

## 0. Ramble to load, poke to steer — never write the middle prompt

**When:** every prompt. This is the shape law the rest sits on.

**What:** your prompts should be bimodal, with nothing in between.

- **RAMBLE** — unbounded, unedited — when loading *new* intent. Type or talk at the
  speed of thought. Leave the typos and false starts in. Include what you already
  tried, what you're worried about, what "good" looks like, what you refuse to do,
  and the tangent you're unsure about (usually the load-bearing part). Then end with:
  > *"Reflect that back as a clean brief, tell me what's missing and what you'd have
  > gotten wrong, and ask clarifying questions before touching anything."*

  Never execute off a raw ramble. The reflection step is the whole trick.

- **POKE** — under ~60 chars — for everything else. The measured vocabulary:
  `continue` · `do it` · `get it done` · `status?` · `gap analysis` ·
  `close the gaps` · `commit and push` · `run it` · `fix it`

- **THE ANTI-MODE** — 200–800 characters of tidied-up, professional-sounding request.
  Too polished to carry your real intent, too short to carry a spec. It's what most
  people type all day and it's worse than either mode.

**The corollary:** the fully-specified prompt still has to exist — your *harness*
writes it. Every requirement you retype is one you failed to install.

**Evidence:** 26.9% of human prompts are under 20 chars, 58.5% under 80 — yet the
5.9% over 1,000 chars carry **78.4% of every character typed**; 71% of those are
prose rambles and 31% keep their typos. The harness meanwhile emitted 9,715
dispatches at a 2,986-char median (1.85 per human prompt):

| prompt contains | human | harness |
|---|---:|---:|
| a file path | 6% | **90%** |
| expected output shape | 1% | **58%** |
| a hard constraint | 3% | **78%** |
| a demand for proof | 4% | **65%** |
| "live / not mocked" | 9% | **73%** |

"Don't assume" appears in **1 of 5,244** human prompts — because it's written once
in a rules file. **You don't type your standards. You install them.**

*Full treatment, including how to mine your own logs: the `ramble-driven-development` skill.*

## 1. State the outcome and the proof standard, not the steps

**When:** scoping anything. **What:** phrase it as "build X — done when [observable
live behavior]". Bundle the verification into the same prompt as the request; never
ask for code now and a test later. **Evidence:** the highest-yield prompts in the
corpus consistently pair ask + proof.

## 2. Live verification gate — a feeling is not a check

**When:** anything claims to be done. **What:** require a check that can FAIL — a live
round-trip, a positive assertion that data actually flows, terminal output shown.
Lint green, tests green and "the code looks right" are necessary and never sufficient.
Watch for the **silent no-op**: a fail-closed path that always returns empty passes
every "returns nothing" assertion while being completely inert. Every feature needs a
positive assertion that the happy path really produces data.
**Evidence:** every phase gate in 231 plan documents is a live check, never "tests pass".

### 2b. A missing tool is a SILENCE — enumerate your MCP config, never assume one file

**When:** your agent seems to have lost its tools, or is quietly doing everything the long
way. **What:** an absent tool raises no error, logs nothing, and fails no call — it is
indistinguishable from a session that never needed one. You cannot notice a tool you were
never offered, so this needs a check, not vigilance.

Two things make it worse than it sounds:

- **A project can hold more than one `.mcp.json`, and the one NEAREST your working
  directory wins.** A perfectly correct config at the repo root proves nothing if a nested
  one shadows it. Enumerate every config in the tree before concluding anything.
- **Use `127.0.0.1`, never `localhost`.** `localhost` resolves `::1` first. Measured on a
  Windows/WSL2 box: `::1:8182` refused after **2120 ms** where `127.0.0.1:8182` connected
  in **3 ms** — a ~2 s tax on every connection, and a hard failure for any client that
  doesn't walk to the next address.

And check the **generator**, not just the file: if a setup script writes that config, a
hand-fix to its output silently reverts the next time the script runs. Fix the emitter.

**Evidence:** a session ran with zero platform tools while the gateway was `Up (healthy)`,
its `/health` returned 200, and an authenticated probe listed 1211 tools. Every cheap
signal was green; the config that was wrong was not the config anyone thinks to read.

## 3. Root cause over workaround

**When:** a fallback or disable-it-for-now is proposed. **What:** refuse by default; fix
the cause even when slower. If a workaround is genuinely necessary it goes in a debt
ledger with a severity and a date — debt is recorded, never silent.

## 4. Plan documents, not plan theater

**When:** work is multi-phase (3+ independently shippable phases), spans sessions, or
carries architectural trade-offs. Skip for one-liners and triage.
**What:** write a persistent plan *file*. Structure: context → **decisions locked**
(trade-offs answered once, never re-debated) → verified current state (audit what
exists before phasing) → phases each with a **live-provable gate** → explicit
sequencing (what blocks what, what parallelizes) → risks flagged at planning time.
Update it in place as phases ship.
**Evidence:** 231 written plans vs 12 interactive plan-mode entries across 207 sessions.

## 5. Persistent memory with an index, or every session starts from zero

**When:** always. **What:** two tiers — an index of one-line entries, each linking to a
detail file. Three archetypes carry the weight:
- **Program state** — name + phase status + next step + plan link
- **Trap** — a recurring gotcha with a severity marker so nobody re-derives it
- **Standing directive** — a durable decision, usually phrased as a prohibition

Mark disproven diagnoses **REFUTED** rather than deleting them; the refutation is the
valuable part when the symptom recurs.

## 6. Orchestrate fan-out; solo the single thread

**Orchestrate when:** the task splits into 3+ independent streams, gap-analysis sweeps,
exhaustive audits, crisis debugging across systems. Give each stream ownership and a
gate that can fail. **Stay solo for:** single-threaded edits, isolated fixes, status
checks. Splitting one coherent edit across agents is bad delegation.
**Evidence:** 1,004 delegations / 541 workflow runs / 1,184 subagent dispatches,
clustering on builds, sweeps and crises — not small fixes.

## 7. Compact tactically; resume relentlessly

**Compact when:** stuck in a loop (same error 3+ times), at a domain boundary inside a
long session, right after a milestone, or before a major escalation.
**Never compact:** mid-implementation, mid-debug with live error context, or right
after a fresh start. **Also:** prefer resuming long sessions over starting fresh —
context is an asset. Marathon sessions with 2–3 compacts are healthy, not failure.
**Evidence:** 354 compacts, work continuing 10–50 prompts after each; resume used
724× vs clear 164×.

## 8. Route models by tier; check limits before they bite

Frontier/deep-reasoning model for architecture, root-cause, security review and
adversarial verification. Strong workhorse for the bulk of implementation (~70% of
message volume). Cheap models almost never for interactive work — they still earn
their keep for mechanical subagent stages inside orchestration. Check usage
proactively and switch preemptively rather than mid-task. Switching more than once
per two hours means re-plan the session, don't push harder.

## 9. Frame architecture by boundaries

State what you will **not** do or own, first. Negative constraints cut scope faster and
survive longer than feature lists. When correcting course, restate the violated
boundary — "broken because it violates X", not just "broken".

## 10. Speed over polish — but never over proof

Ship the working core and iterate live; cosmetics come after the end-to-end round-trip
is proven. **Typos are free. Unproven claims are not.** The asymmetry is deliberate.

## 11. Consolidate sprawl on sight

Two config files, two graph stores, parallel auth paths — duplicated sources of truth
are active hazards because they drift. Flag on sight, demand a merge plan, ledger it if
out of scope.

## 12. Self-skeptic pass before "done"

Every delivery: state at least one concrete weakness, limitation, or untested
assumption — then fix it or flag it. "Looks right to me" is not a check. Pair with:
*what did I leave worse than it should be?*

---

## What this skill does when invoked

### `install`
1. **Inspect read-only first.** Look for an existing rules directory, project context
   file, memory index, plans directory, and debt ledger. **Overwrite nothing** — merge
   and append.
2. **Ask once:** full doctrine (rules + plan template + memory scaffold + context-file
   pointer) or rules only.
3. **Write:**
   - the doctrine to the agent's rules location (or append to its project context file)
   - a plan template to a plans directory
   - a memory index seeded with the three archetypes from §5
   - a pointer from the project context file to both
4. **Offer, don't silently install,** two enforcement hooks: a stop-hook that blocks
   ending a turn where code changed but the debt ledger didn't, and a compaction
   reminder at milestone boundaries.
5. **Report** exactly what was written where and what was skipped.

### `analyze`
Mine the user's own transcripts and tell them which doctrine rules they're already
following. See `ramble-driven-development` for the extraction method — including the
two filters that, if skipped, inflate the median prompt length by ~33x.

### `explain`
Walk the rules with their evidence. Be honest about the limits below.

---

## What does NOT transfer — say this honestly

- **The infrastructure.** The source environment has its own agent mesh and tooling.
  The *principles* — live gates, delegation, effort tiers — transfer. The specific
  plumbing does not. Map them onto whatever the target actually has, even if that's
  just the built-in subagent tools.
- **The tone.** The source corpus is 21.5% ALL-CAPS, 12.4% profanity, unfiltered. What
  did the work was the *content pattern* — outcome + proof + boundary restated — not
  the volume. Install the pattern, not the shouting.
- **Typo tolerance is a choice, not a finding.** The data shows typos didn't degrade
  outcomes in this corpus. Nobody ran the control that would show polish never helps.
- **Model quotas.** Keep the routing logic; re-derive the cadence for the target's plan.
- **One operator, one domain.** Infrastructure and agent platforms. The bimodality in
  §0 is a strong signal; the exact percentages are not a law of nature. Measure your own.
