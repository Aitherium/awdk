"""awrise toolpack — awrise_* agent tools over the harness daemon's ``/wakes``
window (:8362).

WHY THE DAEMON AND NOT THE CLI. ``adk.wakes`` (imported here for its name/payload
validators only) is explicit that it is "the ONE place the harness daemon reads
[AWRISE_HOME]" and that mutation goes through a spawned argv, never a shell
string. This pack does not shell out to the ``awrise`` CLI or touch
``AWRISE_HOME`` itself — a remote surface (awsh MCP, Discord, this toolpack)
mutates ONLY through the daemon's entitlement-gated HTTP window, the same rule
``adk.decisions.wake_view`` states for the read side. Auth reuses
``adk.decisions.steerback.harness_url``/``harness_token`` — the SAME resolution
every other local-daemon caller in this package uses (``AITHER_HARNESS_URL`` /
``AITHER_HARNESS_HOST``+``AITHER_HARNESS_PORT``, ``AITHER_HARNESS_TOKEN`` or
``~/.aither/harness_token``) — imported, not reimplemented, so the two layers
cannot drift apart.

Two capabilities are NOT synchronous. ``POST /wakes`` (a new job) and a
``PATCH /wakes/{name}`` that changes ``command`` never spawn anything directly:
each PROPOSES a change by raising a decision card and answers 202 pending. The
job does not exist (or the command does not change) until the card is answered
— by the terminal, the DM bridge, or ``awrise_confirm`` here. Every tool that
can reach that path says so in its docstring and in the dict it returns
(``pending: true``), so a caller never reports the job as created before it is.

Every tool fails soft: a dict, never a raised exception. Names/payloads are
validated with the SAME regex/length/control-byte checks the daemon applies
(imported from ``adk.wakes``), BEFORE any HTTP request is built — the daemon
re-checks everything itself, so this is a cheap early refusal, never the only
gate.

Known gap, stated rather than hidden: the daemon currently has no
``POST /wakes/{name}/remove`` route (only ``GET/POST /wakes``,
``GET /wakes/count``, ``GET /wakes/ledger``, ``GET/PATCH /wakes/{name}``,
``POST /wakes/{name}/{enable,disable,run}`` exist as of this build).
``awrise_remove`` is still shipped, shaped exactly like every other mutation
here, so it starts working the moment that route lands; until then it returns
a clear ``{"ok": False, "error": ...}`` rather than pretending to delete
anything.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("awrise_pack")

PACK_ID = "awrise"

#: A read/write against a LOCAL daemon on the same host. Generous but bounded —
#: a tool call must never hang the calling agent turn indefinitely.
_TIMEOUT_S = 15.0


def _harness() -> tuple[str, str]:
    """(url, token) for the local harness daemon, resolved the ONE way this
    package resolves it everywhere else."""
    from adk.decisions.steerback import harness_token, harness_url

    return harness_url(), harness_token()


def _daemon_call(method: str, path: str, body: Optional[dict] = None,
                 *, timeout: float = _TIMEOUT_S) -> dict[str, Any]:
    """One HTTP call to the harness daemon. ALWAYS returns a dict carrying
    ``ok``; never raises. ``error`` names which of the daemon's own distinct
    failure shapes fired (no token / 401-403 / 404 route-absent vs job-absent /
    503 awrise-not-installed / unreachable / non-JSON) rather than flattening
    them — the same taxonomy ``adk.decisions.wake_view.fetch`` uses for reads,
    extended here to also cover the mutating verbs.
    """
    base, token = _harness()
    if not token:
        return {"ok": False, "error": "no harness token (AITHER_HARNESS_TOKEN or "
                                      "~/.aither/harness_token)"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base}{path}", data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": f"daemon unreachable: {exc}"}
    except Exception as exc:  # noqa: BLE001 — a tool call must never raise
        return {"ok": False, "error": f"daemon call failed: {type(exc).__name__}: {exc}"}

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "error": "daemon answer was not JSON", "status": status}
    if not isinstance(payload, dict):
        payload = {"result": payload}

    if 200 <= status < 300:
        payload.setdefault("ok", True)
        return payload

    detail = payload.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("error") or json.dumps(detail)
    if not detail:
        detail = f"HTTP {status}"
    out = {"ok": False, "error": str(detail), "status": status}
    if status == 404 and str(detail).strip().lower() in ("not found", ""):
        out["error"] = "daemon has no such route (restart the harness daemon?)"
    return out


def _name_path(name: str) -> str:
    return "/wakes/" + urllib.parse.quote(str(name), safe="")


def _validate_name(name: str) -> Optional[dict]:
    from adk.wakes import valid_name

    if not valid_name(name):
        return {"ok": False, "error": f"invalid wake name: {name!r}"}
    return None


# ── mutating: create / change what a job runs (raises a card; NOT synchronous) ─


def awrise_add(name: str, command: str, every: str, timeout: int = 0, cwd: str = "") -> dict:
    """Propose a NEW wake (recurring background job).

    This does NOT create the job. It raises a ``wakes-add`` decision card and
    the daemon answers 202 pending — the job exists only once the owner answers
    it ``create`` (via the terminal, the DM bridge, or ``awrise_confirm``).
    Tell the owner a card is waiting, never that the job was created.

    Args:
        name: job name, ``^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$``.
        command: the exact command awrise will run on schedule.
        every: the awrise interval grammar (e.g. "15m", "every 4h", cron-ish).
            Validated for shape here (non-empty, bounded, no control bytes);
            awrise's OWN grammar check still runs when the card is answered.
        timeout: optional per-run timeout in seconds (positive int).
        cwd: optional working directory for the job.

    Returns ``{ok, pending: true, card_id, name, ...}`` on success, or
    ``{ok: False, error}``.
    """
    from adk.wakes import MAX_COMMAND_LEN, MAX_CWD_LEN, MAX_EVERY_LEN, valid_payload

    err = _validate_name(name)
    if err:
        return err
    if not valid_payload(command, MAX_COMMAND_LEN):
        return {"ok": False, "error": "invalid or oversized command"}
    if not valid_payload(every, MAX_EVERY_LEN):
        return {"ok": False, "error": "invalid or oversized every"}
    if cwd and not valid_payload(cwd, MAX_CWD_LEN):
        return {"ok": False, "error": "invalid or oversized cwd"}
    if timeout and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0):
        return {"ok": False, "error": "timeout must be a positive integer"}

    body: dict[str, Any] = {"name": name, "command": command, "every": every}
    if timeout:
        body["timeout"] = int(timeout)
    if cwd:
        body["cwd"] = cwd
    result = _daemon_call("POST", "/wakes", body)
    if result.get("ok") and result.get("pending"):
        result.setdefault(
            "note", "a decision card is waiting for the owner; "
                    "the job does not exist yet — it will once the card is answered 'create'")
    return result


def awrise_set(name: str, command: str = "", every: str = "", timeout: int = 0,
              cwd: str = "") -> dict:
    """Change an existing wake.

    ``every``/``timeout``/``cwd`` apply immediately (no card — they change
    WHEN/WHERE the job runs, not what it runs). Supplying ``command`` instead
    PROPOSES a card (``wakes-set-command``, 202 pending) the same way
    ``awrise_add`` does, because replacing the command is the same capability
    as creating a job. Any ``every``/``timeout``/``cwd`` given alongside a
    ``command`` change is carried onto the SAME card.

    Returns ``{ok: True, ...}`` (applied) or ``{ok, pending: true, card_id}``
    (proposed) on success, or ``{ok: False, error}``.
    """
    from adk.wakes import MAX_COMMAND_LEN, MAX_CWD_LEN, MAX_EVERY_LEN, valid_payload

    err = _validate_name(name)
    if err:
        return err
    if not (command or every or timeout or cwd):
        return {"ok": False, "error": "nothing to update"}
    body: dict[str, Any] = {}
    if command:
        if not valid_payload(command, MAX_COMMAND_LEN):
            return {"ok": False, "error": "invalid or oversized command"}
        body["command"] = command
    if every:
        if not valid_payload(every, MAX_EVERY_LEN):
            return {"ok": False, "error": "invalid or oversized every"}
        body["every"] = every
    if cwd:
        if not valid_payload(cwd, MAX_CWD_LEN):
            return {"ok": False, "error": "invalid or oversized cwd"}
        body["cwd"] = cwd
    if timeout:
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            return {"ok": False, "error": "timeout must be a positive integer"}
        body["timeout"] = int(timeout)

    result = _daemon_call("PATCH", _name_path(name), body)
    if result.get("ok") and result.get("pending"):
        result.setdefault(
            "note", "a decision card is waiting for the owner; "
                    "the change is not applied yet — it will once the card is answered 'apply'")
    return result


# ── mutating: turn an already-vetted job on/off/now (synchronous) ─────────────


def awrise_enable(name: str, note: str = "") -> dict:
    """Enable a disabled wake. Applies immediately; no card."""
    err = _validate_name(name)
    if err:
        return err
    body = {"note": note} if note else None
    return _daemon_call("POST", _name_path(name) + "/enable", body)


def awrise_disable(name: str, note: str = "") -> dict:
    """Disable ('pause') an enabled wake. Applies immediately; no card."""
    err = _validate_name(name)
    if err:
        return err
    body = {"note": note} if note else None
    return _daemon_call("POST", _name_path(name) + "/disable", body)


def awrise_run_now(name: str, wait_s: float = 0.0, note: str = "") -> dict:
    """Fire one wake right now, out of schedule.

    Waits up to ``wait_s`` seconds (0..120) for it to finish before returning;
    a still-running child comes back as ``{ok: True, running: True, pid}`` —
    read the outcome afterwards with ``awrise_status(name)``.
    """
    err = _validate_name(name)
    if err:
        return err
    body: dict[str, Any] = {"note": note} if note else None
    qs = urllib.parse.urlencode({"wait_s": max(0.0, min(float(wait_s or 0.0), 120.0))})
    return _daemon_call("POST", f"{_name_path(name)}/run?{qs}", body)


def awrise_remove(name: str, note: str = "") -> dict:
    """Delete a wake.

    Spends the stricter ``wakes:create`` entitlement (removing a scheduled
    command is create-tier risk) but never raises a card — deleting is not
    command-authoring, so it (would) apply immediately.

    KNOWN GAP: the harness daemon does not expose a
    ``POST /wakes/{name}/remove`` route as of this build. This call is shaped
    to match one the moment it ships; until then it returns
    ``{ok: False, error: ...}`` — never a silent no-op, and never a claim that
    the job was deleted.
    """
    err = _validate_name(name)
    if err:
        return err
    body = {"note": note} if note else None
    result = _daemon_call("POST", _name_path(name) + "/remove", body)
    if not result.get("ok") and result.get("status") == 404:
        result.setdefault(
            "hint", "the daemon on this build may not yet expose a /remove route for wakes")
    return result


# ── the owner's answer to a pending card ──────────────────────────────────────


def awrise_confirm(card_id: str, choice: str) -> dict:
    """Answer a decision card an ``awrise_add``/``awrise_set`` call raised.

    ``choice`` must be ``"create"``/``"apply"`` (spend it) or ``"deny"`` (do
    not). Only the owner-bound channel this daemon already authorizes may
    answer for real effect; this tool is a thin wrapper over
    ``POST /decisions/{card_id}/answer``, not a second authorization path.
    """
    if not card_id or not str(card_id).strip():
        return {"ok": False, "error": "card_id is required"}
    if not choice or not str(choice).strip():
        return {"ok": False, "error": "choice is required (e.g. 'create'/'apply' or 'deny')"}
    return _daemon_call(
        "POST", f"/decisions/{urllib.parse.quote(str(card_id).strip(), safe='')}/answer",
        {"choice": str(choice).strip()},
    )


# ── read-only ──────────────────────────────────────────────────────────────────


def awrise_status(name: str = "") -> dict:
    """Every wake with its last state and clock liveness, or one job's detail
    when ``name`` is given. Read-only; never mutates anything."""
    if not name:
        return _daemon_call("GET", "/wakes")
    err = _validate_name(name)
    if err:
        return err
    return _daemon_call("GET", _name_path(name))


def awrise_explain(name: str) -> dict:
    """What one wake runs, on what schedule, and its last outcome — the same
    detail ``awrise_status(name)`` returns, framed for a "what does X do"
    question rather than a health check."""
    err = _validate_name(name)
    if err:
        return err
    data = _daemon_call("GET", _name_path(name))
    if not data.get("error"):
        data.setdefault(
            "explanation",
            f"'{name}' runs {data.get('run', data.get('command', '?'))!r} "
            f"every {data.get('every', '?')}"
            + (" (disabled)" if data.get("enabled") is False else ""),
        )
    return data


def awrise_history(name: str = "", limit: int = 20, since: str = "", event: str = "") -> dict:
    """Recent wake-clock ledger rows — what fired, when, and how it ended.

    ``since`` accepts the daemon's own relative-time grammar (e.g. "-24h");
    ``event`` filters to one ledger event (e.g. "missed", "finished").
    """
    if name:
        err = _validate_name(name)
        if err:
            return err
    params = {"limit": int(limit or 20)}
    if name:
        params["job"] = name
    if since:
        params["since"] = since
    if event:
        params["event"] = event
    qs = urllib.parse.urlencode(params)
    return _daemon_call("GET", f"/wakes/ledger?{qs}")


_TOOL_NAMES = [
    "awrise_add",
    "awrise_set",
    "awrise_enable",
    "awrise_disable",
    "awrise_run_now",
    "awrise_remove",
    "awrise_confirm",
    "awrise_status",
    "awrise_explain",
    "awrise_history",
]
