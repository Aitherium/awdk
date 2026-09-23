"""`adk mail setup --provider proton` -- one command from Bridge to a delivered test mail.

Before this verb, standing up sovereign mail meant several separate tools, a
hand-edited .env and a health check nobody remembered to run. This verb runs
the whole flow against a
running MailCore and stops at the FIRST step that fails, saying which:

  1. bridge     Probe the Proton Bridge SMTP listener. A Bridge with no account
                logged in still accepts TCP and greets "220 ESMTP" -- it only
                fails at AUTH -- so when a Bridge credential is available the
                probe LOGS IN, and a refused login fails the step. With a
                credential, the step then SEEDS MailCore by POSTing it to
                /smtp/config (a Bridge re-login mints a NEW SMTP password, so
                MailCore must be told). Without one, MailCore is left to seed itself from
                the vault in step 2.
  2. bootstrap  POST /smtp/bootstrap -- MailCore re-reads the vault and reports
                whether it is configured. Not configured = fail.
  3. doctor     GET /smtp/doctor -- every REQUIRED sender address must
                authenticate (send-as) on the Bridge account.
  4. test       POST /smtp/config/test with an explicit recipient. Skipped (and
                said so) without --to: a test with no recipient falls back to
                the from-address, i.e. the platform mailing itself, which proves
                nothing about delivery.

Credentials never go on the command line: the Bridge password is read from
$AITHER_SMTP_PASS (the vault key MailCore itself uses) or prompted for when a
human is at the terminal. MailCore is authenticated with the saved adk API key
($AITHER_API_KEY); the server's RBAC decides whether the caller may configure
mail -- this verb grants nothing.

Exit codes: 0 every run step passed, 1 a step failed, 2 bad usage.
"""

from __future__ import annotations

import os
import smtplib
import sys
from typing import Any, Callable

PROVIDERS = ("proton",)

# Where the host's Proton Bridge listens (desktop Bridge: loopback :1025).
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 1025
# The host the FLEET uses to reach that Bridge -- what MailCore gets configured
# with. Matches the protonmail preset in AitherSMTP.PROVIDER_PRESETS.
DEFAULT_FLEET_BRIDGE_HOST = "host.docker.internal"
# MailCore (services.yaml: MailCore port 8206). Every fleet service speaks TLS;
# plain http:// into one hangs, so the default is https.
DEFAULT_MAIL_URL = "https://127.0.0.1:8206"


def mail_url(explicit: str = "") -> str:
    return (explicit or os.environ.get("AITHER_MAIL_URL", "") or DEFAULT_MAIL_URL).rstrip("/")


def _auth_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("AITHER_API_KEY", "")
    if not api_key:
        try:
            from adk.config import load_saved_config
            api_key = (load_saved_config() or {}).get("api_key", "") or ""
        except Exception:  # noqa: BLE001 -- no saved config is a normal state
            api_key = ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def probe_bridge(host: str, port: int, username: str = "", password: str = "",
                 timeout: float = 10.0) -> tuple[bool, str]:
    """Connect to the Bridge SMTP listener; when a credential is given, LOG IN.

    A port that answers proves nothing (an unseeded Bridge greets happily), so
    a successful AUTH is the only pass when a credential is available.
    """
    try:
        s = smtplib.SMTP(host, port, timeout=timeout)
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"Bridge not reachable on {host}:{port}: {exc}"
    try:
        s.ehlo()
        if s.has_extn("starttls"):
            import ssl
            ctx = ssl.create_default_context()
            # Proton Bridge serves a self-signed loopback certificate; the hop
            # is 127.0.0.1 -> 127.0.0.1, so the name/CA check has nothing to
            # verify against. Scoped to this local probe only.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s.starttls(context=ctx)
            s.ehlo()
        if not (username and password):
            return True, ("Bridge is listening (not logged in to verify: no "
                          "credential given -- an unseeded Bridge also answers)")
        try:
            s.login(username, password)
        except smtplib.SMTPAuthenticationError:
            return False, ("Bridge refused the login -- no account is logged in to "
                           "Bridge, or the Bridge SMTP password changed (a Bridge "
                           "re-login mints a new one)")
        return True, f"Bridge accepted the login for {username}"
    except (OSError, smtplib.SMTPException) as exc:
        return False, f"Bridge SMTP handshake failed: {exc}"
    finally:
        try:
            s.quit()
        except (OSError, smtplib.SMTPException):
            s.close()  # the server already hung up; just drop the socket


def _resolve_password(args: Any, interactive: bool) -> str:
    pw = os.environ.get("AITHER_SMTP_PASS", "")
    if pw or not getattr(args, "bridge_user", ""):
        return pw
    if interactive:
        import getpass
        return getpass.getpass("  Proton Bridge SMTP password (from Bridge > mailbox details): ")
    return ""


def run_mail_setup(args: Any, client: Any,
                   bridge_probe: Callable[..., tuple[bool, str]] = probe_bridge,
                   interactive: bool = False,
                   out: Callable[[str], None] = print) -> tuple[int, list[dict]]:
    """Run the flow. ``client`` is an httpx.Client-shaped object. Returns (exit, steps)."""
    base = mail_url(getattr(args, "mail_url", "") or "")
    headers = _auth_headers()
    steps: list[dict] = []

    def record(name: str, ok: bool | None, detail: str) -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail})
        mark = "+" if ok else ("-" if ok is None else "x")
        out(f"  [{mark}] {name:<9} {detail}")
        return bool(ok)

    def call(method: str, path: str, **kw: Any) -> tuple[int, Any]:
        resp = client.request(method, f"{base}{path}", headers=headers, **kw)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 -- non-JSON error page
            body = {"raw": (getattr(resp, "text", "") or "")[:200]}
        return resp.status_code, body

    def http_fail(name: str, code: int, body: Any) -> tuple[int, list[dict]]:
        if code in (401, 403):
            hint = ("MailCore refused the caller -- configuring platform mail "
                    "needs a platform-operator credential (set AITHER_API_KEY)")
        else:
            hint = f"HTTP {code}: {str(body)[:160]}"
        record(name, False, hint)
        return 1, steps

    # 1. bridge -----------------------------------------------------------
    user = getattr(args, "bridge_user", "") or ""
    password = _resolve_password(args, interactive)
    ok, detail = bridge_probe(args.bridge_host, int(args.bridge_port), user, password)
    if not record("bridge", ok, detail):
        return 1, steps
    if user and password:
        payload = {
            "provider": "protonmail",
            "host": args.fleet_bridge_host,
            "port": int(args.fleet_bridge_port or args.bridge_port),
            "username": user,
            "password": password,
            "from_address": getattr(args, "from_address", "") or user,
            "use_tls": True,
            "use_ssl": False,
        }
        try:
            code, body = call("POST", "/smtp/config", json=payload)
        except Exception as exc:  # noqa: BLE001 -- transport error names MailCore
            record("seed", False, f"MailCore unreachable at {base}: {exc}")
            return 1, steps
        if code >= 400:
            return http_fail("seed", code, body)
        record("seed", True, f"MailCore configured for {payload['from_address']} "
                             f"via {payload['host']}:{payload['port']}")

    # 2. bootstrap ----------------------------------------------------------
    try:
        code, body = call("POST", "/smtp/bootstrap")
    except Exception as exc:  # noqa: BLE001
        record("bootstrap", False, f"MailCore unreachable at {base}: {exc}")
        return 1, steps
    if code >= 400:
        return http_fail("bootstrap", code, body)
    if not (isinstance(body, dict) and body.get("configured")):
        record("bootstrap", False, "MailCore is NOT configured -- no SMTP credential "
                                   "in the vault (AITHER_SMTP_HOST/USER/PASS) and none "
                                   "given here; re-run with --bridge-user")
        return 1, steps
    record("bootstrap", True, f"configured (provider={body.get('provider', '?')}, "
                              f"host={body.get('host', '?')})")

    # 3. doctor ---------------------------------------------------------------
    try:
        code, body = call("GET", "/smtp/doctor")
    except Exception as exc:  # noqa: BLE001
        record("doctor", False, f"MailCore unreachable at {base}: {exc}")
        return 1, steps
    if code >= 400:
        return http_fail("doctor", code, body)
    if not (isinstance(body, dict) and body.get("ok")):
        missing = (body or {}).get("missing_required") if isinstance(body, dict) else None
        why = (f"required senders cannot authenticate: {', '.join(missing)}"
               if missing else str((body or {}).get("error", body))[:160])
        record("doctor", False, why)
        return 1, steps
    opt = body.get("missing_optional") or []
    record("doctor", True, "every required sender authenticates"
           + (f" ({len(opt)} optional missing)" if opt else ""))

    # 4. test -------------------------------------------------------------------
    to = getattr(args, "to", "") or ""
    if not to:
        record("test", None, "skipped -- pass --to <address> to send a real test mail "
                             "(without a recipient it only mails itself)")
        return 0, steps
    try:
        code, body = call("POST", "/smtp/config/test", json={"to": to})
    except Exception as exc:  # noqa: BLE001
        record("test", False, f"MailCore unreachable at {base}: {exc}")
        return 1, steps
    if code >= 400:
        return http_fail("test", code, body)
    if not (isinstance(body, dict) and body.get("status") == "ok"):
        record("test", False, str((body or {}).get("message", body))[:160]
               if isinstance(body, dict) else str(body)[:160])
        return 1, steps
    record("test", True, body.get("message", f"sent to {to}"))
    return 0, steps


def cmd_mail_setup(args: Any) -> int:
    provider = (getattr(args, "provider", "") or "").lower()
    if provider not in PROVIDERS:
        print(f"  [x] unsupported provider {provider!r} (supported: {', '.join(PROVIDERS)})")
        return 2
    import httpx

    from adk._tls import tls_verify

    print(f"\n  Mail setup -- provider={provider}, MailCore={mail_url(getattr(args, 'mail_url', '') or '')}\n")
    with httpx.Client(timeout=60, verify=tls_verify()) as client:
        code, _ = run_mail_setup(args, client, interactive=sys.stdin.isatty())
    print("\n  Mail is set up." if code == 0 else "\n  Mail setup stopped at the step marked [x].")
    return code


def cmd_mail(args: Any) -> int:
    if getattr(args, "mail_command", None) == "setup":
        return cmd_mail_setup(args)
    print("  usage: adk mail setup --provider proton [--bridge-user ADDR] [--to ADDR]")
    return 2


def register_parser(sub: Any) -> None:
    """Attach `mail` to the top-level adk subparsers."""
    mail_p = sub.add_parser("mail", help="Sovereign mail: one-command Proton Bridge setup")
    mail_sub = mail_p.add_subparsers(dest="mail_command")
    sp = mail_sub.add_parser(
        "setup", help="Bridge probe/seed -> MailCore bootstrap -> doctor -> test send")
    sp.add_argument("--provider", default="proton", help="Mail provider (proton)")
    sp.add_argument("--bridge-host", default=DEFAULT_BRIDGE_HOST,
                    help="Where THIS machine reaches Proton Bridge SMTP (default 127.0.0.1)")
    sp.add_argument("--bridge-port", type=int, default=DEFAULT_BRIDGE_PORT,
                    help="Bridge SMTP port (default 1025)")
    sp.add_argument("--fleet-bridge-host", default=DEFAULT_FLEET_BRIDGE_HOST,
                    help="Where MailCore reaches the Bridge (default host.docker.internal)")
    sp.add_argument("--fleet-bridge-port", type=int, default=0,
                    help="Bridge port as MailCore sees it (default: --bridge-port)")
    sp.add_argument("--bridge-user", default="",
                    help="Bridge login address; enables the AUTH probe + MailCore seed. "
                         "Password from $AITHER_SMTP_PASS or a prompt, never a flag")
    sp.add_argument("--from", dest="from_address", default="",
                    help="Sender address to configure (must be OWNED on the Proton "
                         "account; default: --bridge-user)")
    sp.add_argument("--to", default="", help="Recipient for the final test mail")
    sp.add_argument("--mail-url", default="",
                    help=f"MailCore base URL (default $AITHER_MAIL_URL or {DEFAULT_MAIL_URL})")
