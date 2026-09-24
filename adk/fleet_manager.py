"""Cross-runtime fleet manager — create & manage agents across three runtimes.

`adk/fleet.py` runs many agents IN ONE PROCESS (a group chat). This is different: it
tracks a durable FLEET whose members each live in a distinct runtime —

  - **local**      : a self-hosted agent process on THIS machine (adk serve / daemon).
  - **managed**    : an Anthropic Managed Agent (hosted twin), deployed through the
                     platform's managed-deploy pipeline (compile → agents.create).
  - **hosted**     : an Aitherium Instance on the platform fleet (POST /v1/instances);
                     `cloud-run` is kept as an alias of it.

The manager owns a durable registry (`~/.aither/fleet.json`) and dispatches lifecycle
(create / status / remove) to a per-runtime DRIVER. Drivers are injected, so the real
ones do process/HTTP/cloud work and tests substitute fakes — the same collaborator-
injection pattern AitherOS's WorkforceProvisioner uses. Nothing here reaches the network
or spawns a process unless a real driver is wired.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

RUNTIMES = ("local", "managed", "hosted", "cloud-run")  # cloud-run = alias of hosted


def _data_dir() -> Path:
    return Path(os.getenv("AITHER_DATA_DIR", os.path.expanduser("~/.aither")))


# ── durable registry ─────────────────────────────────────────────────────

@dataclass
class FleetMember:
    id: str
    name: str
    runtime: str                       # one of RUNTIMES
    status: str = "creating"           # creating|running|pending_runtime|stopped|failed
    ref: str = ""                      # pid (local) | anthropic_agent_id (managed) | service/request id
    endpoint: str = ""                 # local url / hosted url, when known
    source: str = ""                   # pack id or import provenance
    error: str = ""
    created_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FleetMember":
        known = {f: d.get(f) for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        known["meta"] = d.get("meta") or {}
        return cls(**{k: v for k, v in known.items() if v is not None})


class FleetStore:
    """JSON-file registry of fleet members. Atomic writes; safe for a CLI (single writer)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else _data_dir() / "fleet.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        members = data.get("members") if isinstance(data, dict) else None
        return members if isinstance(members, dict) else {}

    def _save(self, members: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"members": members}, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> list[FleetMember]:
        return [FleetMember.from_dict(m) for m in self._load().values()]

    def get(self, member_id: str) -> Optional[FleetMember]:
        m = self._load().get(member_id)
        return FleetMember.from_dict(m) if m else None

    def upsert(self, member: FleetMember) -> None:
        members = self._load()
        members[member.id] = member.to_dict()
        self._save(members)

    def remove(self, member_id: str) -> bool:
        members = self._load()
        if member_id in members:
            del members[member_id]
            self._save(members)
            return True
        return False


# ── runtime driver protocol ──────────────────────────────────────────────

class RuntimeDriver(Protocol):
    runtime: str
    def create(self, member: FleetMember, opts: dict[str, Any]) -> dict[str, Any]: ...
    def status(self, member: FleetMember) -> str: ...
    def remove(self, member: FleetMember) -> bool: ...


# ── local: a self-hosted agent process on this machine ───────────────────

class LocalDriver:
    """Runs/tracks a local agent process. The `spawner` is injectable so tests can
    start a trivial process; the default launches the adk agent server."""

    runtime = "local"

    def __init__(self, spawner: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None) -> None:
        self._spawner = spawner or self._default_spawner

    @staticmethod
    def _default_spawner(name: str, opts: dict[str, Any]) -> dict[str, Any]:
        port = int(opts.get("port") or 8080)
        # Detached background process; on Windows use a new process group so the
        # CLI exiting doesn't kill it.
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        cmd = [sys.executable, "-m", "adk", "run", "--port", str(port)]
        if opts.get("pack"):
            cmd += ["--identity", str(opts["pack"])]
        proc = subprocess.Popen(  # noqa: S603 - args are constructed, not shell
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return {"pid": proc.pid, "endpoint": f"http://127.0.0.1:{port}"}

    def create(self, member: FleetMember, opts: dict[str, Any]) -> dict[str, Any]:
        spawned = self._spawner(member.name, opts)
        return {
            "status": "running",
            "ref": str(spawned.get("pid", "")),
            "endpoint": spawned.get("endpoint", ""),
        }

    def status(self, member: FleetMember) -> str:
        if not member.ref:
            return "failed"
        try:
            pid = int(member.ref)
        except ValueError:
            return "failed"
        return "running" if _pid_alive(pid) else "stopped"

    def remove(self, member: FleetMember) -> bool:
        if not member.ref:
            return True
        try:
            pid = int(member.ref)
        except ValueError:
            return True
        return _kill_pid(pid)


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness. NOTE: on Windows `os.kill(pid, 0)` does NOT probe —
    it calls TerminateProcess — so we use WaitForSingleObject via ctypes instead."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102  # handle NOT signalled → process still running
        k = ctypes.windll.kernel32
        h = k.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False  # no such process (or no rights → treat as not-ours/gone)
        try:
            return k.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(pid: int) -> bool:
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes

        PROCESS_TERMINATE = 0x0001
        k = ctypes.windll.kernel32
        h = k.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            return not _pid_alive(pid)  # can't open → already gone == success
        try:
            k.TerminateProcess(h, 1)
        finally:
            k.CloseHandle(h)
        return not _pid_alive(pid)
    try:
        os.kill(pid, 15)  # SIGTERM
    except OSError:
        return not _pid_alive(pid)  # already gone == success
    # SIGTERM is asynchronous — returning True immediately reports success while
    # the process is still running (or ignoring TERM). Poll for exit, escalate
    # to SIGKILL, and reap along the way: a dead CHILD lingers as a zombie that
    # kill(pid, 0) still "sees" until waited on.
    import time

    def _reap() -> None:
        if hasattr(os, "waitpid") and hasattr(os, "WNOHANG"):
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass  # not our child (or already reaped) — fine

    for _ in range(20):  # ~1s graceful window
        _reap()
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, 9)  # SIGKILL
    except OSError:
        pass
    for _ in range(20):
        _reap()
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


# ── managed: an Anthropic hosted twin via the platform pipeline ──────────

class ManagedDriver:
    """Deploys a hosted twin through the platform's managed-deploy endpoint. The
    `deploy_fn`/`remove_fn` are injected (default: HTTP to the gateway) so the
    request shape is testable without a live gateway."""

    runtime = "managed"

    def __init__(
        self,
        deploy_fn: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None,
        remove_fn: Optional[Callable[[str, dict[str, Any]], bool]] = None,
        apply_fn: Optional[Callable[[str, str], dict[str, Any]]] = None,
    ) -> None:
        self._deploy_fn = deploy_fn or _http_managed_deploy
        self._remove_fn = remove_fn or _http_managed_demigrate
        self._apply_fn = apply_fn or _http_apply_pack

    def create(self, member: FleetMember, opts: dict[str, Any]) -> dict[str, Any]:
        # ``agent_id`` is a BINDING id in the tenant's agent-binding store. Genesis
        # returns an explicit id as given and 404s when no binding has it, so a
        # pack name or fleet name must never be sent as one. Only an explicit
        # --agent-id is forwarded; otherwise Genesis resolves the tenant's primary
        # (or auto-bound default) binding. A --pack is applied onto that binding
        # first (``POST /v1/agent/binding/apply-pack``), then the binding deploys.
        agent_id = str(opts.get("agent_id") or "").strip()
        pack = str(opts.get("pack") or "").strip()
        if pack:
            applied = self._apply_fn(pack, agent_id)
            if not applied.get("ok", False):
                return {"status": "failed",
                        "error": f"apply pack {pack!r}: {applied.get('error') or 'failed'}"}
        res = self._deploy_fn(agent_id, opts)
        if not res.get("ok", res.get("deployed")):
            return {"status": "failed", "error": str(res.get("error") or "deploy failed")}
        bound = ((res.get("binding") or {}).get("agent_id") if isinstance(res.get("binding"), dict)
                 else "") or agent_id
        return {
            "status": "running" if res.get("deployed") else "pending_runtime",
            "ref": res.get("anthropic_agent_id", ""),
            "endpoint": res.get("endpoint", ""),
            "meta": {"digest": res.get("digest", ""), "agent_id": bound or ""},
        }

    def status(self, member: FleetMember) -> str:
        return member.status  # authoritative state lives platform-side; refreshed on deploy

    def remove(self, member: FleetMember) -> bool:
        meta = member.meta or {}
        if member.status == "failed":
            # Nothing was deployed for this record (create or apply-pack failed),
            # so there is nothing platform-side to tear down. Any DELETE here would
            # hit a DIFFERENT, working deployment: a param-less one de-migrates the
            # tenant's primary binding, a named one the binding of that name.
            return True
        if "agent_id" in meta:
            # The binding id the deploy resolved (recorded in meta); empty = primary.
            agent_id = str(meta.get("agent_id") or "")
        else:
            # A member created before meta carried agent_id: target what it was
            # deployed under, exactly as before. Sending NO agent_id would make
            # Genesis de-migrate the tenant's PRIMARY binding instead.
            agent_id = str(member.source or member.name or "")
        return bool(self._remove_fn(agent_id, meta))


def _gateway_base() -> str:
    """Genesis API base: explicit AITHER_API_URL/AITHER_GATEWAY_URL, else the
    portal's ``/api/genesis`` proxy (honours AITHER_PORTAL_URL). Never
    ``http://localhost:8001`` — Genesis publishes no host port and speaks TLS."""
    from adk.control_plane import genesis_api_base

    return genesis_api_base()


def _api_key() -> str:
    return os.getenv("AITHER_API_KEY", "")


def _http_apply_pack(pack: str, agent_id: str) -> dict[str, Any]:
    """Apply a pack onto the tenant's binding (``POST /v1/agent/binding/apply-pack``)."""
    import httpx

    body: dict[str, Any] = {"listing_id": pack}
    if agent_id:
        body["agent_id"] = agent_id
    headers = {}
    if _api_key():
        headers["Authorization"] = f"Bearer {_api_key()}"
    url = f"{_gateway_base()}/v1/agent/binding/apply-pack"
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=60.0)
        if r.status_code >= 400:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:200]}"}
        data = r.json()
        return data if isinstance(data, dict) and "ok" in data else {"ok": True, **(data or {})}
    except Exception as exc:  # noqa: BLE001 - surface as a failed deploy, never crash the CLI
        return {"ok": False, "error": str(exc)}


def _http_managed_deploy(agent: str, opts: dict[str, Any]) -> dict[str, Any]:
    import httpx

    # Genesis ``POST /v1/agent/managed/deploy`` takes ``ManagedDeployBody``.
    # ``agent_id`` is a binding id: send it only when one was given explicitly,
    # else Genesis deploys the tenant's primary/default binding.
    body: dict[str, Any] = {}
    if agent:
        body["agent_id"] = agent
    for k in ("mcp_url", "model", "system_prompt", "environment_id"):
        if opts.get(k):
            body[k] = opts[k]
    headers = {}
    if _api_key():
        headers["Authorization"] = f"Bearer {_api_key()}"
    url = f"{_gateway_base()}/v1/agent/managed/deploy"
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=60.0)
        if r.status_code >= 400:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:200]}"}
        return r.json()
    except Exception as exc:  # noqa: BLE001 - surface as a failed deploy, never crash the CLI
        return {"ok": False, "error": str(exc)}


def _http_managed_demigrate(agent: str, meta: dict[str, Any]) -> bool:
    import httpx

    headers = {}
    if _api_key():
        headers["Authorization"] = f"Bearer {_api_key()}"
    url = f"{_gateway_base()}/v1/agent/managed"
    try:
        params = {"agent_id": agent} if agent else None
        r = httpx.request("DELETE", url, params=params, headers=headers, timeout=30.0)
        return r.status_code < 400
    except Exception:  # noqa: BLE001
        return False


# ── hosted: an Aitherium Instance on the platform's own fleet ─────────────

class HostedDriver:
    """Creates an Aitherium Instance through Genesis ``POST /v1/instances``.

    HISTORY, kept so it is not re-derived: this driver was ``CloudRunDriver``
    (runtime ``cloud-run``) and posted to ``/v1/workforce/cloud-run/provision``
    — a route that never existed in Genesis (measured 2026-09-06: zero hits). It
    then "gracefully degraded" to a local ``pending_runtime`` that nothing ever
    fulfilled, so every hosted agent anyone created sat pending forever with no
    error. The gateway now has the route, and this driver reports what the
    gateway actually said: a 4xx/5xx is a FAILED member with the body in
    ``error``, never a pending one.
    """

    runtime = "hosted"

    def __init__(
        self,
        create_fn: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None,
        status_fn: Optional[Callable[[str], str]] = None,
        remove_fn: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._create_fn = create_fn or _http_instance_create
        self._status_fn = status_fn or _http_instance_status
        self._remove_fn = remove_fn or _http_instance_delete

    def create(self, member: FleetMember, opts: dict[str, Any]) -> dict[str, Any]:
        res = self._create_fn(member.name, opts)
        if not res.get("ok"):
            out = {"status": "failed", "error": str(res.get("error") or "instance create failed")}
            # A failed create can still leave a platform-side record (it counts
            # toward the tenant's instance quota). Keep its id as the ref so
            # `adk fleet rm` tears it down instead of orphaning it.
            if res.get("instance_id"):
                out["ref"] = str(res["instance_id"])
            return out
        inst = res.get("instance") or {}
        return {
            "status": ("running" if inst.get("status") == "ready"
                       else str(inst.get("status") or "failed")),
            "ref": str(inst.get("id", "")),
            "endpoint": str(inst.get("endpoint_url", "")),
            "meta": {"hostname": inst.get("hostname", ""), "ready_ms": inst.get("ready_ms", -1),
                     "placement": {k: inst.get(k) for k in ("brain", "loop", "hands")}},
        }

    def status(self, member: FleetMember) -> str:
        if not member.ref:
            return "failed"
        return self._status_fn(member.ref)

    def remove(self, member: FleetMember) -> bool:
        if not member.ref:
            return True
        return bool(self._remove_fn(member.ref))


# Backwards-compatible alias: `--runtime cloud-run` keeps working and lands on
# the platform's own instances rather than a dead GCP intent.
CloudRunDriver = HostedDriver


def _instance_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if _api_key():
        headers["Authorization"] = f"Bearer {_api_key()}"
    return headers


def _failed_instance_id(resp: Any) -> str:
    """The instance id a FAILED ``POST /v1/instances`` left behind, or ``""``.

    Genesis saves the record as ``failed`` before raising (e.g. 504 not_ready)
    and returns ``{"detail": {"instance": {"id": ...}}}``; that row still counts
    toward the instance quota, so the caller must keep the id to delete it.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - a non-JSON error body carries no id
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    inst = detail.get("instance") if isinstance(detail, dict) else None
    if isinstance(inst, dict) and inst.get("id"):
        return str(inst["id"])
    return ""


def _http_instance_create(name: str, opts: dict[str, Any]) -> dict[str, Any]:
    import httpx

    body: dict[str, Any] = {"name": name, "preset": opts.get("preset") or "hosted"}
    for k in ("brain", "loop", "hands", "image"):
        if opts.get(k):
            body[k] = opts[k]
    env: dict[str, str] = {}
    if opts.get("pack"):
        env["AITHER_IDENTITY"] = str(opts["pack"])
    if opts.get("model"):
        env["AITHER_MODEL"] = str(opts["model"])
    if env:
        body["env"] = env
    url = f"{_gateway_base()}/v1/instances"
    try:
        r = httpx.post(url, json=body, headers=_instance_headers(), timeout=180.0)
        if r.status_code >= 400:
            out: dict[str, Any] = {"ok": False, "error": f"{r.status_code}: {r.text[:300]}"}
            iid = _failed_instance_id(r)
            if iid:
                out["instance_id"] = iid
            return out
        return r.json()
    except Exception as exc:  # noqa: BLE001 - surface as a failed create, never crash the CLI
        return {"ok": False, "error": str(exc)}


def _http_instance_status(instance_id: str) -> str:
    import httpx

    url = f"{_gateway_base()}/v1/instances/{instance_id}"
    try:
        r = httpx.get(url, headers=_instance_headers(), timeout=30.0)
        if r.status_code >= 400:
            return "failed" if r.status_code == 404 else "unknown"
        status = str((r.json().get("instance") or {}).get("status") or "unknown")
        return "running" if status == "ready" else status
    except Exception:  # noqa: BLE001
        return "unknown"


def _http_instance_delete(instance_id: str) -> bool:
    import httpx

    url = f"{_gateway_base()}/v1/instances/{instance_id}"
    try:
        r = httpx.request("DELETE", url, headers=_instance_headers(), timeout=120.0)
        return r.status_code < 400
    except Exception:  # noqa: BLE001
        return False


# ── the manager ──────────────────────────────────────────────────────────

class FleetManager:
    def __init__(
        self,
        store: Optional[FleetStore] = None,
        drivers: Optional[dict[str, RuntimeDriver]] = None,
        *,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self.store = store or FleetStore()
        self._now = now or time.time
        self.drivers: dict[str, RuntimeDriver] = drivers or {
            "local": LocalDriver(),
            "managed": ManagedDriver(),
            "hosted": HostedDriver(),
            "cloud-run": HostedDriver(),
        }

    def create(self, runtime: str, name: str, **opts: Any) -> FleetMember:
        if runtime not in self.drivers:
            raise ValueError(f"unknown runtime {runtime!r} (expected one of {sorted(self.drivers)})")
        if not name:
            raise ValueError("agent name is required")
        member = FleetMember(
            id=uuid.uuid4().hex[:12],
            name=name,
            runtime=runtime,
            source=str(opts.get("pack") or ""),
            created_at=self._now(),
        )
        try:
            updates = self.drivers[runtime].create(member, opts)
        except Exception as exc:  # noqa: BLE001 - a driver failure marks the member failed, never crashes
            updates = {"status": "failed", "error": str(exc)}
        for k, v in updates.items():
            if k == "meta" and isinstance(v, dict):
                member.meta.update(v)
            elif hasattr(member, k):
                setattr(member, k, v)
        self.store.upsert(member)
        return member

    def list_members(self) -> list[FleetMember]:
        return sorted(self.store.all(), key=lambda m: m.created_at)

    def get(self, member_id: str) -> Optional[FleetMember]:
        return self.store.get(member_id)

    def refresh(self, member_id: str) -> Optional[FleetMember]:
        member = self.store.get(member_id)
        if member is None:
            return None
        driver = self.drivers.get(member.runtime)
        if driver is not None:
            member.status = driver.status(member)
            self.store.upsert(member)
        return member

    def remove(self, member_id: str) -> bool:
        member = self.store.get(member_id)
        if member is None:
            return False
        driver = self.drivers.get(member.runtime)
        if driver is not None:
            try:
                driver.remove(member)
            except Exception:  # noqa: BLE001 - always drop the record even if teardown errors
                pass
        return self.store.remove(member_id)


# ── bidirectional: register local agent's MCP endpoint with hosted twin ─────

def connect_local_agent(
    agent_name: str,
    mcp_url: str,
    *,
    token: Optional[str] = None,
    poster: Optional[Callable[[str, str, str], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Register THIS machine's local agent MCP endpoint with the gateway so a
    hosted twin can call back to it (bidirectional). The MCP URL is stored in the
    tenant's endpoint registry and wired into managed twins at deploy/resync.

    The adk MCP server ALWAYS enforces a bearer (``adk.mcp_server`` auto-generates
    one when ``AITHER_MCP_KEY`` is unset), so an endpoint registered without its
    bearer lists 0 tools (401). The bearer is ``token`` if given, else the local
    ``AITHER_MCP_KEY`` / ``AITHER_SERVER_API_KEY``; Genesis stores it in the vault.

    Args:
        agent_name: Name/identifier for this local agent
        mcp_url: Public URL where the local agent's MCP is reachable
        token: Bearer the MCP server expects (default: AITHER_MCP_KEY env)
        poster: Injected HTTP poster ``(name, url, token)`` (default: httpx POST). Testable.

    Returns:
        {"ok": True, "endpoint": {...}} on success; {"ok": False, "error": "..."} on failure.
        Never raises — all errors are returned as a failed dict.
    """
    if not poster:
        poster = _http_register_mcp_endpoint
    bearer = (token if token is not None else _local_mcp_key()).strip()
    try:
        return poster(agent_name, mcp_url, bearer)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _local_mcp_key() -> str:
    """The bearer this box's adk MCP server enforces, when configured by env."""
    return os.getenv("AITHER_MCP_KEY", "") or os.getenv("AITHER_SERVER_API_KEY", "")


def _http_register_mcp_endpoint(name: str, mcp_url: str, token: str = "") -> dict[str, Any]:
    """POST to the gateway's MCP endpoint registration endpoint. Mirrors the
    ManagedDriver HTTP pattern: resilient, returns a dict, never crashes."""
    import httpx

    if not mcp_url:
        return {"ok": False, "error": "mcp_url is required"}

    body: dict[str, Any] = {
        "name": name.strip() or "local-agent",
        "url": mcp_url.strip(),
        "local": True,  # allow localhost/internal URLs (self-hosted agent)
    }
    if token:
        body["token"] = token  # Genesis stores it in the vault; never echoed back

    headers = {}
    if _api_key():
        headers["Authorization"] = f"Bearer {_api_key()}"

    url = f"{_gateway_base()}/v1/agent/mcp-endpoints"
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=30.0)
        if r.status_code >= 400:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:200]}"}
        result = r.json()
        out = {"ok": result.get("ok", True), "endpoint": result.get("endpoint", {})}
        if "secret_stored" in result:
            out["secret_stored"] = bool(result.get("secret_stored"))
        if result.get("hint"):
            out["hint"] = result["hint"]
        if not token:
            out.setdefault("warning", "no bearer sent: the adk MCP server requires one "
                                      "(pass --token or set AITHER_MCP_KEY)")
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


__all__ = [
    "RUNTIMES",
    "FleetMember",
    "FleetStore",
    "FleetManager",
    "LocalDriver",
    "ManagedDriver",
    "CloudRunDriver",
    "HostedDriver",
    "connect_local_agent",
]
