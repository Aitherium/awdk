---
name: github-agentic-c2
allowed-tools: Bash, Read, Write
description: Use GitHub as the control plane for autonomous agents — issues as the task queue, PRs as the review gate, git as the conflict-resolution protocol, Actions on self-hosted runners as your own compute, and Projects as the board. Load when wiring agents to run unattended work with an audit trail, when deciding what belongs in a public vs private repo, or when someone is about to build a bespoke job queue, dashboard and log store that GitHub already gives away.
argument-hint: [wire an agent loop | public/private split | dispatch a job | audit a run]

---

# GitHub as an agentic control plane

Most agent platforms rebuild, badly, five things GitHub already does well: a task
queue, a review gate, a merge/conflict protocol, a job runner with logs, and a
board humans will actually look at. If your agents run on infrastructure you own,
you can have all five for free and keep the receipts.

This is the assembly. The pieces each have their own skill; this one is the wiring
diagram and the traps that only show up once agents — not people — are driving.

## The mapping

| you need | GitHub primitive | why it beats rolling your own |
|---|---|---|
| task queue | **Issues** (+ labels) | typed, assignable, searchable, commentable, and closes with a linked commit |
| work board | **Projects** | a status view a human will actually open |
| review gate | **Pull requests** | required checks, diffs, line comments, approval as an API call |
| conflict resolution | **git** | three-way merge is a solved problem; do not invent a second one |
| compute | **Actions + self-hosted runners** | your hardware, their scheduler and UI |
| audit trail | run logs + commit history | immutable, timestamped, already retained |
| artifacts | Releases / Pages / Packages | versioned hosting you do not operate |

The payoff is that **every agent action lands somewhere a human can review later**
without you building a single dashboard. An agent that opens an issue, pushes a
branch, opens a PR and lets checks run has produced a complete, legible record as
a side effect of doing the work.

## The loop

```
issue labelled agent:todo
   -> workflow triggers (or an agent polls `gh issue list`)
   -> agent works on a branch
   -> opens a PR, checks run on your runner
   -> human approves (or an auto-merge rule does)
   -> merge closes the issue, logs persist
```

Dispatch, from anything that can call `gh`:

```bash
# hand an agent a task
gh issue create --title "migrate billing retries" --label agent:todo --body-file spec.md

# what is queued
gh issue list --label agent:todo --json number,title --limit 50

# trigger a job explicitly
gh workflow run agent-task.yml -f issue=1234

# read the outcome
gh run list --workflow agent-task.yml --limit 5
gh run view <id> --log-failed
```

## Self-hosted runners: the actual lifehack

A self-hosted runner turns "I have a machine" into "I have CI/CD, a job scheduler,
a secret store and a log viewer." You register the machine once; from then on any
workflow targeting your label runs on your hardware, with your GPUs, your disks and
your network — scheduled and displayed by GitHub.

Setup and repair live in **`github-runner-fleet`**. The things that bite:

**Labels are the contract.** A job asking for `ubuntu-latest` will *never* match a
self-hosted runner, however many you register. Adding capacity without changing the
`runs-on` label fixes nothing. Read the *job's* labels, not the workflow file you
think it uses.

**A hosted-runner job that cannot be billed fails with zero steps and no log.**
`gh run view --log-failed` answers `log not found`; the tell is an empty
`runner_name` and `steps: []`. That reads as a broken workflow rather than "no
runner took this", which is how it survives for days.

**The runner's toolchain is not free.** A self-hosted box has no preinstalled tool
cache. Steps that "free up disk space" delete things a hosted image would have had.
Provision deliberately; do not hand-install per job.

**One host, many runners, or your queue serialises.** One runner takes one job.
Parallel matrix jobs on a single runner are a queue with extra steps.

**A ghost registration looks exactly like capacity.** An offline-but-registered
runner shows in the API and satisfies nobody's job. Verify with a job that runs,
not with a listing.

## Public vs private: get this split right first

The decision is not "is the code secret" — it is **what does each repo cost and
expose**:

- **Actions minutes are free on public repos, and free on self-hosted runners in
  either.** So a private repo + self-hosted runners costs nothing in minutes.
- **Pages** publishes from either, but a private repo's Pages site may require a
  paid plan to stay private — a public site from a private repo is the usual shape.
- **Secrets never belong in a public repo**, including in workflow logs. Treat
  anything echoed in a step as published.
- **Forks of public repos do not get your secrets** on `pull_request` — by design.
  A workflow that needs them must not run on untrusted forks.

The practical layout for a solo dev: **private repo for the source and the agent
workflows, public repo (or a public Pages branch) for what the world sees.**
Publish artifacts outward; never invert it.

Site and publishing mechanics: **`repo-to-website`**, **`website-as-code`**,
**`ship-an-app-free`**.

## Workflow hygiene when agents write the workflows

**Pin what an agent may do.** Set `permissions:` explicitly at the job level;
default-broad tokens plus an autonomous author is how a bad turn becomes a force
push. Start read-only and add what a run actually needs.

**`workflow_dispatch` is your API.** It gives an agent a triggerable job with typed
inputs and a permanent log, without exposing a service.

**A workflow that never triggers produces no red run.** Path filters, branch lists
naming a deleted branch, and required checks that report *skipped* all look like
success. Assert the trigger, not just the steps.

**Never let a step swallow a failure into a warning.** An agent loop that reports
success while producing nothing is worse than one that crashes, because nobody
investigates a green run.

**Build once, deploy many.** See **`github-actions-image-pipeline`** — rebuilding
the same image every push is where self-hosted runner time actually goes.

## What this does not give you

Be honest about the edges, or the first surprise becomes distrust:

- **Not real-time.** Actions is a batch scheduler. Sub-second control loops do not
  belong here.
- **Not a database.** Issues are a queue, not state you query hot.
- **Rate limits are real** for a polling agent — prefer webhooks or a dispatch
  trigger over a tight `gh` poll.
- **The audit trail is only as good as the granularity.** One giant "agent did
  stuff" commit records nothing useful. Small commits, real messages.

## See also

- `github-runner-fleet` — stand up, verify and repair the runners
- `github-actions-image-pipeline` — build-once/deploy-many, cache eviction
- `repo-to-website` — a repo to a real Pages site
- `website-as-code` — Pages frontend + a backend on your own machine
- `ship-an-app-free` — the whole free-tier path, start to finish
- `repo-is-not-a-runtime` — why build output must not accumulate in the repo
