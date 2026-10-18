"""`adk models pull <id> --uncensored`: the curated uncensored catalog, gated by your account.

The list lives on the platform, not in this package, and the platform only hands it to a
signed-in account whose adult-content gate is open: an explicit opt-in AND a completed
age verification. Child and teen accounts never receive it. So this module:

1. reads your saved adk credential (``adk login``); none -> it says so and stops;
2. asks the platform for YOUR catalog with that credential;
3. when the gate is closed, prints exactly which step is missing and where to finish it,
   and downloads nothing;
4. otherwise fetches the chosen GGUF with the same resumable, size- and sha256-checked
   downloader every other ``adk models pull`` uses.

Nothing here touches the network at import time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

#: Where a person completes each missing step.
SETTINGS_URL = "https://app.aitherium.com/settings/safety"
CATALOG_PATH = "/safety/config/user/uncensored-models"

#: gate reason -> the one line the person reads.
GATE_STEPS: Dict[str, str] = {
    "sign_in_required": "you are not signed in. Run: adk login",
    "age_verification_required": "your account has not completed age verification. "
                                 f"Verify your age at {SETTINGS_URL}",
    "opt_in_required": "your age is verified, but uncensored content is switched off. "
                       f"Turn it on at {SETTINGS_URL}",
    "account_restricted": "this account cannot see uncensored models (family child or "
                          "teen account).",
    "unavailable": "your account's status could not be checked right now; nothing was "
                   "downloaded. Try again shortly.",
}

HttpGet = Callable[[str, str, float], Dict[str, Any]]


class UncensoredError(RuntimeError):
    """The catalog could not be obtained; the message is the line to print."""


@dataclass
class CatalogResult:
    """The platform's answer: entries, or the gate reason when there are none."""

    entries: List[Dict[str, Any]] = field(default_factory=list)
    gate: Optional[str] = None


def _saved_token() -> str:
    from adk.config import load_saved_config

    return str((load_saved_config() or {}).get("api_key") or "")


def _base_url() -> str:
    from adk.control_plane import genesis_data_base

    return genesis_data_base()


def _http_get_json(url: str, token: str, timeout: float) -> Dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "User-Agent": "awdk-models/1 (+https://aitherium.com)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- https base
        return json.loads(resp.read())


def step_message(gate: str) -> str:
    """The one line naming what is missing for ``gate``."""
    return GATE_STEPS.get(gate, f"the platform refused the catalog ({gate}). "
                                f"Check {SETTINGS_URL}")


def fetch_catalog(token: str = "", base_url: str = "", timeout: float = 15.0,
                  http_get: Optional[HttpGet] = None) -> CatalogResult:
    """Ask the platform for this account's uncensored catalog.

    Args:
        token: The adk credential. Default: the one ``adk login`` saved.
        base_url: The platform's Genesis base. Default: the control-plane bridge.
        timeout: Seconds per request.
        http_get: ``(url, token, timeout) -> dict`` seam for tests.

    Returns:
        ``CatalogResult`` with entries, or with ``gate`` set when the gate is closed
        (``sign_in_required`` when there is no credential or it was refused).

    Raises:
        UncensoredError: The platform could not be reached or answered nonsense.
    """
    token = token or _saved_token()
    if not token:
        return CatalogResult(gate="sign_in_required")
    url = (base_url or _base_url()).rstrip("/") + CATALOG_PATH
    try:
        body = (http_get or _http_get_json)(url, token, timeout)
    except Exception as exc:  # noqa: BLE001 -- offline / 401 / 5xx
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return CatalogResult(gate="sign_in_required")
        raise UncensoredError(f"could not reach your account ({type(exc).__name__}); "
                              "nothing was downloaded") from exc
    if not isinstance(body, dict) or not isinstance(body.get("entries"), list):
        raise UncensoredError("the platform answered without a catalog; nothing was downloaded")
    entries = [e for e in body["entries"] if isinstance(e, dict) and e.get("id")]
    if not entries:
        return CatalogResult(gate=str(body.get("gate") or "unavailable"))
    return CatalogResult(entries=entries)


def as_download_model(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The catalog entry in the shape ``download.fetch_model`` reads."""
    url = str(entry.get("gguf_url") or "")
    if not url.startswith("https://"):
        raise UncensoredError(f"'{entry.get('id')}' has no https download URL")
    return {"file": str(entry["file"]), "urls": [url],
            "size_bytes": int(entry["size_bytes"]) if entry.get("size_bytes") else None,
            "sha256": str(entry.get("sha256") or "")}


def pull(model_id: str, dest_dir: Any, say: Callable[[str], None], err: Callable[[str], None],
         *, token: str = "", base_url: str = "", http_get: Optional[HttpGet] = None,
         fetch_model: Optional[Callable[..., Any]] = None) -> int:
    """Pull one uncensored model for the signed-in account. -> process exit code.

    Never downloads unless the platform returned the entry for THIS account.
    """
    try:
        result = fetch_catalog(token=token, base_url=base_url, http_get=http_get)
    except UncensoredError as exc:
        err(f"adk models pull --uncensored: {exc}")
        return 1
    if result.gate:
        err(f"adk models pull --uncensored: {step_message(result.gate)}")
        return 1
    entry = next((e for e in result.entries if e.get("id") == model_id), None)
    if entry is None:
        err(f"adk models pull --uncensored: no model '{model_id}' in your catalog "
            f"(have: {', '.join(sorted(str(e['id']) for e in result.entries))})")
        return 1
    try:
        model = as_download_model(entry)
    except (UncensoredError, KeyError, ValueError) as exc:
        err(f"adk models pull --uncensored: malformed entry '{model_id}' ({exc})")
        return 1
    from .download import DownloadError
    if fetch_model is None:
        from .download import fetch_model as _fetch_model
        fetch_model = _fetch_model

    say(f"Pulling {model_id} ({entry.get('license', 'licence unknown')}) -> {dest_dir}")
    try:
        path, hashed = fetch_model(model, dest_dir, say=say)
    except DownloadError as exc:
        err(f"adk models pull --uncensored: {exc}")
        return 1
    say(f"Ready: {path}" + (" (size and sha256 verified)" if hashed else " (size verified)"))
    say("  It runs on your own machine. Serve it with llama.cpp, or point awnode/GobboNet "
        "at the file.")
    return 0


__all__ = ["CATALOG_PATH", "CatalogResult", "GATE_STEPS", "SETTINGS_URL", "UncensoredError",
           "as_download_model", "fetch_catalog", "pull", "step_message"]
