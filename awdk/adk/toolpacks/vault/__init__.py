"""Vault Lockbox tool pack — exposes the on-demand secrets lockbox
(``adk.vault_lockbox``) as ``vault_*`` agent tools + a console UI panel.

This is the SAME lockbox the ``adk vault`` CLI drives: it talks to the live
AitherSecrets vault (:8111) using the master key sealed in the OS keychain, so
the console can browse / reveal / copy / rotate credentials without curl. Values
are masked + copied to the clipboard by default; a value is only ever returned
in-band when the caller passes ``reveal=True`` (the UI sets that on an explicit
user click), so an agent can't leak a credential into a transcript by accident.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("vault_pack")

PACK_ID = "vault"


def _lb():
    from adk import vault_lockbox
    return vault_lockbox


def vault_status() -> dict:
    """Report the backend mode (LIVE vault vs LOCAL-only), whether it is reachable,
    whether a PIN gate is on, and how many secrets are stored. The live vault is
    used only for the operator (master key present) or a logged-in subscriber;
    otherwise secrets stay in the local encrypted keyring. Reveals no values."""
    lb = _lb()
    remote, reason = lb.remote_mode()
    ok, detail = lb.vault_reachable()
    out = {
        "mode": "remote" if remote else "local",
        "reason": reason,
        "setup": lb.is_setup(),
        "vault_url": lb.vault_url() if remote else None,
        "reachable": ok,
        "detail": detail,
        "pin_gate": lb._pin_required(),
    }
    try:
        out["secret_count"] = len(lb.list_secrets())
    except Exception as exc:
        out["error"] = str(exc)
    if not remote:
        out["hint"] = "Secrets are local-only. Log in (adk login) with a subscription to use the cloud vault."
    return out


def vault_list(filter: str = "", scope: str = "mine") -> dict:
    """List secret NAMES and metadata from the vault (never values). ``filter``
    keeps only names containing that text. ``scope`` limits which tier is shown —
    "mine" (default; hides platform/system), "tenant", "providers", "platform", or
    "all". A non-empty ``filter`` searches ACROSS all scopes so you can always find a
    secret by name; an empty filter respects ``scope``. Also returns per-scope counts
    for the scope selector."""
    from collections import Counter
    lb = _lb()
    items = lb.list_secrets()
    aliases = sum(1 for i in items if i.get("alias_of"))
    live = [i for i in items if not i.get("alias_of")]  # hide collapsed duplicates
    counts = Counter(i.get("scope", "mine") for i in live)
    f = (filter or "").lower()
    if f:
        shown = [i for i in live if f in i["name"].lower()]  # search ignores scope
    else:
        allowed = lb.SCOPE_VIEWS.get(scope, lb.SCOPE_VIEWS["mine"])
        shown = [i for i in live if i.get("scope", "mine") in allowed]
    return {"count": len(shown), "scope": scope, "total": len(live),
            "hidden_duplicates": aliases,
            "scope_counts": dict(counts), "secrets": shown}


def vault_search(term: str) -> dict:
    """Find secrets whose NAME contains ``term`` (case-insensitive). Returns
    matching names only — never values."""
    lb = _lb()
    hits = [i["name"] for i in lb.list_secrets() if term.lower() in i["name"].lower()]
    return {"count": len(hits), "matches": hits}


def vault_get(name: str, reveal: bool = False) -> dict:
    """Fetch one secret by exact ``name``. By default returns a MASKED preview and
    copies the real value to the clipboard (so it never enters the transcript).
    Pass ``reveal=True`` ONLY when the user explicitly asks to see the value —
    then the plaintext is returned in ``value``."""
    lb = _lb()
    if reveal and not lb._ensure_unlocked():
        return {"name": name, "error": "locked — PIN required to reveal"}
    value = lb.get_secret(name)
    if value is None:
        return {"name": name, "error": "no such secret"}
    if reveal:
        return {"name": name, "value": value, "length": len(value)}
    copied = lb._to_clipboard(value)
    return {"name": name, "masked": lb._mask(value), "length": len(value), "copied": copied}


def vault_set_scope(name: str, scope: str, owner: str = "") -> dict:
    """Re-file a secret under a scope tier (mine/workspace/tenant/provider/platform/
    host) and optional owner. Non-destructive — writes an overlay; the secret's name
    and value are untouched. Use when a secret is filed in the wrong group."""
    lb = _lb()
    try:
        lb.set_scope(name, scope, owner)
        return {"ok": True, "name": name, "scope": scope, "owner": owner or None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def vault_rotate(name: str, length: int = 28, reveal: bool = False) -> dict:
    """Mint a fresh strong random value for an existing secret and store it in the
    vault. Returns masked old/new previews and copies the new value to the
    clipboard; pass ``reveal=True`` to also return the new plaintext. Note: this
    updates the vault only — a service still using the old value must be updated."""
    lb = _lb()
    existing = lb.get_secret(name)
    if existing is None:
        return {"name": name, "error": "no such secret to rotate"}
    if not lb._ensure_unlocked():
        return {"name": name, "error": "locked — PIN required to rotate"}
    import secrets as _secrets
    new_value = _secrets.token_urlsafe(length)[:length]
    if not lb.set_secret(name, new_value):
        return {"name": name, "error": "vault rejected the write — nothing changed"}
    copied = lb._to_clipboard(new_value)
    out = {
        "name": name,
        "old_masked": lb._mask(existing),
        "new_masked": lb._mask(new_value),
        "copied": copied,
        "note": "vault updated — update any service still using the old value",
    }
    if reveal:
        out["new_value"] = new_value
    return out


_TOOLS = [
    (vault_status, "vault_status", "Lockbox + vault health and secret count (no values)."),
    (vault_list, "vault_list", "List secret names + metadata (never values); optional name filter."),
    (vault_search, "vault_search", "Search secret names for a substring; returns names only."),
    (vault_get, "vault_get", "Get one secret — masked + clipboard by default, reveal=true to return the value."),
    (vault_rotate, "vault_rotate", "Mint a fresh strong value for a secret and store it in the vault."),
    (vault_set_scope, "vault_set_scope", "Re-file a secret's scope/owner (non-destructive overlay)."),
]


def register(registry) -> int:
    """Register the vault_* tools on the agent's tool registry."""
    n = 0
    for fn, name, desc in _TOOLS:
        try:
            registry.register(fn, name=name, description=desc)
            n += 1
        except TypeError:
            # Older registries take only the fn (name from __name__, desc from docstring).
            try:
                registry.register(fn)
                n += 1
            except Exception as exc:
                logger.debug("vault pack: skip %s (%s)", name, exc)
        except Exception as exc:
            logger.debug("vault pack: skip %s (%s)", name, exc)
    logger.info("Vault lockbox registered %d vault_* tools", n)
    return n
