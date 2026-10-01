---
name: adversarial-verification
description: >-
  Stop grading your own homework. There is one failure that costs more agent-hours than any bug: the agent reads its own diff, decides it is correct, and reports success. Every expensive incident has that shape. A green healthcheck. An HTTP 200. A passing import. A test that goes green. All true, all at the same time as the feature being completely inert.
---

# adversarial-verification — stop grading your own homework

There is one failure that costs more agent-hours than any bug: **the agent reads its own
diff, decides it is correct, and reports success.** Every expensive incident has that
shape. A green healthcheck. An HTTP 200. A passing import. A test that goes green. All
true, all at the same time as the feature being completely inert.

More care does not fix this. You cannot carefully notice a thing you are structurally
unable to see, and an author is structurally unable to see their own change the way a
stranger does. The fix is **removing yourself from the judgement**.

## The loop

Capture evidence from a clean baseline → change one thing → recapture from the *same*
baseline → have someone who **cannot see your diff** decide which side is better,
**without being told which is which**.

```
1. capture(before)     reset the world, record one observable per surface
2. critique            fresh-context agents see ARTIFACTS ONLY — no code, no diff
3. rank                pick 2-3 gaps; dedupe against the rejected ledger
4. build               one gap per builder; builders do not commit
5. capture(after)      same reset, same probes
6. verdict             blind A/B, sides shuffled, key hidden
7. gate                objective checks, raw exit codes, nothing summarised away
```

Accept **only** if every gate passes **and** every changed surface's blind verdict lands on
`after`. Otherwise revert and write the gap to a rejected ledger so the next round cannot
re-propose it.

**A loop that never rejects is not measuring anything.** Rejection is the system working.

## The five traps, and how to mechanise each

Each of these cost a real run. Prose warnings did not prevent any of them — only a tool
that refuses to proceed did.

### 1. Contaminated baseline

A run was thrown out because before and after ran against a dirty state directory. They
compared two different *worlds*, not two versions of the code.

→ **The capture step performs the reset itself** and refuses to record anything if the
reset fails. Not "remember to reset first" — it cannot be skipped.

### 2. Judging the proxy instead of the thing

A UI check read computed styles twice and reported success while the screen was visibly
wrong. The same shape everywhere: an endpoint that returns 200 to an anonymous probe and
403 to a real session; a search that returns `[]` with HTTP 200; a mounted file that
`grep` shows as fixed while the running process still holds the old import.

→ **Every surface declares its observable, and a surface that emits nothing is DEAD.**

### 3. Crediting noise

Identical captures differ by ~1% from timing alone. If you accept a 1% "improvement" you
are accepting randomness and will do it forever.

→ **Declare a noise floor.** Anything below it is un-verdictable, and a verdict on an
un-verdictable surface is discarded, not counted.

### 4. A filtered run reading as a green board

"All checks pass" is not the same as "all checks ran".

→ **Record argv and the raw exit code for every gate**, and fail on a *missing* gate, not
only a failing one.

### 5. The judge seeing the rationale

A judge who knows which side is the fix will rationalise toward it. Every time.

→ **Shuffle the sides per surface and hide the key.** The builder's reasoning goes in the
commit message and is never shown to a judge.

## FAIL is not DEAD

- **FAIL** — the invariant is broken.
- **DEAD** — the probe could not judge: timeout, missing tool, empty output.

Both block acceptance, and they must be **reported separately**. Conflating them is how
"the checker is broken" gets filed as "the system is fine". Exit non-zero when you could
not look. **Silence is never a pass.**

## Worked example: the fix that would have made things worse

A chat assistant was inventing facts — confidently naming the wrong network port for its
own components, a different wrong answer each time.

Reading the code found it quickly: the routing flag that enables retrieval was computed
from exactly two detectors, neither of which matched a plain factual question. Obvious fix,
small diff, clearly correct. **Ship it.**

The wave loop ran a control surface first — a question that *did* match one of the two
detectors, and therefore *should* have retrieved. It came back with retrieval correctly
routed, **zero tools executed**, and this answer:

> "I couldn't find any references to `<that function>` in the codebase. The function may
> not exist or may be named differently."

The function existed. The assistant had asserted a confident negative from a search that
never ran.

So there were **two** defects, not one. Adding the missing detector would have routed every
factual question into a path that also retrieves nothing — converting *"the port is 8080"*
(obviously wrong) into *"I could find no information about that"* (authoritatively wrong).
A clean diff, a plausible story, and **worse for every user**.

The real cause was three layers down and measurable in one command: the tool planner took
**12-13 seconds** against a **2-second** timeout. It always lost. It picked exactly the
right tools when allowed to finish. The timeout was swallowed at debug level, so nothing
ever surfaced it, and the model was simply handed an empty tool list.

Someone had already found this once and moved the timeout from 0.5s to 2s — halving the
gap on a 13-second problem, and leaving it silently broken.

**The lesson is not "check more carefully."** The author had read the code correctly and
drawn a reasonable conclusion. It took a control surface, chosen in advance to be one that
*ought* to pass, to prove the plan wrong before a line was written.

## Designing surfaces that can actually fail

- **Include a control you expect to PASS.** If it fails, your model of the system is wrong
  and everything downstream is guesswork. This is the single highest-value surface.
- **Include a regression guard.** Something the fix must NOT change. A change that improves
  the target and quietly wrecks a fast path is a net loss.
- **Use at least two instances of the target class.** One is a hardcode waiting to happen.
- **Make the observable structured, not prose.** Free text differs on every sample and
  swamps any noise floor. Record the facts — did it retrieve, how many tools ran, was the
  known-correct value present — and keep one sample of the text for the judge.
- **Compare against ground truth you established independently**, never against what the
  system told you.

## Objective gates outrank the blind vote

The vote is a tiebreak among plausible options, not the decision. Structure it so a
persuasive-but-empty change cannot win:

```
accept = measurably_improved
         AND no_regression_on_guard_surface
         AND blind_votes_for_after >= blind_votes_for_before
```

If the numbers did not move, it does not matter how good it looked to three judges.

## When it is worth the cost

Use it when the change is broad or the feedback is untrustworthy:

- one defect class spread across many call sites — the shape that tempts a bulk find-and-replace
- any surface where the cheap signal is known to lie: behind a healthcheck, a circuit
  breaker, a swallowed exception, or an `if response.ok:` with no else branch
- an area where a *previous* fix was later found not to have worked
- anything you are about to call "fixed" without a check that could have failed

**Skip it** when a single failing test already answers the question. This is expensive, and
spending it where a normal test suffices teaches people to route around it.

## Practical notes

- **Never run a wave against something you cannot afford to disturb.** Use a disposable
  instance. If you restart a live service in order to observe it, you have changed the
  thing you are measuring.
- **A gate can be red for someone else's reasons.** On a shared branch the changed-file set
  grows underneath you. Split gate output by whether *you* touched the file. Fix yours;
  someone else's in-flight work is not your regression, and "fixing" a file another person
  is mid-edit on corrupts it.
- **A mechanical fix creates new violations of other rules.** Adding an argument to nine
  call sites pushed five of them past the line-length limit. Re-run the gate after any bulk
  edit; never assume a fix is net-negative on the violation count.
- **Verify the deploy, not the edit.** Where code is baked into an artifact, an edit on
  disk is not live. Confirm the change is present *inside the running thing* before you
  measure it, or you will carefully A/B two identical builds.
