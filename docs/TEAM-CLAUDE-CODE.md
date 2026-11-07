# Claude Code for the Aitherium team

One command sets up a teammate's machine. It never asks for, writes or emails a
secret: every credential comes from the teammate's own sign-in.

## Teammate: install

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://github.com/Aitherium/awdk/releases/latest/download/aitherium-team.ps1 | iex"
```

macOS / Linux:

```sh
curl -LsSf https://github.com/Aitherium/awdk/releases/latest/download/aitherium-team.sh | sh
```

It is idempotent; re-run it any time. Steps, in order:

| step | what | credential |
|---|---|---|
| 1 | `uv tool install awdk awsettings` | none |
| 2 | clone `Aitherium/AitherOS` to `$AITHER_REPO` (default `~/AitherOS-Fresh`) when missing | your GitHub access |
| 3 | `adk login` (browser device flow) | your Aitherium account |
| 4 | `mint_session_bearer.py` writes `~/.aither/session-bearer` | derived from step 3, per user, mode 600 |
| 5 | `awsettings --quiet pull` (team Claude Code settings) | your login |
| 6 | copy the `awknowledge` doctrine skill to `~/.claude/skills/` | none |
| 7 | add the `aitheros` stdio-bridge entry to `<repo>/.mcp.json`; add `aitheros-cloud` too when no gateway answers on `127.0.0.1:8182` | the bridge reads `~/.aither/session-bearer` |
| 8 | `check_mcp_client_config.py --list` | none |

Skip steps with `AITHER_TEAM_SKIP=login,settings,doctrine,mcp`.

Then open Claude Code in the checkout and run `/awknowledge` once to apply the
doctrine.

## Operator: add a teammate

Run from the dev-tools folder of the AitherOS checkout:

```sh
python aitherium_teammate.py add pat@example.com --role member --proton pat
python aitherium_teammate.py status
python aitherium_teammate.py proton-confirm pat@example.com
```

- `add` sends the existing workspace invite (account, Company Room, role) with
  `onboarding=claude-code`. When the teammate accepts, they get ONE email,
  "Set up Claude Code for Aitherium", with the install command above. It is
  transactional, carries an unsubscribe link, and contains no secret.
- `add` raises one owner decision card for the teammate's **Claude Code seat**.
  No admin API for seats exists, so the owner adds it in Anthropic's console.
- `--proton <local>` raises one owner card naming `<local>@aitherium.com` to
  create in Proton Business admin. A repeat `add` raises no second card.
- `proton-confirm` refuses until the owner answers the card `created`. Then it
  records the mailbox in the team mailbox list and prints the mail-plane steps
  (the IMAP account list and the vault NAME of the Bridge password; never a
  value).

Exit codes for every verb: `0` done, `1` refused or not ready, `2` could not
reach Genesis, the decision daemon or the operator bearer.

`python aitherium_teammate.py --self-test` proves the secret
scanner fires, the mail template is clean and the card flow is one-per-teammate.
