"""
GitHub App Plugin for AitherShell
==================================

Wraps the GitHub App credentials lifecycle for AitherFlow.

Usage:
    /github-app status                         — Show what's configured
    /github-app set-id <APP_ID>                — Store GITHUB_APP_ID
    /github-app set-install <INSTALLATION_ID>  — Store GITHUB_APP_INSTALLATION_ID
    /github-app load-key <PATH-TO-PEM>         — Read .pem and store GITHUB_APP_PRIVATE_KEY
    /github-app gen-webhook-secret             — Generate + store GITHUB_WEBHOOK_SECRET (idempotent)
    /github-app verify                         — Hit AitherFlow /github-app/status
    /github-app help

Aliases: /gha, /ghapp
"""

from __future__ import annotations
from adk._tls import tls_verify

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from adk.shell.plugins import SlashCommand
from adk.shell.plugins.builtins.secret import (
    Secret,
    _headers as _secrets_headers,
    _resolve_admin_key,
    _resolve_url as _secrets_url,
)


def _flow_url() -> str:
    return (
        os.environ.get("AITHERFLOW_URL")
        or os.environ.get("AITHER_FLOW_URL")
        or "http://localhost:8090"
    ).rstrip("/")


def _relay_url() -> str:
    return (
        os.environ.get("AITHERRELAY_URL")
        or os.environ.get("AITHER_RELAY_URL")
        or "http://localhost:8202"
    ).rstrip("/")


def _public_webhook_url() -> str:
    """Where GitHub will POST to. Override with AITHER_PUBLIC_WEBHOOK_URL."""
    return (
        os.environ.get("AITHER_PUBLIC_WEBHOOK_URL")
        or "https://flow.aitherium.com/webhooks/github"
    )


_HELP = """\
/github-app — manage AitherFlow's GitHub App credentials

  /github-app status
      Show which secrets are stored.
  /github-app permissions
      Print the EXACT permissions + events to tick on the GitHub App form.
  /github-app gen-webhook-secret [--rotate]
      Generate + store GITHUB_WEBHOOK_SECRET.
  /github-app set-id <APP_ID>
      Store GITHUB_APP_ID.
  /github-app set-install <INSTALLATION_ID>
      Store GITHUB_APP_INSTALLATION_ID.
  /github-app load-key <PATH-TO-PEM>
      Read the App private key file and store as GITHUB_APP_PRIVATE_KEY.
  /github-app link <workspace-slug> <owner/repo>
      Wire an AitherRelay workspace to a GitHub repo (so events post to its channel).
  /github-app verify
      Hit AitherFlow's /github-app/status endpoint.
  /github-app bootstrap
      One-shot: gen webhook secret + print permissions + show next steps.

Environment overrides:
    AITHERFLOW_URL         (default http://localhost:8090)
    AITHERRELAY_URL        (default http://localhost:8202)
    AITHER_SECRETS_URL     (default https://localhost:8111)
    AITHER_ADMIN_KEY       (or AITHER_INTERNAL_SECRET / AITHER_MASTER_KEY)
"""


_PERMISSIONS = """\
  Required GitHub App permissions
  ===============================

  REPOSITORY permissions:
    Actions ............... Read & write    (trigger/cancel/rerun workflows)
    Administration ........ Read-only       (read repo settings)
    Checks ................ Read & write    (post check runs)
    Code scanning alerts .. Read-only       (security triage)
    Commit statuses ....... Read & write    (CI status)
    Contents .............. Read & write    (branches, commits, file edits)
    Dependabot alerts ..... Read-only       (security triage)
    Deployments ........... Read & write    (track deploys)
    Discussions ........... Read & write    (knowledge graph)
    Environments .......... Read-only       (deployment context)
    Issues ................ Read & write    (create + comment)
    Metadata .............. Read-only       (always required)
    Pull requests ......... Read & write    (review, comment, merge)
    Secret scanning alerts. Read-only       (security)
    Secrets ............... Read & write    (sync from AitherSecrets)
    Webhooks .............. Read & write    (manage hooks)
    Workflows ............. Read & write    (modify .github/workflows)

  ORGANIZATION permissions:
    Members ............... Read-only       (org directory)
    Projects .............. Read & write    (roadmap sync)

  ACCOUNT permissions:
    Email addresses ....... Read-only       (sign-in)

  SUBSCRIBE TO EVENTS:
    [x] Check run               [x] Discussion
    [x] Check suite             [x] Discussion comment
    [x] Code scanning alert     [x] Issues
    [x] Commit comment          [x] Issue comment
    [x] Create                  [x] Label
    [x] Delete                  [x] Member
    [x] Dependabot alert        [x] Milestone
    [x] Deploy key              [x] Pull request
    [x] Deployment              [x] Pull request review
    [x] Deployment status       [x] Pull request review comment
    [x] Push
    [x] Release
    [x] Repository
    [x] Repository dispatch
    [x] Secret scanning alert
    [x] Star
    [x] Status
    [x] Workflow dispatch
    [x] Workflow job
    [x] Workflow run

  Where can this GitHub App be installed?
    \"Any account\" if you want users to install on their forks too,
    \"Only on this account\" for org-internal only.
"""


def _bootstrap_steps(webhook_url: str, secrets_url: str) -> str:
    return f"""\

  AitherOS GitHub App — Bootstrap Checklist
  ==========================================

  1. Webhook secret already in your clipboard.
     Paste into the \"Secret\" field on github.com/settings/apps/new.

  2. Webhook URL field:
       {webhook_url}

  3. Click the permissions per /github-app permissions.

  4. Subscribe to the events per /github-app permissions.

  5. Click \"Create GitHub App\". GitHub takes you to the settings page.

  6. Copy the App ID at the top, then back here:
       /github-app set-id <APP_ID>

  7. Scroll down → \"Generate a private key\". Save the .pem file, then:
       /github-app load-key <path-to-pem>

  8. Left sidebar → \"Install App\" → install on Aitherium org → choose repos.
     URL bar shows installations/<id>. Copy it, then:
       /github-app set-install <INSTALL_ID>

  9. Wire each repo to its workspace channel (optional):
       /github-app link aitherium Aitherium/AitherOS

  10. Restart AitherFlow + AitherRelay so they pick up the secrets:
        docker compose restart aitheros-flow aitheros-relay

  11. Verify:
        /github-app verify
        /github-app status

  12. On github.com → Recent Deliveries → re-deliver the ping.
      Should show 200 OK with body {{\"success\": true, \"action\": \"github_app_ping_acknowledged\"}}.
"""


_KEYS = [
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_WEBHOOK_SECRET",
]


class GitHubApp(SlashCommand):
    def __init__(self):
        super().__init__()
        self.name = "github-app"
        self.description = "Manage GitHub App credentials for AitherFlow"
        self.aliases = ["gha", "ghapp"]
        self.hidden = False

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help", "?"):
            return _HELP
        if not _resolve_admin_key():
            return (
                "[!!] No admin key found in env.\n"
                "     Set AITHER_ADMIN_KEY (or AITHER_INTERNAL_SECRET / AITHER_MASTER_KEY)."
            )

        sub = args[0].lower()
        rest = args[1:]
        try:
            if sub == "status":
                return await self._status()
            if sub in ("permissions", "perms"):
                return _PERMISSIONS
            if sub == "bootstrap":
                return await self._bootstrap(ctx)
            if sub in ("gen-webhook-secret", "gen-secret", "webhook"):
                rotate = "--rotate" in rest or "--overwrite" in rest
                return await Secret().run(
                    ["gen", "GITHUB_WEBHOOK_SECRET"] + (["--rotate"] if rotate else []),
                    ctx,
                )
            if sub == "set-id":
                if not rest:
                    return "Usage: /github-app set-id <APP_ID>"
                return await Secret().run(["set", "GITHUB_APP_ID", rest[0]], ctx)
            if sub in ("set-install", "set-installation"):
                if not rest:
                    return "Usage: /github-app set-install <INSTALLATION_ID>"
                return await Secret().run(
                    ["set", "GITHUB_APP_INSTALLATION_ID", rest[0]], ctx
                )
            if sub == "load-key":
                if not rest:
                    return "Usage: /github-app load-key <PATH-TO-PEM>"
                return await self._load_key(rest[0], ctx)
            if sub == "link":
                if len(rest) < 2:
                    return "Usage: /github-app link <workspace-slug> <owner/repo>"
                return await self._link_workspace(rest[0], rest[1])
            if sub == "verify":
                return await self._verify()
            return f"Unknown subcommand: {sub}\n\n{_HELP}"
        except httpx.HTTPError as e:
            return f"[!!] HTTP error: {e}"

    # ------------------------------------------------------------------
    async def _status(self) -> str:
        url = _secrets_url()
        results: Dict[str, str] = {}
        async with httpx.AsyncClient(timeout=15.0, verify=tls_verify()) as client:
            for name in _KEYS:
                r = await client.get(f"{url}/secrets/{name}", headers=_secrets_headers())
                if r.status_code == 200:
                    try:
                        v = r.json().get("value", "")
                    except Exception:
                        v = ""
                    if v:
                        if name == "GITHUB_APP_PRIVATE_KEY":
                            results[name] = f"[OK] stored ({len(v)} chars)"
                        elif "WEBHOOK" in name or "PRIVATE" in name:
                            results[name] = f"[OK] stored ({v[:6]}…{v[-4:]})"
                        else:
                            results[name] = f"[OK] {v}"
                    else:
                        results[name] = "[--] empty"
                elif r.status_code == 404:
                    results[name] = "[--] not set"
                else:
                    results[name] = f"[!!] HTTP {r.status_code}"

        out = ["", "  GitHub App credentials in AitherSecrets", "  " + "=" * 39]
        for name in _KEYS:
            out.append(f"  {name:<32} {results.get(name, '[?]')}")
        out += [
            "",
            "  Next steps if missing:",
            "    /github-app gen-webhook-secret",
            "    /github-app set-id <APP_ID>",
            "    /github-app set-install <INSTALLATION_ID>",
            "    /github-app load-key C:\\path\\to\\app.pem",
            "    /github-app verify",
            "",
        ]
        return "\n".join(out)

    async def _load_key(self, path: str, ctx: Dict[str, Any]) -> str:
        p = Path(path).expanduser()
        if not p.exists():
            return f"[!!] File not found: {p}"
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            return f"[!!] Could not read {p}: {e}"
        if "BEGIN" not in text or "PRIVATE KEY" not in text:
            return f"[!!] {p} does not look like a PEM private key."
        # Store the actual key
        await Secret().run(["set", "GITHUB_APP_PRIVATE_KEY", text], ctx)
        # Also store the path for reference
        await Secret().run(["set", "GITHUB_APP_PRIVATE_KEY_PATH", str(p.resolve())], ctx)
        return f"[OK] Loaded {len(text)} bytes from {p} → GITHUB_APP_PRIVATE_KEY"

    async def _verify(self) -> str:
        flow = _flow_url()
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=tls_verify()) as client:
                r = await client.get(f"{flow}/github-app/status")
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            return f"[!!] Could not reach AitherFlow at {flow}: {e}"

        lines = [
            "",
            f"  AitherFlow GitHub App status @ {flow}",
            "  " + "=" * (32 + len(flow)),
            f"  auth_mode                   : {data.get('auth_mode')}",
            f"  github_configured           : {data.get('github_configured')}",
            f"  app_id_configured           : {data.get('app_id_configured')}",
            f"  private_key_configured      : {data.get('private_key_configured')}",
            f"  installation_id_configured  : {data.get('installation_id_configured')}",
            f"  webhook_secret_configured   : {data.get('webhook_secret_configured')}",
            f"  repository                  : {data.get('repository')}",
            f"  direct_webhook_path         : {data.get('direct_webhook_path')}",
            "",
        ]
        return "\n".join(lines)

    async def _link_workspace(self, slug: str, repo: str) -> str:
        """Wire an AitherRelay workspace to a GitHub repo for channel notifications."""
        relay = _relay_url()
        admin = _resolve_admin_key()
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=tls_verify()) as client:
                r = await client.put(
                    f"{relay}/v1/workspaces/{slug}/integrations",
                    json={"github_repo": repo},
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {admin}" if admin else "",
                    },
                )
        except Exception as e:
            return f"[!!] Could not reach AitherRelay at {relay}: {e}"

        if r.status_code == 200:
            return (
                f"[OK] Linked workspace '{slug}' → {repo}\n"
                f"     GitHub events for this repo will post to #{slug}-general "
                f"and #aitherium-builds via the AitherFlow → AitherRelay fan-out."
            )
        if r.status_code == 404:
            return f"[!!] Workspace '{slug}' not found on AitherRelay."
        if r.status_code == 403:
            return f"[!!] Admin role required on workspace '{slug}'."
        return f"[!!] Relay returned HTTP {r.status_code}: {r.text[:200]}"

    async def _bootstrap(self, ctx: Dict[str, Any]) -> str:
        """One-shot bootstrap: generate webhook secret + print full instructions."""
        secret_out = await Secret().run(["gen", "GITHUB_WEBHOOK_SECRET"], ctx)
        steps = _bootstrap_steps(_public_webhook_url(), _secrets_url())
        return f"{secret_out or ''}\n{_PERMISSIONS}\n{steps}"
