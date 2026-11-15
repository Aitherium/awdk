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
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── Auth resolution ───────────────────────────────────────────────────────

def _aither_home() -> Path:
    return Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))


def auth_candidates() -> List[Tuple[str, str]]:
    """Every credential on this machine that could open an MCP gateway, best first.

    Returns ``[(token, source_description), ...]`` with duplicates removed. The
    source never contains the token.

    Order matters. Measured 2026-11-12 against the local gateway (:8182): a stale
    ``portal.token`` outranked the live ``adk login`` session, so
    ``adk mcp setup --bake-token`` baked a credential the gateway answers with
    ``401 invalid_token`` while the session bearer the stdio bridge sends
    (``~/.aither/session-bearer``) answered 200. The login session therefore
    comes before the legacy token files.
    """
    out: List[Tuple[str, str]] = []
    seen: set = set()

    def add(token: Any, source: str) -> None:
        tok = str(token or "").strip()
        if tok and tok not in seen:
            seen.add(tok)
            out.append((tok, source))

    # 1. Explicit env (the caller said which key to use)
    add(os.environ.get("AITHER_MCP_KEY", ""), "AITHER_MCP_KEY env var")
    add(os.environ.get("AITHER_API_KEY", ""), "AITHER_API_KEY env var")

    home = _aither_home()

    # 2. The person's login session (`adk login`): auth.json profiles, then the
    #    session-bearer file. This is what a local gateway accepts.
    try:
        from adk.credentials import user_bearer

        add(user_bearer(auth_path=home / "auth.json",
                        bearer_path=home / "session-bearer"),
            "adk login session")
    except Exception:  # noqa: BLE001 - a broken login store is "no session"
        pass
    try:
        add((home / "session-bearer").read_text(encoding="utf-8"),
            str(home / "session-bearer"))
    except OSError:
        pass

    # 3. Portal token (legacy `aither login`)
    try:
        add((home / "portal.token").read_text(encoding="utf-8"), str(home / "portal.token"))
    except OSError:
        pass

    # 4. auth.json top-level token (legacy device flow) / credentials.json (legacy)
    for name, keys in (("auth.json", ("access_token", "token")),
                       ("credentials.json", ("api_key", "token"))):
        path = home / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for key in keys:
                if isinstance(data.get(key), str):
                    add(data[key], str(path))

    return out


def resolve_auth() -> Tuple[Optional[str], str]:
    """
    Find the best available auth token (the first of :func:`auth_candidates`).

    Returns:
        (token_or_none, source_description)
    """
    candidates = auth_candidates()
    if candidates:
        return candidates[0]
    return None, "no token found"


# ── Gateway URL ───────────────────────────────────────────────────────────

# NOTE: local is the HOST-side port, which speaks plain HTTP (the WSL relay in front of
# the gateway). Measured 2026-11-12: http://localhost:8182/health 200, https -> SSL
# WRONG_VERSION_NUMBER, so `adk mcp status` called a healthy gateway unreachable. Fleet
# containers reach the gateway by service name over TLS and never use this entry.
# AITHER_MCP_LOCAL_URL overrides it.
_GATEWAY_URLS = {
    "local": os.environ.get("AITHER_MCP_LOCAL_URL") or "http://localhost:8182/mcp",
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


def auth_headers(token: str) -> Dict[str, str]:
    """The auth headers a baked config carries. :func:`verify_gateway_auth` sends
    exactly these, so what was verified is what gets written."""
    return {"Authorization": f"Bearer {token}", "X-API-Key": token}


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
        headers.update(auth_headers(token))
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

def _tls_verify() -> bool:
    return os.getenv("AITHER_TLS_VERIFY", "true").lower() != "false"


def _parse_rpc(resp: Any) -> Dict[str, Any]:
    """A StreamableHTTP reply: plain JSON, or an SSE stream whose last data event is it."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        payload = ""
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
        return json.loads(payload) if payload else {}
    return resp.json()


def verify_gateway_auth(url: str, token: str, timeout: float = 20.0) -> Dict[str, Any]:
    """Prove a credential against the gateway with a REAL authenticated call.

    Runs the MCP handshake (``initialize``) and then ``tools/list`` with the
    headers a baked config would carry. ``/health`` answers 200 to anyone, so it
    says nothing about whether the credential works.

    Returns ``{ok, status_code, tool_count, error}``:
      * ``ok is True``   the gateway listed tools for this credential
      * ``ok is False``  the gateway answered and refused it (401/403, RPC error)
      * ``ok is None``   cannot judge (unreachable, timeout, malformed reply)
    The token is never placed in ``error``.
    """
    import httpx

    out: Dict[str, Any] = {"ok": None, "status_code": 0, "tool_count": 0, "error": None}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **auth_headers(token),
    }

    def refused(resp: Any, step: str) -> bool:
        out["status_code"] = resp.status_code
        if resp.status_code in (401, 403):
            try:
                reason = str((resp.json() or {}).get("error") or "")[:60]
            except (ValueError, AttributeError):
                reason = ""
            out["ok"] = False
            out["error"] = (f"gateway rejected the credential (HTTP {resp.status_code}"
                            + (f" {reason}" if reason else "") + ")")
            return True
        if resp.status_code >= 400:
            out["error"] = f"{step} returned HTTP {resp.status_code}"
            return True
        return False

    try:
        with httpx.Client(timeout=timeout, verify=_tls_verify(), headers=headers) as client:
            resp = client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "awdk-mcp-setup", "version": "1.0"}},
            })
            if refused(resp, "initialize"):
                return out
            sid = resp.headers.get("mcp-session-id") or ""
            session = {"Mcp-Session-Id": sid} if sid else {}
            if sid:
                client.post(url, headers=session,
                            json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            resp = client.post(url, headers=session,
                               json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            if refused(resp, "tools/list"):
                return out
            data = _parse_rpc(resp)
    except httpx.HTTPError as exc:
        out["error"] = f"gateway unreachable: {type(exc).__name__}"
        return out
    except ValueError:
        out["error"] = "gateway sent a reply that is not JSON-RPC"
        return out

    if not isinstance(data, dict) or "result" not in data:
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            out["ok"] = False
            out["error"] = "tools/list failed: " + str(err.get("message") or "error")[:120]
        else:
            out["error"] = "tools/list returned no result"
        return out
    tools = (data.get("result") or {}).get("tools")
    out["tool_count"] = len(tools) if isinstance(tools, list) else 0
    out["ok"] = True
    return out


def may_try_every_credential(url: str) -> bool:
    """True when ``url`` is this machine or an Aitherium host.

    Trying each stored credential in turn hands every one of them to the server
    at ``url``. That is fine for the local gateway and for Aitherium's own, and
    wrong for any other address a config or a flag might name.
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return host == "aitherium.com" or host.endswith(".aitherium.com")


def select_working_auth(
    url: str, candidates: Optional[List[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """Pick the first credential the gateway at ``url`` actually accepts.

    Returns ``{verdict, token, source, tool_count, tried}``:
      * ``verdict == "ok"``          ``token``/``source`` listed ``tool_count`` tools
      * ``verdict == "rejected"``    the gateway refused every candidate
      * ``verdict == "unverified"``  the gateway could not be asked; ``token`` is the
                                     best candidate, NOT proven
      * ``verdict == "none"``        no credential on this machine
    ``tried`` is ``[(source, error), ...]`` for the candidates that did not pass.
    """
    cands = auth_candidates() if candidates is None else list(candidates)
    if not may_try_every_credential(url):
        # An address that is neither this machine nor Aitherium's gets ONE
        # credential, the best one, as before: never the whole local store.
        cands = cands[:1]
    out: Dict[str, Any] = {"verdict": "none", "token": None, "source": "no token found",
                           "tool_count": 0, "tried": []}
    if not cands:
        return out
    for token, source in cands:
        res = verify_gateway_auth(url, token)
        if res["ok"] is True:
            out.update(verdict="ok", token=token, source=source,
                       tool_count=res["tool_count"])
            return out
        out["tried"].append((source, res["error"] or "unknown"))
        if res["ok"] is None:
            out.update(verdict="unverified", token=cands[0][0], source=cands[0][1])
            return out
    out["verdict"] = "rejected"
    return out


def probe_gateway(url: str, token: Optional[str] = None) -> Dict[str, Any]:
    """
    Health-check the MCP gateway. Returns a status dict.

    Keys: connected (bool), status (str), tier (str), tool_count (int),
          user (str), balance (int), error (str or None),
          auth_ok (True/False, or None when no token was given or it could
          not be judged), auth_error (str or None).

    ``connected`` only means ``/health`` answered. With a token, ``auth_ok``
    is the result of a real authenticated ``tools/list``
    (:func:`verify_gateway_auth`); never report a gateway as usable on
    ``connected`` alone.
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
        "auth_ok": None,
        "auth_error": None,
    }

    # Health check (no auth)
    health_url = url.rsplit("/mcp", 1)[0] + "/health"
    try:
        with httpx.Client(timeout=5, verify=_tls_verify()) as client:
            resp = client.get(health_url)
            if resp.status_code != 200:
                result["error"] = f"Health endpoint returned {resp.status_code}"
                return result
            result["connected"] = True
            result["status"] = "healthy"
    except Exception as exc:
        result["error"] = f"Gateway unreachable: {type(exc).__name__}: {exc}"
        return result

    if token:
        auth = verify_gateway_auth(url, token)
        result["auth_ok"] = auth["ok"]
        result["auth_error"] = auth["error"]
        result["tool_count"] = auth["tool_count"]
        # Account metadata (best effort; the tools/list above is the verdict)
        try:
            with httpx.Client(timeout=10, verify=_tls_verify()) as client:
                info_url = url.rsplit("/mcp", 1)[0] + "/.well-known/mcp.json"
                resp = client.get(info_url, headers=auth_headers(token))
                if resp.status_code == 200:
                    data = resp.json()
                    result["tier"] = data.get("tier", "")
                    result["tool_count"] = result["tool_count"] or data.get("tool_count", 0)
                    result["user"] = data.get("user", "")
                    result["balance"] = data.get("balance", 0)
        except Exception:
            pass  # Non-fatal

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
