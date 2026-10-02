"""``adk home teach setup`` -- a teacher's own agent, on their own computer, at no cost.

One command, in this order:

1. **Probe** the edge for ``/api/v1/classroom`` BEFORE any other network call. A
   routed endpoint answers 401/403 to an anonymous GET (or 200/422); a 404, a 5xx or
   no answer means the classroom is not reachable from here, and the setup says
   ``classroom: offline`` plainly instead of pretending.
2. ``init`` the Agent Home if there is none.
3. Pick the local **Bonsai** model preset (``models.PRESETS['bonsai']``) unless the
   home already runs a local Bonsai (``bonsai`` / ``bonsai2``) or ``keep_model``.
   ``keep_model`` keeps a LOCAL model only: a bring-your-own-key or remote model is
   refused (``HomeError``), because the classroom tools never run on one.
4. **Sign in** (device code) when the home holds no sign-in and the edge is online.
5. Record ``AITHER_HOME_TEACHER=1`` (and the classroom URL) in ``<home>/teacher.json``,
   which :func:`adk.home.serve.teacher_enabled` reads.
6. Print the ``adk home serve --install`` hint.

Nothing here downloads a model or starts a server: ``adk home model --local bonsai
--check`` and the Bonsai installer do that, and the hint names them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import config as hc
from . import models

PROBE_PATH = "/api/v1/classroom/classes"
_PROBE_TIMEOUT = 10.0
#: Anonymous answers that prove the route exists behind auth.
_ROUTED = (200, 401, 403, 422)
LOCAL_BONSAI = ("bonsai", "bonsai2")


def probe_classroom(base_url: str, client: Optional[Any] = None) -> Dict[str, Any]:
    """Does the edge at ``base_url`` route ``/api/v1/classroom``? Never raises."""
    url = (base_url or "").rstrip("/") + PROBE_PATH
    try:
        if client is not None:
            resp = client.request("GET", url)
        else:
            import httpx

            from .teacher_tools import _verify

            with httpx.Client(timeout=_PROBE_TIMEOUT, verify=_verify()) as c:
                resp = c.request("GET", url)
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
        return {"online": False, "url": url, "status": None,
                "why": f"unreachable ({type(exc).__name__})"}
    code = int(resp.status_code)
    if code in _ROUTED:
        return {"online": True, "url": url, "status": code, "why": "routed"}
    why = ("the edge does not route /api/v1/classroom" if code == 404
           else f"the edge answered HTTP {code}")
    return {"online": False, "url": url, "status": code, "why": why}


def write_teacher_state(url: str, root: Optional[Path] = None) -> Path:
    from .serve import TEACHER_FLAG_ENV, TEACHER_STATE

    path = (root or hc.home_dir()) / TEACHER_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({TEACHER_FLAG_ENV: "1", "url": url.rstrip("/")}, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def setup(url: str = "", *, signin: bool = True, keep_model: bool = False,
          root: Optional[Path] = None,
          probe: Callable[[str], Dict[str, Any]] = probe_classroom,
          do_signin: Optional[Callable[[], int]] = None,
          signed_in: Optional[Callable[[], bool]] = None,
          out: Callable[[str], None] = print) -> Dict[str, Any]:
    """Run the setup; returns what it did (the CLI prints it). See the module doc."""
    from .serve import teacher_url

    base = (url or teacher_url(root)).rstrip("/")
    result: Dict[str, Any] = {"url": base}

    # 1. the probe comes before ANY other network call
    reach = probe(base)
    result["classroom"] = "online" if reach.get("online") else "offline"
    result["probe"] = reach
    if reach.get("online"):
        out(f"classroom: online ({base})")
    else:
        out(f"classroom: offline -- {reach.get('why')} ({reach.get('url')}). Setup "
            "continues on this computer; the classroom tools answer 'unreachable' "
            "until it is online.")

    # 2. init
    if not hc.is_initialized(root):
        hc.init_home(name="teacher-agent", root=root)
        result["init"] = "created"
    else:
        result["init"] = "existing"

    # 3. the local Bonsai preset
    cfg = hc.load_config(root)
    if keep_model and not models.is_local(cfg.model):
        # Student records never go to a bring-your-own-key or remote model: refuse
        # before the teacher flag is written, so nothing is half on.
        from .serve import TEACHER_NEEDS_LOCAL

        raise hc.HomeError(f"--keep-model refused ({cfg.model.provider} is not a model "
                           f"on this computer): {TEACHER_NEEDS_LOCAL}")
    if keep_model or (models.is_local(cfg.model) and cfg.model.provider in LOCAL_BONSAI):
        result["model"] = f"kept {cfg.model.provider}"
    else:
        if result["init"] == "existing":
            # An existing home may run another model for every channel of its agent:
            # the caller is told what was replaced (``start`` says it in plain words).
            result["model_previous"] = str(cfg.model.provider or "")
        cfg.model = models.choose_model("bonsai")
        hc.save_config(cfg, root)
        result["model"] = "bonsai"
        out("model: local Bonsai (nothing leaves this computer). "
            + models.bonsai_install_hint())

    # 4. sign in when needed and reachable
    if signed_in is None:
        from .connector_tools import home_signed_in as signed_in
    if signed_in():
        result["signin"] = "existing"
    elif not signin:
        result["signin"] = "skipped"
    elif not reach.get("online"):
        result["signin"] = "skipped (offline)"
    elif do_signin is None:
        result["signin"] = "needed"
    else:
        result["signin"] = "done" if do_signin() == 0 else "failed"

    # 5. the flag
    result["state"] = str(write_teacher_state(base, root))
    out("teacher tools: on (AITHER_HOME_TEACHER=1 in " + result["state"] + ")")

    # 6. the hint
    out("Next: adk home model --local bonsai --check, then "
        "adk home serve --install (your agent starts at logon and answers only you).")
    return result


# ── one click: `adk home teach start` ──────────────────────────────────────────
#
# What the downloadable launcher runs after it has put awdk and a local model on
# the computer. It takes a teacher from "nothing is running" to "my agent is
# running, signed in, and the Classroom page in my browser is talking to it"
# without a command being typed: setup (above), start-at-logon, start now, wait,
# then open the page with a one-time pairing code in the URL FRAGMENT (a fragment
# is never sent to a server; the page removes it from the address bar on arrival).

#: The page the launcher opens. Its origin must be on the serve's browser allowlist.
DEFAULT_PAGE = "https://academy.aitherium.com/classroom/agent"
#: Seconds to wait for the serve to answer after starting it.
START_WAIT_S = 90.0
#: The fragment key the page reads the one-time code from.
PAIR_FRAGMENT = "pair"


def _version() -> str:
    import adk

    return str(adk.__version__)


def stop_serve(root: Optional[Path] = None, ops: Optional["StartOps"] = None) -> bool:
    """Stop THIS home's running serve (``adk home teach stop``; the remover runs it).

    True only when a serve that proved it holds this home's token was signalled. A
    ``local.token`` left behind by a dead serve names a pid that may now belong to
    another program: nothing is signalled unless the serve itself answers."""
    ops = ops or StartOps(root)
    if not ops.status():
        return False
    return bool(ops.stop())


def page_origin(page: str) -> str:
    """``scheme://host[:port]`` of ``page``, lowercased ("" when it is not a URL)."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(page or "")
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def pair_url(page: str, code: str) -> str:
    """``page`` with the one-time code in its fragment (any old fragment is dropped)."""
    return (page or DEFAULT_PAGE).split("#", 1)[0] + f"#{PAIR_FRAGMENT}={code}"


class StartOps:
    """Everything ``start`` does to the machine, in one place a test replaces."""

    def __init__(self, root: Optional[Path] = None):
        self.root = root

    # -- the running serve ---------------------------------------------------------
    def status(self) -> Optional[Dict[str, Any]]:
        """``/browser/status`` of THIS home's serve; None when none is running.

        The listener must first prove it holds this home's token (``/hello``), so a
        stranger on the port is "not running", never "our agent"."""
        import httpx

        from .local_client import LocalClient, LocalClientError

        try:
            client = LocalClient(root=self.root, timeout=10.0)
            with client._client(10.0) as http:
                client._verify(http)
                resp = http.get(f"{client.url}/browser/status")
        except (LocalClientError, httpx.HTTPError):
            return None
        if resp.status_code != 200:
            # A serve older than /browser/status: it runs, but says nothing about itself.
            return {"running": True, "teacher": False, "signed_in": False, "old": True}
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def stop(self) -> bool:
        """Ask the serve recorded in ``local.token`` to exit. True when a pid was signalled."""
        import signal

        from .transports.local import token_path

        try:
            raw = json.loads(token_path(self.root).read_text(encoding="utf-8"))
            pid = int(raw.get("pid"))
        except (OSError, ValueError, TypeError, AttributeError):
            return False
        if pid <= 0 or pid == os.getpid():
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True

    # -- start at logon, and now ---------------------------------------------------
    def serve_argv(self) -> list:
        import sys

        return [sys.executable, "-m", "adk.home", "serve"]

    def autostart_present(self) -> bool:
        return self.autostart_file().is_file()

    def autostart_file(self) -> Path:
        """The file the logon entry runs from: launcher (Windows), plist, or unit."""
        import sys

        from adk import agent_daemon

        from .cli import SERVE_AUTOSTART

        if sys.platform == "win32":
            return agent_daemon.launcher_path(SERVE_AUTOSTART)
        if sys.platform == "darwin":
            return (agent_daemon.launchd_agents_dir()
                    / f"com.aitherium.{SERVE_AUTOSTART}.plist")
        return agent_daemon.systemd_user_dir() / f"{SERVE_AUTOSTART}.service"

    def autostart_current(self) -> bool:
        """True when the logon entry starts THIS interpreter's awdk.

        The entry's name is shared with any earlier ``adk home serve --install`` made
        from another Python (pipx, an older venv). One that names another interpreter
        would start that other awdk at every logon, so it is not "ours"."""
        import sys
        from xml.sax.saxutils import escape

        try:
            text = self.autostart_file().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        exe = sys.executable
        # raw (systemd), repr-escaped (the Windows launcher), XML-escaped (the plist)
        return any(form in text for form in (exe, repr(exe)[1:-1], escape(exe)))

    def install_autostart(self) -> Optional[str]:
        from adk import agent_daemon

        from .cli import SERVE_AUTOSTART

        env = {hc.HOME_ENV: os.environ[hc.HOME_ENV]} if os.environ.get(hc.HOME_ENV) else {}
        return agent_daemon.install_user_autostart(
            SERVE_AUTOSTART, self.serve_argv(), env=env,
            description="Aither Hearth: adk home serve")

    def spawn(self) -> bool:
        """Start the serve now, detached, with no window. True when a start was issued."""
        import subprocess
        import sys

        from adk import agent_daemon

        from .cli import SERVE_AUTOSTART

        # Only an entry that starts THIS awdk: one left by another Python would start
        # that other copy, which is exactly what the caller is replacing.
        ours = self.autostart_current()
        if sys.platform == "darwin" and ours:
            rc = subprocess.run(["launchctl", "kickstart", "-k",
                                 f"gui/{os.getuid()}/com.aitherium.{SERVE_AUTOSTART}"],
                                capture_output=True)
            if rc.returncode == 0:
                return True
        elif sys.platform not in ("win32", "darwin") and ours:
            rc = subprocess.run(["systemctl", "--user", "restart",
                                 f"{SERVE_AUTOSTART}.service"], capture_output=True)
            if rc.returncode == 0:
                return True
        argv = self.serve_argv()
        kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            launcher = agent_daemon.launcher_path(SERVE_AUTOSTART)
            pyw = agent_daemon.pythonw_executable()
            if pyw is not None and ours and launcher.is_file():
                argv = [str(pyw), str(launcher)]      # the same no-window path as logon
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            kwargs["close_fds"] = True
        else:
            kwargs["start_new_session"] = True
        log = agent_daemon.LOG_DIR / f"{SERVE_AUTOSTART}.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "ab") as fh:
                subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, **kwargs)
        except OSError:
            return False
        return True

    # -- the page ------------------------------------------------------------------
    def browser_code(self) -> Dict[str, Any]:
        from .local_client import LocalClient

        return LocalClient(root=self.root, timeout=30.0).browser_code()

    def open_page(self, url: str) -> bool:
        import webbrowser

        try:
            return bool(webbrowser.open(url))
        except Exception:  # noqa: BLE001 - no browser is reported, never fatal
            return False

    def model_probe(self) -> Dict[str, Any]:
        return models.probe(hc.load_config(self.root).model)

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)


def start(url: str = "", *, page: str = DEFAULT_PAGE, signin: bool = True,
          open_page: bool = True, autostart: bool = True,
          root: Optional[Path] = None, ops: Optional[StartOps] = None,
          do_signin: Optional[Callable[[], int]] = None,
          signed_in: Optional[Callable[[], bool]] = None,
          probe: Callable[[str], Dict[str, Any]] = probe_classroom,
          wait_s: float = START_WAIT_S,
          out: Callable[[str], None] = print) -> Dict[str, Any]:
    """Setup, start at logon, start now, and open the page paired. Returns what it did.

    ``result["ready"]`` is True only when a serve of THIS home answered, holds the
    classroom tools AND is signed in (without a sign-in it cannot read one class).
    Every step that could not be done is named in ``result`` and said in plain words
    through ``out``; nothing is reported as done that was not. Nothing printed here
    is a command to type: the launcher is the teacher's only instrument.
    """
    from .transports.browser import browser_origins

    ops = ops or StartOps(root)
    allowed = browser_origins()
    if open_page and page_origin(page) not in allowed:
        # Refuse before anything is changed: the serve would 403 this page anyway.
        raise hc.HomeError(f"{page!r} is not a page this agent pairs with (allowed: "
                           f"{', '.join(allowed) or 'none'})")

    result = setup(url, signin=signin, root=root, probe=probe, do_signin=do_signin,
                   signed_in=signed_in, out=lambda _line: None)
    out("classroom: " + ("online" if result["classroom"] == "online"
                         else f"offline ({result['probe'].get('why')})"))
    out("sign-in: " + str(result["signin"]))
    if result.get("model_previous"):
        # Said, never swallowed: this changes the model for every channel of an agent
        # that existed before the launcher was opened.
        out(f"model: this agent used {result['model_previous']} before. It now uses the "
            "model on this computer, for everything it does: the classroom tools run "
            "only there.")

    model = ops.model_probe()
    result["model_up"] = bool(model.get("ok"))
    result["model_detail"] = str(model.get("detail") or "")
    if not result["model_up"]:
        out("model: not answering on this computer yet. The agent starts, but it cannot "
            "reply until the model does; opening the launcher again installs or starts it.")

    before = ops.status()
    # Tools and the sign-in are read once, when the serve starts: a serve that was
    # already running before this setup has neither.
    # The launcher upgrades awdk on every open: a serve started from the files of an
    # older version would go on running while loading the new ones piece by piece.
    outdated = bool(before) and before.get("version") != _version()
    stale = bool(before) and (not before.get("teacher") or result["signin"] == "done"
                              or bool(before.get("old")) or outdated)
    if not autostart:
        result["autostart"] = "skipped"
    elif ops.autostart_present() and ops.autostart_current():
        result["autostart"] = "existing"
    else:
        # Absent, or left by another Python's awdk under the same name: (re)write it,
        # so logon and the start below both run THIS awdk.
        replaced = ops.autostart_present()
        where = ops.install_autostart()
        result["autostart"] = where or "not installed"
        if not where:
            out("start at logon: could NOT be installed; the agent runs until this "
                "computer restarts.")
        elif replaced:
            result["autostart_replaced"] = True
            stale = bool(before)        # whatever answers was started by the other copy
            out("start at logon: an older entry started another copy of the agent "
                "toolkit. It now starts this one.")

    if before and not stale:
        result["serve"] = "already running"
    else:
        if before:
            ops.stop()
            ops.sleep(2.0)
        result["serve"] = "started" if ops.spawn() else "could not start"

    restarted = result["serve"] == "started"
    status = None
    waited = 0.0
    while True:
        status = ops.status()
        # After a restart, an answer WITHOUT the tools may be the old process on its
        # way out: keep waiting for the new one until the budget is spent.
        if status and (not (stale and restarted)
                       or (status.get("teacher") and not (
                           outdated and status.get("version") != _version()))):
            break
        if result["serve"] == "could not start" or waited >= wait_s:
            break
        ops.sleep(1.0)
        waited += 1.0
    result["status"] = status or {"running": False}
    result["ready"] = bool(status and status.get("teacher") and status.get("signed_in"))
    if not status:
        out(f"agent: NOT running. Nothing answered on this computer after {int(waited)} "
            "seconds.")
        return result
    if not status.get("teacher"):
        out("agent: running, but the classroom tools are OFF. They turn on only when the "
            "agent's model runs on this computer.")
    elif not status.get("signed_in"):
        out("agent: running with the classroom tools, but NOT signed in: it cannot read "
            "your classes. Open the launcher again and approve the sign-in in your browser.")
    else:
        out("agent: running, signed in, classroom tools on.")

    if open_page:
        result["page"] = "not opened"
        try:
            code = str(ops.browser_code().get("code") or "")
        except hc.HomeError as exc:
            code = ""
            result["page_error"] = str(exc)
            out("browser: the page could not be connected by itself this time; open the "
                "launcher again.")
        if code:
            if ops.open_page(pair_url(page, code)):
                result["page"] = "opened"
                out(f"browser: opened {page} (already connected to this agent)")
            else:
                out(f"browser: could not open a browser; open {page} yourself")
    return result
