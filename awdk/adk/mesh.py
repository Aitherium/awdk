"""adk.mesh — autonomous AitherMesh (WireGuard overlay) onboarding.

A fresh node joins the overlay network via a proven bootstrap flow —
now as a first-class, cross-platform SDK capability so `adk mesh join` works
anywhere awdk runs:

  1. generate a WireGuard keypair
  2. POST /v1/mesh/onboard to the Conductor → receive an ``overlay_ip``
  3. fetch the server pubkey from network topology endpoint
  4. write the wg config + bring the interface up (wg-quick / wireguard.exe)
  5. verify the handshake (``wg show``)

Once up, the node reaches services at their mesh addresses exactly like any
fleet peer — which is how a swarm runner talks back to the fleet.

Auth: the node's WireGuard public key registered through the Conductor is the
identity. A pre-shared key (``AITHER_MESH_PSK``) is sent as a bearer challenge
when the control plane requires one. The initial onboard call is a bootstrap:
the node has no internal CA yet, so TLS trust is opt-in — provide
``AITHER_CA_BUNDLE`` (preferred), or set ``AITHER_MESH_INSECURE_BOOTSTRAP=1`` to
match the reference ``curl -sk`` bootstrap (loudly warned; the WireGuard key +
PSK are the real auth, not TLS).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("adk.mesh")

MESH_CIDR = "10.77.0.0/16"
DEFAULT_IFACE = "aithernet0"
WG_PORT = 51820


# ─────────────────────────────────────────────────────────────────────────────
# WireGuard binaries / keys
# ─────────────────────────────────────────────────────────────────────────────

def _wg() -> str | None:
    return shutil.which("wg") or shutil.which("wg.exe")


def _wg_quick() -> str | None:
    return shutil.which("wg-quick")


def _is_windows() -> bool:
    return platform.system().lower().startswith("win")


def generate_keypair() -> tuple[str, str]:
    """Return (private_key, public_key). Requires the ``wg`` tool."""
    wg = _wg()
    if not wg:
        raise RuntimeError(
            "WireGuard 'wg' tool not found. Install wireguard-tools "
            "(apt install wireguard-tools) or WireGuard for Windows.")
    priv = subprocess.run([wg, "genkey"], capture_output=True, text=True,
                          check=True).stdout.strip()
    pub = subprocess.run([wg, "pubkey"], input=priv, capture_output=True,
                         text=True, check=True).stdout.strip()
    return priv, pub


# ─────────────────────────────────────────────────────────────────────────────
# Conductor URL resolution (internal hostname fallback)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_conductor_url(default_url: str) -> str:
    """Resolve the conductor URL. If the default hostname does not
    resolve, fall back to the public endpoint.

    This allows seamless operation in fleet deployments and for customer
    nodes where only the public tunnel is available.

    Args:
        default_url: The default URL (typically internal).

    Returns:
        The default if the hostname resolves, otherwise the public fallback.
    """
    try:
        parsed = urlparse(default_url)
        hostname = parsed.hostname or "localhost"
        # Try to resolve the hostname; if it succeeds, use default.
        socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return default_url
    except (socket.gaierror, OSError):
        # Hostname resolution failed (customer box, not on the fleet mesh) — fall
        # back to the PUBLIC Cloudflare-tunnel endpoint. The tunnel serves the
        # onboard API on 443, NOT the internal :8193, so the public URL carries
        # no port (a :8193 here would hit a dead port and defeat the fallback).
        fallback = "https://conductor.aitherium.com"
        logger.info(
            "Conductor hostname %s not resolvable; falling back to %s",
            urlparse(default_url).hostname, fallback)
        return fallback


def _resolve_mesh_url(default_url: str) -> str | None:
    """Resolve the AitherMesh directory URL for ``mesh ls``.

    Unlike the Conductor (which has a public Cloudflare-tunnel endpoint for
    onboarding), the AitherMesh peer directory is NOT publicly routed — after a
    node joins, peers are reached over the WireGuard tailnet via mesh-core on
    :8125 (see the tunnel-routes note: Strata/Nexus/mesh are proxied over the
    overlay, not a public host). So:

    - If the given hostname resolves (co-located fleet box, or the caller passed
      an explicit overlay address) → use it.
    - Else, if ``$AITHER_AITHERNET_URL`` is set (written post-join with the
      mesh-core overlay address) → use that.
    - Else → return ``None`` so the caller can print an actionable instruction
      (join first, or pass ``--mesh-url https://<mesh-core-overlay-ip>:8125``)
      instead of crashing on a DNS error.
    """
    try:
        hostname = urlparse(default_url).hostname or "localhost"
        socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return default_url
    except (socket.gaierror, OSError):
        overlay = os.getenv("AITHER_AITHERNET_URL", "").strip()
        if overlay:
            logger.info("Mesh hostname unresolvable; using overlay %s", overlay)
            return overlay
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Control-plane calls (Conductor onboard + AitherNet topology)
# ─────────────────────────────────────────────────────────────────────────────

def _verify() -> Any:
    """TLS verify for the bootstrap onboard. Prefer a CA bundle path; allow an
    explicit insecure bootstrap (matches the proven ``curl -sk`` reference) since
    a brand-new node has no internal CA and the WG key + PSK are the real auth."""
    ca = os.getenv("AITHER_CA_BUNDLE") or os.getenv("AITHER_TLS_CA", "")
    if ca and os.path.exists(ca):
        return ca
    if os.getenv("AITHER_MESH_INSECURE_BOOTSTRAP", "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "AITHER_MESH_INSECURE_BOOTSTRAP set — skipping TLS verify for the "
            "mesh onboard bootstrap ONLY (WG key + PSK are the real auth). "
            "Provide AITHER_CA_BUNDLE to remove this.")
        return False
    return True


def _headers(psk: str | None) -> dict:
    h = {"Content-Type": "application/json"}
    psk = psk or os.getenv("AITHER_MESH_PSK", "")
    if psk:
        h["Authorization"] = f"Bearer {psk}"
        h["X-Mesh-PSK"] = psk
    return h


async def onboard(
    conductor_url: str, node_id: str, wg_public_key: str,
    role: str = "worker", external_ip: str | None = None,
    external_port: int = WG_PORT, storage_tiers: list[str] | None = None,
    psk: str | None = None, tenant_id: str | None = None,
) -> dict:
    """POST /v1/mesh/onboard to the Conductor. Returns the parsed response
    (carries ``overlay_ip`` / ``aithernet_ip`` + assigned node id)."""
    import httpx
    import json

    # Read workspace_id from ~/.aither/node_auth.json if present
    workspace_id = None
    try:
        from pathlib import Path
        node_auth_file = Path.home() / ".aither" / "node_auth.json"
        if node_auth_file.exists():
            auth_data = json.loads(node_auth_file.read_text(encoding="utf-8"))
            workspace_id = auth_data.get("workspace_id")
    except Exception:
        pass  # Gracefully ignore missing/unreadable node_auth.json

    payload = {
        "node_id": node_id, "wg_public_key": wg_public_key, "role": role,
        "external_port": external_port,
        "storage_tiers": storage_tiers or ["warm", "cold"],
    }
    if external_ip:
        payload["external_ip"] = external_ip
    if workspace_id:
        payload["workspace_id"] = workspace_id
    # The conductor's OnboardRequest REQUIRES tenant_id ("required for
    # tenant-scoped mesh isolation") — without it every onboard is a 422, which
    # was the last wall between a registered node and its mesh (2026-09-02).
    # It is the tenant enrollment resolved server-side; the conductor still
    # enforces that a tenant-scoped bearer may only name its own tenant.
    if tenant_id:
        payload["tenant_id"] = tenant_id

    url = conductor_url.rstrip("/") + "/v1/mesh/onboard"
    async with httpx.AsyncClient(timeout=30.0, verify=_verify()) as c:
        r = await c.post(url, json=payload, headers=_headers(psk))
        r.raise_for_status()
        return r.json()


async def fetch_server_pubkey(aithernet_url: str, psk: str | None = None) -> tuple[str, str]:
    """GET /aithernet/topology → (server_public_key, server_endpoint)."""
    import httpx
    url = aithernet_url.rstrip("/") + "/aithernet/topology"
    async with httpx.AsyncClient(timeout=20.0, verify=_verify()) as c:
        r = await c.get(url, headers=_headers(psk))
        r.raise_for_status()
        data = r.json()
    pub = data.get("server_public_key") or data.get("public_key") or ""
    endpoint = data.get("server_endpoint") or data.get("endpoint") or ""
    return pub, endpoint


# ─────────────────────────────────────────────────────────────────────────────
# WireGuard interface config + bring-up
# ─────────────────────────────────────────────────────────────────────────────

def _wg_conf(private_key: str, overlay_ip: str, server_pubkey: str,
             server_endpoint: str, psk: str | None = None) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {overlay_ip}/16",
        "",
        "[Peer]",
        f"PublicKey = {server_pubkey}",
    ]
    if psk:
        lines.append(f"PresharedKey = {psk}")
    lines += [
        f"Endpoint = {server_endpoint}",
        f"AllowedIPs = {MESH_CIDR}",
        "PersistentKeepalive = 25",
        "",
    ]
    return "\n".join(lines)


def _conf_dir() -> Path:
    if _is_windows():
        return Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "AitherMesh"
    return Path("/etc/wireguard")


def write_config(iface: str, conf: str) -> Path:
    d = _conf_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise RuntimeError(f"cannot write WireGuard config to {d}: {exc} "
                           "(run with sufficient privileges)") from exc
    path = d / f"{iface}.conf"
    path.write_text(conf, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def bring_up(iface: str, conf_path: Path) -> str:
    """Bring the tunnel up. wg-quick on Linux/macOS; wireguard tunnel service on
    Windows. Returns the command's combined output (idempotent-ish: a re-up after
    an existing tunnel is treated as success)."""
    if _is_windows():
        wgexe = shutil.which("wireguard") or shutil.which("wireguard.exe")
        if not wgexe:
            raise RuntimeError("WireGuard for Windows not found (wireguard.exe).")
        r = subprocess.run([wgexe, "/installtunnelservice", str(conf_path)],
                           capture_output=True, text=True)
        return (r.stdout + r.stderr).strip()
    wgq = _wg_quick()
    if not wgq:
        raise RuntimeError("wg-quick not found (install wireguard-tools).")
    r = subprocess.run([wgq, "up", str(conf_path)], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 and "already exists" not in out.lower():
        raise RuntimeError(f"wg-quick up failed: {out}")
    return out


def status(iface: str = DEFAULT_IFACE) -> str:
    wg = _wg()
    if not wg:
        return "wg tool not found"
    r = subprocess.run([wg, "show", iface], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip() or f"{iface}: no such interface"


def has_handshake(iface: str = DEFAULT_IFACE) -> bool:
    """True once the tunnel has completed at least one handshake."""
    wg = _wg()
    if not wg:
        return False
    r = subprocess.run([wg, "show", iface, "latest-handshakes"],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > 0:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Headscale transport (NAT-friendly alternative to raw WireGuard)
# ─────────────────────────────────────────────────────────────────────────────

def _tailscale() -> str | None:
    """Find the tailscale binary."""
    return shutil.which("tailscale") or shutil.which("tailscale.exe")


def _tailscale_up(
    headscale_url: str, auth_key: str, hostname: str,
) -> str:
    """Bring up a Tailscale tunnel for Headscale mesh transport.

    When raw WireGuard is not viable (e.g., NAT'd customer boxes), Tailscale
    provides a NAT-traversal layer. The tunnel joins the same mesh CIDR
    (10.77.0.0/16) via the Headscale control plane.

    Args:
        headscale_url: Headscale control server URL (e.g.,
            https://headscale.aitherium.com). The node's overlay_ip is still
            assigned by the Conductor; Headscale provides the tunnel transport.
        auth_key: Pre-generated Headscale auth key (passed by the node runner).
        hostname: Desired hostname in the tailnet (e.g., aither-cloud-{node_id}).

    Returns:
        Combined stdout+stderr from tailscale up (for diagnostics).

    Raises:
        RuntimeError if tailscale binary not found or the command fails.
    """
    ts = _tailscale()
    if not ts:
        raise RuntimeError(
            "Tailscale 'tailscale' tool not found. Install tailscale "
            "(https://tailscale.com/download) or use raw WireGuard transport.")

    # Build tailscale up flags: --login-server (Headscale), --authkey, --hostname
    cmd = [
        ts, "up",
        "--login-server", headscale_url,
        "--authkey", auth_key,
        "--hostname", hostname,
    ]

    def _redact(text: str) -> str:
        # Never let the auth key reach logs/exceptions (subprocess errors echo
        # the full argv, and tailscale may print the key on failure).
        return text.replace(auth_key, "***REDACTED***") if auth_key else text

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = _redact((r.stdout + r.stderr).strip())
        if r.returncode != 0:
            logger.warning(
                "[_tailscale_up] tailscale up returned %d: %s",
                r.returncode, out)
        return out
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("tailscale up timed out (30s)") from None
    except Exception as exc:
        raise RuntimeError(f"tailscale up failed: {_redact(str(exc))}") from None


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def join(
    conductor_url: str, node_id: str | None = None, role: str = "worker",
    aithernet_url: str | None = None, iface: str = DEFAULT_IFACE,
    external_ip: str | None = None, psk: str | None = None,
    headscale: bool = False, headscale_url: str | None = None,
    headscale_auth_key: str | None = None, tenant_id: str | None = None,
) -> dict:
    """Run the full onboarding and return a report.

    ``aithernet_url`` defaults to the conductor host on :8125. ``node_id``
    defaults to the SDK's stable node id. Idempotent-ish: a node that re-runs
    join simply re-registers its pubkey and re-ups the interface.

    **Transport Modes**

    **Raw WireGuard** (default): Requires the node to have a public UDP:51820
    endpoint. Works on:
    - Dedicated hardware in data centers (OptiPlex, DGX)
    - Cloud instances with public IPs (Vast.ai)
    - Direct network access (on-premises appliances)

    **Headscale** (NAT-friendly): When ``headscale=True`` or
    ``AITHER_MESH_TRANSPORT=headscale``, the node joins via Tailscale's
    Headscale control plane instead of raw WireGuard. This enables:
    - Customer boxes behind NAT/firewalls
    - Home lab setups with dynamic IPs
    - Constrained networks (CGNAT, restrictive corporate firewalls)

    The Headscale URL defaults to the public endpoint
    (https://hs.aitherium.com — NOT headscale.aitherium.com: that name is blocked
    by at least one ISP by hostname and the edge answers plain HTTP on :443,
    measured 2026-08-15 and again 2026-09-02); override with ``AITHER_HEADSCALE_URL``.
    The auth key must be pre-generated on the Headscale server and passed as
    ``AITHER_HEADSCALE_AUTH_KEY`` or via ``headscale_auth_key`` param.

    **Constraint**: The Conductor still assigns the overlay_ip (mesh address).
    Headscale provides only the tunnel transport layer; the node operates at
    the mesh CIDR (10.77.0.0/16) regardless of transport.
    """
    from adk.fleet_enroll import _generate_node_id
    node_id = node_id or _generate_node_id()

    # Resolve transport mode: explicit param, env var, or default to raw WireGuard
    use_headscale = headscale or (
        os.getenv("AITHER_MESH_TRANSPORT", "").lower() == "headscale"
    )
    if use_headscale:
        logger.info(
            "mesh: joining via Headscale transport (NAT-friendly) for %s",
            node_id)
    else:
        logger.info(
            "mesh: joining via raw WireGuard transport for %s (requires public "
            "UDP:51820 endpoint)", node_id)

    priv, pub = generate_keypair()
    logger.info("mesh: onboarding node %s (role=%s) via %s", node_id, role, conductor_url)
    resp = await onboard(conductor_url, node_id, pub, role=role,
                         external_ip=external_ip, psk=psk, tenant_id=tenant_id)
    overlay_ip = (resp.get("overlay_ip") or resp.get("aithernet_ip")
                  or resp.get("address") or "")
    if not overlay_ip:
        raise RuntimeError(f"Conductor returned no overlay_ip: {resp}")

    # SELF-SERVICE AUTOMATION: the conductor auto-issues a headscale key + URL
    # for NAT'd nodes in the onboard response, so the customer never has to
    # obtain or set one. If the response carries a key, switch to the headscale
    # transport automatically (an explicit --transport/env still forces it too).
    resp_hs_key = (resp.get("headscale_auth_key") or "").strip()
    resp_hs_url = (resp.get("headscale_url") or "").strip()
    if resp_hs_key and not use_headscale:
        use_headscale = True
        logger.info(
            "mesh: conductor issued a headscale key for %s — joining via "
            "Headscale automatically (NAT-friendly)", node_id)

    if use_headscale:
        # Headscale transport: bring up Tailscale tunnel instead of raw WG.
        # Key/URL precedence: explicit arg > env > conductor-issued (auto).
        hs_url = headscale_url or os.getenv("AITHER_HEADSCALE_URL", "") \
            or resp_hs_url or "https://hs.aitherium.com"
        hs_key = headscale_auth_key or os.getenv("AITHER_HEADSCALE_AUTH_KEY", "") \
            or resp_hs_key
        if not hs_key:
            logger.warning(
                "[join] Headscale transport requested but AITHER_HEADSCALE_AUTH_KEY "
                "not provided; falling back to raw WireGuard")
            use_headscale = False
        else:
            hs_hostname = f"aither-{node_id}"
            try:
                hs_out = _tailscale_up(hs_url, hs_key, hs_hostname)
                report = {
                    "node_id": resp.get("node_id_assigned") or node_id,
                    "overlay_ip": overlay_ip,
                    "transport": "headscale",
                    "headscale_url": hs_url,
                    "hostname": hs_hostname,
                    "tailscale_output": hs_out,
                }
                logger.info(
                    "mesh: joined via Headscale as %s (overlay_ip=%s)",
                    report["node_id"], overlay_ip)
                return report
            except RuntimeError as exc:
                logger.warning(
                    "[join] Headscale transport failed (%s); falling back to "
                    "raw WireGuard", exc)
                use_headscale = False

    # Raw WireGuard transport (default or fallback)
    # AitherNet topology (server pubkey + endpoint). Default to the conductor
    # host on the AitherNet control port when not given explicitly.
    if not aithernet_url:
        from urllib.parse import urlparse
        host = urlparse(conductor_url).hostname or "localhost"
        scheme = urlparse(conductor_url).scheme or "https"
        aithernet_url = f"{scheme}://{host}:8125"
    server_pub, server_endpoint = await fetch_server_pubkey(aithernet_url, psk=psk)
    if not server_endpoint:
        # Fall back to the conductor host on the standard WG port.
        from urllib.parse import urlparse
        host = urlparse(conductor_url).hostname or "localhost"
        server_endpoint = f"{host}:{WG_PORT}"
    if not server_pub:
        raise RuntimeError("AitherNet topology returned no server_public_key")

    conf = _wg_conf(priv, overlay_ip, server_pub, server_endpoint, psk=psk)
    conf_path = write_config(iface, conf)
    up_out = bring_up(iface, conf_path)
    report = {
        "node_id": resp.get("node_id_assigned") or node_id,
        "overlay_ip": overlay_ip, "iface": iface,
        "transport": "wireguard",
        "server_endpoint": server_endpoint, "config": str(conf_path),
        "up_output": up_out, "handshake": has_handshake(iface),
    }
    logger.info("mesh: joined as %s (overlay_ip=%s, handshake=%s)",
                report["node_id"], overlay_ip, report["handshake"])
    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    import asyncio
    logging.basicConfig(
        level=os.environ.get("AITHER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser("adk.mesh", description="AitherMesh onboarding")
    sub = p.add_subparsers(dest="cmd", required=True)

    pj = sub.add_parser("join", help="onboard this node into the mesh overlay")
    # Resolve conductor URL: internal docker hostname falls back to public endpoint
    default_conductor = os.getenv(
        "AITHER_CONDUCTOR_URL", "https://aitheros-conductor:8193")
    resolved_conductor = _resolve_conductor_url(default_conductor)
    pj.add_argument("--conductor", default=resolved_conductor)
    pj.add_argument("--aithernet", default=os.getenv("AITHER_AITHERNET_URL", ""))
    pj.add_argument("--node-id", default=os.getenv("AITHER_NODE_ID", ""))
    pj.add_argument("--role", default=os.getenv("AITHER_MESH_ROLE", "worker"))
    pj.add_argument("--iface", default=DEFAULT_IFACE)
    pj.add_argument("--external-ip", default=os.getenv("AITHER_EXTERNAL_IP", ""))
    # Transport selection: raw WireGuard (default) or Headscale (NAT-friendly)
    pj.add_argument("--headscale", action="store_true",
        default=os.getenv("AITHER_MESH_TRANSPORT", "").lower() == "headscale",
        help="Use Headscale tunnel transport instead of raw WireGuard (for NAT'd networks)")
    pj.add_argument("--headscale-url",
        default=os.getenv("AITHER_HEADSCALE_URL", "https://hs.aitherium.com"),
        help="Headscale control server URL (default: https://hs.aitherium.com)")
    pj.add_argument("--headscale-key",
        default=os.getenv("AITHER_HEADSCALE_AUTH_KEY", ""),
        help="Headscale pre-generated auth key (from AITHER_HEADSCALE_AUTH_KEY)")

    sub.add_parser("status", help="show mesh transport status").add_argument(
        "--iface", default=DEFAULT_IFACE)

    args = p.parse_args(argv)
    if args.cmd == "join":
        report = asyncio.run(join(
            conductor_url=args.conductor, node_id=args.node_id or None,
            role=args.role, aithernet_url=args.aithernet or None,
            iface=args.iface, external_ip=args.external_ip or None,
            headscale=getattr(args, "headscale", False),
            headscale_url=getattr(args, "headscale_url", None),
            headscale_auth_key=getattr(args, "headscale_key", "")))
        print(json.dumps(report, indent=2))
        return 0 if report.get("handshake") or report.get("transport") == "headscale" else 2
    if args.cmd == "status":
        print(status(args.iface))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
