"""The local model server this machine ALREADY has installed — find it, start it.

Customer report, 2026-10-14: the llama.cpp server the Windows one-click installer
(aitherium.com/install-bonsai.ps1) put on the machine only starts at Windows sign-in
(a Startup-folder shortcut), so the moment it died the browser said "Server
unavailable" and nothing could bring it back without a terminal. `adk onboard
--quick` did not help either: it saw the Bonsai weights, chose the Docker-only
`adk bonsai-local` path, and failed on a machine with no Docker.

This module is the one place that knows the installed layouts and the exact
arguments each installer serves with, so `adk onboard --quick` and the Desk app's
"Start on this PC" button start the SAME server the installer did:

  bonsai-local     adk bonsai-local    ~/.aither/bonsai-local.json (its exact argv)
  bonsai-selfhost  install-bonsai.ps1  %LOCALAPPDATA%\\Aitherium\\bonsai\\{bin,models}
                   install-bonsai.sh   ~/.aitherium/bonsai/{bin,models}
  llamacpp         adk quickstart-local / llamacpp_setup: ~/.aither/llamacpp + the
                   ``llamacpp`` block it saved in ~/.aither/config.json

``python -m adk.local_model_server`` prints the spec as JSON (exit 0) or
``{"found": false}`` (exit 3); ``--start`` also starts it detached and waits for
/health. Desk calls the JSON form and supervises the server process itself.
Stdlib only, Python 3.10-compatible.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SELFHOST_DEFAULT_PORT = 8080
SELFHOST_ALIAS = "bonsai-selfhost"

# install-bonsai.{ps1,sh} serve with exactly these (see "-- 5. start it" there):
# --reasoning-budget bounds Bonsai's forced <think> block, -fa + PrismML sampling.
_SELFHOST_TAIL = [
    "--ctx-size", "16384", "--reasoning-budget", "2048", "--alias", SELFHOST_ALIAS,
    "-fa", "on", "--temp", "1.0", "--top-p", "0.95", "--top-k", "20",
]


def selfhost_root(env: Optional[Dict[str, str]] = None, platform: str = sys.platform,
                  home: Optional[Path] = None) -> Path:
    """Where install-bonsai put its server and weights on this OS."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "Aitherium" / "bonsai"
    return home / ".aitherium" / "bonsai"


def _newest_model(models: Path) -> Optional[Path]:
    """The weights the installer served last: the newest .gguf that is not a vision tower."""
    try:
        files = [p for p in models.glob("*.gguf")
                 if p.is_file() and "mmproj" not in p.name.lower() and not p.name.endswith(".part")]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _recorded_port(root: Path) -> int:
    """server.pid holds "<pid> <port>" of the last server the installer started."""
    try:
        parts = (root / "server.pid").read_text(encoding="utf-8").split()
        port = int(parts[1])
        if 0 < port < 65536:
            return port
    except (OSError, ValueError, IndexError) as e:
        logger.debug("no usable port in %s/server.pid (%s); using the default", root, e)
    return SELFHOST_DEFAULT_PORT


def _selfhost_spec(root: Path, platform: str) -> Optional[Dict[str, Any]]:
    exe = root / "bin" / ("llama-server.exe" if platform == "win32" else "llama-server")
    if not exe.is_file():
        return None
    model = _newest_model(root / "models")
    if model is None:
        return None
    port = _recorded_port(root)
    try:
        stamp = (root / "release.txt").read_text(encoding="utf-8").strip()
    except OSError:
        stamp = ""
    backend = stamp.rsplit("/", 1)[-1] if "/" in stamp else ""
    argv = [str(exe), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
            *_SELFHOST_TAIL]
    if backend != "cpu":  # the installer offloads every layer unless it chose the CPU build
        argv += ["-ngl", "99"]
    return {
        "found": True, "kind": "bonsai-selfhost", "argv": argv, "cwd": str(exe.parent),
        "port": port, "model": SELFHOST_ALIAS, "weights": model.name,
        "health": f"http://127.0.0.1:{port}/health",
        "inference_url": f"http://127.0.0.1:{port}/v1",
        "log": str(root / "server.log"),
    }


def _llamacpp_spec(home: Path) -> Optional[Dict[str, Any]]:
    """The server `adk quickstart-local` installed (llamacpp_setup.register_config)."""
    try:
        cfg = json.loads((home / ".aither" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    block = cfg.get("llamacpp") if isinstance(cfg, dict) else None
    if not isinstance(block, dict) or not block.get("model_path"):
        return None
    model = Path(str(block["model_path"]))
    if not model.is_file():
        return None
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    llamacpp_dir = home / ".aither" / "llamacpp"
    bins = sorted(llamacpp_dir.rglob(name)) if llamacpp_dir.is_dir() else []
    if not bins:
        return None
    port = int(block.get("port") or 8200)
    from adk.llamacpp_setup import DEFAULT_SERVED_NAME, AccelInfo, _build_server_cmd

    accel = AccelInfo(kind=str(block.get("accel") or "cpu"))
    return {
        "found": True, "kind": "llamacpp", "argv": _build_server_cmd(bins[0], model, port, accel),
        "cwd": str(bins[0].parent), "port": port, "model": DEFAULT_SERVED_NAME,
        "weights": model.name, "health": f"http://127.0.0.1:{port}/health",
        "inference_url": f"http://127.0.0.1:{port}/v1",
        "log": str(home / ".aither" / "logs" / "llamacpp-server.log"),
    }


def _bonsai_local_spec(home: Path) -> Optional[Dict[str, Any]]:
    """The server `adk bonsai-local` installed natively (adk.bonsai_native's state file).

    It records the exact argv it serves with, so this starts the same server its
    autostart does -- after checking the binary and weights are still on disk."""
    try:
        state = json.loads((home / ".aither" / "bonsai-local.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    argv = state.get("argv")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        return None
    if not (Path(argv[0]).is_file() and Path(str(state.get("weights", ""))).is_file()):
        return None
    try:
        port = int(state.get("port") or 8090)
    except (TypeError, ValueError):
        return None
    return {
        "found": True, "kind": "bonsai-local", "argv": argv,
        "cwd": str(state.get("cwd") or Path(argv[0]).parent), "port": port,
        "model": "bonsai-local", "weights": Path(str(state["weights"])).name,
        "health": f"http://127.0.0.1:{port}/health",
        "inference_url": f"http://127.0.0.1:{port}/v1",
        "log": str(home / ".aither" / "logs" / "bonsai-local.log"),
    }


def find_local_model_server(env: Optional[Dict[str, str]] = None, platform: str = sys.platform,
                            home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The installed local model server, or None. Never downloads, never starts anything.

    `adk bonsai-local` (the Desk's Local AI screen) first: it is the one the person chose
    most recently through us; then install-bonsai's; then quickstart-local's llama.cpp."""
    home = Path.home() if home is None else home
    spec = _bonsai_local_spec(home)
    if spec is not None:
        return spec
    spec = _selfhost_spec(selfhost_root(env, platform, home), platform)
    if spec is not None:
        return spec
    return _llamacpp_spec(home)


def healthy(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - loopback only
            return r.status == 200
    except Exception:  # noqa: BLE001 - not answering is the answer
        return False


def start_local_model_server(spec: Optional[Dict[str, Any]] = None, wait_s: float = 180.0,
                             popen=subprocess.Popen, is_healthy=healthy,
                             sleep=time.sleep) -> Dict[str, Any]:
    """Start the installed server detached (already answering = reuse) and wait for /health."""
    spec = spec if spec is not None else find_local_model_server()
    if not spec:
        return {"ok": False, "error": "no local model server is installed on this machine"}
    if is_healthy(spec["health"]):
        return {"ok": True, "started": False, **spec}
    log_path = Path(spec["log"])
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: Dict[str, Any] = {"cwd": spec["cwd"], "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            # detached, new process group, no window
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        else:
            kwargs["start_new_session"] = True
        with open(log_path, "ab") as log:
            proc = popen(spec["argv"], stdout=log, stderr=log, **kwargs)
    except OSError as e:
        return {"ok": False, "error": f"could not start {spec['argv'][0]}: {e}", **spec}
    deadline = wait_s
    while deadline > 0:
        if is_healthy(spec["health"]):
            pid = getattr(proc, "pid", None)
            return {"ok": True, "started": True, "pid": pid, **spec}
        if getattr(proc, "poll", lambda: None)() is not None:
            err = f"the model server exited while loading; see {spec['log']}"
            return {"ok": False, "error": err, **spec}
        sleep(2)
        deadline -= 2
    err = f"the model server did not answer {spec['health']} in {int(wait_s)}s"
    return {"ok": False, "error": err, **spec}


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--start" in args:
        res = start_local_model_server()
        print(json.dumps(res))
        return 0 if res.get("ok") else 1
    spec = find_local_model_server()
    print(json.dumps(spec or {"found": False}))
    return 0 if spec else 3


if __name__ == "__main__":
    sys.exit(main())
