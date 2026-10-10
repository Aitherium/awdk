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
    "start_heartbeat",
    "stop_heartbeat",
    "resume_heartbeat",
    "heartbeat_status",
    "classify_refusal",
    "probe_inference",
    "advertised_inference_url",
    "inference_probe_url",
    "save_advertised_inference_url",
    "validate_advertised_inference_url",
    "default_candidates",
    "InferenceProbe",
    "Candidate",
    "NODE_CLASSES",
    "INFERENCE_KINDS",
]
from adk.device_class import default_node_class, resolve_stored  # noqa: E402

import asyncio
import json
import logging
import os
import platform
import re
import time
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple,
)

log = logging.getLogger("adk.enrollment")

_AITHER_DIR = Path.home() / ".aither"
_WORKSPACE_FILE = _AITHER_DIR / "workspace.json"

#: What kind of device this is. Sent as ``node_class`` on register; the portal
#: groups and bills on it. ``laptop`` is the default because it is the only class
#: a plain ``adk enroll`` on an unknown box can honestly claim.
NODE_CLASSES: Tuple[str, ...] = ("phone", "laptop", "desktop", "deck", "spark", "sovereign")

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
    6. 1234 — LM Studio's local server, proven by ``/v1/models`` (added
       2026-09-21 from the Personal-AI-Router intake: it was the one desktop
       engine no ladder here probed, so a laptop running only LM Studio
       enrolled as ``inference_ready: false``).

    A duplicate port (``BONSAI_PORT=8099``) is probed once.
    """
    bonsai_port = (os.environ.get("BONSAI_PORT") or "").strip() or "8080"
    ladder = [
        Candidate(f"http://{host}:{bonsai_port}", "llama-server"),
        Candidate(f"http://{host}:8099", "llama-server"),
        Candidate(f"http://{host}:8090", "awnode"),
        Candidate(f"http://{host}:11434", "ollama"),
        Candidate(f"http://{host}:8120", "vllm"),
        Candidate(f"http://{host}:1234", "lmstudio"),
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


#: The inference URL a device may be told to advertise (``advertise-inference``):
#: http(s), a host (name, IPv4 or bracketed IPv6) and an explicit port, optionally
#: ``/v1``. No user info, query, fragment, whitespace or control characters. Same
#: rule as identity_node_commands.ADVERTISE_URL_RE.
ADVERTISE_URL_RE = re.compile(
    r"\Ahttps?://(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])"
    r":[0-9]{1,5}(?:/v1)?/?\Z")
ADVERTISE_URL_MAX = 256


def _advertise_path() -> Path:
    """``~/.aither/node-inference.json`` (``$AITHER_HOME`` when set, read per call)."""
    home = os.environ.get("AITHER_HOME") or str(Path.home() / ".aither")
    return Path(home) / "node-inference.json"


def validate_advertised_inference_url(url: str) -> str:
    """The normalised URL to advertise, '' to clear ('' or ``auto``); ValueError otherwise."""
    raw = str(url or "")
    if raw.strip().lower() in ("", "auto"):
        return ""
    if len(raw) > ADVERTISE_URL_MAX or not ADVERTISE_URL_RE.match(raw):
        raise ValueError("url must be http(s)://host:port[/v1], at most 256 characters")
    port = int(raw.split("://", 1)[1].rsplit(":", 1)[1].split("/", 1)[0])
    if not 0 < port < 65536:
        raise ValueError("url port must be 1-65535")
    return _normalize_base(raw)


def save_advertised_inference_url(url: str, probe_url: Optional[str] = None) -> str:
    """Persist the URL the heartbeat advertises (the ``advertise-inference`` command);
    '' / ``auto`` removes the file. Returns what is now stored. Raises ValueError for a
    URL (or ``probe_url``) outside :data:`ADVERTISE_URL_RE`, OSError when it cannot be
    written.

    ``probe_url`` is where THIS host reaches the same server when the advertised URL is
    not reachable from here (a WSL2 model behind a Windows portproxy: peers use the LAN
    URL, the box itself only reaches ``127.0.0.1``). Each call writes the whole record,
    so omitting it clears a previously stored probe URL."""
    clean = validate_advertised_inference_url(url)
    probe = validate_advertised_inference_url(probe_url or "")
    path = _advertise_path()
    if not clean:
        if path.exists():
            path.unlink()
        return ""
    record: Dict[str, Any] = {"url": clean, "set_at": int(time.time())}
    if probe:
        record["probe_url"] = probe
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)
    return clean


def advertised_inference_url() -> str:
    """The inference URL this host pins: ``AITHER_NODE_INFERENCE_URL`` when set, else the
    one the owner sent with ``advertise-inference`` (re-validated), else ''. Read on every
    call, so a heartbeat picks a change up on its next beat."""
    env = (os.environ.get("AITHER_NODE_INFERENCE_URL") or "").strip()
    if env:
        return env
    try:
        data = json.loads(_advertise_path().read_text(encoding="utf-8"))
        return validate_advertised_inference_url(str(data.get("url") or ""))
    except (OSError, ValueError, AttributeError):
        return ""


def inference_probe_url(advertised: str) -> str:
    """Where to PROBE the server advertised at ``advertised``, or '' to probe it directly.

    ``AITHER_NODE_INFERENCE_PROBE_URL`` wins; otherwise the ``probe_url`` stored next to
    the owner's ``advertise-inference`` URL, used only while that stored URL is the one
    being advertised (a probe URL never pairs with a different server). Both are checked
    with :func:`validate_advertised_inference_url`; an invalid value is ignored (logged),
    so the advertised URL is probed as before."""
    env = (os.environ.get("AITHER_NODE_INFERENCE_PROBE_URL") or "").strip()
    if env:
        try:
            return validate_advertised_inference_url(env)
        except ValueError as exc:
            log.warning("AITHER_NODE_INFERENCE_PROBE_URL ignored: %s", exc)
            return ""
    try:
        data = json.loads(_advertise_path().read_text(encoding="utf-8"))
        stored = validate_advertised_inference_url(str(data.get("url") or ""))
        if not stored or stored != _normalize_base(advertised):
            return ""
        return validate_advertised_inference_url(str(data.get("probe_url") or ""))
    except (OSError, ValueError, AttributeError):
        return ""


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
    if not explicit_url and candidates is None:
        # A box whose server is off the ladder (a pool router on a non-standard port such as
        # :8114) names it once in its environment; the heartbeat then advertises it on
        # every beat. Dedicated name: AITHER_INFERENCE_URL already means the CLOUD
        # inference base elsewhere in adk and must never be advertised as this node's.
        # Without the env, the URL the owner set over the command channel
        # (``advertise-inference``, ~/.aither/node-inference.json) is used.
        explicit_url = advertised_inference_url() or None
    if explicit_url and explicit_url.strip().lower() != "auto":
        base = _normalize_base(explicit_url)
        # The ADVERTISED url is always ``base``; a probe url (env or advertise file)
        # only changes where THIS host checks that the server answers.
        target = inference_probe_url(base) or base
        ok, models = _openai_models(target)
        if not ok:
            log.info("Explicit inference url %s did not answer /v1/models%s", base,
                     f" (probed at {target})" if target != base else "")
            return InferenceProbe([], base, "none", False)
        kind = _fingerprint(target)
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
    node_class: str = "",
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
    node_class = node_class or default_node_class()
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
        sysinfo = None
        ram_mb = cpu_count = gpu_vram_mb = 0
        gpu_name = ""
        py_version = platform.python_version()

    probe = probe_inference(inference_url)
    lending = _lending_fields(sysinfo, probe)

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
        # What this node LENDS (probed AND opted in; empty by default -- lending is
        # opt-in) plus every kind's probe verdict. See adk.node_capabilities.
        "capabilities": lending["capabilities"],
        "capability_detail": lending["capability_detail"],
        # Legacy booleans the older identity model still reads.
        "ollama_available": probe.inference_kind == "ollama",
        "vllm_available": probe.inference_kind == "vllm",
        "inference_ready": probe.ready,
        # The fields the portal's device list and the browser's remote rung need.
        # inference_url is OPAQUE to the server (never dereferenced there).
        "inference_url": probe.inference_url,
        "inference_kind": probe.inference_kind,
        "node_class": node_class,
        # This device's signing key (public half); the user's other devices trust
        # what it signs while it stays enrolled. Absent without awseal.
        **_identity_fields(),
    }


def _lending_fields(sysinfo: Any, probe: InferenceProbe) -> Dict[str, Any]:
    try:
        from adk.node_capabilities import advertise_this_node
        return advertise_this_node(sysinfo=sysinfo, inference_kind=probe.inference_kind,
                                   inference_ready=probe.ready)
    except Exception as e:  # noqa: BLE001 -- a failed probe lends nothing, never blocks enrollment
        log.warning("capability probe failed, advertising no lending: %s", e)
        return {"capabilities": [], "capability_detail": {}}


def _identity_fields() -> dict:
    try:
        from adk.device_identity import registration_fields
        return registration_fields()
    except Exception as e:  # noqa: BLE001 -- identity must never block enrollment
        log.debug("device identity unavailable: %s", e)
        return {}


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


# What the heartbeat last did, for the daemon's /health. Without it a node whose
# beats are being refused (expired sign-in, revoked device) looks identical to a
# healthy one from this side, and "offline" is only visible on the owner's
# device page. Never holds the token.
_HEARTBEAT_FRESH_BEATS = 3  # ``online`` = an accepted beat within this many intervals


def _new_heartbeat_state() -> Dict[str, Any]:
    return {
        "node_id": "",
        "interval": 0,
        "started_at": 0.0,
        "beats": 0,
        "last_attempt_at": 0.0,
        "last_ok_at": 0.0,
        "last_status": None,
        "last_result": "never",
        "last_error": "",
        "consecutive_failures": 0,
        "not_started_reason": "",
    }


_heartbeat_state: Dict[str, Any] = _new_heartbeat_state()

# ONE heartbeat task per process, and a reference to it: ``asyncio.create_task``
# with the result dropped is only weakly held by the loop, so the task could be
# collected mid-flight, and every repeat enrollment stacked another loop.
_heartbeat_task: Optional["asyncio.Task[None]"] = None


def _record_beat(status: Optional[int], result: str, error: str = "") -> None:
    """Record one beat's outcome. ``result`` is ``ok`` | ``reregistered`` |
    ``refused`` | ``error``."""
    now = time.time()
    st = _heartbeat_state
    st["beats"] += 1
    st["last_attempt_at"] = now
    st["last_status"] = status
    st["last_result"] = result
    st["last_error"] = error[:200]
    if result in ("ok", "reregistered"):
        st["last_ok_at"] = now
        st["consecutive_failures"] = 0
    else:
        st["consecutive_failures"] += 1
        # The first failure of a run is worth a line at WARNING; the rest stay quiet.
        if st["consecutive_failures"] == 1:
            log.warning("Node heartbeat %s (status=%s): %s", result, status, error[:200])


def _holds_lease(facet: str, interval: float) -> bool:
    """One heartbeat per device: True when this process holds the device lease."""
    try:
        from adk.device_identity import claim_lease
        return claim_lease(facet, interval)
    except Exception as e:  # noqa: BLE001 -- no lease machinery: beat as before
        log.debug("device lease unavailable: %s", e)
        return True


def _release_lease(facet: str) -> None:
    try:
        from adk.device_identity import release_lease
        release_lease(facet)
    except Exception as e:  # noqa: BLE001
        log.debug("device lease release failed: %s", e)


def _effective_facet(facet: str) -> str:
    try:
        from adk.device_identity import effective_facet
        return effective_facet(facet)
    except Exception:  # noqa: BLE001
        return facet


def _record_standby() -> None:
    """Another program on this computer beats for the device: nothing was sent,
    and that is not a failure."""
    st = _heartbeat_state
    st["last_attempt_at"] = time.time()
    st["last_result"] = "standby"
    st["last_error"] = ""


def heartbeat_status() -> Dict[str, Any]:
    """The node heartbeat's state, shaped for ``/health``.

    ``age_seconds`` is the time since the last beat the platform ACCEPTED
    (``None`` when none has been), and ``online`` is true only while the loop is
    running and that age is within a few intervals.
    """
    st = _heartbeat_state
    running = _heartbeat_task is not None and not _heartbeat_task.done()
    now = time.time()
    age = round(now - st["last_ok_at"], 1) if st["last_ok_at"] else None
    interval = st["interval"] or 60
    return {
        "running": running,
        "online": bool(
            running and age is not None and age <= interval * _HEARTBEAT_FRESH_BEATS
        ),
        "node_id": st["node_id"],
        "interval": st["interval"],
        "beats": st["beats"],
        "age_seconds": age,
        "last_attempt_age_seconds": (
            round(now - st["last_attempt_at"], 1) if st["last_attempt_at"] else None
        ),
        "last_status": st["last_status"],
        "last_result": st["last_result"],
        "last_error": st["last_error"],
        "consecutive_failures": st["consecutive_failures"],
        "not_started_reason": "" if running else st["not_started_reason"],
    }


def start_heartbeat(
    base_url: str,
    token: str,
    node_id: str,
    *,
    registered: bool = False,
    **kwargs: Any,
) -> bool:
    """Start this process's node heartbeat, replacing one already running.

    The newest credentials win: a fresh enrollment supersedes a loop resumed from
    the stored node record instead of running beside it.

    Args:
        registered: The node was registered a moment ago, which counts as its
            first accepted beat. Leave False when resuming from a stored record
            so ``/health`` does not claim a beat the platform never saw.
        **kwargs: Passed to :func:`heartbeat_loop`.

    Returns:
        True if the loop was scheduled; False when there is no running event loop
        (a synchronous caller) — never raises.
    """
    global _heartbeat_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("No event loop for heartbeat; skipping background task")
        _heartbeat_state["not_started_reason"] = "no event loop"
        return False
    if _heartbeat_task is not None and not _heartbeat_task.done():
        _heartbeat_task.cancel()
    _heartbeat_state.update(_new_heartbeat_state())
    _heartbeat_state.update({
        "node_id": node_id,
        "interval": int(kwargs.get("interval", 60)),
        "started_at": time.time(),
    })
    if registered:
        _heartbeat_state.update({
            "last_ok_at": time.time(), "last_status": 200, "last_result": "registered",
        })
    _heartbeat_task = loop.create_task(heartbeat_loop(base_url, token, node_id, **kwargs))
    return True


async def stop_heartbeat() -> None:
    """Cancel the node heartbeat (daemon shutdown). Safe when none is running."""
    global _heartbeat_task
    task, _heartbeat_task = _heartbeat_task, None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        log.debug("Node heartbeat cancelled")
    except Exception as e:  # noqa: BLE001 -- shutdown must finish
        log.debug("Node heartbeat ended with %s", e)


def resume_heartbeat(
    token: str,
    *,
    default_base_url: str,
    token_provider: Optional[Callable[[], str]] = None,
    interval: int = 60,
) -> Dict[str, Any]:
    """Resume the heartbeat from the stored node record (``node_auth.json``).

    A registered device has to keep beating after the process that registered it
    exits; this is what a daemon calls on start. It does nothing when a heartbeat
    is already running, when the device was never registered, or when there is no
    sign-in to beat with — and says which.

    Returns:
        ``{"started": bool, "reason": str, "node_id": str}``.
    """
    if _heartbeat_task is not None and not _heartbeat_task.done():
        return {"started": False, "reason": "already running",
                "node_id": _heartbeat_state["node_id"]}
    try:
        from adk.fleet_enroll import _load_node_auth

        node_auth = _load_node_auth()
    except Exception as e:  # noqa: BLE001 -- an unreadable record is "not registered"
        log.debug("node record unreadable: %s", e)
        node_auth = {}
    if not isinstance(node_auth, dict):
        node_auth = {}
    node_id = str(node_auth.get("node_id") or "")

    def _skip(reason: str) -> Dict[str, Any]:
        _heartbeat_state["not_started_reason"] = reason
        return {"started": False, "reason": reason, "node_id": node_id}

    if not node_id:
        return _skip("not registered")
    device_token = str(node_auth.get("bearer_token") or "")
    if not token and device_token and node_auth.get("enrolled_via") == "pairing-code":
        # Paired with a code: no person is signed in here, so the device beats as
        # itself with the capability token its registration answer carried.
        started = start_heartbeat(
            str(node_auth.get("enroll_base") or default_base_url), device_token, node_id,
            interval=interval, inference_url=node_auth.get("inference_url") or None,
            node_class=resolve_stored(node_auth.get("node_class"), node_auth.get("node_class_source")), device=True,
            beat_immediately=True,
        )
        if not started:
            return {"started": False, "reason": "no event loop", "node_id": node_id}
        log.info("Resumed device-token heartbeat for %s", node_id)
        return {"started": True, "reason": "", "node_id": node_id}
    if node_auth.get("mode") != "rich":
        # A legacy hub registration beats on its own loop (fleet_enroll).
        return _skip("not an identity registration")
    if not token:
        return _skip("no sign-in on this device")
    fallback = (device_token if node_auth.get("enrolled_via") == "pairing-code" else "")
    started = start_heartbeat(
        str(node_auth.get("enroll_base") or default_base_url),
        token,
        node_id,
        interval=interval,
        inference_url=node_auth.get("inference_url") or None,
        node_class=resolve_stored(node_auth.get("node_class"), node_auth.get("node_class_source")),
        token_provider=token_provider,
        beat_immediately=True,
        **({"device_fallback_token": fallback} if fallback else {}),
    )
    if not started:
        return {"started": False, "reason": "no event loop", "node_id": node_id}
    log.info("Resumed node heartbeat for %s from the stored registration", node_id)
    return {"started": True, "reason": "", "node_id": node_id}


#: Platform refusals a caller should render as their own state, not a generic
#: failure. Identity answers both with HTTP 402 and a ``detail.error`` code.
_REFUSAL_CODES: Tuple[str, ...] = ("subscription_required", "device_quota_exceeded")


def classify_refusal(http_status: Optional[int], body: Any) -> Optional[Dict[str, Any]]:
    """Name a 402 / quota refusal from the platform, or return None.

    Args:
        http_status: The status the platform answered with.
        body: The response body — raw text or already-parsed JSON.

    Returns:
        ``{"code", "http_status", "hint", "current", "limit", "upgrade"}`` (the
        platform's own fields, verbatim where present) when the answer is a 402
        or carries a known refusal code; otherwise None.
    """
    parsed: Any = body
    if isinstance(body, (str, bytes)):
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
    detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else None
    if not isinstance(detail, dict):
        detail = {}
    code = str(detail.get("error") or detail.get("code") or "")
    if code not in _REFUSAL_CODES:
        if http_status != 402:
            return None
        code = "payment_required"
    upgrade = detail.get("upgrade") if isinstance(detail.get("upgrade"), dict) else {}
    return {
        "code": code,
        "http_status": int(http_status or 402),
        "hint": str(detail.get("hint") or ""),
        "current": detail.get("current"),
        "limit": detail.get("limit"),
        "upgrade": upgrade,
    }


async def heartbeat_loop(
    base_url: str,
    token: str,
    node_id: str,
    *,
    interval: int = 60,
    inference_url: Optional[str] = None,
    node_class: str = "",
    max_beats: Optional[int] = None,
    reach_provider: Optional[Callable[[], str]] = None,
    harness_provider: Optional[Callable[[], Tuple[str, bool]]] = None,
    token_provider: Optional[Callable[[], str]] = None,
    beat_immediately: bool = False,
    device: bool = False,
    facet: str = "daemon",
    device_fallback_token: str = "",
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
        reach_provider: Called each beat for the CURRENT reach kind
            (``wg`` | ``ws`` | ``none``). It is a callable, not a value, because
            reach is the one field that must not be sticky: a phone whose reverse
            link dropped has to stop claiming reach within a minute, or the
            owner's device page offers a machine that cannot answer.
        harness_provider: Called each beat for ``(harness_url, harness_ready)``.
        token_provider: Called each beat for the CURRENT bearer; an empty answer
            keeps the previous one. A sign-in that was refreshed on disk is
            picked up without restarting the loop.
        beat_immediately: Send the first beat at once instead of after
            ``interval`` — for a loop resumed from a stored registration, where
            nothing has told the platform this node is back.
        device: ``token`` is the DEVICE's capability token (a machine paired
            with a code, no person signed in): beat and report on
            ``/v1/nodes/device/*``, and never try to re-register with it.
        facet: Which program on this computer is beating (``daemon`` for adk and
            node_beat). Only the facet holding the device lease in
            ``~/.aither/device.json`` sends anything; the others stand by.
        device_fallback_token: The device's own capability token (a pairing-code
            registration). When a person's sign-in beat answers 401, the loop
            switches to beating as the device instead of failing forever.
    """
    import httpx

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = base_url.rstrip("/")
    log.info("Starting endpoint heartbeat loop (interval=%ds)", interval)
    # ONE client for the loop's lifetime. A fresh AsyncClient per beat built a
    # fresh SSL context per beat, and on Windows that walks the system
    # certificate store -- the daemon's largest remaining idle CPU cost once its
    # file polls were cached (measured 2026-09-27). Same verify policy as before.
    holder = {"client": httpx.AsyncClient(timeout=10.0)}

    async def _renew() -> Any:
        # A refused or failed beat gets a NEW client. Measured 2026-10-03: after an
        # identity redeploy the long-lived client kept answering 404 for 12 minutes
        # while a fresh client on the same box got 200 -- only a restart healed it.
        old, holder["client"] = holder["client"], httpx.AsyncClient(timeout=10.0)
        try:
            await old.aclose()
        except Exception as e:  # noqa: BLE001 -- a client that will not close is dropped
            log.debug("old heartbeat client close failed: %s", e)
        return holder["client"]

    if os.name == "nt":
        # Let this PC's WSL distros find the host they run on (one device, not two).
        try:
            from adk.host_identity import write_host_identity
            await asyncio.to_thread(write_host_identity)
        except Exception as e:  # noqa: BLE001 -- the beat matters more than the file
            log.debug("host identity not written: %s", e)

    try:
        await _heartbeat_beats(
            holder["client"], base, headers, node_id, interval=interval,
            inference_url=inference_url, node_class=node_class, max_beats=max_beats,
            reach_provider=reach_provider, harness_provider=harness_provider,
            token_provider=token_provider, beat_immediately=beat_immediately,
            device=device, renew=_renew, facet=facet,
            device_fallback_token=device_fallback_token,
        )
    finally:
        _release_lease(facet)
        await holder["client"].aclose()


async def _heartbeat_beats(
    client: Any,
    base: str,
    headers: Dict[str, str],
    node_id: str,
    *,
    interval: int,
    inference_url: Optional[str],
    node_class: str,
    max_beats: Optional[int],
    reach_provider: Optional[Callable[[], str]],
    harness_provider: Optional[Callable[[], Tuple[str, bool]]],
    token_provider: Optional[Callable[[], str]] = None,
    beat_immediately: bool = False,
    device: bool = False,
    renew: Optional[Callable[[], Awaitable[Any]]] = None,
    facet: str = "daemon",
    device_fallback_token: str = "",
) -> None:
    """The beat loop of :func:`heartbeat_loop`, on a caller-owned client. ``renew``
    swaps in a fresh client after a refused or failed beat."""
    beat_path = "/v1/nodes/device/heartbeat" if device else "/v1/nodes/heartbeat"
    beats = 0
    while max_beats is None or beats < max_beats:
        if beats or not beat_immediately:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                log.info("Heartbeat loop cancelled")
                break
        beats += 1
        if not _holds_lease(facet, interval):
            _record_standby()
            continue
        try:
            if token_provider is not None:
                try:
                    fresh = str(token_provider() or "")
                except Exception as e:  # noqa: BLE001 -- keep the bearer we have
                    log.debug("token_provider failed: %s", e)
                    fresh = ""
                if fresh:
                    headers = {**headers, "Authorization": f"Bearer {fresh}"}
            # build_registration probes the inference server with a blocking
            # urlopen; off the loop so a slow probe never stalls the daemon.
            # A URL pinned on this host (env, or the owner's advertise-inference
            # command) beats the one registration stored, and is re-read every beat.
            reg = await asyncio.to_thread(
                build_registration,
                node_id, inference_url=advertised_inference_url() or inference_url,
                node_class=node_class,
            )
            hb = {
                "node_id": node_id,
                "inference_ready": reg["inference_ready"],
                "available_models": reg["available_models"],
                "gpu_vram_mb": reg["gpu_vram_mb"],
                "inference_url": reg["inference_url"],
                "inference_kind": reg["inference_kind"],
                # Re-probed every beat: a `lend:` / AITHER_LEND change reaches
                # Identity on the next beat, not only on re-enrollment.
                "capabilities": reg.get("capabilities") or [],
                "capability_detail": reg.get("capability_detail") or {},
                # The lease holder: Identity refreshes this facet's last_seen (and,
                # with two serving facets on one PC, keeps each one's endpoint).
                "facet": _effective_facet(facet),
                # Corrects records enrolled under the old hardcoded 'laptop' default.
                "node_class": reg.get("node_class") or node_class,
            }
            if reach_provider is not None:
                try:
                    hb["reach_kind"] = str(reach_provider() or "none")
                except Exception as e:  # noqa: BLE001 -- reach never breaks a beat
                    log.debug("reach_provider failed: %s", e)
            if harness_provider is not None:
                try:
                    h_url, h_ready = harness_provider()
                    hb["harness_url"] = str(h_url or "")
                    hb["harness_ready"] = bool(h_ready)
                except Exception as e:  # noqa: BLE001
                    log.debug("harness_provider failed: %s", e)
            try:
                from adk.restartable_units import restartable_units

                units = restartable_units()
                if units:  # the host's own list; the server refuses a restart off it
                    hb["restartable_units"] = units
            except Exception as e:  # noqa: BLE001 -- the list never breaks a beat
                log.debug("restartable_units failed: %s", e)
            try:
                from adk.node_commands import appliance_names

                appliances = appliance_names()
                if appliances:  # this host's registry; the server refuses a name off it
                    hb["appliances"] = appliances
            except Exception as e:  # noqa: BLE001 -- the registry never breaks a beat
                log.debug("appliance_names failed: %s", e)
            resp = await client.post(f"{base}{beat_path}", json=hb, headers=headers)
            status = int(resp.status_code)
            if status == 200 and resp.json().get("status") == "unknown_node" and device:
                # A device token cannot re-register: the device was removed.
                _record_beat(status, "refused", "this device was removed from the workspace")
            elif status == 200 and resp.json().get("status") == "unknown_node":
                # Registry lost us — re-register with the full payload.
                again = await client.post(
                    f"{base}/v1/nodes/register", json=reg, headers=headers
                )
                again_status = int(getattr(again, "status_code", 0) or 0)
                if again_status == 200:
                    _record_beat(again_status, "reregistered")
                else:
                    _record_beat(again_status, "refused",
                                 f"re-register answered HTTP {again_status}")
            elif status == 200:
                _record_beat(status, "ok")
                cmds = resp.json().get("commands")
                if cmds:
                    await _run_delivered_commands(client, base, headers, node_id, reg, cmds,
                                                  device=device)
            else:
                _record_beat(status, "refused", f"heartbeat answered HTTP {status}")
                if status == 401 and not device and device_fallback_token:
                    # A stale person sign-in on a code-paired machine (measured
                    # 2026-10-07 on the optiplex: a July auth.json shadowed the
                    # device token and every beat answered 401). The device can
                    # always beat as itself; switch once and keep going.
                    log.warning("Sign-in heartbeat refused (401); beating as the paired device")
                    device = True
                    beat_path = "/v1/nodes/device/heartbeat"
                    token_provider = None
                    headers = {**headers, "Authorization": f"Bearer {device_fallback_token}"}
                if renew is not None:
                    client = await renew()
        except asyncio.CancelledError:
            log.info("Heartbeat loop cancelled")
            break
        except Exception as e:
            log.debug("Heartbeat error: %s", e)
            _record_beat(None, "error", f"{e.__class__.__name__}: {e}")
            if renew is not None:
                try:
                    client = await renew()
                except Exception as re:  # noqa: BLE001 -- keep the old client, keep beating
                    log.debug("heartbeat client renew failed: %s", re)


async def _run_delivered_commands(
    client: Any,
    base: str,
    headers: Dict[str, str],
    node_id: str,
    reg: Dict[str, Any],
    cmds: Any,
    *,
    device: bool = False,
) -> None:
    """Run the commands a beat delivered (``adk.node_commands`` decides which) and
    report each result. A device enrolled before the channel existed has no key
    yet: one re-registration fetches it. Never raises into the beat loop."""
    from adk import node_commands

    try:
        key = node_commands.load_key(node_id)
        if not key and not device:
            again = await client.post(f"{base}/v1/nodes/register", json=reg, headers=headers)
            if int(getattr(again, "status_code", 0) or 0) == 200:
                key = str(again.json().get("command_key") or "")
                node_commands.save_key(node_id, key)
        results = await asyncio.to_thread(node_commands.run_commands, cmds, node_id, key)
        for res in results:
            path = (f"/v1/nodes/device/results/{res['id']}" if device
                    else f"/v1/nodes/{node_id}/commands/{res['id']}/result")
            r = await client.post(f"{base}{path}", json=res, headers=headers)
            if int(getattr(r, "status_code", 0) or 0) != 200:
                log.warning("command %s result refused: HTTP %s", res["id"], r.status_code)
    except Exception as e:  # noqa: BLE001 -- control never breaks liveness
        log.warning("node commands not run: %s: %s", e.__class__.__name__, e)


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
    node_class: str = "",
    reach_provider: Optional[Callable[[], str]] = None,
    harness_provider: Optional[Callable[[], Tuple[str, bool]]] = None,
    token_provider: Optional[Callable[[], str]] = None,
    contribute_storage: Optional[bool] = None,
) -> Dict[str, Any]:
    """Register this device with the rich endpoint spine.

    Args:
        base_url: Control-plane base (Identity service) exposing ``/v1/nodes/*``.
        token: Bearer token from ``adk login`` (caller→tenant on the server).
        node_id: Stable node identifier.
        enable_heartbeat: Start the background heartbeat loop on success.
        inference_url: Explicit inference base URL, or ``None``/``"auto"`` to probe.
        node_class: One of :data:`NODE_CLASSES`.
        token_provider: Passed to the heartbeat (see :func:`heartbeat_loop`).
        contribute_storage: Join = contribute. ``None`` follows the policy in
            :mod:`adk.storage_contribution` (``AITHER_CONTRIBUTE_STORAGE``, else on
            for server/workstation classes and off for laptop/phone); ``True`` /
            ``False`` force it. The outcome is reported under ``"storage"``.

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
        # One device, many facets: Identity answers with the id this MACHINE already
        # has (another facet registered it first), which may not be the one we sent.
        node_id = str(data.get("node_id") or node_id)
        reg["node_id"] = node_id
        try:
            from adk.device_identity import record_facet
            record_facet(str(reg.get("facet") or "daemon"), node_id,
                         capabilities=reg.get("capabilities") or [])
        except Exception as e:  # noqa: BLE001 -- the local record never fails enrollment
            log.debug("device.json not updated: %s", e)
        workspace = data.get("workspace", {}) or {}
        _save_workspace(workspace)

        # Registration already minted and returned the device client cert — persist it.
        # (It used to fire a second request at a route that does not exist; see
        # _persist_device_cert.)
        tenant_id = data.get("tenant_id", "")
        cert_result = _persist_device_cert(data)
        if data.get("command_key"):
            from adk import node_commands

            node_commands.save_key(node_id, str(data["command_key"]))
        cert_enrolled = cert_result.get("success", False)

        if enable_heartbeat:
            # No running loop (sync context) returns False — the caller can
            # start it later.
            start_heartbeat(
                base, token, node_id,
                registered=True,
                inference_url=reg["inference_url"] or None,
                node_class=node_class,
                reach_provider=reach_provider,
                harness_provider=harness_provider,
                token_provider=token_provider,
            )

        log.info("Enrolled endpoint %s (tenant=%s, cert_enrolled=%s, inference=%s %s)",
                 node_id, tenant_id, cert_enrolled, reg["inference_kind"],
                 reg["inference_url"] or "-")
        storage = await _contribute_storage(
            node_id, node_class, data.get("bearer_token") or token, data, contribute_storage
        )
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
            "storage": storage,
        }
    except Exception as e:
        log.warning("Rich enrollment failed: %s", e)
        return {"enrolled": False, "error": str(e)}


async def _contribute_storage(
    node_id: str,
    node_class: str,
    token: str,
    register_response: Dict[str, Any],
    enabled: Optional[bool],
) -> Dict[str, Any]:
    """Join = contribute: lend bounded disk to the mesh storage pool. Never raises.

    Uses the node's own capability token from the register response when there is
    one (it carries the mesh scope), else the enrolling token.
    """
    try:
        from adk.storage_contribution import contribute_after_enroll

        result = await contribute_after_enroll(
            node_id, node_class, token, enroll_response=register_response, enabled=enabled
        )
    except Exception as e:  # noqa: BLE001 -- storage must never fail enrollment
        log.debug("storage contribution unavailable: %s", e)
        return {"registered": False, "error": str(e)}
    if result.get("registered"):
        log.info("Contributing %d bytes of storage as peer %s",
                 result["decision"]["contributed_bytes"], result.get("peer_id", ""))
    else:
        log.info("Not contributing storage: %s",
                 result.get("skipped") or result.get("error") or result.get("http_status"))
    return result
