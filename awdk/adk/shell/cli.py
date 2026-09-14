"""
AitherShell CLI Entry Point
============================

Click-based CLI for AitherShell with:
- Single query mode: aither "query"
- Interactive REPL: aither
- Configuration: aither --config
- Plugins: aither --plugins
- Multiple output formats: --print, --json, --private
- Persona control: --will persona-name
- Effort levels: --effort 1-10
- Safety modes: --safety paranoid/strict/professional/relaxed
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from adk.shell.config import save_default_config, load_config, AitherConfig
from adk.shell.repl import run_repl
from adk.shell.commands import execute_command, CommandError
from adk.shell.genesis_client import GenesisClient, GenesisError
from adk.shell.crash_reporter import install_crash_reporter, set_current_command

# Install global crash reporter — catches uncaught exceptions,
# prompts user to send error report, creates GitHub issue automatically.
install_crash_reporter()

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False):
    """Setup logging."""
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # httpx is extremely chatty at INFO — suppress unless --verbose
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class _QueryFallbackGroup(click.Group):
    """`aither "question"` is the advertised front door — a first token that is
    not a known subcommand must run as a QUERY, not die with `No such command`
    (which is exactly what a plain click.Group does: it resolves the first bare
    argument as a subcommand name BEFORE the group callback's ctx.args logic
    can ever see it, so the documented single-query mode was unreachable)."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            query_cmd = self.get_command(ctx, "__query__")
            if query_cmd is None:  # defensive: fall back to the original error
                raise
            return "__query__", query_cmd, args


@click.group(cls=_QueryFallbackGroup, invoke_without_command=True, context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
@click.option("--print", "output_format", flag_value="text", help="Plain text output")
@click.option("--json", "output_format", flag_value="json", help="JSON output")
@click.option("--private", is_flag=True, help="Private mode (no logging)")
@click.option("--effort", type=click.IntRange(1, 10), help="Effort level (1-10)")
@click.option(
    "--safety",
    type=click.Choice(["paranoid", "strict", "professional", "relaxed"]),
    help="Safety level",
)
@click.option("--will", help="Persona name (e.g., aither-prime)")
@click.option("--model", help="Model override")
@click.option("--max-tokens", type=int, help="Maximum tokens in response")
@click.option("--temperature", type=click.FloatRange(0.0, 2.0), help="Sampling temperature")
@click.option("--session", help="Session ID (resume a previous session)")
@click.option("--verbose", is_flag=True, help="Verbose logging")
@click.option("--init", is_flag=True, help="Initialize shell config")
@click.option("--config", is_flag=True, help="Show configuration")
@click.option("--plugins", is_flag=True, help="List plugins")
@click.option("--status", is_flag=True, help="Check Genesis health")
@click.option("--history", type=int, help="Show history (optionally with count)")
@click.option("--completions", type=click.Choice(["bash", "zsh", "fish", "pwsh"]), help="Generate shell completions")
@click.pass_context
def cli(
    ctx,
    output_format,
    private,
    effort,
    safety,
    will,
    model,
    max_tokens,
    temperature,
    session,
    verbose,
    init,
    config,
    plugins,
    status,
    history,
    completions,
):
    """AitherShell - The AI Operating System CLI.
    
    Usage:
        aither                              # Interactive REPL
        aither "question"                   # Single query
        aither --print "query"              # Plain text output
        aither --json "query"               # JSON output
        aither --private "query"            # Private mode
        aither --will persona "query"       # Use specific persona
        aither --init                       # Initialize config
        aither --config                     # Show configuration
        aither --status                     # Check Genesis health
    """
    setup_logging(verbose)
    
    # Load config
    save_default_config()
    aither_config = load_config()
    
    # Apply CLI overrides to config
    if effort:
        aither_config.effort = effort
    if safety:
        aither_config.safety_level = safety
    if will:
        aither_config.persona = will
    if model:
        aither_config.model = model
    if max_tokens:
        aither_config.max_tokens = max_tokens
    if temperature:
        aither_config.temperature = temperature
    if session:
        aither_config.session_id = session
        aither_config.last_session_id = session
    if output_format == "json":
        aither_config.rich_output = False
        aither_config.stream = False
    if output_format == "text":
        aither_config.rich_output = False
    if private:
        aither_config.privacy_level = "private"
    
    # Handle special flags
    if completions:
        from adk.shell.completions import print_completion_script
        print_completion_script(completions)
        return
    
    if init:
        _init_shell()
        return
    
    if config:
        asyncio.run(_cmd_config(aither_config))
        return
    
    if plugins:
        asyncio.run(_cmd_plugins(aither_config))
        return
    
    if status:
        asyncio.run(_cmd_status(aither_config))
        return
    
    if history is not None:
        asyncio.run(_cmd_history(aither_config, history))
        return
    
    # If a subcommand (deploy, setup, mcp) is being invoked, skip query/REPL
    if ctx.invoked_subcommand is not None:
        ctx.ensure_object(dict)
        ctx.obj["config"] = aither_config
        return

    # Main logic — extra args are the query
    query = ctx.args
    if query:
        # Single query mode
        query_text = " ".join(query)
        asyncio.run(_cmd_query(aither_config, query_text, output_format))
    else:
        # Interactive REPL
        try:
            asyncio.run(run_repl(aither_config))
        except KeyboardInterrupt:
            print("\nGoodbye!")


@cli.command("__query__", hidden=True, context_settings={"ignore_unknown_options": True})
@click.argument("words", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def _query_fallback(ctx, words):
    """Single-query mode reached via _QueryFallbackGroup (never typed by hand)."""
    aither_config = ctx.obj["config"]
    output_format = ctx.parent.params.get("output_format")
    asyncio.run(_cmd_query(aither_config, " ".join(words), output_format))


async def _cmd_query(
    config: AitherConfig,
    query: str,
    output_format: Optional[str],
) -> None:
    """Execute a single query.

    Tries Genesis first. If Genesis is unreachable, falls back to direct
    LLM inference (Ollama, vLLM, OpenAI) for standalone mode.
    """
    # Check if user explicitly configured a direct LLM backend
    llm_backend = getattr(config, "llm_backend", None)
    if llm_backend and llm_backend not in ("genesis", "auto", ""):
        await _cmd_query_direct(config, query, output_format)
        return

    # Try Genesis first
    client = GenesisClient(base_url=config.url)
    genesis_up = await client.health_check()

    if genesis_up:
        try:
            response = ""

            async def _on_event(event_type: str, data: dict) -> None:
                if event_type == "thinking" and config.show_thinking:
                    content = data.get("content", "")
                    if content and output_format != "json":
                        print(f"\033[2m[think] {content}\033[0m", file=sys.stderr, flush=True)

            try:
                async for chunk in client.chat_stream(
                    message=query,
                    persona=config.persona,
                    effort=config.effort,
                    model=config.model,
                    max_tokens=config.max_tokens,
                    safety_level=config.safety_level,
                    private_mode=getattr(config, "privacy_level", "public") == "private",
                    on_event=_on_event,
                ):
                    response += chunk
                    if config.stream and output_format != "json":
                        print(chunk, end="", flush=True)

                if config.stream and output_format != "json":
                    print()

                if output_format == "json":
                    result = {
                        "status": "success",
                        "response": response,
                        "persona": config.persona,
                        "effort": config.effort,
                        "model": config.model,
                    }
                    print(json.dumps(result, indent=2))
                elif not config.stream:
                    print(response)
                return

            finally:
                await client.close()

        except GenesisError as e:
            if output_format == "json":
                print(json.dumps({"status": "error", "error": str(e)}, indent=2))
            else:
                print(f"[ERROR] {e.message}", file=sys.stderr)
            sys.exit(1)

    # Genesis unreachable — try direct LLM fallback
    await client.close()
    if output_format != "json":
        print("\033[2m[standalone] Genesis unreachable, trying local LLM...\033[0m", file=sys.stderr, flush=True)
    await _cmd_query_direct(config, query, output_format)


async def _cmd_query_direct(
    config: AitherConfig,
    query: str,
    output_format: Optional[str],
) -> None:
    """Execute a query via direct LLM connection (no Genesis)."""
    from adk.shell.llm_client import auto_detect_backend, DirectLLMClient

    llm_backend = getattr(config, "llm_backend", None)
    llm_url = getattr(config, "llm_url", None)

    if llm_backend and llm_backend not in ("auto", "genesis", ""):
        backend_type = "ollama" if llm_backend == "ollama" else "openai"
        url = llm_url or ("http://localhost:11434" if backend_type == "ollama" else "http://localhost:8199")
        llm = DirectLLMClient(
            base_url=url,
            model=config.model or "",
            backend=backend_type,
            api_key=config.api_key if llm_backend in ("openai",) else "",
        )
    else:
        llm = await auto_detect_backend()

    if not llm:
        msg = "No LLM backend found. Install Ollama, start vLLM, or set OPENAI_API_KEY."
        if output_format == "json":
            print(json.dumps({"status": "error", "error": msg}, indent=2))
        else:
            print(f"[ERROR] {msg}", file=sys.stderr)
        sys.exit(1)

    try:
        response = ""
        async for chunk in llm.chat_stream(
            message=query,
            model=config.model,
            max_tokens=config.max_tokens,
        ):
            response += chunk
            if config.stream and output_format != "json":
                print(chunk, end="", flush=True)

        if config.stream and output_format != "json":
            print()

        if output_format == "json":
            result = {
                "status": "success",
                "response": response,
                "model": config.model or llm.model,
                "backend": llm.backend,
            }
            print(json.dumps(result, indent=2))
        elif not config.stream:
            print(response)
    except Exception as e:
        msg = str(e)
        if output_format == "json":
            print(json.dumps({"status": "error", "error": msg}, indent=2))
        else:
            print(f"[ERROR] {msg}", file=sys.stderr)
        sys.exit(1)
    finally:
        await llm.close()


async def _cmd_config(config: AitherConfig) -> None:
    """Show configuration."""
    # execute_command RETURNS the output — dropping it makes every one of
    # these flags a silent no-op (measured: `aither --status` printed nothing
    # while the status text was fully computed and discarded).
    print(await execute_command(config, "config", ["show"]))


async def _cmd_plugins(config: AitherConfig) -> None:
    """List plugins."""
    print(await execute_command(config, "plugins", ["list"]))


async def _cmd_status(config: AitherConfig) -> None:
    """Check Genesis status."""
    print(await execute_command(config, "status"))


async def _cmd_history(config: AitherConfig, count: int) -> None:
    """Show command history."""
    print(await execute_command(config, "history", [str(count)] if count else []))


def _init_shell():
    """Initialize AitherShell config and show setup instructions."""
    from adk.shell.config import CONFIG_DIR, CONFIG_FILE, PLUGINS_DIR

    save_default_config()

    print(f"""
AitherShell initialized!

Config:   {CONFIG_FILE}
Plugins:  {PLUGINS_DIR}

LLM backends (edit {CONFIG_FILE} to change):
  genesis   AitherOS orchestrator (default — full pipeline)
  ollama    Direct Ollama (standalone, no AitherOS needed)
  vllm      Direct vLLM (standalone)
  openai    Any OpenAI-compatible API
  auto      Try genesis, fall back to local LLM

Quick start:
  aither                          # Interactive shell
  aither "hello"                  # Single query
  aither --print "question"       # Script mode
  aither --json "question"        # JSON output
  echo "prompt" | aither --print  # Pipe input

Config: edit {CONFIG_FILE}
""")


# ---------------------------------------------------------------------------
# Device flow helper (RFC 8628) — used by `aither login --browser`
# ---------------------------------------------------------------------------

class _DeviceCodeExpiredError(RuntimeError):
    """The device code is no longer valid (expired or unknown to the server)."""


def _device_flow_login(identity_url: str, client_name: str = "AitherShell") -> dict:
    """Run RFC 8628 device code flow. Returns token response dict or raises.

    The pending device code is cached to ``~/.aither/device_pending.json`` so
    that if the poll is interrupted (e.g. a dropped terminal connection in a
    web workspace), simply re-running ``aither login --browser`` RESUMES the
    same code — if it was already approved in the browser, sign-in completes
    instantly with no second authorization. Stale/expired caches fall back to a
    fresh code automatically.
    """
    import time
    import urllib.request
    import urllib.error
    import webbrowser
    from pathlib import Path

    base = identity_url.rstrip("/")
    pending_path = Path.home() / ".aither" / "device_pending.json"

    def _load_pending() -> dict | None:
        try:
            d = json.loads(pending_path.read_text())
        except Exception:
            return None
        if d.get("identity_url") == base and d.get("device_code") and d.get("deadline", 0) > time.time():
            return d
        return None

    def _save_pending(device_code: str, user_code: str, deadline: float, verification_uri: str) -> None:
        try:
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text(json.dumps({
                "identity_url": base,
                "device_code": device_code,
                "user_code": user_code,
                "deadline": deadline,
                "verification_uri": verification_uri,
            }))
        except Exception:
            pass  # Cache is best-effort

    def _clear_pending() -> None:
        try:
            pending_path.unlink()
        except Exception:
            pass

    def _poll_once(device_code: str) -> dict:
        """One poll. Returns the token dict (status/access_token) or raises
        _DeviceCodeExpiredError on a terminal error; transient errors -> pending."""
        poll_data = json.dumps({"device_code": device_code}).encode()
        poll_req = urllib.request.Request(
            f"{base}/auth/device/token",
            data=poll_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(poll_req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                try:
                    detail = json.loads(exc.read()).get("detail", "")
                except Exception:
                    detail = ""
                if detail in ("expired_token", "invalid_device_code"):
                    raise _DeviceCodeExpiredError(detail) from exc
            return {"status": "authorization_pending"}
        except (urllib.error.URLError, OSError):
            return {"status": "authorization_pending"}

    def _poll_until(device_code: str, deadline: float, interval: int) -> dict | None:
        """Poll until approved (token dict), pending-timeout (None), or raise
        _DeviceCodeExpiredError. Polls immediately so an already-approved resume
        completes without waiting a full interval."""
        while time.time() < deadline:
            result = _poll_once(device_code)
            if result.get("access_token"):
                return result
            click.echo(".", nl=False)
            time.sleep(interval)
        return None

    # ── Resume a previously-issued (still-valid) device code ───────────────
    resumed = _load_pending()
    if resumed:
        click.echo()
        click.echo(f"  Resuming previous device authorization (code: {click.style(resumed.get('user_code', ''), bold=True)})")
        if resumed.get("verification_uri"):
            click.echo(f"  If not yet approved, visit: {resumed['verification_uri']}")
        click.echo("  Waiting for approval", nl=False)
        try:
            token_result = _poll_until(resumed["device_code"], resumed["deadline"], 5)
            if token_result:
                _clear_pending()
                click.echo(" approved!")
                return token_result
        except _DeviceCodeExpiredError:
            pass  # stale cache — fall through to a fresh device code
        _clear_pending()

    # ── Step 1: Request a fresh device code ────────────────────────────────
    req_data = json.dumps({"client_name": client_name, "scopes": "full"}).encode()
    req = urllib.request.Request(
        f"{base}/auth/device/code",
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Cannot reach {identity_url}: {exc}") from exc

    user_code = data["user_code"]
    device_code = data["device_code"]
    verification_uri = data.get("verification_uri_complete") or data.get("verification_uri", "")
    interval = max(2, int(data.get("interval", 5)))
    expires_in = int(data.get("expires_in", 900))
    deadline = time.time() + expires_in
    _save_pending(device_code, user_code, deadline, verification_uri)

    # ── Step 2: Show code + open browser ───────────────────────────────────
    click.echo()
    click.echo(f"  Your code: {click.style(user_code, bold=True)}")
    click.echo()
    click.echo(f"  Opening browser to: {verification_uri}")
    click.echo("  (If it doesn't open, visit the URL manually and enter the code)")
    click.echo()

    try:
        webbrowser.open(verification_uri)
    except Exception:
        pass  # Browser open is best-effort

    click.echo("  Waiting for approval", nl=False)

    # ── Step 3: Poll for token ─────────────────────────────────────────────
    try:
        token_result = _poll_until(device_code, deadline, interval)
    except _DeviceCodeExpiredError as exc:
        _clear_pending()
        click.echo()
        if str(exc) == "invalid_device_code":
            raise RuntimeError("Invalid device code.") from exc
        raise RuntimeError("Device code expired. Run `aither login --browser` again.") from exc

    if token_result:
        _clear_pending()
        click.echo(" approved!")
        return token_result

    _clear_pending()
    click.echo()
    raise RuntimeError("Timed out waiting for approval.")


@cli.command()
@click.option("--portal-url", default="https://api.aitherium.com",
              envvar="AITHER_PORTAL_URL", help="Portal base URL")
@click.option("--browser", is_flag=True, help="Use device flow — open browser to approve (like gh auth login)")
@click.option("--email", help="Account email (prompted if omitted)")
@click.option("--password", help="Account password (prompted if omitted)")
@click.option("--tenant", "tenant", default=None,
              help="Tenant slug to scope this session to")
@click.option("--workspace", "workspace", default=None,
              help="Workspace slug to scope this session to")
@click.option("--token", "explicit_token", default=None,
              help="Use an existing portal token instead of logging in")
@click.option("--print-env", is_flag=True,
              help="Print shell export statements for AITHER_* scope vars")
@click.option("--json", "output_json", is_flag=True, help="Output result as JSON")
def login(portal_url, browser, email, password, tenant, workspace, explicit_token,
          print_env, output_json):
    """Authenticate with the AitherOS portal and persist a session token.

    Writes the bearer token to ``~/.aither/portal.token`` (chmod 600) and
    optional scope hints to ``~/.aither/scope.env`` so the ADK CLI
    (``aither_adk``), Genesis, and downstream tools can federate agents up
    to ``api.aitherium.com`` for fleet discovery.

    \b
    aither login                                  # Interactive (email/password)
    aither login --browser                        # Device flow (opens browser)
    aither login --email me@x.com --password ...
    aither login --token aith_pk_...              # Use existing token
    aither login --tenant acme --workspace ws1    # Scope-bound login
    eval "$(aither login --print-env)"            # Load scope into shell
    """
    import os as _os
    import stat as _stat
    from pathlib import Path

    aither_dir = Path.home() / ".aither"
    aither_dir.mkdir(parents=True, exist_ok=True)
    token_path = aither_dir / "portal.token"
    scope_path = aither_dir / "scope.env"

    result: dict = {
        "portal_url": portal_url,
        "tenant": tenant,
        "workspace": workspace,
        "token_path": str(token_path),
        "scope_path": str(scope_path),
    }

    token = explicit_token

    # ── Device flow (--browser) ──────────────────────────────────
    if not token and browser:
        try:
            token_result = _device_flow_login(portal_url)
            token = token_result.get("access_token", "")
            user = token_result.get("user", {})
            if isinstance(user, dict):
                if not tenant:
                    tenant = user.get("tenant_id") or user.get("tenant_slug")
                if not workspace:
                    workspace = user.get("workspace_id") or user.get("workspace_slug")
                result["user"] = user.get("username") or user.get("email")
        except RuntimeError as exc:
            msg = f"Device flow failed: {exc}"
            if output_json:
                print(json.dumps({"ok": False, "error": str(exc)}))
            else:
                print(msg, file=sys.stderr)
            sys.exit(1)

    # ── Email/password flow (default) ────────────────────────────
    if not token:
        if not email:
            email = click.prompt("Email")
        if not password:
            password = click.prompt("Password", hide_input=True)
        try:
            from adk.shell.auth import login_password, AuthError
            resp = asyncio.run(login_password(portal_url, email, password))
            if resp.get("requires_2fa"):
                code = click.prompt("2FA code")
                from adk.shell.auth import verify_2fa
                resp = asyncio.run(verify_2fa(
                    portal_url, resp.get("temp_token", ""), code,
                ))
            token = resp.get("access_token") or resp.get("token")
            user = resp.get("user") or {}
            if not tenant:
                tenant = user.get("tenant_id") or user.get("tenant_slug")
            if not workspace:
                workspace = user.get("workspace_id") or user.get("workspace_slug")
            result["user"] = user.get("username") or user.get("email")
        except Exception as e:  # AuthError, network errors
            msg = f"Login failed: {e}"
            if output_json:
                print(json.dumps({"ok": False, "error": str(e)}))
            else:
                print(msg, file=sys.stderr)
            sys.exit(1)

    if not token:
        msg = "No token returned from portal"
        if output_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    # Validate token by calling /auth/me. This catches expired / revoked /
    # wrong-portal tokens up-front instead of silently writing junk to disk.
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=10.0) as _c:
            me_resp = _c.get(
                f"{portal_url.rstrip('/')}/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
        if me_resp.status_code == 200:
            try:
                me = me_resp.json()
                result["user"] = result.get("user") or me.get("username") or me.get("email")
                if not tenant:
                    tenant = me.get("tenant_id") or me.get("tenant_slug")
                if not workspace:
                    workspace = me.get("workspace_id") or me.get("workspace_slug")
                result["token_validated"] = True
            except Exception:
                result["token_validated"] = True  # 200 but non-json — accept
        elif me_resp.status_code in (401, 403):
            msg = f"Token rejected by portal ({me_resp.status_code}). Not saved."
            if output_json:
                print(json.dumps({"ok": False, "error": msg}))
            else:
                print(msg, file=sys.stderr)
            sys.exit(1)
        else:
            # /auth/me unavailable (404, 5xx) — don't block, but warn.
            result["token_validated"] = False
            result["validation_warning"] = (
                f"/auth/me returned {me_resp.status_code} — token saved unverified."
            )
            if not output_json:
                print(result["validation_warning"], file=sys.stderr)
    except Exception as e:
        result["token_validated"] = False
        result["validation_warning"] = f"Could not reach /auth/me: {e}"
        if not output_json:
            print(result["validation_warning"], file=sys.stderr)

    token_path.write_text(token + "\n", encoding="utf-8")
    try:
        _os.chmod(token_path, _stat.S_IRUSR | _stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass  # Windows w/o ACL support — best effort
    result["token_saved"] = True

    # Also update auth.json so mcp_setup.py / resolve_auth() can find the token
    auth_json_path = aither_dir / "auth.json"
    try:
        if auth_json_path.is_file():
            auth_data = json.loads(auth_json_path.read_text(encoding="utf-8"))
        else:
            auth_data = {"version": 1, "active_profile": "local", "profiles": {}}
        auth_data["profiles"]["local"] = {
            "endpoint": portal_url,
            "genesis_url": portal_url.replace("api.aitherium.com", "localhost:8001"),
            "token_type": "portal",
            "access_token": token,
            "expires_at": "",
            "user": {
                "id": result.get("user", ""),
                "username": result.get("user", ""),
                "display_name": result.get("user", ""),
                "email": "",
                "roles": ["admin"] if "admin" in str(result) else ["user"],
                "tenant_id": tenant or "",
                "tenant_slug": tenant or "",
            }
        }
        auth_json_path.write_text(json.dumps(auth_data, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass  # Best effort — portal.token is the primary store

    # Surface token expiry locally. The portal is the source of truth, but a
    # 24h re-login cycle is much friendlier than a silent 401 three days later.
    try:
        import base64 as _b64
        import time as _time
        if token.count(".") == 2:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            claims = json.loads(_b64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            exp = int(claims.get("exp") or 0)
            if exp:
                now = int(_time.time())
                remaining = exp - now
                result["token_exp"] = exp
                result["token_expires_in_seconds"] = max(0, remaining)
                if remaining <= 0:
                    msg = (
                        f"Warning: token already expired (exp={exp}); "
                        "the portal accepted it but it will fail soon."
                    )
                    result["token_expired"] = True
                    if not output_json:
                        print(msg, file=sys.stderr)
                elif remaining < 3600:
                    msg = (
                        f"Warning: token expires in ~{remaining // 60}m; "
                        "consider re-running `aither login` soon."
                    )
                    if not output_json:
                        print(msg, file=sys.stderr)
    except Exception:
        pass  # JWT parsing is best-effort cosmetics

    # Write scope env (sourced by downstream tools)
    env_lines = [f"AITHER_PORTAL_URL={portal_url}"]
    if tenant:
        env_lines.append(f"AITHER_TENANT_ID={tenant}")
    if workspace:
        env_lines.append(f"AITHER_WORKSPACE_ID={workspace}")
    scope_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    result["scope_saved"] = True

    if print_env:
        for line in env_lines:
            print(f"export {line}")
        print(f"export AITHER_PORTAL_TOKEN=$(cat {token_path})")
        return

    if output_json:
        result["ok"] = True
        print(json.dumps(result, indent=2))
        return

    print(f"Logged in to {portal_url}")
    if result.get("user"):
        print(f"  user:      {result['user']}")
    if tenant:
        print(f"  tenant:    {tenant}")
    if workspace:
        print(f"  workspace: {workspace}")
    print(f"  token:     {token_path} (chmod 600)")
    print(f"  scope:     {scope_path}")
    if result.get("token_expires_in_seconds"):
        secs = result["token_expires_in_seconds"]
        print(f"  expires:   in ~{secs // 3600}h{(secs % 3600) // 60}m")
    # OS-aware activation hint so the operator knows how to load scope.env.
    if _os.name == "nt":
        print("To load scope into PowerShell:")
        print(f"  Get-Content {scope_path} | ForEach-Object {{ "
              "$k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k,$v) }}")
        print(f"  $env:AITHER_PORTAL_TOKEN = (Get-Content {token_path})")
    else:
        print("To load scope into your shell:")
        print(f"  set -a; . {scope_path}; set +a")
        print(f"  export AITHER_PORTAL_TOKEN=$(cat {token_path})")
    print("Or run `aither login --print-env` and `eval` the output.")


@cli.command()
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def logout(output_json):
    """Remove the cached portal token and scope file."""
    from pathlib import Path
    aither_dir = Path.home() / ".aither"
    removed = []
    for name in ("portal.token", "scope.env"):
        p = aither_dir / name
        if p.exists():
            try:
                p.unlink()
                removed.append(str(p))
            except OSError as e:
                if output_json:
                    print(json.dumps({"ok": False, "error": str(e)}))
                else:
                    print(f"Failed to remove {p}: {e}", file=sys.stderr)
                sys.exit(1)
    if output_json:
        print(json.dumps({"ok": True, "removed": removed}))
    else:
        if removed:
            print(f"Removed: {', '.join(removed)}")
        else:
            print("No portal session to remove.")


@cli.command()
@click.option("--refresh", is_flag=True, help="Force a remote refresh against the portal")
@click.option("--strict", is_flag=True, help="Exit non-zero if not on a paid plan or quota is exhausted")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def entitlement(refresh, strict, output_json):
    """Show current plan, quota, feature flags, and cache health.

    \b
    aither entitlement              # quick local read
    aither entitlement --refresh    # force a portal round-trip
    aither entitlement --strict     # exit 2 if free / lapsed / out of quota
    """
    from adk.shell.entitlement import (
        load_cached, refresh as _refresh, summary as _summary,
    )
    if refresh:
        ent = asyncio.run(_refresh(force=True))
    else:
        ent = load_cached() or asyncio.run(_refresh())

    data = asyncio.run(_summary())
    if output_json:
        print(json.dumps(data, indent=2))
    else:
        print(f"Plan:             {data['plan']}")
        print(f"Status:           {data['status']}")
        print(f"Tokens remaining: {data['tokens_remaining']} / {data['tokens_total']}")
        print(f"Expires:          {data['expires_at'] or '(never)'}")
        print(f"Last validated:   {data['validated_at']}  (source={data['source']})")
        print(f"Device:           {data['device_id']}")
        print("Features:")
        for k, v in sorted(data["features"].items()):
            print(f"  - {k:30s} {'YES' if v else 'no'}")

    if strict:
        if data["status"] in ("free", "revoked", "lapsed") or data["tokens_remaining"] <= 0:
            sys.exit(2)


@cli.command()
@click.option("--email", help="Account email")
@click.option("--password", help="Account password")
@click.option("--auto", "auto_mode", is_flag=True, help="Non-interactive mode (for agent automation)")
@click.option("--skip-gpu", is_flag=True, help="Skip GPU detection")
@click.option("--skip-deploy", is_flag=True, help="Skip model deployment")
@click.option("--skip-mcp", is_flag=True, help="Skip Claude Code MCP config")
@click.option("--skip-llm", is_flag=True, help="Skip the optional local-LLM install step")
@click.option("--skip-framework", is_flag=True, help="Skip the optional Hermes/OpenClaw step")
@click.option("--install-llm", is_flag=True, help="In --auto mode: install local LLM (vLLM/Ollama)")
@click.option("--llm-backend", type=click.Choice(["vllm", "ollama"]), default="vllm",
              show_default=True, help="Local LLM backend when --install-llm")
@click.option("--llm-model", default=None, help="Model id (e.g. meta-llama/Meta-Llama-3.1-8B-Instruct or llama3.1:8b)")
@click.option("--llm-port", default=8080, show_default=True, type=int, help="Local LLM port")
@click.option("--install-framework", type=click.Choice(["hermes", "openclaw"]), default=None,
              help="In --auto mode: clone & install an agent framework")
@click.option("--framework-portal-inference", is_flag=True,
              help="Wire the installed framework to portal inference instead of the local LLM")
@click.option("--framework-model", default=None, help="Override model name for the framework overlay")
@click.option("--framework-agent-name", default=None, help="Portal agent name for the framework onboard step")
@click.option("--plan", "plan_only", is_flag=True,
              help="Print the executable step plan as JSON without running anything (for agents)")
@click.option("--json", "output_json", is_flag=True, help="Output result as JSON (for agents)")
def setup(email, password, auto_mode, skip_gpu, skip_deploy, skip_mcp, skip_llm, skip_framework,
          install_llm, llm_backend, llm_model, llm_port, install_framework,
          framework_portal_inference, framework_model, framework_agent_name,
          plan_only, output_json):
    """Full self-service onboarding wizard.

    Walks a human (or an autonomous agent) through portal registration,
    hardware detection, AitherOS connection, optional local-LLM install
    (vLLM/Ollama), optional Hermes/OpenClaw framework deploy, and Claude
    Code MCP config.

    \b
    Interactive:    aither setup
    Plan only:      aither setup --plan --json
    Agent (full):   aither setup --auto --json --email $EMAIL --password $PASS \\
                                  --install-llm --llm-backend ollama --llm-model llama3.1:8b \\
                                  --install-framework hermes
    Minimal auto:   aither setup --auto --email $EMAIL --password $PASS --json
    """
    from adk.shell.onboarding import run_onboarding, plan_onboarding

    if plan_only:
        plan = plan_onboarding(
            install_llm=install_llm,
            llm_backend=llm_backend,
            llm_model=llm_model,
            llm_port=llm_port,
            install_framework=install_framework,
            framework_local_llm=(None if not install_framework
                                 else (not framework_portal_inference)),
            framework_model=framework_model,
            framework_agent_name=framework_agent_name,
            skip_gpu=skip_gpu,
            skip_deploy=skip_deploy,
            skip_mcp=skip_mcp,
        )
        print(json.dumps(plan, indent=2))
        return

    # Entitlement preflight for paid features (framework install / large model).
    if auto_mode:
        _ent_require = None
        _EntErr: type = Exception
        try:
            from adk.shell.entitlement import (
                require as _ent_require,  # type: ignore
                EntitlementError as _EntErr,  # type: ignore
            )
        except ImportError:
            pass
        gates: list[str] = []
        if install_framework == "hermes":
            gates.append("framework_hermes")
        elif install_framework == "openclaw":
            gates.append("framework_openclaw")
        # Large-model gate (>=13B parameters by name heuristic)
        big_hints = ("13b", "14b", "30b", "33b", "34b", "65b", "70b", "72b")
        if install_llm and llm_model and any(h in str(llm_model).lower() for h in big_hints):
            gates.append("model_large")
        if _ent_require is not None:
            for feature in gates:
                try:
                    asyncio.run(_ent_require(feature))
                except _EntErr as e:  # type: ignore[misc]
                    code = getattr(e, "code", "refused")
                    hint = getattr(e, "hint", "")
                    payload = {
                        "status": "refused", "feature": feature,
                        "code": code, "message": str(e), "hint": hint,
                    }
                    if output_json:
                        print(json.dumps(payload, indent=2))
                    else:
                        print(f"\n[entitlement] {e}", file=sys.stderr)
                        if hint:
                            print(f"              -> {hint}", file=sys.stderr)
                    sys.exit(2)

    framework_local_llm: Optional[bool] = None
    if install_framework is not None:
        framework_local_llm = not framework_portal_inference

    result = asyncio.run(run_onboarding(
        mode="auto" if auto_mode else "interactive",
        email=email,
        password=password,
        skip_gpu=skip_gpu,
        skip_deploy=skip_deploy,
        skip_mcp=skip_mcp,
        skip_llm=skip_llm,
        skip_framework=skip_framework,
        install_llm=install_llm if auto_mode else None,
        llm_backend=llm_backend,
        llm_model=llm_model,
        llm_port=llm_port,
        install_framework=install_framework if auto_mode else None,
        framework_local_llm=framework_local_llm,
        framework_model=framework_model,
        framework_agent_name=framework_agent_name,
    ))
    if output_json:
        print(json.dumps(result.to_dict(), indent=2))
    elif result.status != "success":
        print(f"\nSetup failed: {result.errors}", file=sys.stderr)
        sys.exit(1)


@cli.command()
@click.argument("subcommand", required=False, default=None)
@click.option("--profile", "-p", default="chat-minimal", help="Deployment profile")
@click.option("--version", "-v", "ver", default="latest", help="Image version tag")
@click.option("--gpu", default="auto", type=click.Choice(["auto", "none"]), help="GPU mode")
@click.option("--data-dir", type=click.Path(), help="Data directory for volumes")
@click.option("--offline", type=click.Path(exists=True), help="Import offline bundle")
@click.option("--dry-run", is_flag=True, help="Preview without pulling")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
@click.option("--register-fleet", is_flag=True, help="Register with fleet after deploy ($5/mo)")
def deploy(subcommand, profile, ver, gpu, data_dir, offline, dry_run, output_json, register_fleet):
    """Deploy AitherOS locally via Docker.

    \b
    aither deploy                                  # Interactive deploy (chat-minimal)
    aither deploy --profile chat-full              # Full personality stack
    aither deploy --profile chat-full --dry-run    # Preview what would be pulled
    aither deploy status                           # Show deployment health
    aither deploy update                           # Pull newer images
    aither deploy stop                             # Stop all services
    aither deploy profiles                         # List available profiles
    aither deploy export                           # Create offline bundle
    """
    from adk.shell.deployer import Deployer, DeployState
    from pathlib import Path as P

    deployer = Deployer(
        version=ver,
        gpu_mode=gpu,
        data_dir=P(data_dir) if data_dir else None,
    )

    if subcommand == "status":
        state = DeployState.load()
        if not state:
            print("No active deployment. Run `aither deploy` to get started.")
            return
        print(f"Profile:  {state.profile}")
        print(f"Version:  {state.version}")
        print(f"Deployed: {state.deployed_at}")
        print(f"Compose:  {state.compose_path}")
        return

    if subcommand == "stop":
        ok = asyncio.run(deployer.stop())
        print("Stopped." if ok else "No active deployment to stop.")
        return

    if subcommand == "update":
        result = asyncio.run(deployer.update())
        if output_json:
            print(json.dumps(result, indent=2))
        else:
            print(f"Update: {result.get('status')} ({result.get('images_updated', 0)} images)")
        return

    if subcommand == "profiles":
        for p in deployer.list_profiles():
            gpu_info = f"GPU: {p.min_vram_gb}GB" if p.gpu_required else "No GPU"
            print(f"  {p.name:<18} {p.description}")
            print(f"                     RAM: {p.min_ram_gb}GB | Disk: {p.min_disk_gb}GB | {gpu_info} | ~{p.containers_approx} containers")
            print()
        return

    if subcommand == "export":
        output = P.home() / f"aitheros-{profile}-bundle"
        ok = asyncio.run(deployer.export_bundle(profile, output))
        print(f"Bundle: {output}.tar.gz" if ok else "Export failed.")
        return

    # Main deploy flow
    def progress(phase, detail=""):
        if phase not in ("pulling",) and not output_json:
            print(f"  [{phase}] {detail}")

    if offline:
        ok = asyncio.run(deployer.import_bundle(P(offline), progress_callback=progress))
        if not ok:
            print("Import failed.", file=sys.stderr)
            sys.exit(1)
        print("\nOffline import complete. Services starting...")
        return

    result = asyncio.run(deployer.deploy(
        profile_name=profile,
        dry_run=dry_run,
        progress_callback=progress,
    ))

    if output_json:
        print(json.dumps(result, indent=2))
        return

    if result["status"] == "failed":
        for err in result.get("errors", []):
            print(f"  ERROR: {err}", file=sys.stderr)
        sys.exit(1)
    elif result["status"] == "dry_run":
        print(f"\nDry run — would pull {result.get('image_count', 0)} images:")
        for img in result.get("images", []):
            print(f"  {img}")
    else:
        health = result.get("health", {})
        print(f"\nDeployment {result['status']}!")
        for svc, ok in health.items():
            print(f"  {svc}: {'healthy' if ok else 'unhealthy'}")
        print(f"\n  Genesis:   http://localhost:8001")
        print(f"  Dashboard: http://localhost:3000")

        if register_fleet:
            try:
                import httpx
                from adk.shell.auth import AuthStore

                token = None
                try:
                    token = AuthStore.get_active_token() if AuthStore else None
                except Exception:
                    pass

                portal_url = os.environ.get("AITHER_PORTAL_URL", "https://api.aitherium.com")
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                with httpx.Client(timeout=15) as client:
                    resp = client.post(
                        f"{portal_url}/api/fleet/endpoints/register",
                        json={
                            "name": profile or "aither-agent",
                            "url": "http://localhost:8080",
                            "agent_type": "adk-deploy",
                            "capabilities": ["chat", "tools"],
                        },
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        print(f"\n  Fleet registered: {data.get('endpoint_id')}")
                        print(f"  API Key: {data.get('api_key')} (save this)")
                        print("  Billing: $5/mo")
                    else:
                        print(f"\n  Fleet registration failed: {resp.status_code}")
            except Exception as e:
                print(f"\n  Fleet registration failed (non-fatal): {e}")


# ─── aither download (download purchased pack bundles) ──────────────────────

@cli.command()
@click.argument("pack_id")
@click.option("--output-dir", "-o", default=".", type=click.Path(), help="Output directory")
@click.option("--extract", "-x", is_flag=True, help="Auto-extract after download")
@click.option("--portal-url", default=None, help="Portal URL override")
def download(pack_id, output_dir, extract, portal_url):
    """Download a purchased pack bundle.

    \b
    aither download aither-knowledge             # Download to current dir
    aither download aither-knowledge -o ./agent  # Download to specific dir
    aither download aither-knowledge -x          # Download and extract
    """
    import httpx
    from pathlib import Path as P

    base = portal_url or os.environ.get("AITHER_PORTAL_URL", "https://api.aitherium.com")
    headers = {"Content-Type": "application/json"}

    # Try to get auth token if available
    try:
        from adk.shell.auth import AuthStore
        token = AuthStore.get_active_token() if AuthStore else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass

    # First get pack info
    print(f"Fetching pack info: {pack_id}")
    try:
        with httpx.Client(timeout=30) as client:
            info_resp = client.get(f"{base}/api/marketplace/packs/{pack_id}", headers=headers)
            if info_resp.status_code == 404:
                print(f"Pack '{pack_id}' not found.")
                raise SystemExit(1)
            pack_info = info_resp.json() if info_resp.status_code == 200 else {}
            pack_name = pack_info.get("name", pack_id)

            # Download bundle
            print(f"Downloading {pack_name}...")
            dl_resp = client.get(
                f"{base}/api/agent-builder/build/{pack_id}/download",
                headers=headers,
                follow_redirects=True,
            )
            if dl_resp.status_code != 200:
                print(f"Download failed: {dl_resp.status_code}")
                raise SystemExit(1)

            out_path = P(output_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            filename = f"{pack_id}.tar.gz"
            filepath = out_path / filename
            filepath.write_bytes(dl_resp.content)
            print(f"Saved: {filepath} ({len(dl_resp.content)} bytes)")

            if extract:
                import tarfile
                with tarfile.open(filepath, "r:gz") as tar:
                    extract_dir = out_path / pack_id
                    tar.extractall(path=str(extract_dir))
                    print(f"Extracted to: {extract_dir}")
                    # Show first 10 files in bundle
                    for member in tar.getnames()[:10]:
                        print(f"  {member}")
                    if len(tar.getnames()) > 10:
                        print(f"  ... and {len(tar.getnames()) - 10} more files")

            print(f"\nNext steps:")
            print(f"  cd {pack_id if extract else output_dir}")
            print(f"  docker compose up -d")
            print(f"  # Or: aither run")
    except httpx.ConnectError:
        print(f"Could not connect to {base}")
        raise SystemExit(1)


# ─── aither rebuild (build + restart with base image auto-detection) ────────

# Base image dependency chain:
#   1. aitheros-python-freethreaded:3.14  (Dockerfile.freethreaded)
#   2. aitheros-base:latest               (Dockerfile.unified-base --target base)
#   3. Service images                     (docker-compose build <service>)
#
# Genesis uses freethreaded directly. Most other services use aitheros-base.

_BASE_IMAGES = {
    "aitheros-python-freethreaded:3.14": {
        "dockerfile": "docker/services/Dockerfile.freethreaded",
        "context": "docker/services/",
        "description": "Free-threaded CPython 3.14 (no-GIL)",
    },
    "aitheros-base:latest": {
        "dockerfile": "docker/base/Dockerfile.unified-base",
        "context": ".",
        "target": "base",
        "depends": "aitheros-python-freethreaded:3.14",
        "description": "AitherOS service base (~300MB)",
    },
}

# Which base image each service needs
_SERVICE_BASE = {
    "aither-genesis": "aitheros-python-freethreaded:3.14",
    "aither-veil": None,  # Node.js, no Python base needed
}
# Default: most services need aitheros-base:latest


def _image_exists(image_tag: str) -> bool:
    """Check if a Docker image exists locally."""
    result = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        capture_output=True, timeout=10
    )
    return result.returncode == 0


def _build_base_image(image_tag: str, repo_root: str) -> bool:
    """Build a base image if missing. Returns True if built or already exists."""
    if _image_exists(image_tag):
        click.echo(f"  [OK] {image_tag} exists")
        return True

    meta = _BASE_IMAGES.get(image_tag)
    if not meta:
        click.echo(f"  [SKIP] {image_tag} — no build recipe known", err=True)
        return False

    # Build dependency first
    dep = meta.get("depends")
    if dep and not _image_exists(dep):
        click.echo(f"  [BUILD] {dep} (dependency of {image_tag})")
        if not _build_base_image(dep, repo_root):
            return False

    click.echo(f"  [BUILD] {image_tag} — {meta['description']}")
    cmd = [
        "docker", "build",
        "-f", os.path.join(repo_root, meta["dockerfile"]),
        "-t", image_tag,
    ]
    if "target" in meta:
        cmd.extend(["--target", meta["target"]])
    cmd.append(os.path.join(repo_root, meta["context"]))

    result = subprocess.run(cmd, timeout=1200)
    if result.returncode != 0:
        click.echo(f"  [FAIL] {image_tag} build failed", err=True)
        return False
    click.echo(f"  [OK] {image_tag} built")
    return True


def _find_repo_root() -> str:
    """Find the AitherOS repo root (canonical marker: .DEPLOYMENT/compose/docker-compose.aitheros.yml)."""
    # Canonical marker first, then the legacy repo-root path as a fallback.
    markers = [
        os.path.join(".DEPLOYMENT", "compose", "docker-compose.aitheros.yml"),
        "docker-compose.aitheros.yml",
    ]

    def _is_root(path: str) -> bool:
        return any(os.path.isfile(os.path.join(path, m)) for m in markers)

    # Check common locations. Drive roots are DISCOVERED rather than hardcoded —
    # this list used to end with a literal absolute drive path, one developer's
    # drive layout shipped to every pip install.
    from adk.shell._repo_roots import candidate_repo_roots

    candidates = [str(p) for p in candidate_repo_roots()]
    for c in candidates:
        if _is_root(c):
            return c
    # Try git root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            root = result.stdout.strip()
            if _is_root(root):
                return root
    except Exception:
        pass
    return os.getcwd()


def _wait_for_health(service: str, port: int, timeout_sec: int = 120) -> bool:
    """Poll health endpoint until healthy or timeout."""
    import time
    import urllib.request
    import urllib.error

    url = f"http://localhost:{port}/health"
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            req = urllib.request.urlopen(url, timeout=3)
            if req.status == 200:
                return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(3)
    return False


# Service → health port mapping (subset — extend as needed)
_SERVICE_PORTS = {
    "aither-genesis": 8001,
    "aither-veil": 3000,
    "aither-pulse": 8081,
    "aither-chronicle": 8121,
    "aither-node": 8080,
    "aither-microscheduler": 8150,
    "aither-directory": 8214,
    "aither-security-core": 8115,
}


@cli.command()
@click.argument("services", nargs=-1, required=False)
@click.option("--all", "rebuild_all", is_flag=True, help="Rebuild all running services")
@click.option("--no-cache", is_flag=True, help="Build without Docker cache")
@click.option("--skip-health", is_flag=True, help="Skip health check after restart")
@click.option("--compose", default=None, help="Compose file path (auto-detected)")
def rebuild(services, rebuild_all, no_cache, skip_health, compose):
    """Rebuild and restart services with automatic base image detection.

    Checks for missing base images, builds them if needed, then rebuilds
    and restarts the target service(s). Waits for health checks.

    \b
    aither rebuild genesis          # Rebuild + restart Genesis
    aither rebuild veil             # Rebuild + restart Veil
    aither rebuild genesis veil     # Rebuild multiple services
    aither rebuild --all            # Rebuild all running services
    """
    import time

    repo_root = _find_repo_root()
    compose_file = compose or os.path.join(
        repo_root, ".DEPLOYMENT", "compose", "docker-compose.aitheros.yml"
    )

    if not os.path.isfile(compose_file):
        click.echo(f"Compose file not found: {compose_file}", err=True)
        sys.exit(1)

    # Resolve service names (add 'aither-' prefix if missing)
    if rebuild_all:
        # Get all running services
        result = subprocess.run(
            ["docker", "compose", "-f", compose_file, "--project-directory", repo_root, "ps", "--format", "{{.Service}}"],
            capture_output=True, text=True, timeout=30
        )
        targets = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
        if not targets:
            click.echo("No running services found.", err=True)
            sys.exit(1)
    elif services:
        targets = []
        for s in services:
            name = s if s.startswith("aither-") else f"aither-{s}"
            targets.append(name)
    else:
        click.echo("Usage: aither rebuild <service> [service...] or --all", err=True)
        sys.exit(1)

    click.echo(f"Rebuilding {len(targets)} service(s) in {repo_root}")

    # Step 1: Check and build base images
    click.echo("\n--- Step 1: Base images ---")
    needed_bases = set()
    for svc in targets:
        base = _SERVICE_BASE.get(svc)
        if base is None and svc != "aither-veil":
            base = "aitheros-base:latest"
        if base:
            needed_bases.add(base)
            # Also add transitive dependencies
            meta = _BASE_IMAGES.get(base, {})
            dep = meta.get("depends")
            if dep:
                needed_bases.add(dep)

    for base in sorted(needed_bases):
        if not _build_base_image(base, repo_root):
            click.echo(f"\nFailed to build base image {base}. Aborting.", err=True)
            sys.exit(1)

    # Step 2: Build service images
    click.echo("\n--- Step 2: Build services ---")
    build_cmd = ["docker", "compose", "-f", compose_file, "--project-directory", repo_root, "build"]
    if no_cache:
        build_cmd.append("--no-cache")
    build_cmd.extend(targets)

    click.echo(f"  Running: {' '.join(build_cmd)}")
    result = subprocess.run(build_cmd, cwd=repo_root, timeout=1200)
    if result.returncode != 0:
        click.echo("Build failed.", err=True)
        sys.exit(1)

    # Step 3: Restart services
    click.echo("\n--- Step 3: Restart services ---")
    up_cmd = ["docker", "compose", "-f", compose_file, "--project-directory", repo_root, "up", "-d"]
    up_cmd.extend(targets)
    result = subprocess.run(up_cmd, cwd=repo_root, timeout=300)
    if result.returncode != 0:
        click.echo("Restart failed.", err=True)
        sys.exit(1)

    # Step 4: Health checks
    if not skip_health:
        click.echo("\n--- Step 4: Health checks ---")
        all_healthy = True
        for svc in targets:
            port = _SERVICE_PORTS.get(svc)
            if not port:
                click.echo(f"  [{svc}] No health port known — skipping")
                continue
            click.echo(f"  [{svc}] Waiting for health on :{port}...", nl=False)
            start = time.time()
            if _wait_for_health(svc, port):
                elapsed = time.time() - start
                click.echo(f" healthy ({elapsed:.0f}s)")
            else:
                click.echo(f" TIMEOUT (120s)")
                all_healthy = False

        if not all_healthy:
            click.echo("\nSome services failed health checks.", err=True)
            sys.exit(1)

    click.echo(f"\nAll {len(targets)} service(s) rebuilt and healthy.")


@cli.command()
@click.argument("target", required=False)
@click.option("--tier", default="alpha", type=click.Choice(["alpha", "team", "community"]), help="Invite tier")
@click.option("--note", default="", help="Note about this invite")
@click.option("--no-email", is_flag=True, help="Don't send invite email")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def invite(target, tier, note, no_email, output_json):
    """Manage platform invites (admin only).

    \b
    aither invite jeff@example.com                — Send alpha invite
    aither invite jeff@example.com --tier team    — Team-tier invite
    aither invite jeff@example.com --note "CTF"   — With note
    aither invite list                            — Show active invites
    aither invite revoke inv_abc123               — Revoke an invite
    """
    asyncio.run(_cmd_invite(target, tier, note, no_email, output_json))


async def _cmd_invite(target, tier, note, no_email, output_json):
    """Execute invite commands."""
    try:
        from adk.shell.auth import AuthStore
    except ImportError:
        print("Auth module not available. Run `aither setup` first.", file=sys.stderr)
        sys.exit(1)

    user = AuthStore.get_active_user()
    if not user:
        print("Not logged in. Run `aither setup` to authenticate.", file=sys.stderr)
        sys.exit(1)
    if "admin" not in user.get("roles", []):
        print("Insufficient permissions (requires admin role).", file=sys.stderr)
        sys.exit(1)

    import httpx
    identity_url = os.environ.get(
        "AITHER_IDENTITY_URL",
        os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001"),
    )
    headers = {"Content-Type": "application/json"}
    token = AuthStore.get_active_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    if not target or target == "help":
        print("Usage: aither invite <email|list|revoke> [options]")
        return

    if target == "list":
        async with httpx.AsyncClient(base_url=identity_url, headers=headers, timeout=15) as c:
            resp = await c.get("/admin/invites")
        if resp.status_code != 200:
            print(f"Failed: {resp.status_code}", file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        if output_json:
            print(json.dumps(data, indent=2))
        else:
            invites = data.get("invites", [])
            if not invites:
                print("No active invites.")
                return
            for inv in invites:
                status = "revoked" if inv.get("revoked") else (
                    "consumed" if not inv.get("consumable") else "active"
                )
                print(f"  {inv['id']}  {inv.get('email', '—'):<30}  "
                      f"[{inv.get('tier', '?')}]  {status}  "
                      f"expires {inv.get('expires_at', '?')[:10]}")
        return

    if target == "revoke":
        print("Usage: aither invite revoke <invite_id>")
        print("(Pass the invite ID as the target argument)")
        return

    if target.startswith("inv_"):
        # Looks like a revoke attempt
        async with httpx.AsyncClient(base_url=identity_url, headers=headers, timeout=15) as c:
            resp = await c.delete(f"/admin/invites/{target}")
        if resp.status_code == 404:
            print(f"Invite {target} not found.", file=sys.stderr)
            sys.exit(1)
        if resp.status_code != 200:
            print(f"Failed: {resp.status_code}", file=sys.stderr)
            sys.exit(1)
        print(f"Invite {target} revoked.")
        return

    # Default: send invite to email
    body = {
        "email": target,
        "tier": tier,
        "note": note,
        "send_email": not no_email,
    }
    async with httpx.AsyncClient(base_url=identity_url, headers=headers, timeout=15) as c:
        resp = await c.post("/admin/invites", json=body)
    if resp.status_code != 200:
        print(f"Failed: {resp.status_code} — {resp.text[:300]}", file=sys.stderr)
        sys.exit(1)
    data = resp.json()
    if output_json:
        print(json.dumps(data, indent=2))
    else:
        inv = data.get("invite", {})
        print(f"Invite created!")
        print(f"  ID:      {inv.get('id')}")
        print(f"  Code:    {inv.get('code')}")
        print(f"  Email:   {inv.get('email')}")
        print(f"  Tier:    {inv.get('tier')}")
        print(f"  Expires: {inv.get('expires_at')}")
        if inv.get("email_sent"):
            print(f"  Email sent to {inv.get('email')}")


@cli.group(invoke_without_command=True)
@click.pass_context
def inference(ctx):
    """Configure local/custom LLM inference backend.

    \b
    aither inference                              # Show current config
    aither inference set ollama                    # Use local Ollama
    aither inference set vllm http://gpu:8000      # Use custom vLLM
    aither inference set openai --key sk-...       # Use OpenAI
    aither inference map reasoning deepseek-r1:14b # Route reasoning locally
    aither inference test                          # Test connectivity
    aither inference models                        # List available models
    aither inference clear                         # Reset to platform defaults
    """
    if ctx.invoked_subcommand is None:
        asyncio.run(_inference_show())


@inference.command("set")
@click.argument("provider", default="ollama")
@click.argument("url", required=False)
@click.option("--key", help="API key (stored in vault)")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def inference_set(provider, url, key, output_json):
    """Set inference provider and endpoint."""
    asyncio.run(_inference_set(provider, url, key, output_json))


@inference.command("map")
@click.argument("category", type=click.Choice(["chat", "reasoning", "coding", "vision", "general"]))
@click.argument("model")
def inference_map(category, model):
    """Map a model category to a specific model name."""
    asyncio.run(_inference_map_cmd(category, model))


@inference.command("test")
def inference_test():
    """Test connectivity to configured backend."""
    asyncio.run(_inference_test())


@inference.command("models")
def inference_models():
    """List models available on configured backend."""
    asyncio.run(_inference_models())


@inference.command("clear")
def inference_clear():
    """Remove custom config, use platform defaults."""
    asyncio.run(_inference_clear())


# ─── inference recipe ──────────────────────────────────────────────────────
# Bridges to AitherOS/config/model_recipes.yaml. These commands are local-only:
# they don't talk to Genesis. They drive the on-host bootstrap_models.py script
# and persist the selected recipe name to ~/.aither/recipe so other tools can
# read it.

_RECIPES_PATH_ENV = "AITHER_MODEL_RECIPES"
_RECIPE_STATE = "recipe"   # filename under ~/.aither/


def _resolve_recipes_path():
    from pathlib import Path
    if _RECIPES_PATH_ENV in os.environ:
        return Path(os.environ[_RECIPES_PATH_ENV])
    # Walk up from CWD looking for AitherOS/config/model_recipes.yaml
    here = Path.cwd().resolve()
    for parent in [here, *here.parents]:
        cand = parent / "AitherOS" / "config" / "model_recipes.yaml"
        if cand.exists():
            return cand
    # Last resort: relative to this package
    pkg_guess = Path(__file__).resolve().parent.parent.parent / "AitherOS" / "config" / "model_recipes.yaml"
    return pkg_guess


def _load_recipes():
    import yaml
    path = _resolve_recipes_path()
    if not path.exists():
        click.echo(f"model_recipes.yaml not found at {path}", err=True)
        click.echo("Set AITHER_MODEL_RECIPES=/path/to/model_recipes.yaml to override.", err=True)
        sys.exit(1)
    return path, yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _aither_home():
    from pathlib import Path
    return Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))


@inference.group("recipe", invoke_without_command=True)
@click.pass_context
def inference_recipe(ctx):
    """Manage named model recipes (orchestrator+reasoning+embeddings+reflex).

    \b
    aither inference recipe                  # show active recipe
    aither inference recipe list             # list available recipes
    aither inference recipe show NAME        # show recipe details
    aither inference recipe set NAME         # mark NAME as active locally
    aither inference recipe apply [NAME]     # pull all models for the recipe
    """
    if ctx.invoked_subcommand is None:
        state = _aither_home() / _RECIPE_STATE
        if state.exists():
            click.echo(state.read_text(encoding="utf-8").strip())
        else:
            _, data = _load_recipes()
            default = data.get("default", "(none)")
            click.echo(f"(no active recipe set; default = {default})")


@inference_recipe.command("list")
@click.option("--json", "as_json", is_flag=True)
def inference_recipe_list(as_json):
    path, data = _load_recipes()
    recipes = data.get("recipes", {})
    default = data.get("default")
    if as_json:
        click.echo(json.dumps({"path": str(path), "default": default,
                                "recipes": {k: v.get("description", "") for k, v in recipes.items()}},
                               indent=2))
        return
    click.echo(f"Recipes ({path}):")
    for name, body in recipes.items():
        marker = " (default)" if name == default else ""
        click.echo(f"  {name}{marker}")
        desc = body.get("description")
        if desc:
            click.echo(f"      {desc}")


@inference_recipe.command("show")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True)
def inference_recipe_show(name, as_json):
    _, data = _load_recipes()
    recipes = data.get("recipes", {})
    if name not in recipes:
        click.echo(f"Unknown recipe: {name}", err=True)
        click.echo(f"Available: {', '.join(sorted(recipes))}", err=True)
        sys.exit(1)
    recipe = recipes[name]
    if as_json:
        click.echo(json.dumps(recipe, indent=2))
        return
    click.echo(f"Recipe: {name}")
    click.echo(f"  {recipe.get('description', '')}")
    hw = recipe.get("hardware", {})
    if hw:
        click.echo(f"  hardware: gpus={hw.get('gpus', '?')}  "
                    f"min_vram_gb={hw.get('min_vram_gb', '?')}  "
                    f"cpu_fallback={hw.get('cpu_fallback', False)}")
    for role, defn in (recipe.get("models") or {}).items():
        if not isinstance(defn, dict):
            continue
        click.echo(f"  {role:14s} {defn.get('backend', '?'):8s} "
                    f"{defn.get('model_id', '?')} "
                    f"(quant={defn.get('quantization', 'none')}, "
                    f"vram={defn.get('vram_gb', '?')}GB)")


@inference_recipe.command("set")
@click.argument("name")
def inference_recipe_set(name):
    _, data = _load_recipes()
    if name not in data.get("recipes", {}):
        click.echo(f"Unknown recipe: {name}", err=True)
        sys.exit(1)
    home = _aither_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / _RECIPE_STATE).write_text(name + "\n", encoding="utf-8")
    click.echo(f"Active recipe set to: {name}")
    click.echo(f"Apply with: aither inference recipe apply")


@inference_recipe.command("apply")
@click.argument("name", required=False)
@click.option("--check", is_flag=True, help="Verify presence; do not download")
@click.option("--json", "as_json", is_flag=True)
def inference_recipe_apply(name, check, as_json):
    """Pull all backing models for the recipe via bootstrap_models.py."""
    import subprocess as _subprocess
    from pathlib import Path
    recipes_path = _resolve_recipes_path()
    if not name:
        state = _aither_home() / _RECIPE_STATE
        if state.exists():
            name = state.read_text(encoding="utf-8").strip()
        else:
            _, data = _load_recipes()
            name = data.get("default")
    if not name:
        click.echo("No recipe selected. Run: aither inference recipe set <name>", err=True)
        sys.exit(1)
    # Locate bootstrap_models.py adjacent to recipes file (../scripts/bootstrap_models.py)
    aither_root = recipes_path.parent.parent   # AitherOS/
    script = aither_root / "scripts" / "bootstrap_models.py"
    if not script.exists():
        click.echo(f"bootstrap_models.py not found at {script}", err=True)
        sys.exit(1)
    cmd = [sys.executable, str(script), "--config", str(recipes_path), "--recipe", name]
    if check:
        cmd.append("--check")
    if as_json:
        cmd.append("--json")
    click.echo(f"$ {' '.join(cmd)}")
    raise SystemExit(_subprocess.call(cmd))


# ─── agents (proxy to `adk agent ...`) ─────────────────────────────────────
# AitherShell deliberately does not duplicate identity / federation logic;
# it shells out to the ADK typer app where the canonical implementation lives.

@cli.group(invoke_without_command=True)
@click.pass_context
def agents(ctx):
    """Manage agent identities, keypairs, and portal federation.

    Proxies to ``adk agent <subcommand>``. Requires the `aither-platform`
    package to be importable (installed automatically with AitherOS).

    \b
    aither agents keygen NAME            # generate / rotate Ed25519 keypair
    aither agents integrate --only portal  # push identity to api.aitherium.com
    aither agents fleet [--tenant T]     # list visible fleet
    aither agents whoami                 # show active portal identity + scope
    aither agents unregister NAME        # remove identity from portal directory
    """
    if ctx.invoked_subcommand is None:
        _agents_proxy(["--help"])


def _agents_proxy(args):
    """Invoke `adk agent <args>` resiliently.

    Resolution order:
      1. `adk` on PATH (installed via `pip install aither-platform` or `awdk`)
      2. `python -m aither_adk.cli agent ...` (internal package)
      3. `python -m adk.cli agent ...` (legacy top-level package)
    Returns the subprocess exit code via SystemExit so click sees the right code.
    """
    import subprocess as _subprocess
    from shutil import which as _which
    attempts = []
    adk = _which("adk")
    if adk:
        attempts.append([adk, "agent", *args])
    attempts.append([sys.executable, "-m", "aither_adk.cli", "agent", *args])
    attempts.append([sys.executable, "-m", "adk.cli", "agent", *args])
    last_err = None
    for cmd in attempts:
        try:
            rc = _subprocess.call(cmd)
            # 127 / 1 from a -m attempt with no module is unrecoverable for THIS cmd,
            # but try the next strategy on ImportError-class failures only.
            if rc == 0 or rc == 2:   # 2 = typer usage error → command worked, args were wrong
                raise SystemExit(rc)
            last_err = (cmd, rc)
            # Heuristic: if `-m` reports "No module named", fall through to next
            if "-m" in cmd and rc in (1, 127):
                continue
            raise SystemExit(rc)
        except FileNotFoundError as exc:
            last_err = (cmd, str(exc))
            continue
    click.echo("Unable to invoke `adk agent`. Install with: pip install aither-platform",
                err=True)
    if last_err:
        click.echo(f"Last attempt: {last_err}", err=True)
    sys.exit(127)


def _which_adk():
    """Legacy helper kept for back-compat; prefer _agents_proxy."""
    from shutil import which
    return which("adk")


# Each subcommand forwards positional args + click options as raw arg list.
# Using @click.pass_context + ctx.args via ignore_unknown_options keeps us out
# of the way of typer's own arg parsing on the ADK side.

def _make_agents_passthrough(name, help_text):
    @agents.command(name=name, help=help_text, context_settings={"ignore_unknown_options": True,
                                                                    "allow_extra_args": True})
    @click.pass_context
    def _cmd(ctx):
        _agents_proxy([name, *ctx.args])
    _cmd.__name__ = f"agents_{name}"
    return _cmd


_make_agents_passthrough("keygen", "Generate or rotate an Ed25519 keypair for an identity.")
_make_agents_passthrough("integrate", "Provision an identity locally / to AitherOS / to portal.")
_make_agents_passthrough("fleet", "List visible fleet from portal (filter by tenant/workspace).")
_make_agents_passthrough("whoami", "Show active portal identity, token expiry, and scope.")
_make_agents_passthrough("introspect", "Show forge session outcome, attempts, stuck criteria.")
_make_agents_passthrough("unregister", "Remove an identity from the portal directory.")
_make_agents_passthrough("list", "List local identities discovered under config/identities/.")


# ─── agents external (developer portal — OpenClaw/Hermes/3rd-party agents) ─
# Targets `POST /developer/agents/register` and friends on Genesis. This is
# the surface for *external* agents (not local AitherOS identities).

@agents.group("external")
def agents_external():
    """Register and manage 3rd-party agents (OpenClaw, Hermes, custom).

    \b
    aither agents external register NAME --url URL --tier explorer
    aither agents external list
    aither agents external get AGENT_ID
    aither agents external verify AGENT_ID TOKEN
    aither agents external suspend AGENT_ID
    aither agents external reactivate AGENT_ID
    aither agents external usage AGENT_ID
    """
    pass


def _genesis_url():
    return os.environ.get("AITHER_GENESIS_URL",
                          os.environ.get("AITHER_URL", "http://localhost:8001"))


def _portal_headers():
    """Build headers using the portal token written by `aither login`."""
    from pathlib import Path
    tok_path = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "portal.token"
    token = os.environ.get("AITHER_API_KEY") or os.environ.get("AITHER_PORTAL_TOKEN")
    if not token and tok_path.exists():
        token = tok_path.read_text(encoding="utf-8").strip()
    if not token:
        click.echo("Not logged in. Run `aither login` first.", err=True)
        sys.exit(1)
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


@agents_external.command("register")
@click.argument("name")
@click.option("--description", default="", help="Short description shown in the portal.")
@click.option("--url", default=None, help="Agent endpoint URL (triggers URL ownership check).")
@click.option("--tier", type=click.Choice(["explorer", "builder", "enterprise"]),
              default="explorer", help="Capability tier.")
@click.option("--email", default=None, help="Contact email (defaults to portal account email).")
@click.option("--skill", "skills", multiple=True, help="Repeatable; advertised skill tags.")
@click.option("--allow-tool", "tool_allow", multiple=True,
              help="Repeatable; explicit MCP tool allowlist (overrides tier default).")
@click.option("--allow-model", "model_allow", multiple=True,
              help="Repeatable; explicit LLM model allowlist.")
@click.option("--ip", "ip_allow", multiple=True,
              help="Repeatable; CIDR/IP allowlist for outbound traffic.")
@click.option("--residency", default="global",
              help="Data residency hint (global|us|eu|ap-...).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of formatted output.")
def agents_external_register(name, description, url, tier, email, skills, tool_allow,
                              model_allow, ip_allow, residency, as_json):
    """Register a new external agent and receive its API key (shown once)."""
    import httpx
    body = {
        "name": name,
        "description": description,
        "tier": tier,
        "url": url,
        "contact_email": email,
        "skills": list(skills) or None,
        "custom_tool_allowlist": list(tool_allow) or None,
        "allowed_models": list(model_allow) or None,
        "ip_allowlist": list(ip_allow) or None,
        "data_residency": residency,
    }
    body = {k: v for k, v in body.items() if v is not None}
    try:
        with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=20) as c:
            resp = c.post("/developer/agents/register", json=body)
    except httpx.HTTPError as exc:
        click.echo(f"Request failed: {exc}", err=True)
        sys.exit(2)
    if resp.status_code >= 400:
        click.echo(f"HTTP {resp.status_code}: {resp.text[:400]}", err=True)
        sys.exit(2)
    data = resp.json()
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    click.echo(f"Registered: {data.get('agent_id')}")
    click.echo(f"Tier:       {data.get('tier')}")
    click.echo(f"Status:     {data.get('status')}")
    if data.get("verification_token"):
        click.echo(f"Verify-tok: {data['verification_token']}")
        click.echo("\nServe this from your agent at /.well-known/aither-verify.json")
        click.echo("then: aither agents external verify-url <agent_id>")
    api_key = data.get("api_key")
    if api_key:
        click.echo("\n=== API KEY (shown ONCE, store immediately) ===")
        click.echo(api_key)
        click.echo("=" * 47)


@agents_external.command("list")
@click.option("--json", "as_json", is_flag=True)
def agents_external_list(as_json):
    """List external agents owned by the authenticated tenant."""
    import httpx
    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=15) as c:
        resp = c.get("/developer/agents")
    if resp.status_code >= 400:
        click.echo(f"HTTP {resp.status_code}: {resp.text[:300]}", err=True)
        sys.exit(2)
    data = resp.json()
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    agents_list = data if isinstance(data, list) else data.get("agents", [])
    if not agents_list:
        click.echo("No external agents registered.")
        return
    click.echo(f"{'AGENT_ID':<40} {'TIER':<10} {'STATUS':<25} NAME")
    for a in agents_list:
        click.echo(f"{a.get('agent_id', '?'):<40} {a.get('tier', '?'):<10} "
                    f"{a.get('status', '?'):<25} {a.get('name', '?')}")


def _agent_simple_action(path_suffix, agent_id, method="POST", payload=None):
    import httpx
    method = method.upper()
    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=15) as c:
        if method == "GET":
            resp = c.get(f"/developer/agents/{agent_id}{path_suffix}")
        elif method == "POST":
            resp = c.post(f"/developer/agents/{agent_id}{path_suffix}", json=payload or {})
        else:
            raise ValueError(f"Unsupported method: {method}")
    if resp.status_code >= 400:
        click.echo(f"HTTP {resp.status_code}: {resp.text[:300]}", err=True)
        sys.exit(2)
    click.echo(json.dumps(resp.json(), indent=2))


@agents_external.command("get")
@click.argument("agent_id")
def agents_external_get(agent_id):
    """Show full details for an external agent."""
    _agent_simple_action("", agent_id, method="GET")


@agents_external.command("verify-url")
@click.argument("agent_id")
def agents_external_verify_url(agent_id):
    """Trigger URL-ownership verification (fetches /.well-known/aither-verify.json)."""
    _agent_simple_action("/verify-url", agent_id, method="POST")


@agents_external.command("verify")
@click.argument("agent_id")
@click.argument("token")
def agents_external_verify(agent_id, token):
    """Verify an external agent using its emailed verification token."""
    import httpx
    body = {"agent_id": agent_id, "verification_token": token}
    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=15) as c:
        resp = c.post("/developer/agents/verify", json=body)
    if resp.status_code >= 400:
        click.echo(f"HTTP {resp.status_code}: {resp.text[:300]}", err=True)
        sys.exit(2)
    click.echo(json.dumps(resp.json(), indent=2))


@agents_external.command("suspend")
@click.argument("agent_id")
@click.option("--reason", default="manual suspension via aither cli")
def agents_external_suspend(agent_id, reason):
    """Suspend an external agent (revokes API key usage)."""
    _agent_simple_action("/suspend", agent_id, payload={"reason": reason})


@agents_external.command("reactivate")
@click.argument("agent_id")
def agents_external_reactivate(agent_id):
    """Reactivate a suspended external agent."""
    _agent_simple_action("/reactivate", agent_id)


@agents_external.command("deactivate")
@click.argument("agent_id")
def agents_external_deactivate(agent_id):
    """Permanently deactivate (tombstone) an external agent."""
    if not click.confirm(f"Permanently deactivate {agent_id}? This cannot be undone."):
        sys.exit(1)
    _agent_simple_action("/deactivate", agent_id)


@agents_external.command("usage")
@click.argument("agent_id")
@click.option("--history", is_flag=True, help="Show historical usage instead of current period.")
def agents_external_usage(agent_id, history):
    """Show usage / quota stats for an external agent."""
    suffix = "/usage/history" if history else "/usage"
    _agent_simple_action(suffix, agent_id, method="GET")


@agents_external.command("upgrade")
@click.argument("agent_id")
@click.argument("tier", type=click.Choice(["explorer", "builder", "enterprise"]))
def agents_external_upgrade(agent_id, tier):
    """Change tier (capabilities + quota) for an external agent."""
    _agent_simple_action("/upgrade", agent_id, payload={"tier": tier})


async def _get_inference_deps():
    """Shared auth + HTTP setup for inference commands."""
    import httpx
    try:
        from adk.shell.auth import AuthStore as AS
    except ImportError:
        print("Auth module not available. Run `aither setup` first.", file=sys.stderr)
        sys.exit(1)
    token = AS.get_active_token()
    if not token:
        print("Not logged in. Run `aither setup` to authenticate.", file=sys.stderr)
        sys.exit(1)
    url = os.environ.get("AITHER_IDENTITY_URL", os.environ.get("AITHER_GENESIS_URL", "http://localhost:8001"))
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    return httpx, url, headers


async def _inference_show():
    httpx, url, headers = await _get_inference_deps()
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=15) as c:
        resp = await c.get("/auth/me/inference-config")
    if resp.status_code != 200 or not resp.json().get("config"):
        print("No custom inference configured — using platform defaults.")
        print("\nSet up: aither inference set ollama")
        return
    config = resp.json()["config"]
    print(f"Provider:  {config.get('provider', '?')}")
    print(f"URL:       {config.get('base_url', '?')}")
    print(f"API Key:   {'configured' if config.get('has_secret_key') else 'none'}")
    mappings = config.get("model_mapping", {})
    if mappings:
        print("Mappings:")
        for cat, model in mappings.items():
            print(f"  {cat} -> {model}")


async def _inference_set(provider, url, key, output_json):
    httpx, base, headers = await _get_inference_deps()
    defaults = {"ollama": "http://localhost:11434", "vllm": "http://localhost:8000",
                "openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com"}
    if not url:
        url = defaults.get(provider)
    if not url:
        print(f"Specify URL: aither inference set {provider} http://...", file=sys.stderr)
        sys.exit(1)
    body = {"provider": provider, "base_url": url}
    if key:
        body["api_key"] = key
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as c:
        resp = await c.post("/auth/me/inference-config", json=body)
    if resp.status_code != 200:
        print(f"Failed: {resp.status_code} — {resp.text[:300]}", file=sys.stderr)
        sys.exit(1)
    if output_json:
        print(json.dumps(resp.json(), indent=2))
    else:
        print(f"Inference configured: {provider} at {url}")
        print(f"Next: aither inference map chat <model-name>")
        print(f"Then: aither inference test")


async def _inference_map_cmd(category, model):
    httpx, base, headers = await _get_inference_deps()
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as c:
        resp = await c.get("/auth/me/inference-config")
    if resp.status_code != 200 or not resp.json().get("config"):
        print("No backend configured. Run: aither inference set ollama", file=sys.stderr)
        sys.exit(1)
    config = resp.json()["config"]
    mappings = config.get("model_mapping", {})
    mappings[category] = model
    body = {"provider": config["provider"], "base_url": config.get("base_url"), "model_mapping": mappings}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as c:
        resp = await c.post("/auth/me/inference-config", json=body)
    if resp.status_code != 200:
        print(f"Failed: {resp.status_code}", file=sys.stderr)
        sys.exit(1)
    print(f"Mapped {category} -> {model}")


async def _inference_test():
    httpx, base, headers = await _get_inference_deps()
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as c:
        resp = await c.get("/auth/me/inference-config")
    if resp.status_code != 200 or not resp.json().get("config"):
        print("No backend configured.", file=sys.stderr)
        sys.exit(1)
    config = resp.json()["config"]
    provider = config.get("provider", "?")
    url = config.get("base_url", "")
    print(f"Testing {provider} at {url}...")
    try:
        if provider == "ollama":
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{url}/api/tags")
            models = r.json().get("models", [])
            print(f"Connected! {len(models)} model(s):")
            for m in models[:10]:
                print(f"  {m.get('name', '?')} ({m.get('size', 0) / (1024**3):.1f}GB)")
        elif provider in ("vllm", "custom"):
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{url}/v1/models")
            for m in r.json().get("data", []):
                print(f"  {m.get('id', '?')}")
        else:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{url}/models")
            print(f"Endpoint reachable: {r.status_code}")
    except httpx.ConnectError:
        print(f"Connection refused — is {provider} running at {url}?", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


async def _inference_models():
    httpx, base, headers = await _get_inference_deps()
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as c:
        resp = await c.get("/auth/me/inference-config")
    if resp.status_code != 200 or not resp.json().get("config"):
        print("No backend configured.", file=sys.stderr)
        sys.exit(1)
    config = resp.json()["config"]
    provider = config.get("provider")
    url = config.get("base_url")
    try:
        if provider == "ollama":
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{url}/api/tags")
            for m in r.json().get("models", []):
                print(f"  {m.get('name', '?')} ({m.get('size', 0) / (1024**3):.1f}GB)")
        elif provider in ("vllm", "custom"):
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{url}/v1/models")
            for m in r.json().get("data", []):
                print(f"  {m.get('id', '?')}")
        else:
            print(f"Model listing not supported for {provider}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


async def _inference_clear():
    httpx, base, headers = await _get_inference_deps()
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as c:
        resp = await c.delete("/auth/me/inference-config")
    if resp.status_code != 200:
        print(f"Failed: {resp.status_code}", file=sys.stderr)
        sys.exit(1)
    print("Custom inference config removed. Using platform defaults.")


@cli.command()
@click.argument("action", required=False, default="status",
                type=click.Choice(["setup", "status", "stop", "url", "register"]))
@click.option("--port", default=8001, type=int, help="Local port to tunnel")
def tunnel(action, port):
    """Manage Cloudflare tunnel for remote portal access.

    \b
    aither tunnel                  # Show tunnel status
    aither tunnel setup            # Start a tunnel
    aither tunnel setup --port 8001
    aither tunnel stop             # Stop the tunnel
    aither tunnel register         # Register URL with your account
    """
    asyncio.run(_tunnel_cmd(action, port))


async def _tunnel_cmd(action, port):
    import shutil
    cf = shutil.which("cloudflared")
    if action == "setup":
        if not cf:
            import platform as plat
            s = plat.system().lower()
            cmd = {"linux": "curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared",
                   "darwin": "brew install cloudflared"}.get(s, "winget install Cloudflare.cloudflared")
            print(f"cloudflared not found. Install: {cmd}")
            sys.exit(1)
        print(f"Starting tunnel to localhost:{port}...")
        proc = subprocess.Popen(
            [cf, "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        import time
        tunnel_url = None
        deadline = time.time() + 15
        while time.time() < deadline:
            line = proc.stderr.readline() if proc.stderr else ""
            if "trycloudflare.com" in line:
                for word in line.split():
                    if "trycloudflare.com" in word:
                        tunnel_url = word.strip()
                        if not tunnel_url.startswith("http"):
                            tunnel_url = f"https://{tunnel_url}"
                        break
                if tunnel_url:
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if tunnel_url:
            from pathlib import Path
            info = {"url": tunnel_url, "local_port": port, "pid": proc.pid}
            tf = Path.home() / ".aither" / "tunnel.json"
            tf.parent.mkdir(parents=True, exist_ok=True)
            tf.write_text(json.dumps(info), encoding="utf-8")
            print(f"Tunnel active!")
            print(f"  URL:   {tunnel_url}")
            print(f"  Local: http://localhost:{port}")
            print(f"  PID:   {proc.pid}")
            print(f"\nRegister with portal: aither tunnel register")
        else:
            print("Tunnel starting... check: aither tunnel status")
    elif action == "status":
        from pathlib import Path
        tf = Path.home() / ".aither" / "tunnel.json"
        if not tf.exists():
            print("No tunnel running. Start: aither tunnel setup")
            return
        info = json.loads(tf.read_text(encoding="utf-8"))
        pid = info.get("pid")
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except (OSError, ProcessLookupError):
                pass
        if alive:
            print(f"Tunnel active: {info.get('url')} -> localhost:{info.get('local_port')}")
        else:
            print("Tunnel process dead. Restart: aither tunnel setup")
    elif action == "stop":
        from pathlib import Path
        tf = Path.home() / ".aither" / "tunnel.json"
        if tf.exists():
            info = json.loads(tf.read_text(encoding="utf-8"))
            pid = info.get("pid")
            if pid:
                try:
                    import signal
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
            tf.unlink(missing_ok=True)
        print("Tunnel stopped.")
    elif action == "register":
        import httpx
        from pathlib import Path
        tf = Path.home() / ".aither" / "tunnel.json"
        if not tf.exists():
            print("No tunnel running. Start: aither tunnel setup", file=sys.stderr)
            sys.exit(1)
        info = json.loads(tf.read_text(encoding="utf-8"))
        _, base, headers = await _get_inference_deps()
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=15) as c:
            resp = await c.patch("/auth/me/profile", json={
                "metadata": {"node_tunnel_url": info["url"]}
            })
        if resp.status_code == 200:
            print(f"Tunnel registered: {info['url']}")
        else:
            print(f"Failed: {resp.status_code}", file=sys.stderr)
    elif action == "url":
        from pathlib import Path
        tf = Path.home() / ".aither" / "tunnel.json"
        if tf.exists():
            print(json.loads(tf.read_text(encoding="utf-8")).get("url", "none"))
        else:
            print("No tunnel running.")


@cli.group()
def mcp():
    """MCP (Model Context Protocol) tools for Claude Code integration."""
    pass


@mcp.command()
@click.option("--local", "mode", flag_value="local", help="Force local backend")
@click.option("--remote", "mode", flag_value="remote", help="Force remote gateway")
def serve(mode):
    """Start MCP stdio server for Claude Code.

    Auto-detects local Genesis/Node or falls back to mcp.aitherium.com.

    Configure in Claude Code:
        {"mcpServers": {"aitheros": {"command": "aither", "args": ["mcp", "serve"]}}}
    """
    from adk.shell.mcp_bridge import serve_mcp
    asyncio.run(serve_mcp(mode=mode or "auto"))


@mcp.command()
@click.option("--mode", type=click.Choice(["local", "remote"]), default="local",
              help="local = Docker gateway (8182), remote = mcp.aitherium.com")
@click.option("--ide", type=click.Choice(["claude-code", "cursor", "windsurf", "vscode"]),
              default="claude-code", help="Target IDE")
@click.option("--project-dir", type=click.Path(exists=True), default=".",
              help="Project directory for config file")
@click.option("--bake-token", is_flag=True, default=False,
              help="Bake auth token into config headers (fallback for IDEs without OAuth)")
def setup(mode, ide, project_dir, bake_token):
    """Generate MCP config for your IDE.

    The gateway supports OAuth (RFC 9728) — IDEs like Claude Code and Cursor
    will discover AitherIdentity and handle auth automatically via the
    /authenticate dialog. No tokens needed in the config.

    Use --bake-token to embed a token directly for IDEs that lack OAuth.
    """
    from adk.shell.mcp_setup import (
        resolve_auth, resolve_gateway_url, generate_config,
        write_config, probe_gateway,
    )

    url = resolve_gateway_url(mode)
    token, source = resolve_auth()

    if bake_token:
        # User explicitly wants to bake a token — resolve one
        if token:
            click.echo(f"  Auth: baking token from {source}")
        else:
            click.echo("  Auth: no token found for --bake-token")
            if click.confirm("  Log in now via browser? (device flow)", default=True):
                try:
                    if mode == "remote":
                        identity_url = "https://api.aitherium.com"
                    else:
                        identity_url = os.environ.get(
                            "AITHER_PORTAL_URL",
                            "https://api.aitherium.com",
                        )
                    token_result = _device_flow_login(identity_url, client_name=f"MCP-{ide}")
                    token = token_result.get("access_token", "")
                    if token:
                        aither_dir = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
                        aither_dir.mkdir(parents=True, exist_ok=True)
                        (aither_dir / "portal.token").write_text(token + "\n", encoding="utf-8")
                        auth_json_path = aither_dir / "auth.json"
                        try:
                            auth_data = json.loads(auth_json_path.read_text(encoding="utf-8")) if auth_json_path.is_file() else {"version": 1, "active_profile": "local", "profiles": {}}
                            user = token_result.get("user", {})
                            auth_data["profiles"]["local"] = {
                                "endpoint": identity_url,
                                "token_type": "portal",
                                "access_token": token,
                                "user": user if isinstance(user, dict) else {"id": str(user)},
                            }
                            auth_json_path.write_text(json.dumps(auth_data, indent=2) + "\n", encoding="utf-8")
                        except Exception:
                            pass
                        click.echo("  Auth: logged in successfully")
                        source = "device flow"
                except Exception as e:
                    click.echo(f"  Auth failed: {e}")
                    click.echo("  Set AITHER_API_KEY env var manually, or run: aither login --browser")
        config = generate_config(ide, url, token=token)
    else:
        # Default: no token in config — IDE uses OAuth via /authenticate
        click.echo("  Auth: OAuth (IDE will handle via /authenticate)")
        config = generate_config(ide, url, token=None)

    out_path = write_config(config, ide, project_dir)
    click.echo(f"  Config: {out_path}")
    click.echo(f"  Gateway: {url}")

    # Probe gateway if local
    if mode == "local":
        status = probe_gateway(url, token)
        if status["connected"]:
            click.echo(f"  Status: connected ({status['status']})")
        else:
            click.echo("  Status: not reachable — start with: docker compose up -d aither-mcpgateway")
            click.echo("  Tip: No Docker gateway? Run 'aither mcp node' for a lightweight local server.")

        # Auto-trust AitherNet CA for local TLS
        from adk.shell.mcp_setup import ensure_local_ca_trust
        ca_result = ensure_local_ca_trust()
        if ca_result == "set":
            click.echo("  TLS: AitherNet CA trusted (Root CA installed + NODE_EXTRA_CA_CERTS set)")
        elif ca_result == "already":
            click.echo("  TLS: AitherNet CA already trusted")
        elif ca_result:
            click.echo(f"  TLS: {ca_result}")

    click.echo()
    if not bake_token:
        click.echo("  Restart your IDE, then use /authenticate to connect.")
    else:
        click.echo("  Restart your IDE to apply.")


@mcp.command()
def status():
    """Check MCP gateway connectivity, tier, tool count, balance."""
    from adk.shell.mcp_setup import resolve_auth, probe_gateway, _GATEWAY_URLS

    token, source = resolve_auth()
    click.echo(f"  Auth source: {source}")

    for mode_name, url in _GATEWAY_URLS.items():
        result = probe_gateway(url, token)
        icon = "[OK]" if result["connected"] else "[--]"
        click.echo(f"  {icon} {mode_name:8s} {url}")
        if result["connected"]:
            if result.get("tier"):
                click.echo(f"           tier={result['tier']}  tools={result['tool_count']}  "
                           f"balance={result['balance']}")
            if result.get("user"):
                click.echo(f"           user={result['user']}")
        elif result.get("error"):
            click.echo(f"           {result['error']}")


@mcp.command()
@click.argument("tier", type=click.Choice(["free", "starter", "pro", "enterprise", "reset"]))
@click.option("--ide", type=click.Choice(["claude-code", "cursor", "windsurf", "vscode"]),
              default="claude-code")
@click.option("--project-dir", type=click.Path(exists=True), default=".")
def scope(tier, ide, project_dir):
    """Switch MCP tier simulation (admin only). Restart IDE to apply.

    Adds X-Simulate-Tier header to your .mcp.json so the gateway returns
    tools filtered for that tier. Use 'reset' to remove simulation.
    """
    from adk.shell.mcp_setup import set_tier_simulation

    try:
        path, old_est, new_est = set_tier_simulation(ide, project_dir, tier)
    except FileNotFoundError as e:
        click.echo(f"  Error: {e}", err=True)
        raise SystemExit(1)

    if tier == "reset":
        click.echo(f"  Tier simulation removed from {path}")
        click.echo("  Restart IDE to see full admin tool set.")
    else:
        click.echo(f"  Simulating tier: {tier}")
        click.echo(f"  Config updated: {path}")
        if new_est > 0:
            click.echo(f"  Estimated tools: ~{new_est}")
        click.echo("  Restart IDE to apply.")


@mcp.command()
@click.option("--mode", type=click.Choice(["proxy", "standalone"]), default="proxy",
              help="proxy = forward to mcp.aitherium.com, standalone = local tools only")
@click.option("--port", type=int, default=8182,
              help="HTTP port for the MCP server (default: 8182)")
def node(mode, port):
    """Start a local awnode MCP server.

    proxy mode: Forwards tool calls to mcp.aitherium.com (needs aither login).
    standalone mode: Basic local dev tools only (no account needed).

    The server speaks MCP over streamable-http at http://localhost:PORT/mcp.

    \b
    Configure in Claude Code:
        {"mcpServers": {"aitheros": {"url": "http://localhost:8182/mcp"}}}
    """
    from adk.shell.node.server import run_node
    run_node(mode=mode, port=port)


@mcp.command("add")
@click.argument("server_url")
@click.option("--api-key", "-k", required=True, help="MCP gateway API key")
@click.option("--name", "-n", default=None, help="Friendly name for this server")
def mcp_add(server_url, api_key, name):
    """Register a remote MCP tool server.

    \b
    aither mcp add mcp.aitherium.com --api-key <key>
    aither mcp add https://mcp.example.com -k <key> -n "My Tools"
    """
    from pathlib import Path as P
    import datetime

    # Normalize URL
    if not server_url.startswith("http"):
        server_url = f"https://{server_url}"

    config_dir = P.home() / ".aither"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "mcp_servers.json"

    # Load existing
    servers = {}
    if config_file.exists():
        servers = json.loads(config_file.read_text())

    server_name = name or server_url.split("//")[-1].split("/")[0]
    servers[server_name] = {
        "url": server_url,
        "api_key": api_key,
        "added_at": datetime.datetime.now().isoformat(),
    }

    config_file.write_text(json.dumps(servers, indent=2))
    config_file.chmod(0o600)
    print(f"Added MCP server: {server_name} -> {server_url}")
    print(f"Config saved to: {config_file}")

    # Verify connection
    try:
        import httpx
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{server_url}/health", headers={"Authorization": f"Bearer {api_key}"})
            if resp.status_code == 200:
                print("Connection verified: healthy")
            else:
                print(f"Warning: server returned {resp.status_code} (may still work)")
    except Exception as e:
        print(f"Warning: could not verify connection ({e})")


@mcp.command("list")
def mcp_list():
    """List registered MCP servers."""
    from pathlib import Path as P

    config_file = P.home() / ".aither" / "mcp_servers.json"
    if not config_file.exists():
        print("No MCP servers registered. Use: aither mcp add <url> --api-key <key>")
        return

    servers = json.loads(config_file.read_text())
    if not servers:
        print("No MCP servers registered.")
        return

    print(f"{'Name':<25} {'URL':<40} {'Added'}")
    print("-" * 80)
    for name, info in servers.items():
        added = info.get("added_at", "")[:10]
        print(f"{name:<25} {info['url']:<40} {added}")


@mcp.command("remove")
@click.argument("name")
def mcp_remove(name):
    """Remove a registered MCP server."""
    from pathlib import Path as P

    config_file = P.home() / ".aither" / "mcp_servers.json"
    if not config_file.exists():
        print("No MCP servers registered.")
        return

    servers = json.loads(config_file.read_text())
    if name not in servers:
        print(f"Server '{name}' not found. Available: {', '.join(servers.keys())}")
        return

    del servers[name]
    config_file.write_text(json.dumps(servers, indent=2))
    print(f"Removed MCP server: {name}")


@cli.command()
@click.option("--json", "emit_json", is_flag=True, help="Emit JSON instead of human-readable output")
@click.option("--timeout", type=float, default=3.0, help="Per-check HTTP timeout (default: 3s)")
def doctor(emit_json: bool, timeout: float):
    """End-to-end health check (license, portal, backends).

    Delegates to ``awnode doctor`` when available; otherwise runs an
    in-process version. Use this whenever paid features misbehave to find
    out which surface is degraded.
    """
    # Try awnode first — it has the canonical implementation
    try:
        from awnode.cli import _doctor as _node_doctor
        sys.exit(asyncio.run(_node_doctor(emit_json=emit_json, timeout=timeout)))
    except ImportError:
        pass

    # Fallback: minimal in-process check
    import httpx
    from pathlib import Path

    aither_home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
    checks = {
        "aither_home": ("ok" if aither_home.exists() else "warn", str(aither_home)),
        "license_cache": ("ok" if (aither_home / "license.json").exists() else "warn",
                           str(aither_home / "license.json")),
        "portal_token": ("ok" if (aither_home / "portal.token").exists() else "warn",
                          "run `aither login`" if not (aither_home / "portal.token").exists() else "present"),
        "api_key": ("ok" if os.environ.get("AITHER_API_KEY") else "warn", "AITHER_API_KEY env"),
    }
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(os.environ.get("AITHER_URL", "http://localhost:8001") + "/health")
            checks["genesis"] = ("ok" if r.status_code < 500 else "warn", f"HTTP {r.status_code}")
    except Exception as e:
        checks["genesis"] = ("warn", f"unreachable: {type(e).__name__}")

    if emit_json:
        print(json.dumps({"checks": {k: {"status": v[0], "detail": v[1]} for k, v in checks.items()},
                           "note": "awnode not installed — fallback mode"}, indent=2))
    else:
        print("AitherShell Doctor (fallback — install awnode for full checks)")
        print("=" * 60)
        glyph = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}
        for name, (status, detail) in checks.items():
            print(f"  {glyph.get(status, '[?]   ')} {name:<16} {detail}")

    # ─── Phase 2: awnode /mcp/tools (catches standalone vs full mode + tool count)
    node_url = os.environ.get("AITHER_NODE_URL", "http://localhost:8090")
    try:
        with httpx.Client(timeout=timeout) as c:
            mt = c.get(f"{node_url}/mcp/tools")
        if mt.status_code == 200:
            data = mt.json()
            label = f"{data.get('mode','?')} mode, {data.get('available_modules','?')}/{data.get('total_modules','?')} tools"
            checks["awnode_mcp"] = ("ok", label)
        elif mt.status_code == 503:
            checks["awnode_mcp"] = ("warn", "tool registry degraded")
        else:
            checks["awnode_mcp"] = ("warn", f"HTTP {mt.status_code}")
    except Exception as exc:
        checks["awnode_mcp"] = ("warn", f"unreachable: {type(exc).__name__}")

    # ─── Phase 3: model recipe state
    from pathlib import Path
    state = aither_home / "recipe"
    checks["model_recipe"] = (
        ("ok", state.read_text(encoding="utf-8").strip()) if state.exists()
        else ("warn", "no active recipe (run `aither inference recipe set <name>`)")
    )

    if not emit_json:
        for name in ("awnode_mcp", "model_recipe"):
            status, detail = checks[name]
            print(f"  {glyph.get(status, '[?]   ')} {name:<16} {detail}")

    sys.exit(0 if all(s[0] == "ok" for s in checks.values()) else 1)


# ─── aither up (single-command bootstrap) ──────────────────────────────────

@cli.command()
@click.option("--profile", default="dgx-hybrid",
              help="Docker compose profile (dgx-hybrid|chat-full|chat-minimal|node-edge).")
@click.option("--recipe", default=None,
              help="Model recipe name (defaults to active recipe or `dgx-hybrid`).")
@click.option("--skip-pull", is_flag=True, help="Skip model bootstrap step.")
@click.option("--skip-docker", is_flag=True, help="Skip `docker compose up` step.")
@click.option("--skip-integrate", is_flag=True, help="Skip portal agent integration step.")
@click.option("--dry-run", is_flag=True, help="Print the plan but do not execute.")
def up(profile, recipe, skip_pull, skip_docker, skip_integrate, dry_run):
    """One-command bootstrap: models + docker + agents.

    Idempotent. Safe to re-run. Each step is independently skippable so a
    user can resume after a partial run. Exits non-zero on the first hard
    failure (model pull or docker bring-up).

    \b
    aither up                              # full bootstrap, dgx-hybrid profile
    aither up --profile chat-minimal       # smaller stack
    aither up --recipe solo-5090           # use DGX-less recipe
    aither up --skip-pull                  # don't re-download models
    aither up --dry-run                    # plan only
    """
    import shutil
    from pathlib import Path
    import subprocess as _subprocess

    plan = []
    home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
    home.mkdir(parents=True, exist_ok=True)

    # Step 1 — set active recipe if requested
    if recipe:
        plan.append(("set-recipe", [sys.executable, "-c",
                                      f"from adk.shell.cli import cli; "
                                      f"cli.main(['inference','recipe','set','{recipe}'], standalone_mode=False)"]))

    # Step 2 — apply recipe (pull models)
    if not skip_pull:
        plan.append(("pull-models", [sys.executable, "-c",
                                       "from adk.shell.cli import cli; "
                                       "cli.main(['inference','recipe','apply'], standalone_mode=False)"]))

    # Step 3 — docker compose up
    if not skip_docker:
        compose = shutil.which("docker")
        if not compose:
            click.echo("docker not found on PATH — run with --skip-docker or install Docker first.",
                        err=True)
            sys.exit(2)
        # Find compose file by walking up
        cwd = Path.cwd().resolve()
        compose_file = None
        compose_root = None
        for parent in [cwd, *cwd.parents]:
            cand = parent / ".DEPLOYMENT" / "compose" / "docker-compose.aitheros.yml"
            if cand.exists():
                compose_file = cand
                compose_root = parent
                break
        if not compose_file:
            click.echo(".DEPLOYMENT/compose/docker-compose.aitheros.yml not found anywhere up the tree.", err=True)
            sys.exit(2)
        plan.append(("docker-up", [compose, "compose", "-f", str(compose_file),
                                     "--project-directory", str(compose_root),
                                     "--profile", profile, "up", "-d"]))

    # Step 4 — agent integrate (portal federation)
    if not skip_integrate:
        plan.append(("integrate-agents", [sys.executable, "-c",
                                            "from adk.shell.cli import cli; "
                                            "cli.main(['agents','integrate','--only','portal'], standalone_mode=False)"]))

    # Plan preview
    click.echo("Plan:")
    for i, (label, cmd) in enumerate(plan, 1):
        shown = cmd[0] if len(cmd) <= 1 else f"{cmd[0]} ... ({len(cmd)-1} args)"
        click.echo(f"  {i}. {label:<18} {shown}")

    if dry_run:
        click.echo("\n(dry-run — no commands executed)")
        return

    failures = []
    for label, cmd in plan:
        click.echo(f"\n>>> {label}")
        rc = _subprocess.call(cmd)
        if rc != 0:
            click.echo(f"  ! {label} exited {rc}", err=True)
            failures.append((label, rc))
            # Hard-stop on the docker step; soft-continue on integrate
            if label in ("pull-models", "docker-up"):
                sys.exit(rc)

    if failures:
        click.echo(f"\nFinished with {len(failures)} soft failure(s): "
                    f"{', '.join(f'{l}({rc})' for l, rc in failures)}")
        sys.exit(1)
    click.echo("\nDone. Verify with: aither doctor")


# ─── aither lockbox (tiered model allowlist) ───────────────────────────────

@cli.group()
def lockbox():
    """Manage your private model allowlist and safety preferences.

    The lockbox has four tiers:

      user      — your personal allowlist (self-service)
      workspace — your workspace's allowlist (admin-only)
      tenant    — your tenant's allowlist (admin-only)
      platform  — deployment-wide defaults (admin-only)

    The effective allowlist is the union of all four. Backed by
    AitherSecrets and the AitherVeil tiered-lockbox helper.

    \b
    aither lockbox show
    aither lockbox add mymodel_v1.safetensors
    aither lockbox remove mymodel_v1.safetensors
    aither lockbox safety set MEDIUM
    aither lockbox admin set user alice@example.com models a.safetensors b.safetensors
    aither lockbox admin set workspace acme:design models flux-pro.safetensors
    """


def _veil_call(method, path, *, json_body=None, veil_url=None, params=None):
    """Make an authenticated call to the AitherVeil API.

    Reads bearer from ~/.aither/portal.token (written by `aither login`).
    Falls back to AITHER_API_KEY env if no portal token is present.
    """
    import json as _json
    import urllib.request
    import urllib.parse
    import urllib.error
    from pathlib import Path

    veil_url = veil_url or os.environ.get("AITHERVEIL_URL", "http://localhost:3000")
    url = veil_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {"Accept": "application/json"}
    token_path = Path.home() / ".aither" / "portal.token"
    token = None
    if token_path.exists():
        try:
            token = token_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            token = None
    token = token or os.environ.get("AITHER_API_KEY") or os.environ.get("AITHER_PORTAL_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Caller email/tenant hints for self-service routes (proxy normally injects
    # these; for direct calls we forward what the user told us via env).
    for env_name, hdr in (
        ("AITHER_USER_EMAIL", "X-User-Email"),
        ("AITHER_USER_ID", "X-User-Id"),
        ("AITHER_TENANT_ID", "X-Tenant-Id"),
        ("AITHER_WORKSPACE_ID", "X-Workspace-Id"),
    ):
        val = os.environ.get(env_name)
        if val:
            headers[hdr] = val

    data = None
    if json_body is not None:
        data = _json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, _json.loads(body)
            except _json.JSONDecodeError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            return e.code, _json.loads(body)
        except Exception:
            return e.code, {"error": str(e)}
    except urllib.error.URLError as e:
        return 0, {"error": f"AitherVeil unreachable at {veil_url}: {e.reason}"}


def _print_result(status, body, *, as_json=False):
    import json as _json
    if as_json:
        click.echo(_json.dumps({"status": status, "body": body}, indent=2))
        return
    if status == 0:
        click.echo(click.style(body.get("error", "request failed"), fg="red"), err=True)
        sys.exit(1)
    if status >= 400:
        click.echo(click.style(
            f"HTTP {status}: {body.get('error') or body.get('raw') or body}",
            fg="red"
        ), err=True)
        sys.exit(1)
    return body


@lockbox.command("show")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def lockbox_show(as_json):
    """Show your effective lockbox (merged across all tiers)."""
    status, body = _veil_call("GET", "/api/lockbox/me")
    body = _print_result(status, body, as_json=as_json)
    if as_json or body is None:
        return
    resolved = body.get("resolved", {})
    caller = body.get("caller", {})
    click.echo(click.style("Account:", bold=True) + f" {caller.get('email') or '—'}")
    click.echo(click.style("Tenant: ", bold=True) + f" {caller.get('tenant_id') or '—'}")
    click.echo(click.style("Safety: ", bold=True) +
               f" {resolved.get('safety_override') or '(inherit default)'}")
    models = resolved.get("private_models", [])
    click.echo(click.style(f"Models ({len(models)}):", bold=True))
    for m in models:
        click.echo(f"  • {m}")
    tiers = resolved.get("tiers", {}) or {}
    for name in ("user", "tenant", "platform"):
        t = tiers.get(name)
        n = len(t["private_models"]) if t else 0
        click.echo(f"  [{name}] {n} model(s)")


@lockbox.command("add")
@click.argument("model")
def lockbox_add(model):
    """Add a model to your personal allowlist."""
    status, body = _veil_call("POST", "/api/lockbox/me", json_body={"model": model})
    _print_result(status, body)
    click.echo(click.style(f"Added: {model}", fg="green"))


@lockbox.command("remove")
@click.argument("model")
def lockbox_remove(model):
    """Remove a model from your personal allowlist."""
    status, body = _veil_call("DELETE", "/api/lockbox/me", params={"model": model})
    _print_result(status, body)
    click.echo(click.style(f"Removed: {model}", fg="green"))


@lockbox.command("clear")
@click.confirmation_option(prompt="Clear your entire personal lockbox?")
def lockbox_clear():
    """Clear your entire personal lockbox tier."""
    status, body = _veil_call("DELETE", "/api/lockbox/me")
    _print_result(status, body)
    click.echo(click.style("Cleared.", fg="green"))


@lockbox.group("safety")
def lockbox_safety():
    """Manage your personal safety override."""


@lockbox_safety.command("set")
@click.argument("level", type=click.Choice(["OFF", "LOW", "MEDIUM", "HIGH"],
                                            case_sensitive=False))
def lockbox_safety_set(level):
    """Set your personal safety override."""
    # Fetch current to preserve models/notes
    status, body = _veil_call("GET", "/api/lockbox/me")
    body = _print_result(status, body)
    user_tier = ((body or {}).get("resolved", {}).get("tiers", {}) or {}).get("user") or {}
    payload = {
        "private_models": user_tier.get("private_models", []),
        "notes": user_tier.get("notes"),
        "safety_override": level.upper(),
    }
    status, body = _veil_call("PUT", "/api/lockbox/me", json_body=payload)
    _print_result(status, body)
    click.echo(click.style(f"Safety override set to {level.upper()}.", fg="green"))


@lockbox_safety.command("clear")
def lockbox_safety_clear():
    """Remove your personal safety override (inherit default policy)."""
    status, body = _veil_call("GET", "/api/lockbox/me")
    body = _print_result(status, body)
    user_tier = ((body or {}).get("resolved", {}).get("tiers", {}) or {}).get("user") or {}
    payload = {
        "private_models": user_tier.get("private_models", []),
        "notes": user_tier.get("notes"),
        "safety_override": None,
    }
    status, body = _veil_call("PUT", "/api/lockbox/me", json_body=payload)
    _print_result(status, body)
    click.echo(click.style("Personal safety override cleared.", fg="green"))


# ─── lockbox admin (tenant + platform tiers) ───────────────────────────────

@lockbox.group("admin")
def lockbox_admin():
    """Admin-only: manage tenant and platform tiers and other users.

    Requires admin authentication (caller must have the `admin` role).
    """


def _tier_path(tier, id_):
    return f"/api/admin/lockbox/{tier}/{id_}"


@lockbox_admin.command("show")
@click.argument("tier", type=click.Choice(["user", "workspace", "tenant", "platform"]))
@click.argument("id_", metavar="ID", required=False, default="default")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def lockbox_admin_show(tier, id_, as_json):
    """Show the stored config for any tier.

    For `platform`, ID is ignored (use `default`). For `user`, ID is the
    user email or user id. For `tenant`, ID is the tenant id.
    """
    if tier == "platform":
        id_ = "default"
    status, body = _veil_call("GET", _tier_path(tier, id_))
    body = _print_result(status, body, as_json=as_json)
    if as_json or body is None:
        return
    cfg = body.get("config") or {}
    click.echo(click.style(f"[{tier}/{id_}]", bold=True))
    click.echo(f"  safety: {cfg.get('safety_override') or '(none)'}")
    click.echo(f"  notes:  {cfg.get('notes') or ''}")
    models = cfg.get("private_models") or []
    click.echo(f"  models ({len(models)}):")
    for m in models:
        click.echo(f"    • {m}")
    if cfg.get("updated_at"):
        click.echo(f"  updated_at: {cfg['updated_at']} by {cfg.get('updated_by') or '?'}")


@lockbox_admin.command("set")
@click.argument("tier", type=click.Choice(["user", "workspace", "tenant", "platform"]))
@click.argument("id_", metavar="ID")
@click.option("--model", "models", multiple=True,
              help="Add a model (repeat to add many). Replaces existing list unless --append.")
@click.option("--append", is_flag=True, help="Merge with existing models instead of replacing.")
@click.option("--safety", type=click.Choice(["OFF", "LOW", "MEDIUM", "HIGH", "none"],
                                              case_sensitive=False),
              default=None, help="Set safety override (or 'none' to clear).")
@click.option("--notes", default=None, help="Free-form notes")
def lockbox_admin_set(tier, id_, models, append, safety, notes):
    """Set the stored config for any tier.

    \b
    aither lockbox admin set user alice@x.com --model a.safetensors --model b.safetensors
    aither lockbox admin set tenant acme --safety MEDIUM
    aither lockbox admin set platform default --append --model c.safetensors
    """
    if tier == "platform":
        id_ = "default"
    final_models = list(models)
    if append:
        status, body = _veil_call("GET", _tier_path(tier, id_))
        if status < 400 and isinstance(body, dict):
            existing = ((body.get("config") or {}).get("private_models")) or []
            seen = {m.lower() for m in existing}
            final_models = list(existing) + [m for m in models if m.lower() not in seen]

    safety_value = None
    if safety and safety.lower() != "none":
        safety_value = safety.upper()

    payload = {
        "private_models": final_models,
        "safety_override": safety_value,
        "notes": notes,
    }
    status, body = _veil_call("PUT", _tier_path(tier, id_), json_body=payload)
    _print_result(status, body)
    click.echo(click.style(f"[{tier}/{id_}] saved ({len(final_models)} model(s)).",
                            fg="green"))


@lockbox_admin.command("clear")
@click.argument("tier", type=click.Choice(["user", "workspace", "tenant", "platform"]))
@click.argument("id_", metavar="ID", required=False, default="default")
@click.confirmation_option(prompt="Clear this tier?")
def lockbox_admin_clear(tier, id_):
    """Clear a tier's stored config entirely."""
    if tier == "platform":
        id_ = "default"
    status, body = _veil_call("DELETE", _tier_path(tier, id_))
    _print_result(status, body)
    click.echo(click.style(f"[{tier}/{id_}] cleared.", fg="green"))


# ---------------------------------------------------------------------------
# `aither install` / `aither llm` — framework installer + local LLM helper
# ---------------------------------------------------------------------------

def _adk_module_proxy(module: str, args):
    """Invoke ``python -m aither_adk.integrations.<module>`` resiliently.

    Mirrors ``_agents_proxy`` but for the framework-installer / local-LLM
    helpers. Returns exit code via SystemExit so click reports it correctly.
    """
    import subprocess as _subprocess
    attempts = [
        [sys.executable, "-m", f"aither_adk.integrations.{module}", *args],
        [sys.executable, "-m", f"adk.integrations.{module}", *args],
    ]
    last_rc = 127
    for cmd in attempts:
        try:
            rc = _subprocess.call(cmd)
            if rc == 0 or rc == 2:
                raise SystemExit(rc)
            last_rc = rc
            if "-m" in cmd and rc in (1, 127):
                continue
            raise SystemExit(rc)
        except FileNotFoundError:
            continue
    click.echo(
        f"Unable to invoke aither_adk.integrations.{module}. "
        "Install with: pip install aither-platform",
        err=True,
    )
    sys.exit(last_rc)


@cli.group(invoke_without_command=True)
@click.pass_context
def install(ctx):
    """Install and deploy an agent framework (Hermes / OpenClaw).

    \b
    aither install hermes                 # portal-hosted inference (default)
    aither install hermes --local-llm     # point at local vLLM endpoint
    aither install openclaw --portal-inference
    aither install list
    aither install remove hermes --purge
    """
    if ctx.invoked_subcommand is None:
        _adk_module_proxy("framework_installer", ["--help"])


@install.command("hermes")
@click.option("--ref", default=None, help="Git ref (branch/tag/sha)")
@click.option("--repo", default=None, help="Override repo URL")
@click.option("--llm-backend",
              type=click.Choice(["portal", "local", "custom"]), default="portal")
@click.option("--llm-url", default=None,
              help="OpenAI-compatible base URL (required for local/custom)")
@click.option("--local-llm", is_flag=True,
              help="Use the local vLLM endpoint configured via `aither llm install`")
@click.option("--portal-inference", is_flag=True,
              help="Force api.aitherium.com inference (overrides --local-llm)")
@click.option("--model", default=None, help="Model name to wire into config")
@click.option("--force", is_flag=True, help="Reinstall (rm -rf the install dir first)")
@click.option("--no-onboard", is_flag=True, help="Skip the portal onboard step")
@click.option("--agent-name", default=None)
def install_hermes(ref, repo, llm_backend, llm_url, local_llm, portal_inference,
                   model, force, no_onboard, agent_name):
    """Install Nous Research Hermes Agent into ~/.aither/frameworks/hermes/."""
    args = ["install", "hermes"]
    if ref: args += ["--ref", ref]
    if repo: args += ["--repo", repo]
    if portal_inference:
        args += ["--llm-backend", "portal"]
    else:
        args += ["--llm-backend", llm_backend]
    if llm_url: args += ["--llm-url", llm_url]
    if local_llm and not portal_inference: args.append("--local-llm")
    if model: args += ["--model", model]
    if force: args.append("--force")
    if no_onboard: args.append("--no-onboard")
    if agent_name: args += ["--agent-name", agent_name]
    _adk_module_proxy("framework_installer", args)


@install.command("openclaw")
@click.option("--ref", default=None)
@click.option("--repo", default=None)
@click.option("--llm-backend",
              type=click.Choice(["portal", "local", "custom"]), default="portal")
@click.option("--llm-url", default=None)
@click.option("--local-llm", is_flag=True,
              help="Use the local vLLM endpoint configured via `aither llm install`")
@click.option("--portal-inference", is_flag=True)
@click.option("--model", default=None)
@click.option("--force", is_flag=True)
@click.option("--no-onboard", is_flag=True)
@click.option("--agent-name", default=None)
def install_openclaw(ref, repo, llm_backend, llm_url, local_llm, portal_inference,
                     model, force, no_onboard, agent_name):
    """Install OpenClaw OSS agent into ~/.aither/frameworks/openclaw/."""
    args = ["install", "openclaw"]
    if ref: args += ["--ref", ref]
    if repo: args += ["--repo", repo]
    if portal_inference:
        args += ["--llm-backend", "portal"]
    else:
        args += ["--llm-backend", llm_backend]
    if llm_url: args += ["--llm-url", llm_url]
    if local_llm and not portal_inference: args.append("--local-llm")
    if model: args += ["--model", model]
    if force: args.append("--force")
    if no_onboard: args.append("--no-onboard")
    if agent_name: args += ["--agent-name", agent_name]
    _adk_module_proxy("framework_installer", args)


@install.command("list")
def install_list():
    """List installed agent frameworks."""
    _adk_module_proxy("framework_installer", ["list"])


@install.command("remove")
@click.argument("framework", type=click.Choice(["hermes", "openclaw"]))
@click.option("--purge", is_flag=True, help="Actually delete on-disk state")
def install_remove(framework, purge):
    """Delete a framework install."""
    args = ["remove", framework]
    if purge:
        args.append("--purge")
    _adk_module_proxy("framework_installer", args)


@cli.group(invoke_without_command=True)
@click.pass_context
def llm(ctx):
    """Manage a local LLM server (vLLM / Ollama) for offline agent inference.

    \b
    aither llm detect                                # probe GPU/pip/ollama
    aither llm install --backend vllm --model X      # set up local vLLM
    aither llm install --backend ollama --model llama3.1:8b
    aither llm start                                  # background daemon
    aither llm status
    aither llm stop
    aither llm endpoint                               # print base URL
    """
    if ctx.invoked_subcommand is None:
        _adk_module_proxy("local_llm", ["--help"])


@llm.command("detect")
def llm_detect():
    """Probe the host for GPU / CUDA / pip / ollama prerequisites."""
    _adk_module_proxy("local_llm", ["detect"])


@llm.command("install")
@click.option("--backend", type=click.Choice(["vllm", "ollama"]), default="vllm")
@click.option("--model", default=None, help="Model name (HF id for vLLM, tag for Ollama)")
@click.option("--port", type=int, default=8080)
@click.option("--force", is_flag=True, help="Reinstall even if already configured")
def llm_install(backend, model, port, force):
    """Install vLLM (in a dedicated venv) or pull an Ollama model."""
    args = ["install", "--backend", backend, "--port", str(port)]
    if model:
        args += ["--model", model]
    if force:
        args.append("--force")
    _adk_module_proxy("local_llm", args)


@llm.command("start")
def llm_start():
    """Start the local LLM server in the background."""
    _adk_module_proxy("local_llm", ["start"])


@llm.command("status")
def llm_status():
    """Show installed config + running process status."""
    _adk_module_proxy("local_llm", ["status"])


@llm.command("stop")
def llm_stop():
    """Stop the local LLM server."""
    _adk_module_proxy("local_llm", ["stop"])


@llm.command("endpoint")
def llm_endpoint():
    """Print the OpenAI-compatible base URL of the local LLM."""
    _adk_module_proxy("local_llm", ["endpoint"])


# ─── aither install (.aitherpkg installer) ─────────────────────────────────


def _aitherpkg_open(pkg_path):
    """Open a .aitherpkg and return (zipfile, manifest_dict)."""
    import zipfile as _zf
    if not pkg_path.is_file():
        raise click.ClickException(f"Not found: {pkg_path}")
    try:
        zf = _zf.ZipFile(pkg_path, "r")
    except _zf.BadZipFile as exc:
        raise click.ClickException(f"Not a valid .aitherpkg: {exc}")
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except KeyError:
        zf.close()
        raise click.ClickException("Package missing manifest.json")
    return zf, manifest


def _aitherpkg_verify_digests(zf, manifest):
    """Re-hash every entry in file_digests; return list of (arcname, status)."""
    import hashlib as _hash
    issues = []
    for arcname, expected in (manifest.get("file_digests") or {}).items():
        try:
            actual = _hash.sha256(zf.read(arcname)).hexdigest()
        except KeyError:
            issues.append((arcname, "missing"))
            continue
        if actual != expected:
            issues.append((arcname, "mismatch"))
    return issues


def _aitherpkg_verify_signature(zf, manifest):
    """Verify signatures/manifest.sig over canonical manifest bytes.

    Returns one of: ``"unsigned"``, ``"ok"``, ``"bad"``, ``"no-crypto"``.
    """
    names = zf.namelist()
    if "signatures/manifest.sig" not in names:
        return "unsigned", None
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        return "no-crypto", None
    import base64 as _b64
    sig_blob = json.loads(zf.read("signatures/manifest.sig").decode("utf-8"))
    if sig_blob.get("alg") != "ed25519":
        return "bad", sig_blob
    try:
        sig = _b64.b64decode(sig_blob["signature_b64"])
        pub_raw = _b64.b64decode(sig_blob["public_key_b64"])
        pub = ed25519.Ed25519PublicKey.from_public_bytes(pub_raw)
        manifest_bytes = json.dumps(
            manifest, indent=2, sort_keys=True,
        ).encode("utf-8")
        pub.verify(sig, manifest_bytes)
        return "ok", sig_blob
    except (KeyError, ValueError, InvalidSignature):
        return "bad", sig_blob


def _resolve_install_dirs(scope):
    """Decide where to drop identity YAMLs and souls.

    * ``scope=workspace`` -> ``AitherOS/config/{identities,souls}`` if writable
    * ``scope=user``      -> ``~/.aither/{identities,souls}``
    """
    from pathlib import Path as _Path
    if scope == "workspace":
        # Walk up from cwd looking for the AitherOS config dir.
        cwd = _Path.cwd()
        for parent in (cwd, *cwd.parents):
            cand = parent / "AitherOS" / "config" / "identities"
            if cand.is_dir():
                return (
                    parent / "AitherOS" / "config" / "identities",
                    parent / "AitherOS" / "config" / "souls",
                )
        click.echo("[install] No AitherOS/config tree found; falling back to user scope.",
                   err=True)
    home = _Path.home() / ".aither"
    ident = home / "identities"
    soul = home / "souls"
    ident.mkdir(parents=True, exist_ok=True)
    soul.mkdir(parents=True, exist_ok=True)
    return ident, soul


def _shell_step(label, argv):
    """Run a subprocess step, echo stdout/stderr, return exit code."""
    import subprocess as _subprocess
    click.echo(f"[install] {label}: {' '.join(argv)}")
    res = _subprocess.run(argv, capture_output=True, text=True)
    if res.stdout:
        click.echo(res.stdout.rstrip())
    if res.returncode != 0:
        if res.stderr:
            click.echo(res.stderr.rstrip(), err=True)
        click.echo(f"[install] {label} exited {res.returncode}", err=True)
    return res.returncode


@cli.command("install")
@click.argument("package_path", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--scope", type=click.Choice(["workspace", "user"]), default="workspace",
              help="Install destination: workspace (AitherOS/config/) or user (~/.aither/).")
@click.option("--require-signed", is_flag=True,
              help="Refuse to install unsigned or bad-signature packages.")
@click.option("--skip-digests", is_flag=True,
              help="Skip per-file SHA-256 verification (not recommended).")
@click.option("--skip-keygen", is_flag=True,
              help="Don't auto-generate Ed25519 keypairs.")
@click.option("--skip-portal", is_flag=True,
              help="Don't sync new agents up to api.aitherium.com.")
@click.option("--license-key", default=None, envvar="AITHER_LICENSE_KEY",
              help="License key to bind to the install (writes license.slot).")
@click.option("--yes", is_flag=True, help="Don't prompt for confirmation.")
@click.option("--dry-run", is_flag=True, help="Verify + plan only, don't write anything.")
def install(package_path, scope, require_signed, skip_digests, skip_keygen,
            skip_portal, license_key, yes, dry_run):
    """Install a downloaded ``.aitherpkg`` (agent / bundle / tool-pack).

    \b
    aither install ./agent_demiurge-1.0.0.aitherpkg
    aither install bundle_seven-sins-1.0.0.aitherpkg --scope user
    aither install x.aitherpkg --require-signed --license-key xyz
    aither install x.aitherpkg --dry-run

    The installer:
    1. Verifies the signature (if present) and file digests.
    2. Extracts identity YAMLs and souls into the chosen scope.
    3. Generates Ed25519 keypairs (unless ``--skip-keygen``).
    4. Upserts each identity to ``api.aitherium.com`` (unless ``--skip-portal``).
    """
    from pathlib import Path as _Path

    pkg = _Path(package_path)
    zf, manifest = _aitherpkg_open(pkg)
    try:
        click.echo(f"[install] {manifest.get('name')} "
                    f"({manifest.get('listing_id')}) v{manifest.get('version')}")
        click.echo(f"[install] Identities: {', '.join(manifest.get('identities', []))}")
        click.echo(f"[install] Kind: {manifest.get('kind')}")

        # Signature
        sig_status, sig_blob = _aitherpkg_verify_signature(zf, manifest)
        if sig_status == "ok":
            click.echo(f"[install] Signature: OK ({sig_blob.get('key_name')})")
        elif sig_status == "unsigned":
            click.echo("[install] Signature: UNSIGNED")
            if require_signed:
                raise click.ClickException("Package is unsigned and --require-signed was set.")
        elif sig_status == "bad":
            click.echo("[install] Signature: BAD", err=True)
            if require_signed:
                raise click.ClickException("Bad signature; refusing to install.")
        elif sig_status == "no-crypto":
            click.echo("[install] Signature: cannot verify (cryptography not installed)",
                       err=True)
            if require_signed:
                raise click.ClickException("--require-signed needs the 'cryptography' package.")

        # Digests
        if not skip_digests:
            issues = _aitherpkg_verify_digests(zf, manifest)
            if issues:
                for arc, status in issues[:10]:
                    click.echo(f"[install] digest {status}: {arc}", err=True)
                raise click.ClickException(
                    f"{len(issues)} digest issue(s) — package is corrupt or tampered.",
                )
            click.echo(f"[install] Digests: {len(manifest.get('file_digests', {}))} OK")

        # Plan
        identities_dir, souls_dir = _resolve_install_dirs(scope)
        click.echo(f"[install] Target identities dir: {identities_dir}")
        click.echo(f"[install] Target souls dir:      {souls_dir}")

        identity_names = manifest.get("identities", []) or []
        if not yes and not dry_run:
            if not click.confirm("Proceed with install?", default=True):
                click.echo("Aborted.")
                return

        if dry_run:
            click.echo("[install] Dry run — no files written.")
            return

        # Extract identity + soul files
        written = []
        for name in identity_names:
            ident_arc = f"identity/{name}.yaml"
            if ident_arc in zf.namelist():
                dest = identities_dir / f"{name}.yaml"
                dest.write_bytes(zf.read(ident_arc))
                written.append(dest)
            soul_arc = f"soul/{name}.md"
            if soul_arc in zf.namelist():
                dest = souls_dir / f"{name}.md"
                dest.write_bytes(zf.read(soul_arc))
                written.append(dest)
        click.echo(f"[install] Wrote {len(written)} file(s)")

        # License binding
        if license_key:
            slot_dir = _Path.home() / ".aither" / "licenses"
            slot_dir.mkdir(parents=True, exist_ok=True)
            slot = slot_dir / f"{manifest.get('listing_id', 'unknown').replace('.', '_')}.key"
            slot.write_text(license_key, encoding="utf-8")
            try:
                slot.chmod(0o600)
            except OSError:
                pass
            click.echo(f"[install] License key bound: {slot}")

        # Keygen + portal sync per identity
        for name in identity_names:
            if not skip_keygen:
                _shell_step(f"keygen {name}",
                            [sys.executable, "-m", "aither_adk.cli",
                             "agent", "keygen", name])
            if not skip_portal:
                _shell_step(f"portal upsert {name}",
                            [sys.executable, "-m", "aither_adk.cli",
                             "agent", "integrate", "--only", "portal",
                             "--identity", name])

        click.echo("[install] Done. View in Veil → Fleet tab "
                    "or run `aither agent fleet`.")
    finally:
        zf.close()


@cli.command("uninstall-agent")
@click.argument("identity_name")
@click.option("--scope", type=click.Choice(["workspace", "user"]), default="workspace")
@click.option("--keep-portal", is_flag=True,
              help="Don't remove the identity from api.aitherium.com.")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def uninstall_agent(identity_name, scope, keep_portal, yes):
    """Remove an installed identity (and optionally unregister from portal)."""
    from pathlib import Path as _Path

    identities_dir, souls_dir = _resolve_install_dirs(scope)
    ident_path = identities_dir / f"{identity_name}.yaml"
    soul_path = souls_dir / f"{identity_name}.md"
    if not ident_path.is_file():
        raise click.ClickException(f"No installed identity '{identity_name}' at {ident_path}")
    if not yes:
        if not click.confirm(f"Remove identity '{identity_name}' from {scope}?",
                              default=False):
            click.echo("Aborted.")
            return
    ident_path.unlink()
    click.echo(f"[uninstall] removed {ident_path}")
    if soul_path.is_file():
        soul_path.unlink()
        click.echo(f"[uninstall] removed {soul_path}")
    if not keep_portal:
        _shell_step(f"portal unregister {identity_name}",
                    [sys.executable, "-m", "aither_adk.cli",
                     "agent", "unregister", identity_name])


@cli.command("inspect-package")
@click.argument("package_path", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--verify", is_flag=True, help="Verify digests + signature.")
@click.option("--json", "as_json", is_flag=True, help="Output the raw manifest as JSON.")
def inspect_package(package_path, verify, as_json):
    """Inspect a ``.aitherpkg`` without installing it."""
    from pathlib import Path as _Path
    zf, manifest = _aitherpkg_open(_Path(package_path))
    try:
        if as_json:
            click.echo(json.dumps(manifest, indent=2))
            return
        click.echo(f"Listing:    {manifest.get('listing_id')}")
        click.echo(f"Name:       {manifest.get('name')}")
        click.echo(f"Kind:       {manifest.get('kind')}")
        click.echo(f"Version:    {manifest.get('version')}")
        click.echo(f"Built:      {manifest.get('built_at')}")
        click.echo(f"Identities: {', '.join(manifest.get('identities', []))}")
        click.echo(f"Pricing:    {json.dumps(manifest.get('pricing', {}))}")
        click.echo(f"Delivery:   {json.dumps(manifest.get('delivery', {}))}")
        click.echo(f"Tags:       {', '.join(manifest.get('tags', []))}")
        if verify:
            sig_status, sig_blob = _aitherpkg_verify_signature(zf, manifest)
            click.echo(f"Signature:  {sig_status}"
                        + (f" ({sig_blob.get('key_name')})" if sig_blob else ""))
            issues = _aitherpkg_verify_digests(zf, manifest)
            if issues:
                for arc, status in issues[:10]:
                    click.echo(f"  digest {status}: {arc}")
                click.echo(f"Digests:    {len(issues)} issue(s)")
            else:
                click.echo(f"Digests:    {len(manifest.get('file_digests', {}))} OK")
    finally:
        zf.close()


# ---------------------------------------------------------------------------
# Pack management — aither pack list/install/remove/info
# ---------------------------------------------------------------------------

@cli.group()
def pack():
    """Manage ToolPack extensions — list, search, install, remove, inspect.

    \b
    aither pack list                    # Show available and installed packs
    aither pack search <query>          # Search packs by name, description, tags
    aither pack install <pack_id>       # Download and install a pack
    aither pack remove <pack_id>        # Remove an installed pack
    aither pack info <pack_id>          # Show pack details
    """
    pass


@pack.command("list")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def pack_list(output_json):
    """List available and installed tool packs."""
    import httpx

    packs_dir = Path.home() / ".aitheros" / "packs"
    local_packs = set()
    if packs_dir.is_dir():
        for child in packs_dir.iterdir():
            if child.is_dir() and (child / ".toolpack.yaml").exists():
                local_packs.add(child.name)

    # Try to fetch remote catalog
    catalog = []
    try:
        with httpx.Client(base_url=_genesis_url(), timeout=10) as c:
            resp = c.get("/v1/packs/catalog")
            if resp.status_code == 200:
                catalog = resp.json().get("packs", [])
    except Exception:
        pass

    # Merge local-only packs not in catalog
    catalog_ids = {p["id"] for p in catalog}
    for local_id in sorted(local_packs - catalog_ids):
        catalog.append({
            "id": local_id,
            "name": local_id,
            "version": "local",
            "pricing": {},
            "installed": True,
        })

    # Enrich with local install status
    for entry in catalog:
        if entry["id"] in local_packs:
            entry["installed"] = True

    # Check entitlement for licensed status
    licensed = set()
    try:
        from adk.shell.entitlement import load_cached
        ent = load_cached()
        if ent and ent.licensed_packs:
            licensed = set(ent.licensed_packs)
    except (ImportError, OSError, ValueError):
        pass

    if output_json:
        print(json.dumps(catalog, indent=2))
        return

    # Table output
    click.echo(f"{'Pack':<25} {'Version':<10} {'Status':<14} {'Price'}")
    click.echo("-" * 65)
    for p in catalog:
        pid = p.get("id", "?")
        ver = p.get("version", "?")
        pricing = p.get("pricing", {})
        is_free = not pricing
        installed = p.get("installed", False)

        if installed and (is_free or pid in licensed):
            status = click.style("installed", fg="green")
        elif pid in licensed:
            status = click.style("licensed", fg="cyan")
        elif installed:
            status = click.style("installed", fg="yellow")
        else:
            status = "available"

        if is_free:
            price = "free"
        else:
            cents = pricing.get("subscription_cents", 0)
            price = f"${int(cents) / 100:.0f}/mo" if cents else "paid"

        click.echo(f"{pid:<25} {ver:<10} {status:<14} {price}")


@pack.command("search")
@click.argument("query")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def pack_search(query, output_json):
    """Search available tool packs by name, description, or tags."""
    import httpx

    catalog = []
    try:
        with httpx.Client(base_url=_genesis_url(), timeout=10) as c:
            resp = c.get("/v1/packs/catalog")
            if resp.status_code == 200:
                catalog = resp.json().get("packs", [])
    except Exception as e:
        if output_json:
            print(json.dumps({"error": f"Cannot reach Genesis: {e}", "packs": []}))
        else:
            click.echo(f"Cannot reach Genesis: {e}", err=True)
        sys.exit(1)

    q = query.lower()
    matches = [
        p for p in catalog
        if q in p.get("name", "").lower()
        or q in p.get("description", "").lower()
        or q in p.get("id", "").lower()
        or any(q in tag.lower() for tag in p.get("tags", []))
    ]

    # Enrich with local install status
    packs_dir = Path.home() / ".aitheros" / "packs"
    local_packs = set()
    if packs_dir.is_dir():
        for child in packs_dir.iterdir():
            if child.is_dir() and (child / ".toolpack.yaml").exists():
                local_packs.add(child.name)
    for entry in matches:
        if entry["id"] in local_packs:
            entry["installed"] = True

    # Check entitlement for licensed status
    licensed = set()
    try:
        from adk.shell.entitlement import load_cached
        ent = load_cached()
        if ent and ent.licensed_packs:
            licensed = set(ent.licensed_packs)
    except (ImportError, OSError, ValueError):
        pass

    if output_json:
        print(json.dumps({"query": query, "count": len(matches), "packs": matches}, indent=2))
        return

    if not matches:
        click.echo(f"No packs matching '{query}'")
        return

    click.echo(f"{'Pack':<25} {'Version':<10} {'Status':<14} {'Price'}")
    click.echo("-" * 65)
    for p in matches:
        pid = p.get("id", "?")
        ver = p.get("version", "?")
        pricing = p.get("pricing", {})
        is_free = not pricing
        installed = p.get("installed", False)

        if installed and (is_free or pid in licensed):
            status = click.style("installed", fg="green")
        elif pid in licensed:
            status = click.style("licensed", fg="cyan")
        elif installed:
            status = click.style("installed", fg="yellow")
        else:
            status = "available"

        if is_free:
            price = "free"
        else:
            cents = pricing.get("subscription_cents", 0)
            price = f"${int(cents) / 100:.0f}/mo" if cents else "paid"

        click.echo(f"{pid:<25} {ver:<10} {status:<14} {price}")

    click.echo(f"\n{len(matches)} pack(s) matching '{query}'")


@pack.command("install")
@click.argument("pack_id")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def pack_install(pack_id, output_json):
    """Download and install a tool pack."""
    import httpx

    packs_dir = Path.home() / ".aitheros" / "packs"
    target = packs_dir / pack_id

    # Check entitlement
    try:
        from adk.shell.entitlement import load_cached
        ent = load_cached()
        if ent and not ent.has_pack_license(pack_id):
            # Check if it's a free pack by fetching manifest
            pass  # License check happens server-side on download
    except (ImportError, OSError, ValueError):
        pass

    # Download
    try:
        from adk.shell.auth import AuthStore
        profile = AuthStore.get_active_profile() or {}
        token = profile.get("access_token", "")
    except (ImportError, OSError):
        token = ""

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        with httpx.Client(base_url=_genesis_url(), timeout=60) as c:
            # POST /v1/packs/download/{id} is the endpoint Genesis has always
            # served; the GET form only exists on Genesis >= 2026-07-30 (added
            # as an alias for older ADK clients). Prefer POST, fall back to GET
            # for sovereign nodes running something older than either.
            resp = c.post(f"/v1/packs/download/{pack_id}", headers=headers)
            if resp.status_code in (404, 405):
                resp = c.get(f"/v1/packs/{pack_id}/download", headers=headers)

        if resp.status_code == 402:
            msg = resp.json().get("detail", "License required")
            if output_json:
                print(json.dumps({"ok": False, "error": msg, "pack_id": pack_id}))
            else:
                click.echo(f"License required: {msg}", err=True)
                click.echo("Purchase at: https://api.aitherium.com/marketplace", err=True)
            sys.exit(2)

        if resp.status_code == 404:
            msg = f"Pack '{pack_id}' not found"
            if output_json:
                print(json.dumps({"ok": False, "error": msg}))
            else:
                click.echo(msg, err=True)
            sys.exit(1)

        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        msg = f"Download failed: {e}"
        if output_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            click.echo(msg, err=True)
        sys.exit(1)
    except Exception as e:
        msg = f"Cannot reach Genesis: {e}"
        if output_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            click.echo(msg, err=True)
        sys.exit(1)

    # Verify SHA256 if provided
    expected_sha = resp.headers.get("X-Pack-SHA256", "")
    import hashlib
    actual_sha = hashlib.sha256(resp.content).hexdigest()
    if expected_sha and actual_sha != expected_sha:
        msg = f"SHA256 mismatch: expected {expected_sha[:16]}..., got {actual_sha[:16]}..."
        if output_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            click.echo(msg, err=True)
        sys.exit(1)

    # Extract
    import tarfile
    from io import BytesIO
    import shutil

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    buf = BytesIO(resp.content)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        # Security: reject path traversal
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                msg = f"Unsafe path in archive: {member.name}"
                if output_json:
                    print(json.dumps({"ok": False, "error": msg}))
                else:
                    click.echo(msg, err=True)
                sys.exit(1)
        tar.extractall(target, filter="data")

    version = resp.headers.get("X-Pack-Version", "unknown")

    # Trigger MCP reload so new tools appear without manual restart
    mcp_reloaded = False
    try:
        httpx.post(f"{_genesis_url()}/mcp/reload", timeout=5)
        mcp_reloaded = True
    except Exception:
        pass  # Non-fatal — user can restart manually

    if output_json:
        print(json.dumps({
            "ok": True,
            "pack_id": pack_id,
            "version": version,
            "path": str(target),
            "sha256": actual_sha,
            "mcp_reloaded": mcp_reloaded,
        }))
    else:
        click.echo(f"Pack '{pack_id}' v{version} installed to {target}")
        if mcp_reloaded:
            click.echo("MCP tools reloaded — new tools are available now.")
        else:
            click.echo("Restart `aither mcp serve` to activate.")


@pack.command("remove")
@click.argument("pack_id")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def pack_remove(pack_id, output_json):
    """Remove an installed tool pack."""
    import shutil

    target = Path.home() / ".aitheros" / "packs" / pack_id
    if not target.is_dir():
        msg = f"Pack '{pack_id}' is not installed"
        if output_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            click.echo(msg, err=True)
        sys.exit(1)

    shutil.rmtree(target)

    # Trigger MCP reload so removed tools disappear without manual restart
    mcp_reloaded = False
    try:
        import httpx
        httpx.post(f"{_genesis_url()}/mcp/reload", timeout=5)
        mcp_reloaded = True
    except Exception:
        pass  # Non-fatal — user can restart manually

    if output_json:
        print(json.dumps({"ok": True, "pack_id": pack_id, "removed": True, "mcp_reloaded": mcp_reloaded}))
    else:
        if mcp_reloaded:
            click.echo(f"Pack '{pack_id}' removed. MCP tools reloaded.")
        else:
            click.echo(f"Pack '{pack_id}' removed. Restart `aither mcp serve` to take effect.")


@pack.command("info")
@click.argument("pack_id")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def pack_info(pack_id, output_json):
    """Show details about a tool pack."""
    import httpx

    try:
        with httpx.Client(base_url=_genesis_url(), timeout=10) as c:
            resp = c.get(f"/v1/packs/{pack_id}/manifest")
        if resp.status_code == 404:
            msg = f"Pack '{pack_id}' not found"
            if output_json:
                print(json.dumps({"ok": False, "error": msg}))
            else:
                click.echo(msg, err=True)
            sys.exit(1)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        msg = f"Cannot fetch pack info: {e}"
        if output_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            click.echo(msg, err=True)
        sys.exit(1)

    if output_json:
        print(json.dumps(data, indent=2))
        return

    click.echo(f"Pack:        {data.get('name', pack_id)}")
    click.echo(f"ID:          {data.get('id', pack_id)}")
    click.echo(f"Version:     {data.get('version', '?')}")
    click.echo(f"Author:      {data.get('author', 'unknown')}")
    click.echo(f"Category:    {data.get('category', '?')}")
    click.echo(f"Description: {data.get('description', '')}")

    pricing = data.get("pricing", {})
    if pricing:
        cents = pricing.get("subscription_cents", 0)
        click.echo(f"Price:       ${int(cents) / 100:.0f}/mo" if cents else "Price:       paid")
    else:
        click.echo("Price:       free")

    skills = data.get("skills", [])
    if skills:
        click.echo(f"Skills:      {', '.join(skills)}")

    directives = data.get("persona_fragments", [])
    if directives:
        click.echo(f"Directives:  {', '.join(directives)}")

    tools = data.get("tools", [])
    if tools:
        click.echo(f"\nTools ({len(tools)}):")
        for t in tools:
            click.echo(f"  - {t['name']}: {t.get('description', '')[:60]}")

    dist = data.get("distribution", {})
    if dist:
        click.echo(f"\nMin Node:    {dist.get('min_node_version', 'any')}")


def entry():
    """Main entry point for `aither` console_scripts."""
    # Non-blocking update check (once per day, cached)
    try:
        from adk.shell.update_check import print_update_notice
        print_update_notice()
    except ImportError:
        pass

    # License entitlement gate (logs tier, wires metering quotas; never blocks unless
    # AITHER_LICENSE_REQUIRE=1). The free tier stays genuinely useful.
    try:
        from adk.license_startup import gate_startup
        gate_startup(product="shell")
    except Exception as _lic_exc:  # gate must never crash the shell
        logger.debug(f"license gate skipped: {_lic_exc}")

    try:
        cli()
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)
    except CommandError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


def _find_cli_module() -> str:
    """Find aither_cli.py in the repo."""
    # Check env var
    root = os.environ.get("AITHEROS_ROOT")
    if root:
        candidate = os.path.join(root, "AitherOS", "aither_cli.py")
        if os.path.exists(candidate):
            return candidate

    # Check common locations relative to this file
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        # Installed in repo: aithershell/ is sibling of AitherOS/
        os.path.join(here, "..", "..", "AitherOS", "aither_cli.py"),
        # Dev mode
        os.path.join(here, "..", "..", "..", "AitherOS", "aither_cli.py"),
        # CWD
        os.path.join(os.getcwd(), "AitherOS", "aither_cli.py"),
        os.path.join(os.getcwd(), "aither_cli.py"),
    ]

    for c in candidates:
        c = os.path.normpath(c)
        if os.path.exists(c):
            return c

    return ""


def _init_shell():
    """Initialize AitherShell config and show setup instructions."""
    from adk.shell.config import save_default_config, CONFIG_DIR, CONFIG_FILE, PLUGINS_DIR

    save_default_config()

    print(f"""
AitherShell initialized!

Config:   {CONFIG_FILE}
Plugins:  {PLUGINS_DIR}

Shell completions (pick your shell):
  bash:  eval "$(register-python-argcomplete aither)"
  zsh:   eval "$(register-python-argcomplete aither)"
  fish:  register-python-argcomplete --shell fish aither | source
  pwsh:  aither --completions powershell >> $PROFILE

Quick start:
  aither                          # Interactive shell
  aither "hello"                  # Single query
  aither --print "question"       # Script mode
  aither --json "question"        # JSON output
  aither --private                # Private mode
  echo "prompt" | aither --print  # Pipe input

Config: edit {CONFIG_FILE}
Plugins: drop .yaml or .py files in {PLUGINS_DIR}
""")


# ─── aither apps (workspace app management) ────────────────────────────────

@cli.group()
def apps():
    """Manage workspace apps — install, list, set primary, bootstrap.

    \b
    aither apps catalog                        # List available apps
    aither apps list                           # List deployed apps
    aither apps install myapp                  # Install an app
    aither apps primary myapp                  # Set primary app for subdomain
    aither apps bootstrap --tenant customer-1 app-1 app-2   # Seed apps
    """
    pass


@apps.command("catalog")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def apps_catalog(output_json):
    """List all available app manifests from the catalog."""
    import httpx

    with httpx.Client(base_url=_genesis_url(), timeout=15) as c:
        resp = c.get("/apps/catalog")
        resp.raise_for_status()
        data = resp.json()

    if output_json:
        click.echo(json.dumps(data, indent=2))
        return

    if not data:
        click.echo("No apps in catalog.")
        return

    click.echo(f"{'Slug':<20} {'Name':<30} {'Mode':<12} {'Plan':<10}")
    click.echo("-" * 72)
    for app in data:
        click.echo(
            f"{app['slug']:<20} {app['name']:<30} "
            f"{app.get('deployment_mode', 'dedicated'):<12} "
            f"{app.get('requires_plan', 'free'):<10}"
        )


@apps.command("list")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def apps_list(output_json):
    """List deployed apps in the current tenant workspace."""
    import httpx

    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=15) as c:
        resp = c.get("/apps/deployments")
        resp.raise_for_status()
        data = resp.json()

    if output_json:
        click.echo(json.dumps(data, indent=2))
        return

    if not data:
        click.echo("No apps deployed. Run `aither apps install <slug>` to deploy one.")
        return

    click.echo(f"{'Slug':<20} {'Status':<14} {'Mode':<12} {'Primary':<8}")
    click.echo("-" * 54)
    for d in data:
        primary = "*" if d.get("is_primary") else ""
        click.echo(
            f"{d['slug']:<20} {d['status']:<14} "
            f"{d.get('deployment_mode', 'dedicated'):<12} "
            f"{primary:<8}"
        )


@apps.command("install")
@click.argument("slug")
@click.option("--primary", is_flag=True, help="Set as primary app after install")
def apps_install(slug, primary):
    """Install an app to the current workspace.

    \b
    aither apps install myapp
    aither apps install another-app --primary
    """
    import httpx

    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=30) as c:
        resp = c.post("/apps/deploy", json={"slug": slug})
        if resp.status_code == 404:
            click.echo(f"App '{slug}' not found. Run `aither apps catalog` to see available apps.", err=True)
            sys.exit(1)
        resp.raise_for_status()
        data = resp.json()
        click.echo(f"Deployed {slug}: {data.get('status', 'unknown')} (id: {data.get('deployment_id')})")

        if primary:
            resp2 = c.put("/apps/tenant/primary", json={"slug": slug})
            if resp2.status_code < 300:
                click.echo(f"Set {slug} as primary app.")
            else:
                click.echo(f"Warning: could not set as primary: {resp2.text}", err=True)


@apps.command("primary")
@click.argument("slug")
def apps_set_primary(slug):
    """Set an app as the primary app for tenant subdomain routing.

    \b
    aither apps primary myapp
    # Now myapp.<domain> routes to /myapp
    """
    import httpx

    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=15) as c:
        resp = c.put("/apps/tenant/primary", json={"slug": slug})
        if resp.status_code == 404:
            click.echo(f"No active deployment of '{slug}'. Install it first: `aither apps install {slug}`", err=True)
            sys.exit(1)
        resp.raise_for_status()
        click.echo(f"{slug} is now the primary app.")


@apps.command("bootstrap")
@click.argument("app_slugs", nargs=-1, required=True)
@click.option("--tenant", required=True, help="Tenant slug")
@click.option("--tenant-id", default=None, help="Tenant ID (defaults to slug)")
@click.option("--primary", default=None, help="Slug to mark as primary")
def apps_bootstrap(app_slugs, tenant, tenant_id, primary):
    """Seed multiple apps to a tenant workspace in one call.

    \b
    aither apps bootstrap --tenant customer-1 --primary app-1 app-1 app-2
    aither apps bootstrap --tenant customer-2 app-1 app-2 app-3
    """
    import httpx

    body = {
        "tenant_id": tenant_id or tenant,
        "tenant_slug": tenant,
        "apps": list(app_slugs),
        "primary_slug": primary,
    }

    with httpx.Client(base_url=_genesis_url(), timeout=30) as c:
        resp = c.post("/apps/bootstrap-tenant", json=body)
        resp.raise_for_status()
        data = resp.json()

    click.echo(f"Bootstrapped apps for tenant '{tenant}':")
    for r in data.get("results", []):
        primary_mark = " [PRIMARY]" if r.get("is_primary") else ""
        click.echo(f"  {r['slug']}: {r['status']}{primary_mark}")


@apps.command("upgrade")
@click.argument("deployment_id")
@click.option("--version", "-v", "version", default=None, help="Target version (default: latest)")
@click.option("--force", is_flag=True, help="Force upgrade even if same version")
def apps_upgrade(deployment_id, version, force):
    """Upgrade a deployed app to a new version.

    \b
    aither apps upgrade app-abc123def456
    aither apps upgrade app-abc123def456 --version 1.2.0
    aither apps upgrade app-abc123def456 --force
    """
    import httpx

    body = {}
    if version:
        body["version"] = version
    if force:
        body["force"] = True

    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=30) as c:
        resp = c.post(f"/apps/deployments/{deployment_id}/upgrade", json=body)
        if resp.status_code == 409:
            click.echo(f"Cannot upgrade: {resp.json().get('detail', 'conflict')}", err=True)
            sys.exit(1)
        resp.raise_for_status()
        data = resp.json()

    status = data.get("status", "unknown")
    if status == "already_current":
        click.echo(f"Already at version {data.get('version')}. Use --force to re-deploy.")
    else:
        click.echo(f"Upgrading {deployment_id}: {data.get('from_version')} -> {data.get('to_version')}")


@apps.command("doctor")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def apps_doctor(output_json):
    """Health check all deployed apps.

    \b
    aither apps doctor
    aither apps doctor --json
    """
    import httpx

    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=15) as c:
        resp = c.get("/apps/deployments")
        resp.raise_for_status()
        deployments = resp.json()

    if not deployments:
        click.echo("No apps deployed.")
        return

    results = []
    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=10) as c:
        for d in deployments:
            did = d.get("deployment_id", "")
            if d.get("status") in ("destroyed", "failed"):
                results.append({"slug": d["slug"], "id": did, "healthy": False, "reason": d["status"]})
                continue
            try:
                resp = c.get(f"/apps/deployments/{did}/health")
                if resp.status_code == 200:
                    results.append(resp.json())
                else:
                    results.append({"slug": d["slug"], "id": did, "healthy": False, "reason": f"HTTP {resp.status_code}"})
            except Exception as e:
                results.append({"slug": d["slug"], "id": did, "healthy": False, "reason": str(e)})

    if output_json:
        click.echo(json.dumps(results, indent=2))
        return

    click.echo(f"{'Slug':<20} {'ID':<20} {'Healthy':<10} {'Detail'}")
    click.echo("-" * 70)
    for r in results:
        slug = r.get("slug", r.get("deployment_id", "?")[:16])
        did = r.get("id", r.get("deployment_id", ""))[:16]
        healthy = "OK" if r.get("healthy") else "FAIL"
        detail = r.get("reason", r.get("endpoint", ""))
        click.echo(f"{slug:<20} {did:<20} {healthy:<10} {detail}")


@apps.command("export")
@click.argument("slug")
@click.option("--output", "-o", "output_path", default=None, help="Output tar.gz path")
def apps_export(slug, output_path):
    """Export an app image for offline/air-gapped installation.

    \b
    aither apps export node
    aither apps export connect -o /tmp/aither-connect.tar.gz
    """
    import subprocess

    # Resolve image name from catalog
    import httpx
    with httpx.Client(base_url=_genesis_url(), timeout=10) as c:
        resp = c.get(f"/apps/catalog/{slug}")
        if resp.status_code == 404:
            click.echo(f"App '{slug}' not in catalog.", err=True)
            sys.exit(1)
        resp.raise_for_status()
        manifest = resp.json()

    image = manifest.get("image", f"aitherium/aither-{slug}:latest")
    version = manifest.get("version", "latest")
    out = output_path or f"aither-{slug}-{version}.tar.gz"

    click.echo(f"Exporting {image} -> {out}")

    try:
        # Pull first
        subprocess.run(["docker", "pull", image], check=True)
        # Save
        subprocess.run(["docker", "save", image, "-o", out], check=True)
        click.echo(f"Exported: {out} ({os.path.getsize(out) / 1024 / 1024:.1f} MB)")
        click.echo(f"To import: docker load -i {out}")
    except subprocess.CalledProcessError as e:
        click.echo(f"Docker export failed: {e}", err=True)
        sys.exit(1)
    except FileNotFoundError:
        click.echo("docker not found on PATH.", err=True)
        sys.exit(1)


@apps.command("uninstall")
@click.argument("deployment_id")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def apps_uninstall(deployment_id, yes):
    """Remove an app deployment.

    \b
    aither apps uninstall app-abc123def456
    """
    import httpx

    if not yes:
        click.confirm(f"Destroy deployment {deployment_id}?", abort=True)

    with httpx.Client(base_url=_genesis_url(), headers=_portal_headers(), timeout=15) as c:
        resp = c.delete(f"/apps/deployments/{deployment_id}")
        resp.raise_for_status()
        click.echo(f"Deployment {deployment_id} is being destroyed.")


# ─── aither listen (real-time audio intelligence) ─────────────────────────

@cli.group(invoke_without_command=True)
@click.pass_context
def listen(ctx):
    """Real-time audio intelligence — audiobook companion, meeting notes, voice memos.

    \b
    aither listen audiobook "Defiance of the Fall"     # Track stats as you listen
    aither listen meeting "Sprint Planning"            # Meeting transcription + notes
    aither listen note                                 # Quick voice note
    aither listen lecture "CS101"                      # Lecture transcription
    aither listen sessions                             # List active sessions
    aither listen stop SESSION_ID                      # Stop a session
    aither listen export SESSION_ID                    # Export notes as markdown
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@listen.command("audiobook")
@click.argument("title", default="")
@click.option("--author", default="", help="Book author")
@click.option("--genre", default="litrpg",
              type=click.Choice(["litrpg", "fantasy", "scifi", "general"]))
@click.option("--backend", default="wasapi",
              type=click.Choice(["wasapi", "pulse", "sounddevice", "file"]))
@click.option("--file", "audio_file", default=None, help="Audio file path (for file backend)")
@click.option("--workspace", default=None, help="Auto-save to workspace ID")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def listen_audiobook(title, author, genre, backend, audio_file, workspace, output_json):
    """Start audiobook companion — real-time character/stat tracking."""
    import httpx

    body = {
        "mode": "audiobook",
        "book_title": title,
        "author": author,
        "genre": genre,
        "capture_backend": backend,
        "audio_source": audio_file,
        "workspace_id": workspace,
    }

    with httpx.Client(base_url=_genesis_url(), timeout=30) as c:
        resp = c.post("/audiobook/start", json=body)
        resp.raise_for_status()
        data = resp.json()

    session_id = data.get("session_id", "")

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo(f"Audiobook companion started: {session_id[:12]}")
        click.echo(f"  Book:  {title or 'Untitled'}")
        click.echo(f"  Genre: {genre}")
        click.echo(f"  Audio: {backend}")
        click.echo(f"\n  View live: http://localhost:3000/portal/audiobook?session={session_id}")
        click.echo(f"  Stop:      aither listen stop {session_id[:12]}")


@listen.command("meeting")
@click.argument("title", default="")
@click.option("--type", "meeting_type", default="meeting",
              type=click.Choice(["meeting", "lecture", "interview", "brainstorm"]))
@click.option("--participants", "-p", multiple=True, help="Participant names")
@click.option("--backend", default="wasapi",
              type=click.Choice(["wasapi", "pulse", "sounddevice"]))
@click.option("--workspace", default=None, help="Auto-save to workspace ID")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def listen_meeting(title, meeting_type, participants, backend, workspace, output_json):
    """Start meeting transcription — action items, decisions, key points."""
    import httpx

    body = {
        "mode": "meeting",
        "meeting_title": title,
        "meeting_type": meeting_type,
        "participants": list(participants),
        "capture_backend": backend,
        "workspace_id": workspace,
    }

    with httpx.Client(base_url=_genesis_url(), timeout=30) as c:
        resp = c.post("/audiobook/start", json=body)
        resp.raise_for_status()
        data = resp.json()

    session_id = data.get("session_id", "")

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo(f"Meeting transcription started: {session_id[:12]}")
        click.echo(f"  Title: {title or 'Untitled Meeting'}")
        click.echo(f"  Type:  {meeting_type}")
        if participants:
            click.echo(f"  Participants: {', '.join(participants)}")
        click.echo(f"\n  View live: http://localhost:3000/portal/audiobook?session={session_id}")
        click.echo(f"  Stop:      aither listen stop {session_id[:12]}")


@listen.command("note")
@click.argument("title", default="Voice Note")
@click.option("--backend", default="wasapi",
              type=click.Choice(["wasapi", "pulse", "sounddevice"]))
@click.option("--workspace", default=None, help="Auto-save to workspace ID")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def listen_note(title, backend, workspace, output_json):
    """Start voice note — quick dictation with auto-extraction of key points and todos."""
    import httpx

    body = {
        "mode": "voice_note",
        "meeting_title": title,
        "capture_backend": backend,
        "workspace_id": workspace,
    }

    with httpx.Client(base_url=_genesis_url(), timeout=30) as c:
        resp = c.post("/audiobook/start", json=body)
        resp.raise_for_status()
        data = resp.json()

    session_id = data.get("session_id", "")

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo(f"Voice note started: {session_id[:12]}")
        click.echo(f"  Stop:   aither listen stop {session_id[:12]}")
        click.echo(f"  Export: aither listen export {session_id[:12]}")


@listen.command("lecture")
@click.argument("title", default="")
@click.option("--backend", default="wasapi",
              type=click.Choice(["wasapi", "pulse", "sounddevice"]))
@click.option("--workspace", default=None, help="Auto-save to workspace ID")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def listen_lecture(title, backend, workspace, output_json):
    """Start lecture transcription — topics, key concepts, Q&A tracking."""
    import httpx

    body = {
        "mode": "lecture",
        "meeting_title": title,
        "meeting_type": "lecture",
        "capture_backend": backend,
        "workspace_id": workspace,
    }

    with httpx.Client(base_url=_genesis_url(), timeout=30) as c:
        resp = c.post("/audiobook/start", json=body)
        resp.raise_for_status()
        data = resp.json()

    session_id = data.get("session_id", "")

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo(f"Lecture transcription started: {session_id[:12]}")
        click.echo(f"  Title: {title or 'Untitled Lecture'}")
        click.echo(f"  Stop:  aither listen stop {session_id[:12]}")


@listen.command("sessions")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def listen_sessions(output_json):
    """List active listening sessions."""
    import httpx

    with httpx.Client(base_url=_genesis_url(), timeout=10) as c:
        resp = c.get("/audiobook/sessions")
        resp.raise_for_status()
        data = resp.json()

    sessions = data.get("sessions", [])

    if output_json:
        click.echo(json.dumps(data, indent=2))
    elif not sessions:
        click.echo("No active sessions.")
    else:
        for s in sessions:
            sid = s.get("session_id", "")[:12]
            title = s.get("book_title", "Untitled")
            state = s.get("state", "unknown")
            chunks = s.get("chunks_processed", 0)
            chars = s.get("characters_tracked", 0)
            click.echo(f"  {sid}  {state:<10}  {title:<30}  {chunks} chunks  {chars} chars")


@listen.command("stop")
@click.argument("session_id")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def listen_stop(session_id, output_json):
    """Stop a listening session."""
    import httpx

    # Support partial session IDs
    if len(session_id) < 36:
        # Find matching session
        with httpx.Client(base_url=_genesis_url(), timeout=10) as c:
            resp = c.get("/audiobook/sessions")
            if resp.status_code == 200:
                sessions = resp.json().get("sessions", [])
                matches = [s for s in sessions if s["session_id"].startswith(session_id)]
                if len(matches) == 1:
                    session_id = matches[0]["session_id"]
                elif len(matches) > 1:
                    click.echo(f"Ambiguous ID '{session_id}' — matches {len(matches)} sessions.", err=True)
                    return

    with httpx.Client(base_url=_genesis_url(), timeout=15) as c:
        resp = c.post(f"/audiobook/{session_id}/stop")
        resp.raise_for_status()
        data = resp.json()

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo(f"Session stopped: {session_id[:12]}")


@listen.command("export")
@click.argument("session_id")
@click.option("--format", "fmt", default="notes", type=click.Choice(["notes", "transcript"]))
@click.option("--output", "-o", default=None, help="Write to file instead of stdout")
def listen_export(session_id, fmt, output):
    """Export session as markdown notes or raw transcript."""
    import httpx

    # Support partial session IDs
    if len(session_id) < 36:
        with httpx.Client(base_url=_genesis_url(), timeout=10) as c:
            resp = c.get("/audiobook/sessions")
            if resp.status_code == 200:
                sessions = resp.json().get("sessions", [])
                matches = [s for s in sessions if s["session_id"].startswith(session_id)]
                if len(matches) == 1:
                    session_id = matches[0]["session_id"]

    with httpx.Client(base_url=_genesis_url(), timeout=15) as c:
        resp = c.get(f"/audiobook/{session_id}/export/{fmt}")
        resp.raise_for_status()
        data = resp.json()

    content = data.get("content") or data.get("transcript", "")

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(content)
        click.echo(f"Exported to {output}")
    else:
        click.echo(content)


@listen.command("save")
@click.argument("session_id")
@click.option("--workspace", required=True, help="Workspace ID to save to")
def listen_save(session_id, workspace):
    """Save session to an AitherOne workspace."""
    import httpx

    with httpx.Client(base_url=_genesis_url(), timeout=15) as c:
        resp = c.post(f"/audiobook/{session_id}/save", json={"workspace_id": workspace})
        resp.raise_for_status()

    click.echo(f"Saved to workspace {workspace}")


@listen.command("to-deck")
@click.argument("session_id")
@click.option("--theme", default="dark", type=click.Choice(["dark", "light"]))
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def listen_to_deck(session_id, theme, output_json):
    """Convert session into a presentation deck (slides)."""
    import httpx

    with httpx.Client(base_url=_genesis_url(), timeout=30) as c:
        resp = c.post(f"/audiobook/{session_id}/to-presentation", json={"theme": theme})
        resp.raise_for_status()
        data = resp.json()

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        slug = data.get("slug", "")
        count = data.get("slide_count", 0)
        click.echo(f"Presentation created: {slug} ({count} slides)")
        click.echo(f"  View: http://localhost:3000/portal/saga?deck={slug}")
        click.echo(f"  Render video: aither listen to-video {session_id}")


@listen.command("to-blog")
@click.argument("session_id")
@click.option("--publish", is_flag=True, help="Publish as draft blog post")
@click.option("--no-enrich", is_flag=True, help="Skip LLM prose expansion")
@click.option("--output", "-o", default=None, help="Write markdown to file")
def listen_to_blog(session_id, publish, no_enrich, output):
    """Convert session into a blog post."""
    import httpx

    with httpx.Client(base_url=_genesis_url(), timeout=60) as c:
        resp = c.post(f"/audiobook/{session_id}/to-blog", json={
            "enrich": not no_enrich,
            "publish": publish,
        })
        resp.raise_for_status()
        data = resp.json()

    markdown = data.get("markdown", "")
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(markdown)
        click.echo(f"Blog post written to {output}")
    else:
        click.echo(markdown)

    if data.get("blog_post"):
        click.echo(f"\nPublished as draft: {data['blog_post'].get('slug', '')}")


@listen.command("to-video")
@click.argument("session_id")
@click.option("--voice", default="nova", help="TTS voice for narration")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def listen_to_video(session_id, voice, output_json):
    """Convert session into a narrated video (session -> slides -> Remotion render)."""
    import httpx

    click.echo("Creating presentation and rendering video...")
    with httpx.Client(base_url=_genesis_url(), timeout=120) as c:
        resp = c.post(f"/audiobook/{session_id}/to-video", json={"voice": voice})
        resp.raise_for_status()
        data = resp.json()

    if output_json:
        click.echo(json.dumps(data, indent=2))
    else:
        slug = data.get("presentation_slug", "")
        video = data.get("video", {})
        path = video.get("output_path", "")
        click.echo(f"Presentation: {slug} ({data.get('slide_count', 0)} slides)")
        if path:
            click.echo(f"Video: {path}")
        else:
            click.echo("Video render queued (check Remotion status)")


# ---------------------------------------------------------------------------
# aither docker — Docker Desktop recovery (WSL2 500-error hang)
# ---------------------------------------------------------------------------

@cli.group()
def docker():
    """Docker Desktop management and recovery."""
    pass


def _docker_healthy() -> bool:
    """Check if Docker engine responds without 500 errors."""
    import subprocess as _sp
    try:
        r = _sp.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0 and b"500 Internal Server Error" not in r.stderr
    except Exception:
        return False


def _docker_recover(verbose: bool = False) -> bool:
    """Kill Docker Desktop + WSL, restart cleanly. Returns True on success."""
    import subprocess as _sp
    import time

    def _run(cmd: str):
        if verbose:
            click.echo(f"  $ {cmd}")
        _sp.run(cmd, shell=True, capture_output=not verbose, timeout=30)

    click.echo(click.style("[1/5] Killing Docker Desktop...", fg="yellow"))
    for proc in ("Docker Desktop", "com.docker.backend", "com.docker.build",
                 "docker-agent", "docker-sandbox"):
        _run(f'taskkill /F /IM "{proc}.exe" 2>NUL')
    time.sleep(2)

    click.echo(click.style("[2/5] Shutting down WSL...", fg="yellow"))
    _run("wsl --shutdown")
    time.sleep(3)

    click.echo(click.style("[3/5] Cleaning up zombie processes...", fg="yellow"))
    _run("taskkill /F /IM vmmem 2>NUL")
    _run("taskkill /F /IM wslservice.exe 2>NUL")
    time.sleep(2)

    click.echo(click.style("[4/5] Restarting Docker service...", fg="yellow"))
    # Try admin service restart — will silently fail if not elevated
    _run('net stop com.docker.service 2>NUL & net start com.docker.service 2>NUL')
    time.sleep(2)

    click.echo(click.style("[5/5] Starting Docker Desktop...", fg="yellow"))
    docker_exe = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
    _sp.Popen([docker_exe], creationflags=0x00000008)  # DETACHED_PROCESS

    click.echo("Waiting for Docker engine...")
    for elapsed in range(5, 95, 5):
        time.sleep(5)
        if _docker_healthy():
            click.echo(click.style(f"Docker recovered in {elapsed}s!", fg="green"))
            # Clean up dead containers
            dead = _sp.run(
                ["docker", "ps", "-a", "--filter", "status=dead", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            for name in dead.stdout.strip().splitlines():
                if name:
                    click.echo(f"  Removing dead container: {name}")
                    _run(f"docker rm -f {name}")
            # Restart exited containers
            exited = _sp.run(
                ["docker", "ps", "-a", "--filter", "status=exited", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            for name in exited.stdout.strip().splitlines():
                if name:
                    click.echo(f"  Restarting: {name}")
                    _run(f"docker start {name}")
            return True
        if verbose:
            click.echo(f"  ... {elapsed}s")

    click.echo(click.style("Recovery FAILED after 90s. You may need to reboot.", fg="red"))
    return False


@docker.command()
@click.option("--verbose", "-v", is_flag=True, help="Show commands being run")
def recover(verbose):
    """Recover Docker Desktop from WSL2 500-error hang (no reboot needed)."""
    if _docker_healthy():
        click.echo(click.style("Docker engine is healthy. Nothing to do.", fg="green"))
        import subprocess as _sp
        r = _sp.run(
            ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
        click.echo(r.stdout[:2000])
        return
    ok = _docker_recover(verbose=verbose)
    raise SystemExit(0 if ok else 1)


@docker.command()
@click.option("--interval", "-i", default=30, help="Seconds between health checks (default: 30)")
@click.option("--verbose", "-v", is_flag=True, help="Show commands being run")
def watch(interval, verbose):
    """Monitor Docker health and auto-recover on failure."""
    import time
    click.echo(f"Docker health monitor started (checking every {interval}s). Ctrl+C to stop.")
    try:
        while True:
            if not _docker_healthy():
                ts = time.strftime("%H:%M:%S")
                click.echo(f"\n[{ts}] Docker is DOWN — recovering...")
                _docker_recover(verbose=verbose)
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("\nMonitor stopped.")


# ─── aither sessions (Claude Code session manager) ──────────────────────────
# Track / browse / search / resume Claude Code sessions, and guard against the
# "Windows Terminal died and took all 12 sessions with it" failure. Engine in
# adk/shell/claude_sessions.py — read-only against Claude's own journals.


def _sessions_engine():
    from adk.shell import claude_sessions as cs
    return cs


def _secho(text=""):
    """click.echo that survives legacy cp1252 consoles — journal titles and
    snippets routinely contain characters Windows' default codepage can't
    encode, and one bad char must not crash the whole listing."""
    try:
        click.echo(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        click.echo(str(text).encode(enc, errors="replace").decode(enc))


def _sessions_print_table(sessions, show_prompt=True):
    cs = _sessions_engine()
    crash = cs.pending_crash()
    if crash:
        n = len(crash.get("sessions", []))
        _secho(click.style(
            f"\n  ! crash recorded {crash.get('crashed_at', '?')} - {n} session(s) lost. "
            f"Run `aither sessions restore` to reopen them.", fg="yellow"))
    _secho(click.style("\n  Claude Code sessions", fg="cyan", bold=True))
    _secho(click.style("  " + "-" * 72, fg="bright_black"))
    for i, s in enumerate(sessions, 1):
        live = click.style(" LIVE", fg="green", bold=True) if s.live else ""
        branch = click.style(f" ({s.branch})", fg="cyan") if s.branch else ""
        title = s.title if len(s.title) <= 44 else s.title[:43] + "..."
        _secho(
            f"  {click.style(f'{i:>2}', fg='yellow', bold=True)}  "
            f"{click.style(f'{s.age:<9}', fg='bright_black')}  "
            f"{click.style(title, fg='white', bold=True)}{branch}{live}"
        )
        _secho("        " + click.style(s.cwd, fg="bright_black"))
        if show_prompt and s.last_prompt:
            lp = " ".join(s.last_prompt.split())
            if len(lp) > 70:
                lp = lp[:69] + "..."
            _secho("        " + click.style("> " + lp, fg="green"))
    _secho(click.style("  " + "-" * 72, fg="bright_black"))


def _sessions_launch_and_report(cs, chosen, separate, dry_run):
    lines = cs.launch_sessions(chosen, separate_windows=separate, dry_run=dry_run)
    verb = "Would resume" if dry_run else "Resuming"
    _secho(click.style(f"\n  {verb} {len(chosen)} session(s):", fg="green", bold=True))
    for ln in lines:
        _secho(f"    - {ln}")


@cli.group("sessions", invoke_without_command=True)
@click.option("--list", "as_list", is_flag=True, help="Plain list instead of the interactive browser")
@click.option("--filter", "text_filter", default="", help="Substring match on title/cwd/branch/prompt")
@click.option("--hours", default=0.0, help="Only sessions active within N hours")
@click.option("--per-dir", is_flag=True, help="One (most recent) session per directory")
@click.option("--scan", default=120, show_default=True, help="How many recent journals to parse")
@click.option("--top", default=25, show_default=True, help="Max sessions to show")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
@click.pass_context
def sessions_grp(ctx, as_list, text_filter, hours, per_dir, scan, top, output_json):
    """Manage Claude Code sessions — browse, search, resume, crash-recover.

    \b
    aither sessions                        # Interactive browser (filter/preview/resume)
    aither sessions --list                 # Plain list (LIVE-flagged)
    aither sessions search "vllm bug"      # Full-text search across sessions
    aither sessions resume 1,3,5-7         # Reopen sessions as WT tabs
    aither sessions resume all --per-dir   # One tab per project directory
    aither sessions restore                # Reopen everything a WT crash killed
    aither sessions guard install          # Auto-restore watchdog at logon
    aither sessions ingest --watch         # Auto-sync conversations into the brain
    """
    if ctx.invoked_subcommand is not None:
        return
    # Bare `aither sessions` on a real terminal = the interactive browser.
    if not (as_list or output_json or text_filter or per_dir or hours) and sys.stdin.isatty():
        try:
            from adk.shell.session_browser import browse
            browse()
            return
        except (ImportError, RuntimeError) as exc:
            click.echo(click.style(f"  (browser unavailable: {exc} — plain list)", fg="yellow"))
    cs = _sessions_engine()
    found = cs.scan_sessions(
        scan=scan, top=top, per_dir=per_dir,
        text_filter=text_filter, lookback_hours=hours,
    )
    if output_json:
        click.echo(json.dumps([s.to_dict() for s in found], indent=2))
        return
    if not found:
        click.echo("No Claude Code sessions found.")
        return
    _sessions_print_table(found)
    click.echo(click.style(
        "  resume with: aither sessions resume 1,3,5-7   (or 'all')\n", fg="bright_black"))


@sessions_grp.command("browse")
def sessions_browse():
    """Interactive full-screen browser: filter, preview, deep search, resume."""
    from adk.shell.session_browser import browse
    browse()


@sessions_grp.command("ingest")
@click.option("--days", default=7.0, show_default=True, help="Sessions active in the last N days")
@click.option("--watch", is_flag=True, help="Keep running; auto-sync new content")
@click.option("--interval", default=300.0, show_default=True, help="Watch tick seconds")
@click.option("--brain-sync/--local-only", default=True, show_default=True,
              help="Also push deltas to the CompanyBrain hub (needs `adk enroll`)")
@click.option("--classification", default="internal", show_default=True,
              type=click.Choice(["internal", "confidential", "restricted"]))
@click.option("--dry-run", is_flag=True, help="Report what would be ingested")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
def sessions_ingest(days, watch, interval, brain_sync, classification, dry_run, output_json):
    """Ingest session conversations into the local KB / company brain.

    Incremental: each run only processes journal content appended since the
    last run. Chunks containing secret patterns are skipped, never stored.
    """
    from adk.shell.claude_ingest import ingest_sessions, watch_sessions
    kwargs = dict(days=days, brain_sync=brain_sync,
                  classification=classification, dry_run=dry_run)
    if watch:
        click.echo(f"Session ingest watch running (every {interval:g}s, "
                   f"last {days:g} days, {'brain-sync' if brain_sync else 'local-only'}). "
                   "Ctrl+C to stop.")
        try:
            watch_sessions(interval=interval, on_event=lambda m: _secho(f"  [ingest] {m}"),
                           **kwargs)
        except KeyboardInterrupt:
            click.echo("\nIngest watch stopped.")
        return
    result = asyncio.run(ingest_sessions(**kwargs))
    if output_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
        return
    d = result.to_dict()
    verb = "would ingest" if dry_run else "ingested"
    click.echo(f"  sessions seen      : {d['sessions_seen']}")
    click.echo(f"  sessions {verb}: {d['sessions_ingested']}")
    click.echo(f"  chunks created     : {d['chunks_created']}")
    if d["chunks_skipped_secrets"]:
        click.echo(click.style(
            f"  chunks skipped     : {d['chunks_skipped_secrets']} (secret patterns)", fg="yellow"))
    click.echo(f"  brain synced       : {d['chunks_synced'] if d['brain_synced'] else 'no'}")
    for err in d["errors"]:
        _secho(click.style(f"  ! {err}", fg="red"))


@sessions_grp.command("search")
@click.argument("query")
@click.option("--days", default=30.0, show_default=True, help="Only journals touched in the last N days")
@click.option("--limit", default=20, show_default=True, help="Max matching sessions")
@click.option("--json", "output_json", is_flag=True, help="JSON output")
@click.option("--resume", "do_resume", is_flag=True, help="Pick matches to resume after searching")
def sessions_search(query, days, limit, output_json, do_resume):
    """Full-text search across session conversations (user + assistant text)."""
    cs = _sessions_engine()
    hits = cs.search_sessions(query, days=days, max_sessions=limit)
    if output_json:
        click.echo(json.dumps(
            [{**h.session.to_dict(), "matches": h.matches, "snippets": h.snippets} for h in hits],
            indent=2))
        return
    if not hits:
        click.echo(f"No sessions matching {query!r} in the last {days:g} days.")
        return
    cs.mark_live([h.session for h in hits])
    _secho(click.style(f"\n  {len(hits)} session(s) matching ", fg="cyan", bold=True)
           + click.style(query, fg="yellow", bold=True))
    _secho(click.style("  " + "-" * 72, fg="bright_black"))
    for i, h in enumerate(hits, 1):
        s = h.session
        live = click.style(" LIVE", fg="green", bold=True) if s.live else ""
        _secho(
            f"  {click.style(f'{i:>2}', fg='yellow', bold=True)}  "
            f"{click.style(f'{s.age:<9}', fg='bright_black')}  "
            f"{click.style(s.title, fg='white', bold=True)}"
            f"{click.style(f'  [{h.matches} match(es)]', fg='cyan')}{live}"
        )
        _secho("        " + click.style(s.cwd, fg="bright_black"))
        for snip in h.snippets:
            _secho("        " + click.style("> " + snip, fg="green"))
    _secho(click.style("  " + "-" * 72, fg="bright_black"))
    if do_resume:
        answer = click.prompt("  resume which? (e.g. 1,3 / all / Enter=none)",
                              default="", show_default=False)
        idx = cs.expand_selection(answer, len(hits))
        if idx:
            _sessions_launch_and_report(cs, [hits[i - 1].session for i in idx],
                                        separate=False, dry_run=False)


@sessions_grp.command("resume")
@click.argument("selector", default="")
@click.option("--filter", "text_filter", default="", help="Substring match before selecting")
@click.option("--hours", default=0.0, help="Only sessions active within N hours")
@click.option("--per-dir", is_flag=True, help="One (most recent) session per directory")
@click.option("--separate", is_flag=True, help="Separate WT windows instead of tabs")
@click.option("--dry-run", is_flag=True, help="Show what would launch")
@click.option("--include-live", is_flag=True, help="Also offer sessions that look already open")
def sessions_resume(selector, text_filter, hours, per_dir, separate, dry_run, include_live):
    """Reopen sessions: `resume all`, `resume 1,3,5-7`, or interactive pick.

    Already-LIVE sessions are excluded by default so you don't fork a
    conversation that's still open in another tab.
    """
    cs = _sessions_engine()
    found = cs.scan_sessions(per_dir=per_dir, text_filter=text_filter, lookback_hours=hours)
    if not include_live:
        found = [s for s in found if not s.live]
    if not found:
        click.echo("No resumable sessions (everything recent looks already open — "
                   "use --include-live to override).")
        return
    if not selector:
        _sessions_print_table(found)
        selector = click.prompt("  resume which? (e.g. 1,3,5-7 / all / Enter=cancel)",
                                default="", show_default=False)
        if not selector.strip():
            click.echo("  Cancelled.")
            return
    idx = cs.expand_selection(selector, len(found))
    if not idx:
        click.echo("  Nothing selected.")
        return
    _sessions_launch_and_report(cs, [found[i - 1] for i in idx], separate, dry_run)


@sessions_grp.command("restore")
@click.option("--separate", is_flag=True, help="Separate WT windows instead of tabs")
@click.option("--dry-run", is_flag=True, help="Show what would launch")
def sessions_restore(separate, dry_run):
    """Reopen the full session set from the last crash (or last live snapshot)."""
    cs = _sessions_engine()
    snap = cs.pending_crash()
    source = "crash"
    if not snap:
        snap = cs._read_json(cs.LIVE_SNAPSHOT)
        source = "last live snapshot"
    if not snap or not snap.get("sessions"):
        click.echo("No crash or live snapshot recorded yet. "
                   "Run `aither sessions guard install` so the watchdog tracks your sessions.")
        return
    chosen = cs.snapshot_sessions(snap)
    click.echo(f"  Restoring from {source} ({snap.get('taken_at', '?')}).")
    _sessions_launch_and_report(cs, chosen, separate, dry_run)
    if source == "crash" and not dry_run:
        cs.clear_crash()


@sessions_grp.command("guard")
@click.argument("action", default="status",
                type=click.Choice(["status", "run", "install", "uninstall", "pause", "unpause"]))
@click.option("--daemon", is_flag=True, help="(with run) alias used by the scheduled task")
@click.option("--interval", default=20.0, show_default=True, help="Seconds between watchdog ticks")
@click.option("--min-sessions", default=3, show_default=True,
              help="Sessions that must die at once to count as a crash")
@click.option("--no-auto-restore", is_flag=True, help="Record crashes but don't relaunch")
def sessions_guard(action, daemon, interval, min_sessions, no_auto_restore):
    """Crash watchdog: snapshot live sessions, auto-restore after terminal death.

    \b
    aither sessions guard install     # Hidden at-logon scheduled task (recommended)
    aither sessions guard run         # Run the loop in THIS console (for testing)
    aither sessions guard pause       # Temporarily disable crash restore
    """
    cs = _sessions_engine()
    if action == "install":
        name = cs.install_guard(auto_restore=not no_auto_restore)
        click.echo(f"Installed + started scheduled task: {name!r}. "
                   "Your sessions now auto-restore after a terminal crash.")
        return
    if action == "uninstall":
        ok = cs.uninstall_guard()
        click.echo("Guard task removed." if ok else "Guard task was not installed.")
        return
    if action == "pause":
        cs.GUARD_PAUSE.parent.mkdir(parents=True, exist_ok=True)
        cs.GUARD_PAUSE.touch()
        click.echo("Guard paused (crash restore disabled until `guard unpause`).")
        return
    if action == "unpause":
        try:
            cs.GUARD_PAUSE.unlink()
        except OSError:
            pass
        click.echo("Guard unpaused.")
        return
    if action == "run" or daemon:
        if cs.claude_process_count() < 0:
            click.echo("psutil not installed — guard can't see claude processes. "
                       "pip install psutil", err=True)
            sys.exit(1)
        click.echo(f"Session guard running (tick {interval:g}s, crash = >={min_sessions} "
                   f"sessions dying with the terminal). Ctrl+C to stop.")
        click.echo("NOTE: run this OUTSIDE the terminal it guards — "
                   "`aither sessions guard install` does that for you.")
        try:
            cs.guard_loop(interval=interval, auto_restore=not no_auto_restore,
                          min_sessions=min_sessions,
                          on_event=lambda msg: click.echo(f"  [guard] {msg}"))
        except KeyboardInterrupt:
            click.echo("\nGuard stopped.")
        return
    # status
    snap = cs._read_json(cs.LIVE_SNAPSHOT)
    crash = cs.pending_crash()
    n_procs = cs.claude_process_count()
    click.echo(f"  claude processes now : {n_procs if n_procs >= 0 else 'unknown (no psutil)'}")
    if snap:
        click.echo(f"  last live snapshot   : {snap.get('taken_at')} "
                   f"({len(snap.get('sessions', []))} session(s))")
    else:
        click.echo("  last live snapshot   : none — guard has never run")
    click.echo("  pending crash        : "
               + (f"{crash.get('crashed_at')} ({len(crash.get('sessions', []))} lost) "
                  f"-> `aither sessions restore`" if crash else "none"))
    click.echo(f"  paused               : {'YES' if cs.GUARD_PAUSE.exists() else 'no'}")
    if sys.platform == "win32":
        import subprocess as _sp
        r = _sp.run(["schtasks", "/Query", "/TN", cs.GUARD_TASK_NAME],
                    capture_output=True, text=True)
        click.echo(f"  scheduled task       : {'installed' if r.returncode == 0 else 'NOT installed'}"
                   + ("" if r.returncode == 0 else "  -> `aither sessions guard install`"))


# ─── command center (aither hq / inbox / agents / palette / brief / watch) ──
# One cockpit for the fleet. All reads via command_center.fleet_client —
# fail-soft per source, internal-CA TLS, dashboard-grade timeouts.


@cli.command("hq")
def hq_cmd():
    """Command-center dashboard: fleet, LLM queue, alerts, sessions, inbox.

    Hotkeys jump into chat, sessions browser, inbox, agents, brief, watch,
    and docker recovery. The morning one-terminal cockpit.
    """
    from adk.shell.command_center.hq import run_hq
    run_hq()


@cli.command("inbox")
@click.option("--min-severity", default=0.3, show_default=True,
              help="Alert severity floor (0-1)")
def inbox_cmd(min_severity):
    """Unified inbox: mail + relay mentions + Pulse alerts, one queue."""
    from adk.shell.command_center.inbox import run_inbox
    run_inbox(min_severity=min_severity)


@cli.command("palette")
def palette_cmd():
    """Universal fuzzy picker: actions, sessions, services — Enter acts."""
    from adk.shell.command_center.palette import run_palette
    run_palette()


@cli.command("brief")
@click.option("--run", "do_run", is_flag=True,
              help="Trigger a fresh briefing (also emails/inboxes it)")
@click.option("--history", default=1, show_default=True,
              help="Show the latest brief (1) or list the last N")
def brief_cmd(do_run, history):
    """The executive briefing, rendered in your terminal (Atlas archive)."""
    from adk.shell.command_center.brief import run_brief
    run_brief(run=do_run, history=history)


@cli.command("watch")
@click.option("--interval", default=15.0, show_default=True, help="Tick seconds")
@click.option("--auto-recover", is_flag=True,
              help="Auto-run docker recovery when the wedge signature fires")
def watch_cmd(interval, auto_recover):
    """Fleet watchtower: health stream + named wedge-signature detection."""
    from adk.shell.command_center.watchtower import run_watch
    run_watch(interval=interval, auto_recover=auto_recover)


@cli.group("agents", invoke_without_command=True)
@click.pass_context
def agents_grp(ctx):
    """Agent console: roster, ask any agent (effort-tiered), forge, routines.

    \b
    aither agents                          # Interactive console
    aither agents ask hydra "review X"     # One-shot ask (default effort 5)
    aither agents ask -e 8 atlas "why..."  # Reasoning-tier ask
    """
    if ctx.invoked_subcommand is None:
        from adk.shell.command_center.agents_console import run_agents
        run_agents()


@agents_grp.command("ask")
@click.option("-e", "--effort", default=5, show_default=True,
              type=click.IntRange(1, 10), help="Effort tier (1-2 triage, 7-10 reasoning)")
@click.argument("agent")
@click.argument("question", nargs=-1, required=True)
def agents_ask(effort, agent, question):
    """Ask a named agent one question and print the answer."""
    from adk.shell.command_center.agents_console import ask_once
    ask_once(agent, " ".join(question), effort=effort)


if __name__ == "__main__":
    entry()
