---
name: repo-is-not-a-runtime
description: >-
  The architecture doctrine for agent-driven repos. Two rules. Both sound obvious. Both are violated by nearly every repo that has had agents working in it for a few months, and the violation is invisible until a disk fills at 3am.
---

# repo-is-not-a-runtime — the architecture doctrine for agent-driven repos

Two rules. Both sound obvious. Both are violated by nearly every repo that has had
agents working in it for a few months, and the violation is invisible until a disk
fills at 3am.

> **RULE 1 — A repository is a SOURCE artifact, not a runtime.**
> Nothing a service or an agent *writes* may live inside the checkout.
>
> **RULE 2 — Every ephemeral resource an agent creates must have a REAPER.**
> Created-by, a TTL, and something that actually deletes it. Otherwise it is forever.

Measured on a real platform (2026-07-26): a checkout at **1.15 TB**, of which `.git`
was **4.7 GB**. The repository was **0.4% repository.**

| what | size | which rule |
|---|---|---|
| `data/` — service runtime state | 490 GB | Rule 1 |
| `.claude/worktrees/` — 32 abandoned agent worktrees | 295 GB | Rule 2 |
| `AitherOS/` — training data + models beside source | 237 GB | Rule 1 |
| `.iso-out/`, `.build-tars/`, `.hf-cache/`, `.cache/` | 62 GB | Rule 1 |
| `.git` — the actual repository | **4.7 GB** | — |

Reaping the worktrees alone returned **326 GB**.

## Why Rule 1 is architectural, not housekeeping

Runtime data inside a checkout is not merely untidy — it **multiplies**:

- **Every worktree pays for it.** An agent harness that creates 30 isolated worktrees
  just made 30 copies of whatever tracked bulk you left in the tree.
- **Every backup and clone pays for it.**
- **`git status` becomes noise**, so real changes hide among generated files, and
  "744 uncommitted files" stops meaning anything.
- **Quality gates scan the working tree**, so foreign in-flight junk trips them.
- **It fills the same volume as your container storage**, and they race.

The fix is one line of indirection: services take their data path from an env var
that points **outside** the tree.

```bash
# not this
DATA_DIR=./data

# this
DATA_DIR="${APP_DATA_ROOT:?set it outside the checkout}"
```

Then the checkout can be deleted and recloned at any time without losing state —
which is the actual definition of "source artifact".

## Why Rule 2 needs teeth

Agent harnesses create throwaway resources constantly: git worktrees per isolated
task, build tarballs, image layers, scratch containers. Each one is individually
reasonable. None of them clean up. There is no natural back-pressure — you notice at
100% disk, never at 60%.

Anything an agent creates needs three properties:

1. **an owner** — a name/label that says what made it and why
2. **a TTL** — how long it is allowed to exist unattended
3. **a reaper** — something that actually runs and deletes it

If you cannot name the reaper, you have not built a feature, you have built a leak.

## The gate

Doctrine without a gate is a wish. Wire
[`tools/repo-hygiene-audit.sh`](../tools/repo-hygiene-audit.sh) into CI or a
pre-push hook:

```bash
repo-hygiene-audit.sh          # fast checks, exit 1 on violation
repo-hygiene-audit.sh --deep   # adds the .git : working-tree ratio (slow walk)
repo-hygiene-audit.sh --warn   # report only, never fail the build
```

It fails on: bulk runtime directories inside the tree, agent worktrees older than
`REPO_WT_MAX_AGE_DAYS`, and worktree registration drift.

Pair it with [`agent-worktree-reaper.sh`](../tools/agent-worktree-reaper.sh) for
Rule 2, and see [`agent-disk-hygiene`](agent-disk-hygiene.md) for the safety gate
that makes reaping non-destructive.

🪤 **Every size probe must be time-boxed.** The repos that most need this audit are
exactly the ones where `du -sm .` never returns — the first cut of this tool hung
for nine minutes on the tree it was written for. A directory too large to measure is
itself the finding; report it and move on rather than blocking the gate.

## Retrofitting an existing repo

Do it in this order — the cheap reversible steps first:

1. **Reap ephemerals.** Archive diffs, then delete. Biggest win, zero risk when the
   safety gate is honest. (326 GB here.)
2. **Move caches out.** `.hf-cache`, `.build-tars`, `.cache`, `.iso-out` — pure
   regenerable output. Point them at a path outside the tree by env var.
3. **Move runtime data out.** `data/`, `logs/`. Needs a service restart, so schedule
   it; verify each service reads the new path *before* deleting the old one.
4. **Decide about large committed assets.** Training data and model weights beside
   source is a real decision, not an accident — but it should be a *decision*, with
   an artifact store or LFS as the alternative.
5. **Turn on the gate** so it cannot drift back.

## Before you claim done

- the audit exits 0, or every remaining violation is one you consciously accepted
- `git worktree list` matches what is on disk
- you can name the **reaper** for every class of ephemeral your agents create — if
  you cannot, that class is a leak with a countdown on it
- deleting and re-cloning the checkout would lose **nothing** — that is Rule 1,
  stated as a test
