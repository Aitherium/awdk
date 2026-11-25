---
name: windows-goodbye
description: >-
  Install awnix on a Windows PC with Windows Goodbye - one PowerShell line imports awnix as its own WSL2 distro from one SHA-256-checked download. No reboot, no boot entries, no admin; -Uninstall removes it. Covers when to use it, the dry run, uninstall, and every error the script can print.
---
<!-- Generated from the awskills pack (skills/windows-goodbye.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# windows-goodbye — install awnix on a Windows PC, one line, no reboot

Windows Goodbye is a PowerShell script that installs **awnix** (the Aither World's Linux,
built on CentOS Stream 9) as a **WSL2 distro** named `awnix`. Windows itself is untouched:
nothing about how the PC boots changes, and removing it is one flag.

- Site: [goodbyewindows.sh](https://goodbyewindows.sh)
- Source (MIT): [github.com/Aitherium/windowsgoodbye](https://github.com/Aitherium/windowsgoodbye)

## When to use it

- Someone on Windows 10 or 11 (x86_64) wants awnix today, without a second disk, a USB stick
  or a reboot.
- You want an agent-ready Linux next to the Windows tools someone already uses.
- **Not for:** bare metal, dual boot or Hyper-V. Use the awnix ISO from the
  [awnix releases page](https://github.com/Aitherium/awnix/releases/latest) instead.

**Needs:** Windows 10 or 11 on x86_64, WSL2, PowerShell 5.1 or later, about 1.5 GB of download.

## The command

In a normal (not admin) PowerShell window:

```powershell
irm goodbyewindows.sh | iex
```

What it does, in order, and nothing else:

1. Checks `wsl.exe` exists and `wsl --status` answers.
2. Reads the latest release of `Aitherium/windowsgoodbye` from the GitHub API.
3. Downloads each `awnix-rootfs.tar.gz.part*` and checks it against `SHA256SUMS`
   (`Get-FileHash -Algorithm SHA256`). One mismatch and nothing is installed.
4. Joins the parts in a temporary folder, then
   `wsl --import awnix %LOCALAPPDATA%\awnix\wsl <tar.gz> --version 2`.
5. Runs `wsl -d awnix -- cat /etc/os-release` to prove it starts, then opens a shell.

It never writes a boot entry, changes boot order, reboots, installs a driver or asks for admin.

## Dry run first (recommended for someone else's machine)

`irm ... | iex` cannot pass options. Download the script, read it, then run it with `-DryRun`:

```powershell
irm https://goodbyewindows.sh/WindowsGoodbye.ps1 -OutFile WindowsGoodbye.ps1
powershell -ExecutionPolicy Bypass -File .\WindowsGoodbye.ps1 -DryRun
```

Expected: `Release <tag>: <n> part(s), <size> MB to download.` and a `DRY RUN: would download ...`
line showing the exact `wsl --import` it would run. Nothing is downloaded or imported.

Other options: `-DistroName <name>` (default `awnix`), `-InstallDir <path>` (default
`%LOCALAPPDATA%\awnix\wsl`), `-NoLaunch` (install without opening a shell).

## Verify

```powershell
wsl -d awnix -- cat /etc/os-release
```

Expected: `NAME="awnix"` and `PRETTY_NAME="awnix (built on CentOS Stream 9)"`.
Open it any time with `wsl -d awnix`.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\WindowsGoodbye.ps1 -Uninstall
```

Expected: `Removed 'awnix'. Windows is unchanged.` This is `wsl --unregister awnix`: it
**deletes the distro's virtual disk**, so copy out anything made inside it first.
`-Uninstall -DryRun` prints what it would remove and removes nothing.

## Troubleshooting

| it prints | what it means | do |
|---|---|---|
| `WSL is not installed.` | no `wsl.exe` | in an **admin** PowerShell: `wsl --install --no-distribution`, reboot if asked, run again |
| `WSL is present but not ready: ...` | `wsl --status` failed | same as above; the reason is printed verbatim |
| `awnix is already installed as 'awnix'.` | a distro by that name exists | nothing to do; it opens it. Use `-DistroName` for a second copy |
| `The release <tag> has no awnix filesystem yet.` | the latest release has no rootfs parts or no `SHA256SUMS` | try later, or check the repo's releases page |
| `<part> failed its checksum. Nothing was installed.` | a download was corrupted or cut short | run it again; each run downloads fresh into a new temp folder |
| `wsl --import failed: ...` | WSL refused the import (often disk space or a WSL error) | read the WSL text it printed; free space on the drive holding `%LOCALAPPDATA%` and `%TEMP%` |
| `Imported, but it does not start: ...` | imported but `/etc/os-release` could not be read | run `-Uninstall`, then install again |
| `No distro named 'awnix'. Nothing to remove.` | `-Uninstall` found nothing | nothing to do |

## Why it is safe to hand someone

- **One verified file.** The filesystem is flattened once on Linux in the repo's release
  workflow (`docker export`), imported back and run (it must report an OS and carry bash
  and systemd) before it is published. The PC needs no Python and no container layer logic.
- **Tested on Windows after every release.** A `windows-latest` workflow runs the dry run,
  the real install, a start check and the uninstall. GitHub's Windows machines cannot nest
  WSL2, so CI imports with `-WslVersion 1`; on a real PC the default is WSL2.
- **Reversible.** Windows is never modified beyond WSL's own distro list; `-Uninstall`
  takes it back out.
