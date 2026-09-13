"""Claude Code subagent runner — scoped headless `claude -p` execution daemon.

This module lets an orchestrator (AitherOS Genesis, another agent, or a human via
`adk claude spawn`) dispatch tightly scoped, headless Claude Code subagents and
collect their results. It is the shared backend behind BOTH frontends:

- HTTP daemon: ``adk claude serve`` (default 127.0.0.1:8365, Bearer-token auth)
- CLI client:  ``adk claude spawn|runs|kill``

Each run gets an isolated workspace (generated ``--settings`` permission
allowlist, ``--strict-mcp-config`` MCP config, appended system prompt, optional
per-run Claude account via ``CLAUDE_CONFIG_DIR``) and produces a stream-json
transcript whose final ``result`` event carries the outcome and token usage.

Security posture (fail-closed, per AitherOS security-review-patterns):
- The daemon REFUSES to serve without a Bearer token; every non-health endpoint
  denies on missing (401) or wrong (403) credentials.
- Scope validation denies by default: empty tool allowlist → rejected; MCP
  servers must be http/sse to an allowlisted gateway host (stdio servers are
  rejected outright — they are arbitrary command execution).
- The task prompt is passed via stdin (never argv — argv leaks to the process
  table) and stored redacted; API responses redact known secret patterns.
- Runs execute in a per-run workdir by default and with ALL settings files
  excluded (--setting-sources ""), so no repo/user hooks or permission grants
  leak into scoped runs; subagent env is stripped of sensitive variables.

Thread safety: watcher threads (_watch/_finalize) and HTTP handlers (submit/
kill) run concurrently; every record load-modify-save cycle MUST hold
``self._lock``. External code must never mutate records directly.

Environment (all optional):
- AITHER_CLAUDE_RUNNER_ROOT: state root (default ~/.aither/claude-runner)
- AITHER_CLAUDE_RUNNER_TOKEN: Bearer token for the daemon
- AITHER_CLAUDE_RUNNER_PORT / _HOST: bind address (default 127.0.0.1:8365)
- AITHER_CLAUDE_RUNNER_MAX_CONCURRENCY: parallel runs (default 2)
- AITHER_CLAUDE_RUNNER_QUEUE_MAX: queued runs before 503 (default 8)
- AITHER_CLAUDE_RUNNER_GATEWAY_HOSTS: comma list of allowed MCP hosts
  (default mcp.aitherium.com,localhost,127.0.0.1,host.docker.internal)
- AITHER_CLAUDE_RUNNER_BIN: claude executable (default "claude")
- AITHER_CLAUDE_RUNNER_NTFY_URL: ntfy topic URL pushed on terminal state
  (unset => no push). The topic URL is itself the credential — keep it in env.
- AITHER_CLAUDE_RUNNER_NTFY_TOKEN: optional Bearer for a protected ntfy topic

Continuing a conversation: pass ``scope.resume_session_id`` (``adk claude spawn
--resume <id> --cwd <repo>``) to run ``claude --resume`` instead of starting a
fresh session. This is what lets a phone drive a long-running thread on this
host rather than firing disconnected one-shots.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets as std_secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from adk.core.logging import get_logger

_log = get_logger("aither_adk.claude_runner")

# Optional selfgraph integration (no-op if unavailable or disabled)
try:
    from adk.selfgraph import (
        current_run,
        ProvNodeType,
        ProvEdgeType,
        make_node_id,
    )
    _SELFGRAPH_AVAILABLE = True
except ImportError:
    _SELFGRAPH_AVAILABLE = False

DEFAULT_PORT = 8365
DEFAULT_HOST = "127.0.0.1"
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_QUEUE_MAX = 8
DEFAULT_TIMEOUT_SEC = 600
MAX_TIMEOUT_SEC = 3600
DEFAULT_GATEWAY_HOSTS = "mcp.aitherium.com,localhost,127.0.0.1,host.docker.internal"
# Push notification on terminal state. Unset => no push (previous behaviour).
# The topic URL IS the credential for ntfy, so it lives in env, never in git.
NTFY_TIMEOUT_SEC = 10
NTFY_BODY_CHARS = 700
# Exclude ALL settings files by default (fail-closed): no user, project, or
# local settings apply to a scoped run — only the generated --settings file.
# Callers must explicitly opt in (e.g. setting_sources="user") if they want the
# operator's defaults. Empty value verified accepted by claude 2.1.214 live.
DEFAULT_SETTING_SOURCES = ""

# Env vars matching this pattern are STRIPPED from the spawned subagent's
# environment — most importantly the daemon's own Bearer token, which would
# otherwise let a prompt-injected subagent call back into this API.
_SENSITIVE_ENV_RE = re.compile(
    r"(?i)(token|secret|passw|api[_-]?key|credential"
    # Routing vars (2026-09-02): a spawned run must ALWAYS use the owner's default
    # Claude backend - that is the whole reason spawn is owner-only. Measured: the
    # runner process inherited its launcher's env, and only credential-shaped names
    # were stripped, so an ANTHROPIC_BASE_URL exported where the task runs would
    # have silently redirected every fleet-spawned run to another provider.
    r"|^anthropic_(base_url|model|default_|custom_)"
    r"|^claude_code_(max_|auto_|disable_|subagent|effort|enable_)"
    r"|^enable_tool_search$)"
)

# Windows: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP. NOT DETACHED_PROCESS —
# the claude CLI on Windows is an npm .cmd shim, and cmd.exe HANGS when spawned
# with DETACHED_PROCESS (no console at all). CREATE_NO_WINDOW gives it an
# invisible console instead (proven by isolation test 2026-07-18).
_WIN_FLAGS = 0x08000000 | 0x00000200

# Secret patterns from .claude/rules/secret-safety.md — redacted from stored
# records and API responses. The raw stream file stays on disk under the run
# root (0700 best-effort) for forensics.
_SECRET_RES = [
    re.compile(p)
    for p in (
        r"sk-ant-[A-Za-z0-9_\-]{8,}",
        r"sk-[A-Za-z0-9_\-]{16,}",
        r"ghp_[A-Za-z0-9]{16,}",
        r"ghs_[A-Za-z0-9]{16,}",
        r"AKIA[0-9A-Z]{12,}",
        r"pk_live_[A-Za-z0-9]{8,}",
        r"sk_live_[A-Za-z0-9]{8,}",
        r"xox[bp]-[A-Za-z0-9\-]{8,}",
        r"aither_sk_live_[A-Za-z0-9_\-]{8,}",
        r"(?i)(api[_-]?key|token|password|secret)\s*[=:]\s*\S{8,}",
    )
]


class RunnerError(RuntimeError):
    """Raised on runner configuration or lifecycle errors."""


class ScopeError(ValueError):
    """Raised when a submitted scope fails fail-closed validation."""


class QueueFullError(RunnerError):
    """Raised when the pending-run queue is at capacity."""


def _default_mcp_config() -> dict[str, Any]:
    """MCP config a run gets when its scope names none.

    Until 2026-09-02 this was ``{"mcpServers": {}}``: a fleet-spawned Claude had
    ZERO platform tools, so the "fleet drives Claude" lane could read files and
    nothing else. The default now attaches the platform through the stdio bridge
    when it is present beside this package (the bridge reads its own bearer, so no
    secret is written here). ``--allowedTools`` still gates every call - a run
    scoped to ``Read`` cannot invoke an MCP tool it was not granted - and
    ``--strict-mcp-config`` still stops user/project config from leaking in.
    Set AITHER_CLAUDE_RUNNER_DEFAULT_MCP=0 to restore the empty default.
    """
    if os.getenv("AITHER_CLAUDE_RUNNER_DEFAULT_MCP", "1").strip().lower() in ("0", "false", "no"):
        return {"mcpServers": {}}
    bridge = (Path(__file__).resolve().parents[2] / "AitherOS" / "dev" / "tools"
              / "mcp_stdio_bridge.py")
    if not bridge.exists():
        return {"mcpServers": {}}
    return {"mcpServers": {"aitheros": {
        "type": "stdio", "command": sys.executable,
        "args": [str(bridge), "--name", "aitheros-run"],
    }}}


def clean_subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for spawned subagents with sensitive variables stripped.

    The subagent authenticates to Claude via its config home (CLAUDE_CONFIG_DIR
    or the default), never via env vars — so no *TOKEN/*SECRET/*KEY/*PASSW*
    style variable is ever passed through.
    """
    src = dict(os.environ) if base is None else dict(base)
    return {k: v for k, v in src.items() if not _SENSITIVE_ENV_RE.search(k)}


def restrict_file(path: Path) -> None:
    """Best-effort owner-only permissions: chmod 0600 + icacls on Windows."""
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if sys.platform == "win32":
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
            )


def redact(text: str | None) -> str | None:
    """Redact known secret patterns from text (for records and API responses)."""
    if not text:
        return text
    out = text
    for rx in _SECRET_RES:
        out = rx.sub("[REDACTED]", out)
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _allowed_gateway_hosts() -> set[str]:
    raw = os.environ.get("AITHER_CLAUDE_RUNNER_GATEWAY_HOSTS", DEFAULT_GATEWAY_HOSTS)
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


# ---------------------------------------------------------------------------
# The runner-side tool ceiling
# ---------------------------------------------------------------------------
#
# `allowed_tools` comes from the CALLER, and a fleet-originated spawn attaches the
# aitheros stdio bridge -- run 4f410016 saw 1366 platform tools. A caller that writes
# `mcp__aitheros__*` (the ordinary way to say "give it the platform") has thereby also
# handed that run set_secret, stripe_admin_*, and hetzner_destroy_server. Nothing
# refused it, because an allowlist of one wildcard cannot express "everything except
# the tools that spend money".
#
# So the ceiling is enforced HERE, on the DENY side, independent of what the caller
# asked for, and it reaches the child through BOTH `--disallowedTools` and
# settings.json permissions.deny -- two paths, so one silently-unsupported spelling
# cannot make it inert.
#
# The enumeration is server-PREFIXED, which is only sound because the set of server
# names is closed: `_validate_mcp` refuses any mcp_config naming a server outside
# _CEILING_SERVERS unless allow_dangerous. Without that arm a caller would bypass
# every rule below by attaching the same gateway under a different name.
#
# LIMIT, stated plainly rather than implied away: this bounds the MCP surface, not
# the machine. A scope that allows `Bash` can reach podman, gh, or a curl at the
# vault regardless of every pattern below -- Bash in a subagent scope IS the
# dangerous grant. These rules stop the accidental reach, not a determined one.
_CEILING_SERVERS: tuple[str, ...] = ("aitheros",)

_CEILING_TOOLS: tuple[str, ...] = (
    # -- secrets & credentials -------------------------------------------------
    # Reading one leaks it into a transcript this process does not control; writing
    # or deleting one can lock the fleet out of itself. secret-safety.md forbids the
    # VALUES reaching a transcript at all, which a spawned run cannot promise.
    "get_secret", "set_secret", "delete_secret", "list_secrets",
    "workspace_secrets_*", "lockbox_*",
    "create_api_key", "rotate_api_key", "revoke_api_key",
    "provision_gateway_key", "provision_room_gateway_key",
    "mint_node_token", "dev_generate_hmac",
    "set_cloud_provider_key", "remove_cloud_provider_key",
    "formbridge_vault_*", "secretguard_purge",
    # -- money -----------------------------------------------------------------
    # These move real funds or rent real hardware by the hour -- B2 in CLAUDE.md's
    # blocker taxonomy, the one class an agent may not self-serve.
    "stripe_admin_*", "commerce_refund", "commerce_create_checkout",
    "commerce_cancel_subscription", "commerce_payouts", "create_checkout",
    "x402_pay", "wallet_spend", "transfer_tokens", "marketplace_purchase",
    "launch_gpu_instance", "gpu_cluster_provision", "hetzner_provision_server",
    "aither_gpu_deploy_model", "aither_gpu_scale_deployment",
    "deploy_cloud_*", "vast_selfserve",
    # -- destructive fleet ops -------------------------------------------------
    # Not undoable by a command the run can also run: they destroy hosts, retire the
    # DNS/tunnel records the whole platform routes through, or promote an image onto
    # a live tag.
    "hetzner_destroy_server", "hetzner_reboot_server",
    "gpu_cluster_teardown", "teardown_cloud_*", "aither_gpu_destroy_deployment",
    "ring_deploy", "ring_full_deploy", "ring_promote", "ring_rollback",
    "ring_set_image",
    "k3s_deploy", "k3s_node_drain", "k3s_rollback",
    "dgx_stop_containers", "restart_dgx_service",
    "remove_cluster_node", "mesh_node_offboard",
    "cf_dns_delete", "cf_dns_upsert", "cf_worker_deploy",
    "cloudflare_dns_remove", "cloudflare_tunnel_remove",
    "git_push", "fs_delete",
    "dev_delete_user", "dev_suspend_user", "revoke_wallet",
    "delete_workflow", "delete_spec", "formbridge_purge",
)


def ceiling_deny_rules() -> list[str]:
    """Deny entries every run carries unless its scope declares allow_dangerous."""
    return [
        "mcp__{}__{}".format(server, tool)
        for server in _CEILING_SERVERS
        for tool in _CEILING_TOOLS
    ]


# ---------------------------------------------------------------------------
# Scope — pre-compiled by the caller, validated and enforced here
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunScope:
    """A pre-compiled, validated execution scope for one subagent run."""

    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    mcp_config: Optional[dict[str, Any]] = None
    system_prompt: str = ""
    model: str = ""
    cwd: str = ""
    add_dirs: list[str] = field(default_factory=list)
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    budget_usd: float = 0.0
    account_profile: str = ""
    setting_sources: str = DEFAULT_SETTING_SOURCES
    resume_session_id: str = ""
    allow_dangerous: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunScope":
        if not isinstance(data, dict):
            raise ScopeError("scope must be an object")
        scope = cls(
            allowed_tools=list(data.get("allowed_tools") or []),
            disallowed_tools=list(data.get("disallowed_tools") or []),
            mcp_config=data.get("mcp_config"),
            system_prompt=str(data.get("system_prompt") or ""),
            model=str(data.get("model") or ""),
            cwd=str(data.get("cwd") or ""),
            add_dirs=list(data.get("add_dirs") or []),
            timeout_sec=int(data.get("timeout_sec") or DEFAULT_TIMEOUT_SEC),
            budget_usd=float(data.get("budget_usd") or 0.0),
            account_profile=str(data.get("account_profile") or ""),
            setting_sources=str(data.get("setting_sources") or DEFAULT_SETTING_SOURCES),
            resume_session_id=str(data.get("resume_session_id") or ""),
            allow_dangerous=bool(data.get("allow_dangerous") or False),
        )
        scope.validate()
        return scope

    def validate(self) -> None:
        """Fail-closed validation. Raises ScopeError on ANY doubt."""
        if not self.allowed_tools or not all(
            isinstance(t, str) and t.strip() for t in self.allowed_tools
        ):
            raise ScopeError("scope.allowed_tools must be a non-empty list of tool names")
        for tool in self.allowed_tools + self.disallowed_tools:
            if any(ch in tool for ch in ";|&`$\n\r"):
                raise ScopeError(f"illegal characters in tool name: {tool!r}")
        if self.timeout_sec < 5 or self.timeout_sec > MAX_TIMEOUT_SEC:
            raise ScopeError(
                f"scope.timeout_sec must be 5..{MAX_TIMEOUT_SEC}, got {self.timeout_sec}"
            )
        if self.budget_usd < 0:
            raise ScopeError("scope.budget_usd must be >= 0")
        if self.cwd:
            cwd = Path(self.cwd)
            if not cwd.is_absolute() or not cwd.is_dir():
                raise ScopeError(f"scope.cwd must be an existing absolute directory: {self.cwd}")
        for d in self.add_dirs:
            p = Path(d)
            if not p.is_absolute() or not p.is_dir():
                raise ScopeError(f"scope.add_dirs entries must exist and be absolute: {d}")
        if self.account_profile and not self.account_profile.isidentifier():
            raise ScopeError(f"scope.account_profile must be an identifier: {self.account_profile}")
        self._validate_resume()
        self._validate_mcp()
        self._apply_ceiling()

    def _apply_ceiling(self) -> None:
        """Merge the runner-side ceiling into this scope's deny list.

        Runs LAST in validate() so the caller's own entries are already checked, and
        so nothing the caller writes can remove one of ours -- the merge is additive
        and de-duplicated, never a replacement.
        """
        if self.allow_dangerous:
            return
        existing = set(self.disallowed_tools)
        for rule in ceiling_deny_rules():
            if rule not in existing:
                self.disallowed_tools.append(rule)
                existing.add(rule)

    def _validate_resume(self) -> None:
        """Fail-closed rules for continuing an existing conversation.

        `claude --resume <id>` resolves the transcript inside the project derived
        from the process CWD (~/.claude/projects/<slug-of-cwd>/<id>.jsonl) and
        inside whatever CLAUDE_CONFIG_DIR points at. Both of those are things this
        runner sets, so a resume that gets either wrong does NOT fail loudly — it
        starts a brand-new conversation that merely looks resumed. Refuse instead.
        """
        if not self.resume_session_id:
            return
        try:
            uuid.UUID(self.resume_session_id)
        except (ValueError, AttributeError, TypeError):
            raise ScopeError(
                f"scope.resume_session_id must be a UUID: {self.resume_session_id!r}"
            ) from None
        if not self.cwd:
            # Without cwd the run executes in the throwaway per-run workdir, whose
            # project slug holds no sessions — the resume would silently start fresh.
            raise ScopeError(
                "scope.cwd is required when resuming: the session is resolved "
                "relative to the working directory it was created in"
            )
        if self.account_profile:
            # A profile redirects CLAUDE_CONFIG_DIR to an isolated per-run home
            # that contains no prior sessions, so the id would never be found.
            raise ScopeError(
                "scope.account_profile cannot be combined with resume_session_id: "
                "an isolated account home holds none of the session history"
            )

    def _validate_mcp(self) -> None:
        if self.mcp_config is None:
            return
        if not isinstance(self.mcp_config, dict):
            raise ScopeError("scope.mcp_config must be an object")
        servers = self.mcp_config.get("mcpServers")
        if not isinstance(servers, dict):
            raise ScopeError('scope.mcp_config must contain an "mcpServers" object')
        allowed_hosts = _allowed_gateway_hosts()
        for name, server in servers.items():
            if not isinstance(server, dict):
                raise ScopeError(f"mcp server {name!r} must be an object")
            if not self.allow_dangerous and name not in _CEILING_SERVERS:
                # The ceiling's deny rules are server-PREFIXED (mcp__aitheros__x), so
                # they are only complete while the set of server names is closed.
                # Attaching the same gateway as "gw" would otherwise expose every
                # tool the ceiling names, with nothing to say it had happened.
                raise ScopeError(
                    f"mcp server {name!r}: name not in {sorted(_CEILING_SERVERS)} "
                    "(the tool ceiling is keyed on the server name; "
                    "set scope.allow_dangerous to attach another)"
                )
            stype = str(server.get("type") or "").lower()
            if stype not in ("http", "sse"):
                # stdio servers execute arbitrary local commands — never allowed
                # from a submitted scope.
                raise ScopeError(f"mcp server {name!r}: only http/sse servers are allowed")
            url = str(server.get("url") or "")
            parsed = urlparse(url)
            if parsed.scheme not in ("https", "http"):
                raise ScopeError(f"mcp server {name!r}: url must be http(s), got {url!r}")
            host = (parsed.hostname or "").lower()
            if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1"):
                raise ScopeError(f"mcp server {name!r}: plain http only allowed to localhost")
            if host not in allowed_hosts:
                raise ScopeError(
                    f"mcp server {name!r}: host {host!r} not in allowed gateway hosts"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_tools": self.allowed_tools,
            "disallowed_tools": self.disallowed_tools,
            "mcp_config": self.mcp_config,
            "system_prompt": self.system_prompt,
            "model": self.model,
            "cwd": self.cwd,
            "add_dirs": self.add_dirs,
            "timeout_sec": self.timeout_sec,
            "budget_usd": self.budget_usd,
            "account_profile": self.account_profile,
            "setting_sources": self.setting_sources,
            "resume_session_id": self.resume_session_id,
            "allow_dangerous": self.allow_dangerous,
        }


# ---------------------------------------------------------------------------
# Run records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunRecord:
    """Durable state of one subagent run (persisted as record.json)."""

    run_id: str
    goal_id: str = ""
    status: str = "queued"  # queued|running|completed|failed|cancelled
    task_redacted: str = ""
    scope: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    pid: int = 0
    exit_code: Optional[int] = None
    result_text: str = ""
    result_subtype: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    num_turns: int = 0
    error: str = ""
    created_at: str = field(default_factory=_now_iso)
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal_id": self.goal_id,
            "status": self.status,
            "task_redacted": self.task_redacted,
            "scope": self.scope,
            "session_id": self.session_id,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "result_text": self.result_text,
            "result_subtype": self.result_subtype,
            "usage": self.usage,
            "total_cost_usd": self.total_cost_usd,
            "num_turns": self.num_turns,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        rec = cls(run_id=str(data.get("run_id") or ""))
        for key in (
            "goal_id", "status", "task_redacted", "session_id", "result_text",
            "result_subtype", "error", "created_at", "started_at", "completed_at",
        ):
            setattr(rec, key, str(data.get(key) or ""))
        rec.scope = dict(data.get("scope") or {})
        rec.usage = dict(data.get("usage") or {})
        rec.pid = int(data.get("pid") or 0)
        rec.exit_code = data.get("exit_code")
        rec.total_cost_usd = float(data.get("total_cost_usd") or 0.0)
        rec.num_turns = int(data.get("num_turns") or 0)
        return rec


class RunStore:
    """Disk-backed run records under <root>/runs/<run_id>/."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runs_dir = root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        # run_id is always a server-minted uuid4 — validate anyway so a crafted
        # id can never traverse outside the runs root.
        if not re.fullmatch(r"[0-9a-f\-]{36}", run_id):
            raise RunnerError(f"invalid run id: {run_id!r}")
        return self.runs_dir / run_id

    def save(self, rec: RunRecord) -> None:
        d = self.run_dir(rec.run_id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "record.json.tmp"
        tmp.write_text(json.dumps(rec.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, d / "record.json")

    def load(self, run_id: str) -> RunRecord:
        path = self.run_dir(run_id) / "record.json"
        if not path.exists():
            raise RunnerError(f"run not found: {run_id}")
        return RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[RunRecord]:
        records = []
        for rec_path in sorted(self.runs_dir.glob("*/record.json")):
            try:
                records.append(RunRecord.from_dict(json.loads(rec_path.read_text("utf-8"))))
            except (json.JSONDecodeError, OSError):
                _log.warning("Skipping malformed run record", extra={"path": str(rec_path)})
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def stream_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "stream.jsonl"


# ---------------------------------------------------------------------------
# Runner core
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True,
            )
        except OSError:
            # Cannot determine — assume alive so recovery never falsely
            # orphans a run that is still working.
            return True
        return str(pid) in (out.stdout or "")
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ClaudeRunner:
    """Spawns and tracks scoped headless claude runs."""

    def __init__(
        self,
        root: Optional[Path] = None,
        max_concurrency: Optional[int] = None,
        queue_max: Optional[int] = None,
        claude_bin: Optional[str] = None,
    ) -> None:
        env_root = os.environ.get("AITHER_CLAUDE_RUNNER_ROOT", "")
        self.root = (
            Path(env_root) if env_root else (root or Path.home() / ".aither" / "claude-runner")
        )
        self.store = RunStore(self.root)
        self.max_concurrency = max_concurrency or int(
            os.environ.get("AITHER_CLAUDE_RUNNER_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)
        )
        self.queue_max = queue_max or int(
            os.environ.get("AITHER_CLAUDE_RUNNER_QUEUE_MAX", DEFAULT_QUEUE_MAX)
        )
        raw_bin = claude_bin or os.environ.get("AITHER_CLAUDE_RUNNER_BIN", "claude")
        # shutil.which honors PATHEXT — required on Windows, where the claude
        # CLI is an npm .cmd shim that bare CreateProcess cannot find.
        self.claude_bin = shutil.which(raw_bin) or raw_bin
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()
        # In-memory active/queued sets so counts() never re-scans the disk
        # (health probes with a large run history were an O(n)-reads vector).
        self._active_ids: set[str] = set()
        self._queued_ids: set[str] = set()
        self._recover_orphans()
        for rec in self.store.list():
            if rec.status == "queued":
                self._queued_ids.add(rec.run_id)
            elif rec.status == "running":
                self._active_ids.add(rec.run_id)

    # -- lifecycle ---------------------------------------------------------

    def _recover_orphans(self) -> None:
        """On startup, mark runs recorded as active-but-dead as failed."""
        for rec in self.store.list():
            if rec.status in ("running", "queued") and not _pid_alive(rec.pid):
                rec.status = "failed"
                rec.error = rec.error or "orphaned: daemon restarted while run was active"
                rec.completed_at = rec.completed_at or _now_iso()
                self.store.save(rec)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {"active": len(self._active_ids), "queued": len(self._queued_ids)}

    def submit(self, task: str, scope: RunScope, goal_id: str = "") -> RunRecord:
        """Validate, persist, and (slot permitting) start a run. Fail-closed."""
        if not task or not task.strip():
            raise ScopeError("task must be a non-empty string")
        scope.validate()
        self._autoselect_account(scope)
        with self._lock:
            counts = self.counts()
            if counts["active"] + counts["queued"] >= self.max_concurrency + self.queue_max:
                raise QueueFullError(
                    f"runner at capacity ({counts['active']} active, {counts['queued']} queued)"
                )
            rec = RunRecord(
                run_id=str(uuid.uuid4()),
                goal_id=goal_id,
                task_redacted=(redact(task) or "")[:4000],
                scope=scope.to_dict(),
                # Resuming threads the SAME conversation, so the record must carry
                # the existing id — a fresh uuid here would make every follow-up
                # look like a separate run of an unrelated session.
                session_id=scope.resume_session_id or str(uuid.uuid4()),
            )
            self.store.save(rec)
            self._queued_ids.add(rec.run_id)
            task_path = self.store.run_dir(rec.run_id) / "task.txt"
            task_path.write_text(task, encoding="utf-8")
            restrict_file(task_path)
            if counts["active"] < self.max_concurrency:
                try:
                    self._start(rec, scope)
                except (OSError, RuntimeError) as e:
                    # Spawn/account failures become an honest failed record, not
                    # a 500 with a phantom queued run left behind. Exception text
                    # can carry credentials (account/store errors) — redact it.
                    self._mark_failed(rec, redact(f"spawn failed: {e}") or "spawn failed")
            return rec

    def _autoselect_account(self, scope: RunScope) -> None:
        """No explicit account_profile → pick one via the usage scheduler
        (least-loaded + round-robin over saved accounts) so subagent fan-out
        spans accounts instead of hammering one toward its limit.

        Best-effort load-balancing: no saved profiles, all in cooldown, an
        invalid selection, or ANY error → leave account_profile empty, which
        falls back to the default login. It NEVER fails the spawn on account
        selection (a spawn shouldn't die because usage bookkeeping hiccupped).
        """
        if scope.account_profile:
            return
        if scope.resume_session_id:
            # Auto-selecting here would redirect CLAUDE_CONFIG_DIR to an isolated
            # home with no session history, turning the resume into a silent
            # fresh start. The default login owns the transcript — keep it.
            return
        try:
            from adk.claude_account_usage import UsageMonitor, UsageMonitorError
            from adk.claude_accounts import ClaudeAccountStore

            names = [p.name for p in ClaudeAccountStore().list_profiles()]
            if not names:
                return
            try:
                selected = UsageMonitor(root=self.store.root.parent).select_account(names)
            except UsageMonitorError:
                return  # all in cooldown → default login
            if selected and selected.isidentifier():
                scope.account_profile = selected
        except Exception as e:  # noqa: BLE001 — account auto-select is best-effort
            _log.debug("account auto-select skipped: %s", e)

    def _mark_failed(self, rec: RunRecord, error: str) -> None:
        with self._lock:
            rec.status = "failed"
            rec.error = error
            rec.completed_at = _now_iso()
            self.store.save(rec)
            self._queued_ids.discard(rec.run_id)
            self._active_ids.discard(rec.run_id)

    def _pump_queue(self) -> None:
        with self._lock:
            counts = self.counts()
            slots = self.max_concurrency - counts["active"]
            if slots <= 0:
                return
            queued = [r for r in reversed(self.store.list()) if r.status == "queued"]
            for rec in queued[:slots]:
                try:
                    scope = RunScope.from_dict(rec.scope)
                    self._start(rec, scope)
                except (ScopeError, RunnerError, OSError) as e:
                    self._mark_failed(
                        rec, redact(f"failed to start queued run: {e}") or "failed to start"
                    )

    # -- workspace + spawn -------------------------------------------------

    def _build_workspace(self, rec: RunRecord, scope: RunScope) -> tuple[Path, Path, Path]:
        run_dir = self.store.run_dir(rec.run_id)
        workdir = run_dir / "work"
        workdir.mkdir(parents=True, exist_ok=True)

        settings = {
            "permissions": {
                "allow": scope.allowed_tools,
                "deny": scope.disallowed_tools,
            }
        }
        settings_path = run_dir / "settings.json"
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

        # Always write an MCP config and pass --strict-mcp-config so the run can
        # NEVER inherit MCP servers from user/project config. Empty by default.
        mcp_path = run_dir / "mcp.json"
        mcp_path.write_text(
            json.dumps(scope.mcp_config or _default_mcp_config(), indent=2), encoding="utf-8"
        )
        return run_dir, workdir, settings_path

    def _build_account_env(self, rec: RunRecord, scope: RunScope) -> dict[str, str]:
        # Sensitive vars (the daemon's own Bearer token above all) are stripped
        # so a prompt-injected subagent can never call back into this API.
        env = clean_subprocess_env()
        if not scope.account_profile:
            return env
        try:
            from adk.claude_accounts import ClaudeAccountStore  # lazy: client paths skip it
        except ImportError as e:  # pragma: no cover — packaged together
            raise RunnerError(f"adk.claude_accounts unavailable: {e}") from e

        store = ClaudeAccountStore()
        profile = store.get_profile(scope.account_profile)  # raises if missing
        config_home = self.store.run_dir(rec.run_id) / "claude-home"
        config_home.mkdir(parents=True, exist_ok=True)
        creds_path = config_home / ".credentials.json"
        creds_path.write_text(json.dumps(profile.credentials, indent=2), encoding="utf-8")
        restrict_file(creds_path)
        env["CLAUDE_CONFIG_DIR"] = str(config_home)
        return env

    def _build_argv(self, rec: RunRecord, scope: RunScope, run_dir: Path) -> list[str]:
        argv = [
            self.claude_bin,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--settings", str(run_dir / "settings.json"),
            "--setting-sources", scope.setting_sources,
            "--allowedTools", ",".join(scope.allowed_tools),
            "--mcp-config", str(run_dir / "mcp.json"),
            "--strict-mcp-config",
        ]
        # --session-id CREATES a conversation with that id and rejects an id that
        # already exists; --resume CONTINUES one. They are mutually exclusive.
        if scope.resume_session_id:
            argv += ["--resume", rec.session_id]
        else:
            argv += ["--session-id", rec.session_id]
        if scope.disallowed_tools:
            argv += ["--disallowedTools", ",".join(scope.disallowed_tools)]
        if scope.system_prompt:
            argv += ["--append-system-prompt", scope.system_prompt]
        if scope.model:
            argv += ["--model", scope.model]
        if scope.budget_usd > 0:
            argv += ["--max-budget-usd", str(scope.budget_usd)]
        for d in scope.add_dirs:
            argv += ["--add-dir", d]
        return argv

    def _start(self, rec: RunRecord, scope: RunScope) -> None:
        run_dir, workdir, _ = self._build_workspace(rec, scope)
        env = self._build_account_env(rec, scope)
        argv = self._build_argv(rec, scope, run_dir)
        task = (run_dir / "task.txt").read_text(encoding="utf-8")

        stream_f = open(self.store.stream_path(rec.run_id), "ab")  # noqa: SIM115
        stderr_f = (run_dir / "stderr.log").open("ab")
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": stream_f,
            "stderr": stderr_f,
            "cwd": scope.cwd or str(workdir),
            "env": env,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = _WIN_FLAGS
        else:
            kwargs["start_new_session"] = True

        argv_path = run_dir / "argv.txt"
        argv_path.write_text(shlex.join(argv), encoding="utf-8")
        restrict_file(argv_path)
        try:
            proc = subprocess.Popen(argv, **kwargs)
        finally:
            # The child holds its own duplicated handles; release the parent's
            # copies so run files stay deletable on Windows.
            stream_f.close()
            stderr_f.close()
        if proc.stdin is None:  # pragma: no cover — PIPE guarantees a stdin
            raise RunnerError("subprocess stdin unavailable")
        proc.stdin.write(task.encode("utf-8"))
        proc.stdin.close()

        with self._lock:
            rec.status = "running"
            rec.pid = proc.pid
            rec.started_at = _now_iso()
            self.store.save(rec)
            self._queued_ids.discard(rec.run_id)
            self._active_ids.add(rec.run_id)
            self._procs[rec.run_id] = proc

        watcher = threading.Thread(
            target=self._watch, args=(rec.run_id, proc, scope.timeout_sec), daemon=True
        )
        watcher.start()

    def _watch(self, run_id: str, proc: subprocess.Popen, timeout_sec: int) -> None:
        timed_out = False
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _log.error("Run process did not die after kill", extra={"run_id": run_id})
        finally:
            with self._lock:
                self._procs.pop(run_id, None)
            try:
                self._finalize(run_id, proc.returncode, timed_out)
            except (RunnerError, OSError, json.JSONDecodeError) as e:
                _log.error("Failed to finalize run", extra={"run_id": run_id, "err": str(e)})
            self._pump_queue()

    def _finalize(self, run_id: str, exit_code: Optional[int], timed_out: bool) -> None:
        # Parse outside the lock (disk I/O), commit the record inside it so a
        # concurrent kill() can never interleave with our load-modify-save.
        result = self._parse_result(run_id)
        rate_limited = self._check_rate_limit_error(run_id)
        with self._lock:
            rec = self.store.load(run_id)
            self._active_ids.discard(run_id)
            self._queued_ids.discard(run_id)
            if rec.status == "cancelled":
                return
            rec.exit_code = exit_code
            rec.completed_at = _now_iso()

            if result:
                rec.result_subtype = str(result.get("subtype") or "")
                rec.result_text = (redact(str(result.get("result") or "")) or "")[:16000]
                rec.usage = dict(result.get("usage") or {})
                rec.total_cost_usd = float(result.get("total_cost_usd") or 0.0)
                rec.num_turns = int(result.get("num_turns") or 0)

            if timed_out:
                rec.status = "failed"
                rec.error = f"timed out after {rec.scope.get('timeout_sec')}s"
            elif rate_limited:
                rec.status = "failed"
                rec.error = "rate_limited (429): quota exceeded, try again later"
            elif exit_code == 0 and result and not result.get("is_error"):
                rec.status = "completed"
            else:
                rec.status = "failed"
                rec.error = rec.error or (
                    f"exit_code={exit_code}, subtype={rec.result_subtype or 'no result event'}"
                )
            self.store.save(rec)

            # Record to selfgraph if a parent run is active (best-effort)
            self._record_selfgraph(rec)

            # Record usage and handle rate-limit cooldown.
            self._record_usage(rec, rate_limited)

        # Outside the lock: a slow or unreachable push must never hold up a
        # watcher thread that other runs are queued behind.
        self._notify_terminal(rec)

    def _notify_terminal(self, rec: RunRecord) -> None:
        """Push a run's terminal state to an ntfy topic. Best-effort, never raises.

        This is what makes the phone loop work: you fire a run and walk away
        instead of watching a terminal. The message carries `session_id` because
        that is what the next prompt must pass as `resume_session_id` to continue
        the same conversation.
        """
        url = os.environ.get("AITHER_CLAUDE_RUNNER_NTFY_URL", "").strip()
        if not url:
            return
        threading.Thread(target=self._post_ntfy, args=(url, rec), daemon=True).start()

    def _post_ntfy(self, url: str, rec: RunRecord) -> None:
        import urllib.error
        import urllib.request

        ok = rec.status == "completed"
        # result_text/error are redacted upstream; re-redact defensively because
        # this leaves the host.
        body = (redact(rec.result_text or rec.error or "(no output)") or "")[:NTFY_BODY_CHARS]
        cwd = str(rec.scope.get("cwd") or "") or "(isolated workdir)"
        text = (
            f"{body}\n\n"
            f"— {Path(cwd).name or cwd} · {rec.num_turns} turns · ${rec.total_cost_usd:.3f}\n"
            f"resume: {rec.session_id}\n"
            f"run: {rec.run_id}"
        )
        req = urllib.request.Request(
            url,
            data=text.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Title": f"Claude run {rec.status}: {Path(cwd).name or 'run'}",
                "Priority": "default" if ok else "high",
                "Tags": "white_check_mark" if ok else "rotating_light",
            },
        )
        token = os.environ.get("AITHER_CLAUDE_RUNNER_NTFY_TOKEN", "").strip()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT_SEC) as resp:
                if resp.status >= 400:
                    _log.error(
                        "ntfy push failed — owner NOT notified",
                        extra={"run_id": rec.run_id, "status": resp.status},
                    )
        except (urllib.error.URLError, OSError, ValueError) as e:
            # Never re-raise and never log the URL: the topic is the credential.
            _log.error(
                "ntfy push errored — owner NOT notified",
                extra={"run_id": rec.run_id, "err": type(e).__name__},
            )

    def _parse_result(self, run_id: str) -> Optional[dict[str, Any]]:
        path = self.store.stream_path(run_id)
        if not path.exists():
            return None
        result = None
        bad_lines = 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"result"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    bad_lines += 1
                    continue
                if obj.get("type") == "result":
                    result = obj
        if bad_lines:
            _log.warning(
                "Unparseable stream-json lines skipped",
                extra={"run_id": run_id, "bad_lines": bad_lines},
            )
        return result

    def _check_rate_limit_error(self, run_id: str) -> bool:
        """Scan stream.jsonl for 429 rate-limit error events."""
        path = self.store.stream_path(run_id)
        if not path.exists():
            return False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or "429" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                        # Look for error events with 429 status.
                        if obj.get("type") == "error" and obj.get("error", {}).get("status") == 429:
                            return True
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return False

    def _record_selfgraph(self, rec: RunRecord) -> None:
        """Record subagent run to selfgraph (best-effort, never blocks or raises).

        Records:
        - RUN node with scope summary (redacted)
        - ARTIFACT for the result
        - EVALUATION with RUBRIC if the run failed
        - Parent-of link if a parent recorder is active
        """
        if not _SELFGRAPH_AVAILABLE:
            return

        parent_recorder = None
        try:
            parent_recorder = current_run()
        except Exception:  # noqa: BLE001 — best-effort
            pass

        if not parent_recorder:
            return  # No active parent run; nothing to record to

        try:
            # Scope summary: tool allowlist + model + cwd (secrets redacted)
            scope_props = {
                "allowed_tools": rec.scope.get("allowed_tools", []),
                "disallowed_tools": rec.scope.get("disallowed_tools", []),
                "model": rec.scope.get("model", ""),
                "timeout_sec": rec.scope.get("timeout_sec", 0),
            }
            # Redact any secrets in cwd path or system_prompt
            if rec.scope.get("cwd"):
                scope_props["cwd"] = redact(rec.scope.get("cwd", "")) or ""
            if rec.scope.get("system_prompt"):
                scope_props["system_prompt"] = redact(rec.scope.get("system_prompt", "")) or ""

            # Create RUN node for subagent
            run_id = f"subagent-{rec.run_id[:16]}"
            run_node = ProvNodeType.RUN
            run_props = {
                "subagent_run_id": rec.run_id,
                "session_id": rec.session_id,
                "scope": scope_props,
                "status": rec.status,
                "exit_code": rec.exit_code,
                "num_turns": rec.num_turns,
                "total_cost_usd": rec.total_cost_usd,
            }
            parent_recorder.artifact(
                run_id,
                version=1,
                properties=run_props,
            )

            # If there's a result, record it as an artifact
            if rec.result_text:
                import hashlib
                content_hash = hashlib.sha256(rec.result_text.encode()).hexdigest()
                parent_recorder.artifact(
                    f"subagent-result-{rec.run_id[:12]}",
                    version=1,
                    content_hash=content_hash,
                    properties={
                        "subagent_run_id": rec.run_id,
                        "result_subtype": rec.result_subtype,
                    },
                )

            # If failed, record an evaluation
            if rec.status == "failed":
                # Create a RUBRIC node if it doesn't exist
                rubric_id = make_node_id(
                    ProvNodeType.RUBRIC,
                    "subagent-run",
                    tenant_id="",
                )
                parent_recorder.rubric(
                    "subagent-run",
                    criteria={
                        "exit_code": "Process exit code (0 = success)",
                        "error_message": "Error description from runner",
                        "rate_limited": "429 rate limit error flag",
                    },
                )
                parent_recorder.evaluation(
                    rec.error or "failed",
                    rubric_id=rubric_id,
                    target_id=make_node_id(
                        ProvNodeType.ARTIFACT,
                        run_id,
                        tenant_id="",
                    ),
                    reason=(redact(rec.error) or "unknown error")[:200],
                    score=0.0,
                )

            parent_recorder.flush()
        except Exception:  # noqa: BLE001 — selfgraph recording is best-effort
            pass

    def _record_usage(self, rec: RunRecord, rate_limited: bool) -> None:
        """Record usage to the multi-account monitor and handle rate-limit cooldown.

        Safely handles errors: if usage monitor fails, logs warning but doesn't
        fail the run (usage tracking is auxiliary).
        """
        profile_name = rec.scope.get("account_profile", "")
        if not profile_name:
            return  # No account specified, skip usage tracking.

        try:
            from adk.claude_account_usage import UsageMonitor, UsageMonitorError
        except ImportError:
            # Usage monitor not available; skip (shouldn't happen in normal flow).
            return

        try:
            monitor = UsageMonitor(root=self.store.root.parent)
            usage_dict = rec.usage or {}
            input_tokens = int(usage_dict.get("input_tokens", 0))
            output_tokens = int(usage_dict.get("output_tokens", 0))
            total_cost = rec.total_cost_usd

            if rate_limited:
                # Mark profile in cooldown.
                monitor.mark_rate_limited(profile_name)
                _log.info(
                    "Marked account in rate-limit cooldown",
                    extra={"profile": profile_name},
                )
            elif rec.status == "completed" and (input_tokens > 0 or output_tokens > 0):
                # Record successful run usage.
                monitor.record_run(
                    profile_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_cost_usd=total_cost,
                )
        except (UsageMonitorError, OSError) as e:
            # Log but don't fail the run; usage tracking is observability only.
            _log.warning(
                "Failed to record usage",
                extra={"profile": profile_name, "err": str(e)},
            )

    # -- queries / control -------------------------------------------------

    def get(self, run_id: str) -> RunRecord:
        return self.store.load(run_id)

    def list(self) -> list[RunRecord]:
        return self.store.list()

    def events(self, run_id: str, offset: int = 0) -> tuple[str, int]:
        """Return raw stream-json content from byte offset; (chunk, next_offset)."""
        self.store.load(run_id)  # 404 on unknown id
        path = self.store.stream_path(run_id)
        if not path.exists():
            return "", 0
        size = path.stat().st_size
        offset = max(0, min(offset, size))
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
        return chunk.decode("utf-8", errors="replace"), offset + len(chunk)

    def kill(self, run_id: str) -> RunRecord:
        with self._lock:
            rec = self.store.load(run_id)
            proc = self._procs.get(run_id)
            if rec.status in ("queued", "running"):
                rec.status = "cancelled"
                rec.completed_at = _now_iso()
                self.store.save(rec)
                self._queued_ids.discard(run_id)
                self._active_ids.discard(run_id)
            else:
                proc = None  # nothing to kill for terminal states
        if proc is not None:
            proc.kill()
        elif rec.status == "cancelled":
            if rec.pid and _pid_alive(rec.pid):
                # Daemon restarted; process survived from a prior life.
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(rec.pid), "/T", "/F"], capture_output=True
                    )
                else:
                    try:
                        os.kill(rec.pid, 9)
                    except OSError:
                        pass
        return self.store.load(run_id)


# ---------------------------------------------------------------------------
# HTTP daemon (FastAPI, Bearer auth, fail-closed)
# ---------------------------------------------------------------------------


def _runner_token_path() -> Path:
    """The shared token file both the daemon and a tokenless client read."""
    env_root = os.environ.get("AITHER_CLAUDE_RUNNER_ROOT", "")
    root = Path(env_root) if env_root else Path.home() / ".aither" / "claude-runner"
    return root / "token"


def sync_runner_token(token: str) -> None:
    """Write the daemon's resolved token to the shared file so a tokenless client
    (a plain shell with no AITHER_CLAUDE_RUNNER_TOKEN / AITHER_INTERNAL_SECRET)
    resolves the SAME value and authenticates.

    Called once when the daemon starts. Without it, a daemon that booted from the
    shared secret never syncs the file, and a client in a fresh shell falls back to
    whatever stale token the file already held — a 403 with no named cause. The
    value is never echoed; the file is owner-only.
    """
    if not token:
        return
    token_path = _runner_token_path()
    try:
        if token_path.exists() and token_path.read_text(encoding="utf-8").strip() == token:
            return
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token, encoding="utf-8")
        restrict_file(token_path)
    except OSError:
        # Best-effort sync; the daemon still serves with the resolved token.
        return


def resolve_token(explicit: str = "") -> str:
    """Resolve the daemon Bearer token; generate + persist one if none exists.

    Resolution order: explicit arg → AITHER_CLAUDE_RUNNER_TOKEN →
    AITHER_INTERNAL_SECRET → persisted token file → newly generated (persisted,
    printed once). The daemon NEVER runs tokenless.
    """
    token = (
        explicit
        or os.environ.get("AITHER_CLAUDE_RUNNER_TOKEN", "")
        or os.environ.get("AITHER_INTERNAL_SECRET", "")
    )
    if token:
        return token
    token_path = _runner_token_path()
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token = std_secrets.token_urlsafe(32)
    token_path.write_text(token, encoding="utf-8")
    restrict_file(token_path)
    # Never echo the token itself — stdout ends up in logs/CI output.
    print(f"Generated runner token; read it from {token_path}")
    return token


def _record_view(rec: RunRecord) -> dict[str, Any]:
    """API-safe view of a record (already-redacted fields only)."""
    view = rec.to_dict()
    # scope.mcp_config may embed gateway auth headers — never echo them back.
    scope = dict(view.get("scope") or {})
    if scope.get("mcp_config"):
        server_names = sorted((scope["mcp_config"].get("mcpServers") or {}).keys())
        scope["mcp_config"] = {"mcpServers": server_names}
    view["scope"] = scope
    return view


def create_app(runner: ClaudeRunner, token: str):
    """Build the FastAPI app. Token is REQUIRED — no tokenless mode exists."""
    from fastapi import Depends, FastAPI, Header, HTTPException

    if not token:
        raise RunnerError("refusing to build app without a Bearer token (fail-closed)")

    app = FastAPI(title="aither-claude-runner", docs_url=None, redoc_url=None)

    async def _auth(authorization: Optional[str] = Header(default=None)) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        supplied = authorization[len("Bearer "):].strip()
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(status_code=403, detail="invalid token")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        counts = runner.counts()
        return {
            "ok": True,
            "service": "aither-claude-runner",
            "max_concurrency": runner.max_concurrency,
            **counts,
        }

    @app.post("/runs", dependencies=[Depends(_auth)])
    async def create_run(body: dict[str, Any]) -> dict[str, Any]:
        task = str(body.get("task") or "")
        goal_id = str(body.get("goal_id") or "")
        try:
            scope = RunScope.from_dict(body.get("scope") or {})
            rec = runner.submit(task, scope, goal_id=goal_id)
        except ScopeError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except QueueFullError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return _record_view(rec)

    @app.get("/runs", dependencies=[Depends(_auth)])
    async def list_runs(goal_id: str | None = None) -> list[dict[str, Any]]:
        # ?goal_id= was accepted and silently IGNORED until 2026-09-02: the
        # first end-to-end proof of this lane had to filter 15 runs client-side
        # to find its own. An ignored filter reads as "no runs for that goal".
        views = [_record_view(r) for r in runner.list()]
        if goal_id:
            views = [v for v in views if v.get("goal_id") == goal_id]
        return views

    @app.get("/runs/{run_id}", dependencies=[Depends(_auth)])
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            return _record_view(runner.get(run_id))
        except RunnerError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/runs/{run_id}/events", dependencies=[Depends(_auth)])
    async def get_events(run_id: str, offset: int = 0) -> dict[str, Any]:
        try:
            chunk, next_offset = runner.events(run_id, offset)
        except RunnerError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"run_id": run_id, "offset": offset, "next_offset": next_offset,
                "chunk": redact(chunk)}

    @app.delete("/runs/{run_id}", dependencies=[Depends(_auth)])
    async def delete_run(run_id: str) -> dict[str, Any]:
        try:
            return _record_view(runner.kill(run_id))
        except RunnerError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    # -- session index (read-only) ----------------------------------------
    # What a remote client needs BEFORE it can resume anything: which
    # conversations exist, in which repo, and what was last said. The engine is
    # adk.shell.claude_sessions (already used by `adk shell sessions`); these
    # endpoints only expose it, they parse nothing themselves.

    @app.get("/sessions", dependencies=[Depends(_auth)])
    async def list_sessions(
        scan: int = 120, top: int = 50, q: str = "", cwd: str = ""
    ) -> dict[str, Any]:
        from adk.shell import claude_sessions as cs

        found = cs.scan_sessions(scan=scan, top=top, text_filter=q)
        if cwd:
            want = cs._norm_path(cwd)
            found = [s for s in found if cs._norm_path(s.cwd) == want]
        out = []
        for s in found:
            view = s.to_dict()
            # `file` is an absolute host path — useless to a remote client and
            # it leaks the local layout. `id` is what /runs needs.
            view.pop("file", None)
            view["age"] = s.age
            view["lastPrompt"] = redact(view.get("lastPrompt") or "")
            view["title"] = redact(view.get("title") or "")
            out.append(view)
        return {"sessions": out, "count": len(out)}

    @app.get("/sessions/{session_id}", dependencies=[Depends(_auth)])
    async def get_session(session_id: str, turns: int = 30) -> dict[str, Any]:
        from adk.shell import claude_sessions as cs

        try:
            uuid.UUID(session_id)
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(status_code=400, detail="session_id must be a UUID") from None
        # Resolve through the scanner rather than globbing a caller-supplied id
        # into a path — the id reaches the filesystem otherwise.
        match = next((s for s in cs.scan_sessions(scan=400, top=400) if s.id == session_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="session not found")
        transcript = [
            {"role": role, "text": redact(text)}
            for role, text in cs.session_transcript(match.file, max_turns=turns)
        ]
        view = match.to_dict()
        view.pop("file", None)
        view["age"] = match.age
        view["transcript"] = transcript
        return view

    return app


def serve(host: str = "", port: int = 0, token: str = "") -> int:
    """Run the daemon in the foreground (uvicorn)."""
    import uvicorn

    host = host or os.environ.get("AITHER_CLAUDE_RUNNER_HOST", DEFAULT_HOST)
    port = port or int(os.environ.get("AITHER_CLAUDE_RUNNER_PORT", DEFAULT_PORT))
    resolved = resolve_token(token)
    sync_runner_token(resolved)
    runner = ClaudeRunner()
    app = create_app(runner, resolved)
    _log.info("claude-runner serving", extra={"host": host, "port": port})
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


# ---------------------------------------------------------------------------
# Service registration (Windows scheduled task / Linux systemd)
# ---------------------------------------------------------------------------


def _get_wrapper_ps1_content(repo_root: str, host: str, port: int) -> str:
    """Generate Windows wrapper script content."""
    return f"""# Claude runner wrapper — sources AITHER_INTERNAL_SECRET from .env at runtime
# This script is called by the scheduled task and must NOT embed secrets.

$RepoRoot = {shlex.quote(repo_root)}
$Env:AITHER_CLAUDE_RUNNER_HOST = '{host}'
$Env:AITHER_CLAUDE_RUNNER_PORT = '{port}'

# Load AITHER_INTERNAL_SECRET from .env (fail-closed if missing)
$EnvFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $EnvFile)) {{
    [Console]::Error.WriteLine("ERROR: .env file not found at $EnvFile")
    Exit 1
}}

# Simple .env parser: KEY=VALUE lines only (no quotes, no comments).
# AITHER_INTERNAL_SECRET is REQUIRED (fail-closed below). The NTFY_* pair is
# OPTIONAL — absent simply means no push. They must be sourced here rather than
# baked into this file: the ntfy topic URL is itself the credential, and this
# wrapper is world-readable on disk. Do NOT `break` out of this loop after the
# secret; that is what previously made every other key unreachable.
$Wanted = @(
    "AITHER_INTERNAL_SECRET",
    "AITHER_CLAUDE_RUNNER_NTFY_URL",
    "AITHER_CLAUDE_RUNNER_NTFY_TOKEN"
)
$Lines = @(Get-Content $EnvFile -ErrorAction SilentlyContinue |
    Where-Object {{ $_.Trim() -and -not $_.TrimStart().StartsWith("#") }})
foreach ($Line in $Lines) {{
    $Parts = $Line -Split "=", 2
    if ($Parts.Count -eq 2) {{
        $Key = $Parts[0].Trim()
        $Value = $Parts[1].Trim()
        if ($Wanted -contains $Key -and $Value) {{
            Set-Item -Path "Env:$Key" -Value $Value
        }}
    }}
}}

if (-not $Env:AITHER_INTERNAL_SECRET) {{
    [Console]::Error.WriteLine("ERROR: AITHER_INTERNAL_SECRET not found in .env")
    Exit 1
}}

# Append output to log file
$LogDir = Join-Path $Env:USERPROFILE ".aither\\logs"
$LogFile = Join-Path $LogDir "claude-runner.log"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

# Check-then-serve (2026-09-02). The scheduled task re-runs this wrapper every
# 5 minutes; without this probe every tick tried to bind a port already served
# and logged Errno 10048 (256 times before it was noticed). A live runner means
# exit 0 quietly - the same guard lives in the hand-edited live wrapper, and
# this template is what regenerates it, so both must carry it.
try {{
    $Probe = Invoke-RestMethod -Uri "http://127.0.0.1:$Env:AITHER_CLAUDE_RUNNER_PORT/health" `
        -TimeoutSec 2
    if ($Probe.ok) {{
        $Stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Add-Content -Path $LogFile -Value ('{{"ts": "' + $Stamp + '", "level": "INFO", ' +
            '"logger": "claude-runner-wrapper", "msg": "runner already serving - exiting"}}')
        exit 0
    }}
}} catch {{ }}

# Stale-kill (2026-09-02): a runner can WEDGE - alive, port released, /health dead -
# and with MultipleInstances=IgnoreNew that instance kept the task "Running", so every
# 5-minute tick was ignored. If we got here, /health did not answer: any serve is a zombie.
Get-CimInstance Win32_Process |
    Where-Object {{ $_.Name -eq 'python.exe' -and
        $_.CommandLine -match 'adk[.]cli claude serve' }} |
    ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}

# Inherited NODE_EXTRA_CA_CERTS may point at a retired tree; repoint to the live CA or drop.
if ($Env:NODE_EXTRA_CA_CERTS -and -not (Test-Path $Env:NODE_EXTRA_CA_CERTS)) {{
    $Ca = 'C:\\AitherOS-Data\\Library\\Data\\tls\\ca-chain.pem'
    if (Test-Path $Ca) {{ $Env:NODE_EXTRA_CA_CERTS = $Ca }}
    else {{ Remove-Item Env:NODE_EXTRA_CA_CERTS -ErrorAction SilentlyContinue }}
}}

# Detached launch: the task instance ends immediately (IgnoreNew can never mask a wedge)
# and -u lands every log line as it is written instead of dying in a pipe buffer.
$Out = Join-Path $LogDir "claude-runner.stdout.log"
$Err = Join-Path $LogDir "claude-runner.stderr.log"
Start-Process -FilePath python -WindowStyle Hidden `
    -ArgumentList @('-u', '-m', 'adk.cli', 'claude', 'serve',
                    '--host', $Env:AITHER_CLAUDE_RUNNER_HOST,
                    '--port', $Env:AITHER_CLAUDE_RUNNER_PORT) `
    -RedirectStandardOutput $Out -RedirectStandardError $Err
$Stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Add-Content -Path $LogFile -Value ('{{"ts": "' + $Stamp + '", "level": "INFO", ' +
    '"logger": "claude-runner-wrapper", "msg": "launched claude serve detached", ' +
    '"stdout": "' + $Out + '"}}')
"""


def _get_wrapper_sh_content(repo_root: str, host: str, port: int) -> str:
    """Generate Linux/macOS wrapper script content."""
    return f"""#!/bin/bash
# Claude runner wrapper — sources AITHER_INTERNAL_SECRET from .env at runtime
# This script is called by systemd/launchd and must NOT embed secrets.

set -e
cd {shlex.quote(repo_root)}

# Load .env file (fail-closed if missing or unreadable)
EnvFile=".env"
if [ ! -f "$EnvFile" ]; then
    echo "ERROR: .env file not found at $(pwd)/$EnvFile" >&2
    exit 1
fi

# Source .env and validate AITHER_INTERNAL_SECRET is set and non-empty
set -a
source "$EnvFile"
set +a

if [ -z "$AITHER_INTERNAL_SECRET" ]; then
    echo "ERROR: AITHER_INTERNAL_SECRET not found or empty in .env" >&2
    exit 1
fi

# Ensure log directory exists
mkdir -p "$HOME/.aither/logs"

# Run daemon
export AITHER_CLAUDE_RUNNER_HOST={shlex.quote(host)}
export AITHER_CLAUDE_RUNNER_PORT={shlex.quote(str(port))}
exec python -m adk.cli claude serve
"""


def _get_systemd_unit_content(wrapper_path: str, repo_root: str) -> str:
    """Generate systemd --user unit file content."""
    return f"""[Unit]
Description=AitherOS Claude Runner (durable daemon)
After=network.target
Wants=network.target

[Service]
Type=simple
WorkingDirectory={repo_root}
ExecStart={wrapper_path}
Restart=on-failure
RestartSec=5s
StandardOutput=append:{Path.home() / '.aither/logs/claude-runner.log'}
StandardError=append:{Path.home() / '.aither/logs/claude-runner.log'}
KillMode=mixed

[Install]
WantedBy=default.target
"""


def _get_launchd_plist_content(wrapper_path: str, repo_root: str) -> str:
    """Generate macOS launchd plist content."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.aither.claude-runner</string>
    <key>ProgramArguments</key>
    <array>
        <string>{wrapper_path}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{repo_root}</string>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home() / '.aither/logs/claude-runner.log'}</string>
    <key>StandardErrorPath</key>
    <string>{Path.home() / '.aither/logs/claude-runner.log'}</string>
</dict>
</plist>
"""


def _find_repo_root() -> str:
    """Detect repository root by searching for .git or .env."""
    cwd = Path.cwd()
    for ancestor in [cwd] + list(cwd.parents):
        if (ancestor / ".git").exists() or (ancestor / ".env").exists():
            return str(ancestor)
    return str(cwd)


def cmd_register_claude(args: Any) -> int:
    """Register Claude runner as a durable service (Windows/Linux/macOS)."""
    host = getattr(args, "host", "") or "127.0.0.1"
    port = int(getattr(args, "port", 0) or 8365)
    force = getattr(args, "force", False)
    repo_root = _find_repo_root()

    # Validate that .env exists and is readable
    env_file = Path(repo_root) / ".env"
    if not env_file.exists():
        print(f"Error: .env not found in {repo_root}", file=sys.stderr)
        return 1

    try:
        _ = env_file.read_text(encoding="utf-8")
    except OSError as e:
        print(f"Error: cannot read .env: {e}", file=sys.stderr)
        return 1

    # Ensure log directory exists
    log_dir = Path.home() / ".aither" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        return _register_windows(repo_root, host, port, force)
    elif sys.platform == "darwin":
        return _register_macos(repo_root, host, port, force)
    else:
        return _register_linux(repo_root, host, port, force)


def _register_windows(repo_root: str, host: str, port: int, force: bool) -> int:
    """Register Windows scheduled task."""
    wrapper_dir = Path.home() / ".aither" / "bin"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = wrapper_dir / "claude-runner-wrapper.ps1"

    try:
        wrapper_path.write_text(_get_wrapper_ps1_content(repo_root, host, port), encoding="utf-8")
        restrict_file(wrapper_path)
    except OSError as e:
        print(f"Error: cannot write wrapper script: {e}", file=sys.stderr)
        return 1

    # Kill any existing listener on the target port (idempotence)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
             "Where-Object State -eq 'Listen' | Select-Object -ExpandProperty OwningProcess | "
             "ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Register scheduled task
    task_name = "AitherOS-ClaudeRunner"
    user = f"{os.environ.get('USERDOMAIN', 'localhost')}\\{os.environ.get('USERNAME', 'user')}"

    ps_script = f"""
$TaskName = '{task_name}'
$User = '{user}'
$ScriptPath = '{wrapper_path}'
$RepoRoot = '{repo_root}'

# Idempotent unregister
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Create action: run PowerShell with the wrapper script
$Action = New-ScheduledTaskAction `
    -Execute 'pwsh' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File '$ScriptPath'" `
    -WorkingDirectory '{repo_root}'

# Create triggers: AtLogOn (runs in the user's session, has ~/.claude) + 5-min heartbeat.
# NOTE: AtLogOn + Interactive (NOT AtStartup + S4U) — S4U/RunLevel-Highest registration
# hangs from a non-elevated shell waiting on a logon-rights/UAC check. Interactive runs in
# the owner's desktop session (where the Claude login lives) and still survives logoff+reboot.
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
$TriggerHeartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# Create principal: Interactive (owner's session) at RunLevel Limited (runner needs no admin)
$Principal = New-ScheduledTaskPrincipal `
    -UserID $User `
    -LogonType Interactive `
    -RunLevel Limited

# Create settings: multiple instances ignored, no timeout, auto-restart
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

# Register task
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $TriggerLogon, $TriggerHeartbeat `
    -Principal $Principal `
    -Settings $Settings `
    -Force `
    -Description @"
Claude runner: AtLogOn + 5-min heartbeat, Interactive principal, no embedded secrets
"@

# Start it immediately
Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Write-Host "Claude runner registered and started. Host: {host}, Port: {port}"
"""

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            print(f"Error: failed to register task: {result.stderr}", file=sys.stderr)
            return 1
        print(result.stdout)
        return 0
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Error: failed to register scheduled task: {e}", file=sys.stderr)
        return 1


def _register_linux(repo_root: str, host: str, port: int, force: bool) -> int:
    """Register Linux systemd --user unit."""
    wrapper_dir = Path.home() / ".aither" / "bin"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = wrapper_dir / "claude-runner-wrapper.sh"

    try:
        wrapper_path.write_text(_get_wrapper_sh_content(repo_root, host, port), encoding="utf-8")
        wrapper_path.chmod(0o755)
    except OSError as e:
        print(f"Error: cannot write wrapper script: {e}", file=sys.stderr)
        return 1

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "aither-claude-runner.service"

    try:
        unit_content = _get_systemd_unit_content(str(wrapper_path), repo_root)
        unit_path.write_text(unit_content, encoding="utf-8")
    except OSError as e:
        print(f"Error: cannot write systemd unit: {e}", file=sys.stderr)
        return 1

    # Reload, enable, and start
    commands = [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "aither-claude-runner.service"],
        ["systemctl", "--user", "start", "aither-claude-runner.service"],
        ["loginctl", "enable-linger"],  # Ensure user session persists
    ]

    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                _log.warning(f"Command failed: {' '.join(cmd)}: {result.stderr}")
        except (subprocess.TimeoutExpired, OSError) as e:
            _log.warning(f"Failed to run {cmd}: {e}")

    print(f"Claude runner registered with systemd --user. Host: {host}, Port: {port}")
    print("Note: User session will persist across logouts (linger enabled)")
    return 0


def _register_macos(repo_root: str, host: str, port: int, force: bool) -> int:
    """Register macOS launchd agent."""
    wrapper_dir = Path.home() / ".aither" / "bin"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = wrapper_dir / "claude-runner-wrapper.sh"

    try:
        wrapper_path.write_text(_get_wrapper_sh_content(repo_root, host, port), encoding="utf-8")
        wrapper_path.chmod(0o755)
    except OSError as e:
        print(f"Error: cannot write wrapper script: {e}", file=sys.stderr)
        return 1

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.aither.claude-runner.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        plist_content = _get_launchd_plist_content(str(wrapper_path), repo_root)
        plist_path.write_text(plist_content, encoding="utf-8")
    except OSError as e:
        print(f"Error: cannot write launchd plist: {e}", file=sys.stderr)
        return 1

    # Unload and reload
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    try:
        result = subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            _log.warning(f"launchctl load failed: {result.stderr}")
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.warning(f"Failed to load launchd agent: {e}")

    print(f"Claude runner registered with launchd. Host: {host}, Port: {port}")
    return 0


def cmd_unregister_claude(args: Any) -> int:
    """Unregister Claude runner service (stop and remove)."""
    force = getattr(args, "force", False)

    if sys.platform == "win32":
        return _unregister_windows(force)
    elif sys.platform == "darwin":
        return _unregister_macos(force)
    else:
        return _unregister_linux(force)


def _unregister_windows(force: bool) -> int:
    """Unregister Windows scheduled task."""
    task_name = "AitherOS-ClaudeRunner"

    ps_script = f"""
$TaskName = '{task_name}'

# Stop the task if running
Stop-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Unregister
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "Claude runner service unregistered."
"""

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
        )
        print(result.stdout)
        if result.returncode != 0 and "not found" not in result.stderr.lower():
            _log.warning(f"Unregister warning: {result.stderr}")
        return 0
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Error: failed to unregister task: {e}", file=sys.stderr)
        return 1


def _unregister_linux(force: bool) -> int:
    """Unregister Linux systemd --user unit."""
    commands = [
        ["systemctl", "--user", "stop", "aither-claude-runner.service"],
        ["systemctl", "--user", "disable", "aither-claude-runner.service"],
    ]

    for cmd in commands:
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass

    unit_path = Path.home() / ".config" / "systemd" / "user" / "aither-claude-runner.service"
    try:
        unit_path.unlink()
    except OSError:
        pass

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass

    print("Claude runner service unregistered.")
    return 0


def _unregister_macos(force: bool) -> int:
    """Unregister macOS launchd agent."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.aither.claude-runner.plist"

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    try:
        plist_path.unlink()
    except OSError:
        pass

    print("Claude runner service unregistered.")
    return 0


def cmd_status_claude(args: Any) -> int:
    """Report Claude runner service status."""
    if sys.platform == "win32":
        return _status_windows()
    elif sys.platform == "darwin":
        return _status_macos()
    else:
        return _status_linux()


def _status_windows() -> int:
    """Report Windows scheduled task status."""
    task_name = "AitherOS-ClaudeRunner"

    ps_script = f"""
$TaskName = '{task_name}'
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($null -eq $Task) {{
    Write-Host "Registered: no"
    return
}}

Write-Host "Registered: yes"
Write-Host "State: $($Task.State)"
if ($Task.State -eq "Ready" -or $Task.State -eq "Running") {{
    Write-Host "Running: yes"
}} else {{
    Write-Host "Running: no"
}}

if ($Task.LastRunTime) {{
    Write-Host "Last started: $($Task.LastRunTime)"
}}
if ($Task.NextRunTime) {{
    Write-Host "Next scheduled: $($Task.NextRunTime)"
}}
"""

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
        )
        print(result.stdout)
        return 0
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Error: failed to get status: {e}", file=sys.stderr)
        return 1


def _status_linux() -> int:
    """Report Linux systemd status."""
    unit_name = "aither-claude-runner.service"

    try:
        # Check if enabled
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled", unit_name],
            capture_output=True, timeout=5,
        )
        is_enabled = enabled.returncode == 0

        # Check if active
        active = subprocess.run(
            ["systemctl", "--user", "is-active", unit_name],
            capture_output=True, timeout=5,
        )
        is_active = active.returncode == 0

        print(f"Registered: {'yes' if is_enabled else 'no'}")
        print(f"Running: {'yes' if is_active else 'no'}")

        # Get more details
        status = subprocess.run(
            ["systemctl", "--user", "status", unit_name],
            capture_output=True, text=True, timeout=5,
        )
        if status.stdout:
            lines = status.stdout.split("\n")
            for line in lines[1:4]:
                if line.strip():
                    print(line.strip())
        return 0
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Error: failed to get status: {e}", file=sys.stderr)
        return 1


def _status_macos() -> int:
    """Report macOS launchd status."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.aither.claude-runner.plist"

    if not plist_path.exists():
        print("Registered: no")
        return 0

    print("Registered: yes")

    # Check if loaded
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.aither.claude-runner"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print("Running: yes")
        else:
            print("Running: no")
        return 0
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Error: failed to get status: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# CLI (adk claude ...)
# ---------------------------------------------------------------------------


def _client_base_and_token(args: Any) -> tuple[str, str]:
    url = getattr(args, "url", "") or os.environ.get(
        "AITHER_CLAUDE_RUNNER_URL", f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
    )
    token = resolve_token(getattr(args, "token", "") or "")
    return url.rstrip("/"), token


def cmd_claude(args: Any) -> int:
    """Dispatcher for `adk claude <serve|spawn|runs|kill|register|unregister|status>`."""
    sub = getattr(args, "claude_command", None)
    try:
        if sub == "serve":
            if getattr(args, "register", False):
                return cmd_register_claude(args)
            if getattr(args, "unregister", False):
                return cmd_unregister_claude(args)
            if getattr(args, "status", False):
                return cmd_status_claude(args)
            return serve(
                host=getattr(args, "host", "") or "",
                port=int(getattr(args, "port", 0) or 0),
                token=getattr(args, "token", "") or "",
            )
        if sub == "spawn":
            return _cmd_spawn(args)
        if sub == "runs":
            return _cmd_runs(args)
        if sub == "kill":
            return _cmd_kill(args)
        print("Usage: adk claude [serve|spawn|runs|kill]")
        return 1
    except (RunnerError, ScopeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _cmd_spawn(args: Any) -> int:
    import httpx

    task = getattr(args, "task", "") or ""
    task_file = getattr(args, "task_file", "") or ""
    if task_file:
        task = Path(task_file).read_text(encoding="utf-8")
    if not task.strip():
        print("Error: --task or --task-file is required", file=sys.stderr)
        return 1

    scope: dict[str, Any] = {
        "allowed_tools": [t for t in (getattr(args, "allow", "") or "").split(",") if t],
        "disallowed_tools": [t for t in (getattr(args, "deny", "") or "").split(",") if t],
        "allow_dangerous": bool(getattr(args, "allow_dangerous", False)),
        "system_prompt": getattr(args, "append_system_prompt", "") or "",
        "model": getattr(args, "model", "") or "",
        "cwd": getattr(args, "cwd", "") or "",
        "timeout_sec": int(getattr(args, "timeout", 0) or DEFAULT_TIMEOUT_SEC),
        "budget_usd": float(getattr(args, "budget_usd", 0) or 0),
        "account_profile": getattr(args, "account", "") or "",
        "resume_session_id": getattr(args, "resume", "") or "",
    }
    mcp_file = getattr(args, "mcp_config", "") or ""
    if mcp_file:
        scope["mcp_config"] = json.loads(Path(mcp_file).read_text(encoding="utf-8"))

    base, token = _client_base_and_token(args)
    headers = {"Authorization": f"Bearer {token}"}
    body = {"task": task, "goal_id": getattr(args, "goal", "") or "", "scope": scope}

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{base}/runs", json=body, headers=headers)
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} {resp.text}", file=sys.stderr)
            return 1
        rec = resp.json()
        run_id = rec["run_id"]
        print(f"run_id: {run_id}  (goal: {rec.get('goal_id') or '-'})")

        if getattr(args, "no_wait", False):
            return 0

        deadline = time.monotonic() + scope["timeout_sec"] + 60
        while time.monotonic() < deadline:
            time.sleep(3)
            status = client.get(f"{base}/runs/{run_id}", headers=headers).json()
            if status.get("status") not in ("queued", "running"):
                print(f"status: {status.get('status')}  "
                      f"turns: {status.get('num_turns')}  "
                      f"cost: ${status.get('total_cost_usd', 0):.4f}")
                if status.get("result_text"):
                    print("\n" + status["result_text"])
                if status.get("error"):
                    print(f"error: {status['error']}", file=sys.stderr)
                return 0 if status.get("status") == "completed" else 1
        print("Error: timed out waiting for run", file=sys.stderr)
        return 1


def _cmd_runs(args: Any) -> int:
    import httpx

    base, token = _client_base_and_token(args)
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(f"{base}/runs", headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} {resp.text}", file=sys.stderr)
            return 1
        rows = resp.json()
    if not rows:
        print("No runs.")
        return 0
    print(f"  {'run_id':<38} {'status':<10} {'turns':>5} {'cost':>9}  task")
    for r in rows:
        task = (r.get("task_redacted") or "").replace("\n", " ")[:48]
        print(f"  {r['run_id']:<38} {r['status']:<10} {r.get('num_turns', 0):>5} "
              f"${r.get('total_cost_usd', 0):>8.4f}  {task}")
    return 0


def _cmd_kill(args: Any) -> int:
    import httpx

    run_id = getattr(args, "run_id", "") or ""
    base, token = _client_base_and_token(args)
    with httpx.Client(timeout=15.0) as client:
        resp = client.delete(
            f"{base}/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}
        )
        if resp.status_code != 200:
            print(f"Error: {resp.status_code} {resp.text}", file=sys.stderr)
            return 1
        print(f"status: {resp.json().get('status')}")
    return 0
