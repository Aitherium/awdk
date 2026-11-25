"""The portable platform-first gate: delegate to ``awsettings-guard``, ship no rules.

WHY: the owner's standing rule is "never hand-roll anything; find and extend the
platform lane, and if the lane is missing, build it before running the job". Memory and
prose did not stop it (violations 2026-11-11, 11-13, 11-16 twice, 11-18), so the rule is
now a PreToolUse gate. ``awsettings-guard`` (awsettings' own console script) holds THE
rules table for "is a platform lane being bypassed right now". This hook resolves it on
PATH and hands it the host's stdin unread and unparsed, passing stdout, stderr and the
exit code straight back. A machine without awsettings -> silent exit 0.

Why a delegation and not a second copy: one rules table, asserted byte-identical by
``check_platform_first.py``. A plugin copy would drift silently, and two guards that
disagree about the same command are worse than no guard.

Run BY PATH (the plugin cache holds only this directory, not the adk package):

    python -S platform_first.py               # PreToolUse(Bash|PowerShell): stdin = hook JSON
    python -S platform_first.py --self-test   # proves the delegation contract in-process

Contract: stdlib only; fail OPEN on every path (any exception -> exit 0, no output); it
adds no budget of its own (the guard's decide() is < 5 ms and the hook's timeout is
10 s). It never exits non-zero except to carry a deny the guard already made.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
from typing import Callable, Optional

#: The awsettings console script that owns the rules. Never a path: PATH is the lane.
GUARD = "awsettings-guard"

#: The hook's own timeout (portable.json declares 10 s). Leave headroom for teardown.
TIMEOUT_S = 9.0

_CREATE_NO_WINDOW = 0x08000000


def _no_window() -> dict:
    """Keep a console-script child from flashing a window on Windows."""
    return {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}


def resolve(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """The guard's path on PATH, or None. Never raises."""
    try:
        return which(GUARD)
    except Exception:  # noqa: BLE001 - resolution must never fail the tool call
        return None


def delegate(raw: bytes, guard: str,
             runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
             ) -> "tuple[int, bytes, bytes]":
    """Run the guard with ``raw`` on stdin; return (exit code, stdout, stderr).

    The payload is passed through untouched: no JSON parse, no command rewrite, no
    environment change. Any failure (guard missing, spawn error, timeout) is a silent
    allow -- a crashed guard must never wedge a tool call.
    """
    try:
        done = runner([guard], input=raw, capture_output=True, timeout=TIMEOUT_S,
                      **_no_window())
    except Exception:  # noqa: BLE001 - fail open
        return 0, b"", b""
    code = done.returncode if isinstance(done.returncode, int) else 0
    return code, done.stdout or b"", done.stderr or b""


def main(argv: Optional[list[str]] = None,
         stdin: Optional[bytes] = None,
         stdout: Optional[io.BufferedWriter] = None,
         stderr: Optional[io.BufferedWriter] = None,
         which: Callable[[str], Optional[str]] = shutil.which,
         runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> int:
    """Hook entry point. Always returns an int; exits non-zero only to carry a deny."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        return self_test()
    try:
        stdin = sys.stdin.buffer if stdin is None else stdin
        stdout = sys.stdout.buffer if stdout is None else stdout
        stderr = sys.stderr.buffer if stderr is None else stderr
        raw = stdin.read()
        guard = resolve(which)
        if guard is None:
            return 0  # no awsettings on this machine: nothing to delegate to
        code, out, err = delegate(raw, guard, runner)
        if out:
            stdout.write(out)
        if err:
            stderr.write(err)
        # A closed stdout/stderr pipe is the host's business, not a tool failure.
        with contextlib.suppress(OSError):
            stdout.flush()
        with contextlib.suppress(OSError):
            stderr.flush()
        return 0 if code is None else code
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        return 0


def self_test() -> int:
    """Prove the contract without touching PATH or spawning anything.

    The three shapes that matter: no guard -> silent allow and no spawn; a guard ->
    exact byte passthrough with its exit code; a spawn failure -> silent allow.
    """
    failures: list[str] = []

    class Done:
        def __init__(self, code: int, out: bytes = b"", err: bytes = b"") -> None:
            self.returncode, self.stdout, self.stderr = code, out, err

    deny = b'{"hookSpecificOutput":{"permissionDecision":"deny",' \
           b'"permissionDecisionReason":"the guard denied this"}}'
    seen: list[tuple[list[str], dict]] = []

    def runner(cmd, **kw):  # noqa: ANN001 - mirrors subprocess.run
        seen.append((list(cmd), kw))
        return Done(2, deny, b"denied by the guard")

    # 1. No guard on PATH: silent allow, no child.
    out, err = io.BytesIO(), io.BytesIO()
    code = main([], stdin=io.BytesIO(b"{}"), stdout=out, stderr=err,
                which=lambda _n: None, runner=runner)
    if (code, out.getvalue(), err.getvalue(), seen) != (0, b"", b"", []):
        failures.append("no-guard shape did not allow silently")

    # 2. Guard present: same stdin bytes in, stdout/stderr/exit passed through.
    out, err = io.BytesIO(), io.BytesIO()
    payload = b'{"tool_name":"Bash","tool_input":{"command":"x"}}'
    code = main([], stdin=io.BytesIO(payload), stdout=out, stderr=err,
                which=lambda _n: "/bin/awsettings-guard", runner=runner)
    if code != 2 or out.getvalue() != deny or err.getvalue() != b"denied by the guard":
        failures.append("delegation did not pass exit/stdout/stderr through")
    if not seen or seen[-1][0] != ["/bin/awsettings-guard"] \
            or seen[-1][1].get("input") != payload:
        failures.append("delegation did not hand the guard the raw payload")

    # 3. The guard cannot be spawned: silent allow.
    def explode(cmd, **kw):  # noqa: ANN001
        raise OSError("no such guard")

    out, err = io.BytesIO(), io.BytesIO()
    code = main([], stdin=io.BytesIO(b"not json"), stdout=out, stderr=err,
                which=lambda _n: "/bin/awsettings-guard", runner=explode)
    if (code, out.getvalue(), err.getvalue()) != (0, b"", b""):
        failures.append("a failed spawn was not a silent allow")

    if failures:
        for f in failures:
            print(f"platform-first: FAIL: {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
