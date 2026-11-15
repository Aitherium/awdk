"""`adk agent twin deploy --self-host` -- run this project's agent here and register it.

One command, nothing rented: the project's agent is started as a detached local
service (``adk run`` under the kit's own daemon helpers, ``adk.agent_daemon``), and
then registered as the caller's self-hosted twin.

Two separate facts, kept separate:

* **running** -- the agent answers on this machine. That needs a project, a free
  port and a model. It never needs a public URL or a platform account.
* **registered** -- the platform lists the twin and can drive it. The platform dials
  a registered twin, so it only records an address it can pin: this machine's own
  mesh address (``reach=mesh``, private, no public URL) or a public https URL
  (``reach=tunnel``). A loopback address is refused by the platform on purpose (it
  would let a registration point the control plane at its own internal services).

So ``--reach`` decides what the command promises:

  ``mesh``    serve on the mesh address, mint a callback bearer, register.
  ``tunnel``  register a public URL you opened yourself (``--invoke-url``).
  ``local``   serve on loopback only; listed by ``adk agent twin list`` from this
              machine's own record, not registered. No bearer: nothing off the box
              can reach it.
  (unset)     ``mesh`` when this machine has a mesh address, else not ready, with
              the two commands that fix it.

The callback bearer exists only for ``mesh``/``tunnel``: there the server listens
beyond loopback, and without ``AITHER_SERVER_API_KEY`` every route would answer any
peer that can reach the port. It is minted here, written owner-only next to the
twin's record, handed to the child in its environment and to the platform in the
registration, and never printed.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

REACHES = ("mesh", "tunnel", "local")
_MESH_NET = ipaddress.ip_network("100.64.0.0/10")
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")
PORT_SCAN = 20
HEALTH_TIMEOUT_S = 90.0


# ── this machine's record of its twins ───────────────────────────────────────
def home() -> Path:
    return Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))


def twins_dir() -> Path:
    return home() / "twins"


def state_path(name: str) -> Path:
    return twins_dir() / f"{name}.json"


def token_path(name: str) -> Path:
    return twins_dir() / f"{name}.token"


def read_state(name: str) -> Optional[dict]:
    if not _NAME_RE.match(name or ""):
        return None
    try:
        data = json.loads(state_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(state: dict) -> Path:
    path = state_path(str(state["name"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def clear_state(name: str) -> None:
    for path in (state_path(name), token_path(name)):
        try:
            path.unlink()
        except OSError:
            pass


def all_states() -> list[dict]:
    out = []
    try:
        files = sorted(twins_dir().glob("*.json"))
    except OSError:
        files = []
    for f in files:
        st = read_state(f.stem)
        if st:
            out.append(st)
    return out


def local_view(state: dict, *, alive: Optional[Callable[[Any], bool]] = None) -> dict:
    """One twin record as ``adk agent twin list`` shows it: no secret, liveness measured."""
    if alive is None:
        from adk.agent_daemon import pid_alive as alive  # noqa: N813
    return {
        "name": state.get("name"), "reach": state.get("reach"),
        "running": bool(alive(state.get("pid"))), "pid": state.get("pid"),
        "port": state.get("port"), "chat_url": f"http://127.0.0.1:{state.get('port')}",
        "registered": bool(state.get("registered")),
        "invoke_url": state.get("invoke_url") or "",
        "project": state.get("project"), "log": state.get("log"),
    }


# ── reach ────────────────────────────────────────────────────────────────────
def is_mesh_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address((value or "").strip()) in _MESH_NET
    except ValueError:
        return False


def overlay_ip(env: Optional[dict] = None) -> str:
    """This machine's mesh address (100.64.0.0/10), or "" when it is not on a mesh.

    ``AITHER_MESH_OVERLAY_IP`` wins (what ``adk up --reach mesh`` reads); otherwise the
    address the machine itself reports (``adk.kvholder_workspace.overlay_ip``).
    """
    env = os.environ if env is None else env
    explicit = (env.get("AITHER_MESH_OVERLAY_IP") or "").strip()
    if explicit:
        return explicit if is_mesh_ip(explicit) else ""
    try:
        import contextlib
        import io

        from adk.kvholder_workspace import overlay_ip as _probe

        with contextlib.redirect_stderr(io.StringIO()):  # its "no overlay route" note
            found = _probe()
    except Exception:  # noqa: BLE001 - no reading is "not on a mesh"
        found = ""
    return found if is_mesh_ip(found) else ""


def choose_reach(requested: str, invoke_url: str, mesh_ip: str) -> dict[str, Any]:
    """``{reach, ok, bind_host, url_host, platform, detail, fix}`` for a self-hosted twin.

    Pure. ``url_host`` is the host the registered URL carries ("" = the caller's
    ``--invoke-url`` is used as given, or nothing is registered). ``fix`` lists the
    commands that make an unready reach ready, one per entry.
    """
    requested = (requested or "").strip().lower()
    out: dict[str, Any] = {"reach": requested, "ok": True, "bind_host": "127.0.0.1",
                           "url_host": "", "platform": False, "detail": "", "fix": []}
    if requested == "local":
        out["detail"] = ("loopback only: runs here and is listed by this machine; not "
                         "registered, so the platform cannot drive it")
        return out
    if requested == "tunnel" or (not requested and invoke_url):
        out.update(reach="tunnel", platform=True, bind_host="127.0.0.1",
                   detail=f"public URL {invoke_url}" if invoke_url else "")
        if not invoke_url:
            out.update(ok=False, detail="reach=tunnel needs the public https URL of a "
                                        "tunnel to this agent",
                       fix=["adk host    # serves the agent and opens a free quick tunnel",
                            "adk agent twin deploy --self-host --reach mesh    "
                            "# no public URL: your private mesh"])
        return out
    # mesh, asked for or chosen because nothing else was named
    out.update(reach="mesh", platform=True, bind_host="0.0.0.0", url_host=mesh_ip)
    if mesh_ip:
        out["detail"] = (f"mesh address {mesh_ip}: private to your own mesh, no public URL")
        return out
    out.update(ok=False, bind_host="127.0.0.1",
               detail="this machine has no mesh address (100.64.0.0/10), so the platform "
                      "has no private way to reach a twin on it",
               fix=["adk mesh join    # free: puts this machine on your private mesh",
                    "adk agent twin deploy --self-host --reach local    "
                    "# or run it here without registering"])
    return out


# ── callback bearer (mesh/tunnel only) ───────────────────────────────────────
def stored_bearer(name: str) -> str:
    try:
        return token_path(name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_bearer(name: str, env: Optional[dict] = None) -> tuple[str, str]:
    """``(bearer, where)`` for a twin that listens beyond loopback. Never printed.

    The caller's own ``AITHER_SERVER_API_KEY`` wins; else the one stored for this twin;
    else a new one, written owner-only (``adk._private_file``) before it is used.
    """
    env = os.environ if env is None else env
    given = (env.get("AITHER_SERVER_API_KEY") or "").strip()
    if given:
        return given, "$AITHER_SERVER_API_KEY"
    have = stored_bearer(name)
    if have:
        return have, str(token_path(name))
    from adk._private_file import write_private_text

    token = secrets.token_urlsafe(32)
    write_private_text(token_path(name), token + "\n")
    return token, str(token_path(name))


# ── the local service ────────────────────────────────────────────────────────
def pick_port(want: int, *, explicit: bool, owner: Callable[[int], Optional[str]]
              ) -> tuple[int, str, str]:
    """``(port, note, error)``: the port the twin binds. Anything answering counts as
    taken; an explicit ``--port`` that is taken is an error, the default steps up."""
    if owner(want) is None:
        return want, "", ""
    if explicit:
        return want, "", f":{want} is already serving something else"
    for cand in range(want + 1, want + PORT_SCAN):
        if owner(cand) is None:
            return cand, f":{want} is in use; using :{cand}", ""
    return want, "", f":{want} is in use and nothing is free up to :{want + PORT_SCAN - 1}"


def port_owner(port: int) -> Optional[str]:
    """Who holds this port: 'adk', 'other', or None (free).

    ``agent_daemon.port_owner`` asks ``/health`` and calls silence "free", so a port
    held by something that does not speak HTTP (a relay, a raw socket) looked free and
    the child then died on the bind. Silence is only free when the port can be bound.
    """
    import socket

    from adk import agent_daemon

    who = agent_daemon.port_owner(port)
    if who is not None:
        return who
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if sys.platform == "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("0.0.0.0", port))
    except OSError:
        return "other"
    return None


def serve_argv(port: int, host: str, model: str = "") -> list[str]:
    """``adk run`` for the project in the child's working directory."""
    argv = [sys.executable, "-m", "adk", "run", "--port", str(port), "--host", host]
    if model:
        argv += ["--model", model]
    return argv


def running(state: Optional[dict], *, alive: Optional[Callable[[Any], bool]] = None) -> bool:
    if not state or not state.get("pid"):
        return False
    if alive is None:
        from adk.agent_daemon import pid_alive as alive  # noqa: N813
    return bool(alive(state.get("pid")))


def start(*, name: str, project: str, port: int, host: str, model: str = "",
          bearer: str = "", daemon: Any = None, env: Optional[dict] = None) -> dict[str, Any]:
    """Start the project's agent detached and wait until IT answers ``/health``.

    Returns ``{ok, pid, port, log, error}``. A child that exits, or a port answered by
    a different server, is a failure: the child is stopped and nothing is recorded.
    """
    if daemon is None:
        from adk import agent_daemon as daemon  # noqa: N813
    child_env = dict(os.environ if env is None else env)
    if bearer:
        child_env["AITHER_SERVER_API_KEY"] = bearer
    daemon._ensure_dirs()
    log = daemon.LOG_DIR / f"twin-{name}.log"
    spawned_at = time.time()
    pid = daemon.spawn_detached(serve_argv(port, host, model), log, env=child_env, cwd=project)
    if not daemon.wait_for_health(port, timeout=HEALTH_TIMEOUT_S, pid=pid,
                                  not_before=spawned_at):
        daemon.kill_pid(pid)
        return {"ok": False, "pid": None, "port": port, "log": str(log),
                "error": f"the agent did not answer /health on :{port} within "
                         f"{int(HEALTH_TIMEOUT_S)}s (see {log})"}
    return {"ok": True, "pid": pid, "port": port, "log": str(log), "error": "",
            "started_at": spawned_at}


def stop(name: str, *, daemon: Any = None) -> dict[str, Any]:
    """Stop the twin's local service. ``{known, was_running, stopped}``."""
    if daemon is None:
        from adk import agent_daemon as daemon  # noqa: N813
    state = read_state(name)
    if not state:
        return {"known": False, "was_running": False, "stopped": False}
    pid = state.get("pid")
    was = bool(pid) and daemon.pid_alive(pid)
    stopped = bool(daemon.kill_pid(pid)) if was else False
    return {"known": True, "was_running": was, "stopped": stopped}


__all__ = ["REACHES", "home", "twins_dir", "state_path", "token_path", "read_state",
           "write_state", "clear_state", "all_states", "local_view", "is_mesh_ip",
           "overlay_ip", "choose_reach", "stored_bearer", "ensure_bearer", "pick_port",
           "port_owner",
           "serve_argv", "running", "start", "stop"]
