"""``adk auth`` — OAuth provider authentication commands.

Currently supports:
    adk auth google-ai-plan [login] [--no-browser] [--client-id ID] [--client-secret-stdin] [--set-default] [--json]
    adk auth google-ai-plan status [--json]
    adk auth google-ai-plan refresh [--json]
    adk auth google-ai-plan logout [--revoke] [--json]
    adk auth google-ai-plan test [--model MODEL] [--simulate-429] [--json]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from typing import Any, Callable

from adk.llm.google_ai_plan import DEFAULT_MODEL, GoogleAIPlanQuotaError
from adk.llm.google_ai_plan_auth import (
    GoogleAIPlanAuthError,
    GoogleAIPlanConfigError,
    GoogleAIPlanCredentialStore,
    get_valid_access_token,
    login_google_ai_plan,
    revoke_token,
)


def register_parser(sub: Any) -> None:
    """Attach ``auth`` to the top-level ``adk`` subparsers."""
    p = sub.add_parser(
        "auth",
        help="Authenticate external OAuth LLM providers (e.g. Google AI Plan)",
    )
    asub = p.add_subparsers(dest="auth_provider")

    gp = asub.add_parser(
        "google-ai-plan",
        help="Sign in with Google AI Plan (Desktop PKCE + RFC 9449 DPoP)",
    )
    gp.add_argument(
        "action",
        nargs="?",
        default="login",
        choices=("login", "status", "refresh", "logout", "test"),
        help="Action to perform (default: login)",
    )
    gp.add_argument("--no-browser", action="store_true", help="Do not launch browser (SSH/headless paste mode)")
    gp.add_argument("--client-id", default=None, help="Google Desktop OAuth client_id")
    gp.add_argument(
        "--client-secret-stdin",
        action="store_true",
        help="Read Google Desktop OAuth client_secret from stdin (never passed on argv)",
    )
    gp.add_argument("--set-default", action="store_true", help="Set default_backend to google-ai-plan after login")
    gp.add_argument("--revoke", action="store_true", help="Revoke refresh token with Google on logout")
    gp.add_argument("--model", default=DEFAULT_MODEL, help=f"Model for test probe (default: {DEFAULT_MODEL})")
    gp.add_argument(
        "--simulate-429",
        action="store_true",
        help="Inject a synthetic daily-quota 429 on primary to test :8287 local-bridge failover",
    )
    gp.add_argument("--json", action="store_true", help="Machine-readable JSON output")


def _mask_client_id(client_id: str) -> str:
    cid = (client_id or "").strip()
    if not cid:
        return ""
    if len(cid) <= 12:
        return "..." + cid[-4:]
    return f"{cid[:6]}...{cid[-10:]}"


def _probe_local_bridge() -> dict[str, Any]:
    base = os.environ.get("AITHER_LOCAL_BRIDGE_URL", "").strip()
    if not base:
        host = os.environ.get("AITHER_LOCAL_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = os.environ.get("AITHER_LOCAL_BRIDGE_PORT", "8287").strip() or "8287"
        base = f"http://{host}:{port}"
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    result: dict[str, Any] = {"url": base, "reachable": False, "marker_ok": False}
    try:
        with urllib.request.urlopen(f"{base}/__local_bridge_health", timeout=2.0) as resp:
            result["reachable"] = resp.status == 200
            body = resp.read().decode("utf-8", errors="replace")
            result["marker_ok"] = "aither-local-bridge" in body
    except Exception:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=2.0) as resp:
                result["reachable"] = resp.status == 200
        except Exception as exc:
            result["detail"] = str(exc)
    return result


def _status_payload(store: GoogleAIPlanCredentialStore) -> dict[str, Any]:
    """Build sanitized status payload — NEVER includes tokens or private JWK scalars."""
    cred = None
    binding = "unsealed"
    load_error = ""
    try:
        cred = store.load(verify_binding=False)
        if cred is not None:
            binding = store.binding_status(cred)
    except Exception as exc:
        load_error = str(exc)

    now = time.time()
    bridge = _probe_local_bridge()

    if cred is None:
        return {
            "provider": "google-ai-plan",
            "signed_in": False,
            "storage_backend": store.storage_backend,
            "device_binding": binding,
            "local_bridge": bridge,
            "error": load_error,
        }

    ttl_s = max(0, int(cred.expires_at - now))
    return {
        "provider": "google-ai-plan",
        "signed_in": True,
        "storage_backend": store.storage_backend,
        "client_id_masked": _mask_client_id(cred.client_id),
        "expires_at": cred.expires_at,
        "remaining_ttl_s": ttl_s,
        "jwk_thumbprint": cred.jwk_thumbprint,
        "device_binding": binding,
        "bound_at": cred.bound_at,
        "local_bridge": bridge,
    }


def cmd_auth(
    args: Any,
    out: Callable[[str], None] = print,
    *,
    store: GoogleAIPlanCredentialStore | None = None,
) -> int:
    """Dispatch ``adk auth <provider> <action>``."""
    provider = getattr(args, "auth_provider", None)
    if provider != "google-ai-plan":
        out("Usage: adk auth google-ai-plan [login|status|refresh|logout|test]")
        return 2

    action = getattr(args, "action", None) or "login"
    as_json = bool(getattr(args, "json", False))
    cred_store = store or GoogleAIPlanCredentialStore()

    if action == "status":
        payload = _status_payload(cred_store)
        if as_json:
            out(json.dumps(payload, indent=2))
            return 0 if payload["signed_in"] else 1
        out("Google AI Plan Auth")
        out(f"  signed_in:      {payload['signed_in']}")
        out(f"  storage:        {payload['storage_backend']}")
        if payload["signed_in"]:
            out(f"  client_id:      {payload['client_id_masked']}")
            out(f"  remaining_ttl:  {payload['remaining_ttl_s']}s")
            out(f"  dpop_jwk_sha256:{payload['jwk_thumbprint']}")
            binding_str = payload["device_binding"]
            if binding_str == "unsealed":
                binding_str = "unsealed (install awdk[seal] to enable)"
            out(f"  device_binding: {binding_str}")
        elif payload.get("error"):
            out(f"  [!] {payload['error']}")
        bridge = payload["local_bridge"]
        out(
            f"  local_bridge:   {bridge['url']} "
            f"({'reachable' if bridge['reachable'] else 'unreachable'})"
        )
        return 0 if payload["signed_in"] else 1

    if action == "login":
        client_id = getattr(args, "client_id", None)
        client_secret = None
        if getattr(args, "client_secret_stdin", False):
            client_secret = sys.stdin.readline().strip()
        try:
            cred = asyncio.run(
                login_google_ai_plan(
                    store=cred_store,
                    client_id=client_id,
                    client_secret=client_secret,
                    no_browser=bool(getattr(args, "no_browser", False)),
                    out=(lambda _s: None) if as_json else out,
                )
            )
        except (GoogleAIPlanConfigError, GoogleAIPlanAuthError) as exc:
            if as_json:
                out(json.dumps({"ok": False, "error": str(exc)}))
            else:
                out(f"  [!] {exc}")
            return 1

        if getattr(args, "set_default", False):
            try:
                from adk.config import save_saved_config
                save_saved_config({"default_backend": "google-ai-plan"})
            except Exception as exc:
                if not as_json:
                    out(f"  [!] Could not set default backend: {exc}")

        payload = _status_payload(cred_store)
        if as_json:
            out(json.dumps({"ok": True, **payload}, indent=2))
        else:
            out(f"  [+] Signed in to Google AI Plan (storage={cred_store.storage_backend}, thumbprint={cred.jwk_thumbprint[:12]}...)")
            if getattr(args, "set_default", False):
                out("  [+] Set default_backend = google-ai-plan")
        return 0

    if action == "refresh":
        try:
            _, cred = asyncio.run(
                get_valid_access_token(cred_store, force_refresh=True)
            )
        except (GoogleAIPlanConfigError, GoogleAIPlanAuthError) as exc:
            if as_json:
                out(json.dumps({"ok": False, "error": str(exc)}))
            else:
                out(f"  [!] {exc}")
            return 1
        payload = _status_payload(cred_store)
        if as_json:
            out(json.dumps({"ok": True, **payload}, indent=2))
        else:
            out(f"  [+] Token refreshed (remaining_ttl={payload['remaining_ttl_s']}s, thumbprint={cred.jwk_thumbprint[:12]}...)")
        return 0

    if action == "logout":
        revoked = False
        if getattr(args, "revoke", False):
            try:
                cred = cred_store.load(verify_binding=False)
                if cred and cred.refresh_token:
                    revoked = asyncio.run(revoke_token(cred.refresh_token))
            except Exception:
                revoked = False
        removed = cred_store.delete()
        if as_json:
            out(json.dumps({"ok": True, "removed": removed, "revoked": revoked}))
        else:
            out(f"  [+] Google AI Plan credentials cleared (removed={removed}, revoked={revoked})")
        return 0

    if action == "test":
        from adk.llm import LLMRouter, Message

        model = getattr(args, "model", None) or DEFAULT_MODEL
        simulate_429 = bool(getattr(args, "simulate_429", False))

        async def _run_test():
            router = LLMRouter(provider="google-ai-plan", model=model)
            await router.get_provider()
            if simulate_429:
                async def _raise_429(*_a: Any, **_kw: Any):
                    raise GoogleAIPlanQuotaError(
                        f"Simulated daily limit for {model}",
                        scope="daily",
                        model=model,
                    )
                router._provider.chat = _raise_429  # type: ignore[union-attr,method-assign]
            return await router.chat(
                [Message(role="user", content="Reply with OK.")],
                model=model,
                max_tokens=32,
            )

        try:
            resp = asyncio.run(_run_test())
        except Exception as exc:
            if as_json:
                out(json.dumps({"ok": False, "error": str(exc)}))
            else:
                out(f"  [!] Test failed: {exc}")
            return 1

        if as_json:
            out(json.dumps({
                "ok": True,
                "simulated_429": simulate_429,
                "model": resp.model,
                "content": resp.content,
                "finish_reason": resp.finish_reason,
            }, indent=2))
        else:
            out(f"  [+] Test succeeded (model={resp.model}, simulated_429={simulate_429}): {resp.content[:120]}")
        return 0

    out(f"  [!] Unknown action: {action}")
    return 2
