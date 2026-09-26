"""Run ``adk solve`` as a bounded child process and summarise the result.

The shared back half of the two host-side surfaces that are not the CLI itself:
the awsh MCP tool ``awsh_solve`` (``adk.harnesses.mcp_stdio``) and the ``/solve``
shell plugin. Both shell out to ``python -m adk.cli solve --json`` rather than
calling :func:`adk.reasoning.solve.solve` in-process, for two reasons:

* the stdio MCP server handles one request at a time, and a solve can run for
  minutes -- in-process it would also pull numpy and the vendored core into a
  server that is stdlib-only by design;
* the CLI owns env and model construction (``--env``, ``--tier``/``--backend``);
  a second construction path here would drift from it.

Stdlib only. Every limit is clamped (``MAX_*``) so a caller cannot turn a
"bounded" run into an unbounded one, and the child is killed at
``wall_s + GRACE_S``.

Exit codes follow the CLI: 0 won, 1 ran and did not win, 2 could not run (dead
backend, missing extra, or an adk without the ``solve`` verb). A timeout is 2.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "ENVS",
    "build_solve_argv",
    "run_bounded_solve",
    "render_summary",
    "MAX_CALLS",
    "MAX_ACTIONS",
    "MAX_WALL_S",
]

ENVS = ("toy", "arc")
MAX_CALLS = 60
MAX_ACTIONS = 500
MAX_WALL_S = 900.0
GRACE_S = 60.0
DEFAULTS = {"max_calls": 20, "max_actions": 100, "wall_s": 300.0}

#: Fields of ``SolveResult`` kept in the summary; anything else the CLI prints is dropped
#: so an MCP reply stays small (the hypothesis list is reduced to counts).
_KEEP = (
    "finish_reason",
    "won",
    "levels",
    "level_actions",
    "actions",
    "turns",
    "llm_calls",
    "tokens",
    "wall_s",
    "calibration",
    "strategy_trace",
    "log_path",
    "error",
    "exit_code",
)


def _clamp(value: Any, default: float, hi: float, *, integer: bool) -> float:
    try:
        v = float(value) if value is not None and value != "" else float(default)
    except (TypeError, ValueError):
        raise ValueError("expected a number, got %r" % (value,)) from None
    if v <= 0:
        raise ValueError("expected a positive number, got %r" % (value,))
    v = min(v, hi)
    return float(int(v)) if integer else v


def build_solve_argv(args: Dict[str, Any], python: Optional[str] = None) -> List[str]:
    """The exact child argv for one bounded run. Raises ValueError on bad input."""
    env = str(args.get("env") or "toy").lower()
    if env not in ENVS:
        raise ValueError("env must be one of %s, got %r" % (", ".join(ENVS), env))
    calls = _clamp(args.get("max_calls"), DEFAULTS["max_calls"], MAX_CALLS, integer=True)
    acts = _clamp(args.get("max_actions"), DEFAULTS["max_actions"], MAX_ACTIONS, integer=True)
    wall = _clamp(args.get("wall_s"), DEFAULTS["wall_s"], MAX_WALL_S, integer=False)
    argv = [
        python or sys.executable,
        "-m",
        "adk.cli",
        "solve",
        "--env",
        env,
        "--max-calls",
        str(int(calls)),
        "--max-actions",
        str(int(acts)),
        "--wall-s",
        "%g" % wall,
        "--events",
        "none",
        "--json",
    ]
    for key, flag in (
        ("game", "--game"),
        ("env_dir", "--env-dir"),
        ("tier", "--tier"),
        ("backend", "--backend"),
        ("model", "--model"),
        ("run_dir", "--run-dir"),
    ):
        val = args.get(key)
        if val not in (None, ""):
            argv += [flag, str(val)]
    return argv


def _last_json_object(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            return doc
    try:  # a pretty-printed (multi-line) object as the whole of stdout
        doc = json.loads(text)
        return doc if isinstance(doc, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _summarise(doc: Dict[str, Any], exit_code: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {k: doc[k] for k in _KEEP if k in doc}
    hyps = doc.get("hypotheses")
    if isinstance(hyps, list):
        counts: Dict[str, int] = {}
        for h in hyps:
            status = h.get("status", "?") if isinstance(h, dict) else "?"
            counts[status] = counts.get(status, 0) + 1
        out["hypotheses"] = counts
    out["exit_code"] = exit_code
    return out


def _tail(text: str, n: int = 12) -> str:
    return "\n".join((text or "").splitlines()[-n:])


def run_bounded_solve(
    args: Dict[str, Any],
    *,
    runner: Callable[..., Any] = subprocess.run,
    python: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one bounded ``adk solve`` and return its summary. Never raises."""
    try:
        argv = build_solve_argv(args, python=python)
    except ValueError as exc:
        return {"exit_code": 2, "error": "bad arguments: %s" % exc}
    wall = float(argv[argv.index("--wall-s") + 1])
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=wall + GRACE_S)
    except subprocess.TimeoutExpired:
        return {
            "exit_code": 2,
            "error": "adk solve did not finish within %gs (wall_s + %gs grace); "
            "killed" % (wall + GRACE_S, GRACE_S),
            "argv": argv[1:],
        }
    except OSError as exc:
        return {"exit_code": 2, "error": "cannot start %s: %s" % (argv[0], exc)}
    rc = int(proc.returncode)
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    if "invalid choice: 'solve'" in stderr:
        return {
            "exit_code": 2,
            "error": "this adk has no `solve` verb (adk.cli solve); "
            "upgrade awdk to a build with the reasoning CLI",
            "argv": argv[1:],
        }
    if "No module named adk" in stderr:
        return {
            "exit_code": 2,
            "error": "adk is not installed for %s: pip install 'aither-adk[reason]'" % argv[0],
        }
    doc = _last_json_object(stdout)
    if doc is None:
        return {
            "exit_code": rc if rc else 2,
            "error": "adk solve printed no JSON result",
            "stdout_tail": _tail(stdout),
            "stderr_tail": _tail(stderr),
        }
    out = _summarise(doc, rc)
    if rc == 2 and stderr.strip():
        out.setdefault("stderr_tail", _tail(stderr))
    return out


def render_summary(s: Dict[str, Any]) -> str:
    """One short human block for the shell plugin."""
    if s.get("error") and "finish_reason" not in s:
        return "solve: %s (exit %s)" % (s["error"], s.get("exit_code"))
    verdict = "WON" if s.get("won") else "did not win"
    lines = [
        "solve: %s -- %s (exit %s)" % (verdict, s.get("finish_reason", "?"), s.get("exit_code")),
        "  levels %s, actions %s, turns %s, llm calls %s, %.1fs"
        % (
            s.get("levels", "?"),
            s.get("actions", "?"),
            s.get("turns", "?"),
            s.get("llm_calls", "?"),
            float(s.get("wall_s") or 0.0),
        ),
    ]
    if s.get("hypotheses"):
        lines.append(
            "  hypotheses %s" % ", ".join("%s=%s" % kv for kv in sorted(s["hypotheses"].items()))
        )
    if s.get("error"):
        lines.append("  error: %s" % s["error"])
    if s.get("log_path"):
        lines.append("  log: %s" % s["log_path"])
    return "\n".join(lines)
