# AWSH — drive any coding agent from one shell

**Status: working today.** Verified end to end on a Windows host 2026-09-06, including
two agent CLIs installed *during* the verification and picked up with no code change.

This is the install-and-prove path. It exists because the capability was already
built and was effectively undiscoverable: the design is documented at
`README.md` ("Subagents — drive Claude Code, Codex, and eight more"), roughly 200
lines into a 1000-line file, and nothing told you that the harness daemon has to
be running before any of it answers. Two separate multi-agent audits of this
repo concluded "zero working external agents" — both were wrong, and both were
wrong because they read manifests instead of starting the daemon and asking it.

If you take one thing from this page: **ask the daemon, don't read the table.**

---

## What you get

One shell that drives real agent CLIs — the actual products, not
reimplementations against a raw API — plus real terminals, containers and your
own sovereign agents. Sessions live in the daemon, not in the client, so you can
start work on a laptop and re-attach from a phone.

| Harness    | Transport         | What it is |
|------------|-------------------|------------|
| `claude`   | structured-bidi   | Anthropic Claude Code, bidirectional stream-json, full tool use |
| `gemini`   | oneshot-per-turn  | Google Gemini CLI |
| `codex`    | oneshot-per-turn  | OpenAI Codex CLI |
| `opencode` | oneshot-per-turn  | OpenCode |
| `aider`    | oneshot-per-turn  | Aider pair-programming CLI |
| `terminal` | pty-stream        | A genuine pseudo-terminal (pwsh/bash) — ANSI, curses, colours |
| `sandbox`  | pty-stream        | A real Linux TTY inside a dev-workspace container |
| `aither`   | http-stream       | A sovereign agent (Aither/Atlas/Lyra/Aeon…) over Genesis SSE |
| `group`    | http-stream       | Several agents in one room, answering concurrently |

Adding a tenth is a table row in `adk/harnesses/registry.py`, not a new module.
That file is the source of truth; everything below just reads it.

---

## 1. Install the agent CLIs you want

Only the ones you want — a harness you have not installed reports as *not
installed*, with the command to fix it. It is never silently hidden.

```bash
npm  i -g @openai/codex          # codex
npm  i -g opencode-ai            # opencode
pip  install aider-install && aider-install   # aider (two steps: the pip
                                 # package is the INSTALLER, not aider)
```

`claude` and `gemini` are their own installs. Docker Desktop enables `sandbox`.

## 2. Ask what this machine can drive

No daemon needed for this one — it reads the registry directly:

```bash
cd awdk
python -c "
from adk.harnesses.registry import detect
for r in detect():
    print(f\"{r['id']:10} {'yes' if r['installed'] else 'NO':4} {r.get('path') or r.get('install_hint','')}\")
"
```

Real output from the verification run, immediately after the installs above:

```
claude     yes  %APPDATA%\npm\claude.CMD
gemini     yes  %APPDATA%\npm\gemini.CMD
terminal   yes
sandbox    NO   Install Docker Desktop
aither     yes
group      yes
codex      yes  %APPDATA%\npm\codex.CMD
aider      NO   pip install aider-install && aider-install
opencode   yes  %APPDATA%\npm\opencode.CMD
```

Detection is live `PATH` resolution honouring `PATHEXT`, which is why the
Windows `.CMD` shims resolve. Install a CLI and it appears on the next call —
there is no registration step and no restart.

## 3. Start the harness daemon

**This is the step whose absence makes everything look broken.** Without it the
desktop app reports "No harnesses reported by the daemon yet" and `adk shell`
subcommands fail against an unreachable backend.

```bash
cd awdk
python packaging/daemon_entry.py harness serve --host 127.0.0.1 --port 8362
```

Frozen builds expose the identical verbs — the argv contract is deliberate and
load-bearing, see the header of `packaging/daemon_entry.py`:

```bash
awdaemons harness serve --host 127.0.0.1 --port 8362
```

Confirm:

```bash
curl -s http://127.0.0.1:8362/health
```

```json
{"ok":true,"service":"aithershell-harness","sessions":0,
 "harnesses_installed":["claude","gemini","terminal","aither","group","codex","aider","opencode"],
 ...}
```

`harnesses_installed` is the whole proof. Eight of the nine harnesses are live;
`codex`, `opencode` and `aider` are in that list because they were installed
minutes earlier, and no code, config or restart was involved. `aider` in
particular appeared in an **already-running** daemon — detection is live `PATH`
resolution per request, not a boot-time snapshot, so you can install a CLI
mid-session and use it immediately.

Only `sandbox` remains, and only because Docker Desktop is not installed.

## 4. Use it

From the Living OS desktop, open **AitherShell**
(`AitherOS/apps/AitherVeil/src/components/os/apps/aithershell.tsx`). It `GET`s
`/harnesses` and renders whatever the daemon reports — the harness list is data,
so a newly installed CLI appears with no front-end change. The daemon already
allows `https://aitherium.com` in `cors_origins`.

Or from the CLI:

```bash
adk shell harnesses            # what can this machine drive
adk shell new --harness codex
adk shell send  <id> "refactor the retry logic in billing/"
adk shell attach <id>          # watch it work
adk shell kill  <id>           # teardown
```

---

## Verifying the whole chain

Four links, each independently checkable. When something is wrong, this tells
you *which* link — the failure mode that cost the most time here was assuming a
broken feature when the daemon simply was not running.

| # | Link | Check | Healthy |
|---|------|-------|---------|
| 1 | Registry | the `detect()` snippet above | rows printed |
| 2 | Binaries | `installed` column | `yes` for what you installed |
| 3 | Daemon | `curl /health` | `harnesses_installed` non-empty |
| 4 | UI | `GET /harnesses` with a bearer token | rows with `installed` |

Token for links 3–4:

```bash
python -c "from adk.harnesses.daemon import resolve_token; print(resolve_token(''))"
curl -s -H "Authorization: Bearer $TOK" http://127.0.0.1:8362/harnesses
```

`resolve_token` is the daemon's own resolution (explicit → file → minted). Do not
pass `token=""` to `serve()`: it is accepted and then rejects every caller, which
is a daemon that starts and refuses everyone.

---

## Known gaps (honest list)

- **`sandbox` needs Docker Desktop.** Not a code gap.
- **Per-harness affordances are thin.** `aithershell.tsx` sets
  `permission_mode: 'acceptEdits'` for `claude` and `gemini` only; `codex`,
  `opencode` and `aider` launch without one. Works, but not yet tuned per agent.
- **Governance is not wired into the daemon.** `awgit` leases are enforced at
  the tool layer (`adk/tool_guards.py`), but `awbac`/`awiam`/`awdit` are not
  imported by awdk and are not published, so per-client identity and per-action
  RBAC do not exist here yet. The daemon authenticates with a single shared
  bearer token. This is the real remaining work, not the agent integration.
- **No supervision.** Nothing restarts the daemon if it dies; on Windows it is
  started ad hoc rather than as a service.

## Do not

- **Do not conclude a harness is unsupported because the desktop app does not
  list it.** The app lists what the *daemon* reports; an empty list almost always
  means the daemon is not running.
- **Do not add a per-agent runner module.** A harness is a `HarnessSpec` row.
  `claude_runner.py` + `codex_runner.py` + `gemini_runner.py` is exactly the
  drift this design exists to prevent.
- **Do not pass prompts in argv.** The runner feeds them over stdin on purpose —
  argv is visible in the process table.
