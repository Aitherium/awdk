---
name: recover-docker
allowed-tools: PowerShell, Bash, Read
description: Hard-recover Docker Desktop from the WSL2 wedge (API 500 / "did not receive an exit event") and restart exited containers
argument-hint: [--monitor]

---
<!-- Generated from the awskills pack (skills/recover-docker.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

## Context
- Docker server: !`docker version --format '{{.Server.Version}}' 2>&1 | head -1`
- Recovery script: `Recover-Docker.ps1` (from this repo's `scripts/`)
- Arguments: $ARGUMENTS

## Your Role
You recover Docker Desktop when its WSL2 Linux engine wedges. Symptoms:
- `docker` API returns **500 Internal Server Error**, or
- `docker stop` / recreate fails with **"tried to kill container, but did not receive an exit event"** — common on nvidia-runtime / GPU containers, which blocks container recreates.

## Your Task
1. **Run the recovery script.** What it does: stops the Docker Desktop processes, terminates only Docker's own WSL distros (`docker-desktop`, `docker-desktop-data`; it never runs a WSL-wide shutdown or kill, so other distros keep running), restarts Docker Desktop (and its Windows services when elevated), then restarts only exited containers that have a restart policy. Containers left in the `dead` state are reported, not touched, unless you pass `-RemoveDead`, which removes them so their service can be recreated. Point this at wherever you saved `Recover-Docker.ps1`:
   ```powershell
   pwsh -File ./scripts/Recover-Docker.ps1
   ```
   If `$ARGUMENTS` contains `--monitor`, add `-Monitor` to run the auto-recover watch loop (checks every 30s) instead of a single pass. Add `-RemoveDead` only when the user asked for dead containers to be removed.
2. **Admin fallback:** if it logs `Skipping service restart (not admin)` AND Docker is still unhealthy afterward, tell the user to re-run from an **elevated** PowerShell — restarting the Docker/WSL Windows services needs admin.
3. **Verify + report:** after it prints "Docker recovered", confirm with `docker version` and `docker ps`, and report which containers came back up.
4. **Note for follow-up:** the wedge is now cleared, so any pending deploy/recreate will work cleanly. Do NOT rebuild/redeploy anything unless explicitly asked.

Keep it tight: run → verify → report. This command's job is recovery only.
