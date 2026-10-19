---
name: worktrees
description: "Make, list and retire agent git worktrees through `awgit wt` — never raw `git worktree add/remove`. Load before creating a worktree, when a drive is full of old ones, or to get back work from a retired one."
---
<!-- Generated from the awskills pack (skills/worktrees.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# Worktrees — awgit owns the lifecycle

Raw `git worktree add` makes a tree nobody tracks, so nobody retires it: a drive fills
under hundreds of forgotten agent worktrees. Use these instead.

```bash
awgit wt new <name> --sparse <dirs...> --purpose "<why>"   # under the first configured root,
                                                           # base = trunk, registry row written
awgit wt status [--size] [--all]                           # registered? idle, dirty, pushed, LIVE
awgit wt done <name>                                       # snapshot unpushed work, remove, ledger
awgit wt restore <name>                                    # bring a retired tree back (work uncommitted)
awgit wt pin <name> [--off]                                # keep a tree from the reaper
awgit wt adopt [--all]                                     # register trees made behind awgit's back
```

What `done` and the reaper guarantee:

- **Nothing unpushed is lost.** Tracked edits, staged work, untracked files and non-build
  ignored files are committed to `refs/heads/reclaim/<name>` through a PRIVATE index (the
  tree's own index is never touched) and read back before anything is removed. Branches
  are never deleted.
- **Junctions/symlinks are cut as links first** — a forced remove recurses THROUGH a
  junction (a shared `node_modules`) and empties its target.
- **Secret-shaped files never enter a ref.** A tree holding one is kept; a harness COPY of
  a file the main tree has canonically (a copied `.env`) is dropped with the tree.
- Each retirement is a ledger line (`~/.aither/awgit/data/worktrees/reaped.jsonl`) and an
  memory fact (`worktree-reaped:<name>`) when a recorder is configured.

**The reaper** is the awrise job `worktree-reap` (hourly): `awgit wt reap --scheduled`
adopts unregistered trees, then retires trees idle 48 h (6 h when that tree's DRIVE is
under its floor, default 50 GB) with no live awgit session, active lease or pin, and no
file edited inside the threshold. Roots and thresholds: `~/.aither/awgit/worktrees.json`
(`$AWGIT_WT_SETTINGS`). Plan without removing: `awgit wt reap --scheduled --dry-run`.
A lifecycle check asserts these rules.
