---
name: resume-all
description: Reopen your killed Claude Code sessions — snapshot before a reboot, restore after, or pick from every project and resume as terminal tabs or tmux windows
argument-hint: "[snapshot | restore | all | 1,3,5 | <filter text>]  (empty = pick interactively)"
allowed-tools: Bash(pwsh:*)

---

You are helping the user resume their previously-killed Claude Code sessions.

The engine is `scripts/Resume-ClaudeSessions.ps1` from the `awskills` repo.
Replace `<ENGINE>` below with wherever you saved it. It is read-only against the
session history — it never mutates the journals.

Arguments the user passed: `$ARGUMENTS`

## Around a reboot: snapshot → restore

The set of sessions that were actually OPEN only exists while they are running.
Capture it while you still can, then reopen exactly that set:

```
pwsh -NoProfile -File "<ENGINE>" -Snapshot       # before shutting down
pwsh -NoProfile -File "<ENGINE>" -FromSnapshot   # after booting back up
```

Restore skips sessions that are already open, so it is safe to run twice. If
`$ARGUMENTS` is `snapshot` or `restore`, run the matching command, report the
result, and stop — no listing needed.

To keep the snapshot fresh without remembering to run it, wire `-Snapshot -Auto`
into a SessionStart hook in `settings.json` — it prints nothing (a hook's stdout
is charged to every session's context) and refuses to replace a snapshot younger
than `-StaleHours` with a SMALLER capture. That guard is not optional: without it,
restoring 13 tabs fires the hook on tab #1 and overwrites the 13-session record
with a 1-session one, stranding the other 12 at the exact moment the snapshot is
being used.

Verify a hook by making it PROVE it fired, not by reading the config back: plant
a deliberately tiny snapshot, start a session, and check the snapshot regrew.
That is what caught the next bug — a headless `claude -p` run reports
`kind:"interactive"` and was being captured, so a reboot would have reopened
throwaway one-shots as tabs. `entrypoint` is the real discriminator (`cli` vs
`sdk-cli`); the engine excludes `^sdk` by denylist so unknown entrypoints are
still captured.

```json
"hooks": { "SessionStart": [ { "hooks": [ {
  "type": "command",
  "command": "pwsh -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"<ENGINE>\" -Snapshot -Auto",
  "timeout": 20
} ] } ] }
```

If no snapshot exists, `-FromSnapshot` falls back to Claude's own per-process
state files (`~/.claude/sessions/<pid>.json`) left behind by processes that are
gone. That covers a **hard crash / power loss**; it does NOT cover a clean
reboot, because Claude Code deletes its state file when a session exits normally
(verified 2026-07-26). So: take the snapshot. The fallback is insurance, not a
substitute.

## Otherwise: list, choose, launch

1. List resumable sessions as JSON:

   ```
   pwsh -NoProfile -File "<ENGINE>" -Json -Scan 200 -Top 40
   ```

   A directory usually holds MANY sessions and the user typically wants a specific
   one, so list them all rather than collapsing per directory. Add `-PerDir` only
   if they explicitly ask for "one per workspace" / "one per project".

2. Interpret `$ARGUMENTS`:
   - **empty** → present the JSON as a numbered list **grouped by working
     directory** (title · age · last prompt under each cwd heading), then ask which
     numbers to resume. Wait for their answer.
   - **`all`** → resume every listed session.

   Liveness comes from the engine, not from you: every entry carries
   `live: true/false`, resolved from Claude Code's own per-process state files
   (`~/.claude/sessions/<pid>.json`) with the pid validated. Resuming a live
   session opens a SECOND view of one conversation — mark those `⚡ live — skipped`,
   exclude them from `all`, and resume one only if the user names it explicitly.
   Do NOT infer liveness from the age column: measured 2026-07-26, a `3m ago`
   session was live and a `4m ago` one was dead, so any age cutoff is inside the
   noise. `-IncludeLive` overrides deliberately.
   - **a list like `1,3,5-7`** → resume those indices.
   - **anything else** → treat it as a `-Filter` substring; re-run step 1 adding
     `-Filter "<text>"`, then confirm the matches with the user.

3. Reopen the chosen sessions — **always by session id, never by list position**:

   ```
   pwsh -NoProfile -File "<ENGINE>" -SelectId "<id1>,<id2>"
   ```

   Map the user's chosen numbers back to the `id` fields from the step-1 JSON, and
   pass those. Do NOT pass `-Select <numbers>` here.

   ⚠️ Why: sessions sort by last-active time, and Claude Code rewrites session
   journals continuously — including the session you are running in. The list can
   therefore **reorder between your list call and your launch call**, so position 1
   in step 1 may be a different session by step 3. Ids are stable; positions are not.
   `-SelectId` also fails loudly (exit 3) if an id is no longer a candidate, instead
   of silently resuming a subset.

   `-DryRun` prints the resolved sessions to stdout without launching anything.

   Backends are picked automatically: Windows Terminal tabs on Windows, tmux
   windows if tmux is present, Terminal.app on macOS. Add `-Tmux` to force tmux —
   **that's the one to use over SSH**, since tmux windows survive a disconnect.
   Add `-SeparateWindows` for separate windows instead of tabs.

   Resumed tabs come up in COLOR even when you launch this from inside a Claude
   Code session. That is not free: Claude Code exports `NO_COLOR=1` to its
   subprocesses so tool output comes back clean, and the terminal spawned by this
   engine is one of those subprocesses — so the new window, every shell in it, and
   every `claude` inside those shells would inherit `NO_COLOR=1` and render
   monochrome. The engine scrubs it per tab (`Remove-Item Env:NO_COLOR` on the wt
   path, `unset NO_COLOR` on tmux). Keep that scrub if you edit the launch command.
   It must DELETE the variable, not blank it — the consumer tests
   `!("NO_COLOR" in process.env)`, i.e. presence, so `NO_COLOR=""` still suppresses
   colour. Worst on tmux: a server started from a Claude session keeps that
   environment for every window created later.

4. Report which sessions were reopened (titles + directories). If tmux was used,
   tell the user the attach command the engine printed.

Never resume a session whose working directory no longer exists (the engine
already skips those).
