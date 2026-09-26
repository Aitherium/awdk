"""`/mail` — AitherMail (.MAIL product) from the shell.

    /mail status                           SMTP service status (queue, config)
    /mail doctor                           per-sender health (platform operators)
    /mail domains <workspace>              BYO domains registered to a workspace
    /mail domain add <domain> <workspace>  register a domain; prints the DNS records
    /mail domain verify <domain> <workspace>
                                           re-check MX/SPF/DKIM/DMARC/verification TXT

Every verb is one MCP tool call on the platform gateway (`smtp_status`,
`smtp_doctor`, `workspace_mail_domain_list|add|verify`), authenticated as the
logged-in session (~/.aither/session-bearer). The gateway -- not this plugin --
decides the tenant: a tenant caller always acts on its own tenant, and the doctor
refuses anyone who is not a platform operator.

Gateway: AITHER_GATEWAY_URL, else the local gateway http://127.0.0.1:8182
(127.0.0.1, never localhost -- ::1:8182 refuses).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from adk.shell.plugins import SlashCommand

_DEFAULT_GATEWAY = "http://127.0.0.1:8182"


def _make_client():
    """Build the gateway MCP client. Module-level so tests can replace it."""
    from adk.client._gateway_mcp import GatewayMCPClient

    return GatewayMCPClient(gateway_url=os.getenv("AITHER_GATEWAY_URL", "") or _DEFAULT_GATEWAY)


def plan(args: List[str]) -> Tuple[Optional[str], Dict[str, Any], str]:
    """Translate a `/mail` invocation into ``(tool, arguments, usage_error)``.

    ``tool`` is None when the invocation is help or a usage error; the third
    element is then the text to show.
    """
    sub = args[0].lower() if args else "status"
    rest = args[1:]
    if sub in ("help", "-h", "--help"):
        return None, {}, __doc__ or ""
    if sub == "status":
        return "smtp_status", {}, ""
    if sub == "doctor":
        return "smtp_doctor", {}, ""
    if sub == "domains":
        if not rest:
            return None, {}, "Usage: /mail domains <workspace>"
        return "workspace_mail_domain_list", {"workspace_id": rest[0]}, ""
    if sub == "domain":
        verb = rest[0].lower() if rest else ""
        if verb in ("add", "verify") and len(rest) >= 3:
            return (f"workspace_mail_domain_{verb}",
                    {"domain": rest[1], "workspace_id": rest[2]}, "")
        if verb in ("list", "ls") and len(rest) >= 2:
            return "workspace_mail_domain_list", {"workspace_id": rest[1]}, ""
        return None, {}, "Usage: /mail domain add|verify <domain> <workspace>"
    return None, {}, f"Unknown subcommand {sub!r}\n\n{__doc__}"


class MailPlugin(SlashCommand):
    """AitherMail — SMTP health and BYO workspace domains."""

    name = "mail"
    description = "AitherMail: SMTP status/doctor and BYO workspace mail domains"
    aliases = ["smtp"]

    def __init__(self) -> None:
        # Explicit: the dataclass base assigns name="" on the instance otherwise.
        super().__init__(
            name="mail",
            description="AitherMail: SMTP status/doctor and BYO workspace mail domains",
            aliases=["smtp"],
        )

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        tool, arguments, text = plan(args)
        if tool is None:
            return text
        client = _make_client()
        if not getattr(client, "api_key", ""):
            return ("No gateway credential: log in first (~/.aither/session-bearer "
                    "is empty). Not calling the gateway anonymously.")
        ensure = getattr(client, "_ensure_session", None)
        if ensure is not None and not await ensure():
            return f"Gateway MCP session could not be opened at {client.gateway_url}."
        result = await client.call_tool(tool, arguments)
        if result.get("error"):
            msg = result.get("message") or result.get("status") or ""
            return f"/mail: {tool} failed: {result['error']} {msg}".rstrip()
        return result.get("text") or "(the tool returned no content)"
