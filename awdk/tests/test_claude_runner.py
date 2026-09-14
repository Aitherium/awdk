"""Tests for adk.claude_runner — scoped headless Claude subagent runner.

No test here ever invokes the real claude CLI: the integration tests use a fake
claude executable (a Python script emitting genuine stream-json shape) via
AITHER_CLAUDE_RUNNER_BIN, and all state roots are redirected with
AITHER_CLAUDE_RUNNER_ROOT so the real ~/.aither is never touched.
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

from adk.claude_runner import (
    ClaudeRunner,
    QueueFullError,
    RunnerError,
    RunRecord,
    RunScope,
    ScopeError,
    _record_view,
    clean_subprocess_env,
    create_app,
    redact,
    resolve_token,
)


@pytest.fixture
def runner_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "runner-root"
    monkeypatch.setenv("AITHER_CLAUDE_RUNNER_ROOT", str(root))
    return root


@pytest.fixture
def fake_claude(tmp_path: Path) -> str:
    """A fake claude binary that reads stdin and emits stream-json to stdout."""
    script = tmp_path / "fake_claude.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys
            task = sys.stdin.read()
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}))
            print(json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": "fake-done: " + task.strip()[:40],
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "total_cost_usd": 0.0012, "num_turns": 1, "session_id": "s-1",
            }))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        shim = tmp_path / "fake_claude.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(shim)
    shim = tmp_path / "fake_claude.sh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return str(shim)


def _scope(**overrides) -> RunScope:
    base = {"allowed_tools": ["Read"]}
    base.update(overrides)
    return RunScope.from_dict(base)


# ---------------------------------------------------------------------------
# Scope validation — fail-closed
# ---------------------------------------------------------------------------


class TestScopeValidation:
    def test_empty_allowlist_denied(self):
        with pytest.raises(ScopeError, match="allowed_tools"):
            RunScope.from_dict({"allowed_tools": []})

    def test_missing_scope_denied(self):
        with pytest.raises(ScopeError):
            RunScope.from_dict({})

    def test_illegal_tool_chars_denied(self):
        with pytest.raises(ScopeError, match="illegal"):
            RunScope.from_dict({"allowed_tools": ["Read;rm -rf"]})

    def test_stdio_mcp_server_denied(self):
        mcp = {"mcpServers": {"aitheros": {"type": "stdio", "command": "pwsh"}}}
        with pytest.raises(ScopeError, match="http/sse"):
            RunScope.from_dict({"allowed_tools": ["Read"], "mcp_config": mcp})

    def test_unlisted_gateway_host_denied(self):
        mcp = {"mcpServers": {"aitheros": {"type": "http", "url": "https://evil.example.com/mcp"}}}
        with pytest.raises(ScopeError, match="allowed gateway hosts"):
            RunScope.from_dict({"allowed_tools": ["Read"], "mcp_config": mcp})

    def test_plain_http_to_remote_denied(self):
        mcp = {"mcpServers": {"aitheros": {"type": "http", "url": "http://mcp.aitherium.com/mcp"}}}
        with pytest.raises(ScopeError, match="localhost"):
            RunScope.from_dict({"allowed_tools": ["Read"], "mcp_config": mcp})

    def test_unlisted_server_name_denied(self):
        """The server NAME allowlist, which the three tests above deliberately
        satisfy so they can reach the transport/host checks they exist for.

        Those three used to name their server 'evil'/'x', so this check fired
        FIRST and they passed on the wrong refusal -- the deny was real, but
        nothing proved that a stdio transport, an unlisted host or a plain-http
        URL is refused once the name is acceptable. A denial test that matches
        any denial proves only that something said no.
        """
        mcp = {"mcpServers": {"evil": {"type": "http",
                                       "url": "https://mcp.aitherium.com/mcp"}}}
        with pytest.raises(ScopeError, match="name not in"):
            RunScope.from_dict({"allowed_tools": ["Read"], "mcp_config": mcp})

    def test_allowed_gateway_ok(self):
        mcp = {"mcpServers": {"aitheros": {"type": "http", "url": "https://mcp.aitherium.com/mcp"}}}
        scope = RunScope.from_dict({"allowed_tools": ["Read"], "mcp_config": mcp})
        assert scope.mcp_config is not None

    def test_timeout_bounds(self):
        with pytest.raises(ScopeError, match="timeout"):
            RunScope.from_dict({"allowed_tools": ["Read"], "timeout_sec": 999999})

    def test_missing_cwd_denied(self):
        with pytest.raises(ScopeError, match="cwd"):
            RunScope.from_dict({"allowed_tools": ["Read"], "cwd": "Z:/definitely/not/here"})

    def test_bad_account_profile_denied(self):
        with pytest.raises(ScopeError, match="account_profile"):
            RunScope.from_dict({"allowed_tools": ["Read"], "account_profile": "../../etc"})


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    @pytest.mark.parametrize(
        "secret",
        [
            "sk-ant-abc123def456ghi789",
            "ghp_ABCDEF1234567890abcd",
            "AKIAIOSFODNN7EXAMPLE",
            "xoxb-1234-5678-example0",  # placeholder marker keeps secret_scan quiet
            "aither_sk_live_AbCdEf123456",
            "api_key = supersecretvalue123",
        ],
    )
    def test_secrets_redacted(self, secret: str):
        out = redact(f"before {secret} after")
        assert "[REDACTED]" in out
        assert secret not in out

    def test_plain_text_untouched(self):
        assert redact("just words, nothing secret") == "just words, nothing secret"


# ---------------------------------------------------------------------------
# Store + lifecycle (no HTTP)
# ---------------------------------------------------------------------------


class TestRunnerCore:
    def test_run_dir_traversal_rejected(self, runner_root: Path):
        runner = ClaudeRunner()
        with pytest.raises(RunnerError, match="invalid run id"):
            runner.store.run_dir("../../escape")

    def test_submit_and_complete_with_fake_claude(self, runner_root: Path, fake_claude: str):
        runner = ClaudeRunner(claude_bin=fake_claude)
        rec = runner.submit("say hello to CI", _scope(timeout_sec=60))
        assert rec.status in ("queued", "running")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rec = runner.get(rec.run_id)
            if rec.status not in ("queued", "running"):
                break
            time.sleep(0.2)

        assert rec.status == "completed", f"error={rec.error} exit={rec.exit_code}"
        assert rec.result_text.startswith("fake-done: say hello")
        assert rec.usage.get("input_tokens") == 10
        assert rec.total_cost_usd == pytest.approx(0.0012)
        assert rec.num_turns == 1
        # Scoping artifacts actually written and strict flags actually passed.
        run_dir = runner.store.run_dir(rec.run_id)
        argv = (run_dir / "argv.txt").read_text(encoding="utf-8")
        assert "--strict-mcp-config" in argv
        # Fail-closed default: ALL settings files excluded (empty sources arg).
        assert "--setting-sources ''" in argv or '--setting-sources ""' in argv
        settings = json.loads((run_dir / "settings.json").read_text(encoding="utf-8"))
        assert settings["permissions"]["allow"] == ["Read"]

    def test_kill_after_completion_does_not_clobber(
        self, runner_root: Path, fake_claude: str
    ):
        runner = ClaudeRunner(claude_bin=fake_claude)
        rec = runner.submit("quick task", _scope(timeout_sec=60))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            rec = runner.get(rec.run_id)
            if rec.status not in ("queued", "running"):
                break
            time.sleep(0.2)
        assert rec.status == "completed"
        after = runner.kill(rec.run_id)
        assert after.status == "completed"  # kill must not rewrite a terminal state

    def test_task_stored_redacted(self, runner_root: Path, fake_claude: str):
        runner = ClaudeRunner(claude_bin=fake_claude)
        rec = runner.submit("use key sk-ant-abc123def456ghi789 now", _scope(timeout_sec=60))
        assert "sk-ant-" not in rec.task_redacted
        assert "[REDACTED]" in rec.task_redacted

    def test_empty_task_denied(self, runner_root: Path):
        runner = ClaudeRunner()
        with pytest.raises(ScopeError, match="task"):
            runner.submit("   ", _scope())

    def test_queue_full_denied(self, runner_root: Path, monkeypatch: pytest.MonkeyPatch):
        runner = ClaudeRunner(max_concurrency=1, queue_max=1)
        # Never actually spawn: pretend starts succeed and stay running.
        monkeypatch.setattr(
            ClaudeRunner,
            "_start",
            lambda self, rec, scope: (
                setattr(rec, "status", "running"),
                setattr(rec, "pid", 999_999_999),
                self.store.save(rec),
            ),
        )
        runner.submit("a", _scope())
        runner.submit("b", _scope())
        with pytest.raises(QueueFullError):
            runner.submit("c", _scope())

    def test_orphan_recovery_marks_failed(self, runner_root: Path):
        runner = ClaudeRunner()
        rec = RunRecord(run_id="11111111-2222-3333-4444-555555555555")
        rec.status = "running"
        rec.pid = 999_999_999  # certainly dead
        runner.store.save(rec)

        recovered = ClaudeRunner()
        rec2 = recovered.get(rec.run_id)
        assert rec2.status == "failed"
        assert "orphaned" in rec2.error


# ---------------------------------------------------------------------------
# HTTP daemon — fail-closed auth
# ---------------------------------------------------------------------------


class TestHttpAuth:
    @pytest.fixture
    def client(self, runner_root: Path):
        from fastapi.testclient import TestClient

        runner = ClaudeRunner()
        app = create_app(runner, token="test-token-123")
        return TestClient(app)

    def test_tokenless_app_refused(self, runner_root: Path):
        with pytest.raises(RunnerError, match="fail-closed"):
            create_app(ClaudeRunner(), token="")

    def test_health_open(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_missing_token_401(self, client):
        assert client.get("/runs").status_code == 401

    def test_wrong_token_403(self, client):
        resp = client.get("/runs", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 403

    def test_right_token_200(self, client):
        resp = client.get("/runs", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 200

    def test_bad_scope_400(self, client):
        resp = client.post(
            "/runs",
            json={"task": "x", "scope": {"allowed_tools": []}},
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert resp.status_code == 400

    def test_unknown_run_404(self, client):
        resp = client.get(
            "/runs/11111111-2222-3333-4444-555555555555",
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API views + token persistence
# ---------------------------------------------------------------------------


class TestViewsAndToken:
    def test_mcp_auth_headers_never_echoed(self):
        rec = RunRecord(run_id="11111111-2222-3333-4444-555555555555")
        rec.scope = {
            "allowed_tools": ["Read"],
            "mcp_config": {
                "mcpServers": {
                    "aitheros": {
                        "type": "http",
                        "url": "https://mcp.aitherium.com/mcp",
                        "headers": {"Authorization": "Bearer aither_sk_live_SECRET"},
                    }
                }
            },
        }
        view = _record_view(rec)
        assert view["scope"]["mcp_config"] == {"mcpServers": ["aitheros"]}
        assert "aither_sk_live_SECRET" not in json.dumps(view)

    def test_token_generated_and_persisted(
        self, runner_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        monkeypatch.delenv("AITHER_CLAUDE_RUNNER_TOKEN", raising=False)
        monkeypatch.delenv("AITHER_INTERNAL_SECRET", raising=False)
        first = resolve_token()
        assert first and len(first) > 20
        assert (runner_root / "token").exists()
        assert first not in capsys.readouterr().out  # token value never echoed
        assert resolve_token() == first  # stable across calls

    def test_sync_runner_token_converges_tokenless_client(
        self, runner_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A daemon that boots from the shared secret must sync the file so a
        # client in a fresh shell (no env) resolves the SAME token, not a stale
        # self-generated one. This is the 403 the sync exists to prevent.
        from adk.claude_runner import sync_runner_token

        token_file = runner_root / "token"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("stale-self-generated", encoding="utf-8")

        monkeypatch.setenv("AITHER_INTERNAL_SECRET", "shared-secret-value")
        daemon_token = resolve_token()
        assert daemon_token == "shared-secret-value"
        sync_runner_token(daemon_token)
        assert token_file.read_text(encoding="utf-8").strip() == "shared-secret-value"

        monkeypatch.delenv("AITHER_INTERNAL_SECRET", raising=False)
        monkeypatch.delenv("AITHER_CLAUDE_RUNNER_TOKEN", raising=False)
        assert resolve_token() == "shared-secret-value"  # tokenless client matches

    def test_subprocess_env_strips_sensitive_vars(self):
        base = {
            "PATH": "/usr/bin",
            "USERPROFILE": "C:/Users/x",
            "AITHER_CLAUDE_RUNNER_TOKEN": "supersecret",
            "AITHER_INTERNAL_SECRET": "alsosecret",
            "GITHUB_TOKEN": "ghp_abc",
            "MY_API_KEY": "k",
            "DB_PASSWORD": "p",
            "ANTHROPIC_API_KEY": "sk-ant-x",
        }
        env = clean_subprocess_env(base)
        assert env["PATH"] == "/usr/bin"
        assert env["USERPROFILE"] == "C:/Users/x"
        for gone in (
            "AITHER_CLAUDE_RUNNER_TOKEN", "AITHER_INTERNAL_SECRET", "GITHUB_TOKEN",
            "MY_API_KEY", "DB_PASSWORD", "ANTHROPIC_API_KEY",
        ):
            assert gone not in env

    def test_env_token_wins(self, runner_root: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("AITHER_CLAUDE_RUNNER_TOKEN", "env-token")
        assert resolve_token() == "env-token"
        assert resolve_token("explicit") == "explicit"


class TestAccountAutoSelect:
    """submit()/_autoselect_account: account-less run auto-picks via the scheduler,
    best-effort (never fails the spawn on account selection)."""

    class _P:
        def __init__(self, name: str):
            self.name = name

    def test_picks_when_no_profile(self, runner_root: Path, monkeypatch: pytest.MonkeyPatch):
        runner = ClaudeRunner()
        import adk.claude_account_usage as usage
        import adk.claude_accounts as acc
        monkeypatch.setattr(acc.ClaudeAccountStore, "list_profiles",
                            lambda self: [TestAccountAutoSelect._P("alice"),
                                          TestAccountAutoSelect._P("bob")])
        monkeypatch.setattr(usage.UsageMonitor, "select_account", lambda self, names: names[0])
        scope = _scope()
        runner._autoselect_account(scope)
        assert scope.account_profile == "alice"

    def test_noop_when_explicit(self, runner_root: Path, monkeypatch: pytest.MonkeyPatch):
        runner = ClaudeRunner()
        scope = _scope(account_profile="chosen")
        runner._autoselect_account(scope)
        assert scope.account_profile == "chosen"

    def test_soft_fallback_no_profiles(self, runner_root: Path, monkeypatch: pytest.MonkeyPatch):
        runner = ClaudeRunner()
        import adk.claude_accounts as acc
        monkeypatch.setattr(acc.ClaudeAccountStore, "list_profiles", lambda self: [])
        scope = _scope()
        runner._autoselect_account(scope)
        assert scope.account_profile == ""  # falls back to default login

    def test_soft_fallback_all_cooldown(self, runner_root: Path, monkeypatch: pytest.MonkeyPatch):
        runner = ClaudeRunner()
        import adk.claude_account_usage as usage
        import adk.claude_accounts as acc
        monkeypatch.setattr(acc.ClaudeAccountStore, "list_profiles",
                            lambda self: [TestAccountAutoSelect._P("alice")])

        def _raise(self, names):
            raise usage.UsageMonitorError("all in cooldown")

        monkeypatch.setattr(usage.UsageMonitor, "select_account", _raise)
        scope = _scope()
        runner._autoselect_account(scope)
        assert scope.account_profile == ""  # never fails the spawn


# ---------------------------------------------------------------------------
# Resume — continuing an existing conversation
# ---------------------------------------------------------------------------

RESUME_ID = "11111111-2222-3333-4444-555555555555"


class TestResumeScope:
    def test_non_uuid_denied(self, tmp_path: Path):
        with pytest.raises(ScopeError, match="must be a UUID"):
            _scope(resume_session_id="not-a-uuid", cwd=str(tmp_path))

    def test_resume_without_cwd_denied(self):
        # The killer case: without cwd the run lands in the throwaway workdir,
        # whose project slug has no sessions, so claude would silently start a
        # NEW conversation that looks resumed.
        with pytest.raises(ScopeError, match="cwd is required"):
            _scope(resume_session_id=RESUME_ID)

    def test_resume_with_account_profile_denied(self, tmp_path: Path):
        with pytest.raises(ScopeError, match="account_profile cannot be combined"):
            _scope(resume_session_id=RESUME_ID, cwd=str(tmp_path), account_profile="alice")

    def test_valid_resume_scope_round_trips(self, tmp_path: Path):
        scope = _scope(resume_session_id=RESUME_ID, cwd=str(tmp_path))
        assert scope.resume_session_id == RESUME_ID
        assert RunScope.from_dict(scope.to_dict()).resume_session_id == RESUME_ID

    def test_absent_resume_is_the_default(self):
        assert _scope().resume_session_id == ""


class TestResumeArgv:
    def _argv(self, runner_root: Path, tmp_path: Path, **overrides) -> list[str]:
        runner = ClaudeRunner()
        scope = _scope(**overrides)
        rec = RunRecord(run_id="r-1", session_id=overrides.get("resume_session_id") or "fresh-id")
        return runner._build_argv(rec, scope, tmp_path)

    def test_fresh_run_creates_a_session(self, runner_root: Path, tmp_path: Path):
        argv = self._argv(runner_root, tmp_path)
        assert "--session-id" in argv
        assert "--resume" not in argv

    def test_resume_continues_instead_of_creating(self, runner_root: Path, tmp_path: Path):
        argv = self._argv(runner_root, tmp_path, resume_session_id=RESUME_ID, cwd=str(tmp_path))
        # --session-id would REJECT an id that already exists, so passing both
        # (or the wrong one) is the difference between continuing and erroring.
        assert "--session-id" not in argv
        assert argv[argv.index("--resume") + 1] == RESUME_ID

    def test_submit_threads_the_existing_session_id(
        self, runner_root: Path, tmp_path: Path, fake_claude: str,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("AITHER_CLAUDE_RUNNER_BIN", fake_claude)
        runner = ClaudeRunner()
        rec = runner.submit("continue", _scope(resume_session_id=RESUME_ID, cwd=str(tmp_path)))
        # A fresh uuid here would orphan every follow-up from its conversation.
        assert rec.session_id == RESUME_ID

    def test_resume_skips_account_autoselect(
        self, runner_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Auto-select would point CLAUDE_CONFIG_DIR at an isolated home with no
        # history — the resume would find nothing and start over.
        runner = ClaudeRunner()
        import adk.claude_accounts as acc

        def _boom(self):
            raise AssertionError("account auto-select must not run for a resume")

        monkeypatch.setattr(acc.ClaudeAccountStore, "list_profiles", _boom)
        scope = _scope(resume_session_id=RESUME_ID, cwd=str(tmp_path))
        runner._autoselect_account(scope)
        assert scope.account_profile == ""


# ---------------------------------------------------------------------------
# ntfy push on terminal state
# ---------------------------------------------------------------------------


class TestNtfyPush:
    def test_no_url_configured_is_a_noop(
        self, runner_root: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("AITHER_CLAUDE_RUNNER_NTFY_URL", raising=False)
        runner = ClaudeRunner()
        called: list[str] = []
        monkeypatch.setattr(runner, "_post_ntfy", lambda *a: called.append("x"))
        runner._notify_terminal(RunRecord(run_id="r-1"))
        assert called == []

    def test_push_carries_the_resume_id(
        self, runner_root: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        sent: dict = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _urlopen(req, timeout=0):
            sent["url"] = req.full_url
            sent["body"] = req.data.decode("utf-8")
            sent["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _Resp()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        monkeypatch.setenv("AITHER_CLAUDE_RUNNER_NTFY_TOKEN", "tok-123")
        runner = ClaudeRunner()
        rec = RunRecord(
            run_id="r-9", session_id=RESUME_ID, status="completed",
            result_text="all green", num_turns=3, total_cost_usd=0.05,
            scope={"cwd": str(Path.home() / "media-forge")},
        )
        runner._post_ntfy("https://ntfy.example.com/claude", rec)
        # The resume id is the whole point — without it the phone cannot reply.
        assert RESUME_ID in sent["body"]
        assert "all green" in sent["body"]
        assert sent["headers"]["authorization"] == "Bearer tok-123"

    def test_push_failure_never_raises(
        self, runner_root: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import urllib.request

        def _boom(req, timeout=0):
            raise OSError("network down")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        runner = ClaudeRunner()
        # A dead ntfy must not fail the run that just succeeded.
        runner._post_ntfy("https://ntfy.example.com/claude", RunRecord(run_id="r-1"))

    def test_secrets_are_redacted_before_leaving_the_host(
        self, runner_root: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        sent: dict = {}

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _urlopen(req, timeout=0):
            sent["body"] = req.data.decode("utf-8")
            return _Resp()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
        runner = ClaudeRunner()
        # Synthetic, never a real credential: built at runtime so no secret-shaped
        # literal exists in source for scanners (and humans) to trip over.
        leak = "sk-" + "ant-" + 'api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' + "A" * 8
        runner._post_ntfy(
            "https://ntfy.example.com/claude",
            RunRecord(run_id="r-1", result_text=f"token was {leak}"),
        )
        assert leak not in sent["body"]
