---
name: awrise
description: "Schedule wakes (jobs) that record why they did or did not fire. Use to add a recurring job, see why one never ran, or check every job's last verdict."
---

# awrise — wake something on a schedule, with a memory

```bash
awrise add --name NAME --every 15m --run "cmd" [--at HH:MM] [--timeout 300]
awrise list                            # registered jobs
awrise status                          # per-job verdict of the last wake
awrise explain NAME                    # why NAME did or did not fire last tick
awrise history                         # every wake and its reason
awrise run NAME                        # fire now, due or not
```

`set NAME key=value` edits a job; `enable|disable|remove` toggle it. `--executor`
picks shell|python|http|awrun|agent|session; `--receipt FILE` lets the child write its
own verdict. The host clock that calls `run-due` is `awrise install-clock` (idempotent).

**Exit codes:** `status` exits 1 when any job's last wake failed; 0 when every last wake
succeeded or was a policy skip.

**Trap:** a job that never ran reads the same as one not yet due unless you ask
`explain` — cron-style silence is the failure mode this exists to name. Use
`import-routine` (prints a plan) before hand-translating `config/routines/*.yaml`.
