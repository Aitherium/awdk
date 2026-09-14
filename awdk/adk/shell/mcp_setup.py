"""
MCP Setup — IDE config generator with auth for AitherOS MCP Gateway.

Generates `.mcp.json` (or IDE-equivalent) with http transport pointing to
the authenticated MCP gateway, either local (Docker, port 8182) or remote
(mcp.aitherium.com).

Auth strategy: The gateway advertises OAuth Protected Resource Metadata
(RFC 9728) at ``/.well-known/oauth-protected-resource``, pointing IDEs to
AitherIdentity as the authorization server. IDEs that support MCP OAuth
(Claude Code, Cursor) drive the authorization-code + PKCE flow natively —
no baked tokens, no subprocess helpers, no env vars.

For IDEs that don't support OAuth, ``aither mcp setup`` can bake a token
into the headers as a fallback (``--bake-token``).

Usage:
    aither mcp setup                          # local + claude-code (default)
    aither mcp setup --mode remote --ide cursor
    aither mcp setup --bake-token             # fallback: bake token into headers
    aither mcp status                         # connectivity check
    aither mcp scope free                     # simulate tier (admin only)
"""

import json
from adk._tls import tls_verify
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


# ── Auth resolution ───────────────────────────────────────────────────────

def resolve_auth() -> Tuple[Optional[str], str]:
    """
    Find the best available auth token.

    Returns:
        (token_or_none, source_description)
    """
    # 1. Env var (highest priority — explicit)
    env_key = os.environ.get("AITHER_API_KEY", "").strip()
    if env_key:
        return env_key, "AITHER_API_KEY env var"

    aither_home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))

    # 2. Portal token (from `aither login`)
    portal_token = aither_home / "portal.token"
    if portal_token.is_file():
        token = portal_token.read_text(encoding="utf-8").strip()
        if token:
            return token, str(portal_token)

    # 3. auth.json (device flow / OAuth)
    auth_json = aither_home / "auth.json"
    if auth_json.is_file():
        try:
            data = json.loads(auth_json.read_text(encoding="utf-8"))
            token = data.get("access_token") or data.get("token", "")
            if token:
                return token.strip(), str(auth_json)
        except (json.JSONDecodeError, KeyError):
            pass

    # 4. credentials.json (legacy)
    creds = aither_home / "credentials.json"
    if creds.is_file():
        try:
            data = json.loads(creds.read_text(encoding="utf-8"))
            token = data.get("api_key") or data.get("token", "")
            if token:
                return token.strip(), str(creds)
        except (json.JSONDecodeError, KeyError):
            pass

    return None, "no token found"


# ── Gateway URL ───────────────────────────────────────────────────────────

# NOTE: local uses HTTPS — AitherOS services use TLS internally.
_GATEWAY_URLS = {
    "local": "https://localhost:8182/mcp",
    "remote": "https://mcp.aitherium.com/mcp",
}


def resolve_gateway_url(mode: str = "local") -> str:
    """Resolve MCP gateway URL for the given mode."""
    return _GATEWAY_URLS.get(mode, _GATEWAY_URLS["local"])


# ── IDE config generation ─────────────────────────────────────────────────

# Where each IDE reads its MCP config relative to project root
_IDE_CONFIG_PATHS = {
    "claude-code": ".mcp.json",
    "cursor": ".cursor/mcp.json",
    "windsurf": ".windsurf/mcp.json",
    "vscode": ".vscode/mcp.json",
}


def generate_config(
    ide: str,
    url: str,
    token: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Generate IDE-specific MCP config dict.

    The gateway serves RFC 9728 OAuth Protected Resource Metadata, so IDEs
    that support MCP OAuth will discover AitherIdentity and run the
    authorization-code + PKCE flow automatically — no headers needed.

    When ``token`` is provided (``--bake-token`` fallback), the token is
    baked directly into the headers for IDEs that lack OAuth support.
    """
    server: Dict[str, Any] = {
        "type": "http",
        "url": url,
    }

    headers: Dict[str, str] = {}
    if token:
        # Fallback: bake token directly into config for non-OAuth IDEs
        headers["Authorization"] = f"Bearer {token}"
        headers["X-API-Key"] = token
    if extra_headers:
        headers.update(extra_headers)
    if headers:
        server["headers"] = headers

    return {"mcpServers": {"aitheros": server}}


def write_config(config: Dict[str, Any], ide: str, project_dir: str) -> Path:
    """Write config to the correct IDE location. Returns the written path."""
    rel_path = _IDE_CONFIG_PATHS.get(ide, ".mcp.json")
    out_path = Path(project_dir) / rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return out_path


# ── Tier simulation ──────────────────────────────────────────────────────

def set_tier_simulation(
    ide: str,
    project_dir: str,
    tier: Optional[str],
) -> Tuple[Path, int, int]:
    """
    Add or remove X-Simulate-Tier header in the IDE config.

    Returns:
        (config_path, old_tool_count_estimate, new_tool_count_estimate)
        Tool counts are estimates based on tier; -1 means unknown.
    """
    rel_path = _IDE_CONFIG_PATHS.get(ide, ".mcp.json")
    config_path = Path(project_dir) / rel_path

    if not config_path.is_file():
        raise FileNotFoundError(f"No MCP config at {config_path}. Run 'aither mcp setup' first.")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    server = config.get("mcpServers", {}).get("aitheros", {})
    headers = server.get("headers", {})

    old_tier = headers.get("X-Simulate-Tier")
    if tier and tier != "reset":
        headers["X-Simulate-Tier"] = tier
    else:
        headers.pop("X-Simulate-Tier", None)
        tier = None

    server["headers"] = headers
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    # Rough tool count estimates by tier
    _tier_tool_estimates = {"free": 15, "starter": 40, "pro": 80, "enterprise": 120}
    old_est = _tier_tool_estimates.get(old_tier, -1) if old_tier else -1
    new_est = _tier_tool_estimates.get(tier, -1) if tier else -1

    return config_path, old_est, new_est


# ── Gateway probe ─────────────────────────────────────────────────────────

def probe_gateway(url: str, token: Optional[str] = None) -> Dict[str, Any]:
    """
    Health-check the MCP gateway. Returns a status dict.

    Keys: connected (bool), status (str), tier (str), tool_count (int),
          user (str), balance (int), error (str or None).
    """
    import httpx

    result: Dict[str, Any] = {
        "connected": False,
        "status": "unknown",
        "tier": "",
        "tool_count": 0,
        "user": "",
        "balance": 0,
        "error": None,
    }

    # Health check (no auth) — verify=tls_verify() for self-signed local certs
    health_url = url.rsplit("/mcp", 1)[0] + "/health"
    try:
        with httpx.Client(timeout=5, verify=tls_verify()) as client:
            resp = client.get(health_url)
            if resp.status_code != 200:
                result["error"] = f"Health endpoint returned {resp.status_code}"
                return result
            result["connected"] = True
            result["status"] = "healthy"
    except Exception as exc:
        result["error"] = f"Gateway unreachable: {type(exc).__name__}: {exc}"
        return result

    # Authenticated probe — try to list tools
    if token:
        headers = {"Authorization": f"Bearer {token}", "X-API-Key": token}
        try:
            with httpx.Client(timeout=10, verify=tls_verify()) as client:
                # Use the well-known endpoint for metadata
                info_url = url.rsplit("/mcp", 1)[0] + "/.well-known/mcp.json"
                resp = client.get(info_url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    result["tier"] = data.get("tier", "")
                    result["tool_count"] = data.get("tool_count", 0)
                    result["user"] = data.get("user", "")
                    result["balance"] = data.get("balance", 0)
        except Exception:
            pass  # Non-fatal — health is enough

    return result


# ── Local CA trust ────────────────────────────────────────────────────────

def ensure_local_ca_trust() -> str:
    """Ensure the AitherNet Root CA is trusted for local MCP connections.

    For local (self-hosted) mode, the IDE's Node.js runtime needs to trust
    the internal CA. This function:
    1. Finds or builds the CA bundle from the local AitherOS install
    2. Sets NODE_EXTRA_CA_CERTS as a persistent User env var (Windows)
       or writes to ~/.aither/node-ca.pem + prints export instruction
    3. On Windows, also installs the Root CA into the user certificate
       store via certutil so Node.js trusts it system-wide

    Returns: "set", "already", or an error/info message.
    """
    import subprocess

    existing = os.environ.get("NODE_EXTRA_CA_CERTS", "")

    # Search for CA bundle in standard locations
    aither_home = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
    ca_bundle_dst = aither_home / "aithernet-ca-bundle.pem"

    # If already set and the file exists, we're good
    if existing and Path(existing).is_file():
        return "already"

    # Find CA certs from local AitherOS install
    ca_sources = [
        # Docker bind mount path
        Path("AitherOS/Library/Data/secrets/ca"),
        # Absolute path
        Path(os.environ.get("AITHER_ROOT", "")) / "Library" / "Data" / "secrets" / "ca",
        # Relative to cwd
        Path("AitherOS/config/certs"),
    ]

    root_crt = None
    intermediate_crt = None
    for ca_dir in ca_sources:
        r = ca_dir / "root.crt"
        i = ca_dir / "intermediate.crt"
        if r.is_file() and i.is_file():
            root_crt = r
            intermediate_crt = i
            break

    # Also check for pre-built bundle
    for candidate in ca_sources:
        bundle = candidate.parent / "aithernet-ca-bundle.pem"
        if bundle.is_file():
            ca_bundle_dst = bundle
            root_crt = None  # Skip building, use existing
            break

    if root_crt and intermediate_crt:
        # Build bundle
        aither_home.mkdir(parents=True, exist_ok=True)
        try:
            bundle_content = root_crt.read_text() + "\n" + intermediate_crt.read_text()
            ca_bundle_dst.write_text(bundle_content, encoding="utf-8")
        except Exception as e:
            return f"Could not write CA bundle: {e}"

    if not ca_bundle_dst.is_file():
        return "CA bundle not found — remote mode doesn't need this"

    bundle_path = str(ca_bundle_dst.resolve())

    # Set NODE_EXTRA_CA_CERTS persistently
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["powershell.exe", "-Command",
                 f"[Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', '{bundle_path}', 'User')"],
                capture_output=True, timeout=10,
            )
            os.environ["NODE_EXTRA_CA_CERTS"] = bundle_path
        except Exception as e:
            return f"Could not set env var: {e}"

        # Also install Root CA into Windows user certificate store so
        # Node.js (which may ignore NODE_EXTRA_CA_CERTS) trusts it
        if root_crt and root_crt.is_file():
            try:
                subprocess.run(
                    ["certutil", "-addstore", "-user", "Root", str(root_crt)],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass  # Non-fatal — NODE_EXTRA_CA_CERTS is the primary mechanism

        return "set"
    else:
        # Unix: write to shell profile
        os.environ["NODE_EXTRA_CA_CERTS"] = bundle_path
        profile = Path.home() / ".bashrc"
        export_line = f'export NODE_EXTRA_CA_CERTS="{bundle_path}"'
        try:
            if profile.is_file() and export_line not in profile.read_text():
                with open(profile, "a") as f:
                    f.write(f"\n# AitherOS MCP gateway CA trust\n{export_line}\n")
            return "set"
        except Exception:
            return f"Add to your shell profile: {export_line}"
