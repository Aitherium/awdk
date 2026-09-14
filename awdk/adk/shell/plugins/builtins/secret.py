"""
Secret Plugin for AitherShell
==============================

Generate, store, list, get, and rotate secrets in AitherSecrets — without
remembering REST endpoints, curl flags, or API keys.

Usage:
    /secret gen GITHUB_WEBHOOK_SECRET                 — Generate + store + print once
    /secret gen GITHUB_WEBHOOK_SECRET --rotate        — Rotate existing
    /secret gen JWT_KEY --length 64 --format hex      — Custom format
    /secret set DB_PASSWORD "p@ssw0rd"                — Store an existing value
    /secret get GITHUB_WEBHOOK_SECRET                 — Read it back
    /secret ls                                        — List names (no values)
    /secret help                                      — Show help

Aliases: /secrets, /vault

Reads:
    AITHER_SECRETS_URL  (default: http://127.0.0.1:8001 — the genesis proxy, the
                         host-reachable path that accepts the session bearer)
    AITHER_ADMIN_KEY    (or AITHER_INTERNAL_SECRET / AITHER_MASTER_KEY) — the
                         in-network vault / explicit elevation path
    ~/.aither/session-bearer — the logged-in session. If you are the owner you
                         already have platform-wide access; this is the elevation.
"""

from __future__ import annotations
from adk._tls import tls_verify

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from adk.shell.plugins import SlashCommand


def _resolve_url() -> str:
    # Host path: the genesis proxy (127.0.0.1:8001) authenticates the logged-in
    # session and routes to the vault. In-container deployments set
    # AITHER_SECRETS_URL to the vault directly (e.g. https://aitheros-secrets:8111).
    return (
        os.environ.get("AITHER_SECRETS_URL")
        or os.environ.get("AITHERSECRETS_URL")
        or "http://127.0.0.1:8001"
    ).rstrip("/")


def _resolve_admin_key() -> Optional[str]:
    for var in ("AITHER_ADMIN_KEY", "AITHER_INTERNAL_SECRET", "AITHER_MASTER_KEY"):
        v = os.environ.get(var)
        if v:
            return v
    return None


def _resolve_bearer() -> Optional[str]:
    """The logged-in session — the credential that actually works from the host.
    Read from ~/.aither/session-bearer; never printed."""
    try:
        b = (Path.home() / ".aither" / "session-bearer").read_text(encoding="utf-8").strip()
        return b or None
    except Exception:
        return None


def _headers() -> Dict[str, str]:
    """Prefer the explicit admin key (in-network vault / elevation), else fall
    back to the logged-in session bearer (genesis proxy on the host)."""
    key = _resolve_admin_key()
    bearer = _resolve_bearer()
    h = {"Content-Type": "application/json"}
    if key:
        h["X-API-Key"] = key
    elif bearer:
        h["Authorization"] = f"Bearer {bearer}"
    return h


def _parse_flags(args: List[str]) -> tuple[List[str], Dict[str, Any]]:
    """Split positional args from --flag value pairs."""
    positional: List[str] = []
    flags: Dict[str, Any] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            # Boolean flags
            if key in ("rotate", "overwrite", "force"):
                flags["overwrite"] = True
                i += 1
                continue
            # Value flags
            if i + 1 < len(args):
                flags[key] = args[i + 1]
                i += 2
            else:
                flags[key] = True
                i += 1
        else:
            positional.append(a)
            i += 1
    return positional, flags


_HELP = """\
/secret — manage AitherSecrets vault

  /secret gen <NAME> [--length 48] [--format urlsafe|hex|alphanumeric] [--rotate]
      Generate a strong random secret, store it, and print it ONCE.
      Idempotent: if NAME exists, returns the existing value (use --rotate to overwrite).

  /secret set <NAME> <VALUE>
      Store an existing value.

  /secret get <NAME>
      Print the stored value.

  /secret ls
      List all secret names (no values).

Examples:
  /secret gen GITHUB_WEBHOOK_SECRET
  /secret gen GITHUB_WEBHOOK_SECRET --rotate
  /secret gen JWT_KEY --length 64 --format hex
  /secret set GITHUB_APP_ID 1234567
  /secret get GITHUB_WEBHOOK_SECRET
"""


class Secret(SlashCommand):
    name: str = "secret"
    description: str = "Generate / store / read secrets in AitherSecrets"
    aliases: List[str] = None  # set in __init__

    def __init__(self):
        super().__init__()
        self.name = "secret"
        self.description = "Generate / store / read secrets in AitherSecrets"
        self.aliases = ["secrets", "vault"]
        self.hidden = False

    async def run(self, args: List[str], ctx: Dict[str, Any]) -> Optional[str]:
        if not args or args[0] in ("help", "-h", "--help", "?"):
            return _HELP

        sub = args[0].lower()
        rest, flags = _parse_flags(args[1:])
        url = _resolve_url()

        if not _resolve_admin_key():
            return (
                "[!!] No admin key found in env.\n"
                "     Set AITHER_ADMIN_KEY (or AITHER_INTERNAL_SECRET / AITHER_MASTER_KEY)\n"
                "     in this shell, then retry."
            )

        try:
            if sub in ("gen", "generate", "new"):
                return await self._gen(url, rest, flags)
            if sub in ("set", "put", "store"):
                return await self._set(url, rest)
            if sub in ("get", "show", "read"):
                return await self._get(url, rest)
            if sub in ("ls", "list"):
                return await self._list(url)
            return f"Unknown subcommand: {sub}\n\n{_HELP}"
        except httpx.HTTPError as e:
            return f"[!!] AitherSecrets request failed: {e}\n     URL: {url}"

    # ------------------------------------------------------------------
    async def _gen(self, url: str, rest: List[str], flags: Dict[str, Any]) -> str:
        if not rest:
            return "Usage: /secret gen <NAME> [--length N] [--format urlsafe|hex|alphanumeric] [--rotate]"
        name = rest[0]
        length = int(flags.get("length", 48))
        fmt = str(flags.get("format", "urlsafe"))
        overwrite = bool(flags.get("overwrite", False))

        async with httpx.AsyncClient(timeout=15.0, verify=tls_verify()) as client:
            # Preferred: server-side generation (atomic, returns existing if not overwrite)
            try:
                resp = await client.post(
                    f"{url}/secrets/generate",
                    json={
                        "name": name, "length": length, "format": fmt,
                        "secret_type": "generic", "access_level": "internal",
                        "overwrite": overwrite,
                    },
                    headers=_headers(),
                )
                if resp.status_code == 404:
                    raise httpx.HTTPStatusError("no /secrets/generate", request=resp.request, response=resp)
                resp.raise_for_status()
                data = resp.json()
                value = data.get("value", "")
                created = data.get("created", False)
            except (httpx.HTTPStatusError, httpx.HTTPError):
                # Fallback: idempotent check + client-side generation + POST /secrets
                existing = None
                if not overwrite:
                    g = await client.get(f"{url}/secrets/{name}", headers=_headers())
                    if g.status_code == 200:
                        try:
                            jd = g.json()
                            existing = jd.get("value") if isinstance(jd, dict) else None
                        except Exception:
                            existing = None

                if existing and not overwrite:
                    value = existing
                    created = False
                else:
                    import secrets as _secrets
                    import string as _string
                    if fmt == "hex":
                        value = _secrets.token_hex(length)
                    elif fmt in ("alphanumeric", "alnum"):
                        alphabet = _string.ascii_letters + _string.digits
                        value = "".join(_secrets.choice(alphabet) for _ in range(length))
                    else:
                        value = _secrets.token_urlsafe(length)

                    s = await client.post(
                        f"{url}/secrets",
                        json={
                            "name": name, "value": value,
                            "secret_type": "generic", "access_level": "internal",
                        },
                        headers=_headers(),
                    )
                    s.raise_for_status()
                    if not s.json().get("success"):
                        return f"[!!] Store failed: {s.json()}"
                    created = True
                data = {"format": fmt, "length": length}

        # Try to copy to clipboard (best-effort)
        clip_note = ""
        try:
            import pyperclip  # type: ignore
            pyperclip.copy(value)
            clip_note = "  [OK] Copied to clipboard."
        except (ImportError, OSError):
            try:
                import subprocess as _sp
                import platform as _plat
                if _plat.system() == "Windows":
                    _p = _sp.Popen(["clip"], stdin=_sp.PIPE, close_fds=True)
                    _p.communicate(input=value.encode("utf-16-le"))
                    if _p.returncode == 0:
                        clip_note = "  [OK] Copied to clipboard."
            except Exception:
                pass

        status = "Created" if created else "Already existed (use --rotate to overwrite)"
        return (
            f"\n  AitherSecrets — {name}\n"
            f"  {'=' * (16 + len(name))}\n"
            f"  Status: {status}\n"
            f"  Format: {data.get('format', 'n/a')}  length={data.get('length', 'n/a')}\n\n"
            f"  VALUE (copy now):\n\n"
            f"    {value}\n"
            f"{clip_note}\n"
        )

    async def _set(self, url: str, rest: List[str]) -> str:
        if len(rest) < 2:
            return "Usage: /secret set <NAME> <VALUE>"
        name, value = rest[0], " ".join(rest[1:])
        body = {
            "name": name,
            "value": value,
            "secret_type": "generic",
            "access_level": "internal",
        }
        async with httpx.AsyncClient(timeout=15.0, verify=tls_verify()) as client:
            resp = await client.post(f"{url}/secrets", json=body, headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        return f"[OK] Stored '{name}' ({len(value)} chars)." if data.get("success") else f"[!!] Failed: {data}"

    async def _get(self, url: str, rest: List[str]) -> str:
        if not rest:
            return "Usage: /secret get <NAME>"
        name = rest[0]
        async with httpx.AsyncClient(timeout=15.0, verify=tls_verify()) as client:
            resp = await client.get(f"{url}/secrets/{name}", headers=_headers())
        if resp.status_code == 404:
            return f"[!!] '{name}' not found."
        resp.raise_for_status()
        data = resp.json()
        # Endpoint returns {"name": ..., "value": ...} or wrapped
        value = data.get("value") if isinstance(data, dict) else None
        if value is None and isinstance(data, dict):
            value = data.get("secret", {}).get("value") if "secret" in data else None
        return f"  {name} = {value}" if value is not None else f"[!!] No value: {data}"

    async def _list(self, url: str) -> str:
        async with httpx.AsyncClient(timeout=15.0, verify=tls_verify()) as client:
            resp = await client.get(f"{url}/secrets", headers=_headers())
            resp.raise_for_status()
            data = resp.json()
        names = data if isinstance(data, list) else data.get("secrets", data.get("names", data.get("keys", [])))
        if not names:
            return "(no secrets stored)"
        # If list of dicts, extract names
        if names and isinstance(names[0], dict):
            names = [n.get("name", str(n)) for n in names]
        return "Secrets in vault:\n" + "\n".join(f"  - {n}" for n in sorted(names))
