"""P0 probe: measure Claude Code's REAL permission mechanics (the awgov go/no-go).

These are LIVE tests. They cost a few cents of model calls and take minutes, so they
skip unless ``AW_LIVE=1`` is set::

    AW_LIVE=1 python -m pytest awdk/tests/live/test_cc_permission_mechanics.py -v -s

Every case runs a real ``claude -p`` subprocess against an ISOLATED config dir
(``CLAUDE_CONFIG_DIR=<tmp>/cfg``) so the developer's ``~/.claude`` hooks, autoMode
block and plugins never load, with ``--setting-sources user`` and
``--strict-mcp-config`` (no MCP servers). The model is told to run ONE harmless
command or write ONE file, then stop. We assert on the stream-json result's
``permission_denials`` list, on the hook's stdin log, and on the side effect - never
on the exit code alone.

Each test prints ``CASE N OBSERVED: {...}`` so ``-s`` shows the measurement even
when the assertion passes. Re-run after every ``claude update`` (awrise job).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AW_LIVE") != "1",
    reason="live Claude Code permission probe costs real model calls; set AW_LIVE=1",
)

# What 'claude --help' offers on the host; the local router maps this alias to a
# cheap model. Fallbacks are tried once each and the winning name is recorded.
_MODEL_CANDIDATES = ("claude-haiku-5-5", "haiku", "sonnet")
_MODEL: dict[str, str | None] = {"chosen": None}

_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL") or "http://127.0.0.1:8286"
_AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN") or "local-router-vault"
_TIMEOUT_S = 180
_CLAUDE_BIN_CACHE: dict[str, str] = {}
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# Subprocess plumbing
# --------------------------------------------------------------------------- #


def _claude_bin() -> str:
    if "path" in _CLAUDE_BIN_CACHE:
        return _CLAUDE_BIN_CACHE["path"]
    candidates: list[str] = []
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH")
    if execpath:
        candidates.append(execpath)
    for name in ("claude.exe", "claude.cmd", "claude"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if os.path.isfile(candidate):
            _CLAUDE_BIN_CACHE["path"] = candidate
            return candidate
    raise AssertionError(
        "claude CLI not found; set CLAUDE_CODE_EXECPATH or put it on PATH"
    )


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


@dataclasses.dataclass
class Run:
    argv: list[str]
    model: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    events: list[dict[str, Any]]
    init: dict[str, Any] | None
    result: dict[str, Any] | None
    tool_uses: list[dict[str, Any]]
    tool_results: list[str]
    denials: list[dict[str, Any]]
    denial_events: list[dict[str, Any]]


def _parse_run(
    argv: list[str],
    model: str,
    returncode: int,
    stdout: str,
    stderr: str,
    timed_out: bool,
) -> Run:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"),
        None,
    )
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)

    tool_uses: list[dict[str, Any]] = []
    tool_results: list[str] = []
    denial_events: list[dict[str, Any]] = []
    for event in events:
        etype = event.get("type")
        if etype == "assistant":
            content = (event.get("message") or {}).get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tool_uses.append({"name": item.get("name"), "input": item.get("input")})
        elif etype == "user":
            content = (event.get("message") or {}).get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_results.append(_text_of(item.get("content")))
        elif etype == "system" and event.get("subtype") == "permission_denied":
            denial_events.append(event)

    denials = list((result or {}).get("permission_denials") or [])
    return Run(
        argv=argv,
        model=model,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        events=events,
        init=init,
        result=result,
        tool_uses=tool_uses,
        tool_results=tool_results,
        denials=denials,
        denial_events=denial_events,
    )


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text")))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _exec_claude(
    cfg: Path,
    project: Path,
    instruction: str,
    *,
    mode: str,
    model: str,
    extra_env: dict[str, str] | None,
    max_turns: int,
) -> Run:
    argv = [
        _claude_bin(),
        "-p",
        instruction,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        mode,
        "--max-turns",
        str(max_turns),
        "--model",
        model,
        "--setting-sources",
        "user",
        "--strict-mcp-config",
    ]
    if mode == "bypassPermissions":
        argv.append("--allow-dangerously-skip-permissions")

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(cfg)
    env["ANTHROPIC_BASE_URL"] = _BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = _AUTH_TOKEN
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra_env:
        env.update(extra_env)

    kwargs: dict[str, Any] = {}
    if _CREATE_NO_WINDOW:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            argv,
            cwd=str(project),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_S,
            **kwargs,
        )
        out, err, rc, timed = proc.stdout or "", proc.stderr or "", proc.returncode, False
    except subprocess.TimeoutExpired as exc:
        out, err, rc, timed = _decode(exc.stdout), _decode(exc.stderr), -1, True
    return _parse_run(argv, model, rc, out, err, timed)


def _looks_like_model_rejection(run: Run) -> bool:
    blob = (run.stdout + "\n" + run.stderr).lower()
    return "supported api model names" in blob or (
        "invalid_request_error" in blob and "model" in blob
    )


def _run_claude(
    cfg: Path,
    project: Path,
    instruction: str,
    *,
    mode: str = "default",
    settings: dict[str, Any] | None = None,
    extra_env: dict[str, str] | None = None,
    max_turns: int = 4,
) -> Run:
    if settings is not None:
        (cfg / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    chosen = _MODEL["chosen"]
    candidates = [chosen] if chosen else list(_MODEL_CANDIDATES)
    last: Run | None = None
    for model in candidates:
        run = _exec_claude(
            cfg,
            project,
            instruction,
            mode=mode,
            model=model,
            extra_env=extra_env,
            max_turns=max_turns,
        )
        if _looks_like_model_rejection(run):
            last = run
            continue
        _MODEL["chosen"] = model
        return run
    if last is None:
        raise AssertionError("no model candidate was attempted")
    return last


def _describe(run: Run) -> str:
    payload = {
        "model": run.model,
        "returncode": run.returncode,
        "timed_out": run.timed_out,
        "permissionMode": (run.init or {}).get("permissionMode"),
        "denials": run.denials,
        "denial_reasons": [e.get("decision_reason") for e in run.denial_events],
        "tool_uses": run.tool_uses,
        "tool_results": run.tool_results,
        "result_subtype": (run.result or {}).get("subtype"),
        "result_text": (run.result or {}).get("result"),
        "stderr_tail": run.stderr[-800:],
    }
    return json.dumps(payload, ensure_ascii=False)[:4000]


# --------------------------------------------------------------------------- #
# Sandbox + hook fixtures (all writes live under one mkdtemp we remove)
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _sandbox() -> Iterator[tuple[Path, Path, Path]]:
    root = Path(tempfile.mkdtemp(prefix="awgov_cc_probe_"))
    cfg = root / "cfg"
    project = root / "project"
    cfg.mkdir()
    project.mkdir()
    try:
        yield root, cfg, project
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _pre_decision(verdict: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": verdict,
            "permissionDecisionReason": reason,
        }
    }


def _permission_request_allow(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
            "reason": reason,
        }
    }


def _write_hook(path: Path, log_path: Path, decision: dict[str, Any] | None) -> str:
    """Write a tiny command hook that logs its stdin JSON then prints a decision."""
    lines = [
        "import json",
        "import sys",
        "",
        "data = sys.stdin.read()",
        "with open(" + repr(str(log_path)) + ", 'a', encoding='utf-8') as fh:",
        "    fh.write(data + chr(10))",
    ]
    if decision is not None:
        lines.append("print(json.dumps(" + repr(decision) + "))")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f'python "{path.as_posix()}"'


def _pre_tool_use_settings(hook_command: str, matcher: str = "Bash") -> dict[str, Any]:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": matcher,
                    "hooks": [{"type": "command", "command": hook_command, "timeout": 30}],
                }
            ]
        }
    }


def _hook_entries(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _git_init(project: Path) -> None:
    kwargs: dict[str, Any] = {}
    if _CREATE_NO_WINDOW:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        **kwargs,
    )


def _write_attempts(run: Run, path: Path) -> list[dict[str, Any]]:
    target = str(path).replace("\\", "/").lower()
    found = []
    for use in run.tool_uses:
        if use.get("name") != "Write":
            continue
        tool_input = use.get("input") or {}
        candidate = str(tool_input.get("file_path") or "").replace("\\", "/").lower()
        if candidate.endswith(target):
            found.append(use)
    return found


def _denied_tool(run: Run, tool_name: str) -> bool:
    return any(d.get("tool_name") == tool_name for d in run.denials)


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# Local http hook server for the header case
# --------------------------------------------------------------------------- #

_HTTP_RECORDS: list[dict[str, Any]] = []


class _RecorderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        _HTTP_RECORDS.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        payload = json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "header probe",
                }
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


@contextlib.contextmanager
def _http_recorder() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _RecorderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# Instructions sent to the model
# --------------------------------------------------------------------------- #

_INSTR_12345 = (
    "Run exactly this one Bash command and then stop. Do not do anything else: "
    'python -c "print(12345)"'
)
_INSTR_55555 = (
    "Run exactly this one Bash command and then stop. Do not do anything else: "
    'python -c "print(55555)"'
)
_INSTR_43210 = (
    "Run exactly this one Bash command and then stop. Do not do anything else: "
    'python -c "print(43210)"'
)
_INSTR_77777 = (
    "Run exactly this one Bash command and then stop. Do not do anything else: "
    'python -c "print(77777)"'
)
_INSTR_11111 = (
    "Run exactly this one Bash command and then stop. Do not do anything else: "
    'python -c "print(11111)"'
)
_INSTR_24680 = (
    "Run exactly this one Bash command and then stop. Do not do anything else: "
    'python -c "print(24680)"'
)


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #


def test_01_pretooluse_allow_runs_bash() -> None:
    with _sandbox() as (root, cfg, project):
        hook_command = _write_hook(
            root / "hook.py", root / "hook.log", _pre_decision("allow", "probe allow")
        )
        run = _run_claude(
            cfg,
            project,
            _INSTR_12345,
            mode="default",
            settings=_pre_tool_use_settings(hook_command),
        )
        print("\nCASE 1 OBSERVED:", json.dumps({"denials": run.denials,
                                               "tool_results": run.tool_results}))
        assert run.denials == [], f"expected no denial; observed {_describe(run)}"
        assert any("12345" in text for text in run.tool_results), (
            f"command did not run: {_describe(run)}"
        )
        entries = _hook_entries(root / "hook.log")
        assert any(e.get("tool_name") == "Bash" for e in entries), (
            f"PreToolUse hook log missing the Bash call: {entries}"
        )


def test_02_pretooluse_allow_protected_paths() -> None:
    # The go/no-go: does a PreToolUse `allow` cover .claude/** and .git/** writes?
    with _sandbox() as (root, cfg, project):
        hook_command = _write_hook(
            root / "hook.py", root / "hook.log", _pre_decision("allow", "probe allow")
        )
        settings = _pre_tool_use_settings(hook_command, matcher="*")
        claude_probe = project / ".claude" / "probe.txt"
        claude_instr = (
            "Use the Write tool exactly once to create the file "
            f"{claude_probe.as_posix()} with the exact content: hello. Then stop."
        )
        run_claude_dir = _run_claude(
            cfg, project, claude_instr, mode="default", settings=settings
        )
        claude_attempted = bool(_write_attempts(run_claude_dir, claude_probe))
        claude_exists = claude_probe.exists()

        _git_init(project)
        git_probe = project / ".git" / "probe.txt"
        git_instr = (
            "Use the Write tool exactly once to create the file "
            f"{git_probe.as_posix()} with the exact content: hello. Then stop."
        )
        run_git_dir = _run_claude(cfg, project, git_instr, mode="default", settings=settings)
        git_attempted = bool(_write_attempts(run_git_dir, git_probe))
        git_exists = git_probe.exists()

        observed = {
            "claude_claude_dir": {
                "attempted": claude_attempted,
                "file_exists": claude_exists,
                "denied": _denied_tool(run_claude_dir, "Write"),
                "tool_results": run_claude_dir.tool_results,
            },
            "claude_git_dir": {
                "attempted": git_attempted,
                "file_exists": git_exists,
                "denied": _denied_tool(run_git_dir, "Write"),
                "tool_results": run_git_dir.tool_results,
            },
        }
        print("\nCASE 2 OBSERVED:", json.dumps(observed, ensure_ascii=False))

        assert claude_attempted, (
            f"model never called Write for .claude: {_describe(run_claude_dir)}"
        )
        assert git_attempted, f"model never called Write for .git: {_describe(run_git_dir)}"
        # A protected write that neither landed nor was denied is an unmeasured outcome.
        assert claude_exists or _denied_tool(run_claude_dir, "Write"), (
            f".claude write neither succeeded nor was permission-denied: "
            f"{_describe(run_claude_dir)}"
        )
        assert git_exists or _denied_tool(run_git_dir, "Write"), (
            f".git write neither succeeded nor was permission-denied: {_describe(run_git_dir)}"
        )


def test_03_permission_request_allow_runs_bash() -> None:
    with _sandbox() as (root, cfg, project):
        hook_command = _write_hook(
            root / "hook.py", root / "hook.log", _permission_request_allow("probe allow")
        )
        settings = {
            "hooks": {
                "PermissionRequest": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": hook_command, "timeout": 30}],
                    }
                ]
            }
        }
        run = _run_claude(cfg, project, _INSTR_55555, mode="default", settings=settings)
        entries = _hook_entries(root / "hook.log")
        print("\nCASE 3 OBSERVED:", json.dumps(
            {
                "denials": run.denials,
                "tool_results": run.tool_results,
                "hook_events": [e.get("hook_event_name") for e in entries],
            }
        ))
        assert run.denials == [], f"PermissionRequest allow did not clear -p: {_describe(run)}"
        assert any("55555" in text for text in run.tool_results), (
            f"Bash did not run after PermissionRequest allow: {_describe(run)}"
        )
        assert any(e.get("hook_event_name") == "PermissionRequest" for e in entries), (
            f"PermissionRequest hook never fired in -p: {entries}"
        )


def test_04_baseline_no_hooks_is_denied() -> None:
    # Proves the probe can fail: with no hooks, -p refuses the same kind of call.
    with _sandbox() as (root, cfg, project):
        run = _run_claude(cfg, project, _INSTR_43210, mode="default", settings={})
        print("\nCASE 4 OBSERVED:", json.dumps(
            {"denials": run.denials, "tool_results": run.tool_results}
        ))
        assert run.denials, f"baseline should be denied with no hooks: {_describe(run)}"
        assert not any("43210" in text for text in run.tool_results), (
            f"baseline command unexpectedly ran: {_describe(run)}"
        )


def test_05_pretooluse_deny_beats_bypass() -> None:
    with _sandbox() as (root, cfg, project):
        hook_command = _write_hook(
            root / "hook.py", root / "hook.log", _pre_decision("deny", "probe deny")
        )
        run = _run_claude(
            cfg,
            project,
            _INSTR_77777,
            mode="bypassPermissions",
            settings=_pre_tool_use_settings(hook_command),
        )
        print("\nCASE 5 OBSERVED:", json.dumps(
            {
                "permissionMode": (run.init or {}).get("permissionMode"),
                "denials": run.denials,
                "tool_results": run.tool_results,
            }
        ))
        assert _denied_tool(run, "Bash"), (
            f"hook deny did not beat bypassPermissions (or bypass was refused): {_describe(run)}"
        )
        assert not any("77777" in text for text in run.tool_results), (
            f"denied command executed under bypass: {_describe(run)}"
        )


def test_06_disable_bypass_permission_mode() -> None:
    # Informational: does a user-source settings file disable bypass, or is the key
    # managed-only (ignored here)? A control run without the key proves bypass was
    # otherwise reachable, so the two modes are directly comparable.
    with _sandbox() as (root, cfg, project):
        control = _run_claude(
            cfg,
            project,
            "Reply with the single word: ready",
            mode="bypassPermissions",
            settings={},
        )
        settings = {"permissions": {"disableBypassPermissionsMode": "disable"}}
        disabled = _run_claude(
            cfg,
            project,
            "Reply with the single word: ready",
            mode="bypassPermissions",
            settings=settings,
        )
        control_mode = (control.init or {}).get("permissionMode")
        disabled_mode = (disabled.init or {}).get("permissionMode")
        observed = {
            "control_permissionMode": control_mode,
            "control_returncode": control.returncode,
            "disabled_permissionMode": disabled_mode,
            "disabled_returncode": disabled.returncode,
            "disabled_result_subtype": (disabled.result or {}).get("subtype"),
            "disabled_result_text": (disabled.result or {}).get("result"),
            "disabled_stderr_tail": disabled.stderr[-600:],
        }
        print("\nCASE 6 OBSERVED:", json.dumps(observed, ensure_ascii=False))
        if control_mode == "bypassPermissions" and disabled_mode != "bypassPermissions":
            print("CASE 6 NOTE: disableBypassPermissionsMode IS honored from user settings")
        if disabled_mode == "bypassPermissions":
            print("CASE 6 NOTE: disableBypassPermissionsMode NOT honored from user "
                  "settings (managed-only key)")
        assert control.init is not None or control.returncode != 0, (
            f"control bypass run observed nothing: {_describe(control)}"
        )
        assert disabled.init is not None or disabled.returncode != 0, (
            f"disabled-key run observed nothing: {_describe(disabled)}"
        )


def test_07_http_hook_closed_port_onfailure() -> None:
    with _sandbox() as (root, cfg, project):
        url = f"http://127.0.0.1:{_closed_port()}/pdp"
        allow_all = {"permissions": {"allow": ["Bash"]}}

        settings_block = dict(allow_all)
        settings_block["hooks"] = {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "http", "url": url, "timeout": 5,
                               "onFailure": "block"}],
                }
            ]
        }
        run_block = _run_claude(cfg, project, _INSTR_11111, mode="default",
                                settings=settings_block)

        settings_default = dict(allow_all)
        settings_default["hooks"] = {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "http", "url": url, "timeout": 5}]}
            ]
        }
        run_default = _run_claude(cfg, project, _INSTR_11111, mode="default",
                                  settings=settings_default)

        ran_default = any("11111" in text for text in run_default.tool_results)
        print("\nCASE 7 OBSERVED:", json.dumps(
            {
                "onFailure_block": {"denied": _denied_tool(run_block, "Bash"),
                                    "tool_results": run_block.tool_results},
                "no_onfailure": {"denied": _denied_tool(run_default, "Bash"),
                                 "ran": ran_default,
                                 "tool_results": run_default.tool_results},
            },
            ensure_ascii=False,
        ))
        assert _denied_tool(run_block, "Bash"), (
            f"onFailure=block against a closed port did not deny: {_describe(run_block)}"
        )
        assert bool(run_default.denials) or ran_default, (
            f"no-onFailure probe was inconclusive (neither denied nor ran): "
            f"{_describe(run_default)}"
        )


def test_08_subagent_hook_fields() -> None:
    with _sandbox() as (root, cfg, project):
        hook_command = _write_hook(
            root / "hook.py", root / "hook.log", _pre_decision("allow", "probe allow")
        )
        settings = _pre_tool_use_settings(hook_command, matcher="*")
        settings["permissions"] = {"allow": ["Task"]}
        sub_prompt = (
            "Run exactly this one Bash command and then stop: "
            'python -c "print(88888)"'
        )
        instruction = (
            "Use the Task tool exactly once to launch a subagent. Set subagent_type to "
            '"general-purpose" and prompt to: ' + sub_prompt + ". Then stop."
        )
        run = _run_claude(
            cfg, project, instruction, mode="default", settings=settings, max_turns=6
        )
        entries = _hook_entries(root / "hook.log")
        bash_entries = [e for e in entries if e.get("tool_name") == "Bash"]
        field_names = sorted(
            {key for e in bash_entries for key in ("agent_type", "agent_id") if key in e}
        )
        observed = {
            "hook_event_names": [e.get("hook_event_name") for e in entries],
            "tool_names": [e.get("tool_name") for e in entries],
            "bash_agent_fields": [
                {key: e.get(key) for key in ("agent_type", "agent_id")} for e in bash_entries
            ],
            "present_agent_field_names": field_names,
            "tool_uses": [u.get("name") for u in run.tool_uses],
        }
        print("\nCASE 8 OBSERVED:", json.dumps(observed, ensure_ascii=False))
        assert bash_entries, (
            f"no subagent Bash call reached the PreToolUse hook: {_describe(run)}"
        )
        assert any(
            value
            for e in bash_entries
            for value in (e.get("agent_type"), e.get("agent_id"))
        ), (
            "subagent Bash call carried neither agent_type nor agent_id: "
            f"{observed['bash_agent_fields']}"
        )


def test_09_http_hook_forwards_env_header() -> None:
    _HTTP_RECORDS.clear()
    with _sandbox() as (root, cfg, project):
        with _http_recorder() as port:
            settings = {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "http",
                                    "url": f"http://127.0.0.1:{port}/pdp",
                                    "timeout": 10,
                                    "headers": {"X-Aither-Principal": "$AITHER_PDP_PRINCIPAL"},
                                    "allowedEnvVars": ["AITHER_PDP_PRINCIPAL"],
                                }
                            ],
                        }
                    ]
                }
            }
            run = _run_claude(
                cfg,
                project,
                _INSTR_24680,
                mode="default",
                settings=settings,
                extra_env={"AITHER_PDP_PRINCIPAL": "owner-tty"},
            )
        records = list(_HTTP_RECORDS)
        observed = {
            "requests": len(records),
            "headers": records[0]["headers"] if records else None,
            "denials": run.denials,
            "tool_results": run.tool_results,
        }
        print("\nCASE 9 OBSERVED:", json.dumps(observed, ensure_ascii=False))
        assert records, f"http hook never reached the local server: {_describe(run)}"
        received = records[0]["headers"].get("x-aither-principal")
        assert received == "owner-tty", (
            f"env var header was not forwarded (got {received!r}): {records[0]['headers']}"
        )
        assert run.denials == [], f"http allow did not clear -p: {_describe(run)}"
        assert any("24680" in text for text in run.tool_results), (
            f"Bash did not run after http allow: {_describe(run)}"
        )


def test_10_auto_mode_vs_pretooluse_allow() -> None:
    # Informational: in auto mode, does a PreToolUse allow skip the classifier?
    # The command is harmless: the repo has no remote, so the push cannot happen.
    with _sandbox() as (root, cfg, project):
        _git_init(project)
        hook_command = _write_hook(
            root / "hook.py", root / "hook.log", _pre_decision("allow", "probe allow")
        )
        instruction = (
            "Run exactly this one Bash command and then stop. Do not do anything else: "
            "git push --force origin nonexistent-branch-xyz"
        )
        run = _run_claude(
            cfg,
            project,
            instruction,
            mode="auto",
            settings=_pre_tool_use_settings(hook_command),
        )
        print("\nCASE 10 OBSERVED:", json.dumps(
            {
                "permissionMode": (run.init or {}).get("permissionMode"),
                "denials": run.denials,
                "denial_reasons": [e.get("decision_reason") for e in run.denial_events],
                "tool_results": run.tool_results,
                "result_subtype": (run.result or {}).get("subtype"),
                "stderr_tail": run.stderr[-400:],
            },
            ensure_ascii=False,
        ))
        assert run.result is not None, (
            f"auto-mode session produced no result event: {_describe(run)}"
        )
