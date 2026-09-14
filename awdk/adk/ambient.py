"""Terminal sensor for the ambient expertise loop.

Reports what you are doing in the shell to Genesis ``/ambient/observe`` so the
agent can become an expert on it. Salience for this surface is REPETITION: a
command you run once is noise, the same command failing three times is you
being stuck, and being stuck is the moment expertise is worth having.

Design constraints that shaped this:

* **The hook runs on every prompt.** It must never add perceptible latency, so
  the reporter is spawned detached and its output discarded. A shell hook that
  makes the prompt stutter gets uninstalled within a day.
* **It must never break the shell.** Every failure path is swallowed *in the
  hook* (not in the reporter, which logs), because a traceback printed between
  every command is worse than no telemetry at all.
* **Opt-in.** Nothing is installed until ``aither ambient install`` is run, and
  ``AITHER_AMBIENT=0`` disables reporting without uninstalling.

Secrets are redacted server-side by ``lib.cognitive.ambient_expertise``, but
the obvious ones are stripped here too — a command line is the single most
likely place a live token appears, and it should not travel at all.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_GENESIS = os.getenv("AITHER_GENESIS_URL", "http://localhost:8001")
TIMEOUT_SECONDS = 8

# Commands that are pure noise or carry credentials in argv.
_SKIP_COMMANDS = re.compile(
    r"^\s*(cd|ls|ll|dir|pwd|clear|cls|exit|history|echo|which|where|"
    r"aither\s+ambient)\b",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(--?(?:token|password|api[-_]?key|secret)[= ]\S+|"
    r"\b(?:sk-|ghp_|xox[baprs]-|AKIA)\S+)"
)


def _redact(text: str) -> str:
    return _INLINE_SECRET.sub("[REDACTED]", text or "")


def _auth_headers() -> Dict[str, str]:
    """Bearer token from the adk auth store, if the user is logged in."""
    headers = {"Content-Type": "application/json"}
    try:
        from adk.auth import AuthStore

        profile = AuthStore().get_active_profile() or {}
        token = profile.get("access_token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception as e:  # noqa: BLE001 - an unauthenticated shell still works
        # Not fatal, but say so: without a token every observe 401s, and a
        # sensor that 401s forever is indistinguishable from a quiet one.
        print(f"[ambient] no auth token ({e}); observations will be rejected",
              file=sys.stderr)
    return headers


def enabled() -> bool:
    return os.getenv("AITHER_AMBIENT", "1").strip().lower() not in ("0", "false", "no")


def observe(
    surface: str,
    locator: str,
    title: str = "",
    excerpt: str = "",
    dwell_ms: int = 0,
    exit_code: Optional[int] = None,
    genesis_url: Optional[str] = None,
) -> Dict[str, Any]:
    """POST one observation. Returns the server verdict (or an error dict)."""
    url = f"{(genesis_url or DEFAULT_GENESIS).rstrip('/')}/ambient/observe"
    body = json.dumps(
        {
            "surface": surface,
            "locator": _redact(locator),
            "title": title,
            "excerpt": _redact(excerpt),
            "dwell_ms": dwell_ms,
            "exit_code": exit_code,
        }
    ).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=_auth_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Report the status. A 401 here means the shell is not logged in, and
        # silently treating that as "nothing to research" is how a dead sensor
        # passes for a quiet one.
        return {"error": f"HTTP {e.code}", "detail": e.read()[:200].decode("utf-8", "replace")}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def observe_command(
    command: str,
    exit_code: int,
    cwd: str = "",
    error_text: str = "",
    genesis_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Report one finished shell command."""
    if not command.strip() or _SKIP_COMMANDS.match(command):
        return {"skipped": True, "reason": "noise command"}

    # Build enough context for a subject to be nameable. A bare command is
    # ~20 chars, well under any excerpt floor, so the surrounding state is
    # what makes this observation usable at all.
    parts = [f"$ {command.strip()}", f"exit code: {exit_code}"]
    if cwd:
        parts.append(f"cwd: {cwd}")
        marker = Path(cwd) / "package.json"
        pyproject = Path(cwd) / "pyproject.toml"
        if marker.exists():
            parts.append("project type: node")
        elif pyproject.exists():
            parts.append("project type: python")
    if error_text.strip():
        parts.append("error output:\n" + error_text.strip()[:2000])

    return observe(
        surface="terminal",
        locator=command.strip(),
        title=command.strip()[:80],
        excerpt="\n".join(parts),
        exit_code=exit_code,
        genesis_url=genesis_url,
    )


def brief(locator: str, surface: str = "terminal", genesis_url: Optional[str] = None):
    """Ask what the agent already knows about this."""
    from urllib.parse import urlencode

    base = (genesis_url or DEFAULT_GENESIS).rstrip("/")
    qs = urlencode({"locator": locator, "surface": surface})
    req = urllib.request.Request(f"{base}/ambient/brief?{qs}", headers=_auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": str(e)}


def report_detached(command: str, exit_code: int, cwd: str, error_text: str = "") -> None:
    """Spawn the reporter without waiting for it.

    The shell hook calls this. Anything that blocks here is felt on every
    prompt, so the child is fully detached and its streams are discarded.
    """
    if not enabled():
        return
    payload = json.dumps(
        {"command": command, "exit_code": exit_code, "cwd": cwd, "error": error_text}
    )
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    try:
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "adk.ambient", "--report", payload],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
    except Exception as e:  # noqa: BLE001 - never break the user's shell
        # Deliberately not raised: a traceback printed between every command
        # is worse than lost telemetry. Written to stderr so it is at least
        # discoverable when someone asks why nothing is being learned.
        print(f"[ambient] could not spawn reporter: {e}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════
# SHELL HOOKS
# ══════════════════════════════════════════════════════════════════════════

POWERSHELL_HOOK = r"""
# >>> aither ambient >>>
# Reports finished commands to the AitherOS ambient expertise loop.
# Remove this block (or set $env:AITHER_AMBIENT = "0") to disable.
if (-not (Test-Path Function:\__aither_prev_prompt)) {
    if (Test-Path Function:\prompt) {
        Rename-Item Function:\prompt __aither_prev_prompt -Force `
            -ErrorAction SilentlyContinue
    }
}
function prompt {
    $__code = if ($global:LASTEXITCODE -ne $null) { $global:LASTEXITCODE } else { 0 }
    try {
        $__last = (Get-History -Count 1 -ErrorAction SilentlyContinue)
        if ($__last -and $__last.Id -ne $global:__aitherLastHistoryId) {
            $global:__aitherLastHistoryId = $__last.Id
            $__err = ""
            if ($__code -ne 0 -and $Error.Count -gt 0) { $__err = "$($Error[0])" }
            $__payload = @{
                command = $__last.CommandLine
                exit_code = $__code
                cwd = (Get-Location).Path
                error = $__err
            } | ConvertTo-Json -Compress
            $__args = @("ambient", "report", "--payload", $__payload)
            Start-Process -FilePath "aither" -ArgumentList $__args `
                -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
        }
    } catch { }
    $global:LASTEXITCODE = $__code
    if (Test-Path Function:\__aither_prev_prompt) { __aither_prev_prompt }
    else { "PS $($executionContext.SessionState.Path.CurrentLocation)> " }
}
# <<< aither ambient <<<
"""

BASH_HOOK = r"""
# >>> aither ambient >>>
# Reports finished commands to the AitherOS ambient expertise loop.
# Remove this block (or export AITHER_AMBIENT=0) to disable.
__aither_ambient_report() {
  local code=$?
  local cmd
  cmd=$(HISTTIMEFORMAT= history 1 | sed 's/^ *[0-9]* *//')
  if [ -n "$cmd" ] && [ "$cmd" != "$__AITHER_LAST_CMD" ]; then
    __AITHER_LAST_CMD="$cmd"
    # Passed via env, not argv: a command line can contain quotes, newlines
    # and $ — building JSON for it in shell is where this kind of hook breaks.
    ( AITHER_RC_CMD="$cmd" AITHER_RC_CODE="$code" AITHER_RC_CWD="$PWD" \
        python -m adk.ambient --report-env >/dev/null 2>&1 & ) 2>/dev/null
  fi
  return $code
}
case "$PROMPT_COMMAND" in
  *__aither_ambient_report*) ;;
  *) PROMPT_COMMAND="__aither_ambient_report${PROMPT_COMMAND:+; $PROMPT_COMMAND}" ;;
esac
# <<< aither ambient <<<
"""

_MARKER_START = "# >>> aither ambient >>>"
_MARKER_END = "# <<< aither ambient <<<"


def profile_path(shell: str) -> Path:
    if shell == "powershell":
        # Matches pwsh 7's $PROFILE on both platforms.
        if os.name == "nt":
            base = Path.home() / "Documents" / "PowerShell"
        else:
            base = Path.home() / ".config" / "powershell"
        return base / "Microsoft.PowerShell_profile.ps1"
    return Path.home() / ".bashrc"


def install_hook(shell: str) -> str:
    """Idempotently add the hook to the user's shell profile."""
    path = profile_path(shell)
    snippet = POWERSHELL_HOOK if shell == "powershell" else BASH_HOOK
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _MARKER_START in existing:
        return f"already installed in {path}"
    path.write_text(existing.rstrip() + "\n\n" + snippet.strip() + "\n", encoding="utf-8")
    return f"installed in {path} (restart your shell)"


def uninstall_hook(shell: str) -> str:
    path = profile_path(shell)
    if not path.exists():
        return f"nothing to remove ({path} does not exist)"
    text = path.read_text(encoding="utf-8")
    if _MARKER_START not in text:
        return f"not installed in {path}"
    start = text.index(_MARKER_START)
    end = text.index(_MARKER_END) + len(_MARKER_END)
    path.write_text((text[:start].rstrip() + "\n" + text[end:].lstrip()), encoding="utf-8")
    return f"removed from {path}"


def _main(argv: list) -> int:
    """Entry point for the detached reporter (``python -m adk.ambient``)."""
    if argv and argv[0] == "--report-env":
        # The bash hook passes the command via env so shell quoting can never
        # corrupt it.
        observe_command(
            command=os.getenv("AITHER_RC_CMD", ""),
            exit_code=int(os.getenv("AITHER_RC_CODE", "0") or 0),
            cwd=os.getenv("AITHER_RC_CWD", ""),
            error_text=os.getenv("AITHER_RC_ERR", ""),
        )
        return 0
    if len(argv) >= 2 and argv[0] == "--report":
        try:
            data = json.loads(argv[1])
        except json.JSONDecodeError:
            return 1
        observe_command(
            command=data.get("command", ""),
            exit_code=int(data.get("exit_code", 0)),
            cwd=data.get("cwd", ""),
            error_text=data.get("error", ""),
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
