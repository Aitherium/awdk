---
name: github-artifact-quota
description: >-
  The storage limit that silently freezes your deploys. Your site stops updating. Not breaking — updating. Every page returns 200, every workflow reports green on the commits you looked at, and the content is days old.
---

# github-artifact-quota — the storage limit that silently freezes your deploys

Your site stops updating. Not breaking — **updating**. Every page returns 200, every
workflow reports green on the commits you looked at, and the content is days old.

The cause is an exhausted GitHub Actions **artifact storage quota**, and it is worth its own
skill because every part of it fails quietly: the symptom is invisible from outside, most of
the storage is already-expired artifacts you cannot download, the tooling for clearing it is
one HTTP DELETE at a time, and the meter you would use to check your progress lags by up to
twelve hours.

> Measured on a real repository (2026-08-19): **75.37 GB across 18,362 artifacts**, of which
> **60.40 GB was already marked expired**. Twenty minutes of API calls took it to **4.32 GB**
> and the deploys started flowing again. The lever was sorting by size — **150 deletes
> recovered 10.6 of the last 15.2 GB**; the next 1,850 recovered 0.11 GB.

## Why you will not notice

The standard, correct deploy shape is:

```yaml
deploy:
  needs: build
```

If the build fails you do not publish a broken site, and the live version stays untouched.
That is the design you want. It also means **a failed build produces no outward symptom at
all** — the site is up, it returns 200, it renders, and it is simply the *previous* build,
indefinitely. From outside, "we published nothing today" and "we published something
identical today" are the same observation.

The build fails on its very last step:

```
##[error]Failed to CreateArtifact: Artifact storage quota has been hit.
Unable to upload any new artifacts. Usage is recalculated every 6-12 hours.
```

Everything before it passes — install, lint, tests, the export, the smoke test. Only the
upload fails, and the upload is the last thing before publishing.

## Expired does not mean gone

This is the number that explains the whole situation:

```bash
gh api repos/OWNER/REPO/actions/artifacts --paginate \
  --jq '.artifacts[] | [.size_in_bytes, .expired] | @tsv' \
| awk -F'\t' '{t+=$1; n++; if($2=="true"){e+=$1; en++}}
              END{printf "total:   %6d  %.2f GB\nexpired: %6d  %.2f GB\n",
                         n, t/1073741824, en, e/1073741824}'
```

Expired artifacts **cannot be downloaded** — they are dead to every consumer — and they are
**still counted against the quota that is blocking your deploys**. Four-fifths of the
storage in the case above was in that state, the largest single one 405 MB and six months
old. Nothing cleans them up and nothing says a word.

Check this ratio before you conclude your storage is genuinely in use.

## Storage is metered over TIME, which is why deleting does not fix it instantly

The billing unit is **gigabyte-hours**, not gigabytes:

```bash
gh api "organizations/ORG/settings/billing/usage?year=2026&month=8" \
  --jq '[.usageItems[] | select(.sku=="Actions storage") | .quantity] | add'
```

Two consequences that change how you read the failure:

- **An old artifact is far more expensive than a large one.** A 405 MB file sitting for six
  months has accrued more gigabyte-hours than a 4 GB file uploaded this morning. This is why
  long-expired artifacts dominate — they are billed for every hour they were never deleted.
- **The meter accumulates over the billing period, so a snapshot cannot clear it.** In the
  case above, 37,368 GB-hours month-to-date across ~19 days is an average near 82 GB held
  continuously, while the *current* artifact bytes were already down to 4.32 GB. Both
  numbers are true at once, and the upload gate was still refusing.

**The uploads gate and the bill are separate mechanisms, and only one of them is legible.**
Measured here, every single day's `Actions storage` line came back
`discountAmount == grossAmount`, `netAmount: 0` — fully absorbed, including the days at peak
usage — while uploads were being refused for quota the whole time. So a $0 bill tells you
nothing about whether the gate will let you upload.

You also cannot compute your headroom: the usage API returns quantity and price but **no
included-storage figure**, and the older `orgs/{org}/settings/billing/shared-storage`
endpoint now answers **410 Gone**. There is no supported way to ask "how much of my
allowance am I using". That is precisely why the advice below is to judge by re-measuring
your own bytes.

So after a big cleanup you should expect the upload to keep failing for a while with the
identical message, and **that is not evidence your cleanup failed**. Re-measure the current
bytes (the `awk` above) to confirm the deletes landed, and judge by that rather than by
whether the next run went green.

If you are on a paid plan, check `netAmount` before panicking about cost — the included
allowance may fully discount it (it did here: gross \$12.56, net \$0). A blocked deploy and a
bill are different problems, and this can be the first without ever becoming the second.

## Why clearing it is hostile

The obstacle is not conceptual. Every available tool is the wrong shape:

- **There is no bulk delete.** The web UI deletes artifacts one workflow run at a time.
- **The API is one DELETE per artifact** —
  `DELETE /repos/{owner}/{repo}/actions/artifacts/{id}`. No batch endpoint, no
  "delete all expired", no filter.
- **You get 5,000 API requests per hour.** Deleting 14,000 artifacts is a three-hour job
  minimum, spread across rate-limit windows — and shared with everything else in your org
  spending the same budget.
- **You cannot tell whether it worked.** *"Usage is recalculated every 6-12 hours."* You
  delete tens of gigabytes, re-run the deploy, and it fails with the identical error. There
  is no way to distinguish "I did not delete enough" from "I deleted plenty and the meter is
  stale". That one is the cruellest, because it removes the feedback loop you would normally
  use to work the problem.

## The method

Two ideas make it tractable.

**Group by artifact NAME and keep the N most recent of each.** Names are stable across runs
(`static-export`, `app-windows-exe`, `linux-x64`), so "keep the newest three of every kind"
is a rule you can defend, and it preserves your ability to roll back to a recent build.

**Then delete BIGGEST-FIRST.** Artifact sizes are wildly non-uniform — a handful of
installers and disk images dominate everything, and the fourteen thousand small ones are
rounding error. Sorting by size turns a three-hour rate-limited grind into about ninety
seconds of API calls. If you take one thing from this skill, take that.

Expired artifacts are pure profit and go first: nothing can download them anyway.

```bash
#!/usr/bin/env bash
# reclaim.sh — inventory, decide, delete. Dry-run unless CONFIRM=yes.
#   ./reclaim.sh OWNER/REPO [KEEP_PER_NAME] [MAX_DELETES]
set -euo pipefail
REPO="${1:?usage: reclaim.sh OWNER/REPO [KEEP_PER_NAME] [MAX_DELETES]}"
KEEP="${2:-3}"
MAX="${3:-150}"
TAB=$'\t'

# 1. INVENTORY (paginated; ~180 calls for 18k artifacts)
gh api "repos/$REPO/actions/artifacts" --paginate \
  --jq '.artifacts[] | [.id, .name, .size_in_bytes, .created_at, .expired] | @tsv' \
  > artifacts.tsv
echo "inventoried $(wc -l < artifacts.tsv) artifacts"

# 2. DECIDE. Sort by (name, created DESC) so awk can drop the newest $KEEP of each
#    in one pass -- O(n log n), not the O(n^2) a nested scan would cost on 18k rows.
#    Then re-sort the survivors by size, biggest first.
sort -t"$TAB" -k2,2 -k4,4r artifacts.tsv \
| awk -F"$TAB" -v keep="$KEEP" '
    $2 != prev { prev = $2; seen = 0 }
    { seen++; if (seen > keep) print }
  ' \
| sort -t"$TAB" -k3,3nr \
| awk -v m="$MAX" 'NR <= m' > doomed.tsv
#   ^ NOT `head -n $MAX`: head closes the pipe as soon as it has enough, the upstream
#     sort dies of SIGPIPE, and `set -o pipefail` then aborts the whole script -- after
#     doomed.tsv is written, so you get a correct file and no summary and no deletes.

awk -F"$TAB" '{s+=$3} END{printf "would delete %d artifacts, freeing %.2f GB\n",
                                 NR, s/1073741824}' doomed.tsv

# 3. DELETE
[ "${CONFIRM:-no}" = yes ] || { echo "dry run; re-run with CONFIRM=yes"; exit 0; }

before=$(gh api rate_limit --jq .rate.remaining)
n=0
while IFS="$TAB" read -r id _name _size _created _expired; do
  gh api -X DELETE "repos/$REPO/actions/artifacts/$id" >/dev/null 2>&1 || true
  n=$((n + 1))
done < doomed.tsv
after=$(gh api rate_limit --jq .rate.remaining)

# 4. VERIFY THE LOOP ACTUALLY DIALLED -- see trap 1.
echo "iterations: $n   rate-limit delta: $((before - after))"
[ $((before - after)) -ge "$n" ] \
  || echo "WARNING: fewer API calls than iterations -- requests died locally, not at the server"
```

To take the expired ones first — the free win — filter before sorting:
`awk -F"$TAB" '$5 == "true"' artifacts.tsv`.

## Three traps, all of which fail silently

Each of these cost a real attempt, and none of them announced itself.

**1. The id file had Windows line endings.** The loop reported 61 failures, so the obvious
read was "the API is rejecting these". It was not. The rate limit had dropped by **18**
across those 61 iterations — meaning 43 never made an API call at all. Each id carried a
trailing carriage return, the URL was malformed, and the request died locally.

The tell was not in the error output. It was in the **rate-limit delta not matching the
iteration count**, which is why the script above checks that ratio and why you should too.
A loop that fails before it dials looks identical to a loop that fails at the server.

**2. A flag that does not exist fails the whole command.** `--silent` was passed to a `gh`
version that does not support it. Every iteration failed on argument parsing, and because
the loop counted its own exit codes, the conclusion was "the deletes are being rejected".
**Run one call by hand, outside the loop, before believing any batch result.**

**3. The tool watching your run can lie.** `gh run watch <id> --exit-status` exited **0** —
success — because it had given up on an HTTP 403 rate limit. The run had not even started.
An exit code from a tool that could not reach the API is not a verdict about your deploy.

## Do not stop at the cleanup

Deleting artifacts fixes today. It does not fix the class, because the class is *not
knowing*. Two durable changes:

**Set a retention policy** so this cannot re-accumulate — repository Settings → Actions →
Artifact and log retention, or per-upload:

```yaml
- uses: actions/upload-artifact@v4
  with:
    name: static-export
    path: out/
    retention-days: 7        # default is 90
```

Note this bounds *future* growth only. It does not remove what is already there, and it does
not stop expired artifacts from counting until they are actually deleted.

**Add a check that compares the SERVED SITE against the repository**, because that is the
signal that was missing the whole time. A green CI run does not prove users got it, and a red
one does not prove they did not — only fetching the surface does.

If your site is client-rendered, grepping the HTML proves nothing: measured, the served HTML
was **byte-identical (63,086 bytes)** before and after a successful publish, because the
catalogue lives in a JavaScript chunk. The check has to fetch the page, enumerate its
`<script src>` chunks, and grep *those*.

And give that check a **canary** — something you know has been live for months. If the canary
is missing, your probe is broken rather than the site being stale, and it must report *cannot
judge* instead of reporting everything as missing. That guard earned its place on the first
run: the probe counted single-quoted identifiers while the minifier emits double quotes, so
it matched nothing. Instead of 72 false alarms it said it could not tell.

**A probe that cannot judge must never return "fine."** That principle outlives the cleanup.

## The short version

- `needs: build` is correct and it makes build failures invisible from outside. Assume your
  site can silently stop tracking your main branch.
- Expired artifacts still count against the quota. Check `expired: true` before assuming your
  storage is in use.
- No bulk delete exists. Sort by size and delete biggest-first — 1% of the artifacts can hold
  70% of the bytes.
- Usage recalculates on a 6–12 hour lag, so a re-run failing identically does not mean your
  cleanup failed.
- Verify bulk API loops by the **rate-limit delta**, not by your own success counter.
- Set `retention-days` so it cannot come back.
- Check the surface, not the pipeline.
