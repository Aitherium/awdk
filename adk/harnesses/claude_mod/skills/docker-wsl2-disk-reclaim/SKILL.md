---
name: docker-wsl2-disk-reclaim
description: >-
  Your drive is full and `docker system df` is lying to you. Docker Desktop on WSL2 stores everything in a dynamically-expanding VHDX that never shrinks. You delete 200GB of images, docker system df drops, and your drive stays exactly as full as before. People then go looking for the space in the wrong place — or buy a disk.
---

# docker-wsl2-disk-reclaim — your drive is full and `docker system df` is lying to you

Docker Desktop on WSL2 stores everything in a **dynamically-expanding VHDX that never
shrinks**. You delete 200GB of images, `docker system df` drops, and your drive stays exactly
as full as before. People then go looking for the space in the wrong place — or buy a disk.

This skill is the accounting model that explains it, and the sequence that actually reclaims.

> Measured on a real box (2026-07-25), a 3.7 TB drive at **98% full**:
> VHDX allocated **2.0 TB** · ext4 inside the VM **1.6 TB used** · `docker system df` **634 GB**.
> Same data, three numbers, a 1.4 TB spread.

## Three layers, each hiding the next

```
VHDX allocated on the host   ≥   ext4 "used" inside the VM   ≥   docker system df
        2.0 TB                          1.6 TB                       634 GB
        └─ what fills your drive        └─ what the guest thinks     └─ what Docker admits
```

- **LAYER 1 → 2 gap (0.4 TB here):** blocks the VHDX still owns that ext4 has already freed.
  Only **compaction** returns these, and only after `fstrim`.
- **LAYER 2 → 3 gap (~1 TB here):** data ext4 genuinely holds that `docker system df` does
  not count — orphaned overlay2 layers from crashed builds, buildkit state, container logs.
  **A prune will not find this.** `docker system df` is the number everyone looks at and the
  least useful of the three.

## Why it never shrinks by itself

```bash
wsl -d docker-desktop -e mount | grep docker-desktop-disk
# /dev/sdd on /mnt/docker-desktop-disk type ext4 (rw,relatime)
#                                                  ^^^^^^^^^^ no `discard`
```

Docker's data disk is mounted **without `discard`**. When ext4 frees blocks, nothing tells
the VHDX, so the host file stays pinned at its all-time high-water mark. Other WSL mounts on
the same box *do* carry `discard` — this one doesn't. That single missing mount option is the
whole reason a 634 GB working set occupies 2.0 TB.

## The sequence (order matters)

Run the tool: [`tools/docker-wsl2-reclaim.sh`](../tools/docker-wsl2-reclaim.sh)

```bash
docker-wsl2-reclaim.sh            # diagnose — read-only, prints all three layers
docker-wsl2-reclaim.sh --trim     # prune + fstrim — no downtime, no admin
docker-wsl2-reclaim.sh --plan     # prints the admin compaction recipe for YOUR path
```

**1. Prune** — shrinks LAYER 3. Dangling images and build cache are zero-risk:
```bash
docker image prune -f && docker builder prune -af
```
🪤 Do **not** hand-roll an "unused images" list by diffing `docker images` against
`docker ps -a --format '{{.Image}}'`. Running containers report their image as a **short
SHA**, not the tag, so a tag-based grep marks in-use images as orphaned. That mistake was
one command away from deleting the images out from under ~15 live services. Use
`docker image prune -a` and let Docker compute the reference set.

**2. `fstrim`** — the step everyone skips. Turns ext4's free blocks into holes the VHDX can
see. Without it, compaction reclaims almost nothing:
```bash
wsl -d docker-desktop -e fstrim -v /mnt/docker-desktop-disk
```
If this reports a small number while LAYER 2 still far exceeds LAYER 3, the space is *live*
data Docker under-reports — go hunting in `/mnt/docker-desktop-disk/data`, don't keep pruning.

**3. Compact** — the only step that shrinks the host file. Needs the VM stopped **and
administrator**:
```powershell
# Quit Docker Desktop from the tray first
wsl --shutdown

diskpart
  select vdisk file="C:\path\to\docker_data.vhdx"
  attach vdisk readonly
  compact vdisk
  detach vdisk
  exit
```

🪤 **Prefer `diskpart` over `Optimize-VHD`.** `Optimize-VHD` emits no progress whatsoever, so
on a multi-TB file it is indistinguishable from a hang — it gets Ctrl-C'd by people who
assume it's stuck. `diskpart compact vdisk` prints a percentage. Both need admin; neither can
be run unattended from an agent session, because UAC elevation cannot be scripted away.

**Verify it worked** — check the host file, not `docker system df`:
```bash
du -h /c/path/to/docker_data.vhdx      # allocated, the number that matters
ls -la /c/path/to/docker_data.vhdx     # apparent — will still look huge
```

## Stopping the regrowth

- **Schedule the `--trim` step.** Weekly is plenty; it needs no downtime or admin.
- **`.wslconfig`:** `[experimental] sparseVhd=true` — but understand the limit: it applies to
  **newly created** disks only. It does not retro-fit an already-allocated VHDX, which is
  exactly why a box with it enabled still had a 2.0 TB file.
- **Moving the VHDX to another drive does not shrink it.** A 2.0 TB allocated file needs
  2.0 TB at the destination. Compact first, move second — a lot of migration plans die here.

## Related failure you will hit next

Bulk-building images with a large fleet resident crashes the WSL2 backend outright and
produces tens of thousands of Windows disk I/O errors that look exactly like a dying drive.
Different problem, same VM — see **`docker-wsl2-build-safety`** for the guard and the
day-distribution check that tells hardware failure apart from load-induced collapse.

## Before you claim done

- `du -h` on the VHDX actually dropped (not just `docker system df`)
- the fleet came back: `docker ps -q | wc -l` at the expected count, `--filter health=unhealthy` empty
- you know which of the two gaps you closed — trim closes 1→2, prune closes 2→3, and if the
  drive is still full you closed the wrong one
