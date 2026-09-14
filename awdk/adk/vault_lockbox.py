"""Vault lockbox — an on-demand, human-facing front end for the AitherSecrets vault.

The AitherSecrets service (:8111) already stores every fleet credential, but the
only way to read one used to be a hand-built ``curl`` with the master key pasted
on the command line — which then lingers in shell scrollback. This module is the
lockbox on top of it:

  * The vault master key is sealed ONCE into the OS keychain (Windows Credential
    Manager / macOS Keychain / Secret Service) via ``keyring``. It is encrypted at
    rest by the OS, tied to your login account — that login IS the unlock. The key
    never lives in a plaintext ``.env`` you have to source, and is never echoed.
  * An optional PIN adds a second factor before a *value* is revealed in a session.
  * Values are copied to the clipboard by default and only ever printed with an
    explicit ``--show``, so reading a secret doesn't dump it into scrollback.

Talks DIRECTLY to the live vault (list / get / set / rotate) — this is not the
local encrypted keyring that ``adk secret`` syncs into; it's the real vault.

CLI surface (registered in adk/cli.py as ``adk vault``):

    adk vault setup [--from-env] [--pin]     one-time: seal master key into keychain
    adk vault status                          is it set up, reachable, how many secrets
    adk vault ls [--filter TEXT]              list names + metadata (never values)
    adk vault get NAME [--show] [--copy]      reveal one (clipboard by default)
    adk vault search TERM                     fuzzy name search
    adk vault rotate NAME [--length N] [--show]   mint a fresh strong value + store it
    adk vault lock [--forget]                 drop the session PIN unlock (--forget wipes key)
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import secrets as _secrets
import time
from pathlib import Path
from typing import Optional

try:
    from adk._tls import tls_verify
except Exception:  # pragma: no cover - standalone fallback
    def tls_verify():
        return False

# Keychain service/account identifiers (namespaced so we never collide with
# adk's own login token store).
_KR_SERVICE = "aither-vault"
_KR_KEY_ACCOUNT = "master-key"
_KR_URL_ACCOUNT = "url"
_KR_PIN_ACCOUNT = "pin-sha256"

# Where a *session* unlock (not the key) records its expiry. Holds only a
# timestamp — never the key or the PIN.
_SESSION_FILE = Path.home() / ".aither" / "vault-session.json"
_SESSION_TTL_SECONDS = 15 * 60  # a PIN unlock lasts 15 min

# The operator (a machine holding the vault master key) talks to the vault on the
# loopback fleet address; a logged-in end user reaches the same secrets through the
# authenticated public gateway (federation entry), per the AitherMesh onboarding model.
_DEFAULT_URL = "https://127.0.0.1:8111"
_PUBLIC_GATEWAY = "https://gateway.aitherium.com"


# ── keychain plumbing ────────────────────────────────────────────────────────

def _keyring():
    import keyring  # imported lazily so the rest of adk doesn't hard-depend on it
    return keyring


def _get_stored(account: str) -> Optional[str]:
    try:
        return _keyring().get_password(_KR_SERVICE, account)
    except Exception:
        return None


def _set_stored(account: str, value: str) -> None:
    _keyring().set_password(_KR_SERVICE, account, value)


def _del_stored(account: str) -> None:
    try:
        _keyring().delete_password(_KR_SERVICE, account)
    except Exception:
        pass


def vault_url() -> str:
    explicit = os.environ.get("AITHER_SECRETS_URL") or _get_stored(_KR_URL_ACCOUNT)
    if explicit:
        return explicit.rstrip("/")
    # Operator holds the master key → local fleet vault; otherwise a logged-in user
    # reaches the vault through the public gateway (override with AITHER_GATEWAY_URL).
    if _master_key():
        return _DEFAULT_URL
    return os.environ.get("AITHER_GATEWAY_URL", _PUBLIC_GATEWAY).rstrip("/")


def _master_key() -> Optional[str]:
    # env override wins (useful for CI); otherwise the sealed keychain entry.
    return os.environ.get("AITHER_INTERNAL_SECRET") or _get_stored(_KR_KEY_ACCOUNT)


def is_setup() -> bool:
    return bool(_get_stored(_KR_KEY_ACCOUNT))


# ── tier gate: live vault vs local-only ──────────────────────────────────────
# The live AitherSecrets vault is reachable only for (a) the vault operator — a
# machine that holds the master key (this fleet host) — or (b) an end user who is
# logged in AND on a paid tier (subscription). Everyone else stays LOCAL ONLY: an
# encrypted keyring on this machine (~/.aither/secrets.enc), never the live vault.

def _logged_in() -> bool:
    try:
        from adk.shell.auth import AuthStore
        return bool(AuthStore.get_active_token())  # already checks token expiry
    except Exception:
        return False


def _paid_tier() -> bool:
    """True when the resolved license is a paid tier (rank >= STARTER)."""
    try:
        from adk.licensing import get_license_manager, _TIER_ORDER, Tier
        tier = get_license_manager().license.tier
        return _TIER_ORDER.get(tier, 0) >= _TIER_ORDER.get(Tier.STARTER, 1)
    except Exception:
        return False


def remote_mode() -> tuple[bool, str]:
    """Return (use_live_vault, human_reason). Local-only unless operator or
    logged-in+subscribed."""
    if _master_key():
        return True, "operator (vault master key on this machine)"
    if not _logged_in():
        return False, "not logged in — secrets stay local (run: adk login)"
    if not _paid_tier():
        return False, "free tier — secrets stay local; a subscription unlocks the cloud vault"
    return True, "logged in + subscription"


def _use_remote() -> bool:
    return remote_mode()[0]


# ── PIN / session unlock ─────────────────────────────────────────────────────

def _pin_required() -> bool:
    return bool(_get_stored(_KR_PIN_ACCOUNT))


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(("aither-vault:" + pin).encode()).hexdigest()


def _session_valid() -> bool:
    try:
        data = json.loads(_SESSION_FILE.read_text())
        return float(data.get("expires", 0)) > time.time()
    except Exception:
        return False


def _open_session() -> None:
    _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SESSION_FILE.write_text(json.dumps({"expires": time.time() + _SESSION_TTL_SECONDS}))


def _clear_session() -> None:
    try:
        _SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def _ensure_unlocked(now: float = 0.0) -> bool:
    """Gate a value reveal behind the PIN, if one is configured. Returns True when
    it is safe to reveal. Prompts for the PIN if needed and caches a short session.
    """
    if not _pin_required():
        return True
    if _session_valid():
        return True
    stored = _get_stored(_KR_PIN_ACCOUNT)
    for _ in range(3):
        entered = getpass.getpass("Vault PIN: ")
        if _hash_pin(entered) == stored:
            _open_session()
            return True
        print("  Incorrect PIN.")
    return False


# ── vault HTTP ───────────────────────────────────────────────────────────────

def _client():
    import httpx
    key = _master_key()
    headers = {"Content-Type": "application/json"}
    if key:
        # Operator path — the master key authorizes system-wide vault access.
        headers["X-API-Key"] = key
    else:
        # End-user path — the adk login bearer scopes access to their tenant.
        from adk.shell.auth import AuthStore
        tok = AuthStore.get_active_token()
        if not tok:
            raise RuntimeError("Not authorized for the live vault (not logged in).")
        headers["Authorization"] = f"Bearer {tok}"
    return httpx.Client(
        base_url=vault_url(), headers=headers, verify=tls_verify(), timeout=15,
    )


# -- local encrypted keyring backend (the free / offline path) ----------------

def _local_all() -> dict:
    from adk.builtin_tools import _load_secrets
    return dict(_load_secrets())


def _local_set(name: str, value: str) -> bool:
    from adk.builtin_tools import _load_secrets, _save_secrets
    data = _load_secrets()
    data[name] = value
    _save_secrets(data)
    return True


# -- categorization: turn a flat 300+ list into navigable groups ---------------
# The vault has no single clean owner field, so we derive a category from the
# scope prefix (platform::/tenant::/… — the real owner axis) when present, and
# fall back to naming/purpose conventions for the large flat legacy pile.
import re as _re

_PROVIDER_RE = _re.compile(
    r"^(cloudflare|aws|openai|anthropic|github|gitlab|stripe|solana|vast|runpod|gcp|google|"
    r"azure|linkedin|discord|deepseek|hf|hugging|proton|slack|twilio|sendgrid|groq|"
    r"together|replicate|fireworks|mistral|cohere|perplexity|elevenlabs|notion)[_:]?", _re.I)
_INFRA_RE = _re.compile(r"^(postgres|minio|redis|smtp|mail|tunnel_wg|wg_|s3_|db_|database)", _re.I)


def _categorize(item: dict) -> str:
    n = item.get("name", "")
    low = n.lower()
    # Explicit scope prefix (scope::id::name or scope/id/name) — the real owner axis.
    m = _re.match(r"^(platform|tenant|workspace|user)[:/]", low)
    if m:
        return {"platform": "Platform", "tenant": "Tenant", "workspace": "Workspace",
                "user": "User"}[m.group(1)]
    if n.startswith("MESH__") or low.startswith("optiplex"):
        return "Hosts & mesh"
    if n.endswith("_MTLS_ISSUED") or "mtls" in low:
        return "Service certs (mTLS)"
    # A NAMED external provider always wins (google_client_id, CLOUDFLARE_…).
    if _PROVIDER_RE.match(n):
        return "Provider keys"
    # Aither-internal wins over the generic _token/_api_key suffix (AITHER_*_TOKEN
    # is internal, not a third-party key).
    if _re.match(r"^aither", low):
        return "Aither services"
    if (low.endswith("_client_id") or low.endswith("_client_secret")
            or low.endswith("_api_key") or low.endswith("_token")):
        return "Provider keys"
    if _INFRA_RE.match(n) or low.endswith("_password"):
        return "Infrastructure"
    return "Other / misc"


# Stable display order (buckets not listed here sort after, alphabetically).
CATEGORY_ORDER = ["User", "Workspace", "Tenant", "Platform", "Provider keys",
                  "Aither services", "Infrastructure", "Service certs (mTLS)",
                  "Hosts & mesh", "Other / misc"]

# ── scope tiers ──────────────────────────────────────────────────────────────
# A local user / agent should NOT be buried in platform/system secrets by default.
# Each category maps to a scope tier; the named views below are what the UI/CLI
# expose, and the default view ("mine") hides platform/system/provider/tenant —
# they're opt-in via the scope selector. For a real end user the vault already
# returns only their token-scoped secrets, so the same views apply to what they see.
_CATEGORY_SCOPE = {
    "User": "mine", "Workspace": "mine", "Hosts & mesh": "mine", "Other / misc": "mine",
    "Tenant": "tenant",
    "Platform": "platform", "Aither services": "platform",
    "Infrastructure": "platform", "Service certs (mTLS)": "platform",
    "Provider keys": "provider",
}
SCOPE_VIEWS = {
    "mine":      {"mine"},
    "tenant":    {"tenant"},
    "platform":  {"platform"},
    "providers": {"provider"},
    "all":       {"mine", "tenant", "platform", "provider"},
}
SCOPE_ORDER = ["mine", "tenant", "providers", "platform", "all"]
DEFAULT_SCOPE = "mine"


def _scope_of(category: str) -> str:
    return _CATEGORY_SCOPE.get(category, "mine")


# ── scope/owner overlay (non-destructive organization) ───────────────────────
# The vault has no owner field and everything is read by name, so we do NOT rename
# secrets to organize them. Instead an OVERLAY assigns scope/owner per name:
#   1. a bundled default map (adk/toolpacks/vault/scope_map.json), then
#   2. the user's own overrides (~/.aither/vault-scope.json) — what `adk vault
#      scope` / the panel's re-file control write.
# The overlay only affects how the lockbox GROUPS a secret; the secret itself is
# never touched. Valid scope tiers: mine / workspace / tenant / provider / platform / host.
_VALID_SCOPES = {"mine", "workspace", "tenant", "provider", "platform", "host"}
_USER_SCOPE_FILE = Path.home() / ".aither" / "vault-scope.json"
_BUNDLED_SCOPE_FILE = Path(__file__).resolve().parent / "toolpacks" / "vault" / "scope_map.json"
_scope_overlay_cache: Optional[tuple] = None  # (mtime_signature, merged_dict)


def _load_scope_overlay() -> dict:
    # Cache keyed on the files' mtimes so an external edit (or the panel's re-file
    # write) is picked up without a restart, but we don't re-parse on every call.
    global _scope_overlay_cache
    sig = tuple((str(p), p.stat().st_mtime if p.exists() else 0)
                for p in (_BUNDLED_SCOPE_FILE, _USER_SCOPE_FILE))
    if _scope_overlay_cache is not None and _scope_overlay_cache[0] == sig:
        return _scope_overlay_cache[1]
    merged: dict = {}
    for p in (_BUNDLED_SCOPE_FILE, _USER_SCOPE_FILE):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for name, ent in data.items():
                    if name.startswith("_"):
                        continue  # _comment and other metadata keys
                    if isinstance(ent, str):
                        ent = {"scope": ent}
                    if isinstance(ent, dict):
                        merged.setdefault(name, {}).update(ent)
        except Exception:
            pass
    _scope_overlay_cache = (sig, merged)
    return merged


def set_scope(name: str, scope: str, owner: str = "") -> bool:
    """Persist a per-secret scope/owner override to the user overlay. Non-destructive
    — the secret's name and value are untouched."""
    global _scope_overlay_cache
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {sorted(_VALID_SCOPES)}")
    m = {}
    try:
        m = json.loads(_USER_SCOPE_FILE.read_text(encoding="utf-8"))
        if not isinstance(m, dict):
            m = {}
    except Exception:
        m = {}
    ent = m.get(name) if isinstance(m.get(name), dict) else {}
    ent["scope"] = scope
    if owner:
        ent["owner"] = owner
    m[name] = ent
    _USER_SCOPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USER_SCOPE_FILE.write_text(json.dumps(m, indent=2, sort_keys=True), encoding="utf-8")
    _scope_overlay_cache = None  # invalidate
    return True


# -- backend-routed operations (pick live vault OR local keyring) -------------

def list_secrets() -> list[dict]:
    if not _use_remote():
        out = [{"name": k, "type": "local", "store": "keyring"}
               for k in sorted(_local_all(), key=str.lower)]
    else:
        with _client() as c:
            r = c.get("/secrets")
            r.raise_for_status()
            data = r.json()
        if isinstance(data, dict):
            data = data.get("secrets", [])
        out = []
        for item in data:
            if isinstance(item, str):
                out.append({"name": item})
            elif isinstance(item, dict) and item.get("name"):
                out.append(item)
        out.sort(key=lambda d: d["name"].lower())
    overlay = _load_scope_overlay()
    for item in out:
        item["category"] = _categorize(item)
        ov = overlay.get(item["name"], {})
        item["scope"] = ov.get("scope") or _scope_of(item["category"])
        if ov.get("owner"):
            item["owner"] = ov["owner"]
        # A redundant duplicate can be marked alias_of a canonical name — the secret
        # is NOT deleted (services still read it), it's just collapsed out of the view.
        if ov.get("alias_of"):
            item["alias_of"] = ov["alias_of"]
    return out


def get_secret(name: str) -> Optional[str]:
    if not _use_remote():
        return _local_all().get(name)
    with _client() as c:
        r = c.get(f"/secrets/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, str) else data.get("value")


def set_secret(name: str, value: str, secret_type: str = "generic") -> bool:
    if not _use_remote():
        return _local_set(name, value)
    with _client() as c:
        r = c.post(
            "/secrets",
            json={
                "name": name,
                "value": value,
                "secret_type": secret_type,
                "access_level": "internal",
                "allowed_services": [],
                "expires_in_days": None,
            },
        )
    return r.status_code in (200, 201)


def vault_reachable() -> tuple[bool, str]:
    # In local-only mode there is no live vault to reach — report the local store.
    if not _use_remote():
        return True, f"local keyring ({len(_local_all())} secrets)"
    try:
        import httpx
        with httpx.Client(verify=tls_verify(), timeout=8) as c:
            r = c.get(vault_url() + "/health")
            if r.status_code == 200:
                return True, r.json().get("service", "ok")
            return False, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)


# ── presentation helpers ─────────────────────────────────────────────────────

def _mask(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return value[:1] + "*" * (len(value) - 4) + value[-3:]


def _to_clipboard(value: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(value)
        return True
    except Exception:
        return False


# ── command handlers (called from adk/cli.py) ────────────────────────────────

def cmd_setup(args) -> int:
    from_env = getattr(args, "from_env", False)
    url = getattr(args, "url", None) or _DEFAULT_URL

    if from_env:
        # Pull the master key out of the fleet .env once, then seal it away.
        env_path = getattr(args, "env_file", None) or _find_env()
        key = _read_env_value(env_path, "AITHER_INTERNAL_SECRET") if env_path else None
        if not key:
            print("Could not find AITHER_INTERNAL_SECRET in .env. "
                  "Pass --env-file, or omit --from-env to paste the key.")
            return 1
    else:
        key = getpass.getpass("Vault master key (X-API-Key): ").strip()
        if not key:
            print("No key entered.")
            return 1

    _set_stored(_KR_URL_ACCOUNT, url.rstrip("/"))
    _set_stored(_KR_KEY_ACCOUNT, key)

    # Verify the sealed key actually authenticates against the live vault.
    os.environ.pop("AITHER_INTERNAL_SECRET", None)  # force use of the sealed copy
    try:
        n = len(list_secrets())
    except Exception as exc:
        _del_stored(_KR_KEY_ACCOUNT)
        print(f"Key stored but it did NOT authenticate against {url}: {exc}")
        print("Nothing sealed. Check the key/URL and retry.")
        return 1

    print(f"Vault sealed into the OS keychain — {n} secrets reachable at {url}.")
    print("The master key is now encrypted at rest by your OS login; no .env needed.")

    if getattr(args, "pin", False):
        while True:
            p1 = getpass.getpass("Set a PIN (guards value reveals): ")
            p2 = getpass.getpass("Confirm PIN: ")
            if p1 and p1 == p2:
                _set_stored(_KR_PIN_ACCOUNT, _hash_pin(p1))
                print("PIN set. Value reveals will prompt for it (15-min sessions).")
                break
            print("  PINs didn't match / empty — try again.")
    print("\nTry:  adk vault get OPTIPLEX_7090_ADMIN_PASSWORD --copy")
    return 0


def cmd_status(args) -> int:
    remote, reason = remote_mode()
    ok, detail = vault_reachable()
    if remote:
        print(f"Mode          : LIVE VAULT — {reason}")
        print(f"Lockbox setup : {'yes (key sealed in OS keychain)' if is_setup() else 'using adk login token'}")
        print(f"Vault URL     : {vault_url()}")
        print(f"Vault health  : {'reachable — ' + detail if ok else 'UNREACHABLE — ' + detail}")
    else:
        print(f"Mode          : LOCAL ONLY — {reason}")
        print(f"Store         : {detail}")
    print(f"PIN gate      : {'on' if _pin_required() else 'off'}")
    try:
        print(f"Secrets       : {len(list_secrets())}")
    except Exception as exc:
        print(f"Secrets       : error — {exc}")
    return 0 if ok else 1


def cmd_ls(args) -> int:
    flt = (getattr(args, "filter", None) or "").lower()
    scope = getattr(args, "scope", None) or DEFAULT_SCOPE
    try:
        items = list_secrets()
    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    counts = {}
    for i in items:
        counts[i.get("scope", "mine")] = counts.get(i.get("scope", "mine"), 0) + 1
    if flt:  # a name search crosses every scope
        shown = [i for i in items if flt in i["name"].lower()]
        header = f"{len(shown)} secret(s) matching {flt!r} (all scopes)"
    else:
        allowed = SCOPE_VIEWS.get(scope, SCOPE_VIEWS["mine"])
        shown = [i for i in items if i.get("scope", "mine") in allowed]
        others = ", ".join(f"{label}={counts.get(tier, 0)}"
                           for label, tier in (("tenant", "tenant"), ("providers", "provider"),
                                               ("platform", "platform"))
                           if counts.get(tier, 0))
        header = (f"{len(shown)} secret(s) in scope '{scope}' "
                  f"(of {len(items)}; other scopes: {others})")
    print(f"\n{header}")
    print("=" * 64)
    # Group by category for readability.
    by_cat = {}
    for i in shown:
        by_cat.setdefault(i.get("category", "Other / misc"), []).append(i)
    cats = sorted(by_cat, key=lambda c: (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99, c))
    for cat in cats:
        print(f"\n  {cat}  ({len(by_cat[cat])})")
        for i in by_cat[cat]:
            meta = [m for m in (i.get("type"), f"v{i['version']}" if i.get("version") else "") if m]
            suffix = f"   ({', '.join(meta)})" if meta else ""
            print(f"    {i['name']}{suffix}")
    if not flt and scope != "all":
        print(f"\n  (showing '{scope}'. Use --scope all|tenant|platform|providers to see more.)")
    return 0


def cmd_get(args) -> int:
    name = args.name
    show = getattr(args, "show", False)
    copy = getattr(args, "copy", False) or not show  # clipboard is the default
    if show and not _ensure_unlocked():
        print("Locked — PIN required to reveal a value.")
        return 1
    try:
        value = get_secret(name)
    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    if value is None:
        print(f"No secret named '{name}'.  Try:  adk vault search {name}")
        return 1
    copied = _to_clipboard(value) if copy else False
    if show:
        print(value)
    else:
        tail = "copied to clipboard" if copied else "clipboard unavailable — use --show"
        print(f"{name}: {_mask(value)}  ({len(value)} chars — {tail})")
    return 0


def cmd_search(args) -> int:
    term = args.term.lower()
    try:
        items = list_secrets()
    except Exception as exc:
        print(f"Error: {exc}")
        return 1
    hits = [i for i in items if term in i["name"].lower()]
    if not hits:
        print(f"No secret name contains '{args.term}'.")
        return 1
    print(f"\n{len(hits)} match(es):")
    for i in hits:
        print(f"  {i['name']}")
    return 0


def cmd_rotate(args) -> int:
    name = args.name
    length = getattr(args, "length", 28) or 28
    show = getattr(args, "show", False)
    try:
        existing = get_secret(name)
    except Exception as exc:
        print(f"Error reading '{name}': {exc}")
        return 1
    if existing is None:
        print(f"No secret named '{name}' to rotate.  (Use adk vault set to create.)")
        return 1
    if not _ensure_unlocked():
        print("Locked — PIN required to rotate.")
        return 1
    new_value = _secrets.token_urlsafe(length)[:length]
    try:
        ok = set_secret(name, new_value)
    except Exception as exc:
        print(f"Error writing new value: {exc}")
        return 1
    if not ok:
        print("Vault rejected the write — nothing changed.")
        return 1
    copied = _to_clipboard(new_value)
    print(f"Rotated '{name}'  (old {_mask(existing)} -> new {_mask(new_value)})")
    print(f"  {'new value copied to clipboard' if copied else 'clipboard unavailable'}")
    if show:
        print(f"  new value: {new_value}")
    print("  NOTE: update any service still using the old value.")
    return 0


def cmd_lock(args) -> int:
    _clear_session()
    if getattr(args, "forget", False):
        _del_stored(_KR_KEY_ACCOUNT)
        _del_stored(_KR_PIN_ACCOUNT)
        print("Master key and PIN wiped from the OS keychain. Re-run setup to use the vault again.")
    else:
        print("Session locked. PIN will be required again on the next reveal.")
    return 0


def _console_up(port: int) -> bool:
    import urllib.request
    import urllib.error
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/ui", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # any HTTP response = something is serving
    except Exception:
        return False


def cmd_gui(args) -> int:
    """Open the vault in a browser — one command, nothing to remember. Makes the
    console UI + vault pack the defaults, starts a local server if one isn't
    already up, and opens the browser straight to the Vault panel."""
    import shutil
    import socket
    import subprocess
    import sys
    import time
    import webbrowser
    from adk.config import load_saved_config, save_saved_config

    # Ensure the console shows the vault out of the box.
    cfg = load_saved_config()
    packs = list(cfg.get("required_packs") or [])
    if "vault" not in packs:
        packs.append("vault")
    save_saved_config({"agent_ui": "console", "required_packs": packs})

    # Reuse a running console (remembered port) if it's still up; else pick a fresh
    # free port and remember it, so re-running gives you the same URL.
    port = getattr(args, "port", None) or cfg.get("vault_gui_port")
    if not (port and _console_up(int(port))):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        save_saved_config({"vault_gui_port": port})
        exe = shutil.which("adk-serve")
        cmd = ([exe] if exe else [sys.executable, "-m", "adk.server"]) + \
            ["--port", str(port), "--host", "127.0.0.1"]
        env = dict(os.environ)
        env.setdefault("AITHER_OFFLINE", "1")
        log = Path.home() / ".aither" / "vault-gui.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log, "ab")
        kw = {"stdout": logf, "stderr": logf, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_GROUP
        else:
            kw["start_new_session"] = True
        print("Starting the vault console…")
        subprocess.Popen(cmd, env=env, **kw)
        for _ in range(40):
            if _console_up(port):
                break
            time.sleep(0.75)
        else:
            print(f"Console didn't come up on port {port} — see {log}")
            return 1

    url = f"http://127.0.0.1:{port}/ui#k=local"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print(f"\n  Vault GUI:  {url}")
    print("  Opened in your browser — click the Vault tab. Bookmark it; it stays at this URL.")
    print("  (The console runs in the background; it's loopback-only.)")
    return 0


def cmd_scope(args) -> int:
    name = args.name
    scope = args.scope
    owner = getattr(args, "owner", "") or ""
    try:
        set_scope(name, scope, owner)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Filed '{name}' under scope '{scope}'"
          + (f" (owner: {owner})" if owner else "")
          + ".  The secret's name and value are unchanged.")
    return 0


def dispatch(args) -> int:
    sub = getattr(args, "vault_command", None)
    handlers = {
        "setup": cmd_setup, "status": cmd_status, "ls": cmd_ls, "list": cmd_ls,
        "get": cmd_get, "search": cmd_search, "rotate": cmd_rotate, "lock": cmd_lock,
        "gui": cmd_gui, "scope": cmd_scope,
    }
    fn = handlers.get(sub)
    if not fn:
        print("Usage: adk vault {gui|setup|status|ls|get|search|rotate|scope|lock}")
        print("Just want the GUI?  adk vault gui")
        return 1
    return fn(args)


# ── .env discovery (only used by `setup --from-env`) ─────────────────────────

def _find_env() -> Optional[str]:
    # Generic, host-agnostic discovery: an explicit override, then a .env in the
    # working directory or the user's ~/.aither config dir. No hardcoded paths.
    for p in (
        os.environ.get("AITHER_ENV_FILE"),
        ".env",
        str(Path.home() / ".aither" / ".env"),
    ):
        if p and Path(p).is_file():
            return p
    return None


def _read_env_value(path: str, key: str) -> Optional[str]:
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line[len(key) + 1:].strip().strip('"').strip("'")
    except Exception:
        return None
    return None
