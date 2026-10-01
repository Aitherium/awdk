---
name: awgit-claude-code
allowed-tools: Bash, PowerShell, Read, Write
description: "The awgit workflow for Claude Code / an Aither adk agent — how to work when every commit is captured as a semantic edit-op: what to check after a commit, how to read the attribution ledger, how to teleport deltas to a peer node, and how to find/merge node-level conflicts."
argument-hint: "[--repo <path>]"
---

## Context
- awgit (Aither World-Graph git) is set up (see `/awgit-setup`): every commit fires
  a post-commit hook that captures a semantic edit-op — which functions/classes/
  methods changed, bodies by content address, the verified GitHub identity.
- The world model IS the database: nodes are the records, ops are the transactions,
  git is the snapshot/transport.
- The durable store is at `~/.aither/awgit/data` (or `$VCS_DATA_ROOT`) — outside
  the repo, so it never pollutes clones or the tree.

## Your Role
You are an agent (Claude Code, or an adk agent) working on a repo with awgit
installed. The post-commit hook captures your commits automatically — you don't run
`awgit capture` yourself. Your job is to READ the semantic layer it produces, record
attribution for work you did, and use the sync/merge/dedupe surfaces when the task
needs them.

## Before you EDIT a shared file — take a lease

If another agent may touch the same file, claim it first. This is the half that
prevents damage rather than describing it afterwards:

```bash
awgit lease list                 # who holds what RIGHT NOW — check before editing
awgit lease acquire <paths>      # claim what you are about to change
awgit lease acquire --staged     # claim everything staged that the gate guards
awgit lease release <ids>        # by LEASE ID, not path (they also expire)
```

Your actor is derived automatically per session, so nothing needs configuring.
Use `--actor <name>` only to override it — and never to a name another agent
also uses, or their lease covers your commit: all of the friction, none of the
protection.

Where `VCS_LEASES_ENFORCE=1`, the pre-commit hook REJECTS a commit whose staged
guarded files you do not hold: `no active lease covering: <path>`. You are not
stuck — run `awgit lease acquire --staged` and commit again. If instead you see
`lease conflict … held by <actor>`, another agent is genuinely in that file:
coordinate, or wait for the lease to expire. Do not force past it.

**Take the lease BEFORE you edit — it snapshots a baseline.** `acquire` records the
file as a git blob at lease time, which is what makes `awgit stage-mine` able to
separate *your* diff from work that was already in the file. Lease after you edit
and that separation is no longer possible.

```bash
awgit lease acquire --reason "..." <file>   # BEFORE editing — captures the baseline
awgit stage-mine <file> --require "<the line that wires your change up>"
```

Measured 2026-08-10: two commits in one hour landed 150 and 1415 insertions for
~15- and ~60-line edits, swallowing another agent's unfinished work — because a
lease alone says "I claim this going forward" and says nothing about separating
what you wrote from what was already there. `stage-mine` is what answers that.

**Leases are opt-in, and that limit is real:** an agent that never calls `acquire`
is invisible to the plane, and an edit another actor makes *after* your lease falls
inside your diff window.

**Know what the gate guards, because a gate that guards nothing still prints OK.**
It covers source (`.py .ts .tsx .js .jsx .mjs`), config (`.yml .yaml .toml .json
.ini .cfg`), scripts (`.sh .bash .ps1 .psm1`), `.md`, and `.sql .proto .env`. It was
`.py`-ONLY until 2026-08-09 — so every `.yml` and `.md` commit printed
`vcs: lease-check OK` while checking **nothing at all**, for weeks. A green line from
a gate is only worth what the gate's scope is; check the scope before trusting it.

### The lease is not the whole story — the commit FORM matters too

A lease tells you a peer is in a file. It does not stop your commit from swallowing
their work, and the two most common commit forms each have an opposite failure:

- `git commit -m "…" -- <paths>` ignores what a peer has **staged** — but it commits
  the **working tree** for those paths, so it sweeps their **unstaged** hunks. It does
  this *even if you carefully staged only your own hunks first*: the `-- <paths>`
  suffix overrides your index entirely.
- `git commit -m "…"` with no pathspec commits the whole **index**, so any file a peer
  has staged rides along.

So: if the file carries no foreign hunks, use the pathspec form. If it does, stage only
your hunks (`git apply --cached` a filtered patch), **then commit WITHOUT the pathspec**
— and read `git diff --cached --stat` as a checkpoint first, because dropping the
pathspec re-exposes the other hazard. Verify with `git show --stat` after either.

## Seeing the whole picture — `awgit graph`

```bash
awgit graph                                  # mermaid: files, nodes, who touched what
awgit graph --format json --out graph.json   # node/edge form for a graph store
```

Files render as subgraphs with their code nodes inside; actors get edges into
what they touched, and a node **two or more actors touched is drawn as a
collision**. That is the only part of the picture that means something is wrong.

## Languages

Python is understood natively. Everything else — TypeScript, Go, C#, and 70+
other extensions — needs the optional parser:

```bash
pip install "awgit[multilang]"
```

Without it awgit still works and still guards Python; non-Python files simply
carry no node identity, so they get a lease but no semantic diff.

## After every commit — the checklist
1. **What changed, and who** — the semantic view:
   ```bash
   awgit status                 # op count, body store, coverage
   awgit diff <parent> <head>   # node-level diff: which functions, what kind of change
   ```
   Read the diff like a reviewer: did the commit touch the function you meant? Did
   it silently rewrite an unrelated node (a spurious module rewrite signals a file
   whose top-level constants moved)?
2. **Check the attribution** — who changed what, under a verified identity:
   ```bash
   awgit ledger --sha <sha>             # the op: actor, verified identity, what changed
   ```
   Each op carries a deterministic `ledger_ref` — a stable handle for the work. The
   op never blocked the commit; the ledger is a record a reward program can attach
   to later.
3. **Conflict check** (before a merge, or when a merge lands):
   ```bash
   awgit merge-preview <base> <head>    # node-granularity preview
   awgit merge-conflicts                # collisions escalated to a human
   ```
   Disjoint node sets merge clean by construction. A conflict names the exact
   function with both bodies and the blast radius — resolve it deliberately, never
   by blindly taking a side.

## Teleporting to a peer node (differential sync)
A peer node that has your op-log up to a point catches up with the delta, not the
tree:
```bash
# on your side — what the peer is missing
awgit sync export --known <peer-known-op-ids> -o delta.json
# on the peer — idempotent; a bundle applied twice converges
awgit sync import delta.json
```
A caught-up peer gets a tiny delta; a fresh endpoint gets the full clone. Bodies are
content-addressed, so a body the peer already hosts is never re-sent.

## Disk duplication (the worktrees-on-disk problem)
awgit's body store is content-addressed — identical bodies across commits, branches
and worktrees collapse to one blob. For the working trees themselves:
```bash
awgit dedupe --scan <trees...>          # quantify byte-identical duplication
awgit dedupe --reclaim <trees...> --apply   # hard-link identical files (same filesystem only)
```
Reclaim never touches git-tracked files and never walks `.git` internals. Dry-run by
default — `--apply` is the explicit act.

## Coordinating with other agents (leases)
When two agents may edit the same function concurrently:
```bash
awgit lease acquire <targets...>        # all-or-nothing
awgit lease heartbeat <id>              # renew the TTL
awgit lease release <id>
```
Leases are heartbeat-renewed TTLs: a vanished agent's leases free their targets on
their own. Unleased ops are flagged `leased=false`, visible in `awgit status`, never
silently trusted.

**`VCS_LEASES_ENFORCE=1` is the ON switch, and it is not hypothetical.** This page
used to say enforcement was "off by default" three paragraphs after saying the hook
REJECTS unleased commits — the two halves of one file disagreeing about whether the
safety gate fires. Check yours rather than assuming either answer:

```bash
awgit lease-check --help              # the gate exists
git config --get core.hooksPath ; ls .git/hooks/pre-commit.d/   # it is installed
```

## Notes
- **The lease gate CAN block a commit — that is the point of it.** An unleased staged
  guarded file exits 1 with `vcs: commit rejected — no active lease covering: <path>`.
  The *capture* half never blocks and never rewrites your bytes; if a capture looks
  like it did, check `.git/vcs-capture.log` — that hook skips loudly, never silently.
  Do not conflate the two: "awgit never blocks a commit" was written about capture and
  read as being about leases, which is how an agent learns to treat a rejection as a
  bug in the tool instead of as the tool working.
- Every op carries a deterministic `ledger_ref` — a stable handle a downstream
  reward program can attach to the work later. The op-log records attribution; it
  never gates a commit.
