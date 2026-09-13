"""Client-side addon lifecycle manager -- and the ONE component loader.

Manages self-hosted service addons (Qdrant, Knowledge-RAG, CodeGraph, etc.)
alongside customer AitherOS apps.  Four addon types:

- ``docker``     -- managed container via docker compose
- ``process``    -- local binary/script; a ``command`` is spawned (detached, no
                    console window), otherwise the process is assumed running
- ``external``   -- already-running service at a URL (customer BYO)
- ``capability`` -- a library/CLI with NO process and NO port; its UI/API is served
                    by the host named in ``hosted_by`` (``awdk`` = this daemon)

A manifest is ALSO the runtime declaration an aw* brick ships (``brick:`` +
``surfaces:``), read by every host -- awnode's extension manager, the aitheros
launcher (connect.json), awsh packs, awdesk's tray and the Living Desktop's
"On this device" lane. Before 2026-09-06 five hand-kept lists said what was
installed on a machine and none derived from the registry; a brick missing from
one of them failed as a SILENCE. Asserted by check_aw_component_manifests.py.

Manifest sources, first hit by id wins and the source is recorded as ``_source``
(shadowing is logged, never silent):

1. Python entry points in group ``aither.components`` -- a pip-installed brick
   registers ``awX = "awX:component_manifest"`` returning the path of the YAML
   (or a directory of YAMLs) it ships in its wheel.
2. ``~/.aither/components/*.yaml`` -- non-Python surfaces (awdesk, awconnect, the
   awsh npm package) and anything a human drops in.
3. The bundled/monorepo ``addon_manifests`` directory (``_MANIFEST_DIRS``).

State is persisted to ``~/.aitheros/addons/state.json``.

Usage::

    mgr = AddonManager()
    await mgr.enable("qdrant")
    await mgr.enable("knowledge-rag")
    inv = mgr.get_inventory()          # for federation heartbeat
    await mgr.health_check_all()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml

log = logging.getLogger("adk.addon_manager")

# ---------------------------------------------------------------------------
# Addon manifest loading
# ---------------------------------------------------------------------------

#: Entry-point group a pip-installed brick registers its component manifest in.
ENTRY_POINT_GROUP = "aither.components"
#: Non-Python surfaces drop their manifest here (the launcher writes awdesk's).
COMPONENTS_DIR = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "components"

_MANIFEST_DIRS: List[str] = [
    # Inside AitherOS monorepo (Docker or dev)
    "/app/AitherOS/config/addon_manifests",
    # Relative to repo root on local dev
    os.path.join(os.path.dirname(__file__), "..", "..", "AitherOS", "config", "addon_manifests"),
    # Bundled with ADK package
    os.path.join(os.path.dirname(__file__), "addon_manifests"),
]


def _find_manifest_dir() -> Optional[Path]:
    for d in _MANIFEST_DIRS:
        p = Path(d)
        if p.is_dir() and (p / "_schema.yaml").exists():
            return p
    return None


def _read_manifest(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001 -- one bad file must not hide the others
        log.warning("Failed to load manifest %s: %s", path, e)
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None
    return data


def _entry_point_manifest_paths() -> List[tuple[str, Path]]:
    """(source-label, path) for every manifest a pip-installed brick registers."""
    out: List[tuple[str, Path]] = []
    try:
        from importlib.metadata import entry_points
        eps = entry_points()
        if hasattr(eps, "select"):
            group = eps.select(group=ENTRY_POINT_GROUP)
        else:  # Python 3.10/3.11 metadata API
            group = eps.get(ENTRY_POINT_GROUP, [])
    except Exception as e:  # noqa: BLE001 -- a broken metadata index is "no entry points"
        log.debug("entry points unreadable: %s", e)
        return out
    for ep in group:
        try:
            target = ep.load()
            loc = Path(target() if callable(target) else target)
        except Exception as e:  # noqa: BLE001
            log.warning("component entry point %s failed to load: %s", ep.name, e)
            continue
        label = f"entry-point:{ep.name}"
        if loc.is_dir():
            out.extend((label, q) for q in sorted(loc.glob("*.yaml")) if not q.name.startswith("_"))
        elif loc.is_file():
            out.append((label, loc))
    return out


def manifest_sources() -> List[tuple[str, Path]]:
    """Every manifest file, in precedence order, with the source it came from."""
    srcs = _entry_point_manifest_paths()
    if COMPONENTS_DIR.is_dir():
        srcs.extend(("home", q) for q in sorted(COMPONENTS_DIR.glob("*.yaml"))
                    if not q.name.startswith("_"))
    mdir = _find_manifest_dir()
    if mdir:
        srcs.extend(("bundled", q) for q in sorted(mdir.glob("*.yaml"))
                    if not q.name.startswith("_"))
    return srcs


def load_all_manifests() -> List[Dict[str, Any]]:
    """Every manifest from every source; first hit by id wins, shadowing is LOGGED.

    A shadowed manifest that vanished silently is how two copies of one component
    drift apart while both read as installed. Each manifest carries ``_source``.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for label, path in manifest_sources():
        data = _read_manifest(path)
        if not data:
            continue
        aid = str(data["id"])
        if aid in by_id:
            log.info("manifest %s from %s shadowed by %s", aid, label, by_id[aid].get("_source"))
            continue
        data["_source"] = label
        data["_path"] = str(path)
        by_id[aid] = data
    return list(by_id.values())


def load_addon_manifest(addon_id: str) -> Optional[Dict[str, Any]]:
    """Load a single addon manifest by ID, honouring the same source precedence."""
    for m in load_all_manifests():
        if m.get("id") == addon_id:
            return m
    return None


# ---------------------------------------------------------------------------
# AddonInstance dataclass
# ---------------------------------------------------------------------------

@dataclass
class AddonInstance:
    addon_id: str
    pack_id: str = ""
    status: str = "stopped"         # running | stopped | error | starting
    container_id: str = ""
    endpoint: str = ""
    health_ok: bool = False
    started_at: float = 0.0
    error_message: str = ""
    addon_type: str = "docker"      # docker | process | external | capability
    pid: int = 0                    # process type with a `command`: the child we spawned
    brick: str = ""                 # ecosystem.yaml id, when the manifest declares one
    source: str = ""                # manifest source label (entry-point:<x> | home | bundled)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AddonInstance":
        known = {
            "addon_id", "pack_id", "status", "container_id", "endpoint",
            "health_ok", "started_at", "error_message", "addon_type",
            "pid", "brick", "source",
        }
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return Path.home() / ".aitheros" / "addons" / "state.json"


def _load_state() -> Dict[str, Dict[str, Any]]:
    sp = _state_path()
    if sp.is_file():
        try:
            return json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: Dict[str, Dict[str, Any]]) -> None:
    sp = _state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# AddonManager
# ---------------------------------------------------------------------------

class AddonManager:
    """Client-side addon lifecycle engine."""

    def __init__(self) -> None:
        self._state = _load_state()

    def _persist(self) -> None:
        _save_state(self._state)

    # -- enable / disable ----------------------------------------------------

    async def enable(
        self,
        addon_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> AddonInstance:
        """Enable an addon: pull image, start container, health-check."""
        manifest = load_addon_manifest(addon_id)
        if not manifest:
            raise ValueError(f"Unknown addon: {addon_id}. Run 'adk addon list' to see available addons.")

        addon_type = manifest.get("type", "docker")
        instance = AddonInstance(
            addon_id=addon_id,
            pack_id=manifest.get("pack_id", ""),
            addon_type=addon_type,
            status="starting",
            started_at=time.time(),
            brick=str(manifest.get("brick") or ""),
            source=str(manifest.get("_source") or ""),
        )

        # Check dependencies first
        deps = manifest.get("dependencies", [])
        for dep_id in deps:
            dep_state = self._state.get(dep_id, {})
            if dep_state.get("status") != "running":
                log.info("Dependency %s not running, enabling first...", dep_id)
                await self.enable(dep_id)

        if addon_type == "docker":
            instance = await self._start_docker(manifest, instance, config)
        elif addon_type in ("quadlet", "podman"):
            instance = await self._start_quadlet(manifest, instance, config)
        elif addon_type == "external":
            # External: just record the endpoint
            endpoint = (config or {}).get("endpoint", "")
            if not endpoint:
                port = manifest.get("default_port", 8000)
                endpoint = f"http://localhost:{port}"
            instance.endpoint = endpoint
            instance.status = "running"
        elif addon_type == "process":
            instance = await self._start_process(manifest, instance, config)
        elif addon_type == "capability":
            # No process of its own: its UI/API lives on the host it names.
            instance.endpoint = self._host_endpoint(manifest.get("hosted_by", "awdk"))
            instance.status = "running" if instance.endpoint else "error"
            if not instance.endpoint:
                instance.error_message = f"unknown hosted_by {manifest.get('hosted_by')!r}"

        # Health check
        try:
            instance.health_ok = await self._check_health(manifest, instance)
            if instance.health_ok:
                instance.status = "running"
        except Exception as e:
            log.warning("Health check failed for %s: %s", addon_id, e)
            instance.health_ok = False

        self._state[addon_id] = instance.to_dict()
        self._persist()
        log.info("Addon %s enabled: status=%s endpoint=%s", addon_id, instance.status, instance.endpoint)
        return instance

    @staticmethod
    def _host_endpoint(hosted_by: str) -> str:
        """Loopback URL of the host that serves a capability. 127.0.0.1, never localhost."""
        if hosted_by == "awdk":
            port = os.getenv("AITHER_PORT") or os.getenv("AITHER_DAEMON_PORT") or "9001"
            return f"http://127.0.0.1:{port}"
        env_key = f"AITHER_{hosted_by.upper()}_URL"
        return os.getenv(env_key, "")

    async def _start_process(
        self,
        manifest: Dict[str, Any],
        instance: AddonInstance,
        config: Optional[Dict[str, Any]] = None,
    ) -> AddonInstance:
        """Spawn a `process` addon's ``command`` detached, or adopt one already running.

        No console window (Windows allocates one for a console child of a detached
        parent, and it TAKES FOCUS -- the same class as gate 1t / DC007), stdout and
        stderr to ~/.aither/logs/addon-<id>.log so a crash has somewhere to be read.
        ``{port}`` in the command is the port this manifest declares (or the
        ``port`` override in ``config``), so what the manifest says is what binds.
        """
        import shlex
        import shutil
        import subprocess
        import sys

        port = int((config or {}).get("port") or manifest.get("default_port") or 0)
        instance.endpoint = f"http://127.0.0.1:{port}" if port else ""
        command = str(manifest.get("command") or "").strip()
        if not command:
            instance.status = "running"          # assumed already running (legacy shape)
            return instance
        argv = shlex.split(command.format(port=port), posix=(sys.platform != "win32"))

        # RESOLVE THE BINARY BEFORE SPAWNING, and say so in the manifest's own terms.
        #
        # Popen already raises OSError for a missing program and the handler below
        # records it, so this is not about crashing -- it is about WHICH sentence the
        # operator reads. "could not start 'electron': [WinError 2] The system cannot
        # find the file specified" describes a syscall. It does not say that
        # addon_manifests/awdesk.yaml asks for a runtime this machine has never had,
        # which is the actual repair.
        #
        # Measured 2026-09-06: awdesk declares `command: electron .`, `electron` is not
        # on PATH, and no Electron entry point exists anywhere in AitherOS/apps -- the
        # desktop app that ships is PyQt6 (AitherOS/apps/AitherDesktop, "Native PyQt6
        # overlay"). So the manifest names a runtime nothing here provides, and the
        # only way to find that out was to try to start it and read a WinError.
        #
        # Same failure class as the awnix unit whose ExecStart pointed at
        # /usr/local/bin/awdk-daemon, a binary no console script installs: declared,
        # enabled, and unrunnable, with nothing checking the gap between the
        # declaration and the machine. Resolve first, and name the manifest.
        exe = shutil.which(argv[0]) if argv else None
        if exe is None:
            instance.status = "error"
            instance.error_message = (
                f"addon {manifest.get('id')!r}: command {argv[0]!r} is not on PATH "
                f"(manifest command: {command!r}). Install it, or correct "
                f"`command:` in this addon's manifest -- nothing was spawned."
            )
            return instance

        log_dir = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither"))) / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_dir / f"addon-{manifest['id']}.log", "ab")  # noqa: SIM115
            kwargs: Dict[str, Any] = {"stdout": log_fh, "stderr": subprocess.STDOUT,
                                      "stdin": subprocess.DEVNULL}
            if sys.platform == "win32":
                kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                           | subprocess.DETACHED_PROCESS)
            else:
                kwargs["start_new_session"] = True
            defaults = manifest.get("env_defaults") or {}
            env = {**os.environ, **{k: str(v) for k, v in defaults.items()}}
            env["AITHER_HOST"], env["AITHER_PORT"] = "127.0.0.1", str(port)
            proc = subprocess.Popen(argv, env=env, **kwargs)
            instance.pid = proc.pid
            instance.status = "starting"
        except (OSError, ValueError) as e:
            instance.status = "error"
            instance.error_message = f"could not start {argv[0] if argv else command!r}: {e}"
            return instance
        # Wait briefly for it to ANSWER: a pid is not a service.
        for _ in range(20):
            await asyncio.sleep(1)
            if await self._check_health(manifest, instance):
                instance.status = "running"
                instance.health_ok = True
                break
        return instance

    def components_inventory(self) -> List[Dict[str, Any]]:
        """Every known component -- manifest + live state -- for hosts that RENDER them.

        This is what the daemon's ``GET /components`` answers and what the launcher
        folds into connect.json: id, brick, type, surfaces, source, endpoint, status,
        health. Manifests with no state are listed as ``status: available`` so a host
        can OFFER them; a component nobody is offered is indistinguishable from one
        nobody wanted.
        """
        out: List[Dict[str, Any]] = []
        for m in load_all_manifests():
            state = self._state.get(m["id"], {}) or {}
            out.append({
                "id": m["id"],
                "brick": m.get("brick") or "",
                "name": m.get("name") or m["id"],
                "type": m.get("type", "docker"),
                "hosted_by": m.get("hosted_by") or "",
                "default_port": m.get("default_port", 0),
                "surfaces": m.get("surfaces") or {},
                "source": m.get("_source", ""),
                "status": state.get("status", "available"),
                "endpoint": state.get("endpoint", ""),
                "health_ok": bool(state.get("health_ok", False)),
            })
        return out

    async def disable(self, addon_id: str) -> None:
        """Stop and remove an addon."""
        state = self._state.get(addon_id)
        if not state:
            log.info("Addon %s not found in state", addon_id)
            return

        addon_type = state.get("addon_type")
        if addon_type == "docker" and state.get("container_id"):
            try:
                from adk.addon_docker import stop_container
                stop_container(state["container_id"])
            except Exception as e:
                log.warning("Failed to stop container for %s: %s", addon_id, e)
        elif addon_type in ("quadlet", "podman"):
            try:
                from adk.addon_quadlet import stop_quadlet
                stop_quadlet(addon_id)
            except Exception as e:
                log.warning("Failed to stop quadlet unit for %s: %s", addon_id, e)

        del self._state[addon_id]
        pid = int((state or {}).get("pid") or 0) if isinstance(state, dict) else 0
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError) as e:
                log.debug("addon %s pid %s already gone: %s", addon_id, pid, e)
        self._persist()
        log.info("Addon %s disabled", addon_id)

    # -- status / inventory --------------------------------------------------

    async def status(self, addon_id: Optional[str] = None) -> List[AddonInstance]:
        """Get status of one or all addons."""
        if addon_id:
            s = self._state.get(addon_id)
            if not s:
                return []
            return [AddonInstance.from_dict(s)]

        return [AddonInstance.from_dict(s) for s in self._state.values()]

    async def health_check_all(self) -> Dict[str, bool]:
        """Run health checks on all enabled addons."""
        results: Dict[str, bool] = {}
        for addon_id, state in self._state.items():
            if state.get("status") not in ("running", "starting"):
                results[addon_id] = False
                continue
            manifest = load_addon_manifest(addon_id)
            if not manifest:
                results[addon_id] = False
                continue
            inst = AddonInstance.from_dict(state)
            try:
                ok = await self._check_health(manifest, inst)
                results[addon_id] = ok
                state["health_ok"] = ok
                if ok and state.get("status") == "starting":
                    state["status"] = "running"
            except Exception:
                results[addon_id] = False
                state["health_ok"] = False
        self._persist()
        return results

    def addon_env(self) -> Dict[str, str]:
        """Environment an agent should adopt because addons run LOCALLY here.

        The last mile of self-hosting. A pack resolves its endpoint from an env
        var first (AitherBrowser's session.py: AITHER_BROWSER_URL ->
        get_service_url -> the fleet default), and this manager already knows the
        local endpoint the moment an addon is enabled. Nothing carried one to the
        other, so a node could run its own browser and its agent would still
        drive the platform's — the exact thing self-hosting exists to stop, and
        invisible because driving the fleet's browser WORKS.

        Only RUNNING addons contribute: a declared-but-stopped addon pointing an
        agent at a dead port would turn "self-hosted" into "broken", which is
        worse than falling back to the platform.
        """
        env: Dict[str, str] = {}
        for addon_id, state in self._state.items():
            if (state or {}).get("status") != "running":
                continue
            manifest = load_addon_manifest(addon_id) or {}
            provides = manifest.get("provides_env")
            if not isinstance(provides, dict):
                continue
            port = manifest.get("default_port", "")
            endpoint = (state or {}).get("endpoint", "")
            for key, template in provides.items():
                if not isinstance(key, str) or not isinstance(template, str):
                    continue
                env[key] = template.format(port=port, endpoint=endpoint)
        return env

    def get_inventory(self) -> List[Dict[str, Any]]:
        """Return addon inventory for the federation heartbeat.

        Carries ``brick``/``type``/``surfaces`` too, so the hub can list a node's
        components on the desktop. (The hub dropped this whole payload until
        2026-09-06 -- HeartbeatRequest had no ``addons`` field.)
        """
        manifests = {m["id"]: m for m in load_all_manifests()}
        inventory = []
        for addon_id, state in self._state.items():
            m = manifests.get(addon_id, {})
            inventory.append({
                "addon_id": addon_id,
                "status": state.get("status", "unknown"),
                "endpoint": state.get("endpoint", ""),
                "health_ok": state.get("health_ok", False),
                "pack_id": state.get("pack_id", ""),
                "brick": m.get("brick") or state.get("brick", ""),
                "type": m.get("type") or state.get("addon_type", ""),
                "surfaces": m.get("surfaces") or {},
            })
        return inventory

    # -- secrets sync --------------------------------------------------------

    async def sync_secrets(self, addon_id: str, federation_client=None) -> Dict[str, str]:
        """Pull secrets from hub for an addon."""
        if not federation_client:
            return {}
        manifest = load_addon_manifest(addon_id)
        if not manifest:
            return {}
        required = manifest.get("federation", {}).get("secrets_required", [])
        if not required:
            return {}
        result = await federation_client.pull_addon_secrets(addon_id)
        return result.get("secrets", {})

    # -- Docker helpers ------------------------------------------------------

    async def _start_docker(
        self,
        manifest: Dict[str, Any],
        instance: AddonInstance,
        config: Optional[Dict[str, Any]] = None,
    ) -> AddonInstance:
        """Start a Docker container via docker compose.

        Generates (or reuses) a compose file with proper networking,
        volumes, and health checks, then runs ``docker compose up -d``
        for the target service.
        """
        import shutil
        import subprocess

        image = manifest.get("image", "")
        if not image:
            instance.status = "error"
            instance.error_message = f"No image defined for addon {manifest['id']}"
            return instance

        if not shutil.which("docker"):
            instance.status = "error"
            instance.error_message = "docker not found on PATH"
            return instance

        port = manifest.get("default_port", 8000)
        addon_id = manifest["id"]
        svc_name = f"aitheros-addon-{addon_id}"
        compose_file = Path.home() / ".aither" / "docker-compose.addons.yml"

        try:
            from adk.addon_compose import generate_addon_compose

            # Include this addon + its deps in the compose file
            deps = manifest.get("dependencies", [])
            all_ids = deps + [addon_id]
            overrides = {}
            if config and config.get("env"):
                overrides[addon_id] = {"env": config["env"]}
            generate_addon_compose(all_ids, compose_file, overrides=overrides)

            # Ensure the shared network exists
            subprocess.run(
                ["docker", "network", "create", "aitheros_default"],
                capture_output=True,
            )

            # Pull + start just the target service (deps start via depends_on)
            subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "pull", svc_name],
                capture_output=True, timeout=300,
            )
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "up", "-d", svc_name],
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr or f"compose up failed (rc={result.returncode})")

            # Grab the container ID
            id_result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file),
                 "ps", "-q", svc_name],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
            )
            instance.container_id = id_result.stdout.strip()[:12] if id_result.stdout else ""
            instance.endpoint = f"http://localhost:{port}"
            instance.status = "starting"

        except Exception as e:
            instance.status = "error"
            instance.error_message = str(e)
            log.error("Failed to start %s via compose: %s", addon_id, e)

        return instance

    # -- Quadlet/Podman helpers ---------------------------------------------

    async def _start_quadlet(
        self,
        manifest: Dict[str, Any],
        instance: AddonInstance,
        config: Optional[Dict[str, Any]] = None,
    ) -> AddonInstance:
        """Start an addon as a rootless Podman Quadlet systemd unit.

        Writes ~/.config/containers/systemd/<unit>.container, daemon-reloads,
        and ``systemctl --user start`` it. The addon becomes a first-class
        service the agent manages with systemctl.
        """
        addon_id = manifest["id"]
        if not manifest.get("image"):
            instance.status = "error"
            instance.error_message = f"No image defined for addon {addon_id}"
            return instance
        # Allow per-enable env overrides to flow into the unit.
        if config and config.get("env"):
            merged = dict(manifest.get("env_defaults", {}) or {})
            merged.update(config["env"])
            manifest = {**manifest, "env_defaults": merged}
        try:
            from adk.addon_quadlet import start_quadlet
            result = start_quadlet(manifest)
            instance.container_id = result["service"]
            instance.endpoint = result["endpoint"]
            instance.status = "starting"
        except Exception as e:  # noqa: BLE001
            instance.status = "error"
            instance.error_message = str(e)
            log.error("Failed to start %s via quadlet: %s", addon_id, e)
        return instance

    async def _check_health(
        self, manifest: Dict[str, Any], instance: AddonInstance
    ) -> bool:
        """Check health endpoint of an addon."""
        hc = manifest.get("health_check", {})
        path = hc.get("path", "/health")
        endpoint = instance.endpoint
        if not endpoint:
            return False
        url = f"{endpoint}{path}"
        try:
            async with httpx.AsyncClient(timeout=5, verify=os.getenv("AITHER_TLS_VERIFY", "true").lower() != "false") as client:
                resp = await client.get(url)
                return resp.status_code < 400
        except Exception:
            return False
