---
name: github-actions-image-pipeline
allowed-tools: Read, Grep, Glob, Bash, PowerShell
description: Run GitHub Actions image pipelines without rebuilding the same images on every push, or paying an hour-long cold build for a typo. The build-once/deploy-many architecture, why GitHub Actions' 10GB cache silently evicts your base image so every build is cold, the disk-reclaim step an ML base needs on a hosted runner, and the chain of never-exercised steps that each fail once your workflow finally runs. Written from a real deploy pipeline that went from 50-minute failing builds to seconds.
argument-hint: [audit | build-once | cache | disk | never-run]

---

# GitHub Actions image pipeline

**One workflow builds your images. Every other workflow pulls them.** The moment a
deploy job rebuilds an image that another workflow already built and pushed, you have
signed up to pay for the same compile on every push — and on GitHub-hosted runners the
cost isn't minutes, it's an hour.

The pipeline this skill came from rebuilt **Genesis + base-services on every main
push**, taking **40–56 minutes** each time, and failing at a different never-before-run
step on every attempt. After the rebuild, the deploy step completed in **seconds**. The
whole saga is a chain of "this code path had never executed until today" failures, each
one a real, measurable defect.

---

## The core rule: build once, deploy many

GitHub Actions has no cross-workflow "this already built it" dependency. Two workflows
that both trigger on `push` to `main` run **in parallel**. If both build the same image,
you build it twice on every push — and if one of those builds is a 56-minute ML base,
you pay twice every push.

**The fix is a division of labor, not better caching:**

| Workflow | Job | What it does |
|---|---|---|
| `docker.yml` (the builder) | `base-images`, `service-layers`, `specialized-images` | Builds + pushes every image, gated to `main`/`develop`/release, with a registry-backed `:buildcache` |
| `ring-deploy.yml` (the deployer) | `deploy-prod` | **Pulls** `:latest` and retags to `:prod` + `prod-<sha>` via `docker buildx imagetools create` — manifest-only, **seconds**, no layer transfer |

The retag is the key trick most people miss. You don't need `docker pull` + `docker tag`
+ `docker push` (which transfers layers — as slow as building). `docker buildx
imagetools create <src>:latest --tag <dst>:prod` copies the **manifest** in GHCR
without touching the layers. A 56-minute image becomes a 3-second retag.

**Downstream contract:** your ECS/Deploy step only needs the `:prod` tags to exist
before it force-redeploys — it never references a locally-built image. So pull+retag
preserves the exact contract while skipping the build.

### Waiting for the builder

Because the builder and deployer run in parallel, the deployer must **wait for the
builder to publish this commit's images** before retagging. A `github-script` step
polls the Actions API:

```yaml
- name: Wait for docker.yml to publish this commit's images
  uses: actions/github-script@v7
  with:
    script: |
      const { owner, repo } = context.repo
      const sha = context.sha
      let run = null
      for (let i = 0; i < 6 && !run; i++) {
        const { data } = await github.rest.actions.listWorkflowRunsForRepo({
          owner, repo, workflow_id: 'docker.yml', head_sha: sha, per_page: 1,
        })
        run = data.workflow_runs?.[0] ?? null
        if (!run) await new Promise(r => setTimeout(r, 10000))
      }
      if (!run) { core.warning(`no docker.yml run for ${sha} — will pull last :latest`); return }
      const deadline = Date.now() + 40 * 60 * 1000
      let status = run.status
      while (Date.now() < deadline && status !== 'completed') {
        await new Promise(r => setTimeout(r, 30000))
        const { data } = await github.rest.actions.getWorkflowRun({ owner, repo, run_id: run.id })
        status = data.status
      }
```

This step needs **`actions: read`** in the job's `permissions` block — without it the
script dies with `Resource not accessible by integration`, which reads like an auth
failure but is just a missing permission grant.

---

## The cache trap: GitHub Actions cache is 10GB and gets evicted

`cache-from/cache-to: type=gha` sounds fine and is the default people reach for. It is
**capped at 10GB per repository and LRU-evicted**. A base image built with
`mode=max` plus twelve service layers plus specialized images far exceeds 10GB, so the
base cache is **evicted between runs** → every build is cold → "the cache never warms."

**Use a registry-backed cache instead:**

```yaml
cache-from: type=registry,ref=${{ env.IMAGE_PREFIX }}-base:buildcache
cache-to:   type=registry,ref=${{ env.IMAGE_PREFIX }}-base:buildcache,mode=max
```

A dedicated `:buildcache` tag in GHCR has **no eviction limit** and persists. This is
what turns a 40-minute cold rebuild into a warm one — and it's the same tag both
workflows can share, so the builder warms the cache the deployer's pull benefits from.

---

## The disk trap: an ML base doesn't fit a hosted runner's default disk

A base image that installs torch (526MB wheel) + scipy + sentence-transformers exhausts
a GitHub-hosted runner's ~14GB free disk. The build dies mid-`pip install` with
`No space left on device` — after 50 minutes of building. Hosted runners ship dotnet,
GHC, Android SDK, CodeQL, and boost you don't need; reclaiming them frees ~25GB:

```yaml
- name: Free disk space (reclaim ~25GB — ML base hits 'no space left')
  if: runner.environment == 'github-hosted'
  run: |
    sudo rm -rf /usr/share/dotnet /opt/ghc /usr/local/lib/android \
      /opt/hostedtoolcache/CodeQL /usr/local/share/boost "$AGENT_TOOLSDIRECTORY" || true
    sudo docker image prune --all --force || true
    df -h /
```

**The guard matters more than the command.** Use `runner.environment ==
'github-hosted'`, NOT `!contains(runner.labels, 'self-hosted')`. `labels` is **not a
real property of the runner context** — the expression silently evaluates true, so the
step runs **on your self-hosted runner too**, and `docker image prune --all` wipes its
entire image store. That exact bug destroyed the build cache and caused the cold
rebuilds in the first place.

---

## The never-exercised-path chain

Here is the most transferable lesson, and it is the reason this skill exists. When a
workflow **fails to load**, GitHub marks it with *"This run likely failed because of a
workflow file issue"* and **no job ever runs**. Every step in that workflow — including
steps that have been broken for months — is never exercised. Fix the load failure, and
each step now runs for the first time, in order, and **each one fails on its own latent
bug**.

The real chain, each measured:

1. **Workflow won't load** → `secrets.CHROME_EXTENSION_ID` in a step-level `if:` — the
   `secrets` context is invalid in step `if:` (only `env`, `with`, job-level `if`).
   Also: a duplicate `env:` key in one step, and a `${{ }}` expression split across a JS
   string concat. GitHub fails the file at parse, before any job.
2. **Shallow checkout** → a step running `git rev-parse HEAD~1` on a default depth-1
   checkout prints the literal name AND fails, so the `|| git rev-parse HEAD` fallback
   *also* runs, and the two-line value breaks `$GITHUB_ENV` with `Invalid format`. Fix:
   `fetch-depth: 0` on that job's checkout.
3. **Missing `packages: write`** → a job that pushes images inherits the workflow-level
   `packages: read` → `installation not allowed to Write organization package`. Fix: a
   job-level `permissions: packages: write`.
4. **Wrong `target:`** → `target: slim` when the Dockerfile defines only
   `base`/`base-ml`/`base-browser` → `target stage "slim" could not be found`.
5. **Unqualified base ARG** → `FROM ${PYTHON_BASE}` with a default that has no registry
   → resolves to Docker Hub → `pull access denied`. Fix: pass `build-args: PYTHON_BASE=<ghcr-qualified>`.
6. **Evicted cache** → covered above.
7. **Disk full** → covered above.
8. **Wrong Dockerfile path** → `file: apps/AitherWorkspace/Dockerfile` when the app's
   actual directory is `AitherPortal` → `lstat ... no such file or directory`.

Each fix lets the workflow run one step further and exposes the next. **When you fix a
workflow that was failing to load, expect this chain.** Do not be surprised; plan to
run it to completion in one sitting, and fix every step it reveals.

---

## The audit checklist

When you inherit or debug an image pipeline, run through these — each has a
measurement behind it:

- [ ] Does more than one workflow build the same image? Find one builder, everyone else pulls.
- [ ] Any `cache-from: type=gha` on a big image? Switch to `type=registry,ref=...:buildcache`.
- [ ] Any job that pushes images inheriting `packages: read`? Add job-level `packages: write`.
- [ ] Any step-level `if:` referencing `secrets.`? Move the secret to `env` / job `if`.
- [ ] Any `git rev-parse HEAD~1` (or other history walk) on a job whose checkout lacks `fetch-depth: 0`?
- [ ] Any Dockerfile `FROM` or build-arg with an unqualified image name?
- [ ] Any `target:` in a build-push-action matching a stage the Dockerfile actually defines?
- [ ] Any `file:` path that exists in the repo?
- [ ] Building an ML/sizeable base on a hosted runner without a disk-reclaim step?
- [ ] Any `github-script` calling the Actions API without `actions: read` in `permissions`?
- [ ] Any `runner.labels` guard that should be `runner.environment`?

---

## The one-liner

**One workflow builds. Everyone else pulls and retags.** `docker buildx imagetools
create` makes the pull seconds. Registry cache stops the cold rebuild. Reclaim disk for
ML bases. And when you finally unblock a workflow that never ran, expect every
previously-never-run step to fail on a real latent bug — fix the chain, not just the
first error.
