"""``adk tenant watch`` -- a folder on this PC that feeds a company app's knowledge base.

A customer cannot point their company app at a folder on their own PC: the hosted
backend cannot see ``C:\\``, and ``adk ingest`` / ``adk sync`` go to the agent's own
graph or AitherDrive, never to the app's document store. This
closes that gap with the smallest gesture there is: drop a file into the app's
Inbox folder and it is uploaded to ``<app>/api/documents/upload`` with the
``adk login`` identity (the app's AuthMiddleware validates that bearer against
Identity and refuses a token from another tenant).

    adk tenant watch --app acme --url https://acme-api.example   # watch ~/ACME Inbox
    adk tenant watch --app acme --url https://... --once --json  # one pass, JSON result

There is no default app and no built-in API table: the tenant's own setup step
(AitherZero 3267) passes ``--app`` and ``--url`` from that tenant's manifest, so one
tenant's watcher can never send its files to another tenant's API.

Polling, not watchdog: the folder is small and human-paced, a poll needs no
optional dependency, and it cannot miss an event the OS dropped while the agent
was asleep. A per-app state file records each file's (size, mtime) signature and
outcome, so a file is sent once and a CHANGED file is sent again. A failed send
(network, 5xx) is retried with exponential backoff and given up after MAX_TRIES
until the file changes; a file the app cannot read (400/413/415/422) is not retried
at all. A hidden logon watcher must never resend one bad file every few seconds.

A file is sent only once its signature holds still across a short re-check, because
an Explorer copy keeps the SOURCE file's old mtime while its size is still growing.
An edited file the watcher itself sent before is REPLACED (the app refuses a second
upload under the same name with 409, so the watcher deletes the copy it made and
sends the new one); a 409 for a name the watcher never sent is left alone.

The watcher outlives anything one file can do to it: a file moved or deleted
mid-pass, a state file held open by an indexer or antivirus, an unexpected error in
one pass. Each becomes a retry, never an exit, because the watcher runs hidden at
logon and a dead one silently stops the Inbox until the next logon.

A watcher started at logon outlives its login. When the app refuses the bearer
(401/403), or there is no login yet, a continuous watch does NOT exit. It leaves a
plain-text notice in the Inbox (the folder the user is looking at), re-reads the
saved ``adk login`` on every pass, and resumes by itself once a new token appears.
Only ``--once`` and an explicit ``--token`` (which cannot change under it) still
stop with exit 3.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

log = logging.getLogger("adk.tenant_watch")

#: The host app's own list of uploadable types (its RAG supported suffixes). A
#: file outside it would only earn a 415, so it is skipped without a request.
SUPPORTED_SUFFIXES = (".pdf", ".docx", ".xlsx", ".txt", ".md", ".csv",
                      ".png", ".jpg", ".jpeg", ".webp")

#: First retry delay after a failed send (network, 5xx); doubles per failure.
RETRY_BASE_S = 30.0

#: Longest delay between two retries of one file.
RETRY_MAX_S = 3600.0

#: Failed sends the app ANSWERED (5xx) before one unchanged file may be given up
#: ("failed") until it changes. A send that got no answer at all (offline PC, DNS,
#: refused connection, timeout) never counts: it is retried every RETRY_MAX_S at most,
#: for as long as it takes, so a laptop that was offline loses nothing.
MAX_TRIES = 8

#: And the app must have kept failing that file for at least this long. An outage of
#: the app (a proxy answering 502/530 for hours) must not cost the user their files;
#: a file the app has answered 5xx to for three days is genuinely stuck.
GIVE_UP_AFTER_S = 3 * 86400.0

#: Seconds a file must sit unmodified before it is sent (a copy still in progress).
DEFAULT_SETTLE_S = 2.0

#: Seconds between the two looks at a candidate's (size, mtime) inside one pass. An
#: Explorer copy keeps the source's old mtime, so mtime alone would send a file that
#: is still growing; a signature that moves between the two looks waits a pass.
DEFAULT_RECHECK_S = 1.0

UPLOAD_PATH = "/api/documents/upload"
DOCUMENT_PATH = "/api/documents/{doc_id}"

#: Seconds before an UNCHANGED refused token is tried again (a transient 403 heals).
AUTH_RETRY_S = 600.0


def notice_name(app: str, kind: str = "login") -> str:
    """The file a refused watcher leaves in the Inbox. It is never uploaded.

    ``kind`` ``"login"`` (``adk login`` fixes it) or ``"role"`` (only a company admin
    fixes it: the name the user reads in Explorer must not say "sign in again")."""
    if kind == "role":
        return f"ASK YOUR ADMIN - {app.upper()} is not receiving files.txt"
    return f"SIGN IN AGAIN - {app.upper()} is not receiving files.txt"


def problems_name(app: str) -> str:
    """The Inbox note that lists files the app would not take. It is never uploaded."""
    return f"NOT SENT - {app.upper()} could not take some files.txt"


def notice_names(app: str) -> tuple:
    """Every file name the watcher itself writes into the Inbox (never uploaded)."""
    return (notice_name(app), notice_name(app, "role"), problems_name(app))


class AuthRefusedError(RuntimeError):
    """The app answered 401/403: the saved login is missing, expired or another tenant's.

    ``kind`` is ``"login"`` (``adk login`` fixes it) or ``"role"`` (the login is fine
    but the user's role may not upload: only a company admin fixes that, so telling the
    user to sign in again would send them in circles)."""

    def __init__(self, msg: str, *, kind: str = "login", detail: str = ""):
        super().__init__(msg)
        self.kind = kind
        self.detail = detail


def _is_role_refusal(detail: str) -> bool:
    d = detail.lower()
    return "role" in d or "cannot write" in d or "permission" in d


def default_inbox(app: str) -> Path:
    """``~/<APP> Inbox`` -- the folder the setup step creates and shortcuts to."""
    return Path.home() / f"{app.upper()} Inbox"


def default_state_path(app: str) -> Path:
    return Path.home() / ".aither" / f"tenant-watch-{app}.json"


def resolve_api_url(app: str, explicit: str = "") -> str:
    """``--url``, else ``AITHER_<APP>_API_URL``. No built-in table: the tenant's setup
    step passes the URL from that tenant's own manifest."""
    env = f"AITHER_{app.upper().replace('-', '_')}_API_URL"
    url = (explicit or os.environ.get(env, "")).strip().rstrip("/")
    if not url:
        raise ValueError(f"no API URL for app '{app}'; pass --url https://...")
    if not url.startswith(("https://", "http://")) or url.endswith(":/"):
        raise ValueError(f"API URL {url!r} for app '{app}' is not an http(s) URL")
    return url


def retry_delay(tries: int) -> float:
    """Seconds before failed send number ``tries`` (1-based) is tried again."""
    return min(RETRY_MAX_S, RETRY_BASE_S * (2 ** max(0, tries - 1)))


def login_token(saved: Dict[str, Any]) -> str:
    """The ``adk login`` Identity token (access_token first: an aither_sk api key is not
    an Identity session and the app would refuse it)."""
    return str(saved.get("access_token") or saved.get("api_key") or "").strip()


def _load_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: Dict[str, Any]) -> bool:
    """Write the state atomically. False (never raised) when the disk refuses: on
    Windows an indexer or antivirus holding the file fails os.replace, and losing one
    write costs only a resend that the app answers with 409."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _sig(p: Path) -> Optional[List[int]]:
    """(size, mtime_ns), or None when the file is gone or unreadable right now."""
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def pending_files(inbox: Path, state: Dict[str, Any], *, now: float,
                  settle_s: float = DEFAULT_SETTLE_S,
                  skip: tuple = ()) -> List[Path]:
    """Supported files in ``inbox`` (top level) that are new or changed since last sent,
    and have stopped changing for ``settle_s`` seconds. Hidden/temp files and the
    names in ``skip`` (the watcher's own notice) are ignored."""
    out: List[Path] = []
    try:
        entries = sorted(inbox.iterdir())
    except OSError:
        return out
    for p in entries:
        if not p.is_file() or p.name.startswith((".", "~$")) or p.name in skip:
            continue
        if p.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        try:
            st = p.stat()
        except OSError:  # moved or deleted since iterdir(): nothing to send
            continue
        if now - st.st_mtime < settle_s:
            continue
        rec = state.get(p.name) or {}
        if rec.get("sig") == [st.st_size, st.st_mtime_ns]:
            # Same bytes as last time. Only a failed send comes back, and only once
            # its backoff has passed; every other outcome waits for the file to change.
            if rec.get("status") != "error" or now < float(rec.get("retry_at") or 0):
                continue
        out.append(p)
    return out


def upload_one(client: Any, api_url: str, token: str, path: Path) -> Dict[str, Any]:
    """POST one file. Returns ``{"file", "status", "detail"}`` where status is
    ``learned`` (200), ``exists`` (409: the app already holds that name),
    ``refused`` (400/413/415/422: the app cannot use this file; retrying the same bytes
    cannot succeed), or ``error`` (network, 5xx: retried with backoff).
    Raises AuthRefusedError on 401/403."""
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        r = client.post(f"{api_url}{UPLOAD_PATH}",
                        headers={"Authorization": f"Bearer {token}"},
                        files={"file": (path.name, fh, mime)},
                        data={"doc_type": "other"})
    code = r.status_code
    try:
        body = r.json()
    except ValueError:
        body = {}
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("warning") or body.get("detail") or "")
    if code == 403 and _is_role_refusal(detail):
        raise AuthRefusedError(
            f"{api_url} accepts your login but your role may not add documents"
            f" ({detail}). Ask your company admin for write access",
            kind="role", detail=detail)
    if code in (401, 403):
        raise AuthRefusedError(f"{api_url} refused the saved login ({code}). Run: adk login",
                          detail=detail)
    if 200 <= code < 300:
        # The host app's upload response names it doc_id; other kits answer id.
        doc_id = (str(body.get("doc_id") or body.get("id") or "")
                  if isinstance(body, dict) else "")
        return {"file": path.name, "status": "learned", "detail": detail, "doc_id": doc_id}
    if code == 409:
        return {"file": path.name, "status": "exists", "detail": detail}
    if code in (400, 413, 415, 422):
        return {"file": path.name, "status": "refused", "detail": detail or f"HTTP {code}"}
    return {"file": path.name, "status": "error", "detail": detail or f"HTTP {code}"}


def delete_doc(client: Any, api_url: str, token: str, doc_id: str) -> bool:
    """Remove a document the watcher itself uploaded. True when it is gone (2xx, or
    404: already deleted in the app). Any other answer, including 403 for a member
    who is not an admin, leaves it and returns False."""
    r = client.delete(f"{api_url}{DOCUMENT_PATH.format(doc_id=doc_id)}",
                      headers={"Authorization": f"Bearer {token}"})
    return 200 <= r.status_code < 300 or r.status_code == 404


def _send(client: Any, api_url: str, token: str, p: Path,
          rec: Dict[str, Any]) -> Dict[str, Any]:
    """Upload ``p``; when the app already holds that name AND this watcher put it
    there (a doc id is on record), delete that copy and upload the edited file."""
    res = upload_one(client, api_url, token, p)
    old_id = str(rec.get("doc_id") or "")
    if res["status"] != "exists" or not old_id:
        return res
    if not delete_doc(client, api_url, token, old_id):
        res["detail"] = res["detail"] or "the app kept the earlier version"
        return res
    again = upload_one(client, api_url, token, p)
    if again["status"] == "learned":
        again["status"] = "replaced"
    return again


def watch_once(inbox: Path, api_url: str, token: str, state_path: Path, *,
               client: Any, now: Optional[float] = None,
               settle_s: float = DEFAULT_SETTLE_S,
               recheck_s: Optional[float] = None,
               sleep: Callable[[float], None] = time.sleep,
               skip: tuple = ()) -> List[Dict[str, Any]]:
    """One pass: send every pending file, record the outcome, return the results.

    Every outcome is pinned to the file's signature. ``error`` is retried after
    ``retry_delay(attempts)`` (at most RETRY_MAX_S apart). A send with no answer at all
    (network, timeout, a file locked mid-send) is retried indefinitely. Only once the app
    has ANSWERED 5xx MAX_TRIES times AND for GIVE_UP_AFTER_S does it become ``failed``,
    which, like every other outcome, is not resent until the file changes.
    Nothing one file does raises out of here; only AuthRefusedError (the whole login is
    refused) escapes."""
    state = _load_state(state_path)
    results: List[Dict[str, Any]] = []
    stamp = time.time() if now is None else now
    ready = pending_files(inbox, state, now=stamp, settle_s=settle_s, skip=skip)
    sigs_before = {p: _sig(p) for p in ready}
    wait = DEFAULT_RECHECK_S if recheck_s is None else recheck_s
    if ready and wait > 0:
        sleep(wait)
    for p in ready:
        sig = _sig(p)
        if sig is None or sig != sigs_before[p]:
            continue  # gone, or still being written: the next pass looks again
        rec = state.get(p.name) or {}
        try:
            res = _send(client, api_url, token, p, rec)
        except AuthRefusedError:
            _save_state(state_path, state)
            raise
        except Exception as exc:  # noqa: BLE001 -- network, vanished or locked file: retry
            res = {"file": p.name, "status": "error", "detail": f"{type(exc).__name__}: {exc}",
                   "answered": False}
        answered = bool(res.pop("answered", True))
        doc_id = str(res.pop("doc_id", "") or "")
        entry: Dict[str, Any] = {"sig": sig, "status": res["status"], "at": int(stamp)}
        if res["status"] == "error":
            # Same bytes failed before: count on; new bytes start over.
            again = rec.get("sig") == sig and rec.get("status") == "error"
            attempts = int(rec.get("attempts") or rec.get("tries") or 0) + 1 if again else 1
            # Only an ANSWERED failure counts toward giving up: no answer means this PC
            # or the network is down, and the file must wait it out, not be dropped.
            tries = (int(rec.get("tries") or 0) if again else 0) + (1 if answered else 0)
            first_err = float(rec.get("first_error_at") or stamp) if again else stamp
            if tries >= MAX_TRIES and stamp - first_err >= GIVE_UP_AFTER_S:
                res["status"] = entry["status"] = "failed"
            else:
                entry["retry_at"] = stamp + retry_delay(attempts)
            entry.update(tries=tries, attempts=attempts, first_error_at=first_err)
        # Keep the id of the copy WE made, so a later edit can replace it. An "exists"
        # we did not make carries no id: that document is not ours to delete. A failed
        # send leaves the earlier copy in the app, so its id stays on record.
        keep = doc_id or (str(rec.get("doc_id") or "")
                          if res["status"] in ("exists", "error", "failed") else "")
        if keep:
            entry["doc_id"] = keep
        results.append(res)
        state[p.name] = entry
    _save_state(state_path, state)
    return results


def _say(res: Dict[str, Any], app: str) -> str:
    name, status, detail = res["file"], res["status"], res.get("detail") or ""
    label = app.upper()
    if status == "learned":
        return f"  [+] {label} learned {name}" + (f" -- {detail}" if detail else "")
    if status == "replaced":
        return f"  [+] {label} learned the new version of {name}"
    if status == "exists":
        return f"  [=] {label} already has {name} (rename it to send a new version)"
    if status == "refused":
        return f"  [x] {label} cannot read {name}: {detail}"
    if status == "failed":
        return (f"  [x] {name} was not accepted for days ({detail});"
                " save it again to retry")
    return f"  [!] {name} not sent yet ({detail}); retrying later"


def _write_notice(path: Path, app: str, why: str, kind: str = "login") -> None:
    if kind == "role":
        fix = ("Fix: ask your company admin to give your account write access (a member"
               " or admin role). Signing in again will not change this.")
        when = "once your access is changed"
    else:
        fix = "Fix: open PowerShell and run   adk login"
        when = "after you sign in"
    try:
        path.write_text(
            f"{app.upper()} is not receiving the files in this folder.\n\n"
            f"Why: {why}\n\n"
            f"{fix}\n\n"
            "Nothing is lost. The files you drop here stay here and are sent a few "
            f"seconds {when}. This note then disappears.\n",
            encoding="utf-8")
    except OSError as exc:
        log.debug("cannot write the Inbox notice %s: %s", path, exc)


def _clear_notice(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:
        log.debug("cannot remove the Inbox notice %s: %s", path, exc)


def _sync_problems(inbox: Path, state_path: Path, app: str, skip: tuple) -> None:
    """Keep the "NOT SENT" note in step with the files the app would not take.

    The watcher runs in a hidden window, so a file it gave up on (``failed``) or the app
    cannot read (``refused``) must be told where the user looks: the Inbox. The note
    lists each such file still sitting there unchanged, and disappears when none is."""
    path = inbox / problems_name(app)
    state = _load_state(state_path)
    lines: List[str] = []
    for name in sorted(state):
        rec = state.get(name) or {}
        status = rec.get("status")
        if status not in ("failed", "refused") or name in skip:
            continue
        if _sig(inbox / name) != rec.get("sig"):
            continue  # gone, or saved again (it will be sent afresh)
        why = ("the app kept failing to store it" if status == "failed"
               else "the app cannot read this file")
        lines.append(f"  - {name}: {why}")
    if not lines:
        if path.exists():
            _clear_notice(path)
        return
    text = (f"{app.upper()} did not take these files from this folder:\n\n"
            + "\n".join(lines) + "\n\n"
            "To try a file again, save it again (or copy it in again). If the app cannot\n"
            "read it, save it as PDF, Word, Excel or text first. This note disappears when\n"
            "every file here has been sent.\n")
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    except OSError as exc:
        log.debug("cannot write the NOT SENT note %s: %s", path, exc)


def run(app: str, inbox: Path, api_url: str,
        token: Union[str, Callable[[], str]], *, once: bool,
        interval: float, as_json: bool, client_factory: Callable[[], Any],
        state_path: Optional[Path] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_passes: Optional[int] = None,
        device: Any = None, support: Any = None) -> int:
    """The command body: 0 ok, 1 a ``--once`` pass failed, 3 auth refused (``--once``
    or a fixed token). A continuous watch returns only on Ctrl+C or ``max_passes``.

    ``token`` is the bearer, or a callable that re-reads it (the saved ``adk login``).
    With a callable, a continuous watch survives a refused or missing login: it
    leaves a notice in the Inbox, re-reads the login every pass, and resumes when a
    new token appears (an unchanged refused token is retried every AUTH_RETRY_S).
    ``sleep``/``clock``/``max_passes`` are test seams.

    ``device`` (an :class:`adk.app_device.AppDeviceBeat`) keeps THIS PC listed and
    online in the app's device list. It beats with the device's own secret, so it
    runs on every pass even while the login is missing or refused.

    ``support`` (an :class:`adk.support_report.SupportFlusher`) uploads this PC's queued
    crash/error/setup/feedback reports (``~/.aither/support-outbox``) to the same app;
    a failed pass is queued there too."""
    inbox.mkdir(parents=True, exist_ok=True)
    state_path = state_path or default_state_path(app)
    notices = {k: inbox / notice_name(app, k) for k in ("login", "role")}
    skip = notice_names(app)
    reread = callable(token)

    def _token() -> str:
        try:
            return str((token() if callable(token) else token) or "").strip()
        except Exception:  # noqa: BLE001 -- an unreadable config is "not signed in"
            return ""

    def _refused(msg: str, kind: str = "login") -> Optional[int]:
        """Report a refused/missing login: an exit code, or None to keep waiting."""
        if once or not reread:
            if as_json:
                print(json.dumps({"ok": False, "error": msg, "error_code": 3}))
            else:
                print(f"  [x] {msg}")
            return 3
        kind = kind if kind in notices else "login"
        notice = notices[kind]
        if not notice.exists():
            # One reason at a time: a stale "ask your admin" must not sit beside a
            # fresh "sign in again" (or the reverse) and send the user the wrong way.
            for other, path in notices.items():
                if other != kind and path.exists():
                    _clear_notice(path)
            _write_notice(notice, app, msg, kind)
            if as_json:
                print(json.dumps({"ok": False, "error": msg, "error_code": 3,
                                  "waiting": True}))
            else:
                print(f"  [x] {msg}. Waiting for a new login; files stay in {inbox}.")
        return None

    if not as_json:
        print(f"  Watching {inbox} -> {api_url}{UPLOAD_PATH}"
              + ("" if once else f" every {interval:g}s (Ctrl+C to stop)"))
    refused_tok: Optional[str] = None
    refused_at = 0.0
    passes = 0
    said_device = ""
    with client_factory() as client:
        while True:
            passes += 1
            tok = _token()
            if device is not None:
                said_device = _device_tick(device, client, tok, app, as_json, said_device)
            if support is not None:
                _support_tick(support, client, tok)
            if not tok:
                rc = _refused("Not signed in. Run: adk login")
                if rc is not None:
                    return rc
            elif tok == refused_tok and clock() - refused_at < AUTH_RETRY_S:
                pass  # this exact token was just refused: wait for a new login
            else:
                try:
                    results = watch_once(inbox, api_url, tok, state_path, client=client,
                                         skip=skip)
                except AuthRefusedError as exc:
                    refused_tok, refused_at = tok, clock()
                    rc = _refused(str(exc), getattr(exc, "kind", "login"))
                    if rc is not None:
                        return rc
                except Exception as exc:  # noqa: BLE001 -- one bad pass must not end the watch
                    _queue_failure(exc)
                    msg = f"pass failed ({type(exc).__name__}: {exc}); retrying"
                    if as_json:
                        print(json.dumps({"ok": False, "error": msg}))
                    else:
                        print(f"  [!] {msg}")
                    if once:
                        return 1
                else:
                    cleared = [p for p in notices.values() if p.exists()]
                    for path in cleared:
                        _clear_notice(path)
                    if cleared and not as_json:
                        print("  [+] Signed in again; sending the Inbox.")
                    refused_tok = None
                    _sync_problems(inbox, state_path, app, skip)
                    if as_json:
                        print(json.dumps({"ok": True, "inbox": str(inbox),
                                          "results": results}))
                    else:
                        for res in results:
                            print(_say(res, app))
            if once or (max_passes is not None and passes >= max_passes):
                return 0
            try:
                sleep(interval)
            except KeyboardInterrupt:
                return 0


def _queue_failure(exc: BaseException) -> None:
    """Queue a failed pass for the app's support (never raises, honours the opt-out)."""
    try:
        from adk.support_report import report_exception
        report_exception("awdk.tenant_watch", exc, context="adk tenant watch pass failed")
    except Exception as err:  # noqa: BLE001 -- reporting must never add a failure
        log.debug("failed pass not queued for support: %s", err)


def _support_tick(support: Any, client: Any, tok: str) -> None:
    try:
        support.tick(client, tok)
    except Exception as exc:  # noqa: BLE001 -- the upload must never end the watch
        log.debug("support flush failed: %s", exc)


def _device_tick(device: Any, client: Any, tok: str, app: str, as_json: bool,
                 said: str) -> str:
    """One AppDeviceBeat tick; say it when its outcome CHANGED. Returns what was said."""
    try:
        res = device.tick(client, tok)
    except Exception as exc:  # noqa: BLE001 -- the beat must never end the watch
        res = {"ok": False, "status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    if res is None:
        return said
    key = "registered" if res.get("registered") else str(res.get("status"))
    if key == said:
        return said
    if as_json:
        print(json.dumps({"ok": bool(res.get("ok")), "device": res}))
    else:
        from adk.app_device import say
        line = say(res, app)
        if line:
            print(line)
    return "live" if key == "registered" else key


def register(sub: Any) -> None:
    """Add ``adk tenant watch`` to an argparse subparsers object."""
    tenant_p = sub.add_parser("tenant", help="Company app helpers (watch an Inbox folder)")
    tenant_sub = tenant_p.add_subparsers(dest="tenant_action")
    w = tenant_sub.add_parser(
        "watch", help="Upload every file dropped into a folder to the company app's documents")
    w.add_argument("--app", required=True,
                   help="Company app id (names the Inbox: ~/<APP> Inbox)")
    w.add_argument("--dir", default="", help="Folder to watch (default: ~/<APP> Inbox)")
    w.add_argument("--url", default="",
                   help="App API base URL (default: env AITHER_<APP>_API_URL)")
    w.add_argument("--token", default="",
                   help="Bearer to send (default: the adk login token; env AITHER_TENANT_TOKEN)")
    w.add_argument("--interval", type=float, default=5.0, help="Seconds between scans")
    w.add_argument("--once", action="store_true", help="One pass, then exit")
    w.add_argument("--json", action="store_true", help="Machine-readable output")
    w.add_argument("--no-device", action="store_true",
                   help="Do not list this PC in the app's devices or keep it online there")
    w.add_argument("--reset-device", action="store_true",
                   help="Forget this PC's device record for the app and register it again")


def cmd_tenant(args: Any) -> int:
    if getattr(args, "tenant_action", None) != "watch":
        print("usage: adk tenant watch --app APP --url https://API [--dir DIR] [--once]")
        return 2
    from adk._tls import tls_verify
    from adk.config import load_saved_config

    app = (args.app or "").strip().lower()
    try:
        api_url = resolve_api_url(app, args.url)
    except ValueError as exc:
        print(f"  [x] {exc}")
        return 2
    fixed = (args.token or os.environ.get("AITHER_TENANT_TOKEN", "")).strip()

    def _saved_login() -> str:
        # Re-read every pass: `adk login` in another window rewrites the saved config,
        # and a watcher started at logon must pick the new token up by itself.
        return login_token(load_saved_config())

    token: Union[str, Callable[[], str]] = fixed or _saved_login
    inbox = Path(args.dir).expanduser() if args.dir else default_inbox(app)

    def _client():
        import httpx
        return httpx.Client(timeout=300.0, verify=tls_verify())

    device = None
    if not getattr(args, "no_device", False):
        # This PC in the app's device list ("My Hardware"), kept online with the
        # device's own secret -- the setup page starts this watcher at logon, so a PC
        # set up there is seen without any hand-pasted heartbeat.
        from adk import app_device

        if getattr(args, "reset_device", False):
            app_device.forget(api_url)
        device = app_device.AppDeviceBeat(api_url)

    # This PC's crash/error/feedback reports go to the same app (opt-out:
    # `adk feedback off`); a crash of this watcher itself is queued first.
    from adk import support_report

    support_report.install_excepthook("awdk.tenant_watch")
    support = support_report.SupportFlusher(api_url)
    return run(app, inbox, api_url, token, once=bool(args.once),
               interval=max(1.0, float(args.interval)), as_json=bool(args.json),
               client_factory=_client, device=device, support=support)
