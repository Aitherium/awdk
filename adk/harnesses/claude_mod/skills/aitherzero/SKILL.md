---
name: aitherzero
description: Provision any machine from one config file and a library of numbered PowerShell automation-scripts. Clone the public AitherZero repo, build the module, and run scripts or playbooks on Windows, Linux, or macOS under PowerShell 7. Covers the layered config model, the real command surface, and which pieces are public versus internal.
---

# aitherzero — provision any machine from one config file

[AitherZero](https://github.com/Aitherium/AitherZero) (MIT) is the self-service provisioning
surface: a **`config.psd1`** plus a library of **numbered PowerShell automation-scripts** and
playbooks that stand up bare-metal, on-prem, cloud, or hybrid machines. It runs on
**PowerShell 7** everywhere, so the same scripts work on every node.

## Set it up

```powershell
git clone https://github.com/Aitherium/AitherZero.git
cd AitherZero
./build.ps1                       # generates AitherZero.psd1 (+ bin/) from src/
Import-Module ./AitherZero.psd1 -Force
```

> **`./build.ps1` is not optional and is easy to miss.** The repo does **not** ship a committed
> `AitherZero.psd1` — `build.ps1` generates it at the repo root and in `bin/`. Skip it and
> `Import-Module ./AitherZero.psd1` fails with "file not found", which reads like a bad clone.
> (The repo's own README also references a `bootstrap.ps1` that is not currently in the public
> tree — use the module commands below instead.)

**Check — this must list scripts, not error:**

```powershell
Get-AitherScript | Select-Object -First 5
```

## Run it

```powershell
Invoke-AitherScript 1002          # run one automation-script by number (1002 = Install-Git)
Invoke-AitherPlaybook node-onboard
Get-AitherPlaybook                # what playbooks are available
Get-AitherScriptMetadata 1002     # what a script does, and its parameters
```

Scripts live under `library/automation-scripts/<category>/NNNN_Verb-Noun.ps1` — the number is the
handle you pass to `Invoke-AitherScript`. Playbooks are `.psd1` files under `library/playbooks/`.

## Configure it

Config layers, so you only override what you need:

```
config.psd1  <  config.<platform>.psd1  <  config/domains/*.psd1  <  config.local.psd1
```

Copy the template and edit only your overrides:

```powershell
Copy-Item config/config.local.template.psd1 config.local.psd1
```

`config/domains/` splits settings by area (`ai`, `automation`, `development`, `infrastructure`,
`reporting`, `security`), and `config.example.psd1` is the annotated reference. Inspect the
resolved result rather than guessing which layer won:

```powershell
Get-AitherConfigs                 # what config is actually in effect
Compare-AitherConfig              # diff layers against each other
```

## Build your config without hand-editing psd1

The config surface is **generated from the script inventory** — every automation-script's
`param()` block becomes a setting — so writing a script with a `param()` block extends AitherZero
automatically. Two tools make that self-service, and both ship in the repo you just cloned:

```powershell
# 1. generate the schema from any inventory — the public library, or your own scripts
pwsh tools/config-editor/Export-AitherConfigSchema.ps1 `
     -ScriptRoot ./library/automation-scripts `
     -PlaybookRoot ./library/playbooks `
     -OutFile tools/config-editor/config-schema.json

# 2. open tools/config-editor/index.html, load config-schema.json, set parameters
#    with enum dropdowns and live trap checks, then export your config.local.psd1
```

A generated `config-schema.json` is committed, so you can open the builder immediately and only
re-run step 1 after you add or change scripts. Point `-ScriptRoot` at your own directory to fold
private automation into the same editor.

**Check — the exporter prints what it found:**

```
Wrote .../config-schema.json — 69 scripts with a configurable surface, 9 categories.
```

## Drive it from an agent

The `aitherzero` tool pack gives an [awdk](awdk.md) agent the same surface as tools,
so an agent can inventory scripts, validate configs, and plan deployments:

```bash
pip install awdk
python -m adk.toolpacks.aitherzero inventory
python -m adk.toolpacks.aitherzero describe 1002
python -m adk.toolpacks.aitherzero validate --path config.local.psd1
```

## What is public, and what isn't

This matters if you are following older write-ups — several describe an internal layout a public
clone does not have:

Everything this skill tells you to run is in the public repo: `build.ps1`, `src/` (the module),
`library/automation-scripts/`, `library/playbooks/`, `config/`, `plugins/`, and
`tools/config-editor/` (the schema exporter + visual Config Builder).

Aitherium runs a larger internal script inventory on top of the same engine, so older write-ups
may quote bigger numbers or reference paths under a dot-prefixed `PRODUCTS`/`DEPLOYMENT` root.
**Those are private-monorepo paths and will not exist in your clone** — every command here is a
public one, verified against this repository.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Import-Module ./AitherZero.psd1` — file not found | skipped the build | run `./build.ps1` first |
| `bootstrap.ps1` not found | not in the public tree | use `Invoke-AitherPlaybook` / `Invoke-AitherScript` |
| A dot-prefixed `PRODUCTS`-style path does nothing | private-monorepo path from an older write-up | use the public paths in this skill |
| Config Builder shows no parameters | `config-schema.json` not loaded, or stale | re-run the exporter, then load the file in the page |
| Script number not found | inventory differs from internal docs | `Get-AitherScript` lists what you actually have |
| Config change has no effect | a higher layer overrides it | `Get-AitherConfigs` / `Compare-AitherConfig` |

## Part of one substrate

AitherZero provisions the box; [awnode](aithernode.md) makes its hardware usable,
[Awconnect](awconnect.md) wires it to the fleet, [awdk](awdk.md) is the runtime
that drives all of it, and [OmniNode](omninode-node.md) pools the results into one compute fabric.

MIT-licensed, like everything in `awskills`.
