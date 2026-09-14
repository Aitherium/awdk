"""Mesh ops pack — mesh_* agent tools.

Codifies the AitherMesh node lifecycle so any agent (or the operator) can drive
it without hand-SSHing: list registered mesh nodes, run a command on a node over
vault-backed SSH, enroll a node onto the Headscale control plane, register a
node's reachable endpoint (+ its SSH target) in the fleet registry, and deploy
an adk agent onto a node pointed at its own inference.

Design doctrine (same as node_bootstrap):
  * Fail SOFT — every tool returns a dict, never raises.
  * SSH creds are resolved SERVER-SIDE from the vault by name (never passed by a
    caller); a caller supplies only the node's host + a vault key name.
  * Control-plane calls carry the internal token; nothing trusts caller-supplied
    hosts for a security decision.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from typing import Any

import httpx

_GENESIS = os.getenv("AITHER_GENESIS_URL", "https://aitheros-genesis:8001").rstrip("/")
_PORTAL = os.getenv("AITHER_PORTAL_URL", "https://api.aitherium.com").rstrip("/")


def _verify() -> Any:
    """Internal-CA verify for genesis; never disables TLS verification."""
    try:
        from lib.security.TLSConfig import get_internal_httpx_verify  # type: ignore
        return get_internal_httpx_verify()
    except Exception:
        # Fall back to the bundled CA chain path if the helper is unavailable.
        for p in ("/app/AitherOS/Library/Data/tls/ca-chain.pem",
                  os.path.expanduser("~/.aither/ca-chain.pem")):
            if os.path.isfile(p):
                return p
        return True


def _headers() -> dict:
    tok = os.getenv("AITHER_INTERNAL_SECRET", "")
    h = {"Content-Type": "application/json"}
    if tok:
        # Genesis' INTERNAL mesh registry checks X-Internal-KEY (constant-time vs
        # AITHER_INTERNAL_SECRET); the older -Token name is kept for other callers.
        h["X-Internal-Key"] = tok
        h["X-Internal-Token"] = tok
    key = os.getenv("AITHER_API_KEY", "") or os.getenv("AITHER_PORTAL_TOKEN", "")
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def mesh_list_nodes() -> dict:
    """List the mesh nodes registered in the fleet endpoint registry.

    Returns {"nodes": [{name, invoke_url, reach, model, provider_hint, status,
    can_ssh}], "count": N} or {"error": ...}.

    Prefers the genesis INTERNAL /v1/agent/mesh-endpoints route (X-Internal-Key,
    NOT tier-gated) so a platform agent sees the mesh without a self_hosted_fleet
    entitlement; falls back to the tier-gated /agent-endpoints for a customer
    session that holds a bearer.
    """
    attempts = [(f"{_GENESIS}/v1/agent/mesh-endpoints?tenant=platform", True),
                (f"{_GENESIS}/v1/agent/agent-endpoints", False),
                (f"{_PORTAL}/api/genesis/v1/agent/agent-endpoints", False)]
    last = ""
    for url, internal in attempts:
        try:
            with httpx.Client(timeout=10.0, verify=_verify()) as c:
                r = c.get(url, headers=_headers())
            if r.status_code != 200:
                last = f"{url} -> {r.status_code}"
                continue
            body = r.json()
            rows = body.get("endpoints", body.get("agents", []))
            nodes = [{
                "name": a.get("name", ""),
                "invoke_url": a.get("invoke_url", ""),
                "reach": a.get("reach", "unknown"),
                "model": a.get("model", ""),
                "provider_hint": a.get("provider_hint", ""),
                "status": a.get("status", "unknown"),
                "can_ssh": bool(a.get("can_ssh") or a.get("ssh_host")),
            } for a in rows if a.get("name")]
            return {"nodes": nodes, "count": len(nodes), "source": url}
        except Exception as e:  # noqa: BLE001
            last = str(e)
    return {"error": "registry unreachable", "detail": last,
            "hint": "set AITHER_GENESIS_URL / AITHER_INTERNAL_SECRET; is genesis up?"}


def mesh_run_on_node(node: str, command: str, linux: bool = True,
                     timeout_s: int = 120) -> dict:
    """Run a command on a mesh node over vault-backed SSH (no manual keys).

    Delegates to AitherOS/tools/aither_node_ssh.py, which resolves the node's SSH
    key from the vault by name and runs the command. The node must be a known
    mesh node. Returns {"ok": bool, "output": str} or {"error": ...}.
    """
    if not node or not command:
        return {"error": "node and command are required"}
    runner = None
    for p in ("AitherOS/tools/aither_node_ssh.py",
              "/app/AitherOS/tools/aither_node_ssh.py",
              os.path.expanduser("~/AitherOS/tools/aither_node_ssh.py")):
        if os.path.isfile(p):
            runner = p
            break
    if not runner:
        return {"error": "aither_node_ssh.py not found",
                "hint": "run from the controller (repo root) where AitherOS/tools exists"}
    args = ["python", runner, "run", node]
    if linux:
        args.append("--linux")
    args += shlex.split(command)
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
        return {"ok": out.returncode == 0, "returncode": out.returncode,
                "output": (out.stdout or "") + (out.stderr or "")}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "detail": f"node command exceeded {timeout_s}s"}
    except Exception as e:  # noqa: BLE001
        return {"error": "run failed", "detail": str(e)}


def mesh_register_endpoint(name: str, invoke_url: str, reach: str = "mesh",
                           ssh_host: str = "", ssh_user: str = "",
                           vault_ssh_key: str = "", model: str = "",
                           provider_hint: str = "", tenant: str = "platform") -> dict:
    """Register (or update) a mesh node in the fleet endpoint registry, including
    its SSH target so the tunnel console can browser-SSH into it.

    Uses the ManagedAgentEndpointStore directly (cross-process safe; the running
    genesis reads the same store). vault_ssh_key names the AitherSecrets entry
    holding the node's SSH private key (default convention: <NAME>_SSH_PRIVATE_KEY).
    """
    if not name or not invoke_url:
        return {"error": "name and invoke_url are required"}
    if not vault_ssh_key and ssh_host:
        vault_ssh_key = name.upper().replace("-", "_") + "_SSH_PRIVATE_KEY"
    try:
        from lib.agent_packs.managed.agent_endpoints import ManagedAgentEndpointStore
    except Exception:
        return {"error": "registry store unavailable",
                "hint": "run where AitherOS/lib is importable (controller/genesis)"}
    try:
        st = ManagedAgentEndpointStore.for_tenant(tenant)
        rec = st.register(tenant, name, invoke_url, reach=reach, model=model,
                          provider_hint=provider_hint, ssh_host=ssh_host,
                          ssh_user=ssh_user, vault_ssh_key=vault_ssh_key)
        return {"ok": True, "name": name, "reach": rec.get("reach", reach),
                "invoke_url": rec.get("invoke_url", invoke_url),
                "ssh": f"{ssh_user}@{ssh_host}" if ssh_host else ""}
    except ValueError as e:
        return {"error": "invalid endpoint", "detail": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": "register failed", "detail": str(e)}


def mesh_enroll_node(node_host: str, ssh_user: str, ssh_key_path: str,
                     controller_lan_ip: str, hostname: str = "",
                     control_port: int = 8443, timeout_s: int = 120) -> dict:
    """Enroll a Linux node onto the LOCAL Headscale control plane over SSH:
    install the internal CA, add the /etc/hosts override, and `tailscale up`
    against https://headscale.aitherium.com:<port> with a fresh preauth key.

    Returns {"ok": bool, "output": str}. The preauth key is minted server-side
    (headscale container). WSL2/Windows nodes should use the 3220 playbook.
    """
    if not (node_host and ssh_user and ssh_key_path and controller_lan_ip):
        return {"error": "node_host, ssh_user, ssh_key_path, controller_lan_ip required"}
    hostname = hostname or node_host.replace(".", "-")
    # Mint a short-lived reusable preauth key from the headscale container.
    try:
        pak = subprocess.run(
            ["docker", "exec", "aitheros-headscale", "headscale", "preauthkeys",
             "create", "--user", "aither-nodes", "--reusable", "--expiration", "1h"],
            capture_output=True, text=True, timeout=30).stdout.strip().splitlines()[-1].strip()
    except Exception as e:  # noqa: BLE001
        return {"error": "preauth key mint failed", "detail": str(e),
                "hint": "is aitheros-headscale running on this host?"}
    if not pak or len(pak) < 20:
        return {"error": "preauth key mint returned nothing"}
    remote = (
        "sudo cp /tmp/aither-ca.pem /usr/local/share/ca-certificates/aither-internal.crt; "
        "sudo update-ca-certificates >/dev/null 2>&1; "
        "grep -q headscale.aitherium.com /etc/hosts || echo "
        f"'{controller_lan_ip} headscale.aitherium.com' | sudo tee -a /etc/hosts >/dev/null; "
        f"sudo tailscale up --login-server=https://headscale.aitherium.com:{control_port} "
        f"--authkey={pak} --force-reauth --accept-routes --hostname={hostname} 2>&1 | tail -3; "
        "tailscale ip -4 2>/dev/null | head -1"
    )
    ssh_base = ["ssh", "-i", ssh_key_path, "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new", f"{ssh_user}@{node_host}"]
    try:
        # Ship the CA cert first (best-effort; the node may already have it).
        ca = _find_ca()
        if ca:
            subprocess.run(["scp", "-i", ssh_key_path, "-o", "BatchMode=yes",
                            "-o", "StrictHostKeyChecking=accept-new", ca,
                            f"{ssh_user}@{node_host}:/tmp/aither-ca.pem"],
                           capture_output=True, text=True, timeout=30)
        out = subprocess.run(ssh_base + [remote], capture_output=True, text=True,
                             timeout=timeout_s)
        return {"ok": out.returncode == 0, "output": (out.stdout or "") + (out.stderr or ""),
                "hostname": hostname}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "detail": f"enroll exceeded {timeout_s}s"}
    except Exception as e:  # noqa: BLE001
        return {"error": "enroll failed", "detail": str(e)}


def _find_ca() -> str:
    for p in ("AitherOS/Library/Data/tls/ca-chain.pem",
              "/app/AitherOS/Library/Data/tls/ca-chain.pem"):
        if os.path.isfile(p):
            return p
    return ""


def mesh_deploy_agent(node_host: str, ssh_user: str, ssh_key_path: str,
                      overlay_ip: str, inference_url: str, node_id: str = "",
                      port: int = 8080, timeout_s: int = 240) -> dict:
    """Deploy an adk agent onto a mesh node pointed at its OWN inference, over SSH.

    Ensures adk on the node (pip into ~/.adk-venv) then runs
    `AITHER_LLM_BASE_URL=<inference_url> AITHER_MESH_OVERLAY_IP=<overlay_ip>
    adk up --yes --force --reach mesh --name <node_id> --port <port>` — the node
    uses ITS OWN token (never transit one). Returns the parsed adk-up JSON.
    """
    if not (node_host and ssh_user and ssh_key_path and inference_url):
        return {"error": "node_host, ssh_user, ssh_key_path, inference_url required"}
    node_id = node_id or node_host.replace(".", "-")
    remote = (
        "([ -x ~/.adk-venv/bin/adk ] || (python3 -m venv ~/.adk-venv && "
        "~/.adk-venv/bin/pip install -q --upgrade pip awdk)) >/dev/null 2>&1; "
        f"AITHER_LLM_BASE_URL={shlex.quote(inference_url)} "
        f"AITHER_MESH_OVERLAY_IP={shlex.quote(overlay_ip)} "
        f"~/.adk-venv/bin/adk up --yes --force --reach mesh --name {shlex.quote(node_id)} "
        f"--port {port} 2>&1 | tail -4"
    )
    ssh_base = ["ssh", "-i", ssh_key_path, "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new", f"{ssh_user}@{node_host}"]
    try:
        out = subprocess.run(ssh_base + [remote], capture_output=True, text=True,
                             timeout=timeout_s)
        parsed = None
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                except Exception:
                    pass
        return {"ok": out.returncode == 0, "node": node_id,
                "adk_up": parsed, "output": (out.stdout or "") + (out.stderr or "")}
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "detail": f"deploy exceeded {timeout_s}s"}
    except Exception as e:  # noqa: BLE001
        return {"error": "deploy failed", "detail": str(e)}
