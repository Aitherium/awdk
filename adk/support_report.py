"""Crash, error and feedback reports from THIS device to the company app it belongs to.

When something breaks on a customer's PC the people who can fix it used to learn about
it only if the customer pasted a log into an email. This module is the device half of
the support plane:

* :func:`enqueue` / :func:`report_exception` -- write one scrubbed report into the local
  outbox ``~/.aither/support-outbox/`` (one JSON file each). Writing never needs the
  network, so a crash while offline is kept, not lost.
* :func:`flush` -- upload what is queued to the company app the setup page installed
  (``adk tenant watch --url``): with the device's own secret
  (``POST <api>/api/devices/<id>/reports``, ``X-Device-Token``) when this PC is listed
  in the app, else with the ``adk login`` bearer (``POST <api>/api/support/reports``),
  else -- only for a setup result -- anonymously (``POST <api>/api/support/setup-report``).
  The app derives the tenant from that credential, never from anything sent here.
* :class:`SupportFlusher` -- a throttled :func:`flush` driven by ``adk tenant watch``.

The outbox is shared: the Desk app and the setup door write the same file format, so
whichever of them is online first delivers everything. A file is claimed by an atomic
rename before upload, so two uploaders never send one report twice.

Privacy. Reports are scrubbed through awreport (secrets, tokens, home paths) BEFORE they
are written to disk, and fail closed: a report that cannot be scrubbed is not kept. The
app scrubs again on arrival. Reports are on by default for a company install and off
with one setting: ``adk feedback off`` (writes ``~/.aither/support.json``) or the
environment variable ``AITHER_SUPPORT_REPORTS=0``.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

log = logging.getLogger("adk.support_report")

DEVICE_REPORT_PATH = "/api/devices/{device_id}/reports"
SESSION_REPORT_PATH = "/api/support/reports"
SETUP_REPORT_PATH = "/api/support/setup-report"

KINDS = ("crash", "error", "feedback", "setup")
ENV_OPT_OUT = "AITHER_SUPPORT_REPORTS"

#: Caps that keep one report small (the app enforces its own, larger ones).
MAX_TITLE = 200
MAX_MESSAGE = 8000
MAX_STACK = 16000
MAX_LOG_LINES = 200
MAX_LOG_LINE = 500
#: The outbox never grows past this many files; the oldest are dropped first.
MAX_OUTBOX = 200
#: Seconds between two flush attempts from the watcher.
FLUSH_INTERVAL_S = 60.0

_OFF = {"0", "false", "off", "no", "disabled"}
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


class ReportNotRedactableError(RuntimeError):
    """The report could not be scrubbed, so it is not kept and not sent."""


def aither_dir() -> Path:
    return Path.home() / ".aither"


def default_outbox() -> Path:
    return aither_dir() / "support-outbox"


def settings_path() -> Path:
    return aither_dir() / "support.json"


def enabled(path: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> bool:
    """True unless the customer turned reports off (env wins over the settings file)."""
    env = os.environ if env is None else env
    raw = str(env.get(ENV_OPT_OUT, "")).strip().lower()
    if raw:
        return raw not in _OFF
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if isinstance(data, dict) and data.get("enabled") is False:
        return False
    return True


def set_enabled(on: bool, path: Optional[Path] = None) -> Path:
    """Persist the customer's choice (``adk feedback on|off``); Desk reads the same file."""
    p = path or settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["enabled"] = bool(on)
    data["changed_at"] = int(time.time())
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 15] + " ...[truncated]"


def _clip_lines(lines: Optional[Iterable[Any]]) -> List[str]:
    out = [_clip(x, MAX_LOG_LINE) for x in (lines or [])]
    return out[-MAX_LOG_LINES:]


def _adk_version() -> str:
    try:
        from adk import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001 - a version lookup must never stop a report
        return ""


def build_report(kind: str, source: str, title: str, *, message: str = "",
                 stack: str = "", logs: Optional[Iterable[Any]] = None,
                 app_version: str = "", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One report envelope -- the same shape Desk and the setup door write."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    return {
        "id": uuid.uuid4().hex,
        "created_at": time.time(),
        "kind": kind,
        "source": _clip(source, 64),
        "title": _clip(title, MAX_TITLE),
        "message": _clip(message, MAX_MESSAGE),
        "stack": _clip(stack, MAX_STACK),
        "logs": _clip_lines(logs),
        "app_version": _clip(app_version or _adk_version(), 64),
        "os": platform.system().lower(),
        "os_version": _clip(platform.version(), 128),
        "runtime": f"python {platform.python_version()}",
        "extra": dict(extra or {}),
    }


def scrub(report: Dict[str, Any]) -> Dict[str, Any]:
    """Scrub every text field through awreport, then e-mail addresses. Fails closed.

    ``screenshot`` (a PNG the person chose to attach) is passed through untouched:
    pixels are not text, and a regex over base64 would only corrupt the image.
    """
    try:
        from awreport.redact import Redactor
    except Exception as exc:  # noqa: BLE001 - no scrubber = nothing leaves the machine
        raise ReportNotRedactableError(f"awreport is not available: {exc}") from exc
    shot = report.get("screenshot")
    body = {k: v for k, v in report.items() if k != "screenshot"}
    try:
        clean = Redactor().redact_value(body)
    except Exception as exc:  # noqa: BLE001 - RedactionError or anything else: refuse
        raise ReportNotRedactableError(str(exc)) from exc

    def _mail(v: Any) -> Any:
        if isinstance(v, str):
            return _EMAIL_RE.sub("[EMAIL]", v)
        if isinstance(v, list):
            return [_mail(x) for x in v]
        if isinstance(v, dict):
            return {k: _mail(x) for k, x in v.items()}
        return v

    out = _mail(clean)
    if shot:
        out["screenshot"] = shot
    out["scrubbed"] = True
    return out


def _outbox_files(outbox: Path) -> List[Path]:
    try:
        return sorted(p for p in outbox.iterdir() if p.suffix == ".json" and p.is_file())
    except OSError:
        return []


def enqueue(
    report: Dict[str, Any],
    *,
    outbox: Optional[Path] = None,
    settings: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> Optional[Path]:
    """Scrub and queue one report. None when reports are off or it could not be scrubbed."""
    if not enabled(settings, env):
        return None
    try:
        clean = scrub(report)
    except ReportNotRedactableError as exc:
        log.warning("support report not kept (could not scrub): %s", exc)
        return None
    box = outbox or default_outbox()
    box.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time() * 1000):013d}-{clean.get('id') or uuid.uuid4().hex}.json"
    tmp = box / (name + ".tmp")
    tmp.write_text(json.dumps(clean), encoding="utf-8")
    final = box / name
    os.replace(tmp, final)
    files = _outbox_files(box)
    for old in files[: max(0, len(files) - MAX_OUTBOX)]:
        try:
            old.unlink()
        except OSError as exc:
            log.debug("support report: could not trim %s: %s", old, exc)
    return final


def report_exception(source: str, exc: BaseException, *, context: str = "",
                     kind: str = "error", outbox: Optional[Path] = None,
                     settings: Optional[Path] = None,
                     env: Optional[Dict[str, str]] = None) -> Optional[Path]:
    """Queue an exception as a report. Never raises: reporting must not add a failure."""
    try:
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        title = f"{source}: {type(exc).__name__}: {exc}"
        report = build_report(kind, source, title, message=context, stack=stack)
        return enqueue(report, outbox=outbox, settings=settings, env=env)
    except Exception as err:  # noqa: BLE001
        log.debug("support report_exception failed: %s", err)
        return None


def install_excepthook(source: str, *, outbox: Optional[Path] = None) -> Callable[..., Any]:
    """Queue every uncaught exception of this process as a crash, then run the old hook."""
    previous = sys.excepthook

    def _hook(exc_type: Any, exc: BaseException, tb: Any) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            report_exception(source, exc, kind="crash", outbox=outbox)
        previous(exc_type, exc, tb)

    sys.excepthook = _hook
    return previous


# ── Upload ────────────────────────────────────────────────────────────────────


def _route(report: Dict[str, Any], api_url: str, device: Dict[str, Any],
           bearer: str) -> Optional[tuple]:
    """(url, headers, auth) for this report, or None when nothing may carry it."""
    base = api_url.strip().rstrip("/")
    device_id, token = str(device.get("device_id") or ""), str(device.get("token") or "")
    if device_id and token and not device.get("removed"):
        return (base + DEVICE_REPORT_PATH.format(device_id=device_id),
                {"X-Device-Token": token}, "device")
    if bearer:
        return (base + SESSION_REPORT_PATH, {"Authorization": f"Bearer {bearer}"}, "session")
    if report.get("kind") == "setup":
        return (base + SETUP_REPORT_PATH, {}, "anonymous")
    return None


def flush(client: Any, api_url: str, *, bearer: str = "", outbox: Optional[Path] = None,
          device_path: Optional[Path] = None, max_items: int = 20,
          settings: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Upload queued reports, oldest first. Returns counts and why it stopped.

    2xx -> deleted. 400/413/415/422 -> moved to ``rejected/`` (the app will never
    accept it; retrying forever would only hide newer reports). 401/403/404, 429, 5xx
    and network errors -> kept and the pass stops (the next pass retries)."""
    out: Dict[str, Any] = {"sent": 0, "rejected": 0, "kept": 0, "stopped": ""}
    # Off = no AUTOMATIC report leaves; feedback the person sent by hand still does.
    only_feedback = not enabled(settings, env)
    box = outbox or default_outbox()
    try:
        from adk import app_device
        device = app_device.record_for(api_url, device_path)
    except Exception:  # noqa: BLE001 - no device record: other credentials still work
        device = {}
    files = _outbox_files(box)
    for path in files[:max_items]:
        claimed = path.with_name(path.name + f".sending-{os.getpid()}")
        try:
            os.replace(path, claimed)
        except OSError:
            continue  # another uploader took it
        try:
            report = json.loads(claimed.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _move_rejected(box, claimed, path.name)
            out["rejected"] += 1
            continue
        if only_feedback and report.get("kind") != "feedback":
            route = None
        else:
            route = _route(report, api_url, device, bearer)
        if route is None:
            os.replace(claimed, path)
            continue
        url, headers, _auth = route
        try:
            r = client.post(url, headers=headers, json=report)
            code = int(getattr(r, "status_code", 0) or 0)
        except Exception as exc:  # noqa: BLE001 - offline: keep, retry next pass
            os.replace(claimed, path)
            out["stopped"] = f"{type(exc).__name__}: {exc}"[:200]
            break
        if 200 <= code < 300:
            try:
                claimed.unlink()
            except OSError as exc:
                log.debug("support report: sent but could not remove %s: %s", claimed, exc)
            out["sent"] += 1
        elif code in (400, 413, 415, 422):
            _move_rejected(box, claimed, path.name)
            out["rejected"] += 1
        else:
            os.replace(claimed, path)
            out["stopped"] = f"HTTP {code}"
            break
    out["kept"] = len(_outbox_files(box))
    return out


def _move_rejected(box: Path, claimed: Path, name: str) -> None:
    dest = box / "rejected"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        os.replace(claimed, dest / name)
    except OSError:
        try:
            claimed.unlink()
        except OSError as exc:
            log.debug("support report: could not drop rejected %s: %s", claimed, exc)


class SupportFlusher:
    """:func:`flush` on a clock: :meth:`tick` uploads at most once per interval."""

    def __init__(self, api_url: str, *, outbox: Optional[Path] = None,
                 device_path: Optional[Path] = None,
                 clock: Callable[[], float] = time.monotonic,
                 interval: float = FLUSH_INTERVAL_S) -> None:
        self.api_url = api_url
        self.outbox = outbox
        self.device_path = device_path
        self.clock = clock
        self.interval = interval
        self._next = 0.0
        self.last: Dict[str, Any] = {}

    def tick(self, client: Any, bearer: str) -> Optional[Dict[str, Any]]:
        now = self.clock()
        if now < self._next:
            return None
        self._next = now + self.interval
        if not _outbox_files(self.outbox or default_outbox()):
            return None
        self.last = flush(client, self.api_url, bearer=bearer, outbox=self.outbox,
                          device_path=self.device_path)
        return self.last


# ── CLI: adk feedback send|flush|status|on|off ──────────────────────────────────


def register(sub: Any) -> None:
    """Add ``adk feedback`` to an argparse subparsers object."""
    p = sub.add_parser("feedback", help="Send feedback or logs to your company app's support")
    s = p.add_subparsers(dest="support_action")
    send = s.add_parser("send", help="Send a message (and recent logs) to support")
    send.add_argument("message", nargs="?", default="", help="What happened")
    send.add_argument("--url", default="", help="Company app API URL (default: the one "
                      "this PC is listed in)")
    send.add_argument("--log-file", default="", help="Attach the last lines of this file")
    send.add_argument("--lines", type=int, default=100, help="How many log lines to attach")
    fl = s.add_parser("flush", help="Upload queued reports now")
    fl.add_argument("--url", default="", help="Company app API URL")
    s.add_parser("status", help="Show whether reports are on and what is queued")
    s.add_parser("on", help="Turn automatic crash/error reports on")
    s.add_parser("off", help="Turn automatic crash/error reports off")


def _known_api_url(explicit: str) -> str:
    if explicit:
        return explicit.strip().rstrip("/")
    try:
        from adk import app_device
        keys = [k for k, v in app_device.load().items() if isinstance(v, dict)]
    except Exception:  # noqa: BLE001
        keys = []
    return keys[0] if len(keys) == 1 else ""


def _tail(path: str, lines: int) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-max(1, min(lines, MAX_LOG_LINES)):]
    except OSError as exc:
        return [f"(could not read {path}: {type(exc).__name__})"]


def cmd_support(args: Any) -> int:
    action = getattr(args, "support_action", None)
    if action == "on":
        print(f"  Support reports are ON ({set_enabled(True)})")
        return 0
    if action == "off":
        print(f"  Support reports are OFF ({set_enabled(False)}). Feedback you send by hand "
              "still goes through.")
        return 0
    if action == "status":
        files = _outbox_files(default_outbox())
        print(f"  Automatic reports: {'on' if enabled() else 'off'}")
        print(f"  Queued: {len(files)} in {default_outbox()}")
        return 0
    if action not in ("send", "flush"):
        print("usage: adk feedback send|flush|status|on|off")
        return 2
    api_url = _known_api_url(getattr(args, "url", ""))
    if action == "send":
        message = (args.message or "").strip()
        if not message:
            message = input("What happened? ").strip()
        if not message:
            print("  [x] nothing to send")
            return 2
        logs = _tail(args.log_file, args.lines) if args.log_file else []
        report = build_report("feedback", "awdk", message[:120], message=message, logs=logs)
        # Feedback the person typed is sent even with automatic reports off.
        if enqueue(report, env={ENV_OPT_OUT: "1"}) is None:
            print("  [x] the report could not be scrubbed, so it was not kept")
            return 1
    if not api_url:
        print("  Queued. It is sent the next time `adk tenant watch` runs "
              "(or pass --url https://<your app API>).")
        return 0
    import httpx

    from adk._tls import tls_verify
    from adk.config import load_saved_config
    from adk.tenant_watch import login_token

    with httpx.Client(timeout=30.0, verify=tls_verify()) as client:
        res = flush(client, api_url, bearer=login_token(load_saved_config()),
                    env={ENV_OPT_OUT: "1"} if action == "send" else None)
    print(f"  Sent {res['sent']}, kept {res['kept']}"
          + (f" ({res['stopped']})" if res.get("stopped") else ""))
    return 0 if not res.get("stopped") else 1
