---
name: docker-wsl2-build-safety
description: >-
  Stop bulk builds from killing Docker Desktop (and don't blame the disk). Docker Desktop on Windows runs its engine inside a WSL2 VM with a fixed memory ceiling. Build a lot of images while a lot of containers are resident and the VM's storage layer can collapse — taking every container down with it. The failure does not look like 'you ran out of memory'. It looks like your disk is dying.
---

# docker-wsl2-build-safety — stop bulk builds from killing Docker Desktop (and don't blame the disk)

Docker Desktop on Windows runs its engine inside a WSL2 VM with a **fixed memory ceiling**.
Build a lot of images while a lot of containers are resident and the VM's storage layer can
collapse — taking every container down with it. The failure does not look like "you ran out
of memory". It looks like **your disk is dying**.

This skill is the guard, and the diagnostic that tells the two apart.

> Live-proven on a 125 GB host running ~165 containers (2026-07-25): a 72-image bulk build
> followed by a container recreate wave crashed the WSL2 backend. Windows logged **44,051
> `disk` Event-51 errors and 2,266 NTFS delayed-write failures in a single hour**. The drive
> was diagnosed as failing. It was not. The correct read was in the *distribution*, not the
> count.

## The tell: is it the disk, or the VM?

Both produce I/O errors. They differ in **shape over time**.

| | failing drive | WSL2 storage collapse |
|---|---|---|
| distribution | spread over days/weeks, **growing** | one spike, then **decays to zero** |
| preceding days | errors present and rising | **zero** |
| SMART | reallocated/pending sectors climb | clean / `Healthy` |
| correlation | none in particular | starts exactly at a heavy build or recreate wave |
| after reboot | errors return under normal use | fine until the next bulk build |

Run this **before** condemning hardware:

```powershell
Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='disk';Id=51;StartTime=(Get-Date).AddDays(-10)} |
  Group-Object { $_.TimeCreated.ToString('yyyy-MM-dd') } | Select-Object Count,Name
```

All the errors on one day with nothing before it ⇒ **it is the VM, not the disk.** In the
proven case the hourly counts decayed 2,075 → 126 → 65 while SMART stayed `Healthy`, and the
drive worked normally afterwards.

Inside the VM the signature is a virtual SCSI device being taken offline:

```
sd 0:0:0:3: Device offlined - not ready after error recovery
I/O error, dev sdd, sector 1382182208 op 0x0:(READ)
containerd: garbage collection failed: input/output error
overlayfs: failed to get metacopy (-5)
```

`docker` then reports **"Docker Desktop is unable to start"** and no restart fixes it until
the VM is fully shut down (`wsl --shutdown`) and brought back.

## The guard

Two rules, both mechanical:

**1. Never `docker compose build` a large set with the fleet resident.** Build on a
memory-capped `docker-container` builder so a runaway build is OOM-killed *inside* the
builder instead of taking the VM down:

```bash
docker buildx create --name capped --driver docker-container \
  --driver-opt memory=16g --bootstrap
docker buildx build --builder capped -t myimage:latest --load .
```

**2. Gate every bulk operation on real VM headroom.** Drop this in and call it first:

```bash
headroom_gb() {
  wsl.exe -d docker-desktop -e sh -c \
    "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo" 2>/dev/null | tr -d '\r'
}
swap_used_pct() {
  wsl.exe -d docker-desktop -e sh -c \
    "awk '/SwapTotal/{t=\$2} /SwapFree/{f=\$2} END{if(t>0) print int((t-f)*100/t); else print 0}' /proc/meminfo" 2>/dev/null | tr -d '\r'
}
require_headroom() {   # require_headroom <gb> <label>
  local need="$1" label="$2" have swap
  have="$(headroom_gb)"; swap="$(swap_used_pct)"
  if [ "${have:-0}" -lt "$need" ] || [ "${swap:-0}" -ge 75 ]; then
    echo "REFUSING '$label': ${have}GB free (need ${need}), swap ${swap}%" >&2
    return 1
  fi
}

require_headroom 25 "bulk image build" || exit 1
```

Use **`MemAvailable`**, not `MemFree` — it counts reclaimable page cache, which is what a
build can actually use. Check **swap too**: one documented outage had plenty of free RAM
while swap sat at 99% and the VM's embedded DNS resolver began dropping queries, which
presents as unrelated network flakiness.

**3. Never overlap a build wave with a recreate wave.** Every recorded instance of this
failure was *concurrency*, not any single operation. Finish the builds, let the VM settle,
then recreate.

## Sizing the ceiling

`%USERPROFILE%\.wslconfig` governs it. Leave real headroom above your resident baseline:

```ini
[wsl2]
memory=104GB              # host 125GB — leave ~20GB for Windows
swap=48GB                 # last-ditch cushion for spikes, not a substitute for headroom
processors=24
[experimental]
autoMemoryReclaim=gradual # return idle RAM to Windows
sparseVhd=true            # keep new VHDXs sparse
```

Changes need `wsl --shutdown` to take effect — a full Docker outage of a few minutes. Record
*why* each number is what it is in comments; the next person raising it needs the history.

## Recovering after it happens

```powershell
Get-Process -Name "*docker*" | Stop-Process -Force
wsl --shutdown
Start-Sleep 15
Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
```

Then wait — the engine can take several minutes to remount its data disk. If it still won't
start, check `%LOCALAPPDATA%\Docker\log\vm\init.log` for the `Device offlined` line before
assuming hardware.

🪤 **`Optimize-VHD` is not a recovery tool here.** It needs administrator, gives no progress
output, and on a multi-TB disk image will read for hours. It is also useless if the VHDX is
fully allocated — check `du` (allocated) against `ls` (apparent) first.

## Before you claim done

Prove the positive, not the absence of an error:

- `docker ps -q | wc -l` back to the expected count, and `--filter health=unhealthy` empty
- the day-distribution query above shows **no new** disk-51 events after your next build
- your bulk-build path actually used the capped builder — check `docker buildx ls` shows it
  and that your script calls it, because the dangerous path is the *default* one and scripts
  drift back to it silently
