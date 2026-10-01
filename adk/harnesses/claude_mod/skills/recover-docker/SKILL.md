---
name: recover-docker
allowed-tools: PowerShell, Bash, Read
description: Hard-recover Docker Desktop from the WSL2 wedge (API 500 / "did not receive an exit event") and restart exited containers
argument-hint: [--monitor]

---

## Context
- Docker server: !`docker version --format '{{.Server.Version}}' 2>&1 | head -1`
- Recovery script: `Recover-Docker.ps1` (from this repo's `scripts/`)
- Arguments: $ARGUMENTS

## Your Role
You recover Docker Desktop when its WSL2 Linux engine wedges. Symptoms:
- `docker` API returns **500 Internal Server Error**, or
- `docker stop` / recreate fails with **"tried to kill container, but did not receive an exit event"** — common on nvidia-runtime / GPU containers, which blocks container recreates.

## Your Task
1. **Run the recovery script** (it kills Docker Desktop + WSL, restarts them cleanly, then restarts exited containers). Point this at wherever you saved `Recover-Docker.ps1`:
   ```powershell
   pwsh -File ./scripts/Recover-Docker.ps1
   ```
   If `$ARGUMENTS` contains `--monitor`, add `-Monitor` to run the auto-recover watch loop (checks every 30s) instead of a single pass.
2. **Admin fallback:** if it logs `Skipping service restart (not admin)` AND Docker is still unhealthy afterward, tell the user to re-run from an **elevated** PowerShell — restarting the Docker/WSL Windows services needs admin.
3. **Verify + report:** after it prints "Docker recovered", confirm with `docker version` and `docker ps`, and report which containers came back up.
4. **Note for follow-up:** the wedge is now cleared, so any pending deploy/recreate will work cleanly. Do NOT rebuild/redeploy anything unless explicitly asked.

Keep it tight: run → verify → report. This command's job is recovery only.
