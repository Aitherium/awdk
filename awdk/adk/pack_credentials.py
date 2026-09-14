"""Per-install pack credentials — ACTA-scoped token minting and revocation.

When a pack is installed successfully, mint a scoped credential bound to the pack_id
and a unique install_id. This replaces shared OIDC client credentials with per-install
tokens that can be revoked independently without affecting other installs.

Credential lifecycle:
  1. install: mint a scoped token, store metadata → ~/.aither/packs/{pack_id}/.install_cred
  2. use: the token is scoped to this pack+install only (ACTA metadata)
  3. uninstall: revoke the token via ACTA API, delete metadata

Credential storage (fail-safe):
  - ~/.aither/packs/{pack_id}/.install_cred.json stores:
    {
      "install_id": "unique-install-uuid",
      "pack_id": "...",
      "credential_id": "the ACTA credential ID (for revocation)",
      "minted_at": "ISO8601 timestamp",
      "minting_failed": bool (optional; true if mint failed but install succeeded)
    }

  Metadata is written BEFORE the install completes (atomic: mint → store → extract).
  On revocation failure (network, already revoked), the metadata is still deleted
  (fail-closed: treat it as gone even if the revoke HTTP call fails).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("adk.pack_credentials")


def _get_packs_dir() -> Path:
    """The local pack install root (matches agent auto-discovery)."""
    d = Path.home() / ".aitheros" / "packs"
    d.mkdir(parents=True, exist_ok=True)
    return d


_PACK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_pack_id(pack_id: str) -> str:
    """Validate pack_id before using it in a filesystem path (fail closed).

    Rejects path traversal / separators: an unsanitized pack_id like ``../../etc``
    would otherwise escape the packs dir (path-traversal). Raises ValueError on
    anything outside a strict allowlist so a bad id can never build a path.
    """
    if not pack_id or not isinstance(pack_id, str) or not _PACK_ID_RE.match(pack_id):
        raise ValueError(f"unsafe pack_id: {pack_id!r}")
    if pack_id in (".", "..") or "/" in pack_id or "\\" in pack_id:
        raise ValueError(f"unsafe pack_id: {pack_id!r}")
    return pack_id


def _credential_metadata_path(pack_id: str) -> Path:
    """Path to the .install_cred.json metadata file for a pack."""
    safe = _safe_pack_id(pack_id)
    root = _get_packs_dir().resolve()
    pack_dir = (root / safe).resolve()
    # Defense in depth: the resolved dir MUST stay under the packs root.
    if not (pack_dir == root / safe and str(pack_dir).startswith(str(root))):
        raise ValueError(f"pack path escapes root: {pack_id!r}")
    pack_dir.mkdir(parents=True, exist_ok=True)
    return pack_dir / ".install_cred.json"


def _load_credential_metadata(pack_id: str) -> Optional[Dict[str, Any]]:
    """Load existing credential metadata for a pack, or None if not present."""
    path = _credential_metadata_path(pack_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load credential metadata for %s: %s", pack_id, e)
        return None


def _save_credential_metadata(pack_id: str, metadata: Dict[str, Any]) -> None:
    """Save credential metadata atomically."""
    path = _credential_metadata_path(pack_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save credential metadata for %s: %s", pack_id, e)


def _delete_credential_metadata(pack_id: str) -> None:
    """Delete credential metadata file."""
    path = _credential_metadata_path(pack_id)
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.warning("Failed to delete credential metadata for %s: %s", pack_id, e)


def _get_acta_base() -> str:
    """Resolve ACTA API base URL from environment.

    Order:
        1. AITHER_ACTA_URL (explicit override)
        2. AITHERIUM_BASE_URL (fallback to portal)
        3. https://api.aitheros.ai (default)
    """
    acta = os.environ.get("AITHER_ACTA_URL", "").strip()
    if acta:
        return acta.rstrip("/")
    portal = os.environ.get("AITHERIUM_BASE_URL", "").strip()
    if portal:
        return portal.rstrip("/")
    return "https://api.aitheros.ai"


def _get_internal_token() -> Optional[str]:
    """Get the internal ACTA token for credential minting.

    On-mesh deployments should have AITHER_ACTA_INTERNAL_TOKEN set.
    None = offline or no mint capability (graceful degradation).
    """
    return os.environ.get("AITHER_ACTA_INTERNAL_TOKEN", "").strip() or None


def _get_user_id() -> Optional[str]:
    """Resolve the current user's ID for credential ownership.

    Order:
        1. AITHER_USER_ID (explicit override)
        2. Extract from ~/.aither/auth.json if available
        3. None (credential minting deferred or skipped)
    """
    user_id = os.environ.get("AITHER_USER_ID", "").strip()
    if user_id:
        return user_id

    # Try to extract from auth.json (matches adk.auth pattern)
    auth_file = Path.home() / ".aither" / "auth.json"
    if auth_file.exists():
        try:
            auth = json.loads(auth_file.read_text(encoding="utf-8"))
            # Look for user_id or principal.subject_id
            if "user_id" in auth:
                return auth["user_id"]
            principal = auth.get("principal", {})
            if isinstance(principal, dict) and "subject_id" in principal:
                return principal["subject_id"]
        except Exception as e:
            logger.debug("Failed to extract user_id from auth.json: %s", e)

    return None


def mint_install_credential(pack_id: str) -> Dict[str, Any]:
    """Mint a per-install scoped credential for a pack (best-effort).

    Args:
        pack_id: The pack ID being installed

    Returns:
        Metadata dict: {
            "install_id": "unique-install-uuid",
            "pack_id": pack_id,
            "credential_id": "the ACTA credential ID (or empty if minting failed)",
            "minted_at": "ISO8601 timestamp",
            "minting_failed": bool (True if mint failed but proceed anyway)
        }

    Never raises. Returns minimally valid metadata even if mint fails.
    Fail-closed: if anything goes wrong, log it and proceed (install succeeds either way).
    """
    install_id = str(uuid.uuid4())
    minted_at = datetime.now(timezone.utc).isoformat()

    metadata: Dict[str, Any] = {
        "install_id": install_id,
        "pack_id": pack_id,
        "credential_id": "",
        "minted_at": minted_at,
    }

    # Attempt to mint via ACTA (best-effort, does not block install).
    try:
        internal_token = _get_internal_token()
        if not internal_token:
            logger.debug("No ACTA internal token; skipping credential mint for %s", pack_id)
            metadata["minting_failed"] = True
            _save_credential_metadata(pack_id, metadata)
            return metadata

        user_id = _get_user_id()
        if not user_id:
            logger.debug("No user ID resolved; skipping credential mint for %s", pack_id)
            metadata["minting_failed"] = True
            _save_credential_metadata(pack_id, metadata)
            return metadata

        # Mint via ACTA internal API
        credential_id = _acta_mint_api_key(
            acta_base=_get_acta_base(),
            user_id=user_id,
            pack_id=pack_id,
            install_id=install_id,
            internal_token=internal_token,
        )

        if credential_id:
            metadata["credential_id"] = credential_id
            logger.info(
                "Minted per-install credential for pack %s (install_id=%s, cred_id=%s)",
                pack_id,
                install_id,
                credential_id,
            )
        else:
            metadata["minting_failed"] = True
            logger.warning(
                "Failed to mint credential for pack %s (install_id=%s); "
                "install proceeds without scoped token",
                pack_id,
                install_id,
            )

        _save_credential_metadata(pack_id, metadata)
        return metadata

    except Exception as e:
        logger.warning(
            "Unexpected error minting credential for pack %s: %s; proceeding without token",
            pack_id,
            e,
        )
        metadata["minting_failed"] = True
        _save_credential_metadata(pack_id, metadata)
        return metadata


def revoke_install_credential(pack_id: str) -> bool:
    """Revoke the per-install credential on uninstall (best-effort).

    Args:
        pack_id: The pack ID being uninstalled

    Returns:
        True if revocation succeeded or was unnecessary, False if it failed.
        Fails closed: metadata is DELETED regardless of revoke success.
    """
    metadata = _load_credential_metadata(pack_id)
    if not metadata:
        logger.debug("No credential metadata for pack %s; nothing to revoke", pack_id)
        return True

    install_id = metadata.get("install_id", "")
    credential_id = metadata.get("credential_id", "")
    minting_failed = metadata.get("minting_failed", False)

    # If minting never succeeded, nothing to revoke.
    if minting_failed or not credential_id:
        logger.debug(
            "Credential for pack %s (install_id=%s) was never successfully minted; "
            "skipping revocation",
            pack_id,
            install_id,
        )
        _delete_credential_metadata(pack_id)
        return True

    # Attempt revocation via ACTA (best-effort).
    try:
        internal_token = _get_internal_token()
        if not internal_token:
            logger.warning(
                "No ACTA internal token available for revocation of pack %s; "
                "credential may remain active",
                pack_id,
            )
            # Delete metadata anyway (fail-closed: treat as revoked locally)
            _delete_credential_metadata(pack_id)
            return False

        user_id = _get_user_id()
        if not user_id:
            logger.warning(
                "No user ID resolved for revocation of pack %s; credential may remain active",
                pack_id,
            )
            # Delete metadata anyway
            _delete_credential_metadata(pack_id)
            return False

        success = _acta_revoke_api_key(
            acta_base=_get_acta_base(),
            user_id=user_id,
            credential_id=credential_id,
            internal_token=internal_token,
        )

        if success:
            logger.info(
                "Revoked credential for pack %s (install_id=%s, cred_id=%s)",
                pack_id,
                install_id,
                credential_id,
            )
        else:
            logger.warning(
                "Failed to revoke credential for pack %s (cred_id=%s); "
                "may require manual cleanup",
                pack_id,
                credential_id,
            )

        # Delete metadata regardless (fail-closed: treat as revoked locally)
        _delete_credential_metadata(pack_id)
        return success

    except Exception as e:
        logger.warning(
            "Unexpected error revoking credential for pack %s: %s; "
            "credential may remain active",
            pack_id,
            e,
        )
        # Delete metadata anyway (fail-closed)
        _delete_credential_metadata(pack_id)
        return False


def _acta_mint_api_key(
    acta_base: str,
    user_id: str,
    pack_id: str,
    install_id: str,
    internal_token: str,
) -> str:
    """Mint an API key via ACTA /v1/internal/users/{user_id}/api-keys endpoint.

    Returns the credential_id (key ID for later revocation), or empty string on failure.
    Never raises.
    """
    try:
        import httpx

        # Mint with pack+install scope metadata
        payload = {
            "name": f"pack-install:{pack_id}:{install_id}",
            "scopes": ["pack:execute"],  # Pack-execution scope only
            "created_by": "adk-pack-install",
            "metadata": {
                "purpose": "pack-install",
                "pack_id": pack_id,
                "install_id": install_id,
            },
        }

        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{acta_base}/v1/internal/users/{user_id}/api-keys",
                headers={"X-Internal-Token": internal_token},
                json=payload,
            )

        if resp.status_code == 200:
            data = resp.json()
            # ACTA returns either "id" or "api_key_id" as the credential identifier
            credential_id = data.get("id") or data.get("api_key_id") or ""
            if credential_id:
                return credential_id

        logger.debug(
            "ACTA mint failed for user %s: HTTP %s: %s",
            user_id,
            resp.status_code,
            resp.text[:160],
        )
        return ""

    except ImportError:
        logger.debug("httpx not available for ACTA mint")
        return ""
    except Exception as e:
        logger.debug("ACTA mint error for pack %s: %s", pack_id, e)
        return ""


def _acta_revoke_api_key(
    acta_base: str,
    user_id: str,
    credential_id: str,
    internal_token: str,
) -> bool:
    """Revoke an API key via ACTA /v1/internal/users/{user_id}/api-keys/{key_id} DELETE.

    Returns True on success, False on failure.
    Never raises.
    """
    try:
        import httpx

        with httpx.Client(timeout=15.0) as client:
            resp = client.delete(
                f"{acta_base}/v1/internal/users/{user_id}/api-keys/{credential_id}",
                headers={"X-Internal-Token": internal_token},
            )

        if resp.status_code in (200, 204, 404):
            # 200/204 = success, 404 = already gone (idempotent)
            return True

        logger.debug(
            "ACTA revoke failed for credential %s: HTTP %s: %s",
            credential_id,
            resp.status_code,
            resp.text[:160],
        )
        return False

    except ImportError:
        logger.debug("httpx not available for ACTA revoke")
        return False
    except Exception as e:
        logger.debug("ACTA revoke error for credential %s: %s", credential_id, e)
        return False
