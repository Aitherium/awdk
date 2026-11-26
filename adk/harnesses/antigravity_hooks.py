"""Antigravity <-> AitherOS/hookd lifecycle hook bridge (stdlib-only).

Antigravity 2.0 / CLI ``hooks.json`` runs ``type: "command"`` hooks with camelCase
JSON on stdin (``conversationId``, ``hookEventName``, ``toolCall``, ``cwd``,
``lastUserInput``, ``transcriptPath``) and expects Antigravity's JSON contract on
stdout (``decision`` for PreToolUse/Stop, ``injectSteps`` for SessionStart /
PreInvocation / PostInvocation, ``{}`` for PostToolUse).

Rather than duplicating 30+ hooks across Claude Code and Antigravity, this module
is ONE thin, stdlib-only adapter that:

1. Translates Antigravity payloads <-> Claude/hookd hook payloads (tool names,
   parameter shapes, event names, and verdicts).
2. Records live session presence in ``~/.aither/antigravity_sessions/<id>.json``
   and turn traces in ``~/.aither/agent-trace/antigravity-<date>.jsonl`` so
   ``awsh``, ``awdesk``, ``awconnect``, ``awfocus``, and ``awmine`` see every
   Antigravity session alongside Claude Code.
3. Forwards ``PreToolUse`` to ``hookd`` ``/pdp?mode=enforce`` (``awgov`` PDP)
   and repo/user Python hooks through ``hookd`` ``/run`` (with direct fallback
   when ``hookd`` is offline).
4. Drains the unified steering mailbox (``~/.aither/steer/<conversationId>/*.md``)
   so ``awfocus``, ``awdesk``, ``awask``, and ``awsh`` can steer live Antigravity
   conversations directly into context via ``injectSteps`` or ``Stop`` continuation.

Usage (from ``hooks.json``):
    python3 -S -m adk.harnesses.antigravity_hooks PreToolUse
    python3 -S /path/to/antigravity_hooks.py PreInvocation --repo
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

HOOKD_PORT = int(os.environ.get("AITHER_HOOKD_PORT", "8369"))
HOOKD_BASE = os.environ.get("AITHER_HOOKD_URL") or f"http://127.0.0.1:{HOOKD_PORT}"

#: Antigravity tool name -> Claude Code tool name expected by awgov / repo hooks.
TOOL_MAP: dict[str, str] = {
    "run_command": "Bash",
    "edit_file": "Edit",
    "write_file": "Write",
    "read_file": "Read",
    "ask_question": "AskUserQuestion",
    "search_web": "WebSearch",
    "invoke_subagent": "Agent",
}

#: Antigravity hookEventName -> Claude Code hook_event_name.
EVENT_MAP: dict[str, str] = {
    "SessionStart": "SessionStart",
    "PreInvocation": "UserPromptSubmit",
    "PostInvocation": "PostInvocation",
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "Stop": "Stop",
}

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def aither_home() -> Path:
    raw = os.environ.get("AITHER_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".aither"


def sessions_dir() -> Path:
    raw = os.environ.get("AITHER_ANTIGRAVITY_SESSIONS_DIR", "").strip()
    return Path(raw) if raw else aither_home() / "antigravity_sessions"


def steer_root() -> Path:
    raw = os.environ.get("AITHER_STEER_DIR", "").strip()
    return Path(raw) if raw else aither_home() / "steer"


def trace_dir() -> Path:
    raw = os.environ.get("AWROUTER_TRACE_DIR", "").strip()
    return Path(raw) if raw else aither_home() / "agent-trace"


# ---------------------------------------------------------------- translation


#: Antigravity read-only/UI tools that never require PreToolUse PDP/guard subprocesses.
_FAST_ALLOW_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "update_focus",
    "update_steps",
    "list_plugin_accounts",
})


def _normalize_mcp_server(server: str) -> str:
    """Map plugin-prefixed server names (e.g. ``aitheros_aitheros``) to canonical awgov prefixes."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", (server or "").strip()).strip("_")
    if s in ("aitheros_aitheros", "aitheros"):
        return "aitheros"
    if s.startswith("aitheros_") and len(s) > len("aitheros_"):
        return s[len("aitheros_"):]
    return s or "mcp"


def translate_tool_input(ag_tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map an Antigravity tool call to Claude's (tool_name, tool_input)."""
    if not isinstance(args, dict):
        args = {}
    if ag_tool == "call_mcp_tool":
        server = _normalize_mcp_server(str(args.get("ServerName") or args.get("server_name") or ""))
        tool = str(args.get("ToolName") or args.get("tool_name") or "").strip()
        inner_args = args.get("Arguments") if isinstance(args.get("Arguments"), dict) else {}
        # Unwrap routed meta-dispatcher calls (`aither_call`) so awgov PDP evaluates
        # the real underlying tool (e.g. `mcp__aitheros__set_secret`).
        if tool == "aither_call" and isinstance(inner_args.get("tool"), str) and inner_args["tool"].strip():
            tool = inner_args["tool"].strip()
            routed_args = inner_args.get("arguments")
            inner_args = routed_args if isinstance(routed_args, dict) else {}
        claude_name = f"mcp__{server}__{tool}" if tool else f"mcp__{server}"
        return claude_name, dict(inner_args)
    claude_name = TOOL_MAP.get(ag_tool, ag_tool)
    if ag_tool == "run_command":
        out = dict(args)
        cmd = args.get("CommandLine") or args.get("command") or ""
        out["command"] = str(cmd)
        return claude_name, out
    if ag_tool == "edit_file":
        out = dict(args)
        out["file_path"] = str(args.get("Path") or args.get("file_path") or "")
        if "Target" in args and "old_string" not in out:
            out["old_string"] = args["Target"]
        if "Replacement" in args and "new_string" not in out:
            out["new_string"] = args["Replacement"]
        return claude_name, out
    if ag_tool == "write_file":
        out = dict(args)
        out["file_path"] = str(args.get("Path") or args.get("file_path") or "")
        if "Content" in args and "content" not in out:
            out["content"] = args["Content"]
        return claude_name, out
    if ag_tool == "read_file":
        out = dict(args)
        out["file_path"] = str(args.get("Path") or args.get("file_path") or "")
        return claude_name, out
    return claude_name, dict(args)


def resolve_cwd(payload: dict[str, Any]) -> str:
    cwd = str(payload.get("cwd") or "").strip()
    if cwd:
        return cwd
    ws = payload.get("workspacePaths")
    if isinstance(ws, list) and ws:
        return str(ws[0] or "").strip()
    return os.getcwd()


def to_claude_payload(ag_payload: dict[str, Any], event: str) -> dict[str, Any]:
    """Convert an Antigravity hook stdin object to Claude Code's hook JSON shape."""
    claude_event = EVENT_MAP.get(event, event)
    cwd = resolve_cwd(ag_payload)
    out: dict[str, Any] = {
        "hook_event_name": claude_event,
        "session_id": str(ag_payload.get("conversationId") or ""),
        "cwd": cwd,
        "transcript_path": str(ag_payload.get("transcriptPath") or ""),
        "source_harness": "antigravity",
        "model": str(ag_payload.get("modelName") or ""),
    }
    if ag_payload.get("lastUserInput"):
        out["prompt"] = str(ag_payload["lastUserInput"])
    tc = ag_payload.get("toolCall")
    if isinstance(tc, dict):
        ag_tool = str(tc.get("name") or "")
        raw_args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        claude_tool, claude_input = translate_tool_input(ag_tool, raw_args)
        out["tool_name"] = claude_tool
        out["tool_input"] = claude_input
        if tc.get("id"):
            out["tool_use_id"] = str(tc["id"])
    if ag_payload.get("error"):
        out["error"] = str(ag_payload["error"])
    if ag_payload.get("terminationReason"):
        out["termination_reason"] = str(ag_payload["terminationReason"])
    return out


def from_claude_response(
    event: str,
    claude_resp: dict[str, Any],
    *,
    extra_context: Optional[list[str]] = None,
    continuation_active: bool = False,
) -> dict[str, Any]:
    """Convert a merged Claude/hookd JSON response to Antigravity's stdout contract."""
    if not isinstance(claude_resp, dict):
        claude_resp = {}
    hso = claude_resp.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        hso = {}

    if event == "PreToolUse":
        decision = str(hso.get("permissionDecision") or "").strip().lower()
        reason = str(
            hso.get("permissionDecisionReason")
            or claude_resp.get("reason")
            or ""
        ).strip()
        if not decision and claude_resp.get("decision") == "block":
            decision = "deny"
        if decision not in ("allow", "deny", "ask", "force_ask"):
            decision = "allow"
        out: dict[str, Any] = {"decision": decision}
        if reason:
            out["reason"] = reason
        return out

    if event == "PostToolUse":
        return {}

    if event == "Stop":
        # Never re-block when a Stop continuation is already active.
        if continuation_active:
            return {}
        reasons: list[str] = []
        if claude_resp.get("decision") == "block":
            reasons.append(str(claude_resp.get("reason") or "Stop gate requested continuation."))
        if extra_context:
            reasons.extend(c for c in extra_context if c.strip())
        if reasons:
            return {"decision": "continue", "reason": "\n\n".join(reasons)}
        return {}

    # SessionStart / PreInvocation / PostInvocation
    chunks: list[str] = []
    if claude_resp.get("systemMessage"):
        chunks.append(str(claude_resp["systemMessage"]))
    if hso.get("additionalContext"):
        chunks.append(str(hso["additionalContext"]))
    if extra_context:
        chunks.extend(c for c in extra_context if c.strip())

    out_steps: list[dict[str, Any]] = []
    if chunks:
        out_steps.append({"systemMessage": {"systemMessage": "\n\n".join(chunks)}})

    blocked = claude_resp.get("decision") == "block" or claude_resp.get("continue") is False
    if blocked:
        why = str(
            claude_resp.get("reason")
            or claude_resp.get("stopReason")
            or "Blocked by AitherOS hook"
        )
        out_steps.append({"errorMessage": {"userMessage": why}})
        return {"injectSteps": out_steps, "terminationBehavior": "terminate"}

    return {"injectSteps": out_steps} if out_steps else {}


# ---------------------------------------------------------------- presence + trace


def record_session(ag_payload: dict[str, Any], status: str) -> None:
    """Persist live Antigravity conversation metadata for awsh/awdesk/awconnect/awfocus."""
    cid = str(ag_payload.get("conversationId") or "").strip()
    if not cid or not _SAFE_ID_RE.match(cid):
        return
    try:
        sdir = sessions_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / f"{cid}.json"
        prior: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    prior = loaded
            except (OSError, ValueError):
                prior = {}
        now_ts = time.time()
        cwd = resolve_cwd(ag_payload)
        prompt = str(ag_payload.get("lastUserInput") or prior.get("lastPrompt") or "")[:400]
        # Debounce redundant writes during rapid tool-call bursts (same status/prompt within 5s).
        if (
            prior
            and prior.get("status") == status
            and prior.get("lastPrompt") == prompt
            and prior.get("cwd") == cwd
            and (now_ts - float(prior.get("updatedTs") or 0.0)) < 5.0
        ):
            return
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        rec = {
            "sessionId": cid,
            "id": cid,
            "harness": "antigravity",
            "harnessLabel": "Antigravity",
            "cwd": cwd,
            "title": str(prior.get("title") or (prompt[:72] if prompt else f"Antigravity {cid[:8]}")),
            "status": status,
            "live": True,
            "lastPrompt": prompt,
            "transcriptPath": str(ag_payload.get("transcriptPath") or prior.get("transcriptPath") or ""),
            "artifactDirectoryPath": str(
                ag_payload.get("artifactDirectoryPath") or prior.get("artifactDirectoryPath") or ""
            ),
            "modelName": str(ag_payload.get("modelName") or prior.get("modelName") or ""),
            "lsAddress": os.environ.get("ANTIGRAVITY_LS_ADDRESS", "") or str(prior.get("lsAddress") or ""),
            "startedAt": prior.get("startedAt") or now_iso,
            "updatedAt": now_iso,
            "updatedTs": now_ts,
            "pid": os.getppid(),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def append_trace(ag_payload: dict[str, Any], event: str) -> None:
    """Write one non-secret telemetry row to ~/.aither/agent-trace/antigravity-<date>.jsonl."""
    try:
        tdir = trace_dir()
        tdir.mkdir(parents=True, exist_ok=True)
        day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        row = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "harness": "antigravity",
            "event": event,
            "conversation_id": str(ag_payload.get("conversationId") or ""),
            "model": str(ag_payload.get("modelName") or ""),
            "cwd": resolve_cwd(ag_payload),
            "invocation_num": ag_payload.get("invocationNum"),
            "step_idx": ag_payload.get("stepIdx"),
            "termination_reason": ag_payload.get("terminationReason"),
        }
        with (tdir / f"antigravity-{day}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- steering mailbox


def drain_steer_mailbox(conversation_id: str) -> list[str]:
    """Drain ~/.aither/steer/<conversationId>/*.md and archive to delivered/."""
    cid = str(conversation_id or "").strip()
    if not cid or not _SAFE_ID_RE.match(cid):
        return []
    box = steer_root() / cid
    if not box.is_dir():
        return []
    delivered = box / "delivered"
    messages: list[str] = []
    try:
        files = sorted(p for p in box.iterdir() if p.is_file() and p.suffix == ".md")
    except OSError:
        return []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            delivered.mkdir(parents=True, exist_ok=True)
            os.replace(f, delivered / f.name)
            if text:
                messages.append(f"[AitherOS steer/{f.name}]\n{text}")
        except OSError:
            continue
    return messages


# ---------------------------------------------------------------- hookd client


def _hookd_post(path_and_query: str, claude_payload: dict[str, Any], cwd: str, timeout: float = 8.0) -> Optional[dict[str, Any]]:
    url = HOOKD_BASE.rstrip("/") + path_and_query
    body = json.dumps(claude_payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hookd": "1",
            "X-Project-Dir": cwd,
            "X-Aither-Source-Harness": "antigravity",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            if not raw:
                return {}
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
    except (OSError, urllib.error.URLError, ValueError):
        return None


def _ensure_hookd() -> None:
    try:
        req = urllib.request.Request(f"{HOOKD_BASE.rstrip('/')}/health", method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                return
    except (OSError, urllib.error.URLError):
        pass
    hookd_py = Path(__file__).resolve().with_name("hookd.py")
    if not hookd_py.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, "-S", str(hookd_py), "ensure"],
            capture_output=True,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------- portable bricks


def _run_cmd(cmd: list[str], stdin_text: str = "", timeout: float = 6.0, cwd: Optional[str] = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd if (cwd and os.path.isdir(cwd)) else None,
            check=False,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError):
        return 0, "", ""


def _hookgate_path() -> Optional[Path]:
    try:
        spec = importlib.util.find_spec("awrelay")
    except (ImportError, ValueError):
        return None
    for loc in (spec.submodule_search_locations or []) if spec else []:
        gate = Path(loc) / "hookgate.py"
        if gate.is_file():
            return gate
    return None


def _run_platform_first_guard(claude_payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    exe = shutil.which("awsettings-guard")
    if not exe:
        return None
    rc, out, err = _run_cmd([exe], json.dumps(claude_payload), timeout=6.0)
    if rc == 2:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (err or out or "Blocked by platform-first guard").strip(),
            }
        }
    out = out.strip()
    if out.startswith("{"):
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    return None


def _find_repo_hooks_dir(cwd: str) -> Optional[Path]:
    if not cwd:
        return None
    try:
        start = Path(cwd).resolve()
    except OSError:
        return None
    for d in (start, *start.parents):
        cand = d / ".claude" / "hooks"
        if cand.is_dir():
            return cand
    return None


def _run_repo_scripts(
    event: str,
    claude_payload: dict[str, Any],
    scripts: list[tuple[str, list[str]]],
    cwd: str,
) -> dict[str, Any]:
    """Run repo .claude/hooks Python scripts via hookd /run, falling back to direct python -S."""
    hooks_dir = _find_repo_hooks_dir(cwd)
    if not hooks_dir:
        return {}
    existing: list[tuple[Path, list[str]]] = []
    for name, extra_args in scripts:
        p = hooks_dir / name
        if p.is_file():
            existing.append((p, extra_args))
    if not existing:
        return {}

    # Prefer hookd /run (zero Windows process churn, unified merge semantics).
    params = [("t", "15")]
    for p, extra_args in existing:
        spec = "|".join([str(p), *extra_args])
        params.append(("h", spec))
    resp = _hookd_post("/run?" + urllib.parse.urlencode(params), claude_payload, cwd, timeout=16.0)
    if resp is not None:
        return resp

    # Direct fallback when hookd is not reachable (e.g. sandboxed WSL2).
    raw_in = json.dumps(claude_payload)
    merged_ctx: list[str] = []
    for p, extra_args in existing:
        rc, out, err = _run_cmd([sys.executable, "-S", str(p), *extra_args], raw_in, timeout=10.0, cwd=cwd)
        out_s, err_s = out.strip(), err.strip()
        if rc == 2:
            why = err_s or out_s or f"Blocked by {p.name}"
            if event == "PreToolUse":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": why,
                    }
                }
            return {"decision": "block", "reason": why}
        if out_s.startswith("{"):
            try:
                d = json.loads(out_s)
                if isinstance(d, dict):
                    hso = d.get("hookSpecificOutput") or {}
                    if hso.get("permissionDecision") in ("deny", "ask"):
                        return d
                    if d.get("decision") == "block":
                        return d
                    if hso.get("additionalContext"):
                        merged_ctx.append(str(hso["additionalContext"]))
            except ValueError:
                pass
        elif out_s and event in ("SessionStart", "PreInvocation"):
            merged_ctx.append(out_s)
    if merged_ctx:
        return {"hookSpecificOutput": {"additionalContext": "\n\n".join(merged_ctx)}}
    return {}


# ---------------------------------------------------------------- event handlers


def handle_event(event: str, ag_payload: dict[str, Any], *, repo_mode: bool = False) -> dict[str, Any]:
    """Process one Antigravity lifecycle hook event and return the stdout dict."""
    cwd = resolve_cwd(ag_payload)
    cid = str(ag_payload.get("conversationId") or "")
    claude_payload = to_claude_payload(ag_payload, event)
    settings_disabled = os.environ.get("AWSETTINGS_HOOKS_DISABLED", "") == "1"
    relay_disabled = os.environ.get("AWRELAY_HOOKS_DISABLED", "") == "1"

    if event == "SessionStart":
        record_session(ag_payload, "ready")
        _ensure_hookd()
        extra: list[str] = []
        if not settings_disabled and shutil.which("awsettings"):
            _run_cmd(["awsettings", "--quiet", "--never-fail", "pull"], timeout=6.0)
            _run_cmd(["awsettings", "--domain", "antigravity", "--quiet", "--never-fail", "pull"], timeout=6.0)
        if not relay_disabled and shutil.which("awrelay"):
            _, out, _ = _run_cmd(["awrelay", "inbox", "--claude-hook"], json.dumps(claude_payload), timeout=6.0)
            if out.strip():
                extra.append(out.strip())
        if shutil.which("awm"):
            scope = os.environ.get("AWM_SCOPE", "").strip()
            if scope:
                _, out, _ = _run_cmd(["awm", "recall", "--scope", scope, "--limit", "5"], timeout=6.0)
                if out.strip():
                    extra.append(out.strip())
        repo_resp: dict[str, Any] = {}
        if repo_mode:
            repo_resp = _run_repo_scripts(
                event,
                claude_payload,
                [("aeon-well-context.py", []), ("session-focus.py", ["--start"])],
                cwd,
            )
        return from_claude_response(event, repo_resp, extra_context=extra)

    if event == "PreInvocation":
        record_session(ag_payload, "busy")
        extra = drain_steer_mailbox(cid)
        if not relay_disabled:
            gate = _hookgate_path()
            if gate:
                _, out, _ = _run_cmd(
                    [sys.executable, "-S", "-I", str(gate), "--min-interval", "20"],
                    json.dumps(claude_payload),
                    timeout=5.0,
                )
                if out.strip():
                    extra.append(out.strip())
        scope = os.environ.get("AWM_SCOPE", "").strip()
        prompt = str(ag_payload.get("lastUserInput") or "").strip()
        if scope and prompt and shutil.which("awm"):
            q = " ".join(prompt.split())[:200]
            _, out, _ = _run_cmd(
                ["awm", "recall", "--scope", scope, "--query", q, "--limit", "5"],
                timeout=5.0,
            )
            if out.strip():
                extra.append(out.strip())
        repo_resp = {}
        if repo_mode:
            repo_resp = _run_repo_scripts(
                event,
                claude_payload,
                [("prompt-recall.py", []), ("session-focus.py", ["--deliver"])],
                cwd,
            )
        return from_claude_response(event, repo_resp, extra_context=extra)

    if event == "PreToolUse":
        tc = ag_payload.get("toolCall") if isinstance(ag_payload.get("toolCall"), dict) else {}
        ag_tool_name = str(tc.get("name") or "")
        if ag_tool_name in _FAST_ALLOW_TOOLS:
            return {"decision": "allow"}
        record_session(ag_payload, "busy")
        # 1. awgov PDP via hookd (enforced if hookd is up; bounded timeout so a
        #    busy hookd never hits Antigravity's 15s hook kill ceiling).
        pdp = _hookd_post("/pdp?mode=enforce", claude_payload, cwd, timeout=2.5)
        if pdp:
            hso = pdp.get("hookSpecificOutput") or {}
            if hso.get("permissionDecision") in ("deny", "ask", "force_ask"):
                return from_claude_response(event, pdp)

        # 2. Platform-first guard on shell commands.
        claude_tool = str(claude_payload.get("tool_name") or "")
        if claude_tool == "Bash" and not settings_disabled:
            pf = _run_platform_first_guard(claude_payload)
            if pf:
                hso = pf.get("hookSpecificOutput") or {}
                if hso.get("permissionDecision") in ("deny", "ask"):
                    return from_claude_response(event, pf)

        # 3. Repo-level PreToolUse gates when inside a repo with .claude/hooks.
        if repo_mode:
            if claude_tool == "Bash":
                repo_resp = _run_repo_scripts(
                    event,
                    claude_payload,
                    [
                        ("pre-bash-git.py", []),
                        ("pre-bash-platform-first.py", []),
                        ("pre-bash-qa-gate.py", []),
                        ("pre-bash-awdecide.py", []),
                    ],
                    cwd,
                )
                if repo_resp:
                    return from_claude_response(event, repo_resp)
            elif claude_tool in ("Edit", "Write"):
                repo_resp = _run_repo_scripts(
                    event,
                    claude_payload,
                    [("pre-edit-awgit-lease.py", [])],
                    cwd,
                )
                if repo_resp:
                    return from_claude_response(event, repo_resp)

        return {"decision": "allow"}

    if event == "PostToolUse":
        record_session(ag_payload, "busy")
        if not settings_disabled and shutil.which("awsettings"):
            # Skip subprocess spawn when awsettings pushed recently (debounce stamp < 15s).
            stamp = Path.home() / ".awsettings" / "last-push-antigravity"
            try:
                recently_pushed = (time.time() - stamp.stat().st_mtime) < 15.0
            except OSError:
                recently_pushed = False
            if not recently_pushed:
                _run_cmd(["awsettings", "--domain", "antigravity", "--quiet", "--never-fail", "push", "--debounce"], timeout=5.0)
        if not relay_disabled:
            gate = _hookgate_path()
            if gate:
                _run_cmd(
                    [sys.executable, "-S", "-I", str(gate), "--min-interval", "20"],
                    json.dumps(claude_payload),
                    timeout=5.0,
                )
        if repo_mode and claude_payload.get("tool_name") == "Bash":
            _run_repo_scripts(event, claude_payload, [("post-bash-awdecide.py", [])], cwd)
        return {}

    if event == "PostInvocation":
        append_trace(ag_payload, event)
        return {}

    if event == "Stop":
        record_session(ag_payload, "idle")
        append_trace(ag_payload, event)
        if not settings_disabled and shutil.which("awsettings"):
            _run_cmd(["awsettings", "--quiet", "--never-fail", "push", "--debounce"], timeout=5.0)
        if os.environ.get("AITHER_SPEAK_REPLIES", "") == "1" and shutil.which("awvoice"):
            _run_cmd(["awvoice", "reply"], json.dumps(claude_payload), timeout=6.0)
        cont_active = bool(ag_payload.get("continuationActive"))
        steers = drain_steer_mailbox(cid) if not cont_active else []
        repo_resp = {}
        if repo_mode and not cont_active:
            repo_resp = _run_repo_scripts(
                event,
                claude_payload,
                [("session-focus.py", ["--stop"]), ("stop-open-items.py", [])],
                cwd,
            )
        return from_claude_response(
            event,
            repo_resp,
            extra_context=steers,
            continuation_active=cont_active,
        )

    return {}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="antigravity_hooks")
    parser.add_argument("event", nargs="?", default="", help="Antigravity hookEventName override")
    parser.add_argument("--repo", action="store_true", help="Also run repo .claude/hooks scripts")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read()
    except OSError:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}

    event = args.event or str(payload.get("hookEventName") or "")
    if not event:
        sys.stdout.write("{}\n")
        return 0
    try:
        result = handle_event(event, payload, repo_mode=args.repo)
    except Exception:  # noqa: BLE001 -- hook adapter must never crash the agent loop
        result = {"decision": "allow"} if event == "PreToolUse" else {}
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
