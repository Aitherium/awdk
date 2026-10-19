---
name: claude-code-hooks-fast
description: >-
  Make Claude Code hooks cheap, especially on Windows, where every shell-form hook quietly starts four processes on every tool call. Covers exec-form hooks, measuring a hook's real process count, the PowerShell Start-Job trap, fail-soft without `|| true`, and pasting or auto-announcing screenshots.
---
<!-- Generated from the awskills pack (skills/claude-code-hooks-fast.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

# claude-code-hooks-fast — hooks that cost one process, not four

Hooks run on every tool call, so their cost is paid thousands of times a day.
On Windows that cost is mostly invisible process overhead. Over enough uptime it
can show up as a machine where every program starts slowly (see `windows-kernel-leak`).

> Measured 2026-10-18 on a Windows workstation running ~10 Claude Code sessions: one Bash
> tool call started **24 hook processes** and spent several seconds in hooks. The
> machine was creating ~25 processes per second, and the kernel leaked a token on that
> path until it held 29 GB of pool. After the steps below: **12** processes per Bash
> call, and the heaviest hook went from 2.5 s to about 1 s.

## 1. Use exec form, not shell form

A shell-form hook (`"command": "python x.py"`) runs through bash. On Windows that is
Git's `bash.exe` launcher, then the real bash, then a console host, and only then
your program: **4 processes**. Exec form runs the program directly:

```json
{ "type": "command",
  "command": "python",
  "args": ["-S", "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.py"],
  "timeout": 10 }
```

Verified live:
- `${CLAUDE_PROJECT_DIR}` is expanded inside `args`.
- `CLAUDE_PROJECT_DIR` is also set in the hook's environment.
- The working directory is the project root.

That makes a leading `cd "$CLAUDE_PROJECT_DIR" &&` redundant.

Exit 2 and stderr still block exactly as before.

**Prove it before you switch.** A hook that needs a shell (pipes, `||`, `&&`,
redirects) must stay in shell form. For everything else, feed the same payload through
both forms and compare exit code and output. A blocking guard that silently stops
blocking is worse than a slow one.

`adk claude doctor` flags each remaining one-program shell-form Pre/PostToolUse hook
(CCD007) and times exec-form hooks by their real args (CCD004).

## 2. Count processes, not milliseconds

Wall time is noisy on a busy machine. The real cost is the number of processes a hook
starts. Measure it with a Windows **job object**, which counts every process a
command and all its descendants ever start, without admin rights:

- `CreateJobObject`;
- start the hook with `CREATE_SUSPENDED`, `AssignProcessToJobObject`, resume it;
- after it exits, read `QueryInformationJobObject` → `TotalProcesses`.

## 3. Traps inside the hook script itself

- **PowerShell `Start-Job` starts a whole new `pwsh` process** (plus a console host).
  For a fire-and-forget telemetry POST, an inline `Invoke-RestMethod -TimeoutSec 2`
  costs about 40 ms. The job costs two processes and a PowerShell startup.
- **`-Scope Global` does not survive the hook.** Each hook run is a fresh process, so
  "run maintenance every 4 hours" keyed on a global variable runs on *every call*.
  Keep the clock in a file's modification time.
- **A console program started from a console-less parent gets its own `conhost.exe`.**
  `pythonw.exe` avoids that, but only exists on Windows. Use it only in a user-scope
  settings file that is Windows-only anyway, never in a repo file a Linux container
  might also read.

## 4. Fail-soft without a shell

`cmd || true` needs a shell. Give the tool a flag instead: exit 0 whatever happens,
catching bad arguments and crashes. awsettings ships this as `--never-fail`, so
its push hook (which runs after every tool call) needs no shell.

## 5. Screenshots: paste, or let a hook announce them

- **Paste:** in the Claude Code prompt on Windows, an image pastes with **Alt+V**.
  Ctrl+V belongs to the terminal and pastes text only.
- **Announce:** a `UserPromptSubmit` hook stats the screenshots folder for images
  newer than the session's last prompt. It injects their *paths* as
  `additionalContext`, and the model opens one with Read only when the prompt is about
  it. A path costs a few tokens; an attached image costs hundreds. Keep a per-session
  cursor so each screenshot is announced once, look back ~10 minutes on the first
  prompt, and print nothing when there is nothing new.
