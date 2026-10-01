---
name: agent-disk-hygiene
description: >-
  Your coding agents are quietly eating your disk. Nobody tells you this when you start running agents at scale: they generate enormous amounts of disk garbage, in places you will never think to look. Not model weights — *debris*. Throwaway git worktrees, build tarballs, ISO output, caches inside caches.
---

# agent-disk-hygiene — your coding agents are quietly eating your disk

Nobody tells you this when you start running agents at scale: **they generate enormous
amounts of disk garbage, in places you will never think to look.** Not model weights —
*debris*. Throwaway git worktrees, build tarballs, ISO output, caches inside caches.

> Measured on a real box (2026-07-26). A drive at **98% full**. The single biggest
> non-Docker item was the *git checkout* at **1.15 TB** — of which `.git` was **4.7 GB**.
> The repo was **0.4% git** and 99.6% agent debris and runtime data.

The reason it hides so well: every instinct you have says "a checkout is source code."
`git status` is clean. `.git` looks normal. The debris sits in dot-directories beside it.

## Where it actually goes

Real breakdown of that 1.15 TB checkout:

| dir | size | what it is |
|---|---|---|
| `data/` | 490 GB | runtime service data written *inside* the repo |
| **`.claude/worktrees/`** | **295 GB** | **32 abandoned agent worktrees** |
| `AitherOS/` | 237 GB | Library/Training/models committed next to source |
| `.iso-out/` | 40 GB | build artifacts |
| `kaggle-training-data/`, `.hf-cache/`, `.build-tars/` | 33 GB | caches |
| `.git` | **4.7 GB** | ← the actual repository |

**Agent worktrees are the standout**, because they are pure waste: created per isolated
task (`isolation: "worktree"`, swarm/forge fan-outs, parallel reviewers), then orphaned.
32 of them at ~9 GB each. Nothing in any harness reaps them.

## Reap them safely

Use [`tools/agent-worktree-reaper.sh`](../tools/agent-worktree-reaper.sh):

```bash
agent-worktree-reaper.sh                              # audit — read-only
agent-worktree-reaper.sh --archive /backup/wt-diffs   # save dirty state first
agent-worktree-reaper.sh --reap                       # remove only the SAFE ones
agent-worktree-reaper.sh --reap --all --archive DIR   # remove dirty ones too
```

The gate: a worktree is safe only with **no uncommitted tracked changes** and **no
unpushed commits**.

```bash
git -C "$wt" status --porcelain -uno          # tracked changes only
git -C "$wt" log HEAD --not --remotes         # commits missing from every remote
```

🪤 **The trap that makes this look impossible.** `git log --branches --not --remotes`
seems like the natural check and is **wrong**. Worktrees share one object store, so
`--branches` enumerates *every branch in the repo* and returns an identical number for
every worktree. Live: the broken form reported "209 unpushed" for all 32 — so everything
looked unsafe and nothing would ever be cleaned. Anchoring on `HEAD` gave 0 for 30 of
them. **If your audit says every worktree is dirty, suspect your query, not your disk.**

🪤 Use `-uno`. Counting untracked files marks every worktree dirty on build output —
which is precisely the garbage you came to delete.

**Archive before deleting.** Diffs are text: 295 GB of worktrees produced **1.6 MB** of
diffs. There is no excuse for skipping it.

```bash
git -C "$wt" diff HEAD > "$ARCHIVE/$name.diff"
git -C "$wt" log HEAD --not --remotes --patch > "$ARCHIVE/$name.unpushed.patch"
```

**Use `git worktree remove`, not `rm -rf`.** A bare `rm -rf` leaves the registration
behind, so `git worktree list` keeps reporting trees that no longer exist. Follow with
`git worktree prune`. (Live drift seen: 32 directories on disk, 58 registered.)

## Stop it recurring

- **Reap on a schedule.** Weekly `--reap` (safe-only) needs no supervision.
- **Keep runtime data out of the checkout.** `data/`, `Library/`, `.iso-out/` inside a
  repo means every worktree, every backup and every `git status` pays for them. Point
  them at a path outside the tree via env var.
- **Budget for it.** Agents at scale generate debris continuously. If your repo lives on
  the same volume as your container storage, they race each other to fill it.
- **Watch the ratio.** `du -sh .git` versus `du -sh .` — if `.git` is a rounding error,
  you are not storing a repository, you are storing a spoil heap.

## Reaping is slow — plan for it

Deleting hundreds of GB of small files takes **many minutes**, sometimes longer on a
degraded or nearly-full disk. Run it in the background and watch free space climb rather
than waiting on the command. Live: 165 GB → 259 GB free while only 6 of 28 worktrees had
been removed.

Do **not** run a reap concurrently with heavy container/image work — that combination has
its own failure mode, covered in **`docker-wsl2-build-safety`**.

## Before you claim done

- free space actually moved (`df -h`), not just "the command exited 0"
- `git worktree list` matches what is on disk — run `git worktree prune`
- your archive directory has a `.diff` for every worktree the audit called DIRTY
- you can state which check you used for "unpushed" — if it was `--branches`, redo it
