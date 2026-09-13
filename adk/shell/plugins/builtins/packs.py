"""
ADK Shell Plugin: Packs Marketplace
Browse, purchase, and manage agent/skill/tool packs from the Elysium marketplace.
"""

import json
import logging
import os
import tarfile
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from adk.shell.plugins import SlashCommand

__all__ = ["sync_entitled_packs"]

logger = logging.getLogger(__name__)


def _safe_extract(tar: tarfile.TarFile, path: str) -> None:
    """Extract a tarball, rejecting any member that would escape ``path``
    (path-traversal / absolute-path / symlink-escape defense)."""
    base = os.path.realpath(path)
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(path, member.name))
        if not (target == base or target.startswith(base + os.sep)):
            raise ValueError(f"unsafe path in archive: {member.name}")
        if member.issym() or member.islnk():
            link_target = os.path.realpath(os.path.join(path, member.linkname))
            if not (link_target == base or link_target.startswith(base + os.sep)):
                raise ValueError(f"unsafe link in archive: {member.name}")
    # filter="data" (py3.12+) is the hardened extraction profile; our own check
    # above is the portable belt-and-braces for older runtimes.
    try:
        tar.extractall(path, filter="data")
    except TypeError:
        tar.extractall(path)


def _credential_hooks():
    """Return ``(mint_install_credential, revoke_install_credential)`` or ``(None, None)``.

    Per-install ACTA-scoped credentials (:mod:`adk.pack_credentials`) replace a
    shared client credential with a token bound to this pack_id + install_id, so
    one install can be revoked without affecting any other. Both functions are
    documented never-raise/best-effort, so a credential-plane outage can never
    block a pack install. The import is guarded so a trimmed build still installs.
    """
    try:
        from adk.pack_credentials import (
            mint_install_credential,
            revoke_install_credential,
        )
    except ImportError:  # credential plane absent (trimmed build)
        return None, None
    return mint_install_credential, revoke_install_credential


class AuthStore:
    """Simple auth token storage for marketplace API calls."""

    def __init__(self):
        self.token: Optional[str] = None
        self.tenant_id: Optional[str] = None

    def set_auth(self, token: str, tenant_id: str = None):
        """Store auth credentials."""
        self.token = token
        self.tenant_id = tenant_id

    def get_headers(self) -> Dict[str, str]:
        """Build auth headers for API calls."""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.tenant_id:
            headers["X-Tenant-ID"] = self.tenant_id
        return headers


class PacksPlugin(SlashCommand):
    """
    /packs — Browse and manage agent/skill/tool packs from marketplace.

    Subcommands:
      /packs list              List all published packs
      /packs browse            Browse with filters (--type, --search)
      /packs info <pack-id>    Show pack details
      /packs purchase <id>     Open Stripe checkout
      /packs install <id>      Download and extract pack
      /packs uninstall <id>    Remove a pack + revoke its per-install credential
      /packs activate <id>     Hot-reload packs on agent server (no download)
      /packs sync              Install every entitled pack not already present
      /packs library           Show purchased packs
      /packs help              Show help text
    """

    name = "packs"
    # `pack` is pack_customize's NAME and `store` is commerce's alias for a
    # Stripe storefront. Both were claimed here too, and since the loader
    # registers in glob order the LAST writer silently won — which made the
    # winner depend on a filename rather than on a decision.
    aliases = ["packstore"]
    category = "marketplace"

    def __init__(self):
        # Explicitly set the dataclass fields for the parent SlashCommand
        super().__init__(
            name="packs",
            description="Browse and manage agent/skill/tool packs from marketplace.",
            aliases=["packstore"],
        )
        self.auth = AuthStore()
        self._base_url = self._get_base_url()

    def _get_base_url(self) -> str:
        """Resolve Elysium base URL from env or fallback."""
        elysium_url = os.environ.get("AITHER_ELYSIUM_URL")
        if elysium_url:
            return elysium_url.rstrip("/")

        portal_url = os.environ.get("AITHER_PORTAL_URL")
        if portal_url:
            return portal_url.rstrip("/")

        return "https://api.aitherium.com"

    def _get_adk_server_url(self) -> str:
        """Resolve ADK agent server URL for hot-apply.

        Checks env vars AITHER_ADK_SERVER_URL, AITHER_ADK_URL, then defaults
        to localhost:8000 (standard adk serve port).
        """
        adk_url = os.environ.get("AITHER_ADK_SERVER_URL")
        if adk_url:
            return adk_url.rstrip("/")

        adk_url = os.environ.get("AITHER_ADK_URL")
        if adk_url:
            return adk_url.rstrip("/")

        return "http://localhost:8000"

    def _reload_packs_on_server(self, adk_url: str) -> dict:
        """POST to /agent/packs/reload on the agent server (hot-apply).

        Best-effort: returns {"status": "ok", "tools_added": N} on success,
        or {"status": "unavailable", "message": "..."} if server is unreachable.
        Never raises; fails gracefully for offline agent servers.
        """
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(f"{adk_url}/agent/packs/reload")
                if resp.status_code == 404:
                    return {
                        "status": "unavailable",
                        "message": "agent server does not support hot-apply (404)",
                    }
                if resp.status_code >= 200 and resp.status_code < 300:
                    data = resp.json()
                    return {
                        "status": "ok",
                        "tools_added": data.get("tools_added", 0),
                        "message": data.get("message", "pack reloaded"),
                    }
                return {
                    "status": "unavailable",
                    "message": f"agent server error (HTTP {resp.status_code})",
                }
        except httpx.ConnectError:
            return {
                "status": "unavailable",
                "message": f"no running agent server at {adk_url}",
            }
        except Exception as e:  # noqa: BLE001 — best-effort only
            return {
                "status": "unavailable",
                "message": f"hot-apply failed: {type(e).__name__}: {e}",
            }

    def execute(self, args: List[str], **kwargs) -> str:
        """Main entry point for /packs command."""
        if not args:
            return self._show_list()

        command = args[0].lower()

        if command in ("list", "ls"):
            return self._show_list()
        elif command == "browse":
            return self._browse(args[1:])
        elif command == "info":
            if len(args) < 2:
                return "ERROR: /packs info requires <pack-id>"
            return self._show_info(args[1])
        elif command == "purchase":
            if len(args) < 2:
                return "ERROR: /packs purchase requires <pack-id>"
            return self._purchase(args[1])
        elif command == "install":
            if len(args) < 2:
                return "ERROR: /packs install requires <pack-id>"
            return self._install(args[1], args[2:])
        elif command == "activate":
            # activate takes no required args; optional pack_id is ignored
            # (server reloads all discovered packs regardless)
            return self._activate_packs(args[1:] if len(args) > 1 else [])
        elif command in ("uninstall", "remove", "rm"):
            if len(args) < 2:
                return "ERROR: /packs uninstall requires <pack-id>"
            return self._uninstall(args[1])
        elif command == "sync":
            return self._sync(args[1:])
        elif command == "library":
            return self._show_library()
        elif command == "help":
            return self._show_help()
        else:
            return f"ERROR: Unknown command '/packs {command}'. Try '/packs help'"

    def _show_list(self) -> str:
        """List all published packs."""
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self._base_url}/api/marketplace/packs",
                    headers=self.auth.get_headers(),
                )
                resp.raise_for_status()
                packs = resp.json().get("packs", [])

            if not packs:
                return "No packs found in marketplace."

            return self._format_packs_table(packs)

        except httpx.HTTPError as e:
            return f"ERROR: Failed to fetch packs: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def _browse(self, args: List[str]) -> str:
        """Browse packs with optional filters."""
        pack_type = None
        search_query = None

        i = 0
        while i < len(args):
            if args[i] == "--type" and i + 1 < len(args):
                pack_type = args[i + 1]
                i += 2
            elif args[i] == "--search" and i + 1 < len(args):
                search_query = args[i + 1]
                i += 2
            else:
                i += 1

        try:
            params = {}
            if pack_type:
                params["type"] = pack_type
            if search_query:
                params["search"] = search_query

            query_string = urlencode(params) if params else ""
            url = f"{self._base_url}/api/marketplace/packs"
            if query_string:
                url = f"{url}?{query_string}"

            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers=self.auth.get_headers())
                resp.raise_for_status()
                result = resp.json()
                packs = result.get("packs", [])

            if not packs:
                filters = []
                if pack_type:
                    filters.append(f"type={pack_type}")
                if search_query:
                    filters.append(f"search={search_query}")
                filter_str = ", ".join(filters) if filters else "none"
                return f"No packs found matching filters: {filter_str}"

            return self._format_packs_table(packs)

        except httpx.HTTPError as e:
            return f"ERROR: Failed to browse packs: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def _show_info(self, pack_id: str) -> str:
        """Show detailed pack information."""
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self._base_url}/api/marketplace/packs/{pack_id}",
                    headers=self.auth.get_headers(),
                )
                resp.raise_for_status()
                pack = resp.json()

            return self._format_pack_detail(pack)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"ERROR: Pack '{pack_id}' not found."
            return f"ERROR: HTTP {e.response.status_code}: {e}"
        except httpx.HTTPError as e:
            return f"ERROR: Failed to fetch pack info: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def _purchase(self, pack_id: str) -> str:
        """Open Stripe checkout in browser."""
        try:
            checkout_url = f"{self._base_url}/checkout/packs/{pack_id}"
            webbrowser.open(checkout_url)
            return f"Opening checkout for pack '{pack_id}' in browser...\n{checkout_url}"

        except Exception as e:
            return f"ERROR: Failed to open checkout: {e}"

    def _activate_packs(self, args: List[str]) -> str:
        """Hot-reload packs on the agent server without downloading.

        If pack_id is provided, it's included in the message for context.
        The server reloads all discovered packs regardless.
        """
        adk_url = self._get_adk_server_url()
        result = self._reload_packs_on_server(adk_url)

        if result["status"] == "ok":
            tools_added = result.get("tools_added", 0)
            if tools_added > 0:
                return f"✓ Hot-applied packs: {tools_added} tools added to agent"
            return f"✓ Packs reloaded ({result.get('message', 'no new tools')})"
        else:
            return f"⚠ Could not hot-apply to agent server: {result['message']}\n  packs will apply on next agent restart"

    def _install(self, pack_id: str, args: List[str]) -> str:
        """Download and optionally extract pack bundle."""
        extract = "--extract" in args or "-x" in args

        try:
            # Get pack info first to determine filename
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self._base_url}/api/marketplace/packs/{pack_id}",
                    headers=self.auth.get_headers(),
                )
                resp.raise_for_status()
                pack = resp.json()

            pack_name = pack.get("name", pack_id).lower().replace(" ", "-")
            filename = f"{pack_name}.tar.gz"

            # Download bundle
            download_url = (
                f"{self._base_url}/api/agent-builder/build/{pack_id}/download"
            )
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(download_url, headers=self.auth.get_headers())
                resp.raise_for_status()

                with open(filename, "wb") as f:
                    f.write(resp.content)

            result = f"Downloaded pack to '{filename}'"

            if extract:
                extract_dir = pack_name
                os.makedirs(extract_dir, exist_ok=True)

                with tarfile.open(filename, "r:gz") as tar:
                    _safe_extract(tar, extract_dir)

                result += f"\nExtracted to directory '{extract_dir}'"

                # Best-effort: trigger hot-apply on the running agent server.
                # If server is unreachable, degrade gracefully.
                adk_url = self._get_adk_server_url()
                reload_result = self._reload_packs_on_server(adk_url)
                if reload_result["status"] == "ok":
                    tools_added = reload_result.get("tools_added", 0)
                    if tools_added > 0:
                        result += f"\n✓ Hot-applied: {tools_added} tools added"
                    else:
                        result += "\n✓ Pack reloaded (tools available on next agent restart)"
                else:
                    # No error — just informational
                    result += f"\n  → {reload_result['message']} — will apply on next start"

            return result

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"ERROR: Pack '{pack_id}' not found or not available for download."
            return f"ERROR: HTTP {e.response.status_code}: {e}"
        except httpx.HTTPError as e:
            return f"ERROR: Failed to download pack: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    @staticmethod
    def _packs_dir() -> Path:
        """The local pack install root (matches agent auto-discovery)."""
        d = Path(os.path.expanduser("~")) / ".aitheros" / "packs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _uninstall(self, pack_id: str) -> str:
        """Remove an installed pack, revoking its per-install credential FIRST.

        Install mints a credential scoped to this pack + install_id
        (:mod:`adk.pack_credentials`). Without this verb the only revoke path was
        a reinstall, so removing a pack by hand left a LIVE credential whose
        metadata was gone — unrevocable. Order matters: revoke while the metadata
        is still readable, then delete the directory.
        """
        import shutil

        packs_root = self._packs_dir()
        # Resolve and confine: a pack_id like "../../.ssh" must never escape the
        # pack root into an arbitrary rmtree.
        dest = (packs_root / pack_id).resolve()
        root = packs_root.resolve()
        if dest == root or root not in dest.parents:
            return f"ERROR: refusing to remove {pack_id!r} — outside the pack root"

        if not dest.exists():
            return f"Pack '{pack_id}' is not installed (nothing to remove)."

        _, revoke_install_credential = _credential_hooks()
        revoked = "not applicable"
        if revoke_install_credential is not None:
            try:
                revoked = "revoked" if revoke_install_credential(pack_id) else "revoke failed"
            except Exception as e:  # noqa: BLE001 — documented best-effort
                logger.warning("pack %s: credential revoke raised: %s", pack_id, e)
                revoked = f"revoke error ({type(e).__name__})"

        try:
            shutil.rmtree(dest)
        except OSError as e:
            return f"ERROR: credential {revoked}, but failed to remove {dest}: {e}"

        return f"Removed pack '{pack_id}' from {packs_root} (credential: {revoked})"

    def _sync(self, args: List[str]) -> str:
        """Install every entitled pack that isn't already present locally.

        Polls the customer's active licenses from the portal, then for each
        entitled pack not yet in ``~/.aitheros/packs/`` downloads the artifact,
        verifies its Ed25519 signature, and extracts it. A portal purchase thus
        converges onto this node with one command (or on `adk up`), no manual
        per-pack install.
        """
        dry_run = "--dry-run" in args or "-n" in args
        packs_root = self._packs_dir()

        # 1. Entitlements: the customer's active licenses (listing_id == pack id).
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(
                    f"{self._base_url}/v1/marketplace/license/mine",
                    headers=self.auth.get_headers(),
                )
                resp.raise_for_status()
                licenses = resp.json().get("licenses", [])
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                return "ERROR: Not authenticated. Run /auth with your portal token first."
            return f"ERROR: Could not fetch entitlements: HTTP {e.response.status_code}"
        except Exception as e:  # noqa: BLE001
            return f"ERROR: Could not reach the portal: {type(e).__name__}: {e}"

        entitled = sorted({
            lic.get("listing_id", "")
            for lic in licenses
            if lic.get("status") == "active" and lic.get("listing_id")
        })
        if not entitled:
            return "No entitled packs. Buy packs at api.aitherium.com/portal/marketplace/packs"

        installed, skipped, failed = [], [], []
        for pack_id in entitled:
            dest = packs_root / pack_id
            if dest.is_dir() and any(dest.iterdir()):
                skipped.append(pack_id)
                continue
            if dry_run:
                installed.append(f"{pack_id} (would install)")
                continue
            ok, detail = self._download_verify_install(pack_id, dest)
            (installed if ok else failed).append(detail)

        lines = [f"=== PACK SYNC ({'dry run' if dry_run else 'apply'}) ==="]
        lines.append(f"Entitled: {len(entitled)} · installed: {len(installed)} · "
                     f"already present: {len(skipped)} · failed: {len(failed)}")
        for d in installed:
            lines.append(f"  + {d}")
        for d in failed:
            lines.append(f"  ✗ {d}")
        if skipped:
            lines.append(f"  (present) {', '.join(skipped)}")
        return "\n".join(lines)

    def _download_verify_install(self, pack_id: str, dest: Path) -> tuple:
        """Download → verify signature → extract one pack. Returns (ok, message)."""
        try:
            with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                resp = client.get(
                    f"{self._base_url}/v1/packs/{pack_id}/download",
                    headers=self.auth.get_headers(),
                )
            if resp.status_code == 402:
                return False, f"{pack_id}: license required (purchase not active)"
            if resp.status_code == 404:
                return False, f"{pack_id}: no downloadable artifact"
            resp.raise_for_status()
            tarball = resp.content
            signature = resp.headers.get("X-Aither-Pack-Signature")

            # Ed25519 signature verification (fail-closed when a key is pinned).
            try:
                from adk.pack_verifier import verify_pack_tarball

                verified, vmsg = verify_pack_tarball(tarball, signature)
                if not verified:
                    return False, f"{pack_id}: signature check failed ({vmsg})"
            except ImportError:
                pass  # verifier unavailable → proceed (legacy/offline)

            # Extract to a temp dir then atomically swap in (no half-installed pack).
            import io
            import shutil
            import tempfile

            mint_install_credential, revoke_install_credential = _credential_hooks()

            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=str(dest.parent)) as tmp:
                with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
                    _safe_extract(tar, tmp)
                if dest.exists():
                    # Replacing an existing install: its per-install credential is
                    # about to become undiscoverable, so revoke it FIRST. This is
                    # the whole point of per-install tokens — one install's
                    # credential dies without touching any other install's.
                    if revoke_install_credential is not None:
                        revoke_install_credential(pack_id)
                    shutil.rmtree(dest, ignore_errors=True)
                # If the tar has a single top dir, promote it; else move tmp itself.
                children = [p for p in Path(tmp).iterdir()]
                if len(children) == 1 and children[0].is_dir():
                    shutil.move(str(children[0]), str(dest))
                else:
                    shutil.move(tmp, str(dest))
                    Path(tmp).mkdir(exist_ok=True)  # keep context manager happy
            if mint_install_credential is not None:
                mint_install_credential(pack_id)
            return True, f"{pack_id}"
        except Exception as e:  # noqa: BLE001
            return False, f"{pack_id}: {type(e).__name__}: {e}"

    def _show_library(self) -> str:
        """Show purchased packs in personal library."""
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"{self._base_url}/api/marketplace/my-library",
                    headers=self.auth.get_headers(),
                )
                resp.raise_for_status()
                library = resp.json()
                packs = library.get("packs", [])

            if not packs:
                return (
                    "Your library is empty. Purchase packs from the marketplace "
                    "with '/packs purchase <pack-id>'"
                )

            output = "=== MY LIBRARY ===\n\n"
            output += self._format_packs_table(packs)
            return output

        except httpx.HTTPError as e:
            return f"ERROR: Failed to fetch library: {e}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def _show_help(self) -> str:
        """Show detailed help text."""
        return """
=== PACKS MARKETPLACE HELP ===

Browse and manage agent/skill/tool packs from the Elysium marketplace.

SYNTAX:
  /packs [command] [options]

COMMANDS:

  list
    List all published packs in the marketplace.
    Usage: /packs list

  browse
    Browse packs with optional filters.
    Usage: /packs browse [--type agent|skill|tool] [--search query]
    Example: /packs browse --type skill --search "reasoning"

  info <pack-id>
    Show detailed information about a specific pack.
    Usage: /packs info <pack-id>
    Example: /packs info my-skill-pack

  purchase <pack-id>
    Open Stripe checkout to purchase a pack.
    Usage: /packs purchase <pack-id>
    Example: /packs purchase my-skill-pack

  install <pack-id> [options]
    Download pack bundle to current directory.
    Options:
      --extract, -x    Automatically extract the .tar.gz archive
    After extraction, automatically tries to hot-apply to running agent (graceful
    fallback if agent is offline).
    Usage: /packs install <pack-id> [--extract]
    Example: /packs install my-skill-pack --extract

  uninstall <pack-id>            (aliases: remove, rm)
    Remove an installed pack from ~/.aitheros/packs/ and REVOKE its per-install
    credential first, while the credential metadata is still readable. Deleting
    the directory by hand instead leaves a live, unrevocable credential.
    Usage: /packs uninstall <pack-id>
    Example: /packs uninstall my-skill-pack

  activate [pack-id]
    Hot-reload packs on the running agent server (does not download).
    If no agent server is running, prints a hint that packs will apply on restart.
    Usage: /packs activate
    Example: /packs activate

  sync [--dry-run]
    Install every entitled pack not already present locally. Polls your active
    licenses, then downloads + signature-verifies + extracts each missing pack
    into ~/.aitheros/packs/. A portal purchase converges onto this node with one
    command. Use --dry-run to preview without installing.
    Usage: /packs sync [--dry-run]

  library
    Show your purchased packs (my-library).
    Usage: /packs library

  help
    Show this help text.
    Usage: /packs help

AUTHENTICATION:
  Set bearer token and tenant ID via /auth command before using marketplace features.

EXAMPLES:
  /packs                              List all packs
  /packs browse --type agent           Browse agent packs
  /packs info llm-reasoning             Show pack details
  /packs purchase llm-reasoning         Open checkout
  /packs install llm-reasoning --extract Download and extract
  /packs library                        Show owned packs
"""

    def _format_packs_table(self, packs: List[Dict[str, Any]]) -> str:
        """Format pack list as simple text table with optional branding tagline."""
        if not packs:
            return "No packs available."

        lines = []
        header = (
            "ID                    | NAME                    | TAGLINE"
            "                   | TYPE     | PRICE"
        )
        lines.append(header)
        lines.append("-" * 110)

        for pack in packs:
            pack_id = pack.get("id", "unknown")[:20].ljust(22)
            name = pack.get("name", "unnamed")[:20].ljust(24)
            # Extract tagline from branding, truncate to 25 chars
            branding = pack.get("branding", {}) or {}
            tagline = branding.get("tagline", "") if isinstance(branding, dict) else ""
            tagline_display = (tagline[:25] if tagline else "").ljust(27)
            pack_type = pack.get("type", "unknown")[:8].ljust(9)
            price = pack.get("price", "free")

            lines.append(f"{pack_id}| {name}| {tagline_display}| {pack_type}| {price}")

        return "\n".join(lines)

    def _format_pack_detail(self, pack: Dict[str, Any]) -> str:
        """Format pack detail view with optional branding tagline and capabilities."""
        output = []
        output.append("=== PACK DETAILS ===\n")

        output.append(f"ID:           {pack.get('id', 'N/A')}")
        output.append(f"Name:         {pack.get('name', 'N/A')}")
        output.append(f"Type:         {pack.get('type', 'N/A')}")
        output.append(f"Version:      {pack.get('version', 'N/A')}")
        output.append(f"Price:        {pack.get('price', 'free')}")
        output.append(f"Author:       {pack.get('author', 'N/A')}")

        if pack.get("description"):
            output.append(f"\nDescription:")
            output.append(f"  {pack['description']}")

        # Add tagline from branding if present
        branding = pack.get("branding", {}) or {}
        if isinstance(branding, dict) and branding.get("tagline"):
            output.append(f"\nTagline:      {branding['tagline']}")

        # Add capabilities list if present
        capabilities = pack.get("capabilities", []) or []
        if capabilities and isinstance(capabilities, list):
            output.append("\nCapabilities:")
            # Show up to 3 capabilities inline, then "+N more" if there are more
            shown = min(3, len(capabilities))
            for i, cap in enumerate(capabilities[:shown]):
                cap_name = cap.get("name", f"capability-{i}") if isinstance(cap, dict) else str(cap)
                cap_desc = cap.get("description", "") if isinstance(cap, dict) else ""
                if cap_desc:
                    output.append(f"  • {cap_name} — {cap_desc[:60]}...")
                else:
                    output.append(f"  • {cap_name}")
            if len(capabilities) > 3:
                output.append(f"  +{len(capabilities) - 3} more capabilities")

        if pack.get("tags"):
            tags = ", ".join(pack["tags"])
            output.append(f"\nTags: {tags}")

        if pack.get("manifest"):
            output.append(f"\nManifest:")
            manifest = pack["manifest"]
            if isinstance(manifest, dict):
                for key, value in manifest.items():
                    output.append(f"  {key}: {value}")
            else:
                output.append(f"  {manifest}")

        if pack.get("created_at"):
            output.append(f"\nCreated:      {pack['created_at']}")

        if pack.get("updated_at"):
            output.append(f"Updated:      {pack['updated_at']}")

        output.append(f"\n--- Actions ---")
        output.append(f"View:     /packs info {pack.get('id')}")
        output.append(f"Purchase: /packs purchase {pack.get('id')}")
        output.append(f"Install:  /packs install {pack.get('id')} --extract")

        return "\n".join(output)


# ─── Module-level function for enrollment auto-sync ───


async def sync_entitled_packs(
    auth_token: str,
    tenant_id: Optional[str] = None,
    base_url: str = "https://api.aitherium.com",
) -> Tuple[int, int]:
    """Sync entitled packs for a newly enrolled node (best-effort).

    Called by fleet_enroll.py after successful enrollment, to auto-install
    the customer's purchased packs on the new node.

    Args:
        auth_token: Bearer token for authentication
        tenant_id: Optional tenant ID header
        base_url: Portal URL (default api.aitherium.com)

    Returns:
        (installed_count, failed_count) tuple; never raises.
        Best-effort: failures log but do not block enrollment.
    """
    import logging
    log = logging.getLogger("adk.packs")

    def _packs_dir() -> Path:
        """The local pack install root (matches agent auto-discovery)."""
        d = Path(os.path.expanduser("~")) / ".aitheros" / "packs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _download_verify_install(pack_id: str, dest: Path) -> bool:
        """Download → verify signature → extract one pack. Returns True on success."""
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
                if tenant_id:
                    headers["X-Tenant-ID"] = tenant_id

                resp = await client.get(
                    f"{base_url.rstrip('/')}/v1/packs/{pack_id}/download",
                    headers=headers,
                )
            if resp.status_code == 402:
                log.warning("Pack %s: license required (purchase not active)", pack_id)
                return False
            if resp.status_code == 404:
                log.warning("Pack %s: no downloadable artifact", pack_id)
                return False
            if resp.status_code != 200:
                log.warning("Pack %s: HTTP %s", pack_id, resp.status_code)
                return False

            tarball = resp.content
            signature = resp.headers.get("X-Aither-Pack-Signature")

            # Ed25519 signature verification (fail-closed when a key is pinned).
            try:
                from adk.pack_verifier import verify_pack_tarball

                verified, vmsg = verify_pack_tarball(tarball, signature)
                if not verified:
                    log.warning("Pack %s: signature check failed (%s)", pack_id, vmsg)
                    return False
            except ImportError:
                pass  # verifier unavailable → proceed (legacy/offline)

            # Extract to a temp dir then atomically swap in (no half-installed pack).
            import io
            import shutil
            import tempfile

            mint_install_credential, revoke_install_credential = _credential_hooks()

            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=str(dest.parent)) as tmp:
                with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
                    _safe_extract(tar, tmp)
                if dest.exists():
                    # Replacing an install → revoke its credential before the
                    # metadata becomes unreachable (see _credential_hooks).
                    if revoke_install_credential is not None:
                        revoke_install_credential(pack_id)
                    shutil.rmtree(dest, ignore_errors=True)
                # If the tar has a single top dir, promote it; else move tmp itself.
                children = [p for p in Path(tmp).iterdir()]
                if len(children) == 1 and children[0].is_dir():
                    shutil.move(str(children[0]), str(dest))
                else:
                    shutil.move(tmp, str(dest))
                    Path(tmp).mkdir(exist_ok=True)  # keep context manager happy
            if mint_install_credential is not None:
                mint_install_credential(pack_id)
            log.info("Pack %s: installed", pack_id)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Pack %s: %s: %s", pack_id, type(e).__name__, e)
            return False

    # 1. Fetch entitlements
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
            if tenant_id:
                headers["X-Tenant-ID"] = tenant_id

            resp = await client.get(
                f"{base_url.rstrip('/')}/v1/marketplace/license/mine",
                headers=headers,
            )
            resp.raise_for_status()
            licenses = resp.json().get("licenses", [])
    except Exception as e:  # noqa: BLE001
        log.warning("Could not fetch entitlements: %s", e)
        return 0, 0

    entitled = sorted({
        lic.get("listing_id", "")
        for lic in licenses
        if lic.get("status") == "active" and lic.get("listing_id")
    })
    if not entitled:
        log.info("No entitled packs to sync")
        return 0, 0

    # 2. Install each entitled pack not already present
    packs_root = _packs_dir()
    installed, failed = 0, 0
    for pack_id in entitled:
        dest = packs_root / pack_id
        if dest.is_dir() and any(dest.iterdir()):
            log.debug("Pack %s: already present, skipping", pack_id)
            continue
        if await _download_verify_install(pack_id, dest):
            installed += 1
        else:
            failed += 1

    log.info("Pack sync complete: installed=%d, failed=%d", installed, failed)
    return installed, failed
