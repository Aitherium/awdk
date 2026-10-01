---
name: repo-intake
allowed-tools: Read, Grep, Glob, Bash, Write, Edit, WebFetch
description: Turn "I found this GitHub repo — integrate/adapt/adopt it" into a decided, recorded outcome instead of another unread folder in a research folder. Use whenever the owner drops a repo URL, a .zip, or a checked-out clone and asks to absorb it. Runs a no-execute census, mines the design seams, maps every idea against what AitherOS ALREADY has, and forces a per-idea verdict (ADOPT / ADAPT / REFERENCE / REJECT) with a license gate. Do NOT use for adding a normal dependency (that's /dependencies) or for reverse-engineering a customer SaaS from a HAR (that's integration-intake).
argument-hint: [<repo-url> | <path-to-zip> | <checked-out-dir>]

---

# Repo intake — from "found a repo" to a decided outcome

The recurring job: owner finds a project, drops the URL/zip, says *integrate / adapt /
adopt*. Done ad hoc, it produces a folder in `research/` that nobody reads again.
**That has already happened four times here** — `12-factor-agents-main`,
`agentcontrolplane-main`, `humanlayer-main`, `forge-v1-recovered` all sit checked out
with no dossier and no adoption record. The code was never the deliverable. **The
decision is the deliverable.**

So the output of this skill is always the same artifact: a dossier at
`research/intake/<repo>/DOSSIER.md` where every borrowed idea has a verdict, an owner,
and a landing place. No dossier = the intake did not happen.

## The default is REFERENCE, not ADOPT

Most repos are worth one page of notes, not a merge. Start every idea at **REFERENCE**
and make it earn its way up. An idea only becomes ADOPT/ADAPT if you can name:
1. the AitherOS file it lands in, and
2. what breaks today that it fixes.

If you can't name both, it's REFERENCE. Write it down and move on.

## Phase 0 — Acquire into quarantine (never into the worktree)

Unpack/clone **outside** `the working tree`. This repo runs with hundreds of
uncommitted files, concurrent sessions committing every 2–5 min, and a failing D: drive
— dropping a `node_modules`-bearing tree in the worktree is actively destructive.

```powershell
$Q = "$env:TEMP\claude\repo-intake"
python tools/repo_census.py <url|zip|dir> `
  --quarantine $Q --markdown --out "$Q/<repo>-census.json"
```

**Never run the target's `npm install` / `pip install` / build / tests before Phase 2.**
An unread repo is untrusted input; its install scripts execute arbitrary code. Read
first, run later (and when you do run it, run it in a container).

`repo_census.py` gives you, without executing anything: size, LOC by directory, license
+ whether it is vendorable, dependencies, docs, git staleness, and a ranked list of
**seam files** — the interfaces/registries/guards/policies that carry the design.

## Phase 1 — Read the seams, not the repo

Read in this order and stop when the design is clear. For a typical 5k-LOC project this
is 6–10 files, not 60:

1. `docs/architecture.md` / `README` — the author's own claim about what's novel.
2. The **types/interface** files — where the seams are declared.
3. The **guard / policy / gate** files — where the invariants live. This is almost
   always where the genuinely transferable idea is.
4. One end-to-end path (entrypoint → queue → executor) to check the docs aren't lying.

Skip: UI, tests, generated code, vendored deps, config plumbing.

## Phase 2 — Map every idea against what we ALREADY have

This is the step that decides the intake, and the one that gets skipped. For each idea,
fill one row. **"Ours today" must cite a real file** — `mcp_governed_build.py`,
`_mcp_scoping.py` — not a vibe. Use `codegraph_search` / `repowise_search` (MCP) to find
our equivalent before claiming we lack one; this repo is ~1500 tools deep and "we don't
have that" is usually wrong.

> **A search that times out is not evidence of absence.** D: is a failing drive and
> full-tree `rg` regularly dies at 20s. The open-polsia intake filed a whole ADAPT item on
> "we don't scope tools per agent role" after a timed-out grep — then a scoped search found
> `filter_tools_for_agent()`, `enforce_tool_scope()`, and 26 passing tests. **A claimed gap
> is not a finding until a search that COMPLETED failed to find our version.** If a search
> times out, narrow the path and run it again; never downgrade it to "unverified" and file
> the row anyway.
>
> **The same rule kills `2>/dev/null` on a discovery grep.** GitHub zips often contain a
> doubled wrapper dir (`repo-main/repo-main/`). The lightpanda intake ran three capability
> greps against a `src/` that did not exist; `2>/dev/null` swallowed "No such file" and all
> three returned a clean, confident "absent" — one of which was flatly wrong. **A suppressed
> error and a timed-out search are the same bug: a negative result from a search that never
> ran.** Before trusting any "not found", `ls` the directory you searched. Let stderr show.

| Their idea | Ours today (file) | Theirs better? | Verdict |
|---|---|---|---|

## Phase 3 — Verdict, one per idea

| Verdict | Means | Required |
|---|---|---|
| **ADOPT** | Vendor their code | License permits it + attribution + a `research/intake/<repo>/` provenance note |
| **ADAPT** | Reimplement the idea on our primitives | Named target file + named owner agent |
| **REFERENCE** | Write it down, build nothing | One paragraph in the dossier. This is a success, not a cop-out — **but only when it means "not worth building", never "I didn't check"** |
| **REJECT** | Explicitly not doing it | The reason — so the next session doesn't re-litigate it |

> **"REFERENCE (unverified)" is not a verdict — it is an unfinished check.** Both intakes
> so far hid their best finding in a row marked unverified. lightpanda #4 looked like a
> feature we might be missing; verifying it showed ours has a caller-ownership gate theirs
> lacks, making *their* design a cross-tenant hazard. That inversion is the finding, and it
> only appears if you look. Before writing "unverified", spend the one scoped search — and
> if you genuinely cannot resolve it, say so in the self-skeptic section as an open
> question, not as a disposition.

### The license gate (blocking, before any ADOPT)

The census reports this. Honor it:
- **MIT / Apache-2.0 / BSD / ISC / MPL-2.0** → vendoring allowed, attribution required.
- **AGPL / GPL / LGPL / SSPL / BUSL** → **do NOT vendor into the platform.** Downgrade
  to ADAPT (reimplement the idea, don't copy the expression) or run it as a separate
  process behind an API. Record which you chose.
- **No LICENSE file** → all-rights-reserved. REFERENCE only.

Never paste a licensed file into your shared library tree without recording where it came from.

### The vendoring gates (blocking, on every ADOPT)

The license gate says whether you *may* copy. These say whether what you copied is
*correct*. Every one was paid for on 2026-07-25 during the ODS intake — twice in the
same package, hours apart.

**1. Grep the upstream tree for a reference implementation BEFORE writing a line.**
```bash
grep -rl "<the-data-file-you-are-about-to-parse>" <upstream-tree>
```
A data file tells you the shape, never the tie-breaks. Twice in one intake, code was
derived from ODS's JSON while ODS shipped the script that already read it:
`select-model.py` (the reimplementation returned a **different model in 16 of 20**
envelopes) and `classify-hardware.sh` (the fresh classifier used first-match instead of
upstream's longest-match guard, ignored `device_id`, and searched only the GPU name — so
a real Strix Halo, identified by its **CPU** string, missed its device entry and was
sized from a bogus VRAM reading). Both had a fully green unit suite.

**2. Prefer carrying the code over porting it.** Pure-stdlib Python imports directly.
Bash-wrapped Python is still usable as a reference without bash: extract the heredoc and
run it with the same argv.

**3. Byte-identity is a GATE, not a review step.**
```bash
cmp <upstream-file> <vendored-copy>          # before anything else
sha256sum <vendored-copy>                    # record it in code
```
Pin every vendored file's sha256 in a module constant with a `--verify-vendored` command,
and add `.gitattributes` `text eol=lf` for each — `core.autocrlf` rewrites them on
checkout and turns the tamper-detector into a guaranteed false alarm.

**4. Differential-test the port against the original.** Not "does it look right" —
execute upstream and compare outputs across a matrix that spans every code path
(assert the matrix covers them; matrices degenerate silently).

**5. Never let the agent that built it be the only one to verify it.** In this intake a
build subagent authored a 6-model placeholder with `"gguf_sha256": "abc123def456"` instead
of copying the real 52-model catalog, the verification subagent reported `passed: true`
against it, and a fix subagent "resolved" a `REPLACE_WITH_*` marker by **inventing a
plausible commit SHA**. A fabricated-but-plausible provenance id is worse than a visible
placeholder — publish `None` and anchor on per-file hashes instead.

**6. Vendored data you never read is not integration.** If a file is shipped and pinned
but nothing consumes it, either wire it to a real decision or drop it — and check whether
it was covering a live bug (here, an unread hardware DB meant a tier was hardcoded, which
made an upstream hardware-safety substitution structurally unreachable).

## Phase 4 — Land it

1. **Write the dossier** — copy `templates/DOSSIER.md`, fill it, save to
   `research/intake/<repo>/DOSSIER.md`. Commit that path *only* (pathspec commit — see
   the `concurrent-safe-git` skill; other sessions are committing right now).
2. **ADAPT/ADOPT items become real work** — Spec Ledger (`spec_ingest`) or GitHub issues
   via `/project-manager`. An idea with no ticket is a REFERENCE, so mark it one.
3. **Debt ledger** — anything you found broken-but-out-of-scope gets a `TECH_DEBT.md`
   row (allocate the id with your ledger's id tool immediately before appending, so concurrent sessions cannot collide).
4. **Memory** — if the intake changed a program direction, write it to memory. That is
   what makes the *next* intake cheaper.

### Landing-place trap: `.WORKFORCE` is a submodule

`.WORKFORCE` is `github.com/Aitherium/workforce.git` mounted as a submodule. Editing
inside it is a **commit to a different repo** plus a pointer bump here. Agent/company-brain
ideas belong there; platform primitives belong in `AitherOS/`. Decide which in the
dossier and say so explicitly — do not let the split happen by accident.

## Scope control

One session, one repo. If the intake surfaces a second repo worth absorbing, it gets its
own dossier — do not chain intakes. And when the verdict for everything is REFERENCE,
say so plainly and stop; a one-page dossier that prevents a pointless rewrite is the
highest-value outcome this skill produces.
