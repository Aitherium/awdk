---
name: agent-disk-hygiene
description: >-
  Your coding agents are quietly eating your disk. Nobody tells you this when you start running agents at scale: they generate enormous amounts of disk garbage, in places you will never think to look. Not model weights — *debris*. Throwaway git worktrees, build tarballs, ISO output, caches inside caches.
---
<!-- Generated from the awskills pack (skills/agent-disk-hygiene.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

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
agent-worktree-reaper.sh                        # audit — read-only
agent-worktree-reaper.sh --reap                 # remove only the SAFE ones
agent-worktree-reaper.sh --reap --all           # dirty ones too, each snapshotted first
agent-worktree-reaper.sh --self-test            # prove the gate on a scratch repo
git worktree add <dir> reclaim/<name>           # restore a snapshotted one
```

The gate: a worktree is safe only with **no uncommitted changes**, **no unpushed
commits**, and **no activity for `--min-idle-hours`** (default 2).

```bash
git --no-optional-locks -C "$wt" status --porcelain   # untracked COUNTED; only caches skipped
git -C "$wt" log HEAD --not --remotes                 # commits missing from every remote
```

🪤 **The trap that makes this look impossible.** `git log --branches --not --remotes`
seems like the natural check and is **wrong**. Worktrees share one object store, so
`--branches` enumerates *every branch in the repo* and returns an identical number for
every worktree. Live: the broken form reported "209 unpushed" for all 32 — so everything
looked unsafe and nothing would ever be cleaned. Anchoring on `HEAD` gave 0 for 30 of
them. **If your audit says every worktree is dirty, suspect your query, not your disk.**

🪤 **Untracked files are work.** An agent's new file stays untracked until it commits.
This skill used to say "use `-uno`" and archive `git diff HEAD`. Neither sees untracked
files, so archive-then-delete destroyed exactly the newest work. Live (2026-10-04): a
reaped worktree held a 376-line test that existed only as an untracked file. Skip
untracked files only under pure caches (`node_modules`, `__pycache__`, `.venv`, tool
caches). Not `build/` or `dist/`: an untracked `build/deploy.sh` is someone's file, and
an audit of this skill's own reaper deleted exactly that before it was narrowed.

🪤 **What status never shows.** Gitignored files (`.env`, a local database) appear in no
`status --porcelain` and in no snapshot, and `git worktree remove --force` deletes them.
Check `git status --porcelain --ignored` (`!!` lines) and keep any worktree whose ignored
files are not build output, or copy them out first (`--archive DIR`). Two more silent
losses: a detached HEAD's unpushed commits become unreachable (give them a ref before
removal), and a worktree git cannot read must count as UNKNOWN, never as clean.

🪤 **Git activity is not activity.** A session editing files without staging touches no
git stamp for hours. Judge idle time by the newest of the HEAD reflog, the index **and
the changed files' own mtimes**. Read status with `--no-optional-locks`: a plain
`git status` rewrites the index, so your own audit resets the clock it reads.

**Snapshot, then delete.** For a dirty worktree, commit its tracked and untracked changes
through a throwaway index into a ref, check every changed path is in it, and only then
remove the checkout. The worktree's own index and branch are never touched:

```bash
idx="$(git -C "$wt" rev-parse --absolute-git-dir)/reaper-index.tmp"
GIT_INDEX_FILE="$idx" git -C "$wt" read-tree HEAD
GIT_INDEX_FILE="$idx" git -C "$wt" add -A -- . ':(exclude,glob)**/node_modules/**'
tree=$(GIT_INDEX_FILE="$idx" git -C "$wt" write-tree)
sha=$(git -C "$wt" commit-tree "$tree" -p HEAD -m "reclaim snapshot")
git update-ref "refs/heads/reclaim/$name" "$sha"
GIT_INDEX_FILE="$idx" git -C "$wt" diff --name-only "$sha"   # must print nothing
```

🪤 Verify against the throwaway index. Against the real one, `git diff <sha>` reports
every untracked file as *deleted*, and your verify step refuses every worktree.
🪤 On Git Bash for Windows, set `MSYS_NO_PATHCONV=1`: it rewrites a `sha:path` argument
as if it were a file path.

The snapshots are refs in the same object store, so they cost almost nothing: 51 reaped
worktrees (about 80 GB) became 51 commits.

**Use `git worktree remove`, not `rm -rf`.** A bare `rm -rf` leaves the registration
behind, so `git worktree list` keeps reporting trees that no longer exist. Follow with
`git worktree prune`. (Live drift seen: 32 directories on disk, 58 registered.)

## Stop it recurring

- **Reap on a schedule, and check the schedule.** Hourly `--reap` (safe-only) needs no
  supervision; add `--all` when the drive is under pressure. Live: a guard was changed
  from every 4 h to hourly in code, but the installed task was never re-registered
  (that needs elevation), so it kept running every 4 h, and the drive went from 60 GB
  to 10 MB between two runs. Read the trigger back from the scheduler, not from your script.
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
- every DIRTY worktree you removed has a `reclaim/<name>` ref, and its untracked files are in it
  (`git ls-tree -r --name-only reclaim/<name> -- <file>`)
- you can state which check you used for "unpushed" — if it was `--branches`, redo it
