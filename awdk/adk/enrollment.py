"""Rich endpoint enrollment — register this device with AitherIdentity.

This is the convergence point for the self-hosted managed-agent experience: the
customer's box probes its own hardware + inference readiness and registers with
AitherIdentity's node spine (``POST /v1/nodes/register``), so the portal can show
the node (GPU, models, last-seen) and steer per-agent routing. A 60s heartbeat keeps
it live.

Nodes are stored in AitherDirectory as aitherDevice entries, tenant-scoped.

Distinct from the legacy ``adk/fleet_enroll.py`` path (``FederationLiteClient`` →
``/federation/register``), which only carries agents, not hardware. ``fleet_enroll``
now calls :func:`rich_enroll` first and falls back to the federation path — except
when identity REFUSED (401/402/403), which is surfaced verbatim and never papered
over by the federation path.

Inference detection is :func:`probe_inference`. It used to look only at Ollama
(:11434) and vLLM (:8120), so a phone running llama-server on :8099 or a laptop
running awnode on :8090 enrolled as ``inference_ready: false`` and the browser's
``remote`` rung had nothing to chat with. The probe now walks a fixed ladder and
the URL it settles on is persisted so every heartbeat re-probes the SAME server.

Everything here is best-effort: a failure logs and returns a non-enrolled result.
It MUST never raise into the caller — enrollment is optional and must not block
``adk start``.
"""

from __future__ import annotations

__all__ = [
    "rich_enroll",
    "build_registration",
    "heartbeat_loop",
    "probe_inference",
    "default_candidates",
    "InferenceProbe",
    "Candidate",
    "NODE_CLASSES",
    "INFERENCE_KINDS",
]

import asyncio
import json
import logging
import os
import platform
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

log = logging.getLogger("adk.enrollment")

_AITHER_DIR = Path.home() / ".aither"
_WORKSPACE_FILE = _AITHER_DIR / "workspace.json"

#: What kind of device this is. Sent as ``node_class`` on register; the portal
#: groups and bills on it. ``laptop`` is the default because it is the only class
#: a plain ``adk enroll`` on an unknown box can honestly claim.
NODE_CLASSES: Tuple[str, ...] = ("phone", "laptop", "sovereign")

#: What answered the inference probe. ``none`` means nothing did.
INFERENCE_KINDS: Tuple[str, ...] = ("llama-server", "awnode", "ollama", "vllm", "none")

#: Per-request budget for a local probe. A cold box answers "refused" in
#: microseconds; the timeout only matters for a port something else is squatting.
_PROBE_TIMEOUT = 1.5


class InferenceProbe(NamedTuple):
    """Result of :func:`probe_inference` — tuple-compatible with
    ``(models, inference_url, inference_kind, ready)``."""

    models: List[str]
    inference_url: str
    inference_kind: str
    ready: bool


class Candidate(NamedTuple):
    """One rung of the probe ladder: a base URL and the kind it would prove."""

    url: str
    kind: str


def default_candidates(host: str = "127.0.0.1") -> List[Candidate]:
    """The probe ladder, in order. Derived from the ports the doors actually use.

    1. ``$BONSAI_PORT`` (default 8080) — ``phone.sh`` runs llama-server here.
    2. 8099 — llama-server on a laptop (``adk quickstart-local``).
    3. 8090 — awnode; proven by ``/health`` AND ``/v1/models``.
    4. 11434 — Ollama, proven by its own ``/api/tags``.
    5. 8120 — vLLM.

    A duplicate port (``BONSAI_PORT=8099``) is probed once.
    """
    bonsai_port = (os.environ.get("BONSAI_PORT") or "").strip() or "8080"
    ladder = [
        Candidate(f"http://{host}:{bonsai_port}", "llama-server"),
        Candidate(f"http://{host}:8099", "llama-server"),
        Candidate(f"http://{host}:8090", "awnode"),
        Candidate(f"http://{host}:11434", "ollama"),
        Candidate(f"http://{host}:8120", "vllm"),
    ]
    seen: set = set()
    return [c for c in ladder if not (c.url in seen or seen.add(c.url))]


def _get_json(url: str, timeout: float = _PROBE_TIMEOUT) -> Tuple[int, Any]:
    """GET ``url`` and return ``(status, parsed_json_or_None)``.

    ``status`` is 0 when nothing answered (refused, timeout, DNS). A 200 whose body
    is not JSON returns ``(200, None)`` so callers can distinguish "answered with
    the wrong shape" from "did not answer". Uses stdlib urllib so a probe never
    needs httpx and a cold box returns fast.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            status = int(getattr(r, "status", 200) or 200)
    except urllib.error.HTTPError as e:
        return int(e.code), None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return 0, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def _openai_models(base: str) -> Tuple[bool, List[str]]:
    """``GET {base}/v1/models`` — a 200 with a JSON body proves an
    OpenAI-compatible server (awnode's rule). Returns ``(ok, model_ids)``."""
    status, data = _get_json(f"{base}/v1/models")
    if status != 200 or data is None:
        return False, []
    models: List[str] = []
    items = data.get("data", []) if isinstance(data, dict) else []
    for m in items or []:
        mid = m.get("id") if isinstance(m, dict) else None
        if mid:
            models.append(str(mid))
    return True, models


def _ollama_models(base: str) -> Tuple[bool, List[str]]:
    """``GET {base}/api/tags`` — Ollama's own listing. Returns ``(ok, names)``."""
    status, data = _get_json(f"{base}/api/tags")
    if status != 200 or not isinstance(data, dict) or "models" not in data:
        return False, []
    models: List[str] = []
    for m in data.get("models", []) or []:
        name = (m.get("name") or m.get("model")) if isinstance(m, dict) else None
        if name:
            models.append(str(name))
    return True, models


def _dedup(models: List[str]) -> List[str]:
    seen: set = set()
    return [m for m in models if not (m in seen or seen.add(m))]


def _probe_candidate(c: Candidate) -> Optional[InferenceProbe]:
    """Probe one rung with the check that proves ITS kind, or return None."""
    if c.kind == "ollama":
        ok, models = _ollama_models(c.url)
        return InferenceProbe(_dedup(models), c.url, "ollama", True) if ok else None
    if c.kind == "awnode":
        status, _ = _get_json(f"{c.url}/health")
        if status != 200:
            return None
        ok, models = _openai_models(c.url)
        return InferenceProbe(_dedup(models), c.url, "awnode", True) if ok else None
    ok, models = _openai_models(c.url)
    return InferenceProbe(_dedup(models), c.url, c.kind, True) if ok else None


def _fingerprint(base: str) -> Optional[str]:
    """Name the server behind an EXPLICIT url that already answered ``/v1/models``.

    Ollama has ``/api/tags``; awnode's ``/health`` says ``"service": "awnode"``;
    llama-server has ``/props``; vLLM has ``/version``. None of these is
    OpenAI-standard, which is why they identify the implementation.
    """
    status, data = _get_json(f"{base}/api/tags")
    if status == 200 and isinstance(data, dict) and "models" in data:
        return "ollama"
    status, data = _get_json(f"{base}/health")
    if status == 200 and data is not None and "awnode" in json.dumps(data).lower():
        return "awnode"
    status, _ = _get_json(f"{base}/props")
    if status == 200:
        return "llama-server"
    status, _ = _get_json(f"{base}/version")
    if status == 200:
        return "vllm"
    return None


def _normalize_base(url: str) -> str:
    """``http://host:port/v1/`` and ``http://host:port`` name the same server."""
    base = url.strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


def probe_inference(
    explicit_url: Optional[str] = None,
    *,
    candidates: Optional[Sequence[Candidate]] = None,
) -> InferenceProbe:
    """Find the local inference server this device should advertise.

    Args:
        explicit_url: A base URL the operator named (``--inference-url``). ``None``
            or ``"auto"`` walks the ladder. An explicit URL WINS: it is the only
            server probed, and if it does not answer the result is
            ``ready=False`` with that URL kept — never a silent fallback to
            something the operator did not name.
        candidates: The ladder to walk in auto mode (default
            :func:`default_candidates`). Tests pass stub servers here.

    Returns:
        :class:`InferenceProbe` ``(models, inference_url, inference_kind, ready)``.
        In auto mode with nothing listening: ``([], "", "none", False)``.
    """
    if explicit_url and explicit_url.strip().lower() != "auto":
        base = _normalize_base(explicit_url)
        ok, models = _openai_models(base)
        if not ok:
            log.info("Explicit inference url %s did not answer /v1/models", base)
            return InferenceProbe([], base, "none", False)
        kind = _fingerprint(base)
        if kind is None:
            # It IS OpenAI-compatible (that is what /v1/models proved) but none of
            # the implementation fingerprints matched. llama-server is the closest
            # generic label the registry accepts; say so rather than guess quietly.
            log.info(
                "Explicit inference url %s is OpenAI-compatible but unfingerprinted; "
                "reporting inference_kind=llama-server",
                base,
            )
            kind = "llama-server"
        return InferenceProbe(_dedup(models), base, kind, True)

    for c in candidates if candidates is not None else default_candidates():
        hit = _probe_candidate(c)
        if hit is not None:
            log.debug("Inference probe: %s at %s (%d models)", hit.inference_kind,
                      hit.inference_url, len(hit.models))
            return hit
    return InferenceProbe([], "", "none", False)


def build_registration(
    node_id: str,
    *,
    inference_url: Optional[str] = None,
    node_class: str = "laptop",
) -> Dict[str, Any]:
    """Probe hardware + local inference and build the EndpointRegistration payload.

    Reuses :func:`adk.hardware_probe.detect_system` for the hardware fields so the
    enrollment view matches what the first-run wizard detected.

    Args:
        node_id: Stable node identifier.
        inference_url: Explicit inference base URL, or ``None``/``"auto"`` to probe.
        node_class: One of :data:`NODE_CLASSES`.

    Raises:
        ValueError: ``node_class`` is not a known class (the CLI constrains it;
            a programmatic caller gets told instead of enrolling as garbage).
    """
    if node_class not in NODE_CLASSES:
        raise ValueError(f"node_class must be one of {NODE_CLASSES}, got {node_class!r}")

    try:
        from adk.hardware_probe import detect_system

        sysinfo = detect_system()
        ram_mb = int(round(sysinfo.ram_gb * 1024))
        cpu_count = int(sysinfo.cpu_cores or 0)
        gpu_name = sysinfo.gpu_name or ""
        gpu_vram_mb = int(sysinfo.gpu_vram_mb or 0)
        py_version = sysinfo.python_version or platform.python_version()
    except Exception as e:  # hardware probe is best-effort
        log.debug("Hardware probe failed, using minimal info: %s", e)
        ram_mb = cpu_count = gpu_vram_mb = 0
        gpu_name = ""
        py_version = platform.python_version()

    probe = probe_inference(inference_url)

    return {
        "node_id": node_id,
        "hostname": platform.node(),
        "platform": platform.system(),
        "platform_version": platform.release(),
        "python_version": py_version,
        "gpu_name": gpu_name,
        "gpu_vram_mb": gpu_vram_mb,
        "cpu_count": cpu_count,
        "ram_mb": ram_mb,
        "available_models": probe.models,
        "capabilities": ["code_search", "memory", "file_tools"],
        # Legacy booleans the older identity model still reads.
        "ollama_available": probe.inference_kind == "ollama",
        "vllm_available": probe.inference_kind == "vllm",
        "inference_ready": probe.ready,
        # The fields the portal's device list and the browser's remote rung need.
        # inference_url is OPAQUE to the server (never dereferenced there).
        "inference_url": probe.inference_url,
        "inference_kind": probe.inference_kind,
        "node_class": node_class,
    }


def _save_workspace(workspace: Dict[str, Any]) -> None:
    """Persist the returned workspace for local routing.

    Stores ONLY the non-sensitive routing fields — never ``settings``, which the
    server may populate with provider API-key material. Local routing needs only
    the roster + routing map; persisting secrets to a plaintext file would be a
    credential-at-rest leak. The file is also locked to 0600.
    """
    safe = {
        "workspace_id": workspace.get("workspace_id", ""),
        "name": workspace.get("name", ""),
        "tier": workspace.get("tier", ""),
        "agent_roster": workspace.get("agent_roster", []),
        "agent_routing": workspace.get("agent_routing", {}),
    }
    try:
        _AITHER_DIR.mkdir(parents=True, exist_ok=True)
        _WORKSPACE_FILE.write_text(json.dumps(safe, indent=2), encoding="utf-8")
        try:
            _WORKSPACE_FILE.chmod(0o600)
        except OSError:
            log.debug("chmod unsupported for %s", _WORKSPACE_FILE)
    except OSError as e:
        log.debug("Failed to persist workspace.json: %s", e)


async def heartbeat_loop(
    base_url: str,
    token: str,
    node_id: str,
    *,
    interval: int = 60,
    inference_url: Optional[str] = None,
    node_class: str = "laptop",
    max_beats: Optional[int] = None,
) -> None:
    """Background heartbeat — POST /v1/nodes/heartbeat every ``interval`` seconds.

    Every beat re-probes ``inference_url`` (the SAME url registration settled on,
    read back from ``node_auth.json`` by ``fleet_enroll``) and sends
    ``inference_url`` / ``inference_kind`` / ``inference_ready`` so the portal
    sees a phone whose llama-server died go not-ready within a minute.

    Re-registers (full payload) if the server reports the node as unknown (e.g.
    after the registry was reset). Failures are logged but never break the loop.

    Args:
        max_beats: Stop after this many beats (tests); ``None`` runs forever.
    """
    import httpx

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = base_url.rstrip("/")
    log.info("Starting endpoint heartbeat loop (interval=%ds)", interval)
    beats = 0
    while max_beats is None or beats < max_beats:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("Heartbeat loop cancelled")
            break
        beats += 1
        try:
            reg = build_registration(
                node_id, inference_url=inference_url, node_class=node_class
            )
            hb = {
                "node_id": node_id,
                "inference_ready": reg["inference_ready"],
                "available_models": reg["available_models"],
                "gpu_vram_mb": reg["gpu_vram_mb"],
                "inference_url": reg["inference_url"],
                "inference_kind": reg["inference_kind"],
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{base}/v1/nodes/heartbeat", json=hb, headers=headers
                )
                if resp.status_code == 200 and resp.json().get("status") == "unknown_node":
                    # Registry lost us — re-register with the full payload.
                    await client.post(
                        f"{base}/v1/nodes/register", json=reg, headers=headers
                    )
        except asyncio.CancelledError:
            log.info("Heartbeat loop cancelled")
            break
        except Exception as e:
            log.debug("Heartbeat error: %s", e)


def _persist_device_cert(register_response: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the device mTLS client cert that ``POST /v1/nodes/register`` already returned.

    The cert is bound to tenant+node (CN = devcert--<tenant>--<node>) so the cloud derives
    the tenant CRYPTOGRAPHICALLY instead of from a spoofable header.

    FIXED 2026-07-25: this used to POST to ``/v1/nodes/mtls-cert``, **a route that has
    never existed on the identity service**. Every enrollment logged
    ``mtls-cert request HTTP 405: {"detail":"Method Not Allowed"}`` — 405 rather than 404
    because the path matched the GET-only ``/v1/nodes/{node_id}`` route with
    node_id="mtls-cert". The failure was swallowed as "best-effort", so every node silently
    enrolled WITHOUT a device cert and fell back to bearer-token auth, which is exactly the
    spoofable-header posture the cert exists to remove. Meanwhile ``register_node`` already
    mints the cert and returns the full ``{certificate, private_key, chain, cn}`` bundle in
    its own response (identity_nodes.py, `response["mtls"]`) — so the second call was both
    broken AND redundant. Read what we were already handed.

    Args:
        register_response: The parsed JSON body of ``POST /v1/nodes/register``.

    Returns:
        ``{"success": bool, "mtls": {...}, "error": str}`` — never raises.
    """
    mtls_bundle = register_response.get("mtls", {}) or {}

    if not mtls_bundle.get("issued", bool(mtls_bundle.get("certificate"))):
        reason = mtls_bundle.get("reason", "no mtls bundle in register response")
        log.warning("Device cert NOT issued at registration: %s", reason)
        return {"success": False, "error": reason}

    if not mtls_bundle.get("certificate") or not mtls_bundle.get("private_key"):
        log.warning("Device cert bundle incomplete (no certificate/private_key)")
        return {"success": False, "error": "incomplete cert bundle"}

    try:
        from adk.sync import device_identity

        device_identity.save_enrolled_identity(mtls_bundle)
        log.info("Device mTLS cert enrolled and persisted (cn=%s)", mtls_bundle.get("cn", "?"))
        return {"success": True, "mtls": mtls_bundle}
    except Exception as e:
        log.warning("Failed to persist device cert: %s", e)
        return {"success": False, "error": f"cert persistence failed: {e}"}


async def rich_enroll(
    base_url: str,
    token: str,
    node_id: str,
    *,
    enable_heartbeat: bool = True,
    inference_url: Optional[str] = None,
    node_class: str = "laptop",
) -> Dict[str, Any]:
    """Register this device with the rich endpoint spine.

    Args:
        base_url: Control-plane base (Identity service) exposing ``/v1/nodes/*``.
        token: Bearer token from ``adk login`` (caller→tenant on the server).
        node_id: Stable node identifier.
        enable_heartbeat: Start the background heartbeat loop on success.
        inference_url: Explicit inference base URL, or ``None``/``"auto"`` to probe.
        node_class: One of :data:`NODE_CLASSES`.

    Returns:
        ``{"enrolled": bool, "node_id": str, "workspace": dict, "registration": dict,
        "public_url": str, ...}``. On any failure, ``{"enrolled": False, "error": str}``
        — never raises. When identity ANSWERED with a non-200 the result also carries
        ``http_status`` and the VERBATIM ``body`` (402 ``subscription_required``, 403
        ``device_quota_exceeded``) so the CLI can print exactly what the server said.
    """
    if not token:
        return {"enrolled": False, "error": "no auth token (run `adk login`)"}

    try:
        import httpx

        reg = build_registration(node_id, inference_url=inference_url, node_class=node_class)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        base = base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base}/v1/nodes/register", json=reg, headers=headers
            )
        if resp.status_code != 200:
            body = resp.text
            log.warning("Endpoint register HTTP %s: %s", resp.status_code, body[:200])
            return {
                "enrolled": False,
                "error": f"HTTP {resp.status_code}: {body[:200]}",
                "http_status": resp.status_code,
                "body": body,
                "url": f"{base}/v1/nodes/register",
            }

        data = resp.json()
        workspace = data.get("workspace", {}) or {}
        _save_workspace(workspace)

        # Registration already minted and returned the device client cert — persist it.
        # (It used to fire a second request at a route that does not exist; see
        # _persist_device_cert.)
        tenant_id = data.get("tenant_id", "")
        cert_result = _persist_device_cert(data)
        cert_enrolled = cert_result.get("success", False)

        if enable_heartbeat:
            try:
                asyncio.create_task(heartbeat_loop(
                    base, token, node_id,
                    inference_url=reg["inference_url"] or None,
                    node_class=node_class,
                ))
            except RuntimeError:
                # No running loop (sync context) — caller can start it later.
                log.debug("No event loop for heartbeat; skipping background task")

        log.info("Enrolled endpoint %s (tenant=%s, cert_enrolled=%s, inference=%s %s)",
                 node_id, tenant_id, cert_enrolled, reg["inference_kind"],
                 reg["inference_url"] or "-")
        return {
            "enrolled": True,
            "node_id": node_id,
            "tenant_id": tenant_id,
            "workspace_id": data.get("workspace_id", ""),
            "workspace": workspace,
            "registration": reg,
            "public_url": data.get("public_url", "") or "",
            "cert_enrolled": cert_enrolled,
            "_enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # Tenant-scoped capability token (identity_nodes.py's /v1/nodes/register
            # now mints one) — lets this node self-service its OWN gateway API key
            # afterward instead of reusing the enrolling user's own access token.
            "bearer_token": data.get("bearer_token", ""),
        }
    except Exception as e:
        log.warning("Rich enrollment failed: %s", e)
        return {"enrolled": False, "error": str(e)}
