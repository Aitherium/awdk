# Plan B Ledger — one ledger, two faces

Your checkbook, made durable. A **digital face** (Discord bot + CLI) and a
**paper face** (printable continuity sheets) that stay interchangeable:

- While tech is up, tell the bot what happened in plain language —
  `paid the electric bill`, `spent 34.50 on groceries` — and it keeps the ledger.
- Print a **Plan B sheet** (`!sheet`) any time: a structured worksheet with your
  bills as tick-boxes and blank rows with one-digit-per-box amounts.
  Keep it with the checkbook.
- **When tech goes down, business does not.** Work on paper: tick bills as you
  pay them, write purchases in the rows, keep the running balance in pencil.
- When tech comes back, one message merges the paper back:
  `!reconcile PB-0003 paid: electric, internet; spent 34.50 groceries`
  A bill paid on *both* faces is caught as a conflict — never double-counted.

Everything is **local-first**: your ledger is plain JSON at `~/.aither/planb/`,
readable, backupable, yours. No cloud. The optional AI brain (bonsai-27b via
llama.cpp) runs on your own machine too.

## Install

**Windows:** right-click `install.ps1` → *Run with PowerShell*
**macOS/Linux:** `bash install.sh`

The wizard checks Python 3.10+, installs the two dependencies into a private
virtualenv, walks you through creating your Discord bot (2 minutes), detects a
local llama.cpp model if you have one, and offers demo data.

## Use

| You type (Discord DM or `#planb` channel) | It does |
|---|---|
| `paid the electric bill` | records the bill payment, shows new balance |
| `spent 23.75 on lunch` | records an expense, auto-categorized |
| `got my paycheck 1850` | records income |
| `!status` | balance, bills checklist, recent entries |
| `!sheet` | prints checkpoint sheet PB-NNNN (HTML file → Ctrl+P) |
| `!reconcile PB-0003 paid: electric; spent 12 coffee` | merges paper marks |
| `!brain` | shows whether local AI or the built-in parser is answering |

No Discord? Same thing on the command line: `planb.cmd demo` (Windows) /
`./planb demo` (mac/Linux) runs the full 60-second loop.

## The idea

Checkbooks kept people sharp: you always knew what you had, what you could
afford, what you were risking. Banks post transactions on *their* schedule;
this ledger posts on **yours** — every entry written or confirmed by you, with
intention. The paper sheet isn't a backup, it's a co-equal interface: printed
from a checkpoint, reconciled against that checkpoint, so the two faces can
never silently disagree.

*Built on the Aitherium ADK. Local AI: bonsai-27b (llama.cpp) — runs on a
plain CPU with 4GB RAM, no GPU, no internet.*
