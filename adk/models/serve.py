"""Where model files live, and how `adk models use` serves one.

THE LAYOUT IS THE BONSAI INSTALLER'S, not a new one. ``install-bonsai.sh`` puts
``bin/llama-server`` and ``models/*.gguf`` under ``~/.aitherium/bonsai``;
``install-bonsai.ps1`` uses ``%LOCALAPPDATA%\\Aitherium\\bonsai``. Both record the server
they started in ``server.pid`` as ``"<pid> <port>"``, serve chat on 127.0.0.1:8080 under
the alias ``bonsai-selfhost``, and point adk at it with ``adk backend set vllm``. ``adk
models pull`` writes into that ``models/`` and ``adk models use`` restarts that server on
a different GGUF with the installer's own arguments, so the browser dock, the installer
and adk keep agreeing about one server.

AN EMBEDDER IS NOT A CHAT MODEL. It is served separately (``--embedding``, its own port
and pid file) and only the embedding endpoint is configured; the chat backend is left
exactly as it was.

LOOPBACK ONLY, as the installer: 0.0.0.0 would publish an unauthenticated inference
server to the whole network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CHAT_PORT = 8080
CHAT_ALIAS = "bonsai-selfhost"
#: The port ``adk.embeddings`` probes on this machine in the aither-code-embed space.
EMBED_PORT = 8229
Say = Callable[[str], None]


class ServeError(RuntimeError):
    """The model could not be served; the message says what to do."""


def install_root() -> Path:
    """The Bonsai installer's directory on this OS (``AITHER_BONSAI_ROOT`` overrides)."""
    override = os.environ.get("AITHER_BONSAI_ROOT", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Aitherium" / "bonsai"
    return Path.home() / ".aitherium" / "bonsai"


def models_dir(root: Optional[Path] = None) -> Path:
    return (root or install_root()) / "models"


def server_binary(root: Optional[Path] = None) -> Path:
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    return (root or install_root()) / "bin" / name


def server_args(model: Dict[str, Any], gguf: Path, port: int) -> List[str]:
    """llama-server arguments for this model.

    Chat models get the installer's exact string (``--reasoning-budget 2048`` is not
    optional: without it the thinking block never closes and ``content`` comes back
    empty). ``-ngl 99`` is always passed; a CPU build ignores it.
    """
    base = ["--model", str(gguf), "--host", "127.0.0.1", "--port", str(port)]
    if model.get("role") == "embedding":
        return base + ["--embedding", "--alias", str(model["id"]), "-ngl", "99"]
    return base + ["--ctx-size", "16384", "--reasoning-budget", "2048",
                   "--alias", CHAT_ALIAS, "-fa", "on",
                   "--temp", "1.0", "--top-p", "0.95", "--top-k", "20", "-ngl", "99"]


def _pidfile(root: Path, role: str) -> Path:
    return root / ("embed.pid" if role == "embedding" else "server.pid")


def _answers(port: int, path: str = "/health", timeout: float = 2.0) -> bool:
    """Is anything answering HTTP on 127.0.0.1:port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # a 404/503 is still somebody listening
    except (urllib.error.URLError, OSError):
        return False


def _healthy(port: int) -> bool:
    """/health answers 200. llama-server answers 503 while the model is still loading."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3):
            return True
    except (urllib.error.URLError, OSError):
        return False


def stop_ours(root: Path, role: str, say: Say) -> None:
    """Stop the server the pid file records, only if it still runs OUR binary.

    A reused pid, or anyone else's llama-server, is left alone.
    """
    pidfile = _pidfile(root, role)
    try:
        pid = int(pidfile.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return
    from adk.home.bonsai2 import _cmdline

    ours = str(server_binary(root)).replace("\\", "/").lower()
    if ours in _cmdline(pid).replace("\\", "/").lower():
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
            say(f"  stopped the previous server (pid {pid})")
        except OSError as exc:
            say(f"  could not stop the previous server (pid {pid}): {exc}")
    pidfile.unlink(missing_ok=True)


def start(model: Dict[str, Any], gguf: Path, port: int, root: Optional[Path] = None,
          say: Say = print, wait_s: float = 300.0) -> int:
    """Serve ``gguf`` on 127.0.0.1:port with the installer's binary. -> pid.

    Raises:
        ServeError: No installed server, the port belongs to another program, or the
            server exited / never became healthy (the log path is named).
    """
    root = root or install_root()
    server = server_binary(root)
    if not server.is_file():
        from adk.home.models import bonsai_install_hint

        raise ServeError(f"no llama-server at {server}. The Bonsai installer puts it "
                         f"there: {bonsai_install_hint()}")
    role = str(model.get("role") or "chat")
    stop_ours(root, role, say)
    deadline = time.monotonic() + 15
    while _answers(port) and time.monotonic() < deadline:
        time.sleep(0.5)
    if _answers(port):
        raise ServeError(f"127.0.0.1:{port} is in use by a program this did not start; "
                         "it was left running. Pass --port to use another port.")
    from adk.home.bonsai2 import _server_env

    log = root / ("embed.log" if role == "embedding" else "server.log")
    kw: Dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = (subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
                               | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kw["start_new_session"] = True
    with open(log, "ab") as logf:
        proc = subprocess.Popen([str(server)] + server_args(model, gguf, port),
                                cwd=str(server.parent), env=_server_env(server),
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, **kw)
    _pidfile(root, role).write_text(f"{proc.pid} {port}\n", encoding="utf-8")
    say(f"  started llama-server pid {proc.pid} on 127.0.0.1:{port}; loading {gguf.name}")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _pidfile(root, role).unlink(missing_ok=True)
            raise ServeError(f"llama-server exited {proc.returncode} while loading "
                             f"{gguf.name}. The log is at {log}")
        if _healthy(port):
            return proc.pid
        time.sleep(1)
    raise ServeError(f"llama-server did not become healthy in {wait_s:.0f}s; it is still "
                     f"running as pid {proc.pid}. The log is at {log}")


def backend_config(model: Dict[str, Any], port: int) -> Dict[str, str]:
    """The saved-config keys that point adk at this served model.

    Chat: what the installer's own ``adk backend set vllm --base-url ... --model
    bonsai-selfhost`` writes, plus the catalogue id that is loaded. Embedding: ONLY the
    embedding keys -- an embedder must never become the chat backend.
    """
    url = f"http://127.0.0.1:{port}/v1"
    if model.get("role") == "embedding":
        return {"embeddings_url": url, "embeddings_model": str(model["id"]),
                "embed_space": str(model["id"])}
    return {"default_backend": "vllm", "inference_url": url,
            "default_model": CHAT_ALIAS, "local_model_id": str(model["id"])}
