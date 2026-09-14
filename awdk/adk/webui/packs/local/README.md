# Aitherium — Local AI (UI pack `local`)

The **Local AI** surface: a private assistant that lives on the machine. One
self-contained page (sidebar → Ask / Tasks / Visual / Build / Files / Mail)
served by `aither-serve` at `/`, wired to the daemon's real endpoints — no
cloud round-trip, no canned replies.

## Select it

```bash
adk ui set local     # persists; adk ui ls to confirm
adk up               # opens the browser at the Local AI page
```

`AITHER_AGENT_UI=local` also selects it per-process without persisting.

## What each tab actually calls

| Tab | Endpoint | Backing |
|---|---|---|
| Ask | `POST /v1/chat/completions` (SSE) | local model via the daemon's LLM backend (MicroScheduler on this box); stream:true with a JSON-content-type fallback |
| Tasks | `GET/POST /api/local/awrun*` | the real awrun queue (`adk.builtin_tools.queue_*`); cards submit `kind=agent` runs, queue polls every 6s, cancel works |
| Visual | `GET /api/local/images/backends` + `POST /api/local/images/generations` | `adk.images` discovery over ComfyUI / Sana / SD.Next already on loopback; starts nothing; 503 messages are written to be shown to a person |
| Build | `POST /v1/forge/dispatch` | AgentForge (agent "auto", effort 5); result shows agent, tokens, status |
| Files | File System Access API (browser) | files stay on disk; the working set is offered to the Ask agent as context |
| Mail | `GET /api/local/mail/status` + `POST /api/local/mail/send` | the **awmail** package — bridge (127.0.0.1:1025/1143) or SMTP; degrades cleanly when unconfigured |
| Newsletter | `POST /api/local/mail/send` | sends a confirmation note to the entered address when mail is up; local fallback |
| MCP | `GET /agents` + `GET /admin/mcp/servers` | the agent's real tool roster (37 tools on this box) + attached MCP servers; approvals note explains the `POST /sessions/{id}/confirm` review-and-continue loop |

Auth: the page is served by the daemon, so same-origin calls are loopback-
trusted. Over a public tunnel, the bearer is passed from the URL fragment
(`#k=…`, never in server logs) — the same model as the `minimal` pack.

## Honesty contract (the product rule)

- Every status shown is the real status: queue state from the awrun store,
  image backends probed live, mail availability from awmail's actual config.
  No mock data, no fake success.
- When the agent's request could go several directions it asks with an
  `Options:` bullet list instead of assuming; the page renders those as
  tappable follow-up chips (see the system prompt in the page).

## Mail config (optional, for the Mail tab to send)

```powershell
$env:AWMAIL_TRANSPORT = "bridge"          # or "smtp"
$env:AWMAIL_FROM      = "you@domain.com"
$env:AWMAIL_USER      = "you@domain.com"  # bridge mailbox username
$env:AWMAIL_PASSWORD  = "…"               # bridge/SMTP password
$env:AWMAIL_ALLOW     = "you@domain.com"  # required allowlist — unset refuses all sends
```

Without it the tab shows exactly what's missing and offers the fix.

## Files

- `index.html` — the whole pack (single file, inline CSS/JS, packaged by the
  existing `adk/webui/packs/**/*` package-data glob in pyproject.toml).
- The `/api/local/*` routes it calls live in `adk/local_routes.py`, mounted
  in `adk/server.py` (same auth plane as everything else).
