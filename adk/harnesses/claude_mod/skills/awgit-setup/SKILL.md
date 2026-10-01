---
name: awgit-setup
allowed-tools: Bash, PowerShell, Read, Write
description: Set up awgit (Aither World-Graph git) on a machine — the semantic version-control layer that turns every git commit into a function-level edit-op with a verified GitHub identity, a durable attribution record, and differential sync. Install, init, install the capture hooks, verify.
argument-hint: "[--force] [--repo <path>]"
---

## Context
- awgit = a package that rides on top of git. git stays the byte-truth; awgit adds the world model: stable function-level node ids, an op-log, content-addressed bodies, differential sync.
- It is a Python package (`awgit`), console command `awgit`, Python 3.10+.
- The durable store lives OUTSIDE the git tree at `~/.aither/awgit/data` (override with `VCS_DATA_ROOT`).

## Your Role
You set awgit up on a developer's machine or a repo: install it, verify the actor's
GitHub identity, install the chained capture hook, and prove capture is firing. The
setup is complete only when a real `git commit` produces a captured edit-op — a hook
that is installed but silent is not set up.

## Your Task

### 1. Install
```bash
pip install awgit            # PyPI (when published)
# or from source:
git clone https://github.com/aitherium/awgit && cd awgit && pip install -e .
```
Verify: `awgit --help` lists `capture diff status graph evidence merge-preview
merge-conflicts resolve-conflict lease lease-check bodies dedupe ledger sync hooks`.
(`graph`, `evidence` and `hooks` were missing from this line, so anyone comparing
their real output against it saw a mismatch and had to guess whether their install
was broken. It was not.)

### 2. Verify the actor (GitHub identity)
The actor on every op is the box's VERIFIED GitHub login (via `gh`), resolved
automatically and cached 6h. If `gh` is installed and authed, capture records
`actor_verified=true` and `verified_actor=<login>`. Best-effort: capture never
depends on it.

```bash
gh auth status                     # must be "Logged in to github.com"
```
If `gh` is missing or unauth, capture still works — the actor falls back to the
git commit author and is recorded unverified.

### 3. Install the capture hook
```bash
awgit hooks install
```
This wraps the repo's existing `pre-commit` / `post-commit` hooks with a CHAIN
(`chain.sh`): the original hook body (moved to `<hook>.org`) runs first, then the
`.d` fragments. Your existing gates (a secret scanner, a CD-autosync trigger) are
PRESERVED — the chain propagates the first non-zero exit. Installing twice is
idempotent.

### 4. Prove capture is firing
Make a real commit that edits a Python function, then:
```bash
awgit status
```
Expect `N ops`. The captured op records which function changed, the old/new bodies
by content address, the actor, and the verified GitHub identity. If `status` shows
0 ops, check the hook log (skip paths are LOUD, never silent):
```bash
cat .git/vcs-capture.log
```

### 5. Use it
```bash
awgit diff <sha> <sha>            # node-level diff between two commits
awgit merge-preview <a> <b>       # merge preview at node granularity
awgit ledger --sha <sha>          # attribution: who changed what, verified identity
awgit sync export -o delta.json   # teleport the delta to a peer node
awgit dedupe --scan .             # quantify byte-identical disk duplication
```

## Common troubleshooting
- **0 ops after a commit** — read `.git/vcs-capture.log`; the fragment logs why it
  skipped (no sha, not a worktree, CLI missing). A silent skip is a bug — the skip
  paths are deliberately LOUD.
- **`command not found: awgit`** — the console script wasn't installed (re-check
  step 1) or the venv isn't on PATH.
- **actor shows `git-author` / unverified** — `gh` isn't authed or the box has no
  verified GitHub login; capture still works, attribution is just best-effort.
- **store location** — the op-log lives at `~/.aither/awgit/data`, not in the repo.
  To move it, set `VCS_DATA_ROOT` and re-capture.

## Done
- [ ] `awgit --help` lists all 12 subcommands
- [ ] `gh auth status` logged in (or consciously accepted the unverified fallback)
- [ ] `awgit hooks install` reported hooks installed
- [ ] a real commit editing a `.py` function → `awgit status` shows the op
