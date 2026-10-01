---
name: debt-ledger
allowed-tools: Read, Write, Edit, Grep, Glob, Bash, PowerShell
description: Keep one master tech-debt ledger that actually gets written to — severity tables, collision-free ids via the bundled next_debt_id.py, and the discipline that debt found is debt recorded in the same turn. Includes the stop-hook that blocks finishing a code change without a debt check.
argument-hint: [add | audit | next-id | install-hook]
---

# Debt Ledger

**Debt that isn't written down is debt nobody pays.**

One file, at the repo root, append-only-ish. Every session that changes code answers
one question before it finishes: *what did I just leave worse than it should be, or
notice was already broken and not fix?*

That question is the whole discipline. Everything below is mechanics.

---

## What earns a row

- A shortcut taken knowingly — simplified retry, no streaming, in-memory instead of durable
- An edge case skipped, a `TODO`, a disabled or xfail test, a hardcoded value
- A swallowed exception or fail-soft path that can now fail **silently**
- Something you **found** while working — an auth gap, a race, a disabled TLS check —
  and did not fix because it was out of scope
- A pre-existing bug you worked around instead of fixing

## What does not

- Anything you actually fixed this turn — that's a commit, not debt
- Speculative "we might someday want" — that's a feature idea
- Style nits the linter already catches

## Severity is impact, not effort

`P0` data loss / auth bypass / leak · `P1` must fix before release · `P2` should fix ·
`P3` note.

An auth bypass that takes ten minutes to fix is still P0. A three-week refactor that
inconveniences nobody is P3.

---

## Row shape

```
| id | area | debt | file(s) | found | status |
```

Write the **debt** column for someone who has never seen the code and will read it in
six months: the symptom, the mechanism, what you proved, and what you did *not*.
A row saying "auth is weird here" is worthless. A row naming the endpoint, the exact
request that reproduced it, and the reason you left it is a bug report your future
self can act on cold.

## The rules that keep it honest

1. **Same turn.** Debt recorded later is debt not recorded.
2. **Never delete a row.** Fixed → move to Resolved *with the commit sha*. Wrong →
   move to Resolved marked `refuted` with why. The refutation is the valuable part
   when the symptom comes back and someone re-derives the same wrong theory.
3. **"No new debt" is a valid answer** — but it has to be *stated*: "checked, no new
   debt: <one line why>." Silence is not the same as a clean check.
4. **Get the id from the tool, immediately before appending.** Not from reading the
   file and adding one.

---

## Ids collide, and reading max+1 is why

```bash
python tools/next_debt_id.py            # allocate + reserve the next free id
python tools/next_debt_id.py --audit    # find duplicate ids already in the ledger
python tools/next_debt_id.py --sweep    # drop reservations now present in the ledger
DEBT_LEDGER=DEBT.md python tools/next_debt_id.py   # different ledger filename
```

Computing `max + 1` from a file read is a race. Two agents working in parallel read
the same ledger seconds apart, both compute the same next id, and both append it —
which happened three times in one day before this tool existed. `next_debt_id.py`
**reserves** the id atomically in a sidecar directory, and scans every mention in the
ledger including Resolved rows and renumber notes, so an id is never reused.

It finds the repo root via `git rev-parse`, falling back to walking up for the ledger —
deliberately **not** by counting parent directories, because resolving by level count
is exactly how the same tool broke when it was copied one directory shallower and
silently reported the ledger "not found."

---

## What this skill does when invoked

### `add`
1. Run `next_debt_id.py` — **now**, not earlier in the turn.
2. Draft the row: severity by impact, the full mechanism in the debt column, the files,
   today's date, and a status that says what is proven vs assumed.
3. Append to the right severity table. Never reorder or renumber existing rows.

### `audit`
`next_debt_id.py --audit` for duplicate ids, then read for: rows marked open that were
actually fixed, rows with no file reference, and P0/P1 rows older than a month — those
are either mis-severitied or genuinely on fire.

### `next-id`
Just allocate one and print it.

### `install-hook`
The hook is written and self-tested — **copy it, do not reinvent it**:
[`hooks/stop_debt_ledger.py`](../hooks/stop_debt_ledger.py), wiring block and
per-platform notes in [`hooks/README.md`](../hooks/README.md).

```bash
mkdir -p .claude/hooks
cp hooks/hook_common.py hooks/stop_debt_ledger.py .claude/hooks/
python3 hooks/test_hooks.py     # 21 cases, mutation-verified
```

Then add the `Stop` entry from `hooks/README.md` to `.claude/settings.json` and restart
the agent.

What it does: a turn that wrote a file without touching the ledger is blocked. A stated
*"checked, no new debt: <why>"* passes, and so does a turn that edited the ledger.
`DEBT_LEDGER` renames the file it looks for; `DEBT_LEDGER_IGNORE` exempts paths whose
edits are not a code change. Without the escape hatch the hook gets disabled within a
week — which is why it is there and why "no new debt" has to be *said*.

---

## Honest limits

- **A ledger is not a backlog.** It records what is wrong; it does not prioritise or
  schedule. Rows accumulate. Expect hundreds, and expect most to stay open — that is
  the ledger working, not failing.
- **The stop-hook is a blunt instrument.** It detects "code changed, ledger didn't,"
  which fires on genuinely debt-free changes too. That is why the stated-exemption
  path exists, and why the honest answer is sometimes just to say so and move on.
- **Row quality decays under pressure.** The rows written at the end of a long session
  are the shortest and least useful ones, which is precisely when the debt is most
  interesting. No tool fixes this; knowing it helps.
