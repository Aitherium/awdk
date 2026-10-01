---
name: github-runner-fleet
allowed-tools: Bash, Read, Write
description: Stand up, verify and repair a fleet of self-hosted GitHub Actions runners on one Windows host. Load when CI jobs sit queued while `gh api .../actions/runners` shows an online runner, when adding parallel capacity, when a workflow cannot get a runner, or before deleting any runner registration. Encodes the four failure modes measured 2026-08-12 — three of which each cost a failed attempt — plus the ghost-registration trap that makes a dead runner look like capacity.
argument-hint: [add N runners | diagnose stuck queue | migrate off a drive]

---


# GitHub runner fleet — one host, many runners

The recurring job: jobs queue forever, or you want parallel CI. Both come back to
the same question — **is there an agent PROCESS for every registration GitHub
lists?** — and the answer is usually no.

## The ghost-registration trap (check this FIRST)

A runner registration and a runner *process* are different things. GitHub happily
lists a registration whose agent died months ago; it will never take a job.

Measured 2026-08-12: four runners registered, three showing `offline`, and **only
one had an install directory or a service on the host**. The other three were
ghosts from a machine that no longer exists. Deleting them changed capacity by
zero.

```powershell
# registrations GitHub knows about
gh api repos/<OWNER>/<REPO>/actions/runners --jq '.runners[] | "\(.status) busy=\(.busy) \(.name)"'
# agent services actually installed here
Get-Service -Name "actions.runner.*" | Select-Object Name, Status
# install directories actually on disk
Get-ChildItem C:\,D:\,E:\ -Directory -EA SilentlyContinue | Where-Object Name -like 'actions-runner*'
```

**The tell that a registration is a ghost:** jobs sit `queued` while a runner
reports `busy=false`. A live idle runner takes the next job within seconds. If it
does not, it is not there.

Delete ghosts — they make the fleet look larger than it is:

```powershell
$id = gh api repos/<OWNER>/<REPO>/actions/runners --jq '.runners[] | select(.name=="<NAME>") | .id'
gh api -X DELETE repos/<OWNER>/<REPO>/actions/runners/$id
```

## Adding runners — one directory each

Runners do **not** share a directory. Each instance needs its own tree, its own
`_work`, and its own name.

```powershell
# 1. copy the package (NOT the state — see the two traps below)
New-Item -ItemType Directory E:\actions-runner-2 -Force
Copy-Item a runner directory\* E:\actions-runner-2 -Recurse -Force

# 2. STRIP the inherited identity, or config.cmd refuses
Get-ChildItem E:\actions-runner-2 -Force |
  Where-Object Name -in '.runner','.credentials','.credentials_rsaparams','.path','.env','_diag','_work','.runner_migrated' |
  Remove-Item -Recurse -Force

# 3. register (token is single-use and short-lived — mint one PER runner)
cd E:\actions-runner-2
$tok = gh api -X POST repos/<OWNER>/<REPO>/actions/runners/registration-token --jq .token
.\config.cmd --unattended --url https://github.com/<OWNER>/<REPO> --token $tok `
  --name ci-runner-2 --labels "self-hosted,Windows,X64,local" `
  --work "_work" --replace
```

### Trap 1 — `Copy-Item -Exclude` does not apply through `-Recurse`

The copy carries `.runner` and `.credentials` from the source, so `config.cmd`
answers:

> Cannot configure the runner because it is already configured.

`-Exclude` filters only the top-level enumeration, not recursed children. Strip
the state files **after** copying, as step 2 does. Costed one failed attempt.

### Trap 2 — `.runner_migrated` refuses even after the obvious files are gone

Clearing `.runner`/`.credentials` is not enough on a runner that has been through
a version migration: a `.runner_migrated` marker produces the *identical* "already
configured" message. It is easy to conclude the strip did not work. It did — there
is a second marker. Costed a second failed attempt.

```powershell
Get-ChildItem E:\actions-runner-2 -Force | Where-Object Name -like '.*'   # see what is really there
```

### Trap 3 — `--runasservice` needs elevation, `config.cmd` does not

Registration works unelevated. Installing the **service** does not. Split them:

```powershell
# unelevated: registers, and `run.cmd` works until the shell/host restarts
Start-Process .\run.cmd -WorkingDirectory E:\actions-runner-2 -WindowStyle Hidden

# ELEVATED: makes it survive reboot
cd E:\actions-runner-2 ; .\svc.cmd install ; .\svc.cmd start
```

A runner started with `run.cmd` is real capacity **today** and gone after reboot.
Say which one you left behind; "the runners are up" is ambiguous and the
difference shows up as a mysteriously shrinking fleet.

### Trap 4 — the token is not the secret you think

`registration-token` is short-lived (about an hour) and single-use per
registration. Mint one per runner, inline, and **never write it to a file** — it
authorises adding a runner to your repo. Consume it in the same shell expression
that mints it so it never reaches disk, a transcript, or shell history.

## Placement: not on a retired drive

Check your own storage policy before choosing a path. Measured 2026-08-12: the
one live runner sat on a drive that was actively being decommissioned. That is
a single point of failure for **all** CI on a disk being decommissioned, and it is
the likeliest reason the three ghost registrations died in the first place.

New runners go on the runtime drive. Migrating an existing one is
`svc.cmd uninstall` → move → re-register.

## Verify — the fleet, not the file

```powershell
gh api repos/<OWNER>/<REPO>/actions/runners --jq '.runners[] | "\(.status) busy=\(.busy) \(.name)"'
```

Every runner you expect must be `online`. Then prove it takes work: dispatch a job
and watch `busy` flip. A runner that is `online` but never `busy` while jobs queue
is either mislabelled or a ghost.

**Labels are the contract.** A workflow asking for `self-hosted` matches any
runner carrying that label; a workflow asking for `ubuntu-latest` will **never**
match a self-hosted runner no matter how many you add. If jobs queue while your
runners idle, read the workflow's `runs-on` before adding capacity — that was the
actual fault on 2026-08-12, and three more runners would not have fixed it.

## Related

- Your own single-instance runner-setup automation, if you have it — the point of
this page is the four traps it almost certainly does not encode.
