"""adk mesh worker -- take whole small jobs for the fleet with a small model on this device.

Data parallelism, not a model split: measured 2026-10-22, splitting a big model onto a
small device over the mesh costs ~850 ms/token (ggml RPC) against ~40 ms on the 5090, so
a small device helps only by answering WHOLE small requests (tagging, classification,
short summaries) with its own model while the big GPUs do the deep work.

    adk mesh worker on                       # the model this hardware runs well, 1 slot
    adk mesh worker on --parallel 2          # two requests at once
    adk mesh worker on --model bonsai-4b     # one of WORKER_MODELS, nothing else
    adk mesh worker status [--json]
    adk mesh worker off                      # stop and remove the logon hook

What it does, in order:

1. Refuses unless ``adk.mesh.overlay_status`` says this device is joined to the mesh,
   and unless this device holds its command key (``adk.node_commands``): the server's
   API key is derived from it, so a device that was never enrolled serves nobody.
2. Picks the model: a Steam Deck (AMD "Van Gogh" APU, 16 GB unified, Vulkan) runs
   ``bonsai-8b`` on the Vulkan build; anything else ``bonsai-1.7b``. ``--model`` picks
   from :data:`WORKER_MODELS` only.
3. Installs the pinned, sha256-checked PrismML llama-server and the catalogued weights
   (``adk.bonsai_native`` -- no other download source).
4. Starts it detached, bound ONLY to the tailnet address (or 127.0.0.1 when tailscale
   runs in userspace mode, where tailscaled forwards tailnet connections to loopback),
   with ``--api-key`` = :func:`worker_key` and ``-np <parallel>``.
5. Makes it come back after a reboot (logon task / Run key / systemd user unit), opens
   the Windows firewall to 100.64.0.0/10 only, writes ``~/.aither/worker/state.json``.

The heartbeat (``adk.enrollment``) reads that state file every beat and advertises the
worker as ``inference_kind = "llama-worker"`` with its tailnet URL and model; the fleet's
MicroScheduler derives the same key from the device's command key and sends it as a
bearer. The key itself is never printed: status shows its fingerprint.

Exit codes: 0 serving (or stopped); 1 serving but not healthy yet; 2 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from adk import mesh_lend

logger = logging.getLogger("adk.mesh_worker")

DEFAULT_PORT = 8488
INFERENCE_KIND = "llama-worker"
#: The only models a worker serves. Closed: identity_node_commands and
#: adk.node_commands carry the same tuple for ``worker-on``'s ``model``.
WORKER_MODELS: Tuple[str, ...] = ("bonsai-1.7b", "bonsai-4b", "bonsai-8b")
DEFAULT_MODEL = "bonsai-1.7b"
DECK_MODEL = "bonsai-8b"
MAX_PARALLEL = 4
CTX_PER_SLOT = 2048
#: Domain separation for the API key derived from the device command key. The fleet
#: (MicroScheduler via Identity) derives exactly the same bytes.
KEY_LABEL = b"aither-worker-api-key/v1|"
TASK_NAME = "AwdkMeshWorker"
RUN_VALUE = "AwdkMeshWorker"
UNIT_NAME = "awdk-mesh-worker.service"
FIREWALL_RULE = "awdk mesh worker"
_TAILNET = ipaddress.ip_network("100.64.0.0/10")
#: The Steam Deck's APU as Linux and its DMI name it.
_DECK_MARKERS = ("van gogh", "custom apu 0405", "custom apu 0932", "aerith", "sephiroth",
                 "jupiter", "galileo")


class WorkerError(RuntimeError):
    """A refusal with a message meant for the person at the terminal."""


# ── pure pieces ─────────────────────────────────────────────────────────────


def worker_dir() -> Path:
    return Path(os.environ.get("AITHER_WORKER_DIR") or Path.home() / ".aither" / "worker")


def state_path() -> Path:
    return worker_dir() / "state.json"


def worker_key(command_key_hex: str, node_id: str) -> str:
    """The llama-server API key for this device: HMAC-SHA256(command key, label|node_id).

    Same derivation on the fleet side, so the key never travels.
    """
    if not command_key_hex or not node_id:
        raise WorkerError("no command key; re-enroll this device (adk enroll)")
    return hmac.new(command_key_hex.encode(), KEY_LABEL + node_id.encode(),
                    hashlib.sha256).hexdigest()


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def parse_parallel(value: Any) -> int:
    """1..MAX_PARALLEL as an int; anything else is refused (never clamped silently)."""
    if isinstance(value, bool):
        raise WorkerError("--parallel must be a whole number from 1 to 4")
    try:
        n = int(str(value).strip()) if not isinstance(value, int) else value
    except ValueError as exc:
        raise WorkerError("--parallel must be a whole number from 1 to 4") from exc
    if not 1 <= n <= MAX_PARALLEL:
        raise WorkerError(f"--parallel must be from 1 to {MAX_PARALLEL}, got {n}")
    return n


def host_ok(host: str) -> bool:
    """A tailnet IPv4 (100.64.0.0/10), or 127.0.0.1 (tailscale userspace mode)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.version == 4 and (ip in _TAILNET or str(ip) == "127.0.0.1")


def key_path() -> Path:
    return worker_dir() / "api.key"


def write_key_file(key: str, path: Optional[Path] = None) -> Path:
    """The API key in an owner-only file for ``--api-key-file`` (Athena 2026-10-22: a key
    in argv is readable by every local user from /proc/<pid>/cmdline, and a key in the
    systemd unit outlives a rotation). Created fresh with mode 0600 -- never chmod'ed after
    being written world-readable; on Windows the profile directory's ACL is owner-only."""
    if not key:
        raise WorkerError("no API key")
    path = path or key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key + "\n")
    return path


def server_argv(binary: Path | str, model_path: Path | str, host: str, port: int,
                key_file: Path | str, alias: str, parallel: int, gpu: bool) -> List[str]:
    """The llama-server argv. Refuses a host off the tailnet and a parallel outside 1..4.
    The key is passed as a FILE (``--api-key-file``), never on the command line."""
    if not host_ok(host):
        raise WorkerError(f"refusing to bind {host!r}: only a tailnet address")
    n = parse_parallel(parallel)
    if alias not in WORKER_MODELS:
        raise WorkerError(f"model must be one of {', '.join(WORKER_MODELS)}")
    if not key_file:
        raise WorkerError("no API key file")
    return [str(binary), "-m", str(model_path), "--host", host, "--port", str(int(port)),
            "--api-key-file", str(key_file), "--alias", alias, "-np", str(n),
            "-c", str(CTX_PER_SLOT * n),
            "--no-webui", "--cache-ram", "0", "-ngl", "99" if gpu else "0"]


def redact(argv: List[str]) -> List[str]:
    """``argv`` with the API key replaced by its fingerprint (for state and logs)."""
    out = list(argv)
    for i, a in enumerate(out[:-1]):
        if a == "--api-key":
            out[i + 1] = "<key " + key_fingerprint(out[i + 1]) + ">"
    return out


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def is_steam_deck(accel: Any, *, os_release: Optional[str] = None,
                  board: Optional[str] = None, cpu: Optional[str] = None) -> bool:
    """SteamOS, or the Deck's APU / board name. Inputs default to this machine's files."""
    if getattr(accel, "os_family", "") not in ("linux", ""):
        return False
    rel = (os_release if os_release is not None else _read_text("/etc/os-release")).lower()
    if "id=steamos" in rel.replace('"', ""):
        return True
    names = " ".join([
        board if board is not None else _read_text("/sys/devices/virtual/dmi/id/board_name"),
        cpu if cpu is not None else _read_text("/proc/cpuinfo")[:4000],
        str(getattr(accel, "name", "") or ""),
    ]).lower()
    return any(m in names for m in _DECK_MARKERS)


def choose_model(accel: Any, explicit: str = "", *, deck: Optional[bool] = None) -> str:
    """The model this hardware should serve: ``explicit`` (closed list) wins; a Deck
    runs DECK_MODEL; anything else DEFAULT_MODEL."""
    if explicit:
        if explicit not in WORKER_MODELS:
            raise WorkerError(f"--model must be one of {', '.join(WORKER_MODELS)}")
        return explicit
    on_deck = is_steam_deck(accel) if deck is None else deck
    return DECK_MODEL if on_deck else DEFAULT_MODEL


def plan_for(model: str, accel: Any, *, deck: bool = False) -> Any:
    """bonsai_native's placement for ``model``; a Deck is forced onto its Vulkan build
    (its APU shares the 16 GB, which detect_accel may not report as VRAM)."""
    from adk import bonsai_native as bn

    size = bn.BONSAI_SIZES[model]
    plan = bn.plan_size(size, accel)
    if deck:
        plan = bn.override_placement(plan, size, accel, backend="vulkan")
    elif not plan.fits:
        plan = bn.override_placement(plan, size, accel, backend="cpu")
    return plan


def health_ok(base: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=timeout) as r:
            return int(getattr(r, "status", 0) or r.getcode()) == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


# ── state ───────────────────────────────────────────────────────────────────


def read_state() -> Optional[dict]:
    try:
        st = json.loads(state_path().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def write_state(state: dict) -> Path:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def running_state(pid_alive: Callable[[int], bool] = mesh_lend._pid_alive) -> Optional[dict]:
    """The state file when it names a live server, else None."""
    st = read_state()
    if not st or not pid_alive(int(st.get("pid") or 0)):
        return None
    return st


def advertised(pid_alive: Callable[[int], bool] = mesh_lend._pid_alive,
               probe: Callable[[str], bool] = health_ok) -> Optional[Dict[str, Any]]:
    """What the heartbeat advertises while the worker runs, else None (no change)."""
    st = running_state(pid_alive)
    if not st:
        return None
    ip, port, model = str(st.get("ip") or ""), int(st.get("port") or 0), str(st.get("model") or "")
    if not ip or not port or model not in WORKER_MODELS:
        return None
    try:
        # a rotated command key (keyver bump) makes the running server's key stale: the
        # fleet would only be refused, so withdraw until `adk mesh worker on` re-derives it
        node_id, cmd_key = _command_key_record()
        if key_fingerprint(worker_key(cmd_key, node_id)) != st.get("key_fp"):
            return None
    except Exception:  # noqa: BLE001 -- no key now = nothing the fleet may call
        return None
    probe_host = str(st.get("bind") or ip)
    return {
        "inference_url": f"http://{ip}:{port}",
        "inference_kind": INFERENCE_KIND,
        "available_models": [model],
        "inference_ready": bool(probe(f"http://{probe_host}:{port}")),
    }


def status_dict(pid_alive: Callable[[int], bool] = mesh_lend._pid_alive,
                probe: Callable[[str], bool] = health_ok) -> dict:
    st = read_state()
    if not st:
        return {"serving": False}
    alive = pid_alive(int(st.get("pid") or 0))
    host = str(st.get("bind") or st.get("ip") or "")
    return dict(st, serving=alive,
                healthy=alive and probe(f"http://{host}:{int(st.get('port') or 0)}"))


# ── persistence (mirrors adk.mesh_lend) ─────────────────────────────────────


def _unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT_NAME


def _relaunch_argv(model: str, parallel: int, port: int) -> List[str]:
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    exe = str(pyw if os.name == "nt" and pyw.exists() else py)
    return [exe, "-m", "adk.cli", "mesh", "worker", "on", "--model", model,
            "--parallel", str(int(parallel)), "--port", str(int(port))]


def install_persistence(relaunch: List[str], server: List[str],
                        run: Callable[..., Any] = mesh_lend._run) -> str:
    """Windows: re-run this command at logon (it re-derives the key, never stores it in
    a task). Linux: a systemd user unit running the server (it names the owner-only
    key FILE; the key itself is never in the unit). Returns schtasks|run-key|systemd|none."""
    cmdline = subprocess.list2cmdline(relaunch)
    if os.name == "nt":
        try:
            r = run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
                     "/TN", TASK_NAME, "/TR", cmdline])
            if r.returncode == 0:
                return "schtasks"
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mesh worker: best-effort step failed: %s", exc)
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, cmdline)
            return "run-key"
        except OSError:
            return "none"
    import shutil

    if not shutil.which("systemctl"):
        return "none"
    unit = _unit_path()
    try:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=awdk mesh worker (llama-server, tailnet only)\n"
            "After=network-online.target\n\n"
            f"[Service]\nExecStart={subprocess.list2cmdline(server)}\n"
            "Restart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n",
            encoding="utf-8")
        unit.chmod(0o600)
        if run(["systemctl", "--user", "daemon-reload"]).returncode != 0:
            return "none"
        if run(["systemctl", "--user", "enable", UNIT_NAME]).returncode != 0:
            return "none"
    except (OSError, subprocess.SubprocessError):
        return "none"
    try:
        run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("mesh worker: best-effort step failed: %s", exc)
    return "systemd"


def remove_persistence(run: Callable[..., Any] = mesh_lend._run) -> List[str]:
    removed: List[str] = []
    if os.name == "nt":
        try:
            if run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME]).returncode == 0:
                removed.append("schtasks")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mesh worker: best-effort step failed: %s", exc)
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
            removed.append("run-key")
        except OSError as exc:
            logger.debug("mesh worker: best-effort step failed: %s", exc)
        try:
            run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mesh worker: best-effort step failed: %s", exc)
        return removed
    import shutil

    if shutil.which("systemctl"):
        try:
            run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("mesh worker: best-effort step failed: %s", exc)
    unit = _unit_path()
    if unit.exists():
        unit.unlink()
        removed.append("systemd")
    return removed


def _allow_firewall(exe: Path, port: int, run: Callable[..., Any] = mesh_lend._run) -> bool:
    """Windows: inbound to the server from the tailnet range only."""
    if os.name != "nt":
        return True
    try:
        run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"])
        r = run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}",
                 "dir=in", "action=allow", f"program={exe}", "protocol=TCP",
                 f"localport={int(port)}", "remoteip=100.64.0.0/10"])
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ── commands ────────────────────────────────────────────────────────────────


def _say_print(text: str, is_error: bool = False) -> None:
    print(text, file=sys.stderr if is_error else sys.stdout)


def _command_key_record() -> Tuple[str, str]:
    """(node_id, command key hex) from the device's stored command key, or ('', '')."""
    from adk import node_commands

    try:
        data = json.loads(node_commands._key_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("node_id") or ""), str(data.get("key") or "")


def cmd_status(args: argparse.Namespace, *,
               say: Optional[Callable[[str, bool], None]] = None) -> int:
    say = say or _say_print
    st = read_state()
    out = status_dict()
    if getattr(args, "json", False):
        say(json.dumps(out, indent=2, sort_keys=True), False)
    elif not out["serving"]:
        say("worker off" + (" (state file present, server not running)" if st else ""), False)
    else:
        say(f"worker serving {out.get('model')} on {out.get('ip')}:{out.get('port')} "
            f"({out.get('parallel')} slot(s), pid {out.get('pid')}, {out.get('build')}, "
            f"{'healthy' if out.get('healthy') else 'NOT healthy'}, key {out.get('key_fp')}, "
            f"after reboot: {out.get('persist')})", False)
    return 0 if (not st or out["serving"]) else 1


def cmd_off(args: argparse.Namespace, run: Callable[..., Any] = mesh_lend._run, *,
            say: Optional[Callable[[str, bool], None]] = None) -> int:
    say = say or _say_print
    st = read_state() or {}
    pid = int(st.get("pid") or 0)
    stopped = False
    if pid and mesh_lend._pid_alive(pid):
        try:
            if os.name == "nt":
                run(["taskkill", "/PID", str(pid), "/T", "/F"])
            else:
                os.kill(pid, signal.SIGTERM)
            stopped = True
        except (OSError, subprocess.SubprocessError) as exc:
            say(f"could not stop pid {pid}: {exc}", True)
            return 2
    removed = remove_persistence(run=run)
    state_path().unlink(missing_ok=True)
    key_path().unlink(missing_ok=True)
    say(f"worker stopped{' pid ' + str(pid) if stopped else ' (nothing running)'}; "
        f"removed: {', '.join(removed) or 'no logon hook'}", False)
    return 0


def _install(model: str, plan: Any, accel: Any) -> Tuple[Path, Path]:
    from adk import bonsai_native as bn

    binary = bn.install_server(accel, plan.build or "cpu")
    if binary is None:
        raise WorkerError("could not install the pinned llama-server (see above)")
    weights = bn.install_weights(bn.BONSAI_SIZES[model].weights[plan.quant])
    if weights is None:
        raise WorkerError(f"could not fetch the {model} weights (see above)")
    return Path(binary), Path(weights)


def cmd_on(args: argparse.Namespace, *, overlay: Optional[dict] = None,
           accel: Any = None,
           install: Optional[Callable[[str, Any, Any], Tuple[Path, Path]]] = None,
           popen: Callable[..., Any] = subprocess.Popen,
           run: Callable[..., Any] = mesh_lend._run,
           key_record: Optional[Tuple[str, str]] = None,
           probe: Callable[[str], bool] = health_ok,
           sleep: Callable[[float], None] = time.sleep,
           wait_s: float = 60.0,
           say: Optional[Callable[[str, bool], None]] = None) -> int:
    """``adk mesh worker on``. Every side effect is injectable for tests."""
    say = say or _say_print
    try:
        parallel = parse_parallel(getattr(args, "parallel", 1) or 1)
        port = int(getattr(args, "port", 0) or DEFAULT_PORT)
        if not 0 < port < 65536:
            raise WorkerError("--port must be 1-65535")
        node_id, cmd_key = key_record if key_record is not None else _command_key_record()
        key = worker_key(cmd_key, node_id)
        ov = overlay if overlay is not None else mesh_lend._overlay()
        if not ov.get("joined"):
            say(f"not on the mesh: {ov.get('detail') or ov.get('code') or 'unknown'}. "
                "Run: adk mesh join", True)
            return 2
        host = mesh_lend.bind_host(ov)
        ip = str(ov.get("tailnet_ip") or "")
        if not host_ok(host) or not host_ok(ip) or ip == "127.0.0.1":
            raise WorkerError(f"no tailnet address to serve on (got {ip or '-'})")
        if accel is None:
            from adk import llamacpp_setup as lcs
            accel = lcs.detect_accel()
        deck = is_steam_deck(accel)
        model = choose_model(accel, getattr(args, "model", "") or "", deck=deck)
        prior = running_state()
        if prior and prior.get("model") == model and prior.get("parallel") == parallel \
                and prior.get("port") == port and prior.get("key_fp") == key_fingerprint(key):
            say(f"already serving {model} on {prior.get('ip')}:{port} (pid {prior.get('pid')})",
                False)
            return 0
        if prior:
            cmd_off(args, run=run, say=lambda *_: None)
        plan = plan_for(model, accel, deck=deck)
        binary, weights = (install or _install)(model, plan, accel)
        gpu = plan.placement == "gpu"
        argv = server_argv(binary, weights, host, port, write_key_file(key), model,
                           parallel, gpu)
        proc = mesh_lend.spawn_detached(argv, worker_dir() / "llama-server.log", popen=popen)
        persist = install_persistence(_relaunch_argv(model, parallel, port), argv, run=run)
        pid = int(proc.pid)
        if persist == "systemd":
            try:
                proc.terminate()
                run(["systemctl", "--user", "start", UNIT_NAME])
                r = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT_NAME])
                pid = int((r.stdout or "0").strip() or 0)
            except (OSError, subprocess.SubprocessError, ValueError):
                pid = 0
            if pid <= 0:
                raise WorkerError(f"systemd did not start the server; see "
                                  f"journalctl --user -u {UNIT_NAME}")
        if not _allow_firewall(binary, port, run=run):
            say("note: no inbound firewall rule (needs an elevated shell once); "
                "the fleet may not reach this machine", True)
        state = {"pid": pid, "ip": ip, "bind": host, "port": port, "model": model,
                 "parallel": parallel, "build": plan.build or "cpu",
                 "placement": plan.placement or "cpu", "deck": deck,
                 "key_fp": key_fingerprint(key), "node_id": node_id,
                 "argv": redact(argv), "persist": persist, "started_at": int(time.time()),
                 "platform": platform.system().lower()}
        write_state(state)
    except WorkerError as exc:
        say(f"could not start the worker: {exc}", True)
        return 2
    healthy = False
    deadline = time.time() + wait_s
    while True:
        if probe(f"http://{host}:{port}"):
            healthy = True
            break
        if time.time() >= deadline:
            break
        sleep(1.0)
    say(f"worker serving {model} on {ip}:{port} ({parallel} slot(s), "
        f"{plan.placement or 'cpu'}/{plan.build or 'cpu'}, pid {pid}, key {state['key_fp']}, "
        f"after reboot: {persist}){'' if healthy else ' -- not healthy yet'}", not healthy)
    return 0 if healthy else 1


def cmd_worker(args: argparse.Namespace, **kw: Any) -> int:
    action = getattr(args, "worker_action", "") or "status"
    if action == "on":
        return cmd_on(args, **kw)
    if action == "off":
        return cmd_off(args, say=kw.get("say"))
    return cmd_status(args, say=kw.get("say"))


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("worker_action", nargs="?", choices=("on", "off", "status"),
                   default="status", help="on | off | status (default status)")
    p.add_argument("--model", default="", choices=("",) + WORKER_MODELS,
                   help="model to serve (default: by hardware -- Steam Deck bonsai-8b, "
                        "else bonsai-1.7b)")
    p.add_argument("--parallel", type=int, default=1,
                   help=f"requests served at once, 1-{MAX_PARALLEL} (default 1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"server port on the tailnet address (default {DEFAULT_PORT})")
    p.add_argument("--json", action="store_true", help="status as JSON")
