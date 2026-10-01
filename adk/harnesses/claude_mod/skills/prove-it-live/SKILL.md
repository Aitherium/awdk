---
name: prove-it-live
allowed-tools: Read, Grep, Glob, Bash, PowerShell, Edit, Write
description: The verification standard — green tests, a 200, and "deployed" are not proof. Require a check that can FAIL, hunt the silent no-op, and refuse to call anything done until the happy path is shown returning real data live. Includes an installable stop-hook that blocks a turn claiming done with no evidence.
argument-hint: [check | install-hook | explain]

---

# Prove It Live

> **green / 200 / deployed ≠ done.**

This is the standard that catches more real defects than every linter combined, and
it is almost never enforced by anything except a human remembering to care.

The failure it prevents is not "the code is wrong." It's **"the code is inert and
everything looks fine."** A fail-closed path that always returns empty passes every
"returns nothing" assertion. A service that 200s on `/health` while its queue worker
is dead looks healthy. A feature registered under one key and read under another
matches zero rows forever, silently, and every test still passes.

---

## The rule

**A feeling is not a check.** Before anything is called done, one of these must have
actually run and been capable of failing:

1. A test that runs and passes — and that you have seen **fail** when the thing is broken
2. Lint / type-check clean on the changed files
3. Output diffed against the spec
4. Sources actually read, not assumed
5. **A live round-trip** — the real request, against the real running thing, showing
   real data come back

Ordered weakest to strongest. #5 outranks all of them. Everything above it is
necessary and none of it is sufficient.

## The positive assertion

This is the part people skip.

**Every feature needs an assertion that the happy path returns real data.** A test
suite that only asserts denials is blind to a totally inert feature — it will pass
100% while the feature does nothing at all.

```
❌  assert resp.status_code == 403        # proves nothing about the allow path
❌  assert result == []                   # passes when the feature is dead
✅  assert len(result) == 3 and result[0].tenant_id == mine   # proves it WORKS
```

If you cannot point at the assertion that proves the allow path returns data, the
feature is unverified no matter how many denial tests are green.

## The silent-no-op hunt

Before declaring done, ask specifically: **could this return "nothing" for a reason
other than there being nothing?**

- Wrong key / id mismatch — registered under hostname, looked up by pool id
- Missing auth on an internal call — 401s and fails silently on a fire-and-forget path
- A path that does not exist — a linter handed a typo'd filename reports "0 errors"
- An exception swallowed at `debug` level
- A config default quietly substituting for the value you thought you set

Each of these produces a green run over nothing at all.

## Deploy reality — "it's live" usually isn't

A change is not deployed because you edited the file. Know which of these your target is:

- **Baked into an image** → rebuild required. Editing the source changes nothing running.
- **Bind-mounted** → a restart is enough.
- **Built artifact** (frontend bundles) → rebuild; the served copy is not your source.
- **Copied in by hand** (`docker cp` and friends) → live now, **gone on next recreate**.

State which one applies when you claim something is deployed. "The code is fixed" and
"the fix is running" are different sentences, and conflating them is how a P0 gets
closed twice.

---

## What this skill does when invoked

### `check`
Given a change, produce the evidence table — and be honest where there is none:

| claim | evidence | can it fail? |
|---|---|---|
| feature works | *the actual command + its output* | yes/no |
| deployed | rebuild/restart performed, verified how | yes/no |

Any row whose evidence is "the code looks right" is a **fail**. Say so plainly rather
than softening it. Then either get the evidence or state explicitly that the claim is
unverified.

### `install-hook`
The hook is written and self-tested — **copy it, do not reinvent it**:
[`hooks/stop_live_proof.py`](../hooks/stop_live_proof.py), wiring block and
per-platform notes in [`hooks/README.md`](../hooks/README.md).

```bash
mkdir -p .claude/hooks
cp hooks/hook_common.py hooks/stop_live_proof.py .claude/hooks/
python3 hooks/test_hooks.py     # 21 cases, mutation-verified
```

Then add the `Stop` entry from `hooks/README.md` to `.claude/settings.json` and restart
the agent.

What it does: a turn that **changed a file** and ends by claiming done / fixed /
deployed / working, with no test, lint, build or probe having run, is blocked. An
explicit *"unverified because X"* passes, and so does a turn with no completion claim.

The escape hatch matters. A gate with no legitimate way through gets disabled, and a
disabled gate guards nothing — what the hook actually buys is that skipping verification
becomes a *stated* act rather than an accidental one.

Porting to an agent that is not Claude Code is a transport change, not a rewrite — see
the porting section of `hooks/README.md`.

### `explain`
Walk the rule, the positive assertion, and the silent-no-op list with examples.

---

## Honest limits

- **This standard costs time.** Live verification is slower than trusting a green
  suite, and on genuinely trivial changes it is overkill. The judgement call is real;
  the failure mode is applying it never, not applying it sometimes.
- **A stop-hook detecting "evidence" is heuristic.** It can be satisfied by running an
  irrelevant command. It raises the floor, it does not prove anything by itself — the
  hook makes skipping verification *deliberate* rather than accidental, which is the
  actual win.
- **"Live" is not always available.** Air-gapped, pre-deploy, or destructive-to-test
  paths sometimes genuinely cannot be exercised. Then say so, in those words, and put
  it in the debt ledger — an unverified claim that is *labelled* unverified is fine.
  An unverified claim wearing the word "done" is not.
