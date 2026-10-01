---
name: concurrent-safe-git
allowed-tools: Read, Grep, Glob, Bash, PowerShell
description: Commit safely in a worktree you do NOT have to yourself — several agents, a teammate, and a maintenance loop all editing and committing at once. The pathspec commit form, the four commands that silently destroy someone else's work, and the stat-dirty refresh that unblocks a merge git only THINKS is unsafe. Written from real incidents where a 10-line fix committed 270 lines and a reset --hard put a fixed security bug back into production.
argument-hint: [commit | unblock-merge | diverged]
---

# Concurrent-safe git

**You do not have this working tree to yourself.**

The moment more than one agent — or an agent plus a human, or an agent plus a
cron/maintenance loop — edits the same checkout, ordinary git commands become
destructive. Not "risky." Destructive. HEAD advances under you, files appear and
vanish between the moment you survey and the moment you stage, and a bare
`git commit` ships whatever *somebody else* happened to have staged.

If a CD loop deploys from your commits, this stops being a local problem. A careless
git operation in a shared tree **ships**.

---

## Two incidents, because the abstract version doesn't land

**A `reset --hard` re-deployed a fixed security bug.** One session ran
`git reset --hard` to clean up its own mess. That wiped hours of another session's
uncommitted work on *tracked* files — untracked files survived, which is the
signature that tells you this is what happened. The reverted source then flowed
through the deploy loop, putting an already-fixed cross-tenant data leak **back into
production**. Nobody typed "undo the security fix."

**A 10-line fix committed 270 lines.** A bare `git commit` in a file another session
had in-flight hunks in. The commit message described ten lines. The diff was
two hundred and seventy, including a half-finished change to an API contract.

Both were routine commands. Neither operator did anything they'd call careless.

---

## The rules

### 1. Commit with an explicit pathspec, atomically, in ONE command

```bash
git add <paths> && git commit -m "msg" -- <same paths>
```

The trailing `-- <paths>` is the part people drop, and it's the part that matters:
it makes the commit ignore foreign staged content in *both* directions. Same paths
in both halves.

Never a bare `git commit`. Never `git add -A` or `git add .`.

### 2. Never a bare `reset`, `checkout .`, `stash`, or `reset --hard`

These operate on the **whole tree** and clobber whoever else is working in it.

If you must unstage, scope it: `git reset -- <your-paths>`.

If you think you need `reset --hard`: **stop.** That is the exact command from the
first incident. Find another way, or ask a human.

### 3. Re-check `git status --porcelain` immediately before each commit group

Not at the start of your task — *immediately* before. Skip files another session
already landed. Leave their fresh in-flight modifications alone.

### 4. Guard against the same-file sweep

Before committing a file someone else may be editing:

```bash
git diff --stat -- <file>
```

Confirm the line count matches **your** edit. If it's larger, foreign hunks are
riding along. Read the full diff. If they changed one end of a contract — a backend
gate and its frontend field, a schema and its consumer — verify *both* ends before
you let a deploy loop have it.

### 5. Verify after committing

```bash
git show --stat <sha>
```

Broad pathspecs sweep more than you intended. A directory pathspec like
`tests/` will happily collect three other people's new test files. Confirm only your
files landed — after the fact, not on faith.

### 6. Commit early, in small groups

Uncommitted work in a shared tree is **not durable**. Someone else's routine hygiene
can eat it. Don't let a large edit sit unstaged while you keep going.

---

## "Your local changes would be overwritten by merge" is usually a lie

With many writers, index entries go **stat-dirty**: mtime changed, content identical.
Git then blocks the merge to protect changes that *do not exist* — and the obvious
escape (`git stash`) is exactly what rule 2 forbids.

Do this instead:

```bash
git diff --stat -- <the blocked paths>   # which are REALLY modified?
git update-index --refresh               # re-stat; clears false-dirty entries
git merge origin/<branch>                # usually just works now
```

Measured on a real tree: a merge blocked by **7** files. `git diff --stat` showed
exactly **one** had real changes; the other six were stat-only. After `--refresh` the
merge proceeded and **all seven kept their content** — no stash, no reset, nothing lost.

If a file *is* genuinely modified and still blocks, copy it to a scratch directory
first — a filesystem backup git cannot lose — then scope any stash to that one path.
Never the whole tree.

---

## Don't fight a divergence that resolves itself

When every session shares ONE worktree and ONE HEAD, another session's `git push`
carries **your** commits too, and their `merge` clears your `behind` count.

If a push is rejected: re-`fetch`, re-check, wait. Observed on a real tree: a state
that looked like "ahead 6 / behind 6" became "ahead 5 / behind 0" on its own within
minutes, with no intervention.

Your commits are not at risk while they sit on the shared HEAD. **The risk comes from
forcing the issue.**

Push as its own command, never chained — a force-push guard can false-positive on a
compound `a && git push`.

---

## Never

- `git reset --hard` (any form)
- bare `git reset`, `git checkout .`, `git stash`
- `git add -A` / `git add .` / bare `git commit`
- chained `git push`

## The one-line version

*Name your paths, check twice, and never run a command whose blast radius is
"everything."*
